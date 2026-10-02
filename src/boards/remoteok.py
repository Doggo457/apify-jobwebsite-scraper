"""RemoteOK.com via its public JSON feed.

The feed's `?tag=` filter only understands single-word tags (`dev`,
`engineer`, `python`); a multi-word keyword like `software-engineer` returns
nothing but the legal notice. We fetch the full feed once (~0.5 MB, one
request) and filter client-side on keyword tokens, which is both cheaper and
correct for any keyword.
"""

from __future__ import annotations

import re

from apify import Actor

from ..utils import BaseScraper, apply_salary, clean_text

API_URL = "https://remoteok.com/api"
_STOP = {"and", "or", "the", "a", "an", "of", "in", "for", "to", "jobs", "job"}


def keyword_tokens(keyword: str) -> list[str]:
    toks = [t for t in re.split(r"[^a-z0-9+#.]+", keyword.lower()) if t and t not in _STOP]
    return toks or [keyword.lower().strip()]


class RemoteOKScraper(BaseScraper):
    default_currency = "USD"

    @property
    def source_name(self) -> str:
        return "remoteok.com"

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None) -> list[dict]:
        self.stats["mode"] = "api"
        if self.exhausted:
            return []
        self.exhausted = True  # single feed, nothing to page through
        data = await self._fetch_json(API_URL, headers={"User-Agent": "jobs-board-scraper/1.0 (apify)", "Accept": "application/json"})
        if not isinstance(data, list):
            return []
        toks = keyword_tokens(keyword)
        all_jobs: list[dict] = []
        for it in data:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            hay = f"{it.get('position', '')} {' '.join(it.get('tags') or [])} {it.get('description', '')}".lower()
            if not all(t in hay for t in toks):
                continue
            lo, hi = it.get("salary_min") or None, it.get("salary_max") or None
            if salary_min and hi and hi < salary_min:
                continue
            job = {
                "source": self.source_name,
                "title": clean_text(it.get("position", "")),
                "company": clean_text(it.get("company", "")),
                "location": clean_text(it.get("location") or "Remote") or "Remote",
                "snippet": clean_text(re.sub(r"<[^>]+>", " ", it.get("description") or ""))[:500],
                "date_posted": it.get("date", "") or it.get("epoch", ""),
                "url": it.get("url", ""),
                "job_id": str(it.get("id", "")),
                "category": ", ".join((it.get("tags") or [])[:5]),
                "work_mode": "Remote",
                "salary_currency": "USD",
            }
            if lo or hi:
                lo = float(lo or hi)
                hi = float(hi or lo)
                raw = f"${lo:,.0f} per year" if lo == hi else f"${lo:,.0f} - ${hi:,.0f} per year"
                apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": "USD", "period": "annum"})
            all_jobs.append(job)
        # Every match is emitted (main.py truncates to the global cap); holding
        # some back would lose them since this board never runs a second time.
        self.stats["pages"] = 1
        self.stats["jobs"] = len(all_jobs)
        Actor.log.info(f"[RemoteOK] {len(all_jobs)} matched '{keyword}' out of {len(data)} feed items")
        if self.on_page and all_jobs:
            await self.on_page(self.source_name, all_jobs)
        return all_jobs
