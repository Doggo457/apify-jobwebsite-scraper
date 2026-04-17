"""RemoteOK.com scraper — free JSON API for remote jobs worldwide."""

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, clean_text


API_URL = "https://remoteok.com/api"


class RemoteOKScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "remoteok.com"

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        """RemoteOK returns all jobs in a single API call (no pagination)."""
        # The API supports tag-based filtering via URL path
        tag = quote_plus(keyword.lower().replace(" ", "-"))
        url = f"{API_URL}?tag={tag}"

        Actor.log.info(f"[RemoteOK] Fetching API: {url}")

        try:
            response = await self.client.get(url, follow_redirects=True, headers={
                "User-Agent": "jobscraper/1.0",
            })
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            Actor.log.warning(f"[RemoteOK] API request failed: {e}")
            return []

        if not isinstance(data, list):
            return []

        # First item is usually a legal notice, skip it
        items = [item for item in data if isinstance(item, dict) and item.get("id")]

        all_jobs = []
        for item in items:
            if len(all_jobs) >= max_results:
                break

            sal_min = item.get("salary_min")
            sal_max = item.get("salary_max")

            # Apply salary filter
            if salary_min and sal_max and sal_max < salary_min:
                continue

            salary_raw = ""
            if sal_min and sal_max:
                salary_raw = f"${sal_min:,} - ${sal_max:,} per year"
            elif sal_min:
                salary_raw = f"${sal_min:,}+ per year"

            tags = item.get("tags", [])
            if isinstance(tags, list):
                tags = ", ".join(tags[:5])

            all_jobs.append({
                "title": clean_text(item.get("position", "")),
                "company": item.get("company", ""),
                "location": item.get("location", "Remote"),
                "salary_raw": salary_raw,
                "salary_min": sal_min,
                "salary_max": sal_max,
                "salary_period": "annum",
                "snippet": clean_text(item.get("description", ""))[:500],
                "date_posted": item.get("date", ""),
                "url": item.get("url", ""),
                "job_id": str(item.get("id", "")),
                "source": self.source_name,
                "category": tags if isinstance(tags, str) else "",
            })

        Actor.log.info(f"[RemoteOK] Total scraped: {len(all_jobs)}")
        return all_jobs
