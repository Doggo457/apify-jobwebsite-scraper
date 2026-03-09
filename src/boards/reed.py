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

                # Fetch detail page for richer data
                if job.get("url"):
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
        # Try JSON-LD first (most reliable)
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            Actor.log.info(f"[Reed] Found {len(jsonld_jobs)} jobs via JSON-LD")
            return jsonld_jobs, len(jsonld_jobs) >= 20

        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = (
            soup.select('article[data-qa="job-card"]')
            or soup.select('article[class*="job"]')
            or soup.select("article")
        )

        for card in cards:
            job = {"source": self.source_name}

            # Title + URL - try multiple selectors
            title_el = (
                card.select_one('a[data-qa="job-card-title"]')
                or card.select_one('h2 a[href*="/jobs/"]')
                or card.select_one('h3 a[href*="/jobs/"]')
                or card.select_one("h2 a")
                or card.select_one("h3 a")
                or card.select_one('a[href*="/jobs/"]')
            )
            if not title_el:
                continue

            job["title"] = clean_text(title_el.get_text())
            href = title_el.get("href", "")
            job["url"] = urljoin(BASE_URL, href)

            # Extract job ID from URL
            id_match = re.search(r"/(\d+)", href)
            if id_match:
                job["job_id"] = id_match.group(1)

            # Company - broad selectors
            el = (
                card.select_one('[data-qa="job-card-company"]')
                or card.select_one(".gtmJobListingPostedBy")
                or card.select_one('[class*="company"]')
                or card.select_one('[class*="posted-by"]')
                or card.select_one('[class*="employer"]')
            )
            if el:
                company = clean_text(el.get_text())
                # Remove "by " prefix if present
                company = re.sub(r"^by\s+", "", company, flags=re.IGNORECASE)
                job["company"] = company

            # Location - broad selectors
            el = (
                card.select_one('[data-qa="job-card-location"]')
                or card.select_one('[class*="location"]')
                or card.select_one('[class*="job-location"]')
            )
            if el:
                loc = clean_text(el.get_text())
                # Skip if it's just a label
                if loc and loc.lower() not in ("location", "location:"):
                    job["location"] = loc

            # Salary - broad selectors
            el = (
                card.select_one('[data-qa="job-card-salary"]')
                or card.select_one('[class*="salary"]')
                or card.select_one('[class*="job-salary"]')
            )
            if el:
                sal_text = clean_text(el.get_text())
                if sal_text and sal_text.lower() not in ("salary", "salary:"):
                    sal = parse_salary(sal_text)
                    job["salary_raw"] = sal["raw"]
                    job["salary_min"] = sal["min"]
                    job["salary_max"] = sal["max"]
                    job["salary_period"] = sal["period"]

            # Snippet
            el = (
                card.select_one('[data-qa="job-card-description"]')
                or card.select_one('[class*="description"]')
                or card.select_one('[class*="snippet"]')
            )
            if el:
                snippet = clean_text(el.get_text())
                if len(snippet) > 20:
                    job["snippet"] = snippet[:500]

            # Date
            el = (
                card.select_one("time")
                or card.select_one('[data-qa="job-card-date"]')
                or card.select_one('[class*="date"]')
            )
            if el:
                date_val = el.get("datetime", clean_text(el.get_text()))
                if date_val and date_val.lower() not in ("date", "date:"):
                    job["date_posted"] = date_val

            if job.get("title"):
                jobs.append(job)

        has_next = bool(soup.select_one('a[aria-label="Next"]') or soup.select("a[href*='pageno=']"))
        return jobs, has_next

    async def _parse_detail(self, url: str) -> dict:
        html = await self._fetch(url)
        if not html:
            return {}

        # Try JSON-LD from detail page first
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            detail = jsonld_jobs[0]
            # Remove source key as it will be merged
            detail.pop("source", None)
            return detail

        soup = BeautifulSoup(html, "html.parser")
        details = {}

        # Description
        el = (
            soup.select_one('[itemprop="description"]')
            or soup.select_one('[class*="description"]')
            or soup.select_one('[class*="job-detail"]')
            or soup.select_one("#job-description")
        )
        if el:
            details["full_description"] = el.get_text(separator="\n", strip=True)

        # Location from detail page
        el = (
            soup.select_one('[itemprop="jobLocation"]')
            or soup.select_one('[class*="location"]')
        )
        if el:
            loc = clean_text(el.get_text())
            if loc and loc.lower() not in ("location", "location:"):
                details["location"] = loc

        # Salary from detail page
        el = (
            soup.select_one('[itemprop="baseSalary"]')
            or soup.select_one('[class*="salary"]')
        )
        if el:
            sal_text = clean_text(el.get_text())
            if sal_text and sal_text.lower() not in ("salary", "salary:") and len(sal_text) > 3:
                sal = parse_salary(sal_text)
                details["salary_raw"] = sal["raw"]
                details["salary_min"] = sal["min"]
                details["salary_max"] = sal["max"]
                details["salary_period"] = sal["period"]

        # Date posted
        el = soup.select_one('[itemprop="datePosted"]')
        if el:
            details["date_posted"] = el.get("content", el.get_text(strip=True))

        el = soup.select_one('[itemprop="validThrough"]')
        if el:
            details["valid_through"] = el.get("content", el.get_text(strip=True))

        el = soup.select_one('[itemprop="employmentType"]')
        if el:
            details["employment_type"] = clean_text(el.get_text())

        return details
