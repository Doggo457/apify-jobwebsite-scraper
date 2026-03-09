"""
UK Jobs Board Scraper - Multi-Board Aggregator
Scrapes job listings from multiple UK job boards with unified output format.

Supported boards:
  - Reed.co.uk
  - Totaljobs.com
  - CV-Library.co.uk
  - CWJobs.co.uk (IT/Tech)
  - Indeed UK
  - GOV.UK Find a Job
  - Adzuna (via API - requires free API key)
"""

import asyncio
import os
import random

import httpx
from apify import Actor
from playwright.async_api import async_playwright

from .utils import make_headers
from .boards.reed import ReedScraper
from .boards.totaljobs import TotaljobsScraper
from .boards.cvlibrary import CVLibraryScraper
from .boards.cwjobs import CWJobsScraper
from .boards.indeed import IndeedUKScraper
from .boards.findajob import FindAJobScraper
from .boards.adzuna import AdzunaScraper


# Map board names to scraper classes
BOARD_REGISTRY = {
    "reed": ReedScraper,
    "totaljobs": TotaljobsScraper,
    "cvlibrary": CVLibraryScraper,
    "cwjobs": CWJobsScraper,
    "indeed": IndeedUKScraper,
    "findajob": FindAJobScraper,
    # Adzuna handled separately (needs API keys)
}

# Boards that need browser rendering (JS-heavy, anti-bot protection)
BROWSER_BOARDS = {"totaljobs", "cwjobs", "cvlibrary", "indeed"}

# FindAJob uses plain HTTP - gov.uk blocks proxy tunnels
HTTP_ONLY_BOARDS = {"findajob"}

# Stealth browser launch args
STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-http2",  # Fixes ERR_HTTP2_PROTOCOL_ERROR on Cloudflare sites
    "--disable-infobars",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1920,1080",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--metrics-recording-only",
    "--mute-audio",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
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


