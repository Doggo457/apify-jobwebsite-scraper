"""CV-Library.co.uk job board scraper."""

import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import BaseScraper, parse_salary, clean_text

BASE_URL = "https://www.cv-library.co.uk"

JOB_TYPE_MAP = {
    "all": "",
    "permanent": "Permanent",
    "temporary": "Temporary",
    "contract": "Contract",
    "part-time": "Part+Time",
}


class CVLibraryScraper(BaseScraper):

    def __init__(self, client, delay: float = 1.5, **kwargs):
        super().__init__(client, delay, **kwargs)
        self.fetch_details = False

    @property
    def source_name(self) -> str:
        return "cv-library.co.uk"

    def _build_url(self, keyword: str, location: str, job_type: str, salary_min: int | None, page: int) -> str:
        params = [
            f"q={quote_plus(keyword)}",
            f"geo={quote_plus(location)}",
        ]

        if page > 1:
            params.append(f"page={page}")

        cvl_type = JOB_TYPE_MAP.get(job_type, "")
        if cvl_type:
            params.append(f"tempperm={cvl_type}")

        if salary_min:
            params.append(f"salarymin={salary_min}")
            params.append("salarytype=annum")

        params.append("us=1")

        return f"{BASE_URL}/search-jobs?" + "&".join(params)

    async def _fetch_with_retry(self, url: str, retries: int = 2) -> str | None:
        """Fetch with retry for flaky connections — uses browser if available."""
        for attempt in range(retries + 1):
            html = await self._get_html(url)
            if html:
                return html
            if attempt < retries:
                Actor.log.info(f"[CV-Library] Retry {attempt + 1}/{retries} for {url}")
                await self._polite_delay()
        return None

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        page = 1
        detail_count = 0
        MAX_DETAIL_FETCHES = 5

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, page)
            Actor.log.info(f"[CV-Library] Scraping page {page}: {url}")

            html = await self._fetch_with_retry(url)
            if not html:
                break

            jobs, has_next = self._parse_search(html)
            if not jobs:
                Actor.log.info(f"[CV-Library] No jobs on page {page}, stopping.")
                break

            for job in jobs:
                if len(all_jobs) >= max_results:
                    break

                needs_detail = not job.get("location") or not job.get("snippet")
                if self.fetch_details and job.get("url") and needs_detail and detail_count < MAX_DETAIL_FETCHES:
                    detail = await self._parse_detail(job["url"])
                    for k, v in detail.items():
                        if v and not job.get(k):
                            job[k] = v
                    detail_count += 1
                    await self._polite_delay()

                all_jobs.append(job)

            if not has_next or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[CV-Library] Total scraped: {len(all_jobs)}")
        return all_jobs

    async def _parse_detail(self, url: str) -> dict:
        html = await self._fetch_detail(url)
        if not html:
            return {}
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            return jsonld_jobs[0]
        soup = BeautifulSoup(html, "html.parser")
        details = {}
        for sel in ['[class*="job-description"]', '[class*="description"]', '[itemprop="description"]']:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 50:
                details["full_description"] = el.get_text(separator="\n", strip=True)
                details["snippet"] = clean_text(el.get_text())[:500]
                break
        for sel in ['[class*="company"]', '[itemprop="hiringOrganization"]']:
            el = soup.select_one(sel)
            if el:
                details["company"] = clean_text(el.get_text())
                break
        for sel in ['[class*="location"]', '[itemprop="jobLocation"]']:
            el = soup.select_one(sel)
            if el:
                details["location"] = clean_text(el.get_text())
                break
        for sel in ['[class*="salary"]', '[itemprop="baseSalary"]']:
            el = soup.select_one(sel)
            if el:
                sal = parse_salary(el.get_text())
                details["salary_raw"] = sal["raw"]
                details["salary_min"] = sal["min"]
                details["salary_max"] = sal["max"]
                details["salary_period"] = sal["period"]
                break
        return details

    def _parse_search(self, html: str) -> tuple[list[dict], bool]:
        # Collect from all sources, merge for maximum coverage
        all_sources = []

        # 0. JS-extracted from Playwright (most robust)
        js_jobs = self._process_js_extracted()
        if js_jobs:
            all_sources.append(("JS", js_jobs))
            Actor.log.info(f"[CV-Library] JS extraction: {len(js_jobs)} jobs")

        # Try JSON-LD
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            all_sources.append(("JSON-LD", jsonld_jobs))

        # HTML parsing
        html_jobs, html_has_next = self._parse_html_cards(html)
        if html_jobs:
            all_sources.append(("HTML", html_jobs))

        if not all_sources:
            return [], False

        # Use source with most jobs as base, merge others
        all_sources.sort(key=lambda x: len(x[1]), reverse=True)
        best_name, best_jobs = all_sources[0]
        Actor.log.info(f"[CV-Library] Primary source: {best_name} ({len(best_jobs)} jobs)")

        for source_name, source_jobs in all_sources[1:]:
            src_lookup = {}
            for j in source_jobs:
                if j.get("url"):
                    src_lookup[j["url"]] = j
                if j.get("job_id"):
                    src_lookup[j["job_id"]] = j
            for i, job in enumerate(best_jobs):
                match = src_lookup.get(job.get("url")) or src_lookup.get(job.get("job_id"))
                if not match and i < len(source_jobs):
                    match = source_jobs[i]
                if match:
                    for k, v in match.items():
                        if v and not job.get(k):
                            job[k] = v
            Actor.log.info(f"[CV-Library] Merged {source_name} data")

        has_next = html_has_next or len(best_jobs) >= 20
        return best_jobs, has_next

    def _parse_html_cards(self, html: str) -> tuple[list[dict], bool]:
        """Parse HTML cards from CV-Library search results."""
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = (
            soup.select('li[class*="search-result"]')
            or soup.select('div[class*="job-result"]')
            or soup.select('article[class*="job"]')
            or soup.select('li[data-job-id]')
            or soup.select('div[data-job-id]')
        )

        for card in cards:
            job = {"source": self.source_name}

            jid = card.get("data-job-id", "")
            if jid:
                job["job_id"] = jid

            title_el = (
                card.select_one('a[class*="job-title"]')
                or card.select_one("h2 a")
                or card.select_one("h3 a")
                or card.select_one('a[href*="/job/"]')
            )
            if not title_el:
                continue

            job["title"] = clean_text(title_el.get_text())
            href = title_el.get("href", "")
            job["url"] = urljoin(BASE_URL, href)

            if not job.get("job_id"):
                id_match = re.search(r"/job/(\d+)", href)
                if id_match:
                    job["job_id"] = id_match.group(1)

            el = card.select_one('[class*="company"]') or card.select_one('a[href*="/list-jobs/"]')
            if el:
                text = clean_text(el.get_text())
                text = re.sub(r'^Company\s*:?\s*', '', text, flags=re.IGNORECASE).strip()
                if text and len(text) > 1:
                    job["company"] = text

            # Location — try all matching elements, skip labels like "Location:"
            for el in card.select('[class*="location"]'):
                text = clean_text(el.get_text())
                text = re.sub(r'^Location\s*:?\s*', '', text, flags=re.IGNORECASE).strip()
                if text and len(text) > 2:
                    job["location"] = text
                    break

            # Salary — try all matching elements, skip labels like "Salary:"
            for el in card.select('[class*="salary"]'):
                text = clean_text(el.get_text())
                text = re.sub(r'^Salary\s*:?\s*', '', text, flags=re.IGNORECASE).strip()
                if text and len(text) > 2:
                    sal = parse_salary(text)
                    job["salary_raw"] = sal["raw"]
                    job["salary_min"] = sal["min"]
                    job["salary_max"] = sal["max"]
                    job["salary_period"] = sal["period"]
                    break

            el = card.select_one('[class*="description"]') or card.select_one("p")
            if el:
                # Use get_text() to strip HTML tags (e.g. <img> tags in CV-Library)
                text = clean_text(el.get_text())
                if text and len(text) > 10:
                    job["snippet"] = text[:500]

            el = card.select_one("time") or card.select_one('[class*="date"]')
            if el:
                job["date_posted"] = el.get("datetime", clean_text(el.get_text()))

            # ── Text-based fallbacks from card text ──
            card_text = clean_text(card.get_text())

            # Location fallback if company name was wrongly used or not found
            if not job.get("location") or job.get("location") == job.get("company"):
                if job.get("location") == job.get("company"):
                    job["location"] = ""  # Clear wrong location
                loc_match = re.search(
                    r'\b(London|Manchester|Birmingham|Leeds|Bristol|Liverpool|'
                    r'Sheffield|Glasgow|Edinburgh|Cardiff|Newcastle|Nottingham|'
                    r'Southampton|Oxford|Cambridge|Reading|Brighton|Remote|Hybrid'
                    r'|[A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})\b',
                    card_text, re.IGNORECASE
                )
                if loc_match:
                    job["location"] = loc_match.group(0).strip()

            # Date fallback
            if not job.get("date_posted"):
                date_match = re.search(
                    r'\b(\d+\s*(?:day|hour|week|month)s?\s*ago|today|yesterday)\b',
                    card_text, re.IGNORECASE
                )
                if date_match:
                    job["date_posted"] = date_match.group(1).strip()

            # Employment type fallback
            if not job.get("employment_type"):
                emp_match = re.search(
                    r'\b(permanent|contract|temporary|part[\s-]?time|full[\s-]?time)\b',
                    card_text, re.IGNORECASE
                )
                if emp_match:
                    job["employment_type"] = emp_match.group(1).strip().title()

            if job.get("title"):
                jobs.append(job)

        has_next = bool(
            soup.select_one('a[rel="next"]')
            or soup.select_one('a[class*="next"]')
        )
        return jobs, has_next