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

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, page)
            Actor.log.info(f"[CWJobs] Scraping page {page}: {url}")

            html = await self._fetch_browser(url, wait_selector='article, [data-testid="job-card"], a[href*="/job/"]')
            if not html:
                break

            jobs, has_next = self._parse_search(html)
            if not jobs:
                Actor.log.info(f"[CWJobs] No jobs on page {page}, stopping.")
                break

            for job in jobs:
                if len(all_jobs) >= max_results:
                    break

                # Extract company from URL slug if missing
                if not job.get("company") and job.get("url"):
                    job["company"] = self._company_from_url(job["url"])

                all_jobs.append(job)

            if not has_next or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[CWJobs] Total scraped: {len(all_jobs)}")
        return all_jobs

    def _company_from_url(self, url: str) -> str:
        """Extract company name from URL slug like /job/title/company-name-job123456."""
        match = re.search(r"/job/[^/]+/([^/]+)-job\d+", url)
        if match:
            slug = match.group(1)
            return slug.replace("-", " ").title()
        return ""

    async def _fetch_detail(self, url: str) -> dict:
        """Fetch job detail page and extract rich data."""
        html = await self._fetch_browser(url, wait_selector='[class*="description"], [class*="salary"], [itemprop]')
        if not html:
            return {}

        # Try JSON-LD from detail page
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            detail = jsonld_jobs[0]
            detail.pop("source", None)
            return detail

        soup = BeautifulSoup(html, "html.parser")
        details = {}

        el = (
            soup.select_one('[class*="company-name"]')
            or soup.select_one('[data-testid="company-name"]')
            or soup.select_one('[itemprop="hiringOrganization"] [itemprop="name"]')
        )
        if el:
            details["company"] = clean_text(el.get_text())

        el = soup.select_one('[class*="location"]') or soup.select_one('[itemprop="jobLocation"]')
        if el:
            loc = clean_text(el.get_text())
            if loc and loc.lower() not in ("location", "location:"):
                details["location"] = loc

        el = soup.select_one('[class*="salary"]') or soup.select_one('[itemprop="baseSalary"]')
        if el:
            sal_text = clean_text(el.get_text())
            if sal_text and sal_text.lower() not in ("salary", "salary:") and len(sal_text) > 3:
                sal = parse_salary(sal_text)
                details["salary_raw"] = sal["raw"]
                details["salary_min"] = sal["min"]
                details["salary_max"] = sal["max"]
                details["salary_period"] = sal["period"]

        el = soup.select_one('[class*="description"]') or soup.select_one('[itemprop="description"]')
        if el:
            details["snippet"] = clean_text(el.get_text())[:500]

        return details

    def _parse_search(self, html: str) -> tuple[list[dict], bool]:
        # Try JSON-LD first
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            Actor.log.info(f"[CWJobs] Found {len(jsonld_jobs)} jobs via JSON-LD")
            return jsonld_jobs, len(jsonld_jobs) >= 20

        # Try __NEXT_DATA__
        next_data = self._extract_next_data(html)
        if next_data:
            jobs = self._parse_next_data(next_data)
            if jobs:
                Actor.log.info(f"[CWJobs] Found {len(jobs)} jobs via __NEXT_DATA__")
                return jobs, len(jobs) >= 20

        # Fallback to HTML
        Actor.log.debug("[CWJobs] Falling back to HTML parsing")
        return self._parse_html(html)

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

            el = card.select_one('[data-testid="company-name"]') or card.select_one('[class*="company"]')
            if el:
                job["company"] = clean_text(el.get_text())

            el = card.select_one('[data-testid="job-card-location"]') or card.select_one('[class*="location"]')
            if el:
                job["location"] = clean_text(el.get_text())

            el = card.select_one('[data-testid="job-card-salary"]') or card.select_one('[class*="salary"]')
            if el:
                sal = parse_salary(el.get_text())
                job["salary_raw"] = sal["raw"]
                job["salary_min"] = sal["min"]
                job["salary_max"] = sal["max"]
                job["salary_period"] = sal["period"]

            el = card.select_one('[class*="description"]') or card.select_one("p")
            if el:
                job["snippet"] = clean_text(el.get_text())[:500]

            if job.get("title"):
                jobs.append(job)

        has_next = bool(soup.select_one('a[aria-label="Next"]') or soup.select_one('a[rel="next"]'))
        return jobs, has_next
