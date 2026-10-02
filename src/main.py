"""
International Jobs Board Scraper - multi-board aggregator (Apify Actor).

Boards:
  UK:     Reed, Totaljobs, CV-Library, CWJobs, Indeed UK, GOV.UK Find a Job
  US:     USAJobs, Indeed US
  EU:     Indeed DE/FR/NL, Arbeitnow
  Global: Adzuna (multi-country API), RemoteOK, Indeed AU

Cost model on Apify is memory x wall-clock (+ proxy GB), so this entry point:
  * runs every board concurrently instead of browser boards one after another,
  * fetches over HTTP and only launches Chromium if a board is actually blocked,
  * gives API boards a direct (unproxied) client - residential bandwidth is the
    most expensive thing in the bill and public APIs don't need it,
  * pushes results in batches per page instead of one API call per row.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import statistics
import time
from collections import defaultdict

import httpx
from apify import Actor

from .boards.adzuna import AdzunaScraper
from .boards.arbeitnow import ArbeitnowScraper
from .boards.cvlibrary import CVLibraryScraper
from .boards.cwjobs import CWJobsScraper
from .boards.findajob import FindAJobScraper
from .boards.indeed import IndeedScraper, IndeedUKScraper
from .boards.reed import ReedScraper
from .boards.remoteok import RemoteOKScraper
from .boards.totaljobs import TotaljobsScraper
from .boards.usajobs import USAJobsScraper
from .utils import BrowserPool, annualise, make_api_headers, make_headers, normalize_date

# board -> (factory, kind). kind: "html" = proxied HTTP + browser fallback, "api" = direct, no browser.
BOARD_REGISTRY: dict[str, tuple] = {
    "reed":      (ReedScraper, "html"),
    "totaljobs": (TotaljobsScraper, "html"),
    "cvlibrary": (CVLibraryScraper, "html"),
    "cwjobs":    (CWJobsScraper, "html"),
    "indeed":    (IndeedUKScraper, "html"),
    "indeed_us": (lambda c, **kw: IndeedScraper(c, base_url="https://www.indeed.com", source="indeed.com", currency="USD", **kw), "html"),
    "indeed_de": (lambda c, **kw: IndeedScraper(c, base_url="https://de.indeed.com", source="indeed.de", currency="EUR", **kw), "html"),
    "indeed_fr": (lambda c, **kw: IndeedScraper(c, base_url="https://fr.indeed.com", source="indeed.fr", currency="EUR", **kw), "html"),
    "indeed_nl": (lambda c, **kw: IndeedScraper(c, base_url="https://nl.indeed.com", source="indeed.nl", currency="EUR", **kw), "html"),
    "indeed_au": (lambda c, **kw: IndeedScraper(c, base_url="https://au.indeed.com", source="indeed.au", currency="AUD", **kw), "html"),
    "findajob":  (FindAJobScraper, "api"),      # gov.uk blocks proxies; plain HTML over a direct connection
    "usajobs":   (USAJobsScraper, "api"),
    "remoteok":  (RemoteOKScraper, "api"),
    "arbeitnow": (ArbeitnowScraper, "api"),
    "adzuna":    (AdzunaScraper, "api"),
}

COUNTRY_DEFAULTS = {
    "uk": ["reed", "totaljobs", "cvlibrary", "cwjobs", "indeed", "findajob", "adzuna"],
    "us": ["usajobs", "indeed_us", "adzuna", "remoteok"],
    "de": ["indeed_de", "adzuna", "arbeitnow"],
    "fr": ["indeed_fr", "adzuna"],
    "nl": ["indeed_nl", "adzuna"],
    "au": ["indeed_au", "adzuna"],
    "remote": ["remoteok", "arbeitnow", "adzuna"],
}
ADZUNA_COUNTRY = {"uk": "gb", "us": "us", "de": "de", "fr": "fr", "nl": "nl", "au": "au", "remote": "gb"}
COUNTRY_CURRENCY = {"uk": "GBP", "us": "USD", "de": "EUR", "fr": "EUR", "nl": "EUR", "au": "AUD", "remote": "USD"}
ACCEPT_LANGUAGE = {"de": "de-DE,de;q=0.9,en;q=0.7", "fr": "fr-FR,fr;q=0.9,en;q=0.7", "nl": "nl-NL,nl;q=0.9,en;q=0.7",
                   "us": "en-US,en;q=0.9", "au": "en-AU,en;q=0.9"}

PUSH_BATCH = 200
OUTPUT_FIELDS = ("title", "company", "location", "salary_raw", "salary_min", "salary_max", "salary_currency",
                 "salary_period", "employment_type", "work_mode", "snippet", "date_posted", "valid_through",
                 "url", "job_id", "source", "category")


# ──────────────────────────────────────────────────────────────────────
# Categorisation (title keyword matching, first match wins)
# ──────────────────────────────────────────────────────────────────────

_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("Software Development", ("software eng", "software dev", "full stack", "fullstack", "full-stack", "frontend", "front-end",
                              "front end", "backend", "back-end", "back end", "web dev", "mobile dev", "ios dev", "android dev",
                              "flutter", "react", "angular", "vue.js", "node.js", "python dev", "java dev", ".net dev", "c# dev",
                              "c++ dev", "rust dev", "golang", "ruby dev", "php dev", "programmer", "coder", "software architect",
                              "principal engineer", "lead developer", "lead engineer", "tech lead")),
    ("Data & Analytics", ("data scien", "data analy", "data eng", "machine learn", "ml eng", "ai eng", "artificial intelligence",
                          "deep learn", "nlp", "computer vision", "business intellig", "bi analyst", "bi developer", "analytics eng",
                          "data architect")),
    ("DevOps & Infrastructure", ("devops", "sre", "site reliab", "platform eng", "cloud eng", "cloud arch", "infrastructure",
                                 "kubernetes", "docker", "terraform", "aws eng", "azure eng", "gcp eng", "systems eng", "linux eng",
                                 "network eng", "devsecops")),
    ("Cybersecurity", ("cyber", "security eng", "security analy", "penetration", "pen test", "infosec", "information security",
                       "soc analyst", "security architect", "security consult")),
    ("QA & Testing", ("qa ", "qa eng", "test eng", "tester", "sdet", "automation eng", "quality assurance", "test analy",
                      "test lead", "test manager")),
    ("Product Management", ("product manager", "product owner", "product lead", "head of product", "vp product", "cpo",
                            "product director")),
    ("Project & Delivery Management", ("project manager", "programme manager", "program manager", "delivery manager",
                                       "scrum master", "agile coach", "release manager", "pmo", "project coordinator",
                                       "delivery lead")),
    ("Design & UX", ("ux ", "ui ", "ux/ui", "ui/ux", "user experience", "user interface", "product design", "interaction design",
                     "visual design", "graphic design", "web design", "design lead", "creative director")),
    ("IT Management & Support", ("it manager", "it director", "cto", "cio", "head of engineering", "head of it", "vp engineering",
                                 "engineering manager", "development manager", "it service", "service desk", "helpdesk",
                                 "help desk", "it support", "desktop support", "it admin", "systems admin", "it operations")),
    ("Database & BI", ("dba", "database admin", "database eng", "database dev", "sql dev", "etl dev", "data warehouse",
                       "database architect")),
    ("IT Consulting & Architecture", ("solution architect", "enterprise architect", "technical architect", "integration architect",
                                      "it consult", "technology consult", "technical consult", "sap consult", "salesforce",
                                      "dynamics 365", "erp consult", "crm consult", "business analyst")),
    ("Finance & Accounting", ("accountant", "accounting", "finance manager", "financial analy", "financial controller",
                              "bookkeeper", "payroll", "tax ", "audit", "treasury", "credit analy", "fund manager",
                              "investment analy", "actuary", "cfo")),
    ("Marketing & Communications", ("marketing", "seo ", "ppc ", "content writer", "copywriter", "social media", "digital market",
                                    "brand manager", "communications", "pr manager", "public relations", "cmo")),
    ("Sales & Business Development", ("sales manager", "sales exec", "sales rep", "account manager", "account exec",
                                      "business develop", "bdr ", "sdr ", "commercial manager", "sales director")),
    ("HR & Recruitment", ("hr ", "human resource", "recruiter", "recruitment", "talent acqui", "people manager", "people partner",
                          "learning and dev", "l&d ", "training manager", "compensation", "reward", "hrbp")),
    ("Healthcare & Medical", ("nurse", "doctor", "clinical", "pharmacist", "physiotherapist", "occupational therap", "healthcare",
                              "medical", "gp ", "surgeon", "dental", "radiographer", "midwife", "care assistant", "support worker")),
    ("Education & Training", ("teacher", "lecturer", "professor", "tutor", "teaching assistant", "education", "headteacher",
                              "head of department", "send ", "pastoral")),
    ("Engineering", ("mechanical eng", "electrical eng", "civil eng", "structural eng", "chemical eng", "process eng",
                     "manufacturing eng", "maintenance eng", "building services", "quantity surveyor", "site manager",
                     "construction manager")),
    ("Legal & Compliance", ("solicitor", "barrister", "paralegal", "legal counsel", "legal advisor", "lawyer", "conveyancer",
                            "compliance officer", "regulatory")),
    ("Operations & Logistics", ("supply chain", "logistics", "procurement", "warehouse", "buyer", "purchasing",
                                "operations manager", "operations director", "facilities", "fleet manager")),
    ("Customer Service", ("customer service", "customer support", "call centre", "call center", "contact centre",
                          "client service", "customer success")),
    ("Administration", ("admin", "office manager", "receptionist", "personal assistant", "executive assistant", "secretary",
                        "office coordinator")),
]


def _compile_category(keys: tuple[str, ...]) -> re.Pattern:
    # Every key must start at a word boundary ("postdoctoral" is not a doctor,
    # "director" is not a CTO). Short keys must also end at one, so "cto"
    # cannot match inside "contractor"; longer keys stay prefix matches so
    # "software eng" still covers "software engineering".
    parts = []
    for k in keys:
        core = k.strip()
        esc = re.escape(k)
        lead = r"\b" if core[:1].isalnum() else ""
        trail = r"\b" if len(core) <= 4 and core.isalpha() else ""
        parts.append(f"{lead}{esc.strip() if trail else esc}{trail}")
    return re.compile("|".join(parts))


_CATEGORY_PATTERNS = [(name, _compile_category(keys)) for name, keys in _CATEGORIES]


def categorize_job(title: str) -> str:
    if not title:
        return "Other"
    t = title.lower()
    for name, pattern in _CATEGORY_PATTERNS:
        if pattern.search(t):
            return name
    return "Other"


# ──────────────────────────────────────────────────────────────────────
# Result sink: normalise -> filter -> dedup -> (stream | collect)
# ──────────────────────────────────────────────────────────────────────

_NORM = re.compile(r"[^a-z0-9]+")


def fingerprint(job: dict) -> str:
    title = _NORM.sub(" ", job["title"].lower()).strip()
    company = _NORM.sub(" ", job["company"].lower()).strip()
    loc = job["location"].lower().split(",")[0].strip()
    if not company:
        # Without a company name the title+location key is too coarse; only
        # treat exact same listing (by id/url) as a duplicate.
        return f"{job['source']}|{job['job_id'] or job['url']}|{title}"
    return f"{title}|{company}|{loc}"


class ResultSink:
    def __init__(self, *, deduplicate: bool, salary_min: int | None, currency: str,
                 posted_within_days: int | None, streaming: bool):
        self.deduplicate = deduplicate
        self.salary_min = salary_min
        self.currency = currency
        self.posted_within_days = posted_within_days
        self.streaming = streaming
        self.seen: set[str] = set()
        self.per_source: dict[str, list[dict]] = defaultdict(list)
        self.pushed = 0
        self.dropped = {"duplicate": 0, "salary": 0, "date": 0}
        self._pending: list[dict] = []

    def normalize(self, job: dict) -> dict:
        out = {k: job.get(k) for k in OUTPUT_FIELDS}
        for k in OUTPUT_FIELDS:
            if k in ("salary_min", "salary_max"):
                try:
                    v = float(out[k]) if out[k] not in (None, "") else 0.0
                except (TypeError, ValueError):
                    v = 0.0
                out[k] = round(v) if v > 0 else None
            elif out[k] is None:
                out[k] = ""
            elif not isinstance(out[k], str):
                out[k] = str(out[k])
        out["salary_currency"] = out["salary_currency"] or self.currency
        out["salary_period"] = out["salary_period"] or ("annum" if out["salary_min"] else "")
        if out["date_posted"]:
            out["date_posted"] = normalize_date(out["date_posted"]) or out["date_posted"]
        if out["valid_through"]:
            out["valid_through"] = normalize_date(out["valid_through"], future_ok=True) or out["valid_through"]
        if not out["category"]:
            out["category"] = categorize_job(out["title"])
        return out

    def accept(self, job: dict) -> bool:
        if self.salary_min and job["salary_max"] and job["salary_currency"] == self.currency:
            if annualise(job["salary_max"], job["salary_period"]) < self.salary_min:
                self.dropped["salary"] += 1
                return False
        if self.posted_within_days and re.fullmatch(r"\d{4}-\d{2}-\d{2}", job["date_posted"]):
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=self.posted_within_days)).isoformat()
            if job["date_posted"] < cutoff:
                self.dropped["date"] += 1
                return False
        if self.deduplicate:
            fp = fingerprint(job)
            if fp in self.seen:
                self.dropped["duplicate"] += 1
                return False
            self.seen.add(fp)
        return True

    async def on_page(self, source: str, jobs: list[dict]) -> None:
        accepted = [j for j in map(self.normalize, jobs) if j["title"] and self.accept(j)]
        if not accepted:
            return
        if self.streaming:
            await self.push(accepted)
        else:
            self.per_source[source].extend(accepted)

    async def push(self, jobs: list[dict]) -> None:
        for i in range(0, len(jobs), PUSH_BATCH):
            chunk = jobs[i:i + PUSH_BATCH]
            # shield: a per-board timeout must not cancel a push half-way and
            # leave rows marked as seen but never written.
            await asyncio.shield(Actor.push_data(chunk))
            self.pushed += len(chunk)

    def interleaved(self, limit: int) -> list[dict]:
        """Round-robin across boards so no single board dominates the first N rows."""
        iters = {s: iter(j) for s, j in self.per_source.items() if j}
        out: list[dict] = []
        while iters and len(out) < limit:
            for s in list(iters):
                job = next(iters[s], None)
                if job is None:
                    del iters[s]
                else:
                    out.append(job)
                    if len(out) >= limit:
                        break
        return out


def compute_salary_benchmarks(jobs: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for job in jobs:
        lo, hi = job.get("salary_min"), job.get("salary_max")
        if not (lo or hi):
            continue
        mid = annualise(((lo or hi) + (hi or lo)) / 2, job.get("salary_period") or "annum")
        if mid < 5000 or mid > 500000:
            continue
        title = job["title"].lower().strip()
        loc = job["location"].lower().split(",")[0].strip() or "unknown"
        buckets[(title, loc, job["salary_currency"])].append(mid)
    out = []
    for (title, loc, cur), vals in buckets.items():
        if len(vals) < 2:
            continue
        vals.sort()
        out.append({
            "_type": "salary_benchmark", "benchmark_title": title, "benchmark_location": loc,
            "salary_currency": cur, "count": len(vals),
            "salary_mean": round(statistics.mean(vals)), "salary_median": round(statistics.median(vals)),
            "salary_p25": round(vals[len(vals) // 4]), "salary_p75": round(vals[(len(vals) * 3) // 4]),
            "salary_min": round(vals[0]), "salary_max": round(vals[-1]),
        })
    return sorted(out, key=lambda b: b["count"], reverse=True)


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def _pick(actor_input: dict, preset_key: str, custom_key: str, default: str) -> str:
    custom = (actor_input.get(custom_key) or "").strip()
    preset = (actor_input.get(preset_key) or "").strip()
    if custom:
        return custom
    if preset and preset != "__custom__":
        return preset
    return default


async def run_board(name: str, scraper, keyword: str, location: str, per_board: int,
                    job_type: str, salary_min: int | None, timeout: float, stats: dict) -> None:
    """Run one board with a wall-clock budget; results stream out via the sink."""
    t0 = time.perf_counter()
    entry = stats.setdefault(name, {"source": scraper.source_name, "jobs": 0, "pages": 0,
                                    "mode": "", "secs": 0.0, "error": ""})
    try:
        await asyncio.wait_for(
            scraper.search(keyword=keyword, location=location, max_results=per_board,
                           job_type=job_type, salary_min=salary_min),
            timeout=timeout)
    except asyncio.TimeoutError:
        entry["error"] = f"timeout after {timeout:.0f}s (partial results kept)"
        scraper.exhausted = True
        Actor.log.warning(f"[{scraper.source_name}] {entry['error']}")
    except Exception as e:  # one broken board must never sink the run
        entry["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        scraper.exhausted = True
        Actor.log.exception(f"[{scraper.source_name}] scraper failed")
    finally:
        entry["jobs"] = scraper.stats.get("jobs", 0)
        entry["pages"] = scraper.stats.get("pages", 0)
        entry["mode"] = scraper.stats.get("mode", "")
        entry["secs"] = round(entry["secs"] + time.perf_counter() - t0, 1)


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        keyword = _pick(actor_input, "keyword", "custom_keyword", "software engineer")
        location = _pick(actor_input, "location", "custom_location", "London")
        country = (actor_input.get("country") or "uk").lower()
        unlimited = bool(actor_input.get("unlimited", False))
        max_results = 0 if unlimited else max(int(actor_input.get("max_results") or 100), 100)
        salary_min = int(actor_input["salary_min"]) if actor_input.get("salary_min") else None
        job_type = (actor_input.get("job_type") or "all").lower()
        posted_within_days = int(actor_input["posted_within_days"]) if actor_input.get("posted_within_days") else None
        deduplicate = bool(actor_input.get("deduplicate", True))
        salary_benchmark = bool(actor_input.get("salary_benchmark", False))
        max_pages = int(actor_input.get("max_pages_per_board") or 40)
        proxy_mode = (actor_input.get("proxy_mode") or "residential").lower()
        selected = [b for b in (actor_input.get("boards") or COUNTRY_DEFAULTS.get(country, COUNTRY_DEFAULTS["uk"]))
                    if b in BOARD_REGISTRY]
        if not selected:
            Actor.log.error("No valid boards selected")
            await Actor.set_status_message("No valid boards selected", is_terminal=True)
            return

        currency = COUNTRY_CURRENCY.get(country, "GBP")
        n = len(selected)
        if unlimited:
            per_board = 10 ** 6
        else:
            # Over-ask each board by 25% so dedup and short boards still let us
            # reach the requested total; the sink truncates to max_results.
            per_board = max(10, math.ceil(max_results * 1.25 / n))

        Actor.log.info(f"Scrape '{keyword}' in '{location}' country={country} boards={selected} "
                       f"max={'unlimited' if unlimited else max_results} per_board={per_board} type={job_type} "
                       f"salary_min={salary_min} posted_within={posted_within_days}")
        await Actor.set_status_message(f"Searching {n} boards for '{keyword}' in '{location}'")

        # ── Proxy (residential, country-targeted, HTML boards only) ──
        proxy_config = None
        html_boards = [b for b in selected if BOARD_REGISTRY[b][1] == "html"]
        if html_boards and Actor.is_at_home() and proxy_mode != "none":
            groups = ["RESIDENTIAL"] if proxy_mode == "residential" else None
            try:
                proxy_config = await Actor.create_proxy_configuration(
                    groups=groups, country_code=ADZUNA_COUNTRY.get(country, "gb").upper())
                Actor.log.info(f"Proxy: {proxy_mode} ({ADZUNA_COUNTRY.get(country, 'gb').upper()}) for {html_boards}")
            except Exception as e:
                Actor.log.warning(f"Proxy configuration failed, continuing without proxy: {e}")

        browser_pool = BrowserPool(proxy_config=proxy_config) if html_boards else None
        sink = ResultSink(deduplicate=deduplicate, salary_min=salary_min, currency=currency,
                          posted_within_days=posted_within_days, streaming=unlimited)

        direct_client = httpx.AsyncClient(headers=make_api_headers(), timeout=httpx.Timeout(30.0, connect=10.0),
                                          follow_redirects=True)
        clients: list[httpx.AsyncClient] = [direct_client]
        scrapers: dict[str, object] = {}
        adzuna_country = ADZUNA_COUNTRY.get(country, "gb")

        reed_api_key = (actor_input.get("reed_api_key") or "").strip()
        for board in selected:
            factory, kind = BOARD_REGISTRY[board]
            extra: dict = {"on_page": sink.on_page}
            if board == "reed" and reed_api_key:
                kind = "api"  # official API: no proxy, no browser
                extra["api_key"] = reed_api_key
            if kind == "html":
                proxy_url = None
                if proxy_config:
                    proxy_url = await proxy_config.new_url(session_id=f"{board}_{random.randint(1000, 9999)}")
                client = httpx.AsyncClient(headers=make_headers(ACCEPT_LANGUAGE.get(country, "en-GB,en;q=0.9")),
                                           proxy=proxy_url, timeout=httpx.Timeout(25.0, connect=12.0),
                                           follow_redirects=True)
                clients.append(client)
                extra.update(browser_pool=browser_pool, proxy_url=proxy_url, proxy_config=proxy_config)
            else:
                client = direct_client
                if board == "findajob":
                    # gov.uk wants browser-ish headers but no proxy
                    client = httpx.AsyncClient(headers=make_headers(), timeout=httpx.Timeout(25.0, connect=12.0),
                                               follow_redirects=True)
                    clients.append(client)
                elif board == "adzuna":
                    extra.update(app_id=actor_input.get("adzuna_app_id") or "",
                                 app_key=actor_input.get("adzuna_app_key") or "", country=adzuna_country)
                elif board == "usajobs":
                    extra.update(api_key=actor_input.get("usajobs_api_key") or "",
                                 user_email=actor_input.get("usajobs_user_email") or "")
            scraper = factory(client, **extra)
            pages = max_pages if unlimited else min(max_pages, math.ceil(per_board / scraper.page_size) + 1)
            scraper.max_pages = min(pages, scraper.hard_page_cap) if scraper.hard_page_cap else pages
            scrapers[board] = scraper

        # ── Run every board concurrently ──
        pages_needed = math.ceil(per_board / 25)
        board_timeout = 1800.0 if unlimited else float(max(150, min(900, 60 + 15 * pages_needed)))
        stats: dict[str, dict] = {}
        t_start = time.perf_counter()
        try:
            await asyncio.gather(*[
                run_board(name, s, keyword, location, per_board, job_type, salary_min, board_timeout, stats)
                for name, s in scrapers.items()
            ])

            # ── Top-up round: if blocked/empty boards left us short, ask the
            # boards that still have pages for the difference. Cheap (HTTP
            # pages) and it means users get the number of rows they paid for.
            if not unlimited:
                collected = sum(len(v) for v in sink.per_source.values())
                deficit = max_results - collected
                live = [b for b, s in scrapers.items() if not s.exhausted]
                if deficit > 0 and live:
                    extra = math.ceil(deficit * 1.25 / len(live))
                    Actor.log.info(f"Top-up: {collected}/{max_results} collected, asking {live} for ~{extra} more each")
                    for b in live:
                        s = scrapers[b]
                        s.max_pages = min(max_pages, s.max_pages + math.ceil(extra / s.page_size) + 1)
                        if s.hard_page_cap:
                            s.max_pages = min(s.max_pages, s.hard_page_cap)
                    await asyncio.gather(*[
                        run_board(b, scrapers[b], keyword, location, extra, job_type, salary_min,
                                  max(90.0, board_timeout / 2), stats)
                        for b in live
                    ])

            if not unlimited:
                final = sink.interleaved(max_results)
                Actor.log.info(f"Pushing {len(final)} rows ({sum(len(v) for v in sink.per_source.values())} collected)")
                await sink.push(final)
            else:
                final = [j for v in sink.per_source.values() for j in v]

            if salary_benchmark:
                source = final if not unlimited else []
                if unlimited:
                    Actor.log.info("Salary benchmarks: computed from the dataset in unlimited mode")
                    ds = await Actor.open_dataset()
                    source = [i async for i in ds.iterate_items()]
                benches = compute_salary_benchmarks(source)
                if benches:
                    await Actor.push_data(benches)
                Actor.log.info(f"Generated {len(benches)} salary benchmarks")
        finally:
            for c in clients:
                try:
                    await c.aclose()
                except Exception:
                    pass
            if browser_pool:
                await browser_pool.close()

        # ── Summary ──
        elapsed = round(time.perf_counter() - t_start, 1)
        summary = {
            "keyword": keyword, "location": location, "country": country, "boards": selected,
            "rows_pushed": sink.pushed, "dropped": sink.dropped, "elapsed_secs": elapsed,
            "browser_launched": bool(browser_pool and browser_pool.launched),
            "per_board": stats,
        }
        await Actor.set_value("RUN_STATS", summary)
        Actor.log.info("══════════ SCRAPE COMPLETE ══════════")
        Actor.log.info(f"rows={sink.pushed} dropped={sink.dropped} elapsed={elapsed}s browser={'yes' if summary['browser_launched'] else 'no'}")
        for name, e in stats.items():
            Actor.log.info(f"  {e['source']:<22} {e['jobs']:>5} jobs  {e['pages']:>3} pages  {e['mode']:<8} {e['secs']:>6}s  {e['error']}")
        await Actor.set_status_message(
            f"Done: {sink.pushed} jobs from {sum(1 for e in stats.values() if e['jobs'])}/{n} boards in {elapsed}s",
            is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
