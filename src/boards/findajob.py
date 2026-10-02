"""GOV.UK Find a Job - now the DWP "Work Hub" at jobs.service.gov.uk.

The old findajob.dwp.gov.uk host redirects to the Work Hub home page, so the
previous scraper silently returned nothing. The new service is a Next.js app
whose search results are server-rendered with stable `data-testid` hooks.
It accepts a direct (unproxied) connection and needs no browser.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, parse_salary)

BASE_URL = "https://www.jobs.service.gov.uk"
JOB_TYPE_PARAM = {"permanent": "jobType=PERMANENT", "temporary": "jobType=TEMPORARY",
                  "contract": "jobType=CONTRACT", "part-time": "jobPattern=PART_TIME"}
SALARY_BANDS = ((100000, "100000-999999"), (80000, "80000-100000"), (60000, "60000-80000"),
                (30000, "30000-60000"), (10000, "10000-30000"))


class FindAJobScraper(BaseScraper):
    card_selector = 'div[data-testid^="searchResultCard-"]'
    page_size = 30

    @property
    def source_name(self) -> str:
        return "jobs.service.gov.uk"

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        params = [f"keywords={quote_plus(keyword)}", f"location={quote_plus(location)}",
                  f"resultsPerPage={self.page_size}"]
        if page > 1:
            params.append(f"pageNumber={page}")
        if job_type in JOB_TYPE_PARAM:
            params.append(JOB_TYPE_PARAM[job_type])
        if salary_min:
            # The site only filters by fixed bands; pick the band containing the
            # minimum (and every band above it). The sink enforces the exact cut.
            params += [f"salaryBand={band}" for floor, band in SALARY_BANDS if floor >= _band_floor(salary_min)]
        return f"{BASE_URL}/jobs/search?" + "&".join(params)

    def _parse_search(self, html: str, soup: BeautifulSoup) -> tuple[list[dict], bool]:
        jobs = []
        for card in soup.select('div[data-testid^="searchResultCard-"]'):
            a = card.select_one('a[data-testid^="jobTitle-"]') or card.select_one('h2 a, a[href^="/jobs/"]')
            if not a:
                continue
            href = a.get("href", "")
            job = {"source": self.source_name, "title": clean_text(a.get_text()),
                   "url": urljoin(BASE_URL, href.split("?")[0])}
            m = re.search(r"/jobs/([0-9a-f]{12,})", href)
            job["job_id"] = m.group(1) if m else (card.get("data-testid") or "").replace("searchResultCard-", "")

            emp = card.select_one('[data-testid="searchResultCardEmployer"]')
            if emp:
                text = clean_text(emp.get_text(" "))
                company, sep, loc = text.partition(" - ")
                if sep:
                    job["company"], job["location"] = clean_text(company), clean_text(loc)
                else:
                    job["company"] = text
            sal = card.select_one('p.govuk-\\!-font-weight-bold')
            if sal:
                apply_salary(job, parse_salary(sal.get_text(), self.default_currency))
            tags = [clean_text(t.get_text()) for t in card.select('[data-testid="searchResultsCardTags"] .govuk-tag')]
            if tags:
                job["work_mode"] = detect_work_mode(" ".join(tags))
                job["employment_type"] = detect_employment_type(", ".join(tags))
            desc = card.select_one('[data-testid="searchResultCardJobDescription"]')
            job["snippet"] = clean_text(desc.get_text(" "))[:500] if desc else ""
            for p in card.select("p"):
                t = p.get_text(" ", strip=True)
                if t.startswith("Added on"):
                    job["date_posted"] = t.replace("Added on", "").strip()
                    break
            if not job.get("work_mode"):
                job["work_mode"] = detect_work_mode(job.get("location"), job["snippet"])
            jobs.append(job)
        has_next = bool(soup.find("a", string=re.compile(r"Next page", re.IGNORECASE))
                        or soup.select_one('a[rel="next"][href*="pageNumber="]'))
        return jobs, has_next and bool(jobs)


def _band_floor(salary_min: int) -> int:
    for floor, _ in SALARY_BANDS:
        if salary_min >= floor:
            return floor
    return 0
