"""Remotive — free public remote-jobs API (https://remotive.com/api/remote-jobs).

Keyless JSON API of hand-curated remote roles. Supports a server-side
``search=`` term; everything it returns is remote.
"""

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, clean_text


API_URL = "https://remotive.com/api/remote-jobs"


class RemotiveScraper(BaseScraper):
    resumable = False

    @property
    def source_name(self) -> str:
        return "remotive.com"

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        url = f"{API_URL}?search={quote_plus(keyword)}" if keyword else API_URL
        Actor.log.info(f"[Remotive] Fetching API: {url}")

        data = await self._fetch_json(url)
        if not data:
            return []

        jobs_raw = data.get("jobs", [])
        loc = (location or "").lower().strip()
        loc_is_broad = loc in ("", "remote", "anywhere", "worldwide")

        all_jobs: list[dict] = []
        for item in jobs_raw:
            if len(all_jobs) >= max_results:
                break

            required = item.get("candidate_required_location", "") or "Worldwide"

            # Optional soft location filter — Remotive roles are remote, so keep
            # anything open worldwide or matching the requested region.
            if not loc_is_broad:
                rl = required.lower()
                if loc not in rl and "worldwide" not in rl and "anywhere" not in rl:
                    continue

            tags = item.get("tags", [])
            category = item.get("category", "")
            if isinstance(tags, list) and not category:
                category = ", ".join(tags[:5])

            all_jobs.append({
                "title": clean_text(item.get("title", "")),
                "company": item.get("company_name", ""),
                "location": required,
                "salary_raw": item.get("salary", "") or "",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": "USD",
                "salary_period": "annum",
                "snippet": clean_text(item.get("description", ""))[:500],
                "full_description": item.get("description", ""),
                "employment_type": item.get("job_type", ""),
                "date_posted": item.get("publication_date", ""),
                "url": item.get("url", ""),
                "job_id": str(item.get("id", "")),
                "source": self.source_name,
                "category": category,
                "remote": "remote",
            })

        Actor.log.info(f"[Remotive] Total scraped: {len(all_jobs)}")
        return all_jobs
