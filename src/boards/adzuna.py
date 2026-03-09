"""Adzuna.co.uk job board scraper via their public API."""

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, clean_text

# Adzuna has a free API - users need to register at https://developer.adzuna.com
# to get their own app_id and app_key
API_BASE = "https://api.adzuna.com/v1/api/jobs/gb/search"

JOB_TYPE_MAP = {
    "all": {},
    "permanent": {"permanent": "1"},
    "temporary": {"contract": "1"},
    "contract": {"contract": "1"},
    "part-time": {"part_time": "1"},
}


class AdzunaScraper(BaseScraper):

    def __init__(self, client, delay: float = 0.5, app_id: str = "", app_key: str = ""):
        super().__init__(client, delay)
        self.app_id = app_id
        self.app_key = app_key

    @property
    def source_name(self) -> str:
        return "adzuna.co.uk"

    def _build_url(self, keyword: str, location: str, job_type: str,
                   salary_min: int | None, page: int, per_page: int = 20) -> str:
        params = [
            f"app_id={self.app_id}",
            f"app_key={self.app_key}",
            f"results_per_page={per_page}",
            f"what={quote_plus(keyword)}",
            f"where={quote_plus(location)}",
            "sort_by=date",
            "content-type=application/json",
        ]

        type_params = JOB_TYPE_MAP.get(job_type, {})
        for k, v in type_params.items():
            params.append(f"{k}={v}")

        if salary_min:
            params.append(f"salary_min={salary_min}")

        return f"{API_BASE}/{page}?" + "&".join(params)

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        if not self.app_id or not self.app_key:
            Actor.log.warning("[Adzuna] Skipping - no API credentials provided. Register at https://developer.adzuna.com")
            return []

        all_jobs = []
        page = 1
        per_page = min(50, max_results)

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, page, per_page)
            Actor.log.info(f"[Adzuna] Fetching API page {page}")

            data = await self._fetch_json(url)
            if not data:
                break

            results = data.get("results", [])
            if not results:
                Actor.log.info(f"[Adzuna] No results on page {page}, stopping.")
                break

            for item in results:
                if len(all_jobs) >= max_results:
                    break

                job = self._parse_result(item)
                all_jobs.append(job)

            # Check if there are more results
            total = data.get("count", 0)
            if len(all_jobs) >= total or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[Adzuna] Total scraped: {len(all_jobs)}")
        return all_jobs

    def _parse_result(self, item: dict) -> dict:
        """Parse a single Adzuna API result into unified format."""
        location_data = item.get("location", {})
        display_name = location_data.get("display_name", "")

        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")

        # Determine period from contract_time
        period = "annum"
        contract_time = item.get("contract_time", "")

        # Build salary raw string
        salary_raw = ""
        if salary_min and salary_max:
            if salary_min == salary_max:
                salary_raw = f"£{salary_min:,.0f} per {period}"
            else:
                salary_raw = f"£{salary_min:,.0f} - £{salary_max:,.0f} per {period}"

        # Predicted salary flag
        is_predicted = item.get("salary_is_predicted", 0)
        if is_predicted and salary_raw:
            salary_raw += " (estimated)"

        company = item.get("company", {})
        category = item.get("category", {})

        return {
            "title": clean_text(item.get("title", "")),
            "company": company.get("display_name", "") if isinstance(company, dict) else str(company),
            "location": display_name,
            "salary_raw": salary_raw,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": period,
            "snippet": clean_text(item.get("description", ""))[:500],
            "full_description": clean_text(item.get("description", "")),
            "employment_type": item.get("contract_type", ""),
            "date_posted": item.get("created", ""),
            "url": item.get("redirect_url", ""),
            "job_id": str(item.get("id", "")),
            "source": self.source_name,
            "category": category.get("label", "") if isinstance(category, dict) else "",
        }
