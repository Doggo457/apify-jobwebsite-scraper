"""Seek.com.au via the JSON search API that powers the site (no key needed).

GET https://www.seek.com.au/api/jobsearch/v5/search
    ?siteKey=AU-Main&sourcesystem=houston&locale=en-AU&keywords=..&where=..
    &sortmode=ListedDate&pageSize=100&page=N[&worktype=..][&salaryrange=..]

Returns up to 100 listings per page with title, advertiser, locations,
salaryLabel, teaser, bulletPoints, workTypes, classifications and listingDate.
Job URL is https://www.seek.com.au/job/<id>. Answers plain HTTP from
datacenter IPs, so this board runs on the direct (unproxied) client.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from apify import Actor

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, parse_salary)

API_URL = "https://www.seek.com.au/api/jobsearch/v5/search"
# Seek work-type facet ids: 242 full time, 243 part time, 244 contract/temp, 245 casual
WORKTYPE_PARAM = {"permanent": "242", "part-time": "243", "contract": "244", "temporary": "244%2C245"}
_REMOTE_WORDS = ("remote", "work from home", "anywhere")


class SeekScraper(BaseScraper):
    default_currency = "AUD"
    page_size = 100
    max_pages = 20

    def __init__(self, client, delay: float = 0.4, **kwargs):
        super().__init__(client, delay, **kwargs)

    @property
    def source_name(self) -> str:
        return "seek.com.au"

    def _build_url(self, keyword: str, location: str, job_type: str, salary_min, page: int) -> str:
        params = ["siteKey=AU-Main", "sourcesystem=houston", "locale=en-AU", "sortmode=ListedDate",
                  f"pageSize={self.page_size}", f"page={page}", f"keywords={quote_plus(keyword)}"]
        loc = (location or "").strip()
        if loc and loc.lower() not in _REMOTE_WORDS + ("australia",):
            params.append(f"where={quote_plus(loc)}")
        if loc.lower() in _REMOTE_WORDS:
            params.append("workarrangement=2")   # Seek's "Remote" work-arrangement facet
        if job_type in WORKTYPE_PARAM:
            params.append(f"worktype={WORKTYPE_PARAM[job_type]}")
        if salary_min:
            params.append(f"salaryrange={int(salary_min)}-&salarytype=annual")
        return API_URL + "?" + "&".join(params)

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None) -> list[dict]:
        self.stats["mode"] = "api"
        all_jobs: list[dict] = []
        if self.exhausted:
            return all_jobs
        page = self._page
        while len(all_jobs) < max_results and page <= self.max_pages:
            data = await self._fetch_json(self._build_url(keyword, location, job_type, salary_min, page))
            if not data:
                break
            items = data.get("data") or []
            if not items:
                self.exhausted = True
                break
            fresh = [self._parse_item(it) for it in items if isinstance(it, dict) and it.get("title")]
            all_jobs.extend(fresh)
            self.stats["pages"] += 1
            self.stats["jobs"] += len(fresh)
            total = int(data.get("totalCount") or 0)
            Actor.log.info(f"[Seek] page {page}: +{len(fresh)} (run total {self.stats['jobs']}/{total or '?'})")
            if self.on_page and fresh:
                await self.on_page(self.source_name, fresh)
            page += 1
            self._page = page
            if len(items) < self.page_size or (total and self.stats["jobs"] >= total):
                self.exhausted = True
                break
            await self._polite_delay()
        return all_jobs

    def _parse_item(self, it: dict) -> dict:
        advertiser = it.get("advertiser") or {}
        company = it.get("companyName") or advertiser.get("description") or (it.get("employer") or {}).get("name") or ""
        locations = it.get("locations") or []
        location = ", ".join(clean_text(l.get("label", "")) for l in locations if isinstance(l, dict) and l.get("label"))
        bullets = [clean_text(b) for b in (it.get("bulletPoints") or []) if b]
        teaser = clean_text(it.get("teaser") or "")
        snippet = " ".join(filter(None, [teaser] + bullets))[:500]
        classifications = it.get("classifications") or []
        category = ""
        if classifications and isinstance(classifications[0], dict):
            sub = (classifications[0].get("subclassification") or {}).get("description")
            top = (classifications[0].get("classification") or {}).get("description")
            category = clean_text(sub or top or "")
        work_types = [clean_text(w) for w in (it.get("workTypes") or []) if w]
        arrangements = [clean_text(((a.get("label") or {}).get("text") or "")) for a in ((it.get("workArrangements") or {}).get("data") or [])]
        job_id = str(it.get("id") or "")
        job = {
            "source": self.source_name,
            "title": clean_text(it.get("title", "")),
            "company": clean_text(company),
            "location": location,
            "snippet": snippet,
            "date_posted": it.get("listingDate") or it.get("listingDateDisplay") or "",
            "url": f"https://www.seek.com.au/job/{job_id}" if job_id else "",
            "job_id": job_id,
            "category": category,
            "employment_type": ", ".join(work_types) or detect_employment_type(snippet[:160]),
            "salary_currency": "AUD",
        }
        if it.get("salaryLabel"):
            apply_salary(job, parse_salary(str(it["salaryLabel"]), "AUD"))
        arrangement = arrangements[0] if arrangements else ""
        job["work_mode"] = arrangement if arrangement in ("Remote", "Hybrid", "On-site") else detect_work_mode(job["title"], location, snippet)
        return job
