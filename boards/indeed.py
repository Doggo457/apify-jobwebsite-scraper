"""Indeed.co.uk job board scraper."""

import json
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
    Note: Indeed has very aggressive anti-bot measures. Even with residential
    proxies, results may be limited. Falls back to JSON-LD extraction when
    available.
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

            html = await self._fetch(url)
            if not html:
                Actor.log.warning("[Indeed UK] Failed to fetch - Indeed requires browser rendering or residential proxy")
                break

            jobs, has_next = self._parse_search(html)
            if not jobs:
                Actor.log.info(f"[Indeed UK] No jobs at offset {start}, stopping.")
                break

            for job in jobs:
                if len(all_jobs) >= max_results:
                    break
                all_jobs.append(job)

            if not has_next or len(all_jobs) >= max_results:
                break

            start += 10
            await self._polite_delay()

        Actor.log.info(f"[Indeed UK] Total scraped: {len(all_jobs)}")
        return all_jobs

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

                    job["snippet"] = clean_text(item.get("snippet", ""))[:500]
                    job["date_posted"] = item.get("formattedRelativeTime", "")

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

            el = card.select_one('[class*="job-snippet"]')
            if el:
                job["snippet"] = clean_text(el.get_text())[:500]

            if job.get("title"):
                jobs.append(job)

        has_next = bool(soup.select_one('a[aria-label="Next Page"]'))
        return jobs, has_next
