"""
Shared utilities for UK Jobs Board scrapers.
Common salary parsing, data normalisation, and base scraper class.
"""

import json
import re
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Optional

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


class BaseScraper(ABC):
    """Base class for all job board scrapers."""

    def __init__(self, client: httpx.AsyncClient, delay: float = 1.5):
        self.client = client
        self.delay = delay

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

    async def _polite_delay(self):
        """Wait between requests to be respectful."""
        await asyncio.sleep(self.delay)

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
