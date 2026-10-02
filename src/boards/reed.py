"""Reed.co.uk scraper.

Reed's search pages are server-rendered: a plain HTTP request returns 25
fully populated job cards with stable `data-qa` attributes (job-card,
job-card-title, job-posted-by, job-metadata-salary, job-metadata-location),
so the normal path needs no browser at all.

Reed also sits behind Cloudflare Bot Management plus a Next.js middleware
that serves a soft-404 page ("notFoundReason" in __NEXT_DATA__, full-size
HTML) to requests it scores as bots. That page is detected and never parsed;
on the browser fallback path we try Reed's Next.js data route from inside the
live page (real browser TLS + established cookies) to get clean search JSON.

With a Reed API key (free at https://www.reed.co.uk/developers) the official
JSON API is used instead: 100 results per request, includes descriptions,
never blocked, no proxy.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urljoin

from apify import Actor

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, extract_salary_from_text, parse_salary, strip_tracking)

BASE_URL = "https://www.reed.co.uk"
API_URL = "https://www.reed.co.uk/api/1.0/search"

# Reed's search form uses boolean checkboxes: perm / temp / contract / partTime
JOB_TYPE_PARAM = {"permanent": "perm=true", "temporary": "temp=true", "contract": "contract=true", "part-time": "partTime=true"}
API_TYPE_PARAM = {"permanent": "permanent", "temporary": "temp", "contract": "contract", "part-time": "partTime"}


class ReedScraper(BaseScraper):
    card_selector = 'article[data-qa="job-card"]'
    page_size = 25

    # Browser-path knobs (only used if HTTP is blocked): Cloudflare BM flags
    # tampered JS natives and UA/engine mismatches, so run a clean profile;
    # allow CSS so the page renders realistically for its telemetry.
    use_stealth_js = False
    ua_from_browser_version = True
    warmup_url = BASE_URL + "/"
    blocked_resource_types = frozenset({"image", "media", "font"})

    def __init__(self, client, delay: float = 0.8, api_key: str = "", **kwargs):
        super().__init__(client, delay, **kwargs)
        self.api_key = (api_key or "").strip()
        self._current_path = ""

    @property
    def source_name(self) -> str:
        return "reed.co.uk"

    # ── Soft-404 detection + salvage (browser path only) ─────────────

    def page_is_blocked(self, html: str) -> bool:
        if not html:
            return False
        return '"notFoundReason"' in html or "404 - page not found" in html[:3000].lower()

    async def on_blocked_page(self, page, html: str) -> str | None:
        """From the live (blocked) page, fetch the Next.js data route for the
        search. When the middleware lets it through it returns clean JSON."""
        m = re.search(r'"buildId":"([^"]+)"', html)
        if not m or not self._current_path:
            return None
        slug, _, query = self._current_path.partition("?")
        data_url = f"/_next/data/{m.group(1)}/jobs/{slug}.json?criteria={slug}"
        if query:
            data_url += "&" + query
        try:
            result = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u, {headers: {'x-nextjs-data': '1'}});
                    const text = await r.text();
                    return {status: r.status, ct: r.headers.get('content-type') || '', text: text.slice(0, 800000)};
                }""", data_url)
        except Exception as e:
            Actor.log.debug(f"[Reed] data-route fetch failed: {e}")
            return None
        if (result and result.get("status") == 200 and "json" in result.get("ct", "")
                and '"pageProps"' in result.get("text", "") and '"notFoundReason"' not in result["text"]):
            Actor.log.info("[Reed] soft-404 bypassed via Next.js data route")
            return result["text"]
        Actor.log.info(f"[Reed] data route also blocked (status {result.get('status') if result else '?'})")
        return None

    # ── Official API path ────────────────────────────────────────────

    async def search(self, keyword, location, max_results=50, job_type="all", salary_min=None):
        if self.api_key:
            return await self._search_api(keyword, location, max_results, job_type, salary_min)
        return await super().search(keyword, location, max_results, job_type, salary_min)

    async def _search_api(self, keyword, location, max_results, job_type, salary_min) -> list[dict]:
        all_jobs: list[dict] = []
        self._reset_for(keyword or "", location or "")
        if self.exhausted:
            return all_jobs
        skip = getattr(self, "_skip", 0) if self._page > 1 else 0
        self.stats["mode"] = "api"
        while len(all_jobs) < max_results and skip < 10_000:
            params = [f"keywords={quote_plus(keyword)}", f"locationName={quote_plus(location)}",
                      "distanceFromLocation=15", "resultsToTake=100", f"resultsToSkip={skip}"]
            if salary_min:
                params.append(f"minimumSalary={int(salary_min)}")
            if job_type in API_TYPE_PARAM:
                params.append(f"{API_TYPE_PARAM[job_type]}=true")
            try:
                r = await self.client.get(f"{API_URL}?" + "&".join(params), auth=(self.api_key, ""),
                                          headers={"Accept": "application/json"})
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                Actor.log.warning(f"[Reed API] request failed: {e}")
                self.exhausted = True
                break
            results = data.get("results", []) or []
            if not results:
                self.exhausted = True
                break
            fresh = [self._api_item(it) for it in results]
            all_jobs.extend(fresh)
            self.stats["pages"] += 1
            self.stats["jobs"] += len(fresh)
            Actor.log.info(f"[Reed API] +{len(fresh)} (run total {self.stats['jobs']}/{data.get('totalResults', '?')})")
            if self.on_page:
                await self.on_page(self.source_name, fresh)
            skip += len(results)
            self._skip = skip
            self._page += 1
            if skip >= int(data.get("totalResults", 0) or 0):
                self.exhausted = True
                break
        return all_jobs

    def _api_item(self, it: dict) -> dict:
        lo, hi = it.get("minimumSalary"), it.get("maximumSalary")
        cur = it.get("currency") or "GBP"
        desc = it.get("jobDescription") or ""
        job = {
            "source": self.source_name,
            "title": clean_text(it.get("jobTitle")),
            "company": clean_text(it.get("employerName")),
            "location": clean_text(it.get("locationName")),
            "snippet": clean_text(desc)[:500],
            "full_description": desc,
            "date_posted": it.get("date", ""),
            "valid_through": it.get("expirationDate", ""),
            "url": strip_tracking(it.get("jobUrl", "")),
            "job_id": str(it.get("jobId", "")),
            "salary_currency": cur,
        }
        if lo or hi:
            lo = float(lo or hi)
            hi = float(hi or lo)
            sym = "£" if cur == "GBP" else cur + " "
            raw = f"{sym}{lo:,.0f} per annum" if lo == hi else f"{sym}{lo:,.0f} - {sym}{hi:,.0f} per annum"
            apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": cur, "period": "annum"})
        job["work_mode"] = detect_work_mode(job["title"], job["snippet"])
        return job

    # ── HTTP path ────────────────────────────────────────────────────

    @staticmethod
    def _slugify(text: str) -> str:
        text = re.sub(r"[^a-z0-9\s-]", "", (text or "").lower().strip())
        return re.sub(r"[\s-]+", "-", text).strip("-")

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        kw, loc = self._slugify(keyword), self._slugify(location)
        path = f"{kw}-jobs-in-{loc}" if loc else f"{kw}-jobs"
        params = ["sortby=DisplayDate"]
        if page > 1:
            params.append(f"pageno={page}")
        if job_type in JOB_TYPE_PARAM:
            params.append(JOB_TYPE_PARAM[job_type])
        if salary_min:
            params.append(f"salaryFrom={int(salary_min)}")
        query = "&".join(params)
        self._current_path = f"{path}?{query}"
        return f"{BASE_URL}/jobs/{path}?{query}"

    def _parse_search(self, html: str, soup) -> tuple[list[dict], bool]:
        # Salvaged Next.js data-route JSON (returned by on_blocked_page)
        stripped = html.lstrip()
        if stripped.startswith("{"):
            try:
                jobs = self._jobs_from_next_props(json.loads(stripped))
            except (json.JSONDecodeError, ValueError):
                jobs = []
            return jobs, len(jobs) >= self.page_size
        if self.page_is_blocked(html):
            Actor.log.warning("[Reed] soft-404 (bot-blocked) page; refusing to parse it")
            return [], False

        jobs = self._parse_cards(soup)
        nd_jobs = []
        nd = self._extract_next_data(soup)
        if nd:
            nd_jobs = self._jobs_from_next_props(nd)
        if len(nd_jobs) > len(jobs) * 2:
            jobs, nd_jobs = nd_jobs, jobs
        if not jobs:
            jobs = self._extract_jsonld_jobs(soup)
        # Fill gaps (snippets, salary, type) from the secondary source by id
        if jobs and nd_jobs:
            by_id = {j.get("job_id"): j for j in nd_jobs if j.get("job_id")}
            for job in jobs:
                extra = by_id.get(job.get("job_id"))
                if extra:
                    for k, v in extra.items():
                        if v and not job.get(k):
                            job[k] = v
        has_next = bool(soup.select_one('a[aria-label="Next"], a[rel="next"], a[href*="pageno="]')) and len(jobs) >= 20
        return jobs, has_next

    def _parse_cards(self, soup) -> list[dict]:
        jobs = []
        for card in soup.select('article[data-qa="job-card"]') or soup.select("article"):
            title_el = card.select_one('a[data-qa="job-card-title"]') or card.select_one('h2 a, h3 a, a[href*="/jobs/"]')
            if not title_el:
                continue
            href = title_el.get("href", "")
            if "/jobs/" not in href or "/courses/" in href:
                continue
            job = {"source": self.source_name, "title": clean_text(title_el.get_text())}
            job["url"] = strip_tracking(urljoin(BASE_URL, href))
            jid = title_el.get("data-id") or re.sub(r"\D", "", card.get("data-id", "") or "")
            if not jid:
                m = re.search(r"/(\d{5,})", href)
                jid = m.group(1) if m else ""
            job["job_id"] = jid

            posted = card.select_one('[data-qa="job-posted-by"]')
            if posted:
                company_el = posted.select_one("a") or card.select_one('[data-element="recruiter"]')
                job["company"] = clean_text(company_el.get_text()) if company_el else ""
                text = clean_text(posted.get_text())
                m = re.match(r"(.+?)\s+by\s+", text, re.IGNORECASE)
                job["date_posted"] = m.group(1) if m else text
                if not job["company"]:
                    m = re.search(r"\bby\s+(.+)$", text, re.IGNORECASE)
                    job["company"] = clean_text(m.group(1)) if m else ""

            sal_el = card.select_one('[data-qa="job-metadata-salary"]')
            if sal_el:
                apply_salary(job, parse_salary(sal_el.get_text(), self.default_currency))
            loc_el = card.select_one('[data-qa="job-metadata-location"]')
            if loc_el:
                job["location"] = clean_text(loc_el.get_text())

            for li in card.select('[data-qa="job-metadata"] li'):
                if li.get("data-qa"):
                    continue
                text = clean_text(li.get_text())
                if not job.get("work_mode"):
                    wm = detect_work_mode(text)
                    if wm and len(text) < 20:
                        job["work_mode"] = wm
                        continue
                if not job.get("employment_type"):
                    et = detect_employment_type(text)
                    if et:
                        job["employment_type"] = et

            if not job.get("salary_raw"):
                apply_salary(job, extract_salary_from_text(card.get_text(" "), self.default_currency))
            if job.get("company", "").lower().startswith(("job hidden", "undo")):
                job["company"] = ""
            jobs.append(job)
        return jobs

    # ── __NEXT_DATA__ / data-route JSON mapping (kept from v0.11) ────

    _TITLE_KEYS = ("jobTitle", "title", "displayTitle")
    _ID_KEYS = ("jobId", "id", "vacancyId", "jobID")
    _SALARY_PLACEHOLDER = {(12000.0, 160000.0)}
    _SALARY_TYPE = {1: "hour", 2: "day", 3: "week", 4: "month", 5: "annum"}

    @staticmethod
    def _unwrap(d):
        if isinstance(d, dict) and isinstance(d.get("jobDetail"), dict):
            return {**d, **d["jobDetail"]}
        return d

    @classmethod
    def _looks_like_job(cls, d) -> bool:
        if not isinstance(d, dict):
            return False
        d = cls._unwrap(d)
        has_title = any(isinstance(d.get(k), str) and d[k].strip() for k in cls._TITLE_KEYS)
        has_id = (any(d.get(k) not in (None, "") for k in cls._ID_KEYS)
                  or any("/jobs/" in d[k] for k in ("url", "jobUrl") if isinstance(d.get(k), str)))
        return has_title and has_id

    def _jobs_from_next_props(self, data: dict) -> list[dict]:
        """Largest list of job-looking dicts anywhere inside the Next.js props."""
        if not isinstance(data, dict):
            return []
        props = (data.get("props") or {}).get("pageProps") or data.get("pageProps") or data
        best: list = []
        stack = [props]
        visited = 0
        while stack and visited < 20000:
            obj = stack.pop()
            visited += 1
            if isinstance(obj, list):
                job_like = [x for x in obj if self._looks_like_job(x)]
                if len(job_like) > len(best):
                    best = job_like
                stack.extend(x for x in obj if isinstance(x, (dict, list)))
            elif isinstance(obj, dict):
                stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
        jobs = [self._map_reed_job(self._unwrap(j)) for j in best]
        return [j for j in jobs if "/jobs/" in j.get("url", "") and "/courses/" not in j.get("url", "")]

    @staticmethod
    def _first(d: dict, keys):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v:
                return v
            if isinstance(v, dict):
                for kk in ("name", "displayName", "displayLocation", "locality", "city", "label", "value"):
                    vv = v.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        return vv.strip()
        return ""

    def _map_reed_job(self, d: dict) -> dict:
        job = {"source": self.source_name, "title": clean_text(str(self._first(d, self._TITLE_KEYS)))}
        company = self._first(d, ("ouName", "profileName", "employerName", "companyName", "employer",
                                  "recruiterName", "brandName", "advertiserName", "postedBy"))
        if company:
            job["company"] = clean_text(str(company))
        location = self._first(d, ("displayLocationName", "countyLocation", "locationName", "displayLocation",
                                   "location", "jobLocation", "city", "town"))
        if location:
            job["location"] = clean_text(str(location))
        job_id = self._first(d, self._ID_KEYS)
        if job_id:
            job["job_id"] = str(int(job_id)) if isinstance(job_id, float) else str(job_id)
        url = self._first(d, ("jobUrl", "url", "canonicalUrl"))
        if isinstance(url, str) and url:
            job["url"] = strip_tracking(urljoin(BASE_URL, url))
        elif job.get("job_id"):
            job["url"] = f"{BASE_URL}/jobs/{job['job_id']}"
        sal_text = self._first(d, ("salary", "displaySalary", "salaryText", "salaryLabel", "salaryRange"))
        if isinstance(sal_text, str) and sal_text:
            apply_salary(job, parse_salary(sal_text, self.default_currency))
        if not job.get("salary_min"):
            def _num(v):
                return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
            smin = _num(d.get("minimumSalary")) or _num(d.get("salaryFrom")) or _num(d.get("minSalary"))
            smax = _num(d.get("maximumSalary")) or _num(d.get("salaryTo")) or _num(d.get("maxSalary"))
            if (smin, smax) in self._SALARY_PLACEHOLDER:
                smin = smax = None
            if smin or smax:
                lo, hi = smin or smax, smax or smin
                st = d.get("salaryType")
                if isinstance(st, (int, float)) and not isinstance(st, bool):
                    period = self._SALARY_TYPE.get(int(st), "annum")
                else:
                    per_raw = str(st or d.get("salaryPeriod") or "").lower()
                    period = ("hour" if "hour" in per_raw else "day" if "day" in per_raw else
                              "week" if "week" in per_raw else "month" if "month" in per_raw else "annum")
                label = {"annum": "per annum", "month": "per month", "week": "per week", "day": "per day", "hour": "per hour"}[period]
                raw = f"£{lo:,.0f} {label}" if lo == hi else f"£{lo:,.0f} - £{hi:,.0f} {label}"
                apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": "GBP", "period": period})
        date = self._first(d, ("displayDate", "datePosted", "date", "postedDate", "createdOn", "postedOn"))
        if date:
            job["date_posted"] = clean_text(str(date))
        emp = self._first(d, ("contractType", "employmentType"))
        if not isinstance(emp, str):
            emp = ""
        if not emp:
            types = d.get("contractTypes") or d.get("employmentTypes")
            if isinstance(types, list) and types:
                emp = ", ".join(str(t) for t in types if t)
        if not emp:
            jt = d.get("jobType")
            if isinstance(jt, (int, float)) and not isinstance(jt, bool):
                emp = {1: "Permanent", 2: "Contract", 3: "Temporary"}.get(int(jt), "")
        if not emp:
            emp = ", ".join(label for flag, label in (("isPermanent", "Permanent"), ("isContract", "Contract"),
                                                      ("isTemp", "Temporary")) if d.get(flag) is True)
        hours = ("Full-time" if d.get("isFullTime") and not d.get("isPartTime")
                 else "Part-time" if d.get("isPartTime") and not d.get("isFullTime") else "")
        parts = [p for p in (emp, hours) if p]
        if parts:
            job["employment_type"] = clean_text(", ".join(parts))
        desc = self._first(d, ("jobDescriptionSnippet", "jobDescription", "description", "shortDescription", "descriptionSnippet"))
        if isinstance(desc, str) and desc:
            job["snippet"] = clean_text(re.sub(r"<[^>]+>", " ", desc))[:500]
        job["work_mode"] = detect_work_mode(job.get("location"), job.get("snippet"))
        return job
