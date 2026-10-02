"""
Shared utilities for the Jobs Board Scraper boards.

Cost on Apify = memory x wall-clock + residential-proxy GB, so the fetch layer
is built around four rules:

  * HTTP first. Every board tries a plain httpx request before any browser.
    A search page over HTTP is ~50-100 KB on the wire and takes ~1 s; the same
    page in Chromium is 1-3 MB and 10-60 s. Reed, Totaljobs, CWJobs and GOV.UK
    all serve their results server-side, so a typical UK run never needs it.
  * Lazy browser. Chromium is launched only when a board is actually blocked
    over HTTP (bot wall / soft-404 / thin page), and only for that board.
  * One browser context per board, warmed once, reused for every page: the
    cookie jar and proxy session persist across pagination, which scores
    better with Cloudflare Bot Management than a cold context per page and
    avoids paying 300-500 ms of context setup each time.
  * No fixed sleeps. We wait for the job-card selector, not "networkidle".

Board-specific anti-bot hooks (page_is_blocked / on_blocked_page) and the
per-board browser knobs are kept from v0.11 so Reed's soft-404 salvage and the
GOV.UK WAF handling still work on the rare browser path.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import httpx
from apify import Actor
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────────────────────────────
# Browser constants
# ──────────────────────────────────────────────────────────────────────

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--disable-translate",
    "--no-first-run",
    "--mute-audio",
    "--window-size=1366,900",
]

# Injected before any page script runs (boards behind Cloudflare Bot
# Management opt out via use_stealth_js = False: tampered natives score worse).
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
Object.defineProperty(navigator, 'plugins', { get: () => [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
    { name: 'Native Client', filename: 'internal-nacl-plugin' },
]});
Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en-US', 'en'] });
const _gp = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return _gp.call(this, p);
};
window.chrome = { runtime: {}, loadTimes: function () {}, csi: function () {} };
const _q = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
    p.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : _q(p)
);
"""

# Generic in-page extractor: reads visible job cards from the rendered DOM.
# Last-resort fallback when a board's own parser finds nothing.
JS_EXTRACT_JOBS = """
(() => {
    const jobs = [];
    const selectors = [
        '[data-testid*="job"]', 'article', '[data-at="job-item"]',
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
        const allLinks = document.querySelectorAll('a[href*="/job/"]');
        const parents = new Set();
        for (const a of allLinks) {
            if (a.parentElement && a.parentElement.parentElement) parents.add(a.parentElement.parentElement);
        }
        if (parents.size > 2) cards = parents;
    }
    for (const card of cards) {
        const allLinks = card.querySelectorAll('a');
        let titleLink = null;
        for (const a of allLinks) {
            const text = (a.innerText || a.textContent || '').trim();
            if (text.length > 3 && text.length < 200 && a.href &&
                (a.href.includes('/job/') || a.href.includes('/jobs/') || a.href.includes('viewjob'))) { titleLink = a; break; }
        }
        if (!titleLink) {
            for (const a of allLinks) {
                const text = (a.innerText || a.textContent || '').trim();
                if (text.length > 5 && text.length < 200 && a.href) { titleLink = a; break; }
            }
        }
        if (!titleLink) continue;
        const title = (titleLink.innerText || titleLink.textContent || '').trim();
        if (!title || title.length < 3) continue;
        const job = { title: title, url: titleLink.href || '' };
        const idMatch = job.url.match(/\\/job\\/(\\d+)/) || job.url.match(/-job(\\d+)/) || job.url.match(/\\/(\\d{5,})/);
        if (idMatch) job.job_id = idMatch[1];
        job._card_text = (card.innerText || card.textContent || '').substring(0, 2000);
        const segments = [];
        const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT, null, false);
        let node;
        while (node = walker.nextNode()) {
            const text = node.textContent.trim();
            if (text.length > 1 && text.length < 300 && text !== title) segments.push(text);
        }
        job._segments = segments.slice(0, 30);
        for (const a of allLinks) {
            if (a === titleLink) continue;
            const href = a.href || '';
            const text = (a.innerText || a.textContent || '').trim();
            if (text.length > 1 && text.length < 100 &&
                (href.includes('/company/') || href.includes('/employer/') ||
                 href.includes('/recruiter/') || href.includes('/list-jobs/'))) { job._company_link_text = text; break; }
        }
        jobs.push(job);
    }
    return jobs;
})()
"""

# ── Aggressive resource blocking (residential proxy is billed PER GB) ───
# Images/media/fonts/CSS are ~90% of page bytes but carry no job data.
# Never add: document, script (JS-rendered boards), xhr/fetch (job JSON).
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})

