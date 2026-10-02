"""Indeed scraper (UK + international variants).

Indeed renders its result cards client-side from a JSON blob embedded in the
page: ``window.mosaic.providerData["mosaic-provider-jobcards"] = {...}``. The
card DOM churns constantly, but the mosaic blob has been stable for years and
carries richer fields than the DOM, so extraction order is:

  1. mosaic provider JSON (brace-matched, string-aware)
  2. any other embedded results array whose items carry a "jobkey"
  3. JSON-LD (rare on SERPs)
  4. DOM card parsing (fallback only)

Indeed also runs aggressive anti-bot (Cloudflare + its own verification
interstitials). HTTP is tried first; a challenge page switches the board to
the browser, which keeps one warmed context and sends the previous results
page as referer so pagination looks like a real user. Treat it as
best-effort and the most expensive board in the set.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urljoin

from apify import Actor

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, parse_salary, strip_tracking)

JOB_TYPE_PARAM = {"permanent": "permanent", "temporary": "temporary", "contract": "contract", "part-time": "parttime"}
_SALARY_TYPE_MAP = {"yearly": "annum", "annual": "annum", "monthly": "month", "weekly": "week", "daily": "day", "hourly": "hour"}
_CURRENCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "$"}


class IndeedScraper(BaseScraper):
    card_selector = "div.job_seen_beacon, div[data-jk], a[data-jk]"
    page_size = 15
    max_pages = 20

    def __init__(self, client, delay: float = 1.5, base_url: str = "https://uk.indeed.com",
                 source: str = "indeed.co.uk", currency: str = "GBP", **kwargs):
        super().__init__(client, delay, **kwargs)
        self.base_url = base_url
        self._source = source
        self.default_currency = currency
        self._debug_saved = False

    @property
    def source_name(self) -> str:
        return self._source

    def page_is_blocked(self, html: str) -> bool:
        if not html:
            return False
        head = html[:6000].lower()
        if ("just a moment" in head or "verify you are human" in head
                or "additional verification required" in head or "request blocked" in head):
            return True
        return len(html) < 30000 and ("captcha" in head or "cf-chl" in head)

    # Indeed shows ONE result page to anonymous visitors: page 2 (start=10)
    # redirects to secure.indeed.com "page-two-signin" (verified in a real
    # browser from a residential IP). So "pages" here are distinct first
    # pages of related searches, which overlap only partly:
    #   1. newest first   2. relevance   3. relevance, wider radius (or last 14 days)
    VARIANT_PAGES = 3
    max_pages = VARIANT_PAGES
    hard_page_cap = VARIANT_PAGES

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        params = [f"q={quote_plus(keyword)}", f"l={quote_plus(location)}"]
        if page == 1:
            params.append("sort=date")
        elif page == 3:
            params.append("fromage=14" if self.radius_miles else "radius=25")
        if job_type in JOB_TYPE_PARAM:
            params.append(f"jt={JOB_TYPE_PARAM[job_type]}")
        if salary_min:
            params.append(f"salary={int(salary_min)}")
        if self.radius_miles:
            params.append(f"radius={int(self.radius_miles)}")
        return f"{self.base_url}/jobs?" + "&".join(params)

    def _parse_search(self, html: str, soup) -> tuple[list[dict], bool]:
        jobs = self._extract_mosaic(html)
        if not jobs:
            jobs = self._extract_jsonld_jobs(soup)
        if not jobs:
            jobs = self._parse_cards(soup)
        if not jobs:
            # Fire-and-forget diagnostic: the page HTML goes to the run's KV store once.
            self._pending_debug_html = html
        # Pagination is a sign-in wall; continue through the search variants instead.
        return jobs, bool(jobs) and self._page < self.VARIANT_PAGES

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None):
        self._pending_debug_html = None
        jobs = await super().search(keyword, location, max_results, job_type, salary_min)
        if not jobs and self._pending_debug_html:
            await self._save_debug_page(self._pending_debug_html)
        return jobs

    async def _save_debug_page(self, html: str) -> None:
        if self._debug_saved:
            return
        try:
            store = await Actor.open_key_value_store()
            key = f"DEBUG-{self.source_name}.html"
            await store.set_value(key, html, content_type="text/html")
            self._debug_saved = True
            Actor.log.warning(f"[{self.source_name}] 0 jobs parsed from a {len(html)}-byte page; HTML saved to KV key '{key}'")
        except Exception as e:
            Actor.log.debug(f"[{self.source_name}] debug page save failed: {e}")

    # ── Embedded-JSON extraction ─────────────────────────────────────

    @staticmethod
    def _brace_match(text: str, open_pos: int) -> str | None:
        """Balanced {...} or [...] starting at open_pos, string-aware."""
        opener = text[open_pos]
        closer = {"{": "}", "[": "]"}.get(opener)
        if not closer:
            return None
        depth = 0
        in_str = escaped = False
        for i in range(open_pos, min(len(text), open_pos + 5_000_000)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[open_pos:i + 1]
        return None

    def _extract_mosaic(self, html: str) -> list[dict]:
        for marker in ('window.mosaic.providerData["mosaic-provider-jobcards"]',
                       "window.mosaic.providerData['mosaic-provider-jobcards']"):
            idx = html.find(marker)
            if idx == -1:
                continue
            brace = html.find("{", idx + len(marker))
            if brace == -1:
                continue
            blob = self._brace_match(html, brace)
            if not blob:
                continue
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                continue
            results = (((data.get("metaData") or {}).get("mosaicProviderJobCardsModel") or {}).get("results") or [])
            jobs = [j for j in (self._map_mosaic_job(r) for r in results if isinstance(r, dict)) if j.get("title")]
            if jobs:
                return jobs
        for m in re.finditer(r'"(?:results|jobCards|jobcards)"\s*:\s*\[', html):
            open_pos = m.end() - 1
            window = html[open_pos:open_pos + 3000]
            if '"jobkey"' not in window and '"jobKey"' not in window:
                continue
            blob = self._brace_match(html, open_pos)
            if not blob:
                continue
            try:
                results = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(results, list):
                jobs = [j for j in (self._map_mosaic_job(r) for r in results if isinstance(r, dict)) if j.get("title")]
                if jobs:
                    return jobs
        return []

    def _map_mosaic_job(self, item: dict) -> dict:
        job = {"source": self.source_name,
               "title": clean_text(re.sub(r"<[^>]+>", " ", str(item.get("displayTitle") or item.get("title") or "")))}
        company = item.get("company") or item.get("companyName") or ""
        if company:
            job["company"] = clean_text(str(company))
        loc = item.get("formattedLocation") or item.get("jobLocationCity") or item.get("location") or ""
        if isinstance(loc, dict):
            loc = loc.get("formattedLocation") or loc.get("city") or ""
        if loc:
            job["location"] = clean_text(str(loc))
        jk = item.get("jobkey") or item.get("jobKey") or item.get("jk") or ""
        if jk:
            job["job_id"] = str(jk)
            view = item.get("viewJobLink") or f"/viewjob?jk={jk}"
            job["url"] = urljoin(self.base_url, str(view).split("&from=")[0])
        elif item.get("link"):
            job["url"] = strip_tracking(urljoin(self.base_url, str(item["link"])))
        sal_text = ""
        snip = item.get("salarySnippet")
        if isinstance(snip, dict):
            sal_text = snip.get("text") or snip.get("salaryTextFormatted") or ""
        elif isinstance(snip, str):
            sal_text = snip
        if not sal_text:
            sal_text = item.get("formattedSalary") or item.get("formattedSalarySnippet") or ""
        if sal_text:
            apply_salary(job, parse_salary(clean_text(str(sal_text)), self.default_currency))
        if not job.get("salary_min"):
            ext = item.get("extractedSalary")
            if isinstance(ext, dict) and (ext.get("min") or ext.get("max")):
                try:
                    lo = float(ext.get("min") or ext.get("max"))
                    hi = float(ext.get("max") or ext.get("min"))
                except (TypeError, ValueError):
                    lo = hi = 0.0
                if lo:
                    period = _SALARY_TYPE_MAP.get(str(ext.get("type", "")).lower(), "annum")
                    label = {"annum": "per annum", "month": "per month", "week": "per week", "day": "per day", "hour": "per hour"}[period]
                    sym = _CURRENCY_SYMBOL.get(self.default_currency, "£")
                    raw = f"{sym}{lo:,.0f} {label}" if lo == hi else f"{sym}{lo:,.0f} - {sym}{hi:,.0f} {label}"
                    apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": self.default_currency, "period": period})
        desc = item.get("snippet") or ""
        if desc:
            job["snippet"] = clean_text(re.sub(r"<[^>]+>", " ", str(desc)))[:500]
        pub = item.get("pubDate")
        if isinstance(pub, (int, float)) and not isinstance(pub, bool) and pub > 0:
            job["date_posted"] = pub  # epoch ms; the pipeline normalises it
        elif item.get("formattedRelativeTime"):
            job["date_posted"] = clean_text(str(item["formattedRelativeTime"]))
        types = item.get("jobTypes") or item.get("jobType") or []
        if isinstance(types, str):
            types = [types]
        if types:
            job["employment_type"] = ", ".join(clean_text(str(t)) for t in types if t)
        else:
            job["employment_type"] = detect_employment_type(job.get("snippet"))
        job["work_mode"] = "Remote" if item.get("remoteLocation") is True else detect_work_mode(job.get("location"), job.get("snippet"))
        return job

    def _parse_cards(self, soup) -> list[dict]:
        jobs = []
        cards = (soup.select('div[class*="job_seen_beacon"]') or soup.select('div[class*="cardOutline"]')
                 or soup.select('div[class*="jobsearch-SerpJobCard"]') or soup.select("div[data-jk]")
                 or soup.select('td[class*="resultContent"]'))
        for card in cards:
            a = (card.select_one("h2 a") or card.select_one("a[data-jk]") or card.select_one('a[id^="job_"]')
                 or card.select_one('a[class*="JobTitle"]') or card.select_one('a[class*="title"]'))
            if not a:
                span = card.select_one("h2 span") or card.select_one('[class*="jobTitle"] span')
                a = span.find_parent("a") if span else None
            if not a:
                continue
            href = a.get("href", "")
            job = {"source": self.source_name, "title": clean_text(a.get_text()),
                   "url": strip_tracking(urljoin(self.base_url, href))}
            jk = a.get("data-jk", "") or card.get("data-jk", "")
            if not jk:
                m = re.search(r"[?&]jk=([0-9a-f]+)", href)
                jk = m.group(1) if m else ""
            if jk:
                job["job_id"] = jk
                job["url"] = f"{self.base_url}/viewjob?jk={jk}"
            el = card.select_one('[data-testid="company-name"], [class*="companyName"]')
            job["company"] = clean_text(el.get_text()) if el else ""
            el = card.select_one('[data-testid="text-location"], [class*="companyLocation"]')
            job["location"] = clean_text(el.get_text()) if el else ""
            el = card.select_one('[class*="salary-snippet"], [class*="salaryText"], [data-testid*="salary"]')
            if el:
                apply_salary(job, parse_salary(el.get_text(), self.default_currency))
            el = card.select_one('[class*="job-snippet"], [data-testid="jobsnippet_footer"]')
            job["snippet"] = clean_text(el.get_text(" "))[:500] if el else ""
            el = card.select_one('[data-testid="myJobsStateDate"], span.date')
            job["date_posted"] = clean_text(el.get_text()) if el else ""
            job["work_mode"] = detect_work_mode(job["location"], job["snippet"])
            job["employment_type"] = detect_employment_type(job["snippet"])
            jobs.append(job)
        return jobs


class IndeedUKScraper(IndeedScraper):
    def __init__(self, client, delay: float = 1.5, **kwargs):
        super().__init__(client, delay, base_url="https://uk.indeed.com", source="indeed.co.uk", currency="GBP", **kwargs)
