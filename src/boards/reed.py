"""Reed.co.uk job board scraper."""

import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import BaseScraper, JobListing, parse_salary, clean_text

BASE_URL = "https://www.reed.co.uk"

JOB_TYPE_MAP = {
    "all": "",
    "permanent": "perm",
    "temporary": "temp",
    "contract": "contract",
    "part-time": "parttime",
}


class ReedScraper(BaseScraper):

    def __init__(self, client, delay: float = 1.5, **kwargs):
        super().__init__(client, delay, **kwargs)
        self.fetch_details = False  # Set by main.py, saves ~50% requests

    @property
    def source_name(self) -> str:
        return "reed.co.uk"

    def _build_url(self, keyword: str, location: str, job_type: str, salary_min: int | None, page: int) -> str:
        kw = quote_plus(keyword)
        loc = quote_plus(location)
        url = f"{BASE_URL}/jobs/{kw}-jobs-in-{loc}"

        params = []
        if page > 1:
            params.append(f"pageno={page}")
        reed_type = JOB_TYPE_MAP.get(job_type, "")
        if reed_type:
            params.append(f"employmenttype={reed_type}")
        if salary_min:
            params.append(f"salaryfrom={salary_min}")
        params.append("sortby=DisplayDate")

        if params:
            url += "?" + "&".join(params)
        return url

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs = []
        page = 1
        detail_count = 0
        MAX_DETAIL_FETCHES = 5  # Cap detail page fetches to avoid timeouts

        while len(all_jobs) < max_results:
            url = self._build_url(keyword, location, job_type, salary_min, page)
            Actor.log.info(f"[Reed] Scraping page {page}: {url}")

            html = await self._get_html(url)
            if not html:
                break

            jobs, has_next = self._parse_search(html)
            if not jobs:
                Actor.log.info(f"[Reed] No jobs on page {page}, stopping.")
                break

            for job in jobs:
                if len(all_jobs) >= max_results:
                    break

                # Only fetch detail for jobs missing key fields, capped to avoid timeouts
                needs_detail = not job.get("location") or not job.get("snippet")
                if self.fetch_details and job.get("url") and needs_detail and detail_count < MAX_DETAIL_FETCHES:
                    detail = await self._parse_detail(job["url"])
                    for k, v in detail.items():
                        if v and not job.get(k):
                            job[k] = v
                    detail_count += 1
                    await self._polite_delay()

                all_jobs.append(job)

            if not has_next or len(all_jobs) >= max_results:
                break

            page += 1
            await self._polite_delay()

        Actor.log.info(f"[Reed] Total scraped: {len(all_jobs)}")
        return all_jobs

    def _parse_search(self, html: str) -> tuple[list[dict], bool]:
        # Collect from ALL sources, merge for maximum data coverage
        all_sources = []

        # 0. JS-extracted from Playwright — then fix Reed-specific issues
        js_jobs = self._process_js_extracted()
        if js_jobs:
            self._fixup_reed_jobs(js_jobs)
            all_sources.append(("JS", js_jobs))
            Actor.log.info(f"[Reed] JS extraction: {len(js_jobs)} jobs with fields: "
                          f"{sum(1 for j in js_jobs if j.get('company'))}/{len(js_jobs)} company, "
                          f"{sum(1 for j in js_jobs if j.get('location'))}/{len(js_jobs)} location, "
                          f"{sum(1 for j in js_jobs if j.get('salary_raw'))}/{len(js_jobs)} salary")

        # 1. HTML parsing
        html_jobs, html_has_next = self._parse_html_cards(html)
        if html_jobs:
            all_sources.append(("HTML", html_jobs))

        # 2. JSON-LD
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            all_sources.append(("JSON-LD", jsonld_jobs))

        if not all_sources:
            return [], False

        # Use the source with most jobs as base
        all_sources.sort(key=lambda x: len(x[1]), reverse=True)
        best_name, best_jobs = all_sources[0]
        Actor.log.info(f"[Reed] Primary source: {best_name} ({len(best_jobs)} jobs)")

        # Merge data from other sources into base jobs (by URL or position)
        for source_name, source_jobs in all_sources[1:]:
            src_lookup = {}
            for j in source_jobs:
                if j.get("url"):
                    src_lookup[j["url"]] = j
                if j.get("job_id"):
                    src_lookup[j["job_id"]] = j
            for i, job in enumerate(best_jobs):
                match = src_lookup.get(job.get("url")) or src_lookup.get(job.get("job_id"))
                if not match and i < len(source_jobs):
                    match = source_jobs[i]
                if match:
                    for k, v in match.items():
                        if v and not job.get(k):
                            job[k] = v
            Actor.log.info(f"[Reed] Merged {source_name} data")

        has_next = html_has_next or len(best_jobs) >= 20
        return best_jobs, has_next

    def _fixup_reed_jobs(self, jobs: list[dict]) -> None:
        """Fix Reed-specific issues in JS-extracted jobs.

        Reed hides company names behind "Job hidden.Undo" overlays, so the
        JS extraction picks up "Job hidden." as the company. The real company
        name appears after "by" in the card text. Reed also shows NO real job
        descriptions on search cards — only metadata (date, salary, location,
        type). So snippets that are just metadata should be cleared.
        """
        for job in jobs:
            # Get the raw card text stored by _process_js_extracted
            raw = next((r for r in self._last_browser_extracted
                       if r.get("url") == job.get("url")), None)
            card_text = raw.get("_card_text", "") if raw else ""

            # ── Fix company ──
            company = job.get("company", "")
            if not company or "job hidden" in company.lower() or company.lower().startswith("undo"):
                # Extract real company from "by {Company Name}" in card text
                extracted = self._extract_reed_company(card_text, job)
                if extracted:
                    job["company"] = extracted
                else:
                    job["company"] = ""  # Better empty than wrong

            # ── Fix snippet ──
            # Reed search cards have NO real job descriptions. The snippet is
            # just concatenated metadata: "Job hidden.Undo 25 March by Company
            # £45,000 London Permanent See more". Clear it — it's noise.
            snip = job.get("snippet", "")
            if snip and self._is_reed_metadata_snippet(snip, job):
                job["snippet"] = ""

            # ── Fix location/date/employment from card text ──
            if card_text:
                meta = self._parse_reed_metadata(card_text, job)
                if not job.get("location") and meta.get("location"):
                    job["location"] = meta["location"]
                if not job.get("date_posted") and meta.get("date_posted"):
                    job["date_posted"] = meta["date_posted"]
                if not job.get("employment_type") and meta.get("employment_type"):
                    job["employment_type"] = meta["employment_type"]

    def _extract_reed_company(self, card_text: str, job: dict) -> str:
        """Extract the real company name from Reed card text.

        Card text format: "... {date} by {Company Name} {salary/location}..."
        The company is between "by" and the next known field (salary, location,
        or employment type). Handles concatenated text where spaces may be missing.
        """
        if not card_text:
            return ""

        # Find "by" marker — may or may not have spaces around it in concatenated text
        by_match = re.search(r'\s?by\s+', card_text, re.IGNORECASE)
        if not by_match:
            # Also try "by" at start of a segment (after newline or after date)
            by_match = re.search(r'(?:ago|yesterday|today|\d{4})\s*by\s*', card_text, re.IGNORECASE)
        if not by_match:
            return ""

        after_by = card_text[by_match.end():]

        # The company name ends at the first known delimiter:
        # - A salary pattern (£, $, €, Competitive, Negotiable)
        # - A location (city name that we recognise)
        # - An employment type (Permanent, Contract, etc.)
        # - "See more", "Easy apply", "Work from home"
        end_pattern = re.compile(
            r'[£$€]|'
            r'(?:Competitive|Negotiable|Attractive)\s*(?:salary)?|'
            r'(?:London|Manchester|Birmingham|Leeds|Bristol|Liverpool|'
            r'Sheffield|Glasgow|Edinburgh|Cardiff|Newcastle|Nottingham|'
            r'Southampton|Oxford|Cambridge|Reading|Brighton|Bath|York|'
            r'Leicester|Coventry|Swindon|Warwick|Derby|Exeter|Norwich|'
            r'Plymouth|Aberdeen|Dundee|Belfast|Milton Keynes|'
            r'City of London|West London|East London|Central London|'
            r'North London|South London|Canary Wharf|Croydon|Enfield|'
            r'Remote|Hybrid|Home\s*based|[A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})|'
            r'(?:Permanent|Contract|Temporary|Fixed[\s-]?term|Freelance|'
            r'Part[\s-]?time|Full[\s-]?time|Apprenticeship)|'
            r'(?:See\s*more|Easy\s*apply|Work\s*from\s*home)',
            re.IGNORECASE
        )

        end_match = end_pattern.search(after_by)
        if end_match:
            company = after_by[:end_match.start()].strip(' ,.-/')
        else:
            # Take first 60 chars max
            company = after_by[:60].strip()

        # Clean up
        company = re.sub(r'\s+', ' ', company).strip()
        if company and len(company) > 1 and len(company) < 100:
            return company
        return ""

    def _is_reed_metadata_snippet(self, snippet: str, job: dict | None = None) -> bool:
        """Check if a snippet is just Reed card metadata (not a real description).

        Reed search cards don't show job descriptions — only title, date,
        company, salary, location, and employment type. A real snippet would
        have sentences about the actual job.
        """
        # Strip all known metadata patterns
        check = snippet
        # Remove the job title (which often appears verbatim at the start)
        if job:
            title = job.get("title", "")
            if title:
                check = check.replace(title, "", 1)
            company = job.get("company", "")
            if company:
                check = check.replace(company, "", 1)
        check = re.sub(r'Job\s*hidden\.?\s*Undo', '', check, flags=re.IGNORECASE)
        check = self._REED_BADGES.sub('', check)
        check = self._REED_DATE.sub('', check)
        check = self._REED_EMP_TYPES.sub('', check)
        check = self._REED_LOCATIONS.sub('', check)
        # Remove salary
        check = re.sub(
            r'[£$€]\s*[\d,]+(?:\s*[-–]\s*[£$€]?\s*[\d,]+)?'
            r'(?:\s*(?:per|p\.?a\.?|pa|annum|year|day|hour|hr|week|month)\w*)?',
            '', check, flags=re.IGNORECASE
        )
        check = re.sub(r'(?:Competitive|Negotiable|Attractive)\s*(?:salary)?',
                        '', check, flags=re.IGNORECASE)
        # Remove common filler
        check = re.sub(r'\bby\b', '', check, flags=re.IGNORECASE)
        check = re.sub(r'\s+', ' ', check).strip(' ,.-/')
        # If less than 30 chars remain, it's just metadata
        return len(check) < 30

    def _parse_html_cards(self, html: str) -> tuple[list[dict], bool]:
        """Parse HTML cards from Reed search results."""
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        cards = (
            soup.select('article[data-qa="job-card"]')
            or soup.select("article")
            or soup.select('[class*="job-card"]')
            or soup.select('[class*="job-result"]')
            or soup.select('[class*="search-result"]')
        )

        for card in cards:
            job = {"source": self.source_name}

            # Title + URL
            title_el = (
                card.select_one('a[data-qa="job-card-title"]')
                or card.select_one("h2 a")
                or card.select_one("h3 a")
                or card.select_one('a[href*="/jobs/"]')
            )
            if not title_el:
                continue

            job["title"] = clean_text(title_el.get_text())
            href = title_el.get("href", "")
            job["url"] = urljoin(BASE_URL, href)

            id_match = re.search(r"/(\d+)", href)
            if id_match:
                job["job_id"] = id_match.group(1)

            # Company — Reed hides company behind "Job hidden.Undo" overlay
            for sel in ['[data-qa="job-card-company"]', ".gtmJobListingPostedBy",
                        '[class*="company"]', '[class*="posted-by"]', '[class*="employer"]',
                        '[class*="recruiter"]']:
                el = card.select_one(sel)
                if el:
                    text = clean_text(el.get_text())
                    # Reject "Job hidden." overlay text
                    if text and "job hidden" not in text.lower() and not text.lower().startswith("undo"):
                        job["company"] = text
                    break

            # If CSS didn't get a real company, extract from card text "by {Company}"
            if not job.get("company"):
                card_text_for_company = clean_text(card.get_text())
                extracted = self._extract_reed_company(card_text_for_company, job)
                if extracted:
                    job["company"] = extracted

            # Location
            for sel in ['[data-qa="job-card-location"]', ".job-result-location",
                        '[class*="location"]', '[class*="job-location"]',
                        '[class*="where"]']:
                el = card.select_one(sel)
                if el:
                    text = clean_text(el.get_text())
                    text = re.sub(r'^Location\s*:?\s*', '', text, flags=re.IGNORECASE).strip()
                    if text and len(text) > 1:
                        job["location"] = text
                        break

            # Validate location — Reed CSS selectors often grab concatenated text
            # like "AccentureLondon" or "Harvey Nash annumLondon"
            loc = job.get("location", "")
            company = job.get("company", "")
            if loc and company and loc.startswith(company):
                loc = loc[len(company):].strip()
                loc = re.sub(r'^(?:annum|day|hour|week|month|year)\s*', '', loc, flags=re.IGNORECASE).strip()
                job["location"] = loc if loc and len(loc) > 1 else ""

            # Salary — CSS selectors
            for sel in ['[data-qa="job-card-salary"]', ".job-result-salary",
                        '[class*="salary"]', '[class*="pay"]']:
                el = card.select_one(sel)
                if el:
                    sal = parse_salary(el.get_text())
                    if sal.get("min") or sal.get("max"):
                        job["salary_raw"] = sal["raw"]
                        job["salary_min"] = sal["min"]
                        job["salary_max"] = sal["max"]
                        job["salary_period"] = sal["period"]
                        break

            # Salary — text-based fallback
            if not job.get("salary_raw"):
                sal = self._extract_salary_from_text(card.get_text())
                if sal and (sal.get("min") or sal.get("max")):
                    job["salary_raw"] = sal["raw"]
                    job["salary_min"] = sal["min"]
                    job["salary_max"] = sal["max"]
                    job["salary_period"] = sal["period"]

            # Snippet
            for sel in ['[data-qa="job-card-description"]', ".job-result-description",
                        '[class*="description"]', '[class*="snippet"]', "p"]:
                el = card.select_one(sel)
                if el:
                    text = clean_text(el.get_text())
                    if len(text) > 15:
                        job["snippet"] = text[:500]
                        break

            # Validate snippet — reject if just title+location+type metadata
            snip = job.get("snippet", "")
            if snip and self._is_reed_metadata_snippet(snip, job):
                job["snippet"] = ""

            # Date
            el = card.select_one("time") or card.select_one('[data-qa="job-card-date"]') or card.select_one('[class*="date"]')
            if el:
                job["date_posted"] = el.get("datetime", clean_text(el.get_text()))

            # Employment type
            for sel in ['[class*="contract"]', '[class*="job-type"]', '[class*="employment"]']:
                el = card.select_one(sel)
                if el:
                    job["employment_type"] = clean_text(el.get_text())
                    break

            # ── Reed-specific metadata extraction from concatenated card text ──
            card_text = clean_text(card.get_text())
            meta = self._parse_reed_metadata(card_text, job)

            if not job.get("location") and meta.get("location"):
                job["location"] = meta["location"]
            if not job.get("date_posted") and meta.get("date_posted"):
                job["date_posted"] = meta["date_posted"]
            if not job.get("employment_type") and meta.get("employment_type"):
                job["employment_type"] = meta["employment_type"]
            if not job.get("snippet") and meta.get("snippet"):
                job["snippet"] = meta["snippet"]

            if job.get("title"):
                jobs.append(job)

        has_next = bool(soup.select_one('a[aria-label="Next"]') or soup.select("a[href*='pageno=']"))
        return jobs, has_next

    # ── Reed concatenated-text metadata patterns (no \b needed) ──
    _REED_EMP_TYPES = re.compile(
        r'(Permanent|Contract|Temporary|Fixed[\s-]?term|Freelance)'
        r'(?:\s*,\s*(full[\s-]?time|part[\s-]?time))?',
        re.IGNORECASE,
    )
    _REED_DATE = re.compile(
        r'(\d+\s*(?:hr|hour|day|week|month|minute|min)s?\s*ago|Yesterday|Today'
        r'|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*'
        r'(?:\s*\d{2,4})?)',
        re.IGNORECASE,
    )
    _REED_BADGES = re.compile(
        r'(?:Featured|Promoted|New|EarlyBird|Early\s*Bird|Easy\s*apply|'
        r'Job\s*hidden\.?\s*Undo|See\s*more|Work\s*from\s*home)',
        re.IGNORECASE,
    )
    _REED_LOCATIONS = re.compile(
        r'(London|Manchester|Birmingham|Leeds|Bristol|Liverpool|Sheffield|'
        r'Glasgow|Edinburgh|Cardiff|Newcastle|Nottingham|Southampton|Oxford|'
        r'Cambridge|Reading|Brighton|Bath|York|Leicester|Coventry|Swindon|'
        r'Warwick|Derby|Exeter|Norwich|Plymouth|Aberdeen|Dundee|Belfast|'
        r'City of London|West London|East London|Central London|North London|'
        r'South London|Canary Wharf|Paddington|Croydon|Bromley|Enfield|'
        r'Milton Keynes|Stoke[\s-]?on[\s-]?Trent|Kingston upon Thames|'
        r'Remote|Hybrid|England|Scotland|Wales|'
        r'[A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})'  # UK postcode
        r'(?:\s*,\s*[A-Za-z\s]+)?',  # optional region after comma
        re.IGNORECASE,
    )

    def _parse_reed_metadata(self, card_text: str, job: dict) -> dict:
        """Parse Reed's concatenated card text to extract structured fields.

        Reed card text looks like:
          "Job hidden.UndoPromotedJava Software Engineer1 hr ago by
           Competitive salaryLondonPermanent, full-timeSee more"

        Returns dict with location, date_posted, employment_type, snippet.
        """
        meta = {}
        text = card_text

        # 1. Strip noise prefixes/suffixes
        text = re.sub(r'^(?:Job\s*hidden\.?\s*Undo\s*)', '', text)
        text = self._REED_BADGES.sub(' ', text)
        text = text.strip()

        # 2. Extract employment type (no word boundary needed — uses capitalized keywords)
        emp_match = self._REED_EMP_TYPES.search(text)
        if emp_match:
            emp_type = emp_match.group(1).strip().title()
            if emp_match.group(2):
                emp_type += ", " + emp_match.group(2).strip().title()
            meta["employment_type"] = emp_type
            # Remove from text for location extraction
            text_for_loc = text[:emp_match.start()] + text[emp_match.end():]
        else:
            text_for_loc = text

        # 3. Extract date (works without \b — patterns are numeric or capitalized)
        date_match = self._REED_DATE.search(text)
        if date_match:
            meta["date_posted"] = date_match.group(1).strip()

        # 4. Extract location from the text between "by" and employment type
        # Pattern: "{date} by [{salary}]{location}{employment_type}"
        # First try: find text between " by " marker and employment type
        by_match = re.search(r'\sby\s', text)
        if by_match and emp_match:
            between = text[by_match.end():emp_match.start()]
            # Remove salary text (£XX,XXX - £YY,YYY or "Competitive salary" etc.)
            between = re.sub(
                r'[£$€]\s*[\d,]+(?:\s*[-–]\s*[£$€]?\s*[\d,]+)?'
                r'(?:\s*(?:per|p\.?a\.?|pa|annum|year|day|hour|hr|week|month)\w*)?',
                '', between, flags=re.IGNORECASE
            )
            between = re.sub(r'(?:Competitive|Negotiable|Attractive)\s*(?:salary)?',
                             '', between, flags=re.IGNORECASE)
            between = between.strip(' ,.-/')
            if between and len(between) > 1:
                meta["location"] = between

        # 5. If no "by" marker, try direct location match from full text
        if not meta.get("location"):
            loc_match = self._REED_LOCATIONS.search(text_for_loc)
            if loc_match:
                meta["location"] = loc_match.group(0).strip().rstrip(',')

        # 6. Build a clean snippet — remove all the metadata noise
        if not job.get("snippet"):
            snippet = card_text
            # Remove title, company, salary
            for remove in [job.get("title", ""), job.get("company", ""), job.get("salary_raw", "")]:
                if remove:
                    snippet = snippet.replace(remove, "", 1)
            # Remove extracted metadata
            snippet = self._REED_BADGES.sub('', snippet)
            snippet = re.sub(r'Job\s*hidden\.?\s*Undo', '', snippet)
            if meta.get("date_posted"):
                snippet = snippet.replace(meta["date_posted"], "", 1)
            if emp_match:
                snippet = snippet[:emp_match.start()] + snippet[emp_match.end():]
            # Remove "by" prefix and salary noise
            snippet = re.sub(r'\sby\s+', ' ', snippet)
            snippet = re.sub(
                r'[£$€]\s*[\d,]+(?:\s*[-–]\s*[£$€]?\s*[\d,]+)?'
                r'(?:\s*(?:per|p\.?a\.?|pa|annum|year|day|hour|hr|week|month)\w*)?',
                '', snippet, flags=re.IGNORECASE
            )
            snippet = re.sub(r'(?:Competitive|Negotiable|Attractive)\s*(?:salary)?',
                             '', snippet, flags=re.IGNORECASE)
            snippet = re.sub(r'\s{2,}', ' ', snippet).strip(' ,.-/')
            if len(snippet) > 30:
                meta["snippet"] = snippet[:500]

        return meta

    async def _parse_detail(self, url: str) -> dict:
        html = await self._fetch_detail(url)
        if not html:
            return {}

        # Try JSON-LD on the detail page (most complete structured data)
        jsonld_jobs = self._extract_jsonld_jobs(html)
        if jsonld_jobs:
            # Return the first job's data as detail fields
            return jsonld_jobs[0]

        soup = BeautifulSoup(html, "html.parser")
        details = {}

        # Description
        for sel in ['[itemprop="description"]', ".description", '[class*="job-description"]',
                    '[class*="vacancy-description"]', "#job-description"]:
            el = soup.select_one(sel)
            if el:
                details["full_description"] = el.get_text(separator="\n", strip=True)
                if not details.get("snippet"):
                    details["snippet"] = clean_text(el.get_text())[:500]
                break

        # Location
        for sel in ['[itemprop="addressLocality"]', '[itemprop="jobLocation"]',
                    '[class*="location"]', '[data-qa*="location"]']:
            el = soup.select_one(sel)
            if el:
                details["location"] = clean_text(el.get_text())
                break

        # Salary
        for sel in ['[itemprop="baseSalary"]', '[class*="salary"]', '[data-qa*="salary"]']:
            el = soup.select_one(sel)
            if el:
                sal = parse_salary(el.get_text())
                details["salary_raw"] = sal["raw"]
                details["salary_min"] = sal["min"]
                details["salary_max"] = sal["max"]
                details["salary_period"] = sal["period"]
                break

        # Company
        for sel in ['[itemprop="hiringOrganization"]', '[itemprop="name"]',
                    '[class*="company"]', '[data-qa*="company"]']:
            el = soup.select_one(sel)
            if el:
                text = clean_text(el.get_text())
                if text and len(text) < 100:
                    details["company"] = text
                    break

        # Date
        el = soup.select_one('[itemprop="datePosted"]')
        if el:
            details["date_posted"] = el.get("content", el.get_text(strip=True))

        el = soup.select_one('[itemprop="validThrough"]')
        if el:
            details["valid_through"] = el.get("content", el.get_text(strip=True))

        # Employment type
        for sel in ['[itemprop="employmentType"]', '[class*="contract"]', '[class*="job-type"]']:
            el = soup.select_one(sel)
            if el:
                details["employment_type"] = clean_text(el.get_text())
                break

        return details