# Ad / analytics / tracking / consent / session-replay / chat-widget hosts,
# matched as substrings of the full request URL for every resource type.
# Never add first-party job-board hosts or Cloudflare challenge paths.
BLOCKED_URL_PATTERNS = (
    "google-analytics", "googletagmanager", "googlesyndication", "googleadservices",
    "doubleclick", "adservice.google", "googleoptimize", "google.com/pagead", "imasdk.googleapis",
    "facebook.net", "connect.facebook", "facebook.com/tr",
    "bat.bing", "clarity.ms", "snap.licdn", "px.ads.linkedin", "analytics.tiktok", "ads-twitter",
    "ct.pinterest", "events.redditmedia", "sc-static.net", "tr.snapchat", "mc.yandex",
    "hotjar", "mixpanel", "segment.io", "segment.com", "amplitude", "heapanalytics", "fullstory",
    "mouseflow", "crazyegg", "kissmetrics", "quantummetric", "inspectlet", "smartlook",
    "luckyorange", "chartbeat", "matomo", "plausible.io", "statcounter",
    "newrelic", "nr-data.net", "sentry.io", "bugsnag", "datadoghq", "rollbar", "speedcurve", "dynatrace",
    "optimizely", "vwo.com", "visualwebsiteoptimizer", "abtasty", "kameleoon",
    "adsystem", "adservice", "adnxs", "criteo", "taboola", "outbrain", "scorecardresearch",
    "quantserve", "quantcount", "pubmatic", "rubiconproject", "openx.net", "casalemedia",
    "smartadserver", "teads.tv", "yieldlab", "moatads", "adsafeprotected", "doubleverify",
    "adsrvr.org", "mathtag", "bidswitch", "sharethrough", "gumgum", "33across", "indexww",
    "sonobi", "adform.net",
    "id5-sync", "permutive", "liveramp", "krxd.net", "bluekai", "demdex", "everesttech",
    "agkn.com", "rlcdn.com", "tapad.com", "crwdcntrl", "exelator", "eyeota",
    "cdn.cookielaw", "onetrust", "cookiebot", "usercentrics", "trustarc", "consensu.org",
    "sourcepoint", "quantcast",
    "tiqcdn.com", "ensighten", "adobedtm", "omtrdc.net", "branch.io", "appsflyer", "adjust.com",
    "intercom.io", "intercomcdn", "livechatinc", "tawk.to", "drift.com", "zdassets", "zopim",
    "onesignal", "pushwoosh", "liveperson", "salesforceliveagent",
    "youtube.com/embed", "youtube-nocookie", "player.vimeo", "brightcove", "jwplayer", "jwpcdn",
    "fonts.googleapis", "fonts.gstatic", "use.typekit", "fast.fonts.net",
)

CHALLENGE_TITLE_WORDS = (
    "just a moment", "challenge", "attention required", "access denied",
    "security check", "pardon our interruption", "blocked", "are you a human",
    "request unsuccessful", "verify you are", "bot detection", "something went wrong",
)


def is_challenge_page(title: str, status: int | None, body_len: int, keyword: str = "") -> bool:
    """Heuristic: is this a bot-wall rather than a results page?

    Title words are only trusted on small bodies, and the search keyword is
    removed first, so a genuine results page like "Blocked Drain Engineer Jobs
    in London" (800 KB) is never mistaken for Cloudflare.
    """
    if status in (401, 403, 406, 429, 503):
        return True
    if body_len < 3000:
        return True
    t = (title or "").lower()
    if keyword:
        t = t.replace(keyword.lower(), "")
    return body_len < 200_000 and any(w in t for w in CHALLENGE_TITLE_WORDS)


def parse_proxy_url(proxy_url: str) -> dict:
    p = urlparse(proxy_url)
    d = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        d["username"] = p.username
    if p.password:
        d["password"] = p.password
    return d


# ──────────────────────────────────────────────────────────────────────
# Browser pool (lazy Chromium, one warmed context per board)
# ──────────────────────────────────────────────────────────────────────

