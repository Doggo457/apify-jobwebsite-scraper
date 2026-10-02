"""USAJobs.gov via its official API.

The API requires an `Authorization-Key` header (free: email
recruiter-help@usajobs.gov or sign up at https://developer.usajobs.gov).
Without a key the API answers 401, so the board is skipped with a warning.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, apply_salary, clean_text, detect_work_mode

API_BASE = "https://data.usajobs.gov/api/search"
SCHEDULE_CODE = {"part-time": "2"}


class USAJobsScraper(BaseScraper):
    default_currency = "USD"
    page_size = 100

    def __init__(self, client, delay: float = 0.3, api_key: str = "", user_email: str = "", email: str = "", **kwargs):
        super().__init__(client, delay, **kwargs)
        self.api_key = (api_key or "").strip()
        self.user_email = (user_email or email or "").strip() or "jobs-board-scraper@apify.com"

    @property
    def source_name(self) -> str:
        return "usajobs.gov"

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None) -> list[dict]:
        self.stats["mode"] = "api"
        all_jobs: list[dict] = []
        if self.exhausted:
            return all_jobs
        if not self.api_key:
            Actor.log.warning("[USAJobs] skipped: no API key (free at https://developer.usajobs.gov/apirequest/)")
            self.exhausted = True
            return all_jobs
        headers = {"Host": "data.usajobs.gov", "User-Agent": self.user_email, "Authorization-Key": self.api_key}
        page = self._page
        while len(all_jobs) < max_results and page <= self.max_pages:
            # Always a full page: page N must mean the same window on every call
            params = [f"Keyword={quote_plus(keyword)}", f"ResultsPerPage={self.page_size}", f"Page={page}",
                      "SortField=opendate", "SortDirection=Desc"]
            if location and location.lower() not in ("remote", "usa", "united states"):
                params.append(f"LocationName={quote_plus(location)}")
            elif location.lower() == "remote":
                params.append("RemoteIndicator=True")
            if salary_min:
                params.append(f"RemunerationMinimumAmount={int(salary_min)}")
            if job_type in SCHEDULE_CODE:
                params.append(f"PositionScheduleTypeCode={SCHEDULE_CODE[job_type]}")
            data = await self._fetch_json(f"{API_BASE}?" + "&".join(params), headers=headers)
            if not data:
                break
            sr = data.get("SearchResult") or {}
            items = sr.get("SearchResultItems") or []
            if not items:
                self.exhausted = True
                break
            fresh = [self._parse(it) for it in items]
            all_jobs.extend(fresh)
            self.stats["pages"] += 1
            self.stats["jobs"] += len(fresh)
            Actor.log.info(f"[USAJobs] page {page}: +{len(fresh)} (run total {self.stats['jobs']}/{sr.get('SearchResultCountAll', '?')})")
            if self.on_page:
                await self.on_page(self.source_name, fresh)
            page += 1
            self._page = page
            if self.stats["jobs"] >= int(sr.get("SearchResultCountAll", 0) or 0):
                self.exhausted = True
                break
            await self._polite_delay()
        return all_jobs

    def _parse(self, it: dict) -> dict:
        d = it.get("MatchedObjectDescriptor") or {}
        details = (d.get("UserArea") or {}).get("Details") or {}
        duties = details.get("MajorDuties")
        snippet = duties[0] if isinstance(duties, list) and duties else (duties or d.get("QualificationSummary") or "")
        locs = d.get("PositionLocation") or []
        sched = d.get("PositionSchedule") or []
        job = {
            "source": self.source_name,
            "title": clean_text(d.get("PositionTitle", "")),
            "company": clean_text(d.get("OrganizationName") or d.get("DepartmentName", "")),
            "location": clean_text(locs[0].get("LocationName", "")) if locs else "",
            "snippet": clean_text(str(snippet))[:500],
            "employment_type": clean_text(sched[0].get("Name", "")) if sched else "",
            "date_posted": d.get("PublicationStartDate", ""),
            "valid_through": d.get("ApplicationCloseDate", ""),
            "url": d.get("PositionURI", ""),
            "job_id": str(d.get("PositionID") or it.get("MatchedObjectId", "")),
            "salary_currency": "USD",
            "category": ", ".join(c.get("Name", "") for c in (d.get("JobCategory") or [])[:3]),
        }
        rem = d.get("PositionRemuneration") or []
        if rem:
            r = rem[0]
            try:
                lo = float(r.get("MinimumRange") or 0) or None
                hi = float(r.get("MaximumRange") or 0) or None
            except (TypeError, ValueError):
                lo = hi = None
            if lo or hi:
                lo = lo or hi
                hi = hi or lo
                period = {"per year": "annum", "per hour": "hour", "per day": "day", "per month": "month"}.get(
                    str(r.get("Description", "Per Year")).lower(), "annum")
                apply_salary(job, {"raw": f"${lo:,.0f} - ${hi:,.0f} {r.get('Description', 'Per Year')}",
                                   "min": lo, "max": hi, "currency": "USD", "period": period})
        job["work_mode"] = "Remote" if str(details.get("RemoteIndicator", "")).lower() == "true" else detect_work_mode(job["location"], job["title"])
        return job
