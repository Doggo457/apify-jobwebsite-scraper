"""Arbeitnow.com scraper — free JSON API for EU and remote jobs."""

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, clean_text, parse_salary


API_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "arbeitnow.com"

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        page = 1

        while len(all_jobs) < max_results:
            url = f"{API_URL}?page={page}"
            Actor.log.info(f"[Arbeitnow] Fetching API page {page}")

            data = await self._fetch_json(url)
            if not data:
                break

            items = data.get("data", [])
            if not items:
                Actor.log.info(f"[Arbeitnow] No results on page {page}, stopping.")
                break

            keyword_lower = keyword.lower()
            location_lower = location.lower()

            for item in items:
                if len(all_jobs) >= max_results:
                    break

                title = item.get("title", "")
                item_location = item.get("location", "")
                description = item.get("description", "")

                # Client-side keyword + location filter (API doesn't support search params)
                searchable = f"{title} {description} {item.get('tags', '')}".lower()
                if keyword_lower not in searchable:
                    continue

                if location_lower and location_lower != "remote":
                    loc_searchable = f"{item_location} {item.get('remote', '')}".lower()
                    if location_lower not in loc_searchable and "remote" not in loc_searchable:
                        continue

                tags = item.get("tags", [])
                if isinstance(tags, list):
                    tags = ", ".join(tags[:5])

                all_jobs.append({
                    "title": clean_text(title),
                    "company": item.get("company_name", ""),
                    "location": item_location or "Remote",
                    "salary_raw": "",
                    "salary_min": None,
                    "salary_max": None,
                    "salary_period": "annum",
                    "snippet": clean_text(description)[:500],
                    "date_posted": item.get("created_at", ""),
                    "url": item.get("url", ""),
                    "job_id": str(item.get("slug", "")),
                    "source": self.source_name,
                    "employment_type": item.get("job_types", [""])[0] if item.get("job_types") else "",
                    "category": tags if isinstance(tags, str) else "",
                })

            # Check for next page
            if not data.get("links", {}).get("next"):
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[Arbeitnow] Total scraped: {len(all_jobs)}")
        return all_jobs
