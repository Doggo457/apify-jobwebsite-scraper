"""CV-Library.co.uk scraper.

CV-Library sits behind a Cloudflare managed challenge that re-arms on new
navigations even for real browsers, so it is best-effort: HTTP first, browser
fallback, and `limit=100` so a successful page is worth as many rows as
possible. Card markup (verified Oct 2026) uses stable `data-qa`
hooks: job-card-N, job-title-link, job-card-posted-N (ISO datetime),
job-card-company-link-N, job-card-location-N, job-card-salary-N,
job-card-job-type-N.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, extract_salary_from_text, parse_salary, strip_tracking)

BASE_URL = "https://www.cv-library.co.uk"
JOB_TYPE_PARAM = {"permanent": "Permanent", "temporary": "Temporary", "contract": "Contract", "part-time": "Part+Time"}
_MILES = re.compile(r"\s*\(\d+(?:\.\d+)?\s*miles?\)\s*$", re.IGNORECASE)


class CVLibraryScraper(BaseScraper):
    card_selector = 'a[data-qa="job-title-link"]'
    page_size = 100

    @property
    def source_name(self) -> str:
        return "cv-library.co.uk"

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        params = [f"q={quote_plus(keyword)}", f"geo={quote_plus(location)}", "us=1", "order=date",
                  f"limit={self.page_size}"]
        if page > 1:
            params.append(f"page_number={page}")
        if job_type in JOB_TYPE_PARAM:
            params.append(f"tempperm={JOB_TYPE_PARAM[job_type]}")
        if salary_min:
            params += [f"salarymin={int(salary_min)}", "salarytype=annum"]
        return f"{BASE_URL}/search-jobs?" + "&".join(params)

    def _parse_search(self, html: str, soup: BeautifulSoup) -> tuple[list[dict], bool]:
        jobs = self._parse_cards(soup)
        if not jobs:
            jobs = self._extract_jsonld_jobs(soup)
        has_next = bool(soup.select_one('a[data-qa="next"], a[rel="next"], a[href*="page_number="]'))
        return jobs, has_next and bool(jobs)

    def _parse_cards(self, soup: BeautifulSoup) -> list[dict]:
        jobs = []
        cards = (soup.select('[itemprop="itemListElement"][data-qa^="job-card-"]')
                 or soup.select('div[class*="JobCard_job__"]')
                 or soup.select("li[data-job-id], div[data-job-id], article[data-job-id]"))
        for card in cards:
            title_el = card.select_one('a[data-qa="job-title-link"], h2 a, a[href*="/job/"]')
            if not title_el:
                continue
            href = title_el.get("href", "")
            job = {"source": self.source_name, "title": clean_text(title_el.get_text(" ")),
                   "url": strip_tracking(urljoin(BASE_URL, href))}
            m = re.search(r"/job/(\d+)", href)
            job["job_id"] = m.group(1) if m else str(card.get("data-job-id") or "")

            el = card.select_one('[data-qa^="job-card-company-link-"], [class*="company"], a[href*="/list-jobs/"]')
            job["company"] = re.sub(r"^Company\s*:?\s*", "", clean_text(el.get_text(" ")), flags=re.IGNORECASE) if el else ""
            el = card.select_one('[data-qa^="job-card-location-"], [class*="location"]')
            job["location"] = _MILES.sub("", clean_text(el.get_text(" "))) if el else ""
            el = card.select_one('[data-qa^="job-card-salary-"], [class*="salary"]')
            if el:
                apply_salary(job, parse_salary(el.get_text(" "), self.default_currency))
            el = card.select_one('[data-qa^="job-card-job-type-"], [class*="job-type"]')
            if el:
                job["employment_type"] = detect_employment_type(clean_text(el.get_text(" "))) or clean_text(el.get_text(" "))
            el = card.select_one('time[data-qa^="job-card-posted-"], time')
            if el:
                job["date_posted"] = el.get("datetime") or clean_text(el.get_text())
            el = card.select_one('p[class*="JobCard_descText"], [class*="description"]')
            job["snippet"] = clean_text(el.get_text(" "))[:500] if el else ""

            if not job.get("salary_raw"):
                apply_salary(job, extract_salary_from_text(job["snippet"], self.default_currency))
            if not job.get("employment_type"):
                job["employment_type"] = detect_employment_type(job["snippet"][:160])
            job["work_mode"] = detect_work_mode(job["location"], job["snippet"])
            jobs.append(job)
        return jobs
