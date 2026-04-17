"""CWJobs.co.uk job board scraper - UK IT & Tech jobs (Totaljobs group)."""

import json
import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import BaseScraper, parse_salary, clean_text

BASE_URL = "https://www.cwjobs.co.uk"

JOB_TYPE_MAP = {
    "all": "",
    "permanent": "permanent",
    "temporary": "temporary",
    "contract": "contract",
    "part-time": "parttime",
}


class CWJobsScraper(BaseScraper):

    def __init__(self, client, delay: float = 1.5, **kwargs):
        super().__init__(client, delay, **kwargs)
        self.fetch_details = False

    @property
    def source_name(self) -> str:
        return "cwjobs.co.uk"

    def _build_url(self, keyword: str, location: str, job_type: str, salary_min: int | None, page: int) -> str:
        kw = quote_plus(keyword)
        loc = quote_plus(location)
        url = f"{BASE_URL}/jobs/{kw}/in-{loc}"

        params = []
        if page > 1:
            params.append(f"page={page}")
        cw_type = JOB_TYPE_MAP.get(job_type, "")
        if cw_type:
            params.append(f"employmenttype={cw_type}")
        if salary_min:
            params.append(f"salary={salary_min}")
        params.append("sortby=Date")

        if params:
            url += "?" + "&".join(params)
        return url

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        page = 1
        detail_count = 0
        MAX_DETAIL_FETCHES = 5

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, page)
            Actor.log.info(f"[CWJobs] Scraping page {page}: {url}")

            html = await self._get_html(url)
            if not html:
                break

            jobs, has_next = self._parse_search(html)
            if not jobs:
                Actor.log.info(f"[CWJobs] No jobs on page {page}, stopping.")
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

        Actor.log.info(f"[CWJobs] Total scraped: {len(all_jobs)}")
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
        for sel in ['[class*="description"]', '[itemprop="description"]']:
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
        # Collect from ALL sources, merge for maximum data coverage
        all_sources = []

        # 0. JS-extracted from Playwright (most robust — reads visible DOM text)
        js_jobs = self._process_js_extracted()
        if js_jobs:
            all_sources.append(("JS", js_jobs))
            Actor.log.info(f"[CWJobs] JS extraction: {len(js_jobs)} jobs with fields: "
                          f"{sum(1 for j in js_jobs if j.get('company'))}/{len(js_jobs)} company, "
                          f"{sum(1 for j in js_jobs if j.get('location'))}/{len(js_jobs)} location, "
                          f"{sum(1 for j in js_jobs if j.get('salary_raw'))}/{len(js_jobs)} salary")

        html_jobs, html_has_next = self._parse_html(html)
        if html_jobs:
            all_sources.append(("HTML", html_jobs))

        next_data = self._extract_next_data(html)
        if next_data:
            nd_jobs = self._parse_next_data(next_data)
            if nd_jobs:
                all_sources.append(("NEXT_DATA", nd_jobs))

        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            all_sources.append(("JSON-LD", jsonld_jobs))

        if not all_sources:
            return [], False

        all_sources.sort(key=lambda x: len(x[1]), reverse=True)
        best_name, best_jobs = all_sources[0]
        Actor.log.info(f"[CWJobs] Primary source: {best_name} ({len(best_jobs)} jobs)")

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
            Actor.log.info(f"[CWJobs] Merged {source_name} data")

        has_next = html_has_next or len(best_jobs) >= 20
        return best_jobs, has_next

    def _parse_next_data(self, data: dict) -> list[dict]:
        """Extract jobs from Next.js __NEXT_DATA__."""
        jobs = []
        try:
            props = data.get("props", {}).get("pageProps", {})
            results = (
                props.get("searchResults", {}).get("results", [])
                or props.get("jobs", [])
                or props.get("results", [])
                or props.get("data", {}).get("results", [])
            )
            for item in results:
                job = {"source": self.source_name}
                job["title"] = item.get("title", item.get("jobTitle", ""))
                job["company"] = item.get("company", item.get("companyName", item.get("advertiserName", "")))
                job["location"] = item.get("location", item.get("locationName", ""))
                job["url"] = urljoin(BASE_URL, item.get("url", item.get("jobUrl", "")))
                job["job_id"] = str(item.get("id", item.get("jobId", "")))
                job["date_posted"] = item.get("datePosted", item.get("postedDate", ""))

                sal = item.get("salary", item.get("salaryDescription", ""))
                if isinstance(sal, str) and sal:
                    parsed = parse_salary(sal)
                    job["salary_raw"] = parsed["raw"]
                    job["salary_min"] = parsed["min"]
                    job["salary_max"] = parsed["max"]
                    job["salary_period"] = parsed["period"]
                elif isinstance(sal, dict):
                    job["salary_min"] = sal.get("minimum") or sal.get("from")
                    job["salary_max"] = sal.get("maximum") or sal.get("to")
                    job["salary_raw"] = sal.get("label", sal.get("description", ""))
                    if not job["salary_raw"] and (job["salary_min"] or job["salary_max"]):
                        smin = job["salary_min"]
                        smax = job["salary_max"]
                        if smin and smax:
                            job["salary_raw"] = f"£{smin:,.0f} - £{smax:,.0f} per annum"
                        elif smin:
                            job["salary_raw"] = f"£{smin:,.0f}+ per annum"

                job["snippet"] = clean_text(item.get("description", item.get("snippet", "")))[:500]
                if job.get("title"):
                    jobs.append(job)
        except (KeyError, TypeError, AttributeError) as e:
            Actor.log.debug(f"[CWJobs] __NEXT_DATA__ parse error: {e}")
        return jobs

    def _parse_html(self, html: str) -> tuple[list[dict], bool]:
        """Fallback HTML parsing."""
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = (
            soup.select('[data-testid="job-card"]')
            or soup.select('article[class*="job"]')
            or soup.select('div[class*="SearchResult"]')
            or soup.select("article")
        )

        for card in cards:
            job = {"source": self.source_name}

            title_el = (
                card.select_one('a[data-testid="job-card-title"]')
                or card.select_one("h2 a")
                or card.select_one('a[href*="/job/"]')
            )
            if not title_el:
                continue

            job["title"] = clean_text(title_el.get_text())
            href = title_el.get("href", "")
            job["url"] = urljoin(BASE_URL, href)

            id_match = re.search(r"/job/(\d+)", href)
            if id_match:
                job["job_id"] = id_match.group(1)

            for sel in ['[data-testid="company-name"]', '[class*="company"]',
                        '[class*="employer"]', '[class*="recruiter"]']:
                el = card.select_one(sel)
                if el:
                    job["company"] = clean_text(el.get_text())
                    break

            for sel in ['[data-testid="job-card-location"]', '[class*="location"]',
                        '[class*="where"]']:
                el = card.select_one(sel)
                if el:
                    text = clean_text(el.get_text())
                    text = re.sub(r'^Location\s*:?\s*', '', text, flags=re.IGNORECASE).strip()
                    if text and len(text) > 1:
                        job["location"] = text
                        break

            for sel in ['[data-testid="job-card-salary"]', '[class*="salary"]',
                        '[class*="pay"]']:
                el = card.select_one(sel)
                if el:
                    sal = parse_salary(el.get_text())
                    if sal.get("min") or sal.get("max"):
                        job["salary_raw"] = sal["raw"]
                        job["salary_min"] = sal["min"]
                        job["salary_max"] = sal["max"]
                        job["salary_period"] = sal["period"]
                        break

            if not job.get("salary_raw"):
                sal = self._extract_salary_from_text(card.get_text())
                if sal and (sal.get("min") or sal.get("max")):
                    job["salary_raw"] = sal["raw"]
                    job["salary_min"] = sal["min"]
                    job["salary_max"] = sal["max"]
                    job["salary_period"] = sal["period"]

            el = card.select_one('[class*="description"]') or card.select_one("p")
            if el:
                job["snippet"] = clean_text(el.get_text())[:500]

            # Text-based fallbacks for date and employment type
            card_text = clean_text(card.get_text())
            if not job.get("date_posted"):
                date_match = re.search(
                    r'\b(\d+\s*(?:day|hour|week|month)s?\s*ago|today|yesterday)\b',
                    card_text, re.IGNORECASE
                )
                if date_match:
                    job["date_posted"] = date_match.group(1).strip()
            if not job.get("employment_type"):
                emp_match = re.search(
                    r'\b(permanent|contract|temporary|part[\s-]?time|full[\s-]?time|fixed[\s-]?term)\b',
                    card_text, re.IGNORECASE
                )
                if emp_match:
                    job["employment_type"] = emp_match.group(1).strip().title()

            if job.get("title"):
                jobs.append(job)

        has_next = bool(soup.select_one('a[aria-label="Next"]') or soup.select_one('a[rel="next"]'))
        return jobs, has_next