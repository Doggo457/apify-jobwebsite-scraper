"""
Shared utilities for International Jobs Board scrapers.
Common salary parsing, data normalisation, base scraper class with
Playwright browser support, stealth JS, and proxy rotation.
"""

import json
import re
import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse

import httpx
from apify import Actor
from bs4 import BeautifulSoup


# ── Stealth JS injected into every browser context ──────────────────────
STEALTH_JS = """
// Hide webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// Fake plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
        { name: 'Native Client', filename: 'internal-nacl-plugin' },
    ],
});

// Fake languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-GB', 'en-US', 'en'],
});

// Spoof WebGL renderer
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};

// Chrome runtime
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };

// Permissions API
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);

// Prevent iframe detection
Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    get: function() { return window; }
});

// Override toString for modified functions
const nativeToString = Function.prototype.toString;
Function.prototype.toString = function() {
    if (this === WebGLRenderingContext.prototype.getParameter) {
        return 'function getParameter() { [native code] }';
    }
    return nativeToString.call(this);
};
"""

# ── JS extraction script: reads visible job cards from rendered DOM ────
# This is far more robust than CSS selectors — it reads what's actually
# visible on screen regardless of class names or HTML structure.
JS_EXTRACT_JOBS = """
(() => {
    const jobs = [];
    // Try multiple selectors for job cards
    const selectors = [
        '[data-testid*="job"]', 'article',
        '[class*="JobCard"]', '[class*="job-card"]',
        '[class*="SearchResult"]', '[class*="search-result"]',
        '[class*="job-result"]', 'li[class*="job"]',
    ];
    let cards = [];
    for (const sel of selectors) {
        const found = document.querySelectorAll(sel);
        if (found.length > cards.length) cards = found;
    }
    if (cards.length < 2) {
        // Last resort: find containers with multiple job-like links
        const allLinks = document.querySelectorAll('a[href*="/job/"]');
        const parents = new Set();
        for (const a of allLinks) {
            if (a.parentElement && a.parentElement.parentElement) {
                parents.add(a.parentElement.parentElement);
            }
        }
        if (parents.size > 2) cards = parents;
    }

    for (const card of cards) {
        // Find the best title link
        const allLinks = card.querySelectorAll('a');
        let titleLink = null;
        for (const a of allLinks) {
            const text = (a.innerText || a.textContent || '').trim();
            if (text.length > 3 && text.length < 200 && a.href &&
                (a.href.includes('/job/') || a.href.includes('/jobs/'))) {
                titleLink = a;
                break;
            }
        }
        if (!titleLink) {
            // Fallback: first link with substantial text
            for (const a of allLinks) {
                const text = (a.innerText || a.textContent || '').trim();
                if (text.length > 5 && text.length < 200 && a.href) {
                    titleLink = a;
                    break;
                }
            }
        }
        if (!titleLink) continue;

        const title = (titleLink.innerText || titleLink.textContent || '').trim();
        if (!title || title.length < 3) continue;

        const job = {
            title: title,
            url: titleLink.href || '',
        };

        // Job ID from URL — handles /job/12345 and company-job12345 patterns
        const idMatch = job.url.match(/\\/job\\/(\\d+)/) || job.url.match(/-job(\\d+)/) || job.url.match(/\\/(\\d{5,})/);
        if (idMatch) job.job_id = idMatch[1];

        // Grab full card text for Python-side processing
        job._card_text = (card.innerText || card.textContent || '').substring(0, 2000);

        // Extract leaf text segments (text in elements with no child elements)
        const segments = [];
        const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT, null, false);
        let node;
        while (node = walker.nextNode()) {
            const text = node.textContent.trim();
            if (text.length > 1 && text.length < 300) {
                // Skip if it's the title text
                if (text === title) continue;
                segments.push(text);
            }
        }
        job._segments = segments.slice(0, 30);

        // Try to find company link (link to company/employer page, not the job)
        for (const a of allLinks) {
            if (a === titleLink) continue;
            const href = a.href || '';
            const text = (a.innerText || a.textContent || '').trim();
            if (text.length > 1 && text.length < 100 &&
                (href.includes('/company/') || href.includes('/employer/') ||
                 href.includes('/recruiter/') || href.includes('/list-jobs/'))) {
                job._company_link_text = text;
                break;
            }
        }

        jobs.push(job);
    }
    return jobs;
})()
"""

