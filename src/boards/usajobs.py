"""USAJobs.gov scraper — free US government jobs API (no key needed for basic search)."""

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, clean_text


API_BASE = "https://data.usajobs.gov/api/search"


class USAJobsScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "usajobs.gov"

    def _build_url(self, keyword: str, location: str, page: int, per_page: int = 25) -> str:
        params = [
            f"Keyword={quote_plus(keyword)}",
            f"LocationName={quote_plus(location)}",
            f"ResultsPerPage={per_page}",
            f"Page={page}",
            "SortField=DatePosted",
            "SortDirection=Desc",
        ]
        return f"{API_BASE}?" + "&".join(params)

    async def _fetch_json(self, url: str) -> dict | None:
        """Override to add required User-Agent header for USAJobs API."""
        try:
            headers = {
                "User-Agent": "jobscraper@apify.com",
                "Host": "data.usajobs.gov",
            }
            response = await self.client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            Actor.log.warning(f"[{self.source_name}] Failed to fetch {url}: {e}")
            return None

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        page = 1
        per_page = min(50, max_results) if max_results > 0 else 50

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, page, per_page)
            Actor.log.info(f"[USAJobs] Fetching API page {page}")

            data = await self._fetch_json(url)
            if not data:
                break

            results = data.get("SearchResult", {}).get("SearchResultItems", [])
            if not results:
                Actor.log.info(f"[USAJobs] No results on page {page}, stopping.")
                break

            for item in results:
                if len(all_jobs) >= max_results:
                    break
                job = self._parse_result(item)
                all_jobs.append(job)

            total = int(data.get("SearchResult", {}).get("SearchResultCountAll", 0))
            if len(all_jobs) >= total or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[USAJobs] Total scraped: {len(all_jobs)}")
        return all_jobs

    def _parse_result(self, item: dict) -> dict:
        match = item.get("MatchedObjectDescriptor", {})

        salary_min = None
        salary_max = None
        salary_raw = ""
        remuneration = match.get("PositionRemuneration", [])
        if remuneration:
            r = remuneration[0]
            salary_min = float(r.get("MinimumRange", 0)) or None
            salary_max = float(r.get("MaximumRange", 0)) or None
            desc = r.get("Description", "Per Year")
            if salary_min and salary_max:
                salary_raw = f"${salary_min:,.0f} - ${salary_max:,.0f} {desc}"

        locations = match.get("PositionLocation", [])
        location_str = locations[0].get("LocationName", "") if locations else ""

        return {
            "title": clean_text(match.get("PositionTitle", "")),
            "company": match.get("OrganizationName", ""),
            "location": location_str,
            "salary_raw": salary_raw,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": "annum",
            "snippet": clean_text(match.get("UserArea", {}).get("Details", {}).get("MajorDuties", [""])[0] if match.get("UserArea", {}).get("Details", {}).get("MajorDuties") else match.get("QualificationSummary", ""))[:500],
            "date_posted": match.get("PublicationStartDate", ""),
            "url": match.get("PositionURI", ""),
            "job_id": match.get("PositionID", ""),
            "source": self.source_name,
            "employment_type": match.get("PositionSchedule", [{}])[0].get("Name", "") if match.get("PositionSchedule") else "",
        }
