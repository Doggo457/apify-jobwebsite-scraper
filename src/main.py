"""
International Jobs Board Scraper - Multi-Board Aggregator
Scrapes job listings from UK, US, EU and remote job boards with unified output.

Supported boards:
  UK:     Reed, Totaljobs, CV-Library, CWJobs, Indeed UK, GOV.UK Find a Job
  US:     USAJobs, Indeed US
  EU:     Indeed DE/FR/NL, Arbeitnow
  Global: Adzuna (multi-country API), RemoteOK
"""

import asyncio
import os
import random
import re
import statistics

import httpx
from apify import Actor
from playwright.async_api import async_playwright

from .utils import make_headers
from .boards.reed import ReedScraper
from .boards.totaljobs import TotaljobsScraper
from .boards.cvlibrary import CVLibraryScraper
from .boards.cwjobs import CWJobsScraper
from .boards.indeed import IndeedUKScraper, IndeedScraper
from .boards.findajob import FindAJobScraper
from .boards.adzuna import AdzunaScraper
from .boards.usajobs import USAJobsScraper
from .boards.remoteok import RemoteOKScraper
from .boards.arbeitnow import ArbeitnowScraper


# Map board names to scraper classes
BOARD_REGISTRY = {
    # UK boards
    "reed": ReedScraper,
    "totaljobs": TotaljobsScraper,
    "cvlibrary": CVLibraryScraper,
    "cwjobs": CWJobsScraper,
    "indeed": IndeedUKScraper,
    "findajob": FindAJobScraper,
    # US boards
    "usajobs": USAJobsScraper,
    "indeed_us": lambda client, **kw: IndeedScraper(client, base_url="https://www.indeed.com", source="indeed.com", **kw),
    # EU boards
    "indeed_de": lambda client, **kw: IndeedScraper(client, base_url="https://de.indeed.com", source="indeed.de", **kw),
    "indeed_fr": lambda client, **kw: IndeedScraper(client, base_url="https://fr.indeed.com", source="indeed.fr", **kw),
    "indeed_nl": lambda client, **kw: IndeedScraper(client, base_url="https://nl.indeed.com", source="indeed.nl", **kw),
    "indeed_au": lambda client, **kw: IndeedScraper(client, base_url="https://au.indeed.com", source="indeed.au", **kw),
    # Remote / global
    "remoteok": RemoteOKScraper,
    "arbeitnow": ArbeitnowScraper,
    # Adzuna handled separately (needs API keys + country config)
}

# Boards that REQUIRE Playwright browser rendering (JS-heavy, anti-bot)
BROWSER_BOARDS = {"totaljobs", "cwjobs", "cvlibrary", "indeed", "reed",
                  "indeed_us", "indeed_de", "indeed_fr", "indeed_nl", "indeed_au"}

# HTTP-only boards (no browser needed, or gov.uk blocks proxies)
HTTP_ONLY_BOARDS = {"findajob", "usajobs", "remoteok", "arbeitnow", "adzuna"}

# Default boards per country
COUNTRY_DEFAULTS = {
    "uk": ["reed", "totaljobs", "cvlibrary", "cwjobs", "indeed", "findajob", "adzuna"],
    "us": ["usajobs", "indeed_us", "adzuna", "remoteok"],
    "de": ["indeed_de", "adzuna", "arbeitnow"],
    "fr": ["indeed_fr", "adzuna"],
    "nl": ["indeed_nl", "adzuna"],
    "au": ["indeed_au", "adzuna"],
    "remote": ["remoteok", "arbeitnow", "adzuna"],
}

# Adzuna country code mapping
ADZUNA_COUNTRY_MAP = {
    "uk": "gb", "us": "us", "de": "de", "fr": "fr",
    "nl": "nl", "au": "au", "remote": "gb",
}

# Playwright browser launch args for stealth
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
    "--ignore-certificate-errors",
    "--window-size=1920,1080",
]


async def run_board(scraper, keyword, location, max_per_board, job_type, salary_min) -> list[dict]:
    """Run a single board scraper with error handling."""
    try:
        return await scraper.search(
            keyword=keyword,
            location=location,
            max_results=max_per_board,
            job_type=job_type,
            salary_min=salary_min,
        )
    except Exception as e:
        Actor.log.error(f"[{scraper.source_name}] Scraper failed: {e}")
        return []


