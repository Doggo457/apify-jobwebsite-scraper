"""Adzuna via its official API (free keys at https://developer.adzuna.com)."""

from __future__ import annotations

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, apply_salary, clean_text, detect_work_mode

API_BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search"
JOB_TYPE_PARAM = {"permanent": {"permanent": "1"}, "temporary": {"contract": "1"},
                  "contract": {"contract": "1"}, "part-time": {"part_time": "1"}}
COUNTRY_CURRENCY = {"gb": "GBP", "us": "USD", "au": "AUD", "de": "EUR", "fr": "EUR", "nl": "EUR",
                    "at": "EUR", "be": "EUR", "it": "EUR", "es": "EUR", "pl": "PLN", "ca": "CAD",
                    "nz": "NZD", "sg": "SGD", "in": "INR", "br": "BRL", "mx": "MXN", "za": "ZAR", "ch": "CHF"}
SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}


class AdzunaScraper(BaseScraper):
    page_size = 50

    def __init__(self, client, delay: float = 0.3, app_id: str = "", app_key: str = "", country: str = "gb", **kwargs):
        super().__init__(client, delay, **kwargs)
        self.app_id = (app_id or "").strip()
        self.app_key = (app_key or "").strip()
        self.country = country
        self.default_currency = COUNTRY_CURRENCY.get(country, "GBP")

    @property
    def source_name(self) -> str:
        return f"adzuna.{self.country}"

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None) -> list[dict]:
        self.stats["mode"] = "api"
        all_jobs: list[dict] = []
        if self.exhausted:
            return all_jobs
        if not self.app_id or not self.app_key:
            Actor.log.warning("[Adzuna] skipped: no API credentials (free at https://developer.adzuna.com)")
            self.exhausted = True
            return all_jobs
        page = self._page
        while len(all_jobs) < max_results and page <= self.max_pages:
            # Always a full page: page N must mean the same window on every call
            params = [f"app_id={self.app_id}", f"app_key={self.app_key}", f"results_per_page={self.page_size}",
                      f"what={quote_plus(keyword)}", f"where={quote_plus(location)}", "sort_by=date",
                      "content-type=application/json"]
            for k, v in JOB_TYPE_PARAM.get(job_type, {}).items():
                params.append(f"{k}={v}")
            if salary_min:
                params.append(f"salary_min={int(salary_min)}")
            if self.radius_miles:
                params.append(f"distance={round(int(self.radius_miles) * 1.60934)}")  # Adzuna wants km
            data = await self._fetch_json(f"{API_BASE.format(country=self.country)}/{page}?" + "&".join(params))
            if not data:
                break
            results = data.get("results") or []
            if not results:
                self.exhausted = True
                break
            fresh = [self._parse_result(it) for it in results]
            all_jobs.extend(fresh)
            self.stats["pages"] += 1
            self.stats["jobs"] += len(fresh)
            Actor.log.info(f"[Adzuna] page {page}: +{len(fresh)} (run total {self.stats['jobs']}/{data.get('count', '?')})")
            if self.on_page:
                await self.on_page(self.source_name, fresh)
            page += 1
            self._page = page
            if self.stats["jobs"] >= int(data.get("count", 0) or 0):
                self.exhausted = True
                break
            await self._polite_delay()
        return all_jobs

    def _parse_result(self, it: dict) -> dict:
        company = it.get("company") or {}
        category = it.get("category") or {}
        item_desc = it.get("description", "") or ""
        desc = clean_text(item_desc)
        job = {
            "source": self.source_name,
            "title": clean_text(it.get("title", "")),
            "company": clean_text(company.get("display_name", "") if isinstance(company, dict) else str(company)),
            "location": clean_text((it.get("location") or {}).get("display_name", "")),
            "snippet": desc[:500],
            "full_description": item_desc,
            "employment_type": clean_text(it.get("contract_type", "")).replace("_", "-").title(),
            "date_posted": it.get("created", ""),
            "url": it.get("redirect_url", ""),
            "job_id": str(it.get("id", "")),
            "category": category.get("label", "") if isinstance(category, dict) else "",
            "salary_currency": self.default_currency,
        }
        lo, hi = it.get("salary_min"), it.get("salary_max")
        if lo or hi:
            lo = float(lo or hi)
            hi = float(hi or lo)
            sym = SYMBOL.get(self.default_currency, self.default_currency + " ")
            raw = f"{sym}{lo:,.0f} per annum" if lo == hi else f"{sym}{lo:,.0f} - {sym}{hi:,.0f} per annum"
            if it.get("salary_is_predicted") in (1, "1", True):
                raw += " (estimated)"
            apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": self.default_currency, "period": "annum"})
        job["work_mode"] = detect_work_mode(job["title"], job["location"], desc[:300])
        return job
