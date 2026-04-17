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

                if self.fetch_details and job.get("url"):
                    detail = await self._parse_detail(job["url"])
                    for k, v in detail.items():
                        if v and not job.get(k):
                            job[k] = v
                    await self._polite_delay()

                all_jobs.append(job)

            if not has_next or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[CWJobs] Total scraped: {len(all_jobs)}")
        return all_jobs

    async def _parse_detail(self, url: str) -> dict:
        html = await self._get_html(url)
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