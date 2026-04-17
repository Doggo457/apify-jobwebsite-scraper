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
            value = salary.get("value", {})
            if isinstance(value, dict):
                job["salary_min"] = value.get("minValue")
                job["salary_max"] = value.get("maxValue")
                unit = value.get("unitText", "YEAR")
                job["salary_period"] = {"YEAR": "annum", "MONTH": "month", "DAY": "day", "HOUR": "hour"}.get(unit, "annum")

        # Employment type
        emp_type = posting.get("employmentType", "")
        if isinstance(emp_type, list):
            emp_type = ", ".join(emp_type)
        job["employment_type"] = emp_type

        # Job ID from identifier
        identifier = posting.get("identifier", {})
        if isinstance(identifier, dict):
            job["job_id"] = str(identifier.get("value", ""))

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