def categorize_job(title: str) -> str:
    """Infer a job category from the title using keyword matching.

    Returns one of a fixed set of categories, or "Other" if no match.
    Checked in order — first match wins, so more specific patterns come first.
    """
    if not title:
        return "Other"
    t = title.lower()

    # ── Software / IT ──
    if any(k in t for k in ("software eng", "software dev", "full stack", "fullstack",
                            "full-stack", "frontend", "front-end", "front end",
                            "backend", "back-end", "back end", "web dev",
                            "mobile dev", "ios dev", "android dev", "flutter",
                            "react", "angular", "vue.js", "node.js", "python dev",
                            "java dev", ".net dev", "c# dev", "c++ dev", "rust dev",
                            "golang", "ruby dev", "php dev", "programmer", "coder",
                            "software architect", "principal engineer",
                            "lead developer", "lead engineer", "tech lead")):
        return "Software Development"

    if any(k in t for k in ("data scien", "data analy", "data eng", "machine learn",
                            "ml eng", "ai eng", "artificial intelligence",
                            "deep learn", "nlp", "computer vision",
                            "business intellig", "bi analyst", "bi developer",
                            "analytics eng", "data architect")):
        return "Data & Analytics"

    if any(k in t for k in ("devops", "sre", "site reliab", "platform eng",
                            "cloud eng", "cloud arch", "infrastructure",
                            "kubernetes", "docker", "terraform", "aws eng",
                            "azure eng", "gcp eng", "systems eng", "linux eng",
                            "network eng", "devsecops")):
        return "DevOps & Infrastructure"

    if any(k in t for k in ("cyber", "security eng", "security analy",
                            "penetration", "pen test", "infosec",
                            "information security", "soc analyst",
                            "security architect", "security consult")):
        return "Cybersecurity"

    if any(k in t for k in ("qa ", "qa eng", "test eng", "tester", "sdet",
                            "automation eng", "quality assurance", "test analy",
                            "test lead", "test manager")):
        return "QA & Testing"

    if any(k in t for k in ("product manager", "product owner", "product lead",
                            "head of product", "vp product", "cpo",
                            "product director")):
        return "Product Management"

    if any(k in t for k in ("project manager", "programme manager",
                            "program manager", "delivery manager",
                            "scrum master", "agile coach", "release manager",
                            "pmo", "project coordinator", "delivery lead")):
        return "Project & Delivery Management"

    if any(k in t for k in ("ux ", "ui ", "ux/ui", "ui/ux", "user experience",
                            "user interface", "product design", "interaction design",
                            "visual design", "graphic design", "web design",
                            "design lead", "creative director")):
        return "Design & UX"

    if any(k in t for k in ("it manager", "it director", "cto", "cio",
                            "head of engineering", "head of it",
                            "vp engineering", "engineering manager",
                            "development manager", "it service",
                            "service desk", "helpdesk", "help desk",
                            "it support", "desktop support", "it admin",
                            "systems admin", "it operations")):
        return "IT Management & Support"

    if any(k in t for k in ("dba", "database admin", "database eng",
                            "database dev", "sql dev", "etl dev",
                            "data warehouse", "database architect")):
        return "Database & BI"

    if any(k in t for k in ("solution architect", "enterprise architect",
                            "technical architect", "integration architect",
                            "it consult", "technology consult",
                            "technical consult", "sap consult",
                            "salesforce", "dynamics 365", "erp consult",
                            "crm consult", "business analyst")):
        return "IT Consulting & Architecture"

    # ── Non-IT categories ──
    if any(k in t for k in ("accountant", "accounting", "finance manager",
                            "financial analy", "financial controller",
                            "bookkeeper", "payroll", "tax ", "audit",
                            "treasury", "credit analy", "fund manager",
                            "investment analy", "actuary", "cfo")):
        return "Finance & Accounting"

    if any(k in t for k in ("marketing", "seo ", "ppc ", "content writer",
                            "copywriter", "social media", "digital market",
                            "brand manager", "communications", "pr manager",
                            "public relations", "cmo")):
        return "Marketing & Communications"

    if any(k in t for k in ("sales manager", "sales exec", "sales rep",
                            "account manager", "account exec",
                            "business develop", "bdr ", "sdr ",
                            "commercial manager", "sales director")):
        return "Sales & Business Development"

    if any(k in t for k in ("hr ", "human resource", "recruiter", "recruitment",
                            "talent acqui", "people manager", "people partner",
                            "learning and dev", "l&d ", "training manager",
                            "compensation", "reward", "hrbp")):
        return "HR & Recruitment"

    if any(k in t for k in ("nurse", "doctor", "clinical", "pharmacist",
                            "physiotherapist", "occupational therap",
                            "healthcare", "medical", "gp ", "surgeon",
                            "dental", "radiographer", "midwife",
                            "care assistant", "support worker")):
        return "Healthcare & Medical"

    if any(k in t for k in ("teacher", "lecturer", "professor", "tutor",
                            "teaching assistant", "education", "headteacher",
                            "head of department", "send ", "pastoral")):
        return "Education & Training"

    if any(k in t for k in ("mechanical eng", "electrical eng", "civil eng",
                            "structural eng", "chemical eng", "process eng",
                            "manufacturing eng", "maintenance eng",
                            "building services", "quantity surveyor",
                            "site manager", "construction manager")):
        return "Engineering"

    if any(k in t for k in ("solicitor", "barrister", "paralegal", "legal counsel",
                            "legal advisor", "lawyer", "conveyancer",
                            "compliance officer", "regulatory")):
        return "Legal & Compliance"

    if any(k in t for k in ("supply chain", "logistics", "procurement",
                            "warehouse", "buyer", "purchasing",
                            "operations manager", "operations director",
                            "facilities", "fleet manager")):
        return "Operations & Logistics"

    if any(k in t for k in ("customer service", "customer support",
                            "call centre", "call center", "contact centre",
                            "client service", "customer success")):
        return "Customer Service"

    if any(k in t for k in ("admin", "office manager", "receptionist",
                            "personal assistant", "executive assistant",
                            "secretary", "office coordinator")):
        return "Administration"

    return "Other"


