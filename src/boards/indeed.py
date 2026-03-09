"""Indeed.co.uk job board scraper."""

import asyncio
import json
import random
import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import BaseScraper, parse_salary, clean_text

BASE_URL = "https://uk.indeed.com"

JOB_TYPE_MAP = {
    "all": "",
    "permanent": "permanent",
    "temporary": "temporary",
    "contract": "contract",
    "part-time": "part-time",
}


class IndeedUKScraper(BaseScraper):
    """
    Indeed UK scraper.
    Uses XHR interception to capture job data from Indeed's internal API,
    plus fallback to mosaic data extraction and HTML parsing.
    """

    @property
    def source_name(self) -> str:
        return "indeed.co.uk"

    def _build_url(self, keyword: str, location: str, job_type: str,
                   salary_min: int | None, start: int = 0) -> str:
        params = [
            f"q={quote_plus(keyword)}",
            f"l={quote_plus(location)}",
            "sort=date",
        ]

        if start > 0:
            params.append(f"start={start}")

        indeed_type = JOB_TYPE_MAP.get(job_type, "")
        if indeed_type:
            params.append(f"jt={indeed_type}")

        if salary_min:
            params.append(f"salary={salary_min}")

        return f"{BASE_URL}/jobs?" + "&".join(params)

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        start = 0

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, start)
            Actor.log.info(f"[Indeed UK] Scraping offset {start}: {url}")

            # Try XHR interception first, then fall back to regular fetch
            jobs_from_xhr = await self._fetch_with_xhr_intercept(url)
            if jobs_from_xhr:
                Actor.log.info(f"[Indeed UK] Got {len(jobs_from_xhr)} jobs via XHR interception")
                for job in jobs_from_xhr:
                    if len(all_jobs) >= max_results:
                        break
                    all_jobs.append(job)
                if len(jobs_from_xhr) < 10:
                    break
            else:
                # Fall back to HTML parsing
                html = await self._fetch_browser(url, wait_selector='div[class*="job_seen_beacon"], div[data-jk]')
                if not html:
                    Actor.log.warning("[Indeed UK] Failed to fetch page")
                    break

                jobs, has_next = self._parse_search(html)
                if not jobs:
                    Actor.log.info(f"[Indeed UK] No jobs at offset {start}, stopping.")
                    break

                for job in jobs:
                    if len(all_jobs) >= max_results:
                        break
                    all_jobs.append(job)

                if not has_next:
                    break

            if len(all_jobs) >= max_results:
                break

            start += 10
            await self._polite_delay()

        # Strip empty string values from all jobs
        all_jobs = [{k: v for k, v in job.items() if v} for job in all_jobs]
        Actor.log.info(f"[Indeed UK] Total scraped: {len(all_jobs)}")
        return all_jobs

    async def _fetch_with_xhr_intercept(self, url: str) -> list[dict] | None:
        """Navigate to Indeed and intercept API/XHR responses containing job data."""
        if not self.browser:
            return None

        max_attempts = 3 if self.proxy_config else 1

        for attempt in range(max_attempts):
            if attempt > 0:
                await self._rotate_proxy()
                await asyncio.sleep(random.uniform(1.0, 3.0))

            result = await self._xhr_intercept_once(url)
            if result is not None:
                return result

            # If we got None and have proxy config, try again
            if self.proxy_config and attempt < max_attempts - 1:
                Actor.log.info(f"[Indeed UK] XHR attempt {attempt + 1} failed, rotating proxy...")
                continue
            break

        return None

    async def _xhr_intercept_once(self, url: str) -> list[dict] | None:
        """Single attempt at XHR interception."""
        from urllib.parse import urlparse
        captured_jobs = []

        try:
            context_opts = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "viewport": {"width": 1920, "height": 1080},
                "locale": "en-GB",
                "timezone_id": "Europe/London",
                "extra_http_headers": {
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                },
            }
            if self.proxy_url:
                parsed = urlparse(self.proxy_url)
                proxy_opts = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
                if parsed.username:
                    proxy_opts["username"] = parsed.username
                if parsed.password:
                    proxy_opts["password"] = parsed.password
                context_opts["proxy"] = proxy_opts
                context_opts["ignore_https_errors"] = True

            # Inject stealth JS
            context = await self.browser.new_context(**context_opts)
            await context.add_init_script(self.STEALTH_JS)

            try:
                page = await context.new_page()

                # Intercept responses for job data
                async def handle_response(response):
                    try:
                        resp_url = response.url
                        if any(p in resp_url for p in ["/api/", "/rpc/", "jobCards", "serp", "search"]):
                            ct = response.headers.get("content-type", "")
                            if "json" in ct or "javascript" in ct:
                                body = await response.text()
                                if len(body) > 200 and ("title" in body or "jobTitle" in body):
                                    try:
                                        data = json.loads(body)
                                        self._extract_jobs_from_api(data, captured_jobs)
                                    except json.JSONDecodeError:
                                        pass
                    except Exception:
                        pass

                page.on("response", handle_response)

                # Use 'load' instead of 'networkidle' - Indeed keeps connections open
                try:
                    await page.goto(url, wait_until="load", timeout=25000)
                except Exception as nav_err:
                    if self._is_proxy_error(nav_err):
                        raise
                    Actor.log.info("[Indeed UK] 'load' timed out, trying 'commit'...")
                    try:
                        await page.goto(url, wait_until="commit", timeout=15000)
                    except Exception:
                        pass
                await page.wait_for_timeout(4000)  # Let XHR requests complete

                # Also try to extract from the rendered page
                html = await page.content()
                if html:
                    page_jobs = self._extract_mosaic_data(html)
                    if page_jobs:
                        captured_jobs.extend(page_jobs)
                    if not captured_jobs:
                        # Try JSON-LD
                        jsonld = self._extract_jsonld_jobs(html)
                        if jsonld:
                            captured_jobs.extend(jsonld)
                    if not captured_jobs:
                        # Try HTML parsing
                        html_jobs, _ = self._parse_html(html)
                        if html_jobs:
                            captured_jobs.extend(html_jobs)

                    if not captured_jobs:
                        title = await page.title()
                        Actor.log.debug(f"[Indeed UK] Page title: '{title}', HTML length: {len(html)}")

                return captured_jobs if captured_jobs else None
            finally:
                await context.close()
        except Exception as e:
            if self._is_proxy_error(e):
                Actor.log.warning(f"[Indeed UK] Proxy error: {e}")
                return None  # Signal retry
            Actor.log.warning(f"[Indeed UK] XHR intercept failed: {e}")
            return None

    def _extract_jobs_from_api(self, data: dict | list, jobs: list):
        """Extract jobs from Indeed API response data."""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._extract_jobs_from_api(item, jobs)
            return

        if not isinstance(data, dict):
            return

        # Check if this dict looks like a job
        if "title" in data or "displayTitle" in data or "jobTitle" in data:
            title = data.get("title", data.get("displayTitle", data.get("jobTitle", "")))
            if title and isinstance(title, str) and len(title) > 3:
                job = {"source": self.source_name}
                job["title"] = title
                job["company"] = data.get("company", data.get("companyName", ""))
                job["location"] = data.get("formattedLocation", data.get("jobLocationCity", data.get("location", "")))
                jk = data.get("jobkey", data.get("jk", ""))
                if jk:
                    job["job_id"] = jk
                    job["url"] = f"{BASE_URL}/viewjob?jk={jk}"
                snippet = clean_text(
                    data.get("snippet", "")
                    or data.get("description", "")
                    or data.get("truncatedDescription", "")
                    or data.get("jobSnippet", "")
                )[:500]
                if snippet:
                    job["snippet"] = snippet

                date_val = (
                    data.get("formattedRelativeTime", "")
                    or data.get("pubDate", "")
                    or data.get("datePublished", "")
                    or data.get("formattedDate", "")
                )
                if date_val:
                    job["date_posted"] = date_val

                sal = data.get("formattedSalarySnippet", "")
                if not sal:
                    sal_obj = data.get("salarySnippet", {})
                    if isinstance(sal_obj, dict):
                        sal = sal_obj.get("text", "")
                if sal:
                    parsed = parse_salary(sal)
                    job["salary_raw"] = parsed["raw"]
                    job["salary_min"] = parsed["min"]
                    job["salary_max"] = parsed["max"]
                    job["salary_period"] = parsed["period"]

                jobs.append(job)
                return

        # Recurse into nested structures looking for job arrays
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                self._extract_jobs_from_api(val, jobs)

    def _parse_search(self, html: str) -> tuple[list[dict], bool]:
        # Try JSON-LD first
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            Actor.log.info(f"[Indeed UK] Found {len(jsonld_jobs)} jobs via JSON-LD")
            return jsonld_jobs, len(jsonld_jobs) >= 10

        # Try to extract from Indeed's mosaic data (embedded JSON)
        jobs = self._extract_mosaic_data(html)
        if jobs:
            Actor.log.info(f"[Indeed UK] Found {len(jobs)} jobs via mosaic data")
            return jobs, len(jobs) >= 10

        # Fallback to HTML parsing
        return self._parse_html(html)

    def _extract_mosaic_data(self, html: str) -> list[dict]:
        """Try to extract from Indeed's window.mosaic.providerData."""
        jobs = []
        # Indeed embeds job data in script tags
        match = re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*({.+?});\s*</script>', html, re.DOTALL)
        if not match:
            match = re.search(r'"jobCards"\s*:\s*(\[.+?\])', html, re.DOTALL)

        if match:
            try:
                data = json.loads(match.group(1))
                results = data.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results", [])
                if not results and isinstance(data, list):
                    results = data
                for item in results:
                    job = {"source": self.source_name}
                    job["title"] = item.get("title", item.get("displayTitle", ""))
                    job["company"] = item.get("company", item.get("companyName", ""))
                    job["location"] = item.get("formattedLocation", item.get("jobLocationCity", ""))
                    jk = item.get("jobkey", item.get("jk", ""))
                    if jk:
                        job["job_id"] = jk
                        job["url"] = f"{BASE_URL}/viewjob?jk={jk}"

                    sal = item.get("formattedSalarySnippet", item.get("salarySnippet", {}).get("text", ""))
                    if sal:
                        parsed = parse_salary(sal)
                        job["salary_raw"] = parsed["raw"]
                        job["salary_min"] = parsed["min"]
                        job["salary_max"] = parsed["max"]
                        job["salary_period"] = parsed["period"]

                    snippet = clean_text(
                        item.get("snippet", "")
                        or item.get("description", "")
                        or item.get("truncatedDescription", "")
                    )[:500]
                    if snippet:
                        job["snippet"] = snippet

                    date_val = (
                        item.get("formattedRelativeTime", "")
                        or item.get("pubDate", "")
                        or item.get("formattedDate", "")
                    )
                    if date_val:
                        job["date_posted"] = date_val

                    if job.get("title"):
                        jobs.append(job)
            except (json.JSONDecodeError, AttributeError, KeyError):
                pass
        return jobs

    def _parse_html(self, html: str) -> tuple[list[dict], bool]:
        """Fallback HTML parsing."""
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = (
            soup.select('div[class*="job_seen_beacon"]')
            or soup.select('div[class*="jobsearch-SerpJobCard"]')
            or soup.select('div[data-jk]')
            or soup.select('td[class*="resultContent"]')
        )

        for card in cards:
            job = {"source": self.source_name}

            title_el = (
                card.select_one('h2 a')
                or card.select_one('a[data-jk]')
                or card.select_one('a[class*="title"]')
            )
            if not title_el:
                title_span = card.select_one('h2 span') or card.select_one('[class*="jobTitle"] span')
                if title_span:
                    parent_a = title_span.find_parent("a")
                    if parent_a:
                        title_el = parent_a

            if not title_el:
                continue

            job["title"] = clean_text(title_el.get_text())
            href = title_el.get("href", "")
            job["url"] = urljoin(BASE_URL, href)

            jk = title_el.get("data-jk", "") or card.get("data-jk", "")
            if jk:
                job["job_id"] = jk

            el = card.select_one('[data-testid="company-name"]') or card.select_one('[class*="companyName"]')
            if el:
                job["company"] = clean_text(el.get_text())

            el = card.select_one('[data-testid="text-location"]') or card.select_one('[class*="companyLocation"]')
            if el:
                job["location"] = clean_text(el.get_text())

            el = card.select_one('[class*="salary-snippet"]') or card.select_one('[class*="salaryText"]')
            if el:
                sal = parse_salary(el.get_text())
                job["salary_raw"] = sal["raw"]
                job["salary_min"] = sal["min"]
                job["salary_max"] = sal["max"]
                job["salary_period"] = sal["period"]

            el = (
                card.select_one('[class*="job-snippet"]')
                or card.select_one('[class*="snippet"]')
                or card.select_one('[class*="description"]')
                or card.select_one("ul")
            )
            if el:
                snippet = clean_text(el.get_text())[:500]
                if snippet and len(snippet) > 10:
                    job["snippet"] = snippet

            el = card.select_one('[class*="date"]') or card.select_one("time") or card.select_one('[class*="visually-hidden"]')
            if el:
                date_val = el.get("datetime", clean_text(el.get_text()))
                if date_val and "ago" in date_val.lower() or re.match(r"\d", date_val):
                    job["date_posted"] = date_val

            if job.get("title"):
                jobs.append(job)

        has_next = bool(soup.select_one('a[aria-label="Next Page"]'))
        return jobs, has_next
