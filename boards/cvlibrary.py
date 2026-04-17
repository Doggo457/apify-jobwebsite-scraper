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

        Actor.log.info(f"[CV-Library] Total scraped: {len(all_jobs)}")
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
        # Try JSON-LD first
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            Actor.log.info(f"[CV-Library] Found {len(jsonld_jobs)} jobs via JSON-LD")
            return jsonld_jobs, len(jsonld_jobs) >= 20

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
                job["company"] = clean_text(el.get_text())

            el = card.select_one('[class*="location"]')
            if el:
                job["location"] = clean_text(el.get_text())

            el = card.select_one('[class*="salary"]')
            if el:
                sal = parse_salary(el.get_text())
                job["salary_raw"] = sal["raw"]
                job["salary_min"] = sal["min"]
                job["salary_max"] = sal["max"]
                job["salary_period"] = sal["period"]

            el = card.select_one('[class*="description"]') or card.select_one("p")
            if el:
                job["snippet"] = clean_text(el.get_text())[:500]

            el = card.select_one("time") or card.select_one('[class*="date"]')
            if el:
                job["date_posted"] = el.get("datetime", clean_text(el.get_text()))

            if job.get("title"):
                jobs.append(job)

        has_next = bool(
            soup.select_one('a[rel="next"]')
            or soup.select_one('a[class*="next"]')
        )
        return jobs, has_next