def normalize_job(job: dict) -> dict:
    """Ensure all job dicts have every expected field (no undefined in output)."""
    defaults = {
        "title": "",
        "company": "",
        "location": "",
        "salary_raw": "",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "GBP",
        "salary_period": "",
        "snippet": "",

        "employment_type": "",
        "date_posted": "",
        "valid_through": "",
        "url": "",
        "job_id": "",
        "source": "",
        "category": "",
    }
    normalized = {**defaults, **{k: v for k, v in job.items() if v is not None and v != ""}}
    normalized.pop("full_description", None)
    # Ensure None values don't override defaults for string fields
    for key in ["title", "company", "location", "salary_raw", "snippet",
                "employment_type", "date_posted",
                "valid_through", "url", "job_id", "source", "category",
                "salary_period", "salary_currency"]:
        if normalized.get(key) is None:
            normalized[key] = defaults[key]
    # Infer category from title when not provided by the board
    if not normalized["category"]:
        normalized["category"] = categorize_job(normalized["title"])
    return normalized


def deduplicate_jobs(jobs: list[dict]) -> list[dict]:
    """
    Remove duplicate jobs across boards.
    Uses a combination of normalised title + company + location.
    """
    seen = set()
    unique = []

    for job in jobs:
        # Create a fingerprint
        title = job.get("title", "").lower().strip()
        company = job.get("company", "").lower().strip()
        location = job.get("location", "").lower().strip()

        fingerprint = f"{title}|{company}|{location}"

        if fingerprint not in seen:
            seen.add(fingerprint)
            unique.append(job)

    return unique