# Resources to block in browser (reduces detection, saves bandwidth)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_PATTERNS = [
    "google-analytics", "googletagmanager", "facebook.net",
    "doubleclick.net", "hotjar", "segment.io", "optimizely",
    "newrelic", "sentry.io", "fullstory",
]


@dataclass
class JobListing:
    """Unified job listing format across all boards."""
    title: str = ""
    company: str = ""
    location: str = ""
    salary_raw: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: str = "GBP"
    salary_period: str = "annum"  # annum, day, hour
    snippet: str = ""
    full_description: str = ""
    employment_type: str = ""  # permanent, contract, temporary, part-time
    date_posted: str = ""
    valid_through: str = ""
    url: str = ""
    job_id: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        # Nest salary fields for cleaner output
        d["salary"] = {
            "raw": d.pop("salary_raw"),
            "min": d.pop("salary_min"),
            "max": d.pop("salary_max"),
            "currency": d.pop("salary_currency"),
            "period": d.pop("salary_period"),
        }
        return d


def parse_salary(salary_text: str) -> dict:
    """Parse salary text into structured data."""
    salary_text = salary_text.strip()

    result = {
        "raw": salary_text,
        "min": None,
        "max": None,
        "currency": "GBP",
        "period": "annum",
    }

    if not salary_text or "competitive" in salary_text.lower() or "negotiable" in salary_text.lower():
        return result

    # Detect currency
    if "$" in salary_text or "USD" in salary_text.upper():
        result["currency"] = "USD"
    elif "€" in salary_text or "EUR" in salary_text.upper():
        result["currency"] = "EUR"

    # Detect period
    lower = salary_text.lower()
    if "per day" in lower or "/day" in lower or "a day" in lower:
        result["period"] = "day"
    elif "per hour" in lower or "/hour" in lower or "an hour" in lower or "p/h" in lower:
        result["period"] = "hour"
    elif "per week" in lower or "/week" in lower or "a week" in lower:
        result["period"] = "week"
    elif "per month" in lower or "/month" in lower or "a month" in lower:
        result["period"] = "month"

    # Extract numbers - handle formats like £30,000 or £30k or 30000
    cleaned = salary_text.replace(",", "").replace("£", "").replace("$", "").replace("€", "")
    # Handle 30k format
    cleaned = re.sub(r"(\d+)k\b", lambda m: str(int(m.group(1)) * 1000), cleaned, flags=re.IGNORECASE)

    numbers = re.findall(r"[\d]+(?:\.\d+)?", cleaned)
    numbers = [float(n) for n in numbers if float(n) > 0]

    if len(numbers) >= 2:
        result["min"] = numbers[0]
        result["max"] = numbers[1]
    elif len(numbers) == 1:
        result["min"] = numbers[0]
        result["max"] = numbers[0]

    return result


def clean_text(text: str) -> str:
    """Clean whitespace and normalise text."""
    if not text:
        return ""
    # Collapse multiple whitespace/newlines
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_headers() -> dict:
    """Return standard browser-like headers."""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


