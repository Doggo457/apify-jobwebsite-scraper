"""Arbeitnow.com via its public JSON API (EU + remote, mostly DACH).

The API has no search parameters: each page is ~2.5 MB of unfiltered
listings, so we filter client-side on keyword tokens and cap the page count.
"""

from __future__ import annotations

import re

from apify import Actor

from ..utils import BaseScraper, clean_text, detect_work_mode
from .remoteok import keyword_tokens

API_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowScraper(BaseScraper):
    default_currency = "EUR"
    max_pages = 6
    hard_page_cap = 6

    @property
    def source_name(self) -> str:
        return "arbeitnow.com"

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None) -> list[dict]:
        self.stats["mode"] = "api"
        toks = keyword_tokens(keyword)
        loc = (location or "").lower().strip()
        want_remote = loc in ("", "remote")
        all_jobs: list[dict] = []
        if self.exhausted:
            return all_jobs
        page = self._page
        while len(all_jobs) < max_results and page <= self.max_pages:
            data = await self._fetch_json(f"{API_URL}?page={page}")
            if not data:
                self.exhausted = True
                break
            items = data.get("data") or []
            if not items:
                self.exhausted = True
                break
            fresh = []
            for it in items:
                title = it.get("title", "")
                desc = it.get("description", "") or ""
                tags = it.get("tags") or []
                hay = f"{title} {desc} {' '.join(tags)}".lower()
                if not all(t in hay for t in toks):
                    continue
                item_loc = (it.get("location") or "").lower()
                is_remote = bool(it.get("remote"))
                if not want_remote and loc not in item_loc and not is_remote:
                    continue
                job_types = it.get("job_types") or []
                fresh.append({
                    "source": self.source_name,
                    "title": clean_text(title),
                    "company": clean_text(it.get("company_name", "")),
                    "location": clean_text(it.get("location") or "Remote"),
                    "snippet": clean_text(re.sub(r"<[^>]+>", " ", desc))[:500],
                    "date_posted": it.get("created_at", ""),
                    "url": it.get("url", ""),
                    "job_id": str(it.get("slug", "")),
                    "employment_type": clean_text(job_types[0]) if job_types else "",
                    "category": ", ".join(tags[:5]),
                    "work_mode": "Remote" if is_remote else detect_work_mode(title, desc[:300]),
                    "salary_currency": "EUR",
                })
            all_jobs.extend(fresh)
            self.stats["pages"] += 1
            self.stats["jobs"] += len(fresh)
            Actor.log.info(f"[Arbeitnow] page {page}: +{len(fresh)} (run total {self.stats['jobs']})")
            if self.on_page and fresh:
                await self.on_page(self.source_name, fresh)
            page += 1
            self._page = page
            if not (data.get("links") or {}).get("next"):
                self.exhausted = True
                break
        if self.hard_page_cap and page > self.hard_page_cap:
            self.exhausted = True
        return all_jobs