def compute_salary_benchmarks(jobs: list[dict]) -> list[dict]:
    """Compute salary benchmarks grouped by title keyword and location."""
    from collections import defaultdict

    # Group salaries by normalised title + location
    buckets = defaultdict(list)
    for job in jobs:
        sal_min = job.get("salary_min")
        sal_max = job.get("salary_max")
        period = job.get("salary_period", "annum")

        # Only use annual salaries for benchmarking
        if not sal_min and not sal_max:
            continue

        # Rough annualisation
        mid = ((sal_min or sal_max) + (sal_max or sal_min)) / 2
        if period == "day":
            mid *= 220
        elif period == "hour":
            mid *= 1760
        elif period == "month":
            mid *= 12
        elif period == "week":
            mid *= 52

        if mid < 5000 or mid > 500000:
            continue  # filter outliers

        title = job.get("title", "").lower().strip()
        location = job.get("location", "").lower().strip()
        # Use first meaningful word of location
        loc_key = location.split(",")[0].strip() if location else "unknown"
        buckets[(title, loc_key)].append(mid)

    benchmarks = []
    for (title, loc), salaries in buckets.items():
        if len(salaries) < 2:
            continue
        salaries.sort()
        benchmarks.append({
            "benchmark_title": title,
            "benchmark_location": loc,
            "count": len(salaries),
            "salary_mean": round(statistics.mean(salaries)),
            "salary_median": round(statistics.median(salaries)),
            "salary_p25": round(salaries[len(salaries) // 4]),
            "salary_p75": round(salaries[(len(salaries) * 3) // 4]),
            "salary_min": round(min(salaries)),
            "salary_max": round(max(salaries)),
            "_type": "salary_benchmark",
        })

    return sorted(benchmarks, key=lambda b: b["count"], reverse=True)


async def main() -> None:
    async with Actor:
        # ─── Read Input ────────────────────────────────────────────────
        actor_input = await Actor.get_input() or {}

        # Custom fields override presets when filled in
        keyword = actor_input.get("custom_keyword") or actor_input.get("keyword", "software engineer")
        if keyword == "__custom__":
            keyword = actor_input.get("custom_keyword", "software engineer")
        location = actor_input.get("custom_location") or actor_input.get("location", "London")
        if location == "__custom__":
            location = actor_input.get("custom_location", "London")

        unlimited = actor_input.get("unlimited", False)
        max_results = 0 if unlimited else max(actor_input.get("max_results", 100), 100)
        salary_min = actor_input.get("salary_min") or None
        job_type = actor_input.get("job_type", "all")
        fetch_details = False  # Detail pages cost extra compute per job
        country = actor_input.get("country", "uk")
        salary_benchmark = actor_input.get("salary_benchmark", False)

        # Board selection - use country defaults if empty/not specified
        selected_boards = actor_input.get("boards") or COUNTRY_DEFAULTS.get(country, COUNTRY_DEFAULTS["uk"])

        # Adzuna API credentials (optional)
        adzuna_app_id = actor_input.get("adzuna_app_id", "")
        adzuna_app_key = actor_input.get("adzuna_app_key", "")

        # Deduplication toggle
        deduplicate = actor_input.get("deduplicate", True)

        Actor.log.info(f"Starting multi-board scrape: '{keyword}' in '{location}' (country={country})")
        Actor.log.info(f"Boards: {selected_boards}")
        Actor.log.info(f"Max results: {max_results or 'unlimited'} | Job type: {job_type} | Details: {fetch_details}")

        # ─── Calculate per-board limits ────────────────────────────────
        # Divide the target evenly across boards.
        num_boards = len(selected_boards)
        if max_results == 0:
            max_per_board = 10000
        elif num_boards > 0:
            max_per_board = max_results // num_boards
        else:
            max_per_board = max_results

        # ─── Set up Apify proxy config (before httpx client so we can pass proxy) ──
        headers = make_headers()
        proxy_config = None
        http_proxy_url = None
        is_on_apify = os.environ.get("APIFY_IS_AT_HOME", "0") == "1"
        need_browser = any(b in BROWSER_BOARDS for b in selected_boards)

        if is_on_apify:
            proxy_country = ADZUNA_COUNTRY_MAP.get(country, "GB").upper()
            try:
                proxy_config = await Actor.create_proxy_configuration(
                    groups=["RESIDENTIAL"],
                    country_code=proxy_country,
                )
                http_proxy_url = await proxy_config.new_url(
                    session_id=f"http_main_{random.randint(1000, 9999)}"
                )
                Actor.log.info(f"Using Apify residential proxy ({proxy_country}) for HTTP + browser")
            except Exception as e:
                Actor.log.warning(f"Proxy config failed: {e}")

        # ─── Create HTTP client (WITH proxy on Apify for detail page fetching) ──
        async with httpx.AsyncClient(
            headers=headers,
            timeout=30.0,
            proxy=http_proxy_url,
        ) as client:

            # ─── Launch Playwright browser if needed ──────────────────
            browser = None
            playwright_instance = None

            if need_browser:
                Actor.log.info("Launching Playwright browser for JS-heavy boards...")
                try:
                    playwright_instance = await async_playwright().start()

                    # Chromium requires browser-level proxy for context-level proxy to work
                    # Use a placeholder so per-context proxy overrides are allowed
                    launch_kwargs = {
                        "headless": True,
                        "args": STEALTH_ARGS,
                    }
                    if proxy_config:
                        # Get an initial proxy URL to set at browser level
                        initial_proxy_url = await proxy_config.new_url(
                            session_id=f"browser_init_{random.randint(1000, 9999)}"
                        )
                        from urllib.parse import urlparse
                        parsed = urlparse(initial_proxy_url)
                        launch_kwargs["proxy"] = {
                            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                            "username": parsed.username or "",
                            "password": parsed.password or "",
                        }
                        Actor.log.info(f"Browser launched with proxy: {parsed.hostname}:{parsed.port}")

                    browser = await playwright_instance.chromium.launch(**launch_kwargs)
                    Actor.log.info("Playwright browser launched successfully")
                except Exception as e:
                    Actor.log.error(f"Failed to launch Playwright: {e}")
                    Actor.log.warning("Browser boards will fall back to httpx (may return fewer results)")

            try:
                # ─── Initialise scrapers (split browser vs API) ───────
                browser_scrapers = []
                api_scrapers = []
                adzuna_country = ADZUNA_COUNTRY_MAP.get(country, "gb")

                for board_name in selected_boards:
                    if board_name == "adzuna":
                        scraper = AdzunaScraper(
                            client,
                            delay=0.5,
                            app_id=adzuna_app_id,
                            app_key=adzuna_app_key,
                            country=adzuna_country,
                        )
                        api_scrapers.append(scraper)

                    elif board_name in BOARD_REGISTRY:
                        factory = BOARD_REGISTRY[board_name]

                        if board_name in BROWSER_BOARDS and browser and proxy_config:
                            # Sanitize board name for session ID (only [\w._~] allowed)
                            safe_name = re.sub(r"[^a-zA-Z0-9._~]", "_", board_name)
                            proxy_url = await proxy_config.new_url(
                                session_id=f"jobs_{safe_name}_{random.randint(1000, 9999)}"
                            )
                            if callable(factory) and not isinstance(factory, type):
                                scraper = factory(client, delay=1.5, browser=browser,
                                                  proxy_url=proxy_url, proxy_config=proxy_config)
                            else:
                                scraper = factory(client, delay=1.5, browser=browser,
                                                  proxy_url=proxy_url, proxy_config=proxy_config)
                            browser_scrapers.append(scraper)
                        elif board_name in BROWSER_BOARDS and browser:
                            if callable(factory) and not isinstance(factory, type):
                                scraper = factory(client, delay=1.5, browser=browser)
                            else:
                                scraper = factory(client, delay=1.5, browser=browser)
                            browser_scrapers.append(scraper)
                        else:
                            # HTTP-only boards — still pass browser for detail page fallback
                            extra = {}
                            if browser:
                                extra["browser"] = browser
                            if proxy_config:
                                safe_name = re.sub(r"[^a-zA-Z0-9._~]", "_", board_name)
                                purl = await proxy_config.new_url(
                                    session_id=f"jobs_{safe_name}_{random.randint(1000, 9999)}"
                                )
                                extra["proxy_url"] = purl
                                extra["proxy_config"] = proxy_config
                            if callable(factory) and not isinstance(factory, type):
                                scraper = factory(client, delay=0.5, **extra)
                            else:
                                scraper = factory(client, delay=0.5, **extra)
                            api_scrapers.append(scraper)

                        # Pass fetch_details to scrapers that support it
                        if hasattr(scraper, "fetch_details"):
                            scraper.fetch_details = fetch_details
                    else:
                        Actor.log.warning(f"Unknown board: {board_name}, skipping.")

                all_scrapers = api_scrapers + browser_scrapers
                if not all_scrapers:
                    Actor.log.error("No valid boards selected!")
                    return

                all_jobs = []
                dedup_seen = set()

                def dedup_and_normalize(jobs: list[dict]) -> list[dict]:
                    """Normalize and deduplicate a batch of jobs."""
                    accepted = []
                    for job in jobs:
                        normalized = normalize_job(job)
                        if deduplicate:
                            fp = (f"{normalized.get('title','').lower().strip()}|"
                                  f"{normalized.get('company','').lower().strip()}|"
                                  f"{normalized.get('location','').lower().strip()}")
                            if fp in dedup_seen:
                                continue
                            dedup_seen.add(fp)
                        accepted.append(normalized)
                    return accepted

                if unlimited:
                    # ─── UNLIMITED MODE ───────────────────────────────
                    # Each board gets 100 per pass. Cycle through all
                    # boards, pushing after each one so results survive
                    # if the user aborts mid-run.
                    BATCH = 100
                    Actor.log.info("Unlimited mode: cycling boards in rounds of 100...")

                    # Run all scrapers once with 100 each, push as we go
                    # API scrapers in parallel first
                    if api_scrapers:
                        Actor.log.info(f"Running {len(api_scrapers)} API scrapers in parallel...")
                        api_results = await asyncio.gather(
                            *[run_board(s, keyword, location, BATCH, job_type, salary_min)
                              for s in api_scrapers],
                        )
                        for scraper, jobs in zip(api_scrapers, api_results):
                            accepted = dedup_and_normalize(jobs)
                            Actor.log.info(f"[{scraper.source_name}] {len(jobs)} scraped, {len(accepted)} pushed")
                            for job in accepted:
                                await Actor.push_data(job)
                            all_jobs.extend(accepted)

                    # Browser scrapers sequentially
                    if browser_scrapers:
                        for scraper in browser_scrapers:
                            Actor.log.info(f"── Starting {scraper.source_name} ──")
                            jobs = await run_board(scraper, keyword, location, BATCH, job_type, salary_min)
                            accepted = dedup_and_normalize(jobs)
                            Actor.log.info(f"[{scraper.source_name}] {len(jobs)} scraped, {len(accepted)} pushed")
                            for job in accepted:
                                await Actor.push_data(job)
                            all_jobs.extend(accepted)

                else:
                    # ─── LIMITED MODE ─────────────────────────────────
                    # Split the target evenly across boards. Collect all,
                    # then deduplicate, interleave, and push.
                    board_results = {}

                    if api_scrapers:
                        Actor.log.info(f"Running {len(api_scrapers)} API scrapers in parallel...")
                        api_results = await asyncio.gather(
                            *[run_board(s, keyword, location, max_per_board, job_type, salary_min)
                              for s in api_scrapers],
                        )
                        for scraper, jobs in zip(api_scrapers, api_results):
                            Actor.log.info(f"═══ {scraper.source_name} returned {len(jobs)} jobs ═══")
                            board_results[scraper.source_name] = jobs

                    if browser_scrapers:
                        Actor.log.info(f"Running {len(browser_scrapers)} browser scrapers sequentially...")
                        for scraper in browser_scrapers:
                            Actor.log.info(f"── Starting {scraper.source_name} ──")
                            jobs = await run_board(scraper, keyword, location, max_per_board, job_type, salary_min)
                            Actor.log.info(f"═══ {scraper.source_name} returned {len(jobs)} jobs ═══")
                            board_results[scraper.source_name] = jobs

                    # Normalize + dedup per board
                    for source in board_results:
                        board_results[source] = dedup_and_normalize(board_results[source])
                        Actor.log.info(f"[{source}] {len(board_results[source])} after dedup")

                    # Round-robin interleave for fair representation
                    board_iters = {src: iter(jobs) for src, jobs in board_results.items() if jobs}
                    while board_iters:
                        exhausted = []
                        for src in list(board_iters):
                            job = next(board_iters[src], None)
                            if job is None:
                                exhausted.append(src)
                            else:
                                all_jobs.append(job)
                        for src in exhausted:
                            del board_iters[src]

                    # Push all
                    Actor.log.info(f"Pushing {len(all_jobs)} jobs to dataset...")
                    for job in all_jobs:
                        await Actor.push_data(job)

                # ─── Salary benchmarks ────────────────────────────────
                if salary_benchmark:
                    benchmarks = compute_salary_benchmarks(all_jobs)
                    Actor.log.info(f"Generated {len(benchmarks)} salary benchmarks")
                    for b in benchmarks:
                        await Actor.push_data(b)

            finally:
                # ─── Clean up browser ─────────────────────────────────
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                if playwright_instance:
                    try:
                        await playwright_instance.stop()
                    except Exception:
                        pass

        # ─── Summary ──────────────────────────────────────────────────
        source_counts = {}
        for job in all_jobs:
            src = job.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1

        Actor.log.info(f"╔══════════════════════════════════════╗")
        Actor.log.info(f"║  SCRAPE COMPLETE                     ║")
        Actor.log.info(f"║  Total jobs: {len(all_jobs):<23}║")
        for src, count in sorted(source_counts.items()):
            Actor.log.info(f"║  {src}: {count:<26}║")
        Actor.log.info(f"╚══════════════════════════════════════╝")


if __name__ == "__main__":
    asyncio.run(main())