class BaseScraper(ABC):
    """Base class for all job board scrapers with optional Playwright browser support."""

    def __init__(self, client: httpx.AsyncClient, delay: float = 1.5,
                 browser=None, proxy_url: str | None = None, proxy_config=None):
        self.client = client
        self.delay = delay
        self.browser = browser
        self.proxy_url = proxy_url
        self.proxy_config = proxy_config
        self._proxy_failures = 0
        self._last_browser_extracted: list[dict] = []  # JS-extracted jobs from last browser fetch

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Name of this job board source."""
        ...

    @abstractmethod
    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        """Run a search and return unified job listings."""
        ...

    # ── HTTP fetching ────────────────────────────────────────────────────

    async def _fetch(self, url: str) -> str | None:
        """Fetch a URL with httpx (no browser)."""
        try:
            response = await self.client.get(url, follow_redirects=True)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as e:
            Actor.log.warning(f"[{self.source_name}] Failed to fetch {url}: {e}")
            return None

    async def _fetch_json(self, url: str) -> dict | None:
        """Fetch JSON from a URL."""
        try:
            response = await self.client.get(url, follow_redirects=True)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            Actor.log.warning(f"[{self.source_name}] Failed to fetch JSON {url}: {e}")
            return None

    # ── Detail page fetching (fast httpx first, browser fallback) ────────

    async def _fetch_detail(self, url: str) -> str | None:
        """Fetch a detail page efficiently.

        Most job detail pages include JSON-LD in raw HTML before JS runs,
        so httpx is usually enough and 10x faster than browser rendering.
        Falls back to browser only when httpx returns a thin/empty page.
        """
        html = await self._fetch(url)
        if html:
            # If raw HTML has JSON-LD or substantial content, no browser needed
            if 'application/ld+json' in html or len(html) > 10000:
                return html
        # Browser fallback for JS-rendered pages
        if self.browser:
            browser_html = await self._fetch_browser(url)
            if browser_html and len(browser_html) > (len(html) if html else 0):
                return browser_html
        return html

    # ── Browser fetching (Playwright + stealth) ──────────────────────────

    async def _get_html(self, url: str) -> str | None:
        """Smart fetch: uses Playwright browser if available, falls back to httpx."""
        if self.browser:
            html = await self._fetch_browser(url)
            if html:
                return html
            # Browser failed all attempts — try httpx as last resort
            Actor.log.info(f"[{self.source_name}] Browser failed, trying httpx fallback for {url}")
            return await self._fetch(url)
        return await self._fetch(url)

    async def _fetch_browser(self, url: str, max_attempts: int = 3) -> str | None:
        """Fetch a URL using Playwright with stealth and proxy rotation."""
        if not self.browser:
            return await self._fetch(url)

        for attempt in range(max_attempts):
            try:
                html = await self._fetch_browser_once(url)
                if html and len(html) > 5000:
                    self._proxy_failures = 0
                    return html
                Actor.log.warning(f"[{self.source_name}] Browser fetch returned thin page ({len(html) if html else 0} bytes), attempt {attempt + 1}/{max_attempts}")
            except Exception as e:
                Actor.log.warning(f"[{self.source_name}] Browser fetch error (attempt {attempt + 1}): {e}")
                # Rotate proxy on timeouts and connection errors
                await self._rotate_proxy()

            if attempt < max_attempts - 1:
                wait = 3 + random.random() * 4
                Actor.log.info(f"[{self.source_name}] Retrying in {wait:.1f}s...")
                await asyncio.sleep(wait)

        Actor.log.warning(f"[{self.source_name}] All {max_attempts} browser attempts failed for {url}")
        return None

    @staticmethod
    def _parse_proxy_url(proxy_url: str) -> dict:
        """Parse a proxy URL into Playwright's proxy format with separate credentials."""
        parsed = urlparse(proxy_url)
        proxy_dict = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        }
        if parsed.username:
            proxy_dict["username"] = parsed.username
        if parsed.password:
            proxy_dict["password"] = parsed.password
        return proxy_dict

    async def _fetch_browser_once(self, url: str) -> str | None:
        """Single browser fetch attempt with stealth context."""
        if not self.browser:
            return None

        # Parse proxy URL into Playwright format (server + username + password)
        proxy_arg = None
        if self.proxy_url:
            proxy_arg = self._parse_proxy_url(self.proxy_url)
            Actor.log.debug(f"[{self.source_name}] Using proxy server: {proxy_arg.get('server')}")

        context = await self.browser.new_context(
            proxy=proxy_arg,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-GB",
            timezone_id="Europe/London",
            java_script_enabled=True,
            bypass_csp=True,
        )

        try:
            page = await context.new_page()

            # Inject stealth JS before any page loads
            await page.add_init_script(STEALTH_JS)

            # Block tracking/analytics resources
            await page.route("**/*", self._block_resources)

            Actor.log.info(f"[{self.source_name}] Navigating to {url}")

            # Navigate — use networkidle so JS-rendered content (Next.js etc) finishes loading
            try:
                response = await page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception:
                # networkidle can be flaky, fall back to load
                Actor.log.info(f"[{self.source_name}] networkidle timed out, trying load event...")
                response = await page.goto(url, wait_until="load", timeout=30000)

            if not response:
                Actor.log.warning(f"[{self.source_name}] No response from page")
                return None

            Actor.log.info(f"[{self.source_name}] Page loaded, status={response.status}")

            # Handle Cloudflare challenge pages
            page_title = await page.title()
            if response.status == 403 or "challenge" in page_title.lower() or "just a moment" in page_title.lower():
                Actor.log.info(f"[{self.source_name}] Cloudflare/challenge detected (title='{page_title}'), waiting 8s...")
                await page.wait_for_timeout(8000)
                page_title = await page.title()
                if "challenge" in page_title.lower() or "just a moment" in page_title.lower():
                    Actor.log.warning(f"[{self.source_name}] Still blocked by Cloudflare after wait")
                    return None
                Actor.log.info(f"[{self.source_name}] Challenge passed, new title='{page_title}'")

            # Wait for JS content to fully render (Next.js apps need this)
            await page.wait_for_timeout(3000 + random.randint(0, 2000))

            # Try to wait for common job card selectors to appear
            for selector in ['[data-testid="job-card"]', 'article', '[class*="job"]', '[class*="search-result"]']:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                    Actor.log.info(f"[{self.source_name}] Found content selector: {selector}")
                    break
                except Exception:
                    continue

            # Run JS extraction on the live rendered page before grabbing HTML
            try:
                self._last_browser_extracted = await page.evaluate(JS_EXTRACT_JOBS) or []
                Actor.log.info(f"[{self.source_name}] JS extraction found {len(self._last_browser_extracted)} cards")
            except Exception as e:
                Actor.log.debug(f"[{self.source_name}] JS extraction failed: {e}")
                self._last_browser_extracted = []

            html = await page.content()
            Actor.log.info(f"[{self.source_name}] Got {len(html)} bytes of HTML")
            return html

        finally:
            await context.close()

    async def _block_resources(self, route):
        """Block tracking/analytics resources to reduce detection."""
        request = route.request
        if request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        url_lower = request.url.lower()
        for pattern in BLOCKED_URL_PATTERNS:
            if pattern in url_lower:
                await route.abort()
                return
        await route.continue_()

    # ── Proxy rotation ───────────────────────────────────────────────────

    async def _rotate_proxy(self):
        """Rotate to a new proxy URL if proxy config is available."""
        if self.proxy_config:
            try:
                # Sanitize source_name: session_id only allows [\w._~]
                safe_name = re.sub(r"[^a-zA-Z0-9._~]", "_", self.source_name)
                new_session = f"{safe_name}_{random.randint(10000, 99999)}"
                self.proxy_url = await self.proxy_config.new_url(session_id=new_session)
                self._proxy_failures = 0
                Actor.log.info(f"[{self.source_name}] Rotated to new proxy session")
            except Exception as e:
                Actor.log.warning(f"[{self.source_name}] Proxy rotation failed: {e}")

    @staticmethod
    def _is_proxy_error(error) -> bool:
        """Check if an error is likely proxy-related."""
        err_str = str(error).lower()
        return any(kw in err_str for kw in [
            "proxy", "tunnel", "connect", "timeout", "reset",
            "connection refused", "502", "503", "407",
        ])

    # ── JS-extracted job processing ────────────────────────────────────

    def _process_js_extracted(self) -> list[dict]:
        """Process raw JS-extracted card data into structured job dicts.

        Uses heuristics on visible text segments to identify company,
        location, salary, date_posted, employment_type, and snippet.
        """
        jobs = []
        for raw in self._last_browser_extracted:
            if not raw.get("title"):
                continue

            job = {"source": self.source_name}
            job["title"] = clean_text(raw["title"])
            job["url"] = raw.get("url", "")
            if raw.get("job_id"):
                job["job_id"] = raw["job_id"]

            # Fallback job_id from URL if JS didn't extract it
            if not job.get("job_id") and job["url"]:
                id_match = (re.search(r'/job/(\d+)', job["url"])
                           or re.search(r'-job(\d+)', job["url"])
                           or re.search(r'/(\d{5,})', job["url"]))
                if id_match:
                    job["job_id"] = id_match.group(1)

            # Company from dedicated link
            company_from_link = ""
            if raw.get("_company_link_text"):
                company_from_link = clean_text(raw["_company_link_text"])
                job["company"] = company_from_link

            card_text = raw.get("_card_text", "")
            segments = raw.get("_segments", [])

            # Salary from card text
            sal = self._extract_salary_from_text(card_text)
            if sal and (sal.get("min") or sal.get("max")):
                job["salary_raw"] = sal["raw"]
                job["salary_min"] = sal["min"]
                job["salary_max"] = sal["max"]
                job["salary_period"] = sal["period"]

            # Classify segments
            company_candidates = []
            location_candidates = []
            snippet_candidates = []

            # Location patterns — cities/regions (NOT company suffixes like "UK" alone)
            location_patterns = re.compile(
                r'\b(?:london|manchester|birmingham|leeds|bristol|'
                r'liverpool|sheffield|glasgow|edinburgh|cardiff|'
                r'newcastle|nottingham|southampton|oxford|cambridge|'
                r'reading|brighton|bath|york|leicester|coventry|'
                r'city of london|west london|east london|central london|'
                r'north london|south london|canary wharf|paddington|'
                r'remote|hybrid|on-?site|work from home|wfh|'
                r'england|scotland|wales|'
                r'[A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})\b',  # UK postcode
                re.IGNORECASE
            )

            # Date patterns — capture these for date_posted
            date_pattern = re.compile(
                r'\b(\d+\s*(?:day|hour|minute|week|month)s?\s*ago|'
                r'today|yesterday|just\s*(?:now|posted)|'
                r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|'
                r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:\s*,?\s*\d{2,4})?)'
                r'\b', re.IGNORECASE
            )

            # Employment type patterns
            emp_type_pattern = re.compile(
                r'\b(permanent|contract|temporary|part[\s-]?time|full[\s-]?time|'
                r'fixed[\s-]?term|freelance|internship|apprenticeship)\b',
                re.IGNORECASE
            )

            for seg in segments:
                seg_clean = seg.strip()
                if not seg_clean or len(seg_clean) < 2:
                    continue

                # Skip if it's the title or company name
                if seg_clean == job["title"]:
                    continue
                if company_from_link and seg_clean == company_from_link:
                    continue

                # Strip HTML tags from segment
                seg_clean = re.sub(r'<[^>]+>', '', seg_clean).strip()
                if not seg_clean:
                    continue

                # Capture date_posted from date-like segments
                if not job.get("date_posted") and len(seg_clean) < 40:
                    date_match = date_pattern.search(seg_clean)
                    if date_match:
                        job["date_posted"] = date_match.group(1).strip()
                        continue

                # Capture employment_type
                if not job.get("employment_type") and len(seg_clean) < 40:
                    emp_match = emp_type_pattern.search(seg_clean)
                    if emp_match:
                        job["employment_type"] = emp_match.group(1).strip().title()
                        # Don't continue — segment may also contain location info
                        if len(seg_clean) < 20:
                            continue

                # Skip salary segments (already extracted)
                if re.search(r'[£$€]\s*\d', seg_clean):
                    continue

                # Location detection — but NOT if it only matches because of
                # a company suffix like "UK" in "Capital One UK"
                if location_patterns.search(seg_clean) and len(seg_clean) < 80:
                    # Avoid classifying company names as locations
                    # If the segment is very similar to the company name, skip it
                    if company_from_link:
                        company_lower = company_from_link.lower()
                        seg_lower = seg_clean.lower()
                        if (seg_lower in company_lower or company_lower in seg_lower
                                or seg_lower.replace(" ", "") == company_lower.replace(" ", "")):
                            continue
                    location_candidates.append(seg_clean)
                    continue

                # Short segments (< 50 chars) are likely company or metadata
                if len(seg_clean) < 50:
                    company_candidates.append(seg_clean)
                else:
                    snippet_candidates.append(seg_clean)

            # Also try to extract employment_type from full card text
            if not job.get("employment_type") and card_text:
                emp_match = emp_type_pattern.search(card_text)
                if emp_match:
                    job["employment_type"] = emp_match.group(1).strip().title()

            # Also try to extract date_posted from full card text
            if not job.get("date_posted") and card_text:
                date_match = date_pattern.search(card_text)
                if date_match:
                    job["date_posted"] = date_match.group(1).strip()

            # Assign best candidates
            if not job.get("company") and company_candidates:
                for c in company_candidates:
                    text = re.sub(r'^(?:Company|Posted by|Employer)\s*:?\s*', '', c, flags=re.IGNORECASE).strip()
                    if text and len(text) > 1 and len(text) < 80:
                        job["company"] = clean_text(text)
                        break

            if location_candidates:
                text = location_candidates[0]
                text = re.sub(r'^Location\s*:?\s*', '', text, flags=re.IGNORECASE).strip()
                job["location"] = clean_text(text)

            if snippet_candidates:
                # Strip any remaining HTML from snippets
                snippet = re.sub(r'<[^>]+>', '', snippet_candidates[0])
                job["snippet"] = clean_text(snippet)[:500]
            elif card_text and not job.get("snippet"):
                snippet = card_text.replace(job["title"], "").strip()
                snippet = re.sub(r'<[^>]+>', '', snippet)
                if len(snippet) > 30:
                    job["snippet"] = clean_text(snippet)[:500]

            if job.get("title"):
                jobs.append(job)

        return jobs

    # ── Text-based extraction fallbacks ─────────────────────────────────

    @staticmethod
    def _extract_salary_from_text(text: str) -> dict | None:
        """Extract salary from free text containing £/$/€ amounts."""
        match = re.search(
            r'[£$€]\s*[\d,]+(?:\.?\d+)?(?:k)?'
            r'(?:\s*[-–to]+\s*[£$€]?\s*[\d,]+(?:\.?\d+)?(?:k)?)?'
            r'(?:\s*(?:per\s+)?(?:annum|year|day|hour|week|month|p\.?a\.?|p/h|p/d))?',
            text, re.IGNORECASE
        )
        if match:
            return parse_salary(match.group())
        return None

    # ── Utilities ────────────────────────────────────────────────────────

    async def _polite_delay(self):
        """Wait between requests with random jitter."""
        jitter = self.delay + random.uniform(0.2, 0.8)
        await asyncio.sleep(jitter)

    def _extract_jsonld_jobs(self, html: str) -> list[dict]:
        """Extract job postings from JSON-LD structured data in HTML."""
        soup = BeautifulSoup(html, "html.parser")
        jobs = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
                # Handle both single objects and arrays
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "JobPosting":
                        jobs.append(self._jsonld_to_job(item))
                    # Some sites wrap in ItemList
                    if item.get("@type") == "ItemList":
                        for elem in item.get("itemListElement", []):
                            posting = elem.get("item", elem)
                            if posting.get("@type") == "JobPosting":
                                jobs.append(self._jsonld_to_job(posting))
            except (json.JSONDecodeError, TypeError):
                continue
        return jobs

    def _jsonld_to_job(self, posting: dict) -> dict:
        """Convert a JSON-LD JobPosting to our unified format."""
        job = {"source": self.source_name}
        job["title"] = posting.get("title", "")
        job["url"] = posting.get("url", "")
        job["date_posted"] = posting.get("datePosted", "")
        job["valid_through"] = posting.get("validThrough", "")
        job["snippet"] = clean_text(posting.get("description", ""))[:500]

        # Company
        org = posting.get("hiringOrganization", {})
        if isinstance(org, dict):
            job["company"] = org.get("name", "")
        elif isinstance(org, str):
            job["company"] = org

        # Location
        loc = posting.get("jobLocation", {})
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                parts = [addr.get("addressLocality", ""), addr.get("addressRegion", "")]
                job["location"] = ", ".join(p for p in parts if p)
            elif isinstance(addr, str):
                job["location"] = addr
        elif isinstance(loc, list) and loc:
            first = loc[0]
            addr = first.get("address", {}) if isinstance(first, dict) else {}
            if isinstance(addr, dict):
                job["location"] = addr.get("addressLocality", "")

        # Salary
        salary = posting.get("baseSalary", {})
        if isinstance(salary, dict):
            currency = salary.get("currency", "GBP")
            job["salary_currency"] = currency
            currency_symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, currency + " ")
            value = salary.get("value", {})
            if isinstance(value, dict):
                job["salary_min"] = value.get("minValue")
                job["salary_max"] = value.get("maxValue")
                unit = value.get("unitText", "YEAR")
                job["salary_period"] = {"YEAR": "annum", "MONTH": "month", "DAY": "day", "HOUR": "hour"}.get(unit, "annum")
                period_label = {"annum": "per annum", "month": "per month", "day": "per day", "hour": "per hour"}.get(job["salary_period"], "per annum")
                if job["salary_min"] and job["salary_max"]:
                    if job["salary_min"] == job["salary_max"]:
                        job["salary_raw"] = f"{currency_symbol}{job['salary_min']:,.0f} {period_label}"
                    else:
                        job["salary_raw"] = f"{currency_symbol}{job['salary_min']:,.0f} - {currency_symbol}{job['salary_max']:,.0f} {period_label}"
                elif job["salary_min"]:
                    job["salary_raw"] = f"{currency_symbol}{job['salary_min']:,.0f}+ {period_label}"
                elif job["salary_max"]:
                    job["salary_raw"] = f"Up to {currency_symbol}{job['salary_max']:,.0f} {period_label}"

        # Employment type
        emp_type = posting.get("employmentType", "")
        if isinstance(emp_type, list):
            emp_type = ", ".join(emp_type)
        job["employment_type"] = emp_type

        # Job ID from identifier or URL
        identifier = posting.get("identifier", {})
        if isinstance(identifier, dict) and identifier.get("value"):
            job["job_id"] = str(identifier["value"])
        elif isinstance(identifier, str) and identifier:
            job["job_id"] = identifier
        elif job.get("url"):
            # Try to extract ID from URL as fallback
            id_match = re.search(r"/(\d{4,})", job["url"])
            if id_match:
                job["job_id"] = id_match.group(1)

        return job

    def _extract_next_data(self, html: str) -> dict | None:
        """Extract __NEXT_DATA__ JSON from Next.js pages."""
        soup = BeautifulSoup(html, "html.parser")
        script = soup.select_one('script#__NEXT_DATA__')
        if script and script.string:
            try:
                return json.loads(script.string)
            except json.JSONDecodeError:
                pass
        return None
