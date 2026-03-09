"""GOV.UK Find a Job scraper - UK Government job service."""

import re
from urllib.parse import quote_plus, urljoin

import httpx
from apify import Actor
from bs4 import BeautifulSoup

from ..utils import BaseScraper, parse_salary, clean_text, make_headers

BASE_URL = "https://findajob.dwp.gov.uk"

JOB_TYPE_MAP = {
    "all": "",
    "permanent": "Permanent",
    "temporary": "Temporary",
    "contract": "Contract",
    "part-time": "",
}


class FindAJobScraper(BaseScraper):
    """
    GOV.UK Find a Job service scraper.
    Uses a direct (non-proxied) connection since the gov.uk site
    blocks proxy traffic but allows direct requests.
    """

    @property
    def source_name(self) -> str:
        return "findajob.dwp.gov.uk"

    def _build_url(self, keyword: str, location: str, job_type: str,
                   salary_min: int | None, page: int) -> str:
        params = [
            f"q={quote_plus(keyword)}",
            f"w={quote_plus(location)}",
            "pp=25",
            "sort=dt.rv",
        ]

        if page > 1:
            params.append(f"pg={page}")

        gov_type = JOB_TYPE_MAP.get(job_type, "")
        if gov_type:
            params.append(f"ct={quote_plus(gov_type)}")

        if job_type == "part-time":
            params.append("wh=Part+time")

        if salary_min:
            params.append(f"sb={salary_min}")

        return f"{BASE_URL}/search?" + "&".join(params)

    async def _fetch_direct(self, url: str) -> str | None:
        """Fetch without proxy - gov.uk blocks proxy traffic."""
        try:
            async with httpx.AsyncClient(
                headers=make_headers(),
                timeout=30.0,
            ) as direct_client:
                response = await direct_client.get(url, follow_redirects=True)
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as e:
            Actor.log.warning(f"[{self.source_name}] Failed to fetch {url}: {e}")
            return None

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        page = 1

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, page)
            Actor.log.info(f"[FindAJob] Scraping page {page}: {url}")

            # Try direct first (gov.uk blocks proxies), fall back to proxied client
            html = await self._fetch_direct(url)
            if not html:
                html = await self._fetch(url)
            if not html:
                break

            jobs, has_next = self._parse_search(html)
            if not jobs:
                Actor.log.info(f"[FindAJob] No jobs on page {page}, stopping.")
                break

            for job in jobs:
                if len(all_jobs) >= max_results:
                    break

                if job.get("url"):
                    detail_html = await self._fetch_direct(job["url"])
                    if detail_html:
                        detail = self._parse_detail_html(detail_html)
                        job.update(detail)
                    await self._polite_delay()

                all_jobs.append(job)

            if not has_next or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[FindAJob] Total scraped: {len(all_jobs)}")
        return all_jobs

    def _parse_search(self, html: str) -> tuple[list[dict], bool]:
        # Try JSON-LD first
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            Actor.log.info(f"[FindAJob] Found {len(jsonld_jobs)} jobs via JSON-LD")
            return jsonld_jobs, len(jsonld_jobs) >= 20

        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        # Find a Job uses a specific listing structure
        cards = (
            soup.select('div[class*="search-result"]')
            or soup.select('div[class*="job-result"]')
        )

        # Try broader: look for links to /details/
        if not cards:
            job_links = soup.select('a[href*="/details/"]')
            seen = set()
            for link in job_links:
                card = link.find_parent("div") or link.find_parent("li")
                if card and id(card) not in seen:
                    seen.add(id(card))
                    cards.append(card)

        for card in cards:
            job = {"source": self.source_name}

            title_el = (
                card.select_one('a[href*="/details/"]')
                or card.select_one("h2 a")
                or card.select_one("h3 a")
            )
            if not title_el:
                continue

            job["title"] = clean_text(title_el.get_text())
            href = title_el.get("href", "")
            job["url"] = urljoin(BASE_URL, href)

            id_match = re.search(r"/details/(\d+)", href)
            if id_match:
                job["job_id"] = id_match.group(1)

            el = card.select_one('[class*="company"]') or card.select_one('[class*="employer"]')
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
                text = clean_text(el.get_text())
                if len(text) > 20:
                    job["snippet"] = text[:500]

            el = card.select_one("time") or card.select_one('[class*="date"]')
            if el:
                job["date_posted"] = el.get("datetime", clean_text(el.get_text()))

            if job.get("title"):
                jobs.append(job)

        has_next = bool(
            soup.select_one('a[rel="next"]')
            or soup.select_one('[class*="next"]')
        )
        return jobs, has_next

    def _parse_detail_html(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        details = {}

        el = (
            soup.select_one('[class*="vacancy-description"]')
            or soup.select_one('[class*="job-description"]')
            or soup.select_one("#main-content")
        )
        if el:
            details["full_description"] = el.get_text(separator="\n", strip=True)

        el = soup.select_one('[class*="contract-type"]') or soup.select_one('[class*="employment"]')
        if el:
            details["employment_type"] = clean_text(el.get_text())

        el = soup.select_one('[class*="closing-date"]')
        if el:
            details["valid_through"] = clean_text(el.get_text())

        return details