class BrowserPool:
    """Lazily launched Chromium with one persistent context per board.

    Nothing starts until the first `context()` call, so runs whose boards all
    succeed over HTTP never pay for a browser at all.
    """

    def __init__(self, proxy_config=None, max_concurrent_pages: int = 3):
        self.proxy_config = proxy_config
        self._pw = None
        self._browser = None
        self._contexts: dict[str, object] = {}
        self._launch_lock = asyncio.Lock()
        self._page_sem = asyncio.Semaphore(max_concurrent_pages)
        self.launched = False

    async def _ensure_browser(self):
        if self._browser:
            return self._browser
        async with self._launch_lock:
            if self._browser:
                return self._browser
            from playwright.async_api import async_playwright

            Actor.log.info("[browser] Launching Chromium (first board that needs it)")
            self._pw = await async_playwright().start()
            launch_kwargs = {"headless": True, "args": STEALTH_ARGS}
            if self.proxy_config:
                # Chromium needs a browser-level proxy before per-context
                # proxies are honoured; the real per-board proxy is set on
                # the context.
                seed = await self.proxy_config.new_url(session_id=f"seed_{random.randint(1000, 9999)}")
                launch_kwargs["proxy"] = parse_proxy_url(seed)
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
            self.launched = True
            return self._browser

    @property
    def version(self) -> str:
        try:
            return self._browser.version if self._browser else ""
        except Exception:
            return ""

    async def context(self, scraper: "BaseScraper"):
        key = scraper.source_name
        ctx = self._contexts.get(key)
        if ctx:
            return ctx
        browser = await self._ensure_browser()
        ua = CHROME_UA
        if scraper.ua_from_browser_version:
            try:
                major = browser.version.split(".")[0]
                ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
            except Exception:
                pass
        ctx = await browser.new_context(
            proxy=parse_proxy_url(scraper.proxy_url) if scraper.proxy_url else None,
            viewport={"width": 1366, "height": 900},
            user_agent=ua,
            locale="en-GB",
            timezone_id="Europe/London",
            java_script_enabled=True,
            bypass_csp=True,
        )
        if scraper.use_stealth_js:
            await ctx.add_init_script(STEALTH_JS)
        blocked_types = frozenset(scraper.blocked_resource_types)

        async def _route(route):
            req = route.request
            if req.resource_type in blocked_types:
                await route.abort()
                return
            url = req.url.lower()
            for pat in BLOCKED_URL_PATTERNS:
                if pat in url:
                    await route.abort()
                    return
            await route.continue_()

        await ctx.route("**/*", _route)
        self._contexts[key] = ctx

        # Warm-up: land on a page the site serves freely (homepage) so anti-bot
        # cookies are established, then navigate like a real user would.
        if scraper.warmup_url:
            page = await ctx.new_page()
            try:
                Actor.log.info(f"[{key}] warm-up visit: {scraper.warmup_url}")
                await page.goto(scraper.warmup_url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(1500 + random.randint(0, 1000))
                await page.mouse.move(400 + random.randint(0, 400), 300 + random.randint(0, 200))
                await page.mouse.wheel(0, 500 + random.randint(0, 400))
                await page.wait_for_timeout(500 + random.randint(0, 500))
            except Exception as e:
                Actor.log.debug(f"[{key}] warm-up failed (continuing): {e}")
            finally:
                await page.close()
        return ctx

    async def reset_context(self, key: str):
        ctx = self._contexts.pop(key, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass

    @property
    def page_slot(self) -> asyncio.Semaphore:
        return self._page_sem

    async def close(self):
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None


# ──────────────────────────────────────────────────────────────────────
# Text / value helpers
# ──────────────────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", str(text)).strip()


def make_headers(accept_language: str = "en-GB,en;q=0.9") -> dict:
    """Realistic Chrome request headers for HTML boards.

    A Chrome UA without matching sec-ch-ua client hints gets failover pages
    from some WAFs (GOV.UK), so the full consistent set is sent. Accept-Encoding
    is left to httpx, which advertises only the codecs it can decode.
    """
    return {
        "User-Agent": CHROME_UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "Accept-Language": accept_language,
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }


def make_api_headers() -> dict:
    return {"User-Agent": "Mozilla/5.0 (compatible; jobs-board-scraper/1.0)", "Accept": "application/json"}


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


class LazySoup:
    """BeautifulSoup that is only built on first attribute access.

    Boards that read embedded JSON (StepStone, Indeed) never touch the DOM on
    the happy path, so they skip the parse entirely.
    """

    __slots__ = ("_html", "_soup")

    def __init__(self, html: str):
        self._html = html
        self._soup = None

    def __getattr__(self, name):
        if self._soup is None:
            self._soup = make_soup(self._html)
        return getattr(self._soup, name)


def _as_soup(html_or_soup) -> BeautifulSoup | LazySoup:
    return html_or_soup if hasattr(html_or_soup, "select") else LazySoup(html_or_soup or "")


@dataclass
class JobListing:
    """Unified job listing format (kept for import compatibility)."""
    title: str = ""
    company: str = ""
    location: str = ""
    salary_raw: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: str = ""
    salary_period: str = "annum"
    snippet: str = ""
    full_description: str = ""
    employment_type: str = ""
    date_posted: str = ""
    valid_through: str = ""
    url: str = ""
    job_id: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


CURRENCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$"}
DOLLAR_CURRENCIES = {"USD", "AUD", "CAD", "NZD", "SGD"}

_SAL_K = re.compile(r"(\d+(?:\.\d+)?)\s*k\b", re.IGNORECASE)
_SAL_NUM = re.compile(r"\d+(?:\.\d+)?")
_THOUSANDS_DOT = re.compile(r"(?<=\d)\.(?=\d{3}(?!\d))")      # 45.000 -> 45000 (never 12.50)
_THOUSANDS_SPACE = re.compile(r"(?<=\d)[   ](?=\d{3}(?!\d))")  # 45 000 -> 45000
_PERIOD_ANNUM = re.compile(r"per\s*annum|\bp\.?a\.?\b|per\s*year|\ba\s*year\b|annual|yearly|pro\s*jahr|par\s*an\b|per\s*jaar", re.IGNORECASE)
_PERIOD_DAY = re.compile(r"(?:\b(?:per|a)\b|/)\s*day\b|\bp/?d\b|\bdaily\b|pro\s*tag|par\s*jour|per\s*dag", re.IGNORECASE)
_PERIOD_HOUR = re.compile(r"(?:\b(?:per|an)\b|/)\s*hour\b|\bp/?h\b|\bhourly\b|\bph\b|pro\s*stunde|par\s*heure|de\s*l'heure|per\s*uur", re.IGNORECASE)
_PERIOD_WEEK = re.compile(r"(?:\b(?:per|a)\b|/)\s*week\b|\bweekly\b|\bp/?w\b|pro\s*woche|par\s*semaine|per\s*week", re.IGNORECASE)
_PERIOD_MONTH = re.compile(r"(?:\b(?:per|a)\b|/)\s*month\b|\bmonthly\b|\bp/?m\b|pro\s*monat|par\s*mois|per\s*maand", re.IGNORECASE)


def parse_salary(salary_text: str | None, default_currency: str = "GBP") -> dict:
    """Parse free-text salary into {raw, min, max, currency, period}."""
    raw = clean_text(salary_text)
    result = {"raw": raw, "min": None, "max": None, "currency": default_currency, "period": "annum"}
    if not raw:
        return result
    lower = raw.lower()
    if any(w in lower for w in ("competitive", "negotiable", "unspecified", "not specified", "undisclosed")):
        return result

    if "a$" in lower or "au$" in lower or "aud" in lower:
        result["currency"] = "AUD"
    elif "$" in raw or "usd" in lower:
        # A bare "$" means the board's own dollar (AUD on Indeed AU, USD elsewhere)
        result["currency"] = default_currency if default_currency in DOLLAR_CURRENCIES else "USD"
    elif "€" in raw or "eur" in lower:
        result["currency"] = "EUR"
    elif "£" in raw or "gbp" in lower:
        result["currency"] = "GBP"

    # Explicit annual wording wins: "£24,000 per annum, 30 hours per week"
    if _PERIOD_ANNUM.search(lower):
        result["period"] = "annum"
    elif _PERIOD_DAY.search(lower):
        result["period"] = "day"
    elif _PERIOD_HOUR.search(lower):
        result["period"] = "hour"
    elif _PERIOD_WEEK.search(lower):
        result["period"] = "week"
    elif _PERIOD_MONTH.search(lower):
        result["period"] = "month"

    cleaned = raw.replace(",", "")
    cleaned = _THOUSANDS_DOT.sub("", cleaned)
    cleaned = _THOUSANDS_SPACE.sub("", cleaned)
    cleaned = _SAL_K.sub(lambda m: str(int(float(m.group(1)) * 1000)), cleaned)
    numbers = [float(n) for n in _SAL_NUM.findall(cleaned)]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return result

    lo = numbers[0]
    hi = numbers[1] if len(numbers) > 1 else lo
    # "£60,000 + 25 days holiday" -> second number is not a salary bound
    if hi < lo / 5:
        hi = lo
    if hi < lo:
        lo, hi = hi, lo
    result["min"], result["max"] = lo, hi
    return result


def annualise(amount: float | None, period: str) -> float | None:
    if amount is None:
        return None
    return {"day": 220, "hour": 1760, "week": 52, "month": 12}.get(period, 1) * amount


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_REL = re.compile(r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month)s?\s*ago", re.IGNORECASE)
_REL_EU = re.compile(r"(?:vor|il y a|geleden)?\s*(\d+)\s*\+?\s*(tag|tage|tagen|jour|jours|dag|dagen|woche|wochen|semaine|semaines|week|weken)\b", re.IGNORECASE)
_DMY = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
_DM = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?(?:\s+(\d{4}))?", re.IGNORECASE)
_MD = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", re.IGNORECASE)


def normalize_date(text, today: date | None = None, future_ok: bool = False) -> str:
    """Return an ISO date (YYYY-MM-DD) for the many formats boards use, or ''.

    Handles ISO timestamps, "today", "yesterday", "3 days ago", "2 weeks ago",
    "18 September", "Sep 18, 2026", "18/09/2026" (day-first), epoch seconds or
    milliseconds, and German/French/Dutch relative phrases. A day-month string
    with no year is assumed past (a posting date) unless `future_ok` is set
    (a closing date), in which case it is the next occurrence.
    """
    if text is None:
        return ""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        try:
            ts = float(text)
            if ts > 1e11:  # milliseconds
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    s = clean_text(str(text))
    if not s:
        return ""
    today = today or date.today()
    low = s.lower()

    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""

    if (re.search(r"\b(today|just now|just posted|new|heute|aujourd'hui|vandaag)\b", low)
            or re.search(r"\d+\s*\+?\s*(minute|min|hour|hr|stunde|stunden|heure|heures|uur)s?\b", low)):
        return today.isoformat()
    if re.search(r"\b(yesterday|gestern|hier|gisteren)\b", low):
        return (today - timedelta(days=1)).isoformat()

    m = _REL.search(low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n if unit == "day" else n * 7 if unit == "week" else n * 30
        return (today - timedelta(days=days)).isoformat()
    m = _REL_EU.search(low)
    if m:
        n = int(m.group(1))
        days = n * 7 if m.group(2).lower().startswith(("woch", "semain", "week", "weken")) else n
        return (today - timedelta(days=days)).isoformat()

    m = _DMY.search(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if mo > 12 and d <= 12:
            d, mo = mo, d
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return ""

    for pat, d_idx, m_idx in ((_DM, 1, 2), (_MD, 2, 1)):
        m = pat.search(s)
        if m:
            mon = _MONTHS.get(m.group(m_idx)[:3].lower())
            if not mon:
                continue
            d = int(m.group(d_idx))
            y = int(m.group(3)) if m.group(3) else today.year
            try:
                dt = date(y, mon, d)
            except ValueError:
                continue
            if not m.group(3):
                try:
                    if not future_ok and dt > today + timedelta(days=1):
                        dt = date(y - 1, mon, d)
                    elif future_ok and dt < today - timedelta(days=1):
                        dt = date(y + 1, mon, d)
                except ValueError:  # 29 Feb shifted into a non-leap year: keep as is
                    pass
            return dt.isoformat()

    if re.fullmatch(r"\d{9,13}", s):
        return normalize_date(int(s), today, future_ok)
    return ""


_WORK_MODE = re.compile(
    r"\b(remote|work from home|wfh|fully remote|home[\s-]?based)\b|"
    r"\b(hybrid)\b|"
    r"\b(on[\s-]?site|office[\s-]?based|in[\s-]?office)\b",
    re.IGNORECASE,
)


def detect_work_mode(*texts: str | None) -> str:
    """Return 'Remote', 'Hybrid', 'On-site' or '' from any of the given texts.

    Texts are checked in order (most specific first); within a text, Hybrid
    beats Remote beats On-site, since "remote ... hybrid" almost always
    describes a hybrid role.
    """
    for t in texts:
        if not t:
            continue
        found = {("Hybrid" if m.group(2) else "Remote" if m.group(1) else "On-site") for m in _WORK_MODE.finditer(str(t))}
        if found:
            for mode in ("Hybrid", "Remote", "On-site"):
                if mode in found:
                    return mode
    return ""


_EMP_TYPE = re.compile(
    r"\b(permanent|contract|temporary|temp|fixed[\s-]?term|freelance|internship|"
    r"apprenticeship|graduate)\b(?:\s*,\s*(full[\s-]?time|part[\s-]?time))?|"
    r"\b(full[\s-]?time|part[\s-]?time)\b",
    re.IGNORECASE,
)


def detect_employment_type(text: str | None) -> str:
    if not text:
        return ""
    m = _EMP_TYPE.search(str(text))
    if not m:
        return ""
    if m.group(1):
        out = "Temporary" if m.group(1).lower() == "temp" else m.group(1).title()
        if m.group(2):
            out += ", " + m.group(2).lower()
        return out
    return m.group(3).title()


_SAL_IN_TEXT = re.compile(
    r"(?:A\$|AU\$|[£$€])\s*[\d,]+(?:\.\d+)?k?"
    r"(?:\s*(?:-|–|to)\s*(?:A\$|AU\$|[£$€])?\s*[\d,]+(?:\.\d+)?k?)?"
    r"(?:\s*(?:per|a|an|/)\s*(?:annum|year|day|hour|week|month)|\s*p\.?a\.?|\s*p/?[hdw])?",
    re.IGNORECASE,
)


def extract_salary_from_text(text: str | None, default_currency: str = "GBP") -> dict | None:
    if not text:
        return None
    m = _SAL_IN_TEXT.search(str(text))
    if not m:
        return None
    sal = parse_salary(m.group(), default_currency)
    return sal if sal["min"] else None


def apply_salary(job: dict, sal: dict | None) -> None:
    if not sal or not (sal.get("min") or sal.get("max")):
        return
    job["salary_raw"] = sal["raw"]
    job["salary_min"] = sal["min"]
    job["salary_max"] = sal["max"]
    job["salary_currency"] = sal["currency"]
    job["salary_period"] = sal["period"]


def strip_tracking(url: str) -> str:
    """Drop tracking query strings that make the same job look like two URLs."""
    if not url:
        return ""
    p = urlparse(url)
    if p.query and any(k in p.query for k in ("sid=", "source=", "utm_", "ref=", "cmp=", "src=", "WT.mc_id", "position=")):
        return p._replace(query="", fragment="").geturl()
    return p._replace(fragment="").geturl()


# ──────────────────────────────────────────────────────────────────────
# Base scraper
# ──────────────────────────────────────────────────────────────────────

PageCallback = Callable[[str, list[dict]], Awaitable[None]]


class BaseScraper(ABC):
    """Base class with HTTP-first fetching and lazy browser fallback.

    HTML boards implement `_build_url` and `_parse_search(html, soup)`; the
    resumable pagination loop lives here. API boards override `search`.
    """

    # ── Per-board knobs (override in subclasses) ──
    default_currency = "GBP"
    card_selector: str | None = None      # waited on in browser mode
    page_size = 25
    max_pages = 40                        # per-run cap; main.py lowers it to the budget
    hard_page_cap: int | None = None      # absolute cap a board sets for itself
    use_js_fallback = True
    resumable = True                      # False for boards that override search() without resume state
    use_stealth_js = True                 # inject STEALTH_JS into the browser context
    ua_from_browser_version = False       # derive the UA from the real engine version
    warmup_url: str | None = None         # visited once when the browser context is created
    blocked_resource_types = BLOCKED_RESOURCE_TYPES
    browser_max_attempts: int | None = None  # None -> 2 with a proxy to rotate, else 1

    def __init__(self, client: httpx.AsyncClient, delay: float = 0.8, *,
                 browser_pool: BrowserPool | None = None, proxy_url: str | None = None,
                 proxy_config=None, on_page: PageCallback | None = None, browser=None):
        self.client = client
        self.delay = delay
        self.browser_pool = browser_pool
        self.proxy_url = proxy_url
        self.proxy_config = proxy_config
        self.on_page = on_page
        self.fetch_mode = "auto"          # auto -> http | browser
        self.stats = {"pages": 0, "http_pages": 0, "browser_pages": 0, "jobs": 0, "mode": "http"}
        self._last_browser_extracted: list[dict] = []
        self._last_fetch_salvaged = False
        self._next_referer: str | None = None
        self.radius_miles = None
        self.country_hint = ""
        self.fetch_details = False
        # Resumable pagination state: a second `search()` call for the same
        # keyword continues where the first stopped (top-up round).
        self._page = 1
        self._seen: set[str] = set()
        self.exhausted = False
        self._keyword = ""
        self._location = ""

    @property
    def browser(self):
        """True-ish when a browser fallback is available (compat with v0.11 boards)."""
        return self.browser_pool

    # ── Board-specific soft-block hooks ──────────────────────────────

    def page_is_blocked(self, html: str) -> bool:
        """Board-specific soft-block detection (bot-detection pages served
        with HTTP 200 and full-size HTML). Default: never blocked."""
        return False

    async def on_blocked_page(self, page, html: str) -> str | None:
        """Salvage hook called with the LIVE Playwright page when
        page_is_blocked() fired. Return replacement content to use it, or
        None to fail the attempt (proxy rotates, retry)."""
        return None

    @property
    @abstractmethod
    def source_name(self) -> str: ...

    # ── Pagination loop shared by HTML boards ────────────────────────

    def _build_url(self, keyword: str, location: str, job_type: str,
                   salary_min: int | None, page: int) -> str:
        raise NotImplementedError

    def _parse_search(self, html: str, soup) -> tuple[list[dict], bool]:
        raise NotImplementedError

    def _reset_for(self, keyword: str, location: str) -> None:
        if keyword != self._keyword or location != self._location:
            self._keyword, self._location = keyword, location
            self._page = 1
            self.exhausted = False
            self._next_referer = None

    async def search(self, keyword: str, location: str, max_results: int = 50,
                     job_type: str = "all", salary_min: int | None = None) -> list[dict]:
        all_jobs: list[dict] = []
        self._reset_for(keyword or "", location or "")
        if self.exhausted:
            return all_jobs
        seen = self._seen
        page = self._page
        while len(all_jobs) < max_results and page <= self.max_pages:
            url = self._build_url(keyword, location, job_type, salary_min, page)
            html = await self._get_html(url)
            if not html:
                self.exhausted = True
                break
            soup = LazySoup(html)
            jobs, has_next = self._parse_search(html, soup)
            if not jobs and self.use_js_fallback and self._last_browser_extracted:
                jobs = self._process_js_extracted()
            if not jobs:
                Actor.log.info(f"[{self.source_name}] No jobs on page {page}, stopping")
                self.exhausted = True
                break
            fresh = []
            for job in jobs:
                key = job.get("job_id") or job.get("url") or job.get("title")
                if key in seen:
                    continue
                seen.add(key)
                job.setdefault("source", self.source_name)
                job.setdefault("salary_currency", self.default_currency)
                fresh.append(job)
            if not fresh:
                self.exhausted = True
                break
            page += 1
            self._page = page
            self._next_referer = url
            # Whole pages are emitted (never truncated) so nothing marked as
            # seen is lost on resume; main.py truncates to the global cap.
            all_jobs.extend(fresh)
            self.stats["pages"] += 1
            self.stats["jobs"] += len(fresh)
            Actor.log.info(f"[{self.source_name}] page {page - 1}: +{len(fresh)} (run total {self.stats['jobs']}) via {self.stats['mode']}")
            if self.on_page:
                await self.on_page(self.source_name, fresh)
            if not has_next:
                self.exhausted = True
                break
            if len(all_jobs) >= max_results:
                break
            await self._polite_delay()
        return all_jobs

    # ── Fetching ─────────────────────────────────────────────────────

    async def _get_html(self, url: str) -> str | None:
        """HTTP first; switch the whole board to browser mode on the first failure."""
        if self.fetch_mode != "browser":
            html = await self._fetch_html_http(url)
            if html:
                self.fetch_mode = "http"
                self.stats["mode"] = "http"
                self.stats["http_pages"] += 1
                return html
            if not self.browser_pool:
                return None
            Actor.log.info(f"[{self.source_name}] HTTP blocked/thin, switching to browser for this board")
            self.fetch_mode = "browser"
            self.stats["mode"] = "browser"
        html = await self._fetch_browser(url)
        if html:
            self.stats["browser_pages"] += 1
        return html

    async def _fetch_html_http(self, url: str) -> str | None:
        """Fetch a search page over HTTP and sanity-check it; None if blocked."""
        try:
            r = await self.client.get(url)
        except httpx.HTTPError as e:
            Actor.log.info(f"[{self.source_name}] HTTP fetch failed: {type(e).__name__}")
            return None
        text = r.text
        m = re.search(r"<title[^>]*>(.*?)</title>", text[:5000], re.IGNORECASE | re.DOTALL)
        title = clean_text(m.group(1)) if m else ""
        if is_challenge_page(title, r.status_code, len(text), self._keyword):
            Actor.log.info(f"[{self.source_name}] HTTP {r.status_code} '{title[:40]}' ({len(text)} B) looks blocked")
            return None
        if self.page_is_blocked(text):
            Actor.log.info(f"[{self.source_name}] HTTP response is the board's soft-block page")
            return None
        return text

    async def _fetch(self, url: str) -> str | None:
        try:
            r = await self.client.get(url)
            r.raise_for_status()
            return r.text
        except httpx.HTTPError as e:
            Actor.log.warning(f"[{self.source_name}] Failed to fetch {url}: {type(e).__name__}")
            return None

    async def _fetch_json(self, url: str, headers: dict | None = None):
        try:
            r = await self.client.get(url, headers=headers)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            Actor.log.warning(f"[{self.source_name}] Failed to fetch JSON {url[:120]}: {e}")
            return None

    async def _fetch_detail(self, url: str) -> str | None:
        """Detail page: httpx first (JSON-LD is in the raw HTML), browser only
        when the page is thin."""
        html = await self._fetch(url)
        if html and ("application/ld+json" in html or len(html) > 10000):
            return html
        if self.browser_pool:
            browser_html = await self._fetch_browser(url)
            if browser_html and len(browser_html) > len(html or ""):
                return browser_html
        return html

    async def _fetch_browser(self, url: str, max_attempts: int | None = None) -> str | None:
        if not self.browser_pool:
            return None
        if max_attempts is None:
            max_attempts = self.browser_max_attempts
        if max_attempts is None:
            # A retry only has a chance if we can rotate to a fresh IP.
            max_attempts = 2 if self.proxy_config else 1
        for attempt in range(1, max_attempts + 1):
            try:
                self._last_fetch_salvaged = False
                html = await self._fetch_browser_once(url)
                if html:
                    return html
            except Exception as e:
                Actor.log.warning(f"[{self.source_name}] browser attempt {attempt}: {type(e).__name__}: {str(e)[:120]}")
            if attempt < max_attempts:
                await self._rotate_proxy()
                await asyncio.sleep(1.5 + random.random() * 2)
        Actor.log.warning(f"[{self.source_name}] browser gave up on {url}")
        return None

    async def _fetch_browser_once(self, url: str) -> str | None:
        ctx = await self.browser_pool.context(self)
        async with self.browser_pool.page_slot:
            page = await ctx.new_page()
            try:
                goto_kwargs = {"wait_until": "domcontentloaded", "timeout": 30000}
                if self._next_referer:
                    goto_kwargs["referer"] = self._next_referer
                elif self.warmup_url:
                    goto_kwargs["referer"] = self.warmup_url
                try:
                    resp = await page.goto(url, **goto_kwargs)
                except Exception:
                    Actor.log.info(f"[{self.source_name}] domcontentloaded timed out, trying load event")
                    resp = await page.goto(url, wait_until="load", timeout=20000)
                status = resp.status if resp else None
                selector = self.card_selector or 'a[href*="/job"]'
                try:
                    await page.wait_for_selector(selector, timeout=8000)
                except Exception:
                    pass
                title = await page.title()
                if is_challenge_page(title, status, 10_000, self._keyword):
                    # A Cloudflare JS challenge that is going to pass does so in
                    # a few seconds; longer means this IP is burned.
                    Actor.log.info(f"[{self.source_name}] challenge page (status={status}, title='{title[:40]}'), waiting")
                    for _ in range(6):
                        await page.wait_for_timeout(2000)
                        t = (await page.title()).lower().replace(self._keyword.lower(), "")
                        if not any(w in t for w in CHALLENGE_TITLE_WORDS):
                            break
                    else:
                        return None
                    try:
                        await page.wait_for_selector(selector, timeout=5000)
                    except Exception:
                        pass
                html = await page.content()
                if self.page_is_blocked(html):
                    Actor.log.info(f"[{self.source_name}] soft-block page after load, trying salvage")
                    await page.wait_for_timeout(2500 + random.randint(0, 1000))
                    salvage = await self.on_blocked_page(page, html)
                    if salvage:
                        self._last_fetch_salvaged = True
                        return salvage
                    return None
                if len(html) < 3000:
                    return None
                self._last_browser_extracted = []
                if self.use_js_fallback:
                    try:
                        self._last_browser_extracted = await page.evaluate(JS_EXTRACT_JOBS) or []
                    except Exception:
                        pass
                return html
            finally:
                await page.close()

    async def _rotate_proxy(self):
        """New proxy session for this board; the warmed browser context is
        discarded with it so a burned session is never reused."""
        if self.browser_pool:
            await self.browser_pool.reset_context(self.source_name)
        if not self.proxy_config:
            return
        try:
            safe = re.sub(r"[^a-zA-Z0-9._~]", "_", self.source_name)
            self.proxy_url = await self.proxy_config.new_url(session_id=f"{safe}_{random.randint(10000, 99999)}")
            Actor.log.info(f"[{self.source_name}] rotated proxy session")
        except Exception as e:
            Actor.log.warning(f"[{self.source_name}] proxy rotation failed: {e}")

    async def _polite_delay(self):
        await asyncio.sleep(self.delay + random.uniform(0.1, 0.5))

    # ── Generic extractors ───────────────────────────────────────────

    def _extract_salary_from_text(self, text: str) -> dict | None:
        return extract_salary_from_text(text, self.default_currency)

    _LOC_PAT = re.compile(
        r"\b(?:london|manchester|birmingham|leeds|bristol|liverpool|sheffield|glasgow|edinburgh|cardiff|"
        r"newcastle|nottingham|southampton|oxford|cambridge|reading|brighton|bath|york|leicester|coventry|"
        r"city of london|west london|east london|central london|north london|south london|canary wharf|"
        r"remote|hybrid|on-?site|work from home|wfh|england|scotland|wales|[A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2})\b",
        re.IGNORECASE)
    _DATE_PAT = re.compile(
        r"\b(\d+\s*(?:day|hour|minute|week|month)s?\s*ago|today|yesterday|just\s*(?:now|posted)|"
        r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}(?:\s*,?\s*\d{2,4})?)\b",
        re.IGNORECASE)

    def _process_js_extracted(self) -> list[dict]:
        """Turn raw JS-extracted card data into job dicts using text heuristics."""
        jobs = []
        for raw in self._last_browser_extracted:
            title = clean_text(raw.get("title"))
            if not title:
                continue
            job = {"source": self.source_name, "title": title, "url": strip_tracking(raw.get("url", ""))}
            if raw.get("job_id"):
                job["job_id"] = str(raw["job_id"])
            else:
                m = re.search(r"/job/(\d+)|-job(\d+)|/(\d{5,})", job["url"])
                if m:
                    job["job_id"] = next(g for g in m.groups() if g)
            company_from_link = clean_text(raw.get("_company_link_text"))
            if company_from_link:
                job["company"] = company_from_link
            card_text = raw.get("_card_text", "") or ""
            apply_salary(job, extract_salary_from_text(card_text, self.default_currency))
            company_candidates, location_candidates, snippet_candidates = [], [], []
            for seg in raw.get("_segments", []) or []:
                seg_clean = re.sub(r"<[^>]+>", "", seg).strip()
                if len(seg_clean) < 2 or seg_clean == title or seg_clean == company_from_link:
                    continue
                if not job.get("date_posted") and len(seg_clean) < 40:
                    dm = self._DATE_PAT.search(seg_clean)
                    if dm:
                        job["date_posted"] = dm.group(1).strip()
                        continue
                if not job.get("employment_type") and len(seg_clean) < 40:
                    et = detect_employment_type(seg_clean)
                    if et:
                        job["employment_type"] = et
                        if len(seg_clean) < 20:
                            continue
                if re.search(r"[£$€]\s*\d", seg_clean):
                    continue
                if self._LOC_PAT.search(seg_clean) and len(seg_clean) < 80:
                    if company_from_link and (seg_clean.lower() in company_from_link.lower()
                                              or company_from_link.lower() in seg_clean.lower()):
                        continue
                    location_candidates.append(seg_clean)
                    continue
                (company_candidates if len(seg_clean) < 50 else snippet_candidates).append(seg_clean)
            if not job.get("employment_type"):
                job["employment_type"] = detect_employment_type(card_text)
            if not job.get("date_posted"):
                dm = self._DATE_PAT.search(card_text)
                if dm:
                    job["date_posted"] = dm.group(1).strip()
            if not job.get("company"):
                for c in company_candidates:
                    text = re.sub(r"^(?:Company|Posted by|Employer)\s*:?\s*", "", c, flags=re.IGNORECASE).strip()
                    if 1 < len(text) < 80:
                        job["company"] = clean_text(text)
                        break
            if location_candidates:
                job["location"] = clean_text(re.sub(r"^Location\s*:?\s*", "", location_candidates[0], flags=re.IGNORECASE))
            if snippet_candidates:
                job["snippet"] = clean_text(re.sub(r"<[^>]+>", "", snippet_candidates[0]))[:500]
            job["work_mode"] = detect_work_mode(job.get("location"), card_text)
            jobs.append(job)
        return jobs

    def _extract_jsonld_jobs(self, html_or_soup) -> list[dict]:
        soup = _as_soup(html_or_soup)
        jobs = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                t = item.get("@type")
                if t == "JobPosting":
                    jobs.append(self._jsonld_to_job(item))
                elif t == "ItemList":
                    for elem in item.get("itemListElement", []):
                        posting = elem.get("item", elem) if isinstance(elem, dict) else {}
                        if isinstance(posting, dict) and posting.get("@type") == "JobPosting":
                            jobs.append(self._jsonld_to_job(posting))
                elif "@graph" in item:
                    for g in item["@graph"]:
                        if isinstance(g, dict) and g.get("@type") == "JobPosting":
                            jobs.append(self._jsonld_to_job(g))
        return jobs

    def _jsonld_to_job(self, p: dict) -> dict:
        job = {
            "source": self.source_name,
            "title": clean_text(p.get("title", "")),
            "url": strip_tracking(p.get("url", "")),
            "date_posted": p.get("datePosted", ""),
            "valid_through": p.get("validThrough", ""),
            "snippet": clean_text(re.sub(r"<[^>]+>", " ", p.get("description", "") or ""))[:500],
            "full_description": p.get("description", "") or "",
        }
        org = p.get("hiringOrganization", {})
        job["company"] = clean_text(org.get("name", "") if isinstance(org, dict) else str(org))
        loc = p.get("jobLocation", {})
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                job["location"] = ", ".join(x for x in (addr.get("addressLocality", ""), addr.get("addressRegion", "")) if x)
            elif isinstance(addr, str):
                job["location"] = addr
        sal = p.get("baseSalary", {})
        if isinstance(sal, dict):
            cur = sal.get("currency") or self.default_currency
            val = sal.get("value", {})
            if isinstance(val, dict):
                lo = val.get("minValue") or val.get("value")
                hi = val.get("maxValue") or lo
                unit = (val.get("unitText") or "YEAR").upper()
                period = {"YEAR": "annum", "MONTH": "month", "WEEK": "week", "DAY": "day", "HOUR": "hour"}.get(unit, "annum")
                try:
                    lo = float(lo or hi or 0)
                    hi = float(hi or lo or 0)
                except (TypeError, ValueError):
                    lo = hi = 0.0
                if lo or hi:
                    sym = CURRENCY_SYMBOL.get(cur, cur + " ")
                    label = {"annum": "per annum", "month": "per month", "week": "per week", "day": "per day", "hour": "per hour"}[period]
                    raw = f"{sym}{lo:,.0f} {label}" if lo == hi else f"{sym}{lo:,.0f} - {sym}{hi:,.0f} {label}"
                    apply_salary(job, {"raw": raw, "min": lo, "max": hi, "currency": cur, "period": period})
        emp = p.get("employmentType", "")
        if isinstance(emp, list):
            emp = ", ".join(emp)
        job["employment_type"] = clean_text(str(emp)).replace("_", "-").title()
        if p.get("jobLocationType") == "TELECOMMUTE":
            job["work_mode"] = "Remote"
        ident = p.get("identifier", {})
        if isinstance(ident, dict) and ident.get("value"):
            job["job_id"] = str(ident["value"])
        elif isinstance(ident, str) and ident:
            job["job_id"] = ident
        elif job["url"]:
            m = re.search(r"/(\d{4,})", job["url"])
            if m:
                job["job_id"] = m.group(1)
        return job

    @staticmethod
    def _extract_next_data(html_or_soup) -> dict | None:
        soup = _as_soup(html_or_soup)
        script = soup.select_one("script#__NEXT_DATA__")
        if script and script.string:
            try:
                return json.loads(script.string)
            except json.JSONDecodeError:
                return None
        return None
