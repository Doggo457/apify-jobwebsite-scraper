"""Shared scraper for StepStone-platform boards (Totaljobs, CWJobs).

Both sites embed the full result list as JSON in
`window.__PRELOADED_STATE__["app-unifiedResultlist"]` with ISO dates, expiry
dates, work-from-home flags and snippets, so we read that first and only fall
back to the `data-at` card markup if the blob is missing.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, extract_salary_from_text, parse_salary, strip_tracking)

JOB_TYPE_PARAM = {"permanent": "permanent", "temporary": "temporary", "contract": "contract", "part-time": "parttime"}

_STATE_RE = re.compile(r'__PRELOADED_STATE__\["app-unifiedResultlist"\]\s*=\s*(\{.*?\});\s*\n', re.DOTALL)


class StepStoneScraper(BaseScraper):
    base_url = "https://www.totaljobs.com"
    card_selector = '[data-at="job-item"]'
    page_size = 25

    def __init__(self, client, delay: float = 1.6, **kwargs):
        super().__init__(client, delay, **kwargs)
        self._page_count: int | None = None

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        # NOTE: the JSON blob advertises an `of=25&action=paging_next` link but
        # the server ignores it and serves page 1 again; `page=N` is what the
        # HTML "Next" anchor uses and it is honoured.
        slug = lambda s: quote_plus(re.sub(r"\s+", "-", s.strip().lower()))
        url = f"{self.base_url}/jobs/{slug(keyword)}/in-{slug(location)}"
        params = ["sortby=Date"]
        if page > 1:
            params.append(f"page={page}")
        if job_type in JOB_TYPE_PARAM:
            params.append(f"employmenttype={JOB_TYPE_PARAM[job_type]}")
        if salary_min:
            params.append(f"salary={int(salary_min)}")
        return url + "?" + "&".join(params)

    def _parse_search(self, html: str, soup: BeautifulSoup) -> tuple[list[dict], bool]:
        jobs, has_next = self._parse_state(html)
        if jobs:
            return jobs, has_next
        jobs = self._parse_cards(soup)
        if not jobs:
            jobs = self._extract_jsonld_jobs(soup)
        nxt = soup.select_one('a[aria-label="Next"], a[rel="next"], a[data-at="pagination-next"]')
        return jobs, bool(nxt) and len(jobs) >= 20

    def _parse_state(self, html: str) -> tuple[list[dict], bool]:
        m = _STATE_RE.search(html)
        if not m:
            return [], False
        try:
            state = json.loads(m.group(1))
        except json.JSONDecodeError:
            return [], False
        sr = state.get("searchResults") or {}
        items = sr.get("items") or []
        jobs = []
        for it in items:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            job = {
                "source": self.source_name,
                "title": clean_text(it.get("title")),
                "company": clean_text(it.get("companyName")),
                "location": clean_text(it.get("location")),
                "url": strip_tracking(urljoin(self.base_url, it.get("url", ""))),
                "job_id": str(it.get("id", "")),
                "date_posted": it.get("datePosted") or it.get("publishFromDate") or "",
                "valid_through": it.get("publishToDate") or "",
                "snippet": clean_text(re.sub(r"<[^>]+>", " ", it.get("textSnippet") or ""))[:500],
            }
            sal = it.get("salary")
            if isinstance(sal, str):
                apply_salary(job, parse_salary(sal, self.default_currency))
            us = it.get("unifiedSalary")
            if isinstance(us, dict) and not job.get("salary_min"):
                lo, hi = us.get("min") or us.get("from"), us.get("max") or us.get("to")
                if lo or hi:
                    period = {"year": "annum", "annual": "annum", "day": "day", "hour": "hour", "month": "month"}.get(
                        str(us.get("period") or us.get("unit") or "year").lower(), "annum")
                    lo = float(lo or hi)
                    hi = float(hi or lo)
                    raw = sal if isinstance(sal, str) and sal else f"£{lo:,.0f} - £{hi:,.0f} per {period}"
                    apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": "GBP", "period": period})
            wfh = str(it.get("workFromHome") or "")
            job["work_mode"] = detect_work_mode(wfh, job["title"], job["snippet"])
            job["employment_type"] = detect_employment_type(" ".join(
                str(l.get("label", l)) if isinstance(l, dict) else str(l) for l in (it.get("labels") or [])) + " " + job["snippet"][:120])
            jobs.append(job)
        pag = sr.get("pagination") or {}
        page = int(pag.get("page", 1) or 1)
        page_count = int(pag.get("pageCount", 1) or 1)
        self._page_count = page_count
        return jobs, page < page_count

    def _parse_cards(self, soup: BeautifulSoup) -> list[dict]:
        jobs = []
        cards = soup.select('article[data-at="job-item"]') or soup.select('[data-testid="job-item"]') or soup.select("article")
        for card in cards:
            title_el = card.select_one('a[data-at="job-item-title"]') or card.select_one('h2 a, a[href*="/job/"]')
            if not title_el:
                continue
            job = {"source": self.source_name, "title": clean_text(title_el.get_text())}
            href = title_el.get("href", "")
            job["url"] = strip_tracking(urljoin(self.base_url, href))
            m = re.search(r"job(\d{6,})", (card.get("id") or "") + href)
            job["job_id"] = m.group(1) if m else ""
            el = card.select_one('[data-at="job-item-company-name"]')
            job["company"] = clean_text(el.get_text()) if el else ""
            el = card.select_one('[data-at="job-item-location"]')
            job["location"] = clean_text(el.get_text()) if el else ""
            el = card.select_one('[data-at="job-item-salary-info"]')
            if el:
                apply_salary(job, parse_salary(el.get_text(), self.default_currency))
            el = card.select_one('[data-at="jobcard-content"], [data-at="job-item-middle"]')
            job["snippet"] = clean_text(el.get_text(" ")).replace(" more", "")[:500] if el else ""
            el = card.select_one('[data-at="job-item-timeago"]')
            job["date_posted"] = clean_text(el.get_text()) if el else ""
            if not job.get("salary_raw"):
                apply_salary(job, extract_salary_from_text(job["snippet"], self.default_currency))
            job["work_mode"] = detect_work_mode(job["location"], job["snippet"])
            job["employment_type"] = detect_employment_type(job["snippet"][:160])
            jobs.append(job)
        return jobs
