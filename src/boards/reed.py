"""Reed.co.uk scraper.

Reed's search pages are server-rendered, so plain HTTP returns 25 fully
populated job cards per page with stable `data-qa` attributes. No browser.

If a Reed API key is supplied (free at https://www.reed.co.uk/developers)
we use the official JSON API instead: 100 results per request, includes a
description snippet, and never gets blocked.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, extract_salary_from_text, parse_salary, strip_tracking)

BASE_URL = "https://www.reed.co.uk"
API_URL = "https://www.reed.co.uk/api/1.0/search"

# Reed's search form uses boolean checkboxes: perm / temp / contract / partTime / fullTime
JOB_TYPE_PARAM = {"permanent": "perm=true", "temporary": "temp=true", "contract": "contract=true", "part-time": "partTime=true"}
API_TYPE_PARAM = {"permanent": "permanent", "temporary": "temp", "contract": "contract", "part-time": "partTime"}


class ReedScraper(BaseScraper):
    card_selector = 'article[data-qa="job-card"]'
    page_size = 25

    def __init__(self, client, delay: float = 0.8, api_key: str = "", **kwargs):
        super().__init__(client, delay, **kwargs)
        self.api_key = (api_key or "").strip()

    @property
    def source_name(self) -> str:
        return "reed.co.uk"

    # ── Official API path ────────────────────────────────────────────

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None):
        if self.api_key:
            return await self._search_api(keyword, location, max_results, job_type, salary_min)
        return await super().search(keyword, location, max_results, job_type, salary_min)

    async def _search_api(self, keyword, location, max_results, job_type, salary_min) -> list[dict]:
        all_jobs: list[dict] = []
        if self.exhausted:
            return all_jobs
        skip = getattr(self, "_skip", 0)
        take = 100
        self.stats["mode"] = "api"
        while len(all_jobs) < max_results and skip < 10_000:
            params = [f"keywords={quote_plus(keyword)}", f"locationName={quote_plus(location)}",
                      "distanceFromLocation=15", f"resultsToTake={min(take, max_results - len(all_jobs))}",
                      f"resultsToSkip={skip}"]
            if salary_min:
                params.append(f"minimumSalary={int(salary_min)}")
            if job_type in API_TYPE_PARAM:
                params.append(f"{API_TYPE_PARAM[job_type]}=true")
            url = f"{API_URL}?" + "&".join(params)
            try:
                r = await self.client.get(url, auth=(self.api_key, ""), headers={"Accept": "application/json"})
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                Actor.log.warning(f"[Reed API] request failed: {e}")
                break
            results = data.get("results", []) or []
            if not results:
                self.exhausted = True
                break
            fresh = [self._api_item(it) for it in results]
            all_jobs.extend(fresh)
            self.stats["pages"] += 1
            self.stats["jobs"] += len(fresh)
            Actor.log.info(f"[Reed API] +{len(fresh)} (run total {self.stats['jobs']}/{data.get('totalResults', '?')})")
            if self.on_page:
                await self.on_page(self.source_name, fresh)
            skip += len(results)
            self._skip = skip
            if skip >= int(data.get("totalResults", 0) or 0):
                self.exhausted = True
                break
        return all_jobs

    def _api_item(self, it: dict) -> dict:
        lo, hi = it.get("minimumSalary"), it.get("maximumSalary")
        cur = it.get("currency") or "GBP"
        job = {
            "source": self.source_name,
            "title": clean_text(it.get("jobTitle")),
            "company": clean_text(it.get("employerName")),
            "location": clean_text(it.get("locationName")),
            "snippet": clean_text(it.get("jobDescription"))[:500],
            "date_posted": it.get("date", ""),
            "valid_through": it.get("expirationDate", ""),
            "url": strip_tracking(it.get("jobUrl", "")),
            "job_id": str(it.get("jobId", "")),
            "salary_currency": cur,
        }
        if lo or hi:
            lo = float(lo or hi)
            hi = float(hi or lo)
            sym = "£" if cur == "GBP" else cur + " "
            raw = f"{sym}{lo:,.0f} per annum" if lo == hi else f"{sym}{lo:,.0f} - {sym}{hi:,.0f} per annum"
            apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": cur, "period": "annum"})
        job["work_mode"] = detect_work_mode(job["title"], job["snippet"])
        return job

    # ── HTTP path ────────────────────────────────────────────────────

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        slug = lambda s: quote_plus(re.sub(r"\s+", "-", s.strip().lower()))
        url = f"{BASE_URL}/jobs/{slug(keyword)}-jobs-in-{slug(location)}"
        params = ["sortby=DisplayDate"]
        if page > 1:
            params.append(f"pageno={page}")
        if job_type in JOB_TYPE_PARAM:
            params.append(JOB_TYPE_PARAM[job_type])
        if salary_min:
            params.append(f"salaryFrom={int(salary_min)}")
        return url + "?" + "&".join(params)

    def _parse_search(self, html: str, soup: BeautifulSoup) -> tuple[list[dict], bool]:
        jobs = self._parse_cards(soup)
        if not jobs:
            jobs = self._extract_jsonld_jobs(soup)
        has_next = bool(soup.select_one('a[aria-label="Next"], a[rel="next"], a[href*="pageno="]')) and len(jobs) >= 20
        return jobs, has_next

    def _parse_cards(self, soup: BeautifulSoup) -> list[dict]:
        jobs = []
        for card in soup.select('article[data-qa="job-card"]') or soup.select("article"):
            title_el = card.select_one('a[data-qa="job-card-title"]') or card.select_one('h2 a, h3 a, a[href*="/jobs/"]')
            if not title_el:
                continue
            job = {"source": self.source_name, "title": clean_text(title_el.get_text())}
            href = title_el.get("href", "")
            job["url"] = strip_tracking(urljoin(BASE_URL, href))
            jid = title_el.get("data-id") or re.sub(r"\D", "", card.get("data-id", "") or "")
            if not jid:
                m = re.search(r"/(\d{5,})", href)
                jid = m.group(1) if m else ""
            job["job_id"] = jid

            posted = card.select_one('[data-qa="job-posted-by"]')
            if posted:
                company_el = posted.select_one("a") or card.select_one('[data-element="recruiter"]')
                job["company"] = clean_text(company_el.get_text()) if company_el else ""
                text = clean_text(posted.get_text())
                m = re.match(r"(.+?)\s+by\s+", text, re.IGNORECASE)
                job["date_posted"] = m.group(1) if m else text
                if not job["company"]:
                    m = re.search(r"\bby\s+(.+)$", text, re.IGNORECASE)
                    job["company"] = clean_text(m.group(1)) if m else ""

            sal_el = card.select_one('[data-qa="job-metadata-salary"]')
            if sal_el:
                apply_salary(job, parse_salary(sal_el.get_text(), self.default_currency))
            loc_el = card.select_one('[data-qa="job-metadata-location"]')
            if loc_el:
                job["location"] = clean_text(loc_el.get_text())

            for li in card.select('[data-qa="job-metadata"] li'):
                if li.get("data-qa"):
                    continue
                text = clean_text(li.get_text())
                if not job.get("work_mode"):
                    wm = detect_work_mode(text)
                    if wm and len(text) < 20:
                        job["work_mode"] = wm
                        continue
                if not job.get("employment_type"):
                    et = detect_employment_type(text)
                    if et:
                        job["employment_type"] = et

            if not job.get("salary_raw"):
                apply_salary(job, extract_salary_from_text(card.get_text(" "), self.default_currency))
            if job.get("company", "").lower().startswith(("job hidden", "undo")):
                job["company"] = ""
            jobs.append(job)
        return jobs
