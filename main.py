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

import httpx
from apify import Actor

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
        location = actor_input.get("location", "London")
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

        Actor.log.info(f"Starting multi-board scrape: '{keyword}' in '{location}'")
        Actor.log.info(f"Boards: {selected_boards}")
        Actor.log.info(f"Max results: {max_results} | Job type: {job_type}")

        # ─── Calculate per-board limits ────────────────────────────────
        num_boards = len(selected_boards)
        # Give each board an equal share, with some extra to account for dedup
        max_per_board = max(10, (max_results * 2) // num_boards) if num_boards > 0 else max_results

        # ─── Create HTTP client with Apify proxy ──────────────────────
        headers = make_headers()

        # Use Apify proxy when running on the platform
        proxy_url = None
        is_on_apify = os.environ.get("APIFY_IS_AT_HOME", "0") == "1"

        if is_on_apify:
            proxy_config = await Actor.create_proxy_configuration(
                groups=["RESIDENTIAL"],
                country_code="GB",
            )
            if proxy_config:
                proxy_url = await proxy_config.new_url()
                Actor.log.info(f"Using Apify residential proxy (GB)")

        async with httpx.AsyncClient(
            headers=headers,
            timeout=60.0,
            proxy=proxy_url,
        ) as client:
            # ─── Initialise scrapers ───────────────────────────────────
            scrapers = []

            for board_name in selected_boards:
                if board_name == "adzuna":
                    scraper = AdzunaScraper(
                        client,
                        delay=0.5,
                        app_id=adzuna_app_id,
                        app_key=adzuna_app_key,
                    )
                    scrapers.append(scraper)
                elif board_name in BOARD_REGISTRY:
                    scraper = BOARD_REGISTRY[board_name](client, delay=1.5)
                    scrapers.append(scraper)
                else:
                    Actor.log.warning(f"Unknown board: {board_name}, skipping.")

            if not scrapers:
                Actor.log.error("No valid boards selected!")
                return

            # ─── Run all scrapers ──────────────────────────────────────
            # Run scrapers sequentially to be respectful with rate limits
            # (could be parallelised with asyncio.gather for speed)
            all_jobs = []

            for scraper in scrapers:
                Actor.log.info(f"═══ Starting {scraper.source_name} ═══")
                jobs = await run_board(
                    scraper, keyword, location, max_per_board, job_type, salary_min
                )
                all_jobs.extend(jobs)
                Actor.log.info(f"═══ {scraper.source_name} returned {len(jobs)} jobs ═══")

            # ─── Deduplicate ───────────────────────────────────────────
            if deduplicate:
                before_count = len(all_jobs)
                all_jobs = deduplicate_jobs(all_jobs)
                removed = before_count - len(all_jobs)
                if removed > 0:
                    Actor.log.info(f"Deduplication removed {removed} duplicates")

            # ─── Trim to max results ───────────────────────────────────
            all_jobs = all_jobs[:max_results]

            # ─── Push results ──────────────────────────────────────────
            for job in all_jobs:
                await Actor.push_data(job)

        # ─── Summary ──────────────────────────────────────────────────
        # Count per source
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
