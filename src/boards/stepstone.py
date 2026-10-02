"""Shared scraper for StepStone-platform boards (Totaljobs, CWJobs, StepStone.de).

All three embed the full result list as JSON in
`window.__PRELOADED_STATE__["app-unifiedResultlist"]` with ISO dates, expiry
dates, work-from-home flags and snippets, so we read that first and only fall
back to the `data-at` card markup if the blob is missing.

The UK sites (Totaljobs, CWJobs) stall every search page past 4, and most
facet query parameters, for non-browser clients: probed from two networks,
pages 1-4 answer in ~1 s and page 5 never answers, whatever the session age.
The `employmenttype=` query parameter is silently ignored too. So instead of
paginating past 100 rows, the UK boards SWEEP a set of distinct searches
(contract-type path segment x sort order), four pages each, and merge them by
job id. StepStone.de serves deep pages normally and paginates plainly.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import (BaseScraper, LazySoup, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, extract_salary_from_text, parse_salary, strip_tracking)

# Contract type is a PATH segment on the UK sites (/jobs/contract/<kw>/in-<loc>);
# "temporary" has no segment and is left to the pipeline's filter.
JOB_TYPE_PATH = {"permanent": "permanent", "contract": "contract", "part-time": "part-time"}
SORT_DATE, SORT_SALARY, SORT_RELEVANCE = "sortby=Date", "sort=4", "sort=1"

_STATE_RE = re.compile(r'__PRELOADED_STATE__\["app-unifiedResultlist"\]\s*=\s*(\{.*?\});\s*\n', re.DOTALL)


class StepStoneScraper(BaseScraper):
    base_url = "https://www.totaljobs.com"
    card_selector = '[data-at="job-item"]'
    page_size = 25
    sweep = True            # UK sites: distinct searches x 4 pages (see module doc)
    sweep_depth = 4         # pages the UK edge serves per search before stalling

    def __init__(self, client, delay: float = 1.6, **kwargs):
        super().__init__(client, delay, **kwargs)
        self._page_count: int | None = None
        self._variant = 0       # sweep position: index into _variants()
        self._vpage = 1         # page within the current variant

    # ── URL building ─────────────────────────────────────────────────

    def _search_url(self, keyword, location, segment: str, sort: str, salary_min, page: int) -> str:
        slug = lambda s: quote_plus(re.sub(r"\s+", "-", s.strip().lower()))
        path = f"/jobs/{segment}/{slug(keyword)}" if segment else f"/jobs/{slug(keyword)}"
        url = f"{self.base_url}{path}/in-{slug(location)}"
        params = [sort]
        if page > 1:
            params.append(f"page={page}")
        if salary_min:
            params.append(f"salary={int(salary_min)}&salarytypeid=1")   # annual "at least" facet
        return url + "?" + "&".join(params)

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        # Plain pagination (used by StepStone.de and by the base loop).
        return self._search_url(keyword, location, JOB_TYPE_PATH.get(job_type, ""), SORT_DATE, salary_min, page)

    def _variants(self, job_type: str) -> list[tuple[str, str]]:
        """(path segment, sort) searches whose first pages are largely disjoint,
        newest-first ones first so a small request is still the freshest rows."""
        if job_type in JOB_TYPE_PATH:
            seg = JOB_TYPE_PATH[job_type]
            return [(seg, SORT_DATE), (seg, SORT_SALARY), (seg, SORT_RELEVANCE)]
        return [("", SORT_DATE), ("contract", SORT_DATE), ("permanent", SORT_SALARY), ("contract", SORT_SALARY),
                ("", SORT_RELEVANCE), ("part-time", SORT_DATE), ("permanent", SORT_RELEVANCE)]

    def _reset_for(self, keyword: str, location: str) -> None:
        if keyword != self._keyword or location != self._location:
            self._variant, self._vpage = 0, 1
        super()._reset_for(keyword, location)

    # ── Sweep loop (UK sites) ────────────────────────────────────────

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None) -> list[dict]:
        if not self.sweep:
            return await super().search(keyword, location, max_results, job_type, salary_min)
        all_jobs: list[dict] = []
        self._reset_for(keyword or "", location or "")
        if self.exhausted:
            return all_jobs
        variants = self._variants(job_type)
        pages_left = self.max_pages - self.stats["pages"]
        while len(all_jobs) < max_results and pages_left > 0:
            if self._variant >= len(variants):
                self.exhausted = True
                break
            segment, sort = variants[self._variant]
            url = self._search_url(keyword, location, segment, sort, salary_min, self._vpage)
            html = await self._get_html(url)
            jobs, has_next = self._parse_search(html, LazySoup(html)) if html else ([], False)
            pages_left -= 1
            fresh = []
            for job in jobs:
                key = job.get("job_id") or job.get("url") or job.get("title")
                if key in self._seen:
                    continue
                self._seen.add(key)
                job.setdefault("source", self.source_name)
                job.setdefault("salary_currency", self.default_currency)
                fresh.append(job)
            label = f"{segment or 'all'}/{sort.split('=')[1]}"
            if fresh:
                self.stats["pages"] += 1
                self.stats["jobs"] += len(fresh)
                all_jobs.extend(fresh)
                Actor.log.info(f"[{self.source_name}] {label} p{self._vpage}: +{len(fresh)} (run total {self.stats['jobs']}) via {self.stats['mode']}")
                if self.on_page:
                    await self.on_page(self.source_name, fresh)
            else:
                Actor.log.info(f"[{self.source_name}] {label} p{self._vpage}: nothing new, next search")
            # Advance: next page of this search while it has one and we are
            # under the depth cap; otherwise the next search variant.
            if fresh and has_next and self._vpage < self.sweep_depth:
                self._vpage += 1
            else:
                self._variant += 1
                self._vpage = 1
            if len(all_jobs) >= max_results:
                break
            await self._polite_delay()
        if self._variant >= len(variants):
            self.exhausted = True
        return all_jobs

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
            # workFromHome is a facet code on StepStone.de ('1' remote only,
            # '2' partly from home) and free text or empty on the UK sites.
            wfh = str(it.get("workFromHome") or "")
            job["work_mode"] = {"1": "Remote", "2": "Hybrid"}.get(wfh) or detect_work_mode(wfh, job["title"], job["snippet"])
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
