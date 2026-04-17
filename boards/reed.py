"""Reed.co.uk job board scraper."""

import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import BaseScraper, JobListing, parse_salary, clean_text

BASE_URL = "https://www.reed.co.uk"

JOB_TYPE_MAP = {
    "all": "",
    "permanent": "perm",
    "temporary": "temp",
    "contract": "contract",
    "part-time": "parttime",
}


class ReedScraper(BaseScraper):

    def __init__(self, client, delay: float = 1.5, **kwargs):
        super().__init__(client, delay, **kwargs)
        self.fetch_details = False  # Set by main.py, saves ~50% requests

    @property
    def source_name(self) -> str:
        return "reed.co.uk"

    def _build_url(self, keyword: str, location: str, job_type: str, salary_min: int | None, page: int) -> str:
        kw = quote_plus(keyword)
        loc = quote_plus(location)
        url = f"{BASE_URL}/jobs/{kw}-jobs-in-{loc}"

        params = []
        if page > 1:
            params.append(f"pageno={page}")
        reed_type = JOB_TYPE_MAP.get(job_type, "")
        if reed_type:
            params.append(f"employmenttype={reed_type}")
        if salary_min:
            params.append(f"salaryfrom={salary_min}")
        params.append("sortby=DisplayDate")

        if params:
            url += "?" + "&".join(params)
        return url

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        page = 1

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, page)
            Actor.log.info(f"[Reed] Scraping page {page}: {url}")

            html = await self._fetch(url)
            if not html:
                break

            jobs, has_next = self._parse_search(html)
            if not jobs:
                Actor.log.info(f"[Reed] No jobs on page {page}, stopping.")
                break

            for job in jobs:
                if len(all_jobs) >= max_results:
                    break

                # Fetch detail page only if enabled (expensive: 1 request per job)
                if self.fetch_details and job.get("url"):
                    detail = await self._parse_detail(job["url"])
                    job.update(detail)
                    await self._polite_delay()

                all_jobs.append(job)

            if not has_next or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[Reed] Total scraped: {len(all_jobs)}")
        return all_jobs

    def _parse_search(self, html: str) -> tuple[list[dict], bool]:
        # Try JSON-LD first (most structured data)
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            Actor.log.info(f"[Reed] Found {len(jsonld_jobs)} jobs via JSON-LD")
            return jsonld_jobs, len(jsonld_jobs) >= 20

        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = soup.select('article[data-qa="job-card"]')
        if not cards:
            cards = soup.select("article")
        if not cards:
            cards = soup.select('[class*="job-card"]')
        if not cards:
            cards = soup.select('[class*="job-result"]')

        for card in cards:
            job = {"source": self.source_name}

            # Title + URL
            title_el = (
                card.select_one('a[data-qa="job-card-title"]')
                or card.select_one("h2 a")
                or card.select_one("h3 a")
                or card.select_one('a[href*="/jobs/"]')
            )
            if not title_el:
                continue

            job["title"] = clean_text(title_el.get_text())
            href = title_el.get("href", "")
            job["url"] = urljoin(BASE_URL, href)

            id_match = re.search(r"/(\d+)", href)
            if id_match:
                job["job_id"] = id_match.group(1)

            # Company — try multiple selectors
            for sel in ['[data-qa="job-card-company"]', ".gtmJobListingPostedBy",
                        '[class*="company"]', '[class*="posted-by"]', '[class*="employer"]']:
                el = card.select_one(sel)
                if el:
                    job["company"] = clean_text(el.get_text())
                    break

            # Location — try multiple selectors
            for sel in ['[data-qa="job-card-location"]', ".job-result-location",
                        '[class*="location"]', '[class*="job-location"]']:
                el = card.select_one(sel)
                if el:
                    job["location"] = clean_text(el.get_text())
                    break

            # Salary — try multiple selectors
            for sel in ['[data-qa="job-card-salary"]', ".job-result-salary",
                        '[class*="salary"]', '[class*="pay"]']:
                el = card.select_one(sel)
                if el:
                    sal = parse_salary(el.get_text())
                    job["salary_raw"] = sal["raw"]
                    job["salary_min"] = sal["min"]
                    job["salary_max"] = sal["max"]
                    job["salary_period"] = sal["period"]
                    break

            # Snippet — try multiple selectors
            for sel in ['[data-qa="job-card-description"]', ".job-result-description",
                        '[class*="description"]', '[class*="snippet"]', "p"]:
                el = card.select_one(sel)
                if el:
                    text = clean_text(el.get_text())
                    if len(text) > 15:
                        job["snippet"] = text[:500]
                        break

            # Date
            el = card.select_one("time") or card.select_one('[data-qa="job-card-date"]') or card.select_one('[class*="date"]')
            if el:
                job["date_posted"] = el.get("datetime", clean_text(el.get_text()))

            # Employment type
            for sel in ['[class*="contract"]', '[class*="job-type"]', '[class*="employment"]']:
                el = card.select_one(sel)
                if el:
                    job["employment_type"] = clean_text(el.get_text())
                    break

            if job.get("title"):
                jobs.append(job)

        has_next = bool(soup.select_one('a[aria-label="Next"]') or soup.select("a[href*='pageno=']"))
        return jobs, has_next

    async def _parse_detail(self, url: str) -> dict:
        html = await self._fetch(url)
        if not html:
            return {}

        # Try JSON-LD on the detail page (most complete structured data)
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            # Return the first job's data as detail fields
            return jsonld_jobs[0]

        soup = BeautifulSoup(html, "html.parser")
        details = {}

        # Description
        for sel in ['[itemprop="description"]', ".description", '[class*="job-description"]',
                    '[class*="vacancy-description"]', "#job-description"]:
            el = soup.select_one(sel)
            if el:
                details["full_description"] = el.get_text(separator="\n", strip=True)
                if not details.get("snippet"):
                    details["snippet"] = clean_text(el.get_text())[:500]
                break

        # Location
        for sel in ['[itemprop="addressLocality"]', '[itemprop="jobLocation"]',
                    '[class*="location"]', '[data-qa*="location"]']:
            el = soup.select_one(sel)
            if el:
                details["location"] = clean_text(el.get_text())
                break

        # Salary
        for sel in ['[itemprop="baseSalary"]', '[class*="salary"]', '[data-qa*="salary"]']:
            el = soup.select_one(sel)
            if el:
                sal = parse_salary(el.get_text())
                details["salary_raw"] = sal["raw"]
                details["salary_min"] = sal["min"]
                details["salary_max"] = sal["max"]
                details["salary_period"] = sal["period"]
                break

        # Company
        for sel in ['[itemprop="hiringOrganization"]', '[itemprop="name"]',
                    '[class*="company"]', '[data-qa*="company"]']:
            el = soup.select_one(sel)
            if el:
                text = clean_text(el.get_text())
                if text and len(text) < 100:
                    details["company"] = text
                    break

        # Date
        el = soup.select_one('[itemprop="datePosted"]')
        if el:
            details["date_posted"] = el.get("content", el.get_text(strip=True))

        el = soup.select_one('[itemprop="validThrough"]')
        if el:
            details["valid_through"] = el.get("content", el.get_text(strip=True))

        # Employment type
        for sel in ['[itemprop="employmentType"]', '[class*="contract"]', '[class*="job-type"]']:
            el = soup.select_one(sel)
            if el:
                details["employment_type"] = clean_text(el.get_text())
                break

        return details