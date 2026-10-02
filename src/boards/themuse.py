"""The Muse — free public jobs API (https://www.themuse.com/developers/api/v2).

Keyless JSON API covering companies worldwide with categories, levels and
locations. No server-side keyword search, so we paginate newest-first
(``descending=true``) and filter by keyword client-side. A ``location=``
param is passed server-side, but the API SILENTLY IGNORES strings it doesn't
recognise (e.g. a bare city) and returns the global feed — so the client-side
location filter is ALWAYS applied on top, never trusted to the server.

The location filter keeps: (a) jobs matching the requested city/term,
(b) jobs with an office in the requested country (main.py sets country_hint),
and (c) genuinely remote/flexible listings. Jobs whose only offices are in
other countries are dropped — a global board must not pad local searches.

Fetched pages are cached per (category, location) for the lifetime of the
scraper, so multi-term runs don't re-download the same newest-first feed
once per search term.
"""

from urllib.parse import quote_plus

from apify import Actor

from ..utils import BaseScraper, clean_text


API_URL = "https://www.themuse.com/api/public/jobs"

# country input value → country name used in Muse location strings
_COUNTRY_NAMES = {
    "uk": "united kingdom", "us": "united states", "de": "germany",
    "fr": "france", "nl": "netherlands", "au": "australia",
}

# Broad keyword → Muse category hints (used to narrow the server-side pull when
# we recognise the role; falls back to an unfiltered newest-first scan).
_CATEGORY_HINTS = {
    "software": "Software Engineering",
    "developer": "Software Engineering",
    "engineer": "Software Engineering",
    "data": "Data Science",
    "designer": "Design and UX",
    "ux": "Design and UX",
    "product manager": "Product Management",
    "marketing": "Marketing",
    "sales": "Sales",
    "account": "Accounting and Finance",
    "hr": "HR and Recruiting",
    "recruit": "HR and Recruiting",
}


class TheMuseScraper(BaseScraper):
    resumable = False

    def __init__(self, client, delay: float = 0.5, **kwargs):
        super().__init__(client, delay, **kwargs)
        # (category, location, page) → results list. Lives for the whole run,
        # so N search terms share one download of the newest-first feed.
        self._page_cache: dict[tuple[str, str, int], list | None] = {}

    @property
    def source_name(self) -> str:
        return "themuse.com"

    def _category_for(self, keyword: str) -> str:
        kw = keyword.lower()
        for needle, cat in _CATEGORY_HINTS.items():
            if needle in kw:
                return cat
        return ""

    def _location_ok(self, query_loc: str, loc_names: list[str]) -> bool:
        """Tightened location relevance for a global board.

        Keep: city/term matches, offices in the requested country
        (country_hint), and genuinely remote/flexible listings. Drop
        listings whose only concrete offices are in other countries —
        even when they also carry a "Flexible / Remote" tag, a US-office
        role is padding for a UK city search.
        """
        if not loc_names:
            return True  # no location info — treated as flexible
        lows = [n.lower() for n in loc_names]
        if query_loc and any(query_loc in n for n in lows):
            return True
        country = _COUNTRY_NAMES.get(
            (getattr(self, "country_hint", "") or "").lower(), "")
        if country and any(country in n for n in lows):
            return True
        concrete = [n for n in lows
                    if "remote" not in n and "flexible" not in n]
        return not concrete  # purely remote/flexible → genuinely location-free

    async def _fetch_page(self, category: str, location_param: str, page: int) -> list | None:
        """Fetch one feed page, memoised across search terms."""
        key = (category, location_param, page)
        if key in self._page_cache:
            Actor.log.debug(f"[TheMuse] Page {page} served from cache")
            return self._page_cache[key]

        url = f"{API_URL}?page={page}&descending=true"
        if category:
            url += f"&category={quote_plus(category)}"
        if location_param:
            url += f"&location={quote_plus(location_param)}"
        Actor.log.info(f"[TheMuse] Fetching API page {page}"
                       + (f" (location='{location_param}')" if location_param else ""))

        data = await self._fetch_json(url)
        results = data.get("results", []) if data else None
        if results is not None:  # don't cache transport failures
            self._page_cache[key] = results
        return results

    async def _scan(self, kw: str, loc: str, category: str, location_param: str,
                    max_results: int, max_pages: int) -> list[dict]:
        """Paginate the feed applying the client-side keyword (and, when no
        server-side location was used, the client-side location) filter."""
        all_jobs: list[dict] = []
        page = 0

        while len(all_jobs) < max_results and page <= max_pages:
            results = await self._fetch_page(category, location_param, page)
            if not results:
                break

            for item in results:
                if len(all_jobs) >= max_results:
                    break

                title = item.get("name", "")
                contents = item.get("contents", "") or ""
                cats = ", ".join(c.get("name", "") for c in item.get("categories", []) if isinstance(c, dict))
                levels = ", ".join(l.get("name", "") for l in item.get("levels", []) if isinstance(l, dict))
                locations = item.get("locations", []) or []
                loc_names = [l.get("name", "") for l in locations
                             if isinstance(l, dict) and l.get("name")]
                loc_str = ", ".join(loc_names)

                # Client-side keyword filter (title + body + taxonomy).
                if kw:
                    hay = f"{title} {contents} {cats} {levels}".lower()
                    if kw not in hay:
                        continue

                # Client-side location filter — ALWAYS applied: the API
                # silently ignores location strings it doesn't recognise,
                # so a server-side param is no guarantee of narrowing.
                if loc and not self._location_ok(loc, loc_names):
                    continue

                company = item.get("company", {})
                landing = (item.get("refs", {}) or {}).get("landing_page", "")

                all_jobs.append({
                    "title": clean_text(title),
                    "company": company.get("name", "") if isinstance(company, dict) else str(company),
                    "location": loc_str or "Flexible / Remote",
                    "salary_raw": "",
                    "salary_min": None,
                    "salary_max": None,
                    "salary_currency": "",
                    "salary_period": "annum",
                    "snippet": clean_text(contents)[:500],
                    "full_description": contents,
                    "employment_type": levels,
                    "date_posted": item.get("publication_date", ""),
                    "url": landing,
                    "apply_url": landing,
                    "job_id": str(item.get("id", "")),
                    "source": self.source_name,
                    "category": cats,
                })

            if len(results) < 20:  # last page
                break
            page += 1
            # Only be polite when the page actually came from the network.
            if (category, location_param, page) not in self._page_cache:
                await self._polite_delay()

        return all_jobs

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        kw = (keyword or "").lower().strip()
        loc = (location or "").lower().strip()
        loc_is_broad = loc in ("", "remote", "anywhere", "worldwide")
        category = self._category_for(keyword)

        # Bound pages so a single board can't dominate cost. The Muse returns 20
        # per page; scan a bit deeper than needed to survive client-side filtering.
        max_pages = min(40, max(6, (max_results // 5) + 6))

        all_jobs: list[dict] = []
        if not loc_is_broad:
            # Try the server-side location filter first — far fewer pages when
            # the API recognises the string (it wants e.g. "London, United
            # Kingdom", so a bare city name may return nothing).
            all_jobs = await self._scan(kw, loc, category, location, max_results, max_pages)
            if not all_jobs:
                Actor.log.info("[TheMuse] Server-side location returned nothing — "
                               "falling back to client-side location scan")

        if not all_jobs:
            all_jobs = await self._scan(kw, "" if loc_is_broad else loc, category, "",
                                        max_results, max_pages)

        Actor.log.info(f"[TheMuse] Total scraped: {len(all_jobs)}")
        return all_jobs