async def main() -> None:
    async with Actor:
        # ─── Read Input ────────────────────────────────────────────────
        actor_input = await Actor.get_input() or {}

        keyword = actor_input.get("keyword", "software engineer")
        # Custom location overrides the dropdown selection
        location = actor_input.get("custom_location", "").strip() or actor_input.get("location", "London")
        max_results = actor_input.get("max_results", 50)
        salary_min = actor_input.get("salary_min")
        job_type = actor_input.get("job_type", "all")

        # Board selection - defaults to all boards
        selected_boards = actor_input.get("boards", list(BOARD_REGISTRY.keys()) + ["adzuna"])

        # Adzuna API credentials (optional)
        adzuna_app_id = actor_input.get("adzuna_app_id", "")
        adzuna_app_key = actor_input.get("adzuna_app_key", "")

        # Deduplication toggle
        deduplicate = actor_input.get("deduplicate", True)

        # Scraping rounds
        max_rounds = actor_input.get("max_rounds", 2)

        Actor.log.info(f"Starting multi-board scrape: '{keyword}' in '{location}'")
        Actor.log.info(f"Boards: {selected_boards}")
        Actor.log.info(f"Max results: {max_results} | Job type: {job_type}")

        # ─── Calculate per-board limits ────────────────────────────────
        num_boards = len(selected_boards)
        max_per_board = max(10, (max_results * 2) // num_boards) if num_boards > 0 else max_results

        # ─── Check if any selected boards need browser ────────────────
        needs_browser = any(b in BROWSER_BOARDS for b in selected_boards)

        # ─── Set up Apify proxy config (sessions created per-board) ───
        proxy_config = None
        is_on_apify = os.environ.get("APIFY_IS_AT_HOME", "0") == "1"
        if is_on_apify and needs_browser:
            try:
                proxy_config = await Actor.create_proxy_configuration(
                    groups=["RESIDENTIAL"],
                    country_code="GB",
                )
                Actor.log.info("Created RESIDENTIAL GB proxy configuration")
            except Exception as e:
                Actor.log.warning(f"Failed to create residential proxy, trying datacenter: {e}")
                try:
                    proxy_config = await Actor.create_proxy_configuration()
                    Actor.log.info("Created datacenter proxy configuration")
                except Exception as e2:
                    Actor.log.warning(f"Failed to create any proxy: {e2}")

        # ─── Create HTTP client ───────────────────────────────────────
        headers = make_headers()

        async with httpx.AsyncClient(
            headers=headers,
            timeout=60.0,
        ) as client:

            # ─── Launch browser if needed ─────────────────────────────
            browser = None
            pw = None
            if needs_browser:
                Actor.log.info("Launching stealth Playwright browser...")
                pw = await async_playwright().start()

                browser = await pw.chromium.launch(
                    headless=True,
                    args=STEALTH_ARGS,
                )
                Actor.log.info("Browser launched successfully")

            try:
                # ─── Create scrapers ──────────────────────────────────
                scrapers = {}
                for board_name in selected_boards:
                    if board_name == "adzuna":
                        scrapers[board_name] = AdzunaScraper(
                            client,
                            delay=0.5,
                            app_id=adzuna_app_id,
                            app_key=adzuna_app_key,
                        )
                    elif board_name in BOARD_REGISTRY:
                        if board_name in BROWSER_BOARDS and browser:
                            proxy_url = None
                            if proxy_config:
                                session_id = f"uk_jobs_{board_name}_{random.randint(1000, 9999)}"
                                proxy_url = await proxy_config.new_url(session_id=session_id)
                                Actor.log.info(f"[{board_name}] Using proxy session: {session_id}")
                            scrapers[board_name] = BOARD_REGISTRY[board_name](
                                client, delay=1.5, browser=browser, proxy_url=proxy_url, proxy_config=proxy_config
                            )
                        else:
                            scrapers[board_name] = BOARD_REGISTRY[board_name](client, delay=1.5)
                    else:
                        Actor.log.warning(f"Unknown board: {board_name}, skipping.")

                # ─── Multi-round scraping ─────────────────────────────
                all_jobs = []
                exhausted_boards = set()  # Boards that returned 0 results
                round_num = 0

                while len(all_jobs) < max_results and round_num < max_rounds:
                    round_num += 1
                    still_needed = max_results - len(all_jobs)
                    active_boards = [b for b in scrapers if b not in exhausted_boards]

                    if not active_boards:
                        Actor.log.info("All boards exhausted, stopping.")
                        break

                    per_board = max(10, (still_needed * 2) // len(active_boards))
                    Actor.log.info(f"── Round {round_num}: need {still_needed} more, asking {len(active_boards)} boards for {per_board} each ──")

                    for board_name in active_boards:
                        if len(all_jobs) >= max_results:
                            break

                        # Short delay between boards
                        if all_jobs and round_num == 1:
                            await asyncio.sleep(random.uniform(1.0, 2.0))

                        scraper = scrapers[board_name]
                        Actor.log.info(f"═══ Starting {scraper.source_name} (round {round_num}) ═══")
                        jobs = await run_board(
                            scraper, keyword, location, per_board, job_type, salary_min
                        )

                        if not jobs:
                            exhausted_boards.add(board_name)
                            Actor.log.info(f"═══ {scraper.source_name} exhausted (0 jobs) ═══")
                        else:
                            all_jobs.extend(jobs)
                            Actor.log.info(f"═══ {scraper.source_name} returned {len(jobs)} jobs (total: {len(all_jobs)}) ═══")

                    # Deduplicate after each round
                    if deduplicate:
                        before_count = len(all_jobs)
                        all_jobs = deduplicate_jobs(all_jobs)
                        removed = before_count - len(all_jobs)
                        if removed > 0:
                            Actor.log.info(f"Deduplication removed {removed} duplicates (total: {len(all_jobs)})")

                # ─── Trim to max results ──────────────────────────────
                all_jobs = all_jobs[:max_results]

                # ─── Push results ─────────────────────────────────────
                for job in all_jobs:
                    await Actor.push_data(job)

            finally:
                # ─── Cleanup browser ──────────────────────────────────
                if browser:
                    await browser.close()
                if pw:
                    await pw.stop()

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
