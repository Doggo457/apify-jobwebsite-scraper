"""
Shared utilities for UK Jobs Board scrapers.
Common salary parsing, data normalisation, and base scraper class.
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
    cleaned = salary_text.replace(",", "").replace("£", "").replace("$", "")
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


# Blocked resource domains (tracking, analytics, ads) - reduces detection surface
BLOCKED_DOMAINS = {
    "google-analytics.com", "googletagmanager.com", "googlesyndication.com",
    "doubleclick.net", "facebook.net", "facebook.com", "hotjar.com",
    "newrelic.com", "nr-data.net", "sentry.io", "fullstory.com",
    "optimizely.com", "amplitude.com", "mixpanel.com", "segment.io",
    "segment.com", "quantserve.com", "scorecardresearch.com",
    "adsrvr.org", "adnxs.com", "criteo.com", "taboola.com", "outbrain.com",
}

# Resource types to block (saves bandwidth, reduces fingerprinting)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


class BaseScraper(ABC):
    """Base class for all job board scrapers."""

    def __init__(self, client: httpx.AsyncClient, delay: float = 1.5, browser=None, proxy_url: str | None = None, proxy_config=None):
        self.client = client
        self.delay = delay
        self.browser = browser  # Playwright browser instance
        self.proxy_url = proxy_url  # Apify proxy URL for browser contexts
        self.proxy_config = proxy_config  # Apify proxy config for rotation
        self._proxy_failures = 0  # Track consecutive failures for rotation

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

    async def _fetch(self, url: str) -> str | None:
        """Fetch a URL with error handling."""
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

    # Comprehensive stealth JS - covers all major detection vectors
    STEALTH_JS = """
        // Hide webdriver flag
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        delete navigator.__proto__.webdriver;

        // Fake plugins (Chrome always has these)
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const plugins = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                ];
                plugins.length = 3;
                return plugins;
            },
        });

        // Languages
        Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'language', { get: () => 'en-GB' });

        // Platform
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

        // Hardware concurrency (real browsers report CPU cores)
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

        // Device memory
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

        // Max touch points (0 for desktop)
        Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

        // Chrome runtime object
        window.chrome = {
            runtime: { connect: function(){}, sendMessage: function(){} },
            loadTimes: function(){ return {}; },
            csi: function(){ return {}; },
            app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
        };

        // Permissions API
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);

        // Prevent detection via toString
        const originalToString = Function.prototype.toString;
        Function.prototype.toString = function() {
            if (this === window.navigator.permissions.query) {
                return 'function query() { [native code] }';
            }
            return originalToString.call(this);
        };

        // Connection info (real browsers have this)
        Object.defineProperty(navigator, 'connection', {
            get: () => ({
                effectiveType: '4g',
                rtt: 50,
                downlink: 10,
                saveData: false,
            }),
        });

        // WebGL vendor/renderer (avoid "Google SwiftShader" which screams headless)
        const getParameterProxyHandler = {
            apply: function(target, ctx, args) {
                const param = args[0];
                const result = Reflect.apply(target, ctx, args);
                // UNMASKED_VENDOR_WEBGL
                if (param === 37445) return 'Google Inc. (NVIDIA)';
                // UNMASKED_RENDERER_WEBGL
                if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return result;
            }
        };
        try {
            const canvas = document.createElement('canvas');
            const gl = canvas.getContext('webgl') || canvas.getContext('webgl2');
            if (gl) {
                const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
                if (debugInfo) {
                    const origGetParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = new Proxy(origGetParameter, getParameterProxyHandler);
                    if (typeof WebGL2RenderingContext !== 'undefined') {
                        WebGL2RenderingContext.prototype.getParameter = new Proxy(origGetParameter, getParameterProxyHandler);
                    }
                }
            }
        } catch(e) {}
    """

    async def _rotate_proxy(self):
        """Get a new proxy URL from the proxy config."""
        if not self.proxy_config:
            return
        self._proxy_failures += 1
        session_id = f"uk_jobs_{self.source_name}_{random.randint(10000, 99999)}"
        self.proxy_url = await self.proxy_config.new_url(session_id=session_id)
        Actor.log.info(f"[{self.source_name}] Rotated to new proxy session: {session_id}")

    def _is_proxy_error(self, error: Exception) -> bool:
        """Check if an error is proxy-related and worth retrying with a new proxy."""
        err_str = str(error).lower()
        return any(s in err_str for s in [
            "err_tunnel_connection_failed",
            "err_proxy_connection_failed",
            "err_connection_reset",
            "err_connection_refused",
            "err_connection_closed",
            "err_timed_out",
            "err_empty_response",
            "ns_error_proxy",
        ])

    async def _fetch_browser(self, url: str, wait_selector: str = "body", wait_ms: int = 8000) -> str | None:
        """Fetch a page using Playwright browser with stealth, proxy rotation, and Cloudflare handling."""
        if not self.browser:
            Actor.log.warning(f"[{self.source_name}] No browser available, falling back to HTTP")
            return await self._fetch(url)

        max_attempts = 3 if self.proxy_config else 1
        last_error = None

        for attempt in range(max_attempts):
            if attempt > 0:
                await self._rotate_proxy()
                await asyncio.sleep(random.uniform(1.0, 3.0))

            try:
                html = await self._fetch_browser_once(url, wait_selector, wait_ms)
                if html:
                    self._proxy_failures = 0  # Reset on success
                    return html
            except Exception as e:
                last_error = e
                if self._is_proxy_error(e) and self.proxy_config and attempt < max_attempts - 1:
                    Actor.log.warning(f"[{self.source_name}] Proxy error (attempt {attempt + 1}/{max_attempts}): {e}")
                    continue
                else:
                    Actor.log.warning(f"[{self.source_name}] Browser fetch failed for {url}: {e}")
                    return None

        Actor.log.warning(f"[{self.source_name}] All {max_attempts} proxy attempts failed for {url}")
        return None

    async def _fetch_browser_once(self, url: str, wait_selector: str = "body", wait_ms: int = 8000) -> str | None:
        """Single attempt to fetch a page using Playwright browser."""
        # Create a new context with proxy and stealth settings
        context_opts = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080},
            "screen": {"width": 1920, "height": 1080},
            "locale": "en-GB",
            "timezone_id": "Europe/London",
            "color_scheme": "light",
            "extra_http_headers": {
                "Accept-Language": "en-GB,en;q=0.9",
                "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        }
        if self.proxy_url:
            parsed = urlparse(self.proxy_url)
            proxy_opts = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_opts["username"] = parsed.username
            if parsed.password:
                proxy_opts["password"] = parsed.password
            context_opts["proxy"] = proxy_opts
            context_opts["ignore_https_errors"] = True

        context = await self.browser.new_context(**context_opts)
        try:
            # Inject stealth scripts before any page loads
            await context.add_init_script(self.STEALTH_JS)

            page = await context.new_page()

            # Block tracking/analytics resources to reduce detection surface
            async def block_resources(route):
                req = route.request
                # Block by resource type
                if req.resource_type in BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                    return
                # Block known tracking domains
                try:
                    req_host = urlparse(req.url).hostname or ""
                    if any(d in req_host for d in BLOCKED_DOMAINS):
                        await route.abort()
                        return
                except Exception:
                    pass
                await route.continue_()

            await page.route("**/*", block_resources)

            # Navigate - try 'load' first, tighter timeouts to save money
            try:
                await page.goto(url, wait_until="load", timeout=15000)
            except Exception as nav_err:
                # If it's a proxy error, re-raise immediately for rotation
                if self._is_proxy_error(nav_err):
                    raise
                # If load fails, try commit (bare minimum - at least we got HTML)
                Actor.log.info(f"[{self.source_name}] 'load' timed out, trying 'commit'...")
                try:
                    await page.goto(url, wait_until="commit", timeout=10000)
                except Exception:
                    raise nav_err

            # Wait for actual content to appear
            try:
                await page.wait_for_selector(wait_selector, timeout=wait_ms)
            except Exception:
                # Check if we're stuck on a Cloudflare/challenge page
                title = await page.title()
                if "just a moment" in title.lower() or "attention" in title.lower() or "challenge" in title.lower():
                    Actor.log.info(f"[{self.source_name}] Challenge page detected, waiting...")
                    await page.wait_for_timeout(8000)
                    try:
                        await page.wait_for_selector(wait_selector, timeout=10000)
                    except Exception:
                        Actor.log.info(f"[{self.source_name}] Still on challenge page after wait")

            # Small random delay to simulate human reading
            await page.wait_for_timeout(random.randint(800, 2000))
            html = await page.content()

            # Debug: log page info when content seems empty
            if len(html) < 5000:
                title = await page.title()
                Actor.log.debug(f"[{self.source_name}] Short page ({len(html)} chars), title: '{title}'")

            return html
        finally:
            await context.close()

    async def _polite_delay(self):
        """Wait between requests to be respectful, with jitter."""
        jitter = random.uniform(0.3, 1.0)
        await asyncio.sleep(self.delay * jitter)

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
