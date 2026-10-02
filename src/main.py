"""
Jobs Board Scraper: Indeed, Reed, Adzuna, RemoteOK & More - Multi-Board Aggregator
Scrapes job listings from UK, US, EU and remote job boards with unified output.

Supported boards:
  UK:     Reed, Totaljobs, CV-Library, CWJobs, Indeed UK, GOV.UK Find a Job (Work Hub)
  US:     USAJobs, Indeed US
  EU:     Indeed DE/FR/NL, Arbeitnow
  Global: Adzuna (multi-country API), RemoteOK, The Muse, Remotive, Jobicy

Cost model on Apify is memory x wall-clock (+ residential-proxy GB), so the
collection phase here:
  * runs every board concurrently instead of browser boards one after another,
  * fetches over HTTP and only launches Chromium for a board that is blocked,
  * gives API boards a direct (unproxied) client,
  * bounds every board with a wall-clock budget so a hung site cannot burn CU,
  * tops up from boards that still have pages when others come up short.
Everything after collection (enrich -> filter -> merge -> incremental -> push)
is the v0.11 pipeline, unchanged.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from apify import Actor
from apify_client.errors import ApifyApiError

from .boards.adzuna import AdzunaScraper
from .boards.arbeitnow import ArbeitnowScraper
from .boards.cvlibrary import CVLibraryScraper
from .boards.cwjobs import CWJobsScraper
from .boards.findajob import FindAJobScraper
from .boards.indeed import IndeedScraper, IndeedUKScraper
from .boards.jobicy import JobicyScraper
from .boards.reed import ReedScraper
from .boards.remoteok import RemoteOKScraper
from .boards.remotive import RemotiveScraper
from .boards.themuse import TheMuseScraper
from .boards.totaljobs import TotaljobsScraper
from .boards.usajobs import USAJobsScraper
from . import pipeline
from .utils import HTML_TIMEOUT, BrowserPool, make_api_headers, make_headers, normalize_date

# board -> (factory, kind). "html": proxied HTTP with lazy browser fallback;
# "api": direct connection, never a browser.
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
    # gov.uk's WAF 403s datacenter IPs but accepts residential traffic, so it
    # takes the proxied client; plain HTML, browser only as a last resort.
    "findajob":  (FindAJobScraper, "html"),
    "usajobs":   (USAJobsScraper, "api"),
    "remoteok":  (RemoteOKScraper, "api"),
    "arbeitnow": (ArbeitnowScraper, "api"),
    "themuse":   (TheMuseScraper, "api"),
    "remotive":  (RemotiveScraper, "api"),
    "jobicy":    (JobicyScraper, "api"),
    "adzuna":    (AdzunaScraper, "api"),
}

COUNTRY_DEFAULTS = {
    "uk": ["reed", "totaljobs", "cvlibrary", "cwjobs", "indeed", "findajob", "adzuna", "themuse"],
    "us": ["usajobs", "indeed_us", "adzuna", "remoteok", "themuse", "remotive"],
    "de": ["indeed_de", "adzuna", "arbeitnow", "themuse"],
    "fr": ["indeed_fr", "adzuna", "themuse"],
    "nl": ["indeed_nl", "adzuna", "themuse"],
    "au": ["indeed_au", "adzuna", "themuse"],
    "remote": ["remoteok", "arbeitnow", "remotive", "jobicy", "themuse", "adzuna"],
}
ADZUNA_COUNTRY_MAP = {"uk": "gb", "us": "us", "de": "de", "fr": "fr", "nl": "nl", "au": "au", "remote": "gb"}
ACCEPT_LANGUAGE = {"de": "de-DE,de;q=0.9,en;q=0.7", "fr": "fr-FR,fr;q=0.9,en;q=0.7", "nl": "nl-NL,nl;q=0.9,en;q=0.7",
                   "us": "en-US,en;q=0.9", "au": "en-AU,en;q=0.9"}

MAX_SEARCH_TERMS = 10
UNLIMITED_BOARD_CAP = 2000
RESOLVE_HARD_CAP = 200
PUSH_BATCH = 500

# Default salary currency per source board, used when a board doesn't set one.
SOURCE_CURRENCY = {
    "usajobs.gov": "USD", "remoteok.com": "USD", "remotive.com": "USD", "jobicy.com": "USD",
    "themuse.com": "USD", "indeed.com": "USD", "arbeitnow.com": "EUR", "indeed.de": "EUR",
    "indeed.fr": "EUR", "indeed.nl": "EUR", "indeed.au": "AUD", "adzuna.us": "USD",
    "adzuna.de": "EUR", "adzuna.fr": "EUR", "adzuna.nl": "EUR", "adzuna.au": "AUD",
}


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
    # Every key starts at a word boundary ("postdoctoral" is not a doctor,
    # "director" is not a CTO); short keys also end at one so "cto" cannot
    # match inside "contractor". Longer keys stay prefix matches.
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


def normalize_job(job: dict) -> dict:
    """Ensure every job dict has every expected field (no undefined in output)."""
    defaults = {
        "title": "", "company": "", "location": "", "salary_raw": "", "salary_min": None, "salary_max": None,
        "salary_currency": "", "salary_period": "", "snippet": "", "employment_type": "", "work_mode": "",
        "date_posted": "", "valid_through": "", "url": "", "job_id": "", "source": "", "category": "",
    }
    normalized = {**defaults, **{k: v for k, v in job.items() if v is not None and v != ""}}
    # full_description is kept: the pipeline builds `description` from it and
    # strips it in finalize().
    for key in ("title", "company", "location", "salary_raw", "snippet", "employment_type", "work_mode",
                "date_posted", "valid_through", "url", "job_id", "source", "category", "salary_period",
                "salary_currency"):
        if normalized.get(key) is None:
            normalized[key] = defaults[key]
    for key in ("salary_min", "salary_max"):
        try:
            v = float(normalized[key]) if normalized[key] not in (None, "") else 0.0
        except (TypeError, ValueError):
            v = 0.0
        normalized[key] = v if v > 0 else None
    if normalized["date_posted"]:
        normalized["date_posted"] = normalize_date(normalized["date_posted"]) or str(normalized["date_posted"])
    if normalized["valid_through"]:
        normalized["valid_through"] = normalize_date(normalized["valid_through"], future_ok=True) or str(normalized["valid_through"])
    if not normalized["category"]:
        normalized["category"] = categorize_job(normalized["title"])
    if not normalized["salary_currency"]:
        normalized["salary_currency"] = SOURCE_CURRENCY.get(normalized["source"], "GBP")
    return normalized


async def resolve_apply_urls(jobs: list[dict], client: httpx.AsyncClient) -> None:
    """Opt-in (resolveApplyUrl): follow redirects on aggregator links to reveal
    the real ATS/company apply URL, then re-detect the ATS. Bounded concurrency
    and a hard cap; runs over the un-proxied client on the final set only."""
    targets = [j for j in jobs if j.get("url")][:RESOLVE_HARD_CAP]
    if not targets:
        return
    Actor.log.info(f"[resolveApplyUrl] Resolving apply URLs for {len(targets)} jobs...")
    sem = asyncio.Semaphore(15)

    async def resolve(job: dict) -> None:
        url = job.get("url")
        async with sem:
            final = None
            try:
                resp = await client.head(url, follow_redirects=True, timeout=15.0)
                if resp.status_code == 405:
                    resp = await client.get(url, follow_redirects=True, timeout=15.0)
                final = str(resp.url)
            except Exception:
                return
        if final and final != url:
            job["resolved_url"] = final
            pipeline.redetect_ats(job)

    await asyncio.gather(*[resolve(j) for j in targets], return_exceptions=True)


def compute_salary_benchmarks(jobs: list[dict]) -> list[dict]:
    """Salary benchmarks grouped by title + location, from the pipeline's
    standardised annual figures so they match the per-record fields."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for job in jobs:
        ann_min, ann_max = job.get("salary_annual_min"), job.get("salary_annual_max")
        if not ann_min and not ann_max:
            continue
        mid = ((ann_min or ann_max) + (ann_max or ann_min)) / 2
        if mid < 5000 or mid > 500000:
            continue
        title = job.get("title", "").lower().strip()
        loc_key = (job.get("location", "").lower().strip().split(",")[0].strip()) or "unknown"
        buckets[(title, loc_key)].append(mid)
    out = []
    for (title, loc), vals in buckets.items():
        if len(vals) < 2:
            continue
        vals.sort()
        out.append({
            "benchmark_title": title, "benchmark_location": loc, "count": len(vals),
            "salary_mean": round(statistics.mean(vals)), "salary_median": round(statistics.median(vals)),
            "salary_p25": round(vals[len(vals) // 4]), "salary_p75": round(vals[(len(vals) * 3) // 4]),
            "salary_min": round(vals[0]), "salary_max": round(vals[-1]), "_type": "salary_benchmark",
        })
    return sorted(out, key=lambda b: b["count"], reverse=True)


DATASET_ITEM_EVENT = "apify-default-dataset-item"


def _affordable_rows() -> int | None:
    """Rows the run's max-total-charge can still pay for, or None when the run
    is not metered per row (local run, no PPE pricing, or no charge limit)."""
    try:
        cm = Actor.get_charging_manager()
        if not cm.get_pricing_info().is_pay_per_event:
            return None
        return cm.calculate_max_event_charge_count_within_limit(DATASET_ITEM_EVENT)
    except Exception as e:  # charging info is advisory; never fail a run over it
        Actor.log.debug(f"Charging info unavailable: {e}")
        return None


def _charged_rows() -> int | None:
    try:
        cm = Actor.get_charging_manager()
        if not cm.get_pricing_info().is_pay_per_event:
            return None
        return cm.get_charged_event_count(DATASET_ITEM_EVENT)
    except Exception:
        return None


async def push_rows(items: list[dict]) -> int:
    """Push to the default dataset. actor.json registers the dataset schema,
    so the platform rejects a whole batch when ONE record fails validation;
    bisect such a batch so one bad record costs one row, not the run.
    Returns the number of records accepted."""
    if not items:
        return 0
    try:
        await Actor.push_data(items)
        return len(items)
    except ApifyApiError as exc:
        if "schema" not in str(exc.message or exc).lower():
            raise
        if len(items) == 1:
            Actor.log.warning(f"Dataset schema rejected a record, dropping it: {items[0].get('url') or items[0]} "
                              f"({exc.message}; {exc.data or ''})")
            return 0
        mid = len(items) // 2
        return await push_rows(items[:mid]) + await push_rows(items[mid:])


# ──────────────────────────────────────────────────────────────────────
# Board runner with a wall-clock budget
# ──────────────────────────────────────────────────────────────────────

async def run_board(name: str, scraper, term: str, location: str, limit: int, job_type: str,
                    salary_min, timeout: float, stats: dict) -> tuple[list[dict], bool]:
    """Run one board for one term. Returns (jobs, errored). Partial results
    are not lost on a timeout because stats/exhausted are tracked on the scraper."""
    t0 = time.perf_counter()
    entry = stats.setdefault(name, {"source": scraper.source_name, "jobs": 0, "pages": 0,
                                    "mode": "", "secs": 0.0, "error": ""})
    jobs: list[dict] = []
    errored = False
    buffered: list[dict] = []

    async def _on_page(_src: str, page_jobs: list[dict]) -> None:
        buffered.extend(page_jobs)

    scraper.on_page = _on_page
    try:
        returned = await asyncio.wait_for(
            scraper.search(keyword=term, location=location, max_results=limit,
                           job_type=job_type, salary_min=salary_min),
            timeout=timeout) or []
        jobs = buffered if buffered else returned
    except asyncio.TimeoutError:
        jobs = buffered   # pages already fetched are kept
        entry["error"] = f"timeout after {timeout:.0f}s ({len(jobs)} jobs kept)"
        scraper.exhausted = True
        errored = True
        Actor.log.warning(f"[{scraper.source_name}] '{term}': {entry['error']}")
    except Exception as e:  # one broken board must never sink the run
        jobs = buffered
        entry["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        scraper.exhausted = True
        errored = True
        Actor.log.exception(f"[{scraper.source_name}] failed on '{term}'")
    finally:
        scraper.on_page = None
        if not getattr(scraper, "resumable", True):
            scraper.exhausted = True
        entry["jobs"] += len(jobs)
        entry["pages"] = scraper.stats.get("pages", 0)
        entry["mode"] = scraper.stats.get("mode", "")
        entry["secs"] = round(entry["secs"] + time.perf_counter() - t0, 1)
    return jobs, errored


def _pick(actor_input: dict, preset_key: str, custom_key: str, default: str) -> str:
    custom = (actor_input.get(custom_key) or "").strip()
    preset = (actor_input.get(preset_key) or "").strip()
    if custom:
        return custom
    if preset and preset != "__custom__":
        return preset
    return default


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        keyword = _pick(actor_input, "keyword", "custom_keyword", "software engineer")
        location = _pick(actor_input, "location", "custom_location", "London")
        unlimited = bool(actor_input.get("unlimited", False))
        max_results = 0 if unlimited else max(int(actor_input.get("max_results") or 1000), 100)
        salary_min = int(actor_input["salary_min"]) if actor_input.get("salary_min") else None
        job_type = (actor_input.get("job_type") or "all").lower()
        country = (actor_input.get("country") or "uk").lower()
        salary_benchmark = bool(actor_input.get("salary_benchmark", False))

        search_terms = [t.strip() for t in (actor_input.get("searchTerms") or []) if t and t.strip()]
        if not search_terms:
            search_terms = [keyword]
        search_terms = list(dict.fromkeys(search_terms))[:MAX_SEARCH_TERMS]

        job_type_norm = actor_input.get("jobType", "any") or "any"
        posted_within_hours = actor_input.get("postedWithinHours") or None
        remote_only = bool(actor_input.get("remoteOnly", False))
        radius_miles = actor_input.get("radiusMiles") or None
        description_format = actor_input.get("descriptionFormat", "markdown") or "markdown"
        resolve_apply_url = bool(actor_input.get("resolveApplyUrl", False))
        incremental_only = bool(actor_input.get("incrementalOnly", False))
        deduplicate = bool(actor_input.get("deduplicate", True))
        max_pages = int(actor_input.get("max_pages_per_board") or 40)
        proxy_mode = (actor_input.get("proxy_mode") or "residential").lower()

        if job_type == "all" and job_type_norm != "any":
            job_type = {"fulltime": "permanent", "parttime": "part-time", "contract": "contract",
                        "internship": "all"}.get(job_type_norm, "all")

        selected_boards = [b for b in (actor_input.get("boards") or COUNTRY_DEFAULTS.get(country, COUNTRY_DEFAULTS["uk"]))
                           if b in BOARD_REGISTRY]
        # API keys: the run input wins; otherwise fall back to the Actor's own
        # secret environment variables (Console > Actor > Settings > Environment
        # variables) so published tasks can use Adzuna / USAJobs without the
        # keys appearing in a public task input.
        adzuna_app_id = (actor_input.get("adzuna_app_id") or os.environ.get("ADZUNA_APP_ID") or "").strip()
        adzuna_app_key = (actor_input.get("adzuna_app_key") or os.environ.get("ADZUNA_APP_KEY") or "").strip()
        usajobs_api_key = (actor_input.get("usajobs_api_key") or os.environ.get("USAJOBS_API_KEY") or "").strip()
        usajobs_email = (actor_input.get("usajobs_email") or os.environ.get("USAJOBS_EMAIL") or "").strip()
        if adzuna_app_id and adzuna_app_key and not actor_input.get("adzuna_app_key"):
            Actor.log.info("Adzuna: using the Actor's own API credentials (environment)")
        if usajobs_api_key and not actor_input.get("usajobs_api_key"):
            Actor.log.info("USAJobs: using the Actor's own API key (environment)")
        reed_api_key = (actor_input.get("reed_api_key") or "").strip()

        if not selected_boards:
            Actor.log.error("No valid boards selected!")
            await Actor.set_status_message("No valid boards selected", is_terminal=True)
            return

        # ── Budget awareness (pay-per-event) ──
        # The platform silently truncates push_data() to whatever the run's
        # "max total charge" still covers, so rows scraped beyond that are pure
        # compute waste and the status message would lie about them. Cap the
        # target at the affordable row count and say so up front.
        budget_rows = _affordable_rows()
        if budget_rows is not None:
            budget_rows -= 1   # the platform ABORTS a run that reaches its limit exactly; stay one row under
        if budget_rows is not None and (unlimited or budget_rows < max_results):
            budget_usd = Actor.get_charging_manager().get_max_total_charge_usd()
            if budget_rows <= 0:
                Actor.log.error(f"Run budget (${budget_usd:.2f} max total charge) cannot pay for a single row after the start fee")
                await Actor.set_status_message(
                    "Run budget too low for any rows: raise 'Maximum cost per run' when starting the Actor", is_terminal=True)
                return
            Actor.log.warning(
                f"Run budget ${budget_usd:.2f} covers {budget_rows} rows (requested {max_results or 'unlimited'}); "
                f"capping this run at {budget_rows}. Raise 'Maximum cost per run' when starting the Actor to get more.")
            max_results, unlimited = budget_rows, False

        Actor.log.info(f"Starting multi-board scrape: {search_terms} in '{location}' (country={country})")
        Actor.log.info(f"Boards: {selected_boards}")
        Actor.log.info(f"Max results: {max_results or 'unlimited'} | Contract type: {job_type} | "
                       f"Job type: {job_type_norm} | Remote only: {remote_only} | "
                       f"Radius: {radius_miles or '-'} mi | Incremental: {incremental_only}")
        await Actor.set_status_message(f"Searching {len(selected_boards)} boards for {search_terms} in '{location}'")

        # ── Per-board budget (boards that will be skipped for lack of a key
        # are excluded from the divisor so working boards aren't starved) ──
        effective_boards = [b for b in selected_boards
                            if not (b == "usajobs" and not usajobs_api_key)
                            and not (b == "adzuna" and not (adzuna_app_id and adzuna_app_key))]
        if len(effective_boards) != len(selected_boards):
            skipped = sorted(set(selected_boards) - set(effective_boards))
            Actor.log.info(f"Budget adjustment: {skipped} will be skipped (no API key) and are excluded from the per-board budget")
        num_terms = len(search_terms)
        divisor_boards = max(1, len(effective_boards))
        max_per_board = 10 ** 6 if unlimited else max(10, max_results // (divisor_boards * max(1, num_terms)))

        # ── Proxy: residential, country-targeted, HTML boards only ──
        html_boards = [b for b in selected_boards if BOARD_REGISTRY[b][1] == "html" and not (b == "reed" and reed_api_key)]
        proxy_config = None
        if html_boards and Actor.is_at_home() and proxy_mode != "none":
            groups = ["RESIDENTIAL"] if proxy_mode == "residential" else None
            try:
                proxy_config = await Actor.create_proxy_configuration(
                    groups=groups, country_code=ADZUNA_COUNTRY_MAP.get(country, "gb").upper())
                Actor.log.info(f"Proxy: {proxy_mode} ({ADZUNA_COUNTRY_MAP.get(country, 'gb').upper()}) for {html_boards}")
            except Exception as e:
                Actor.log.warning(f"Proxy configuration failed, continuing without proxy: {e}")

        # Lazy: nothing is launched until a board is actually blocked over HTTP.
        browser_pool = BrowserPool(proxy_config=proxy_config) if html_boards else None

        api_client = httpx.AsyncClient(headers=make_api_headers(), timeout=httpx.Timeout(30.0, connect=10.0),
                                       follow_redirects=True)
        clients: list[httpx.AsyncClient] = [api_client]
        scrapers: dict[str, object] = {}
        adzuna_country = ADZUNA_COUNTRY_MAP.get(country, "gb")

        for board in selected_boards:
            factory, kind = BOARD_REGISTRY[board]
            extra: dict = {}
            if board == "reed" and reed_api_key:
                kind = "api"  # official API: no proxy, no browser
                extra["api_key"] = reed_api_key
            if kind == "html":
                proxy_url = None
                if proxy_config:
                    proxy_url = await proxy_config.new_url(session_id=f"{board}_{random.randint(1000, 9999)}")
                headers = make_headers(ACCEPT_LANGUAGE.get(country, "en-GB,en;q=0.9"))
                client = httpx.AsyncClient(headers=headers, proxy=proxy_url, timeout=HTML_TIMEOUT, follow_redirects=True)
                clients.append(client)
                extra.update(browser_pool=browser_pool, proxy_url=proxy_url, proxy_config=proxy_config,
                             client_headers=headers)
            else:
                client = api_client
                if board == "adzuna":
                    extra.update(app_id=adzuna_app_id, app_key=adzuna_app_key, country=adzuna_country)
                elif board == "usajobs":
                    extra.update(api_key=usajobs_api_key, email=usajobs_email)
            scraper = factory(client, **extra)
            if kind == "api":
                scraper.stats["mode"] = "api"
            pages = max_pages if unlimited else min(max_pages, math.ceil(max_per_board / scraper.page_size) + 1)
            scraper.max_pages = min(pages, scraper.hard_page_cap) if scraper.hard_page_cap else pages
            scraper.radius_miles = radius_miles
            scraper.country_hint = country
            scrapers[board] = scraper

        pages_needed = math.ceil(max_per_board / 25)
        # Budget per board per term. Partial results survive a timeout, so this
        # bounds cost rather than deciding what the customer gets.
        board_timeout = 1800.0 if unlimited else float(max(180, min(900, 120 + 20 * pages_needed)))
        stats: dict[str, dict] = {}
        t_start = time.perf_counter()

        raw_jobs: list[dict] = []
        all_jobs: list[dict] = []
        rows_pushed = 0
        board_attempts = 0
        board_errors = 0
        dead_boards: set[str] = set()          # circuit breaker for expensive (browser) boards
        unlimited_counts: dict[str, int] = {}  # per-board totals across terms
        seen_source_urls: set[tuple[str, str]] = set()

        def board_limit(name: str) -> int:
            if not unlimited:
                return max_per_board
            return max(0, UNLIMITED_BOARD_CAP - unlimited_counts.get(name, 0))

        def ingest(src: str, jobs: list[dict]) -> None:
            # A job matching two search terms comes back twice from the same
            # board with the same URL; drop those at ingest regardless of the
            # deduplicate toggle (which only controls the cross-board merge).
            fresh = []
            for j in jobs:
                url = j.get("url") or ""
                if url:
                    key = (j.get("source") or src, url)
                    if key in seen_source_urls:
                        continue
                    seen_source_urls.add(key)
                fresh.append(j)
            if len(fresh) < len(jobs):
                Actor.log.info(f"[{src}] {len(jobs) - len(fresh)} duplicate listing(s) already collected earlier in this run")
            if unlimited:
                unlimited_counts[src] = unlimited_counts.get(src, 0) + len(fresh)
            raw_jobs.extend(normalize_job(j) for j in fresh)

        async def collect_term(term: str, limit_override: int | None = None, only: list[str] | None = None) -> None:
            nonlocal board_attempts, board_errors
            names = [n for n in scrapers if n not in dead_boards and (only is None or n in only)]
            if not names:
                return
            budgets = {n: (limit_override if limit_override is not None else board_limit(scrapers[n].source_name)) for n in names}
            names = [n for n in names if budgets[n] > 0]
            board_attempts += len(names)
            results = await asyncio.gather(*[
                run_board(n, scrapers[n], term, location, budgets[n], job_type, salary_min, board_timeout, stats)
                for n in names
            ])
            for n, (jobs, errored) in zip(names, results):
                s = scrapers[n]
                if errored:
                    board_errors += 1
                if (errored or not jobs) and s.stats.get("mode") == "browser":
                    dead_boards.add(n)   # don't pay browser time again for a board that gave nothing
                    Actor.log.warning(f"[{s.source_name}] no results via browser; skipping it for remaining terms")
                Actor.log.info(f"[{s.source_name}] '{term}' -> {len(jobs)} jobs")
                ingest(s.source_name, jobs)

        try:
            # ── Collect raw jobs: every board concurrently, term by term ──
            RAW_CAP = 0 if unlimited else max_results * 3
            for term in search_terms:
                if RAW_CAP and len(raw_jobs) >= RAW_CAP:
                    Actor.log.info(f"Raw buffer cap ({RAW_CAP}) reached; stopping collection early.")
                    break
                Actor.log.info(f"── Search term: '{term}' ──")
                await collect_term(term)

            # ── Top-up: if boards came up short, ask the ones that still have
            # pages for the difference (cheap: HTTP pages). Resumes from the
            # page each board stopped at for the last search term. The target
            # is measured AFTER cross-board dedup (what the customer receives),
            # scaled by the duplicate rate seen so far, up to two rounds.
            for _round in range(2):
                if unlimited:
                    break
                unique_now = pipeline.merged_count(raw_jobs, deduplicate)
                if unique_now >= max_results:
                    break
                live = [n for n, s in scrapers.items()
                        if not s.exhausted and n not in dead_boards and getattr(s, "resumable", True)]
                if not live:
                    break
                deficit = max_results - unique_now
                keep_rate = unique_now / len(raw_jobs) if raw_jobs else 1.0   # share of raw rows that survive dedup
                extra = math.ceil(deficit / max(0.5, keep_rate) * 1.1 / len(live))
                Actor.log.info(f"Top-up {_round + 1}: {unique_now} unique of {len(raw_jobs)} raw (target {max_results}), "
                               f"asking {live} for ~{extra} more each")
                for n in live:
                    s = scrapers[n]
                    s.max_pages = min(max_pages, s.max_pages + math.ceil(extra / s.page_size) + 1)
                    if s.hard_page_cap:
                        s.max_pages = min(s.max_pages, s.hard_page_cap)
                before = len(raw_jobs)
                await collect_term(search_terms[-1], limit_override=extra, only=live)
                if len(raw_jobs) == before:
                    break   # nothing new came back; another round would only burn time

            Actor.log.info(f"Collected {len(raw_jobs)} raw jobs across {len(search_terms)} term(s)")

            # ── Total-outage guard ──
            if not raw_jobs and board_attempts > 0 and board_errors == board_attempts:
                msg = f"All {board_attempts} board attempts failed (network / anti-bot). No data was returned."
                Actor.log.error(msg)
                if hasattr(Actor, "fail"):
                    await Actor.fail(status_message=msg)
                else:
                    await Actor.set_status_message(msg)
                    raise RuntimeError(msg)
                return

            # ── Post-processing pipeline (unchanged from v0.11) ──
            now = datetime.now(timezone.utc)
            opts = {
                "description_format": description_format, "remote_only": remote_only,
                "job_type_norm": job_type_norm, "posted_within_hours": posted_within_hours,
                "salary_min": salary_min,
            }
            for job in raw_jobs:
                pipeline.enrich_job(job, opts, now)

            drop_counts: dict[str, int] = {}
            filtered = []
            for j in raw_jobs:
                reason = pipeline.filter_reason(j, opts)
                if reason is None:
                    filtered.append(j)
                else:
                    drop_counts[reason] = drop_counts.get(reason, 0) + 1
            Actor.log.info(f"{len(filtered)}/{len(raw_jobs)} jobs pass filters (dropped: {drop_counts or 'none'})")
            if raw_jobs and not filtered:
                await Actor.set_status_message(
                    "Every collected job was removed by your filters; try relaxing Remote Only / Job Type / "
                    "Posted Within / Minimum Salary.")

            merged = pipeline.merge_jobs(filtered, do_merge=deduplicate)
            if deduplicate:
                Actor.log.info(f"Merged {len(filtered)} -> {len(merged)} records "
                               f"({len(filtered) - len(merged)} cross-board duplicates collapsed)")
            merged.sort(key=lambda j: j.get("posted_at") or "", reverse=True)
            if not unlimited and len(merged) > max_results:
                merged = merged[:max_results]
            if resolve_apply_url:
                await resolve_apply_urls(merged, api_client)
            for job in merged:
                pipeline.derive_salary_insights(job)

            store = seen_key = None
            prior_seen: list = []
            if incremental_only:
                store = await Actor.open_key_value_store(name="uk-jobs-incremental")
                seen_key = pipeline.incremental_key({
                    "search_terms": search_terms, "location": location,
                    "country": country, "boards": selected_boards,
                })
                prior_seen = await store.get_value(seen_key) or []
                before = len(merged)
                merged = pipeline.filter_unseen(merged, set(prior_seen))
                Actor.log.info(f"Incremental: {len(merged)}/{before} jobs are new since last run")

            all_jobs = [pipeline.finalize(j) for j in merged]
            Actor.log.info(f"Pushing {len(all_jobs)} jobs to dataset...")
            for i in range(0, len(all_jobs), PUSH_BATCH):
                rows_pushed += await push_rows(all_jobs[i:i + PUSH_BATCH])
            charged = _charged_rows()
            if charged is not None and charged < rows_pushed:
                Actor.log.warning(f"Run budget reached: {charged} of {rows_pushed} rows were stored. "
                                  "Raise 'Maximum cost per run' when starting the Actor to get the rest.")
                rows_pushed = charged

            if incremental_only and store is not None:
                new_seen = pipeline.updated_seen_list(prior_seen, all_jobs)
                await store.set_value(seen_key, new_seen)
                Actor.log.info(f"Incremental store '{seen_key}' now holds {len(new_seen)} fingerprints")

            if salary_benchmark:
                benchmarks = compute_salary_benchmarks(all_jobs)
                Actor.log.info(f"Generated {len(benchmarks)} salary benchmarks")
                for i in range(0, len(benchmarks), PUSH_BATCH):
                    await push_rows(benchmarks[i:i + PUSH_BATCH])
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
        source_counts: dict[str, int] = {}
        for job in all_jobs:
            src = job.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1
        summary = {
            "search_terms": search_terms, "location": location, "country": country, "boards": selected_boards,
            "rows_pushed": rows_pushed, "rows_prepared": len(all_jobs), "raw_collected": len(raw_jobs),
            "elapsed_secs": elapsed, "budget_capped": budget_rows is not None and rows_pushed >= budget_rows,
            "browser_launched": bool(browser_pool and browser_pool.launched),
            "per_board": stats, "rows_per_source": source_counts,
        }
        try:
            await Actor.set_value("RUN_STATS", summary)
        except Exception as e:
            Actor.log.debug(f"RUN_STATS not saved: {e}")
        Actor.log.info("══════════ SCRAPE COMPLETE ══════════")
        Actor.log.info(f"rows={rows_pushed} prepared={len(all_jobs)} raw={len(raw_jobs)} elapsed={elapsed}s "
                       f"browser={'yes' if summary['browser_launched'] else 'no'}")
        for e in stats.values():
            Actor.log.info(f"  {e['source']:<22} {e['jobs']:>5} jobs  {e['pages']:>3} pages  {e['mode']:<8} {e['secs']:>6}s  {e['error']}")
        note = " (run budget reached, raise 'Maximum cost per run' for more)" if summary["budget_capped"] else ""
        await Actor.set_status_message(
            f"Done: {rows_pushed} jobs from {sum(1 for e in stats.values() if e['jobs'])}/{len(selected_boards)} boards in {elapsed}s{note}",
            is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
