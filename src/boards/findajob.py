"""GOV.UK Find a Job (the DWP "Work Hub" at jobs.service.gov.uk).

The old findajob.dwp.gov.uk host 302s every path to the new home page and
drops the query, so this scraper targets the new service directly:

  search: /jobs/search?keywords=..&location=..&resultsPerPage=50&pageNumber=N
  cards:  div[data-testid^="searchResultCard-"] with stable data-testid
          children (jobTitle-*, searchResultCardEmployer,
          searchResultsCardTags, searchResultCardJobDescription)

The service's WAF (Akamai) 403s datacenter IPs and serves a "Something went
wrong" failover page to inconsistent header sets, so main.py hands this board
the residential-proxied client with the full Chrome header set. Fetching is
tiered, cheapest first: proxied HTTP, then up to two rotated residential
sessions over HTTP, then the browser (real Chrome TLS) as the last resort.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, parse_salary)

BASE_URL = "https://www.jobs.service.gov.uk"
JOB_TYPE_PARAM = {"permanent": "jobType=PERMANENT", "temporary": "jobType=TEMPORARY",
                  "contract": "jobType=CONTRACT", "part-time": "jobPattern=PART_TIME"}
SALARY_BANDS = ((100000, "100000-999999"), (80000, "80000-100000"), (60000, "60000-80000"),
                (30000, "30000-60000"), (10000, "10000-30000"))


class FindAJobScraper(BaseScraper):
    card_selector = 'div[data-testid^="searchResultCard-"]'
    page_size = 50

    @property
    def source_name(self) -> str:
        return "jobs.service.gov.uk"

    def page_is_blocked(self, html: str) -> bool:
        if not html:
            return False
        head = html[:4000]
        return "waf_failover" in head or "Something went wrong" in head or "Access Denied" in head

    http_retry_rotations = 2   # one flagged exit IP must not kill the board

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        params = [f"keywords={quote_plus(keyword)}", f"resultsPerPage={self.page_size}"]
        if location:
            params.append(f"location={quote_plus(location)}")
        if page > 1:
            params.append(f"pageNumber={page}")
        if job_type in JOB_TYPE_PARAM:
            params.append(JOB_TYPE_PARAM[job_type])
        if salary_min:
            # The site filters by fixed bands: select every band from the one
            # containing the minimum upwards. The pipeline enforces the exact cut.
            floor = next((f for f, _ in SALARY_BANDS if salary_min >= f), 0)
            params += [f"salaryBand={band}" for f, band in SALARY_BANDS if f >= floor]
        return f"{BASE_URL}/jobs/search?" + "&".join(params)

    def _parse_search(self, html: str, soup) -> tuple[list[dict], bool]:
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
                spans = emp.find_all("span")
                if len(spans) >= 2:
                    job["company"] = clean_text(spans[0].get_text())
                    job["location"] = clean_text(spans[1].get_text()).lstrip("-–— ").strip()
                else:
                    text = clean_text(emp.get_text(" "))
                    company, sep, loc = text.partition(" - ")
                    job["company"] = clean_text(company)
                    if sep:
                        job["location"] = clean_text(loc)
            for p in card.select('p[class*="font-weight-bold"]'):
                text = clean_text(p.get_text())
                if text and re.search(r"[£$€]\s*\d", text):
                    apply_salary(job, parse_salary(text, self.default_currency))
                    break
            tags = [clean_text(t.get_text()) for t in card.select('[data-testid="searchResultsCardTags"] span')]
            if tags:
                job["work_mode"] = detect_work_mode(" ".join(tags))
                job["employment_type"] = detect_employment_type(", ".join(tags))
            desc = card.select_one('[data-testid="searchResultCardJobDescription"]')
            job["snippet"] = clean_text(desc.get_text(" "))[:500] if desc else ""
            dm = re.search(r"Added on\s+(\d{1,2}\s+\w{3,9}\s+\d{4})", card.get_text(" "))
            if dm:
                job["date_posted"] = dm.group(1)
            if not job.get("work_mode"):
                job["work_mode"] = detect_work_mode(job.get("location"), job["snippet"])
            jobs.append(job)
        has_next = bool(soup.find("a", string=re.compile(r"Next page", re.IGNORECASE))
                        or soup.select_one('a[rel="next"][href*="pageNumber="]'))
        return jobs, has_next and bool(jobs)
