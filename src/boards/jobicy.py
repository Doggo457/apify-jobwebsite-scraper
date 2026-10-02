"""Jobicy — free public remote-jobs API (https://jobicy.com/jobs-rss-feed).

Keyless JSON API of remote roles. Supports a server-side ``tag=`` keyword and a
``count=`` limit; everything it returns is remote.
"""

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, clean_text


API_URL = "https://jobicy.com/api/v2/remote-jobs"


class JobicyScraper(BaseScraper):
    resumable = False

    @property
    def source_name(self) -> str:
        return "jobicy.com"

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        count = min(50, max(5, max_results)) if max_results else 50
        url = f"{API_URL}?count={count}"
        if keyword:
            url += f"&tag={quote_plus(keyword)}"
        Actor.log.info(f"[Jobicy] Fetching API: {url}")

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

            geo = item.get("jobGeo", "") or "Anywhere"
            if not loc_is_broad:
                gl = geo.lower()
                if loc not in gl and "anywhere" not in gl and "worldwide" not in gl:
                    continue

            job_types = item.get("jobType", [])
            emp = ", ".join(job_types) if isinstance(job_types, list) else str(job_types)
            industry = item.get("jobIndustry", [])
            category = ", ".join(industry) if isinstance(industry, list) else str(industry)
            description = item.get("jobDescription", "") or item.get("jobExcerpt", "") or ""

            # Jobicy v2 sometimes exposes annual salary figures.
            sal_min = item.get("annualSalaryMin")
            sal_max = item.get("annualSalaryMax")
            currency = item.get("salaryCurrency") or "USD"

            all_jobs.append({
                "title": clean_text(item.get("jobTitle", "")),
                "company": item.get("companyName", ""),
                "location": geo,
                "salary_raw": "",
                "salary_min": float(sal_min) if sal_min else None,
                "salary_max": float(sal_max) if sal_max else None,
                "salary_currency": currency,
                "salary_period": "annum",
                "snippet": clean_text(item.get("jobExcerpt", "") or description)[:500],
                "full_description": description,
                "employment_type": emp,
                "date_posted": item.get("pubDate", ""),
                "url": item.get("url", ""),
                "job_id": str(item.get("id", "")),
                "source": self.source_name,
                "category": category,
                "remote": "remote",
            })

        Actor.log.info(f"[Jobicy] Total scraped: {len(all_jobs)}")
        return all_jobs
