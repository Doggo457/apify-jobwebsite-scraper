"""Indeed scraper (UK + international variants).

Indeed sits behind an aggressive Cloudflare challenge; even residential
proxies plus a stealth browser get through only some of the time. Treat it
as best-effort and the most expensive board in the set.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urljoin

from apify import Actor
from bs4 import BeautifulSoup

from ..utils import (BaseScraper, apply_salary, clean_text, detect_employment_type,
                     detect_work_mode, parse_salary, strip_tracking)

JOB_TYPE_PARAM = {"permanent": "permanent", "temporary": "temporary", "contract": "contract", "part-time": "parttime"}
# NOTE: BeautifulSoup is only used for the HTML-card fallback; the mosaic JSON path needs no parsing.

_MOSAIC_MARK = re.compile(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*')
_JOBCARDS_MARK = re.compile(r'"jobCards"\s*:\s*')
_DECODER = json.JSONDecoder()


def _json_after(html: str, marker: re.Pattern) -> dict | list | None:
    """Decode exactly one JSON value that starts right after `marker`.

    raw_decode handles nested brackets and strings properly, which a
    non-greedy regex cannot.
    """
    m = marker.search(html)
    if not m:
        return None
    try:
        value, _ = _DECODER.raw_decode(html, m.end())
        return value
    except (json.JSONDecodeError, ValueError):
        return None


class IndeedScraper(BaseScraper):
    card_selector = "div.job_seen_beacon, div[data-jk], a[data-jk]"
    page_size = 15
    max_pages = 20

    def __init__(self, client, delay: float = 1.5, base_url: str = "https://uk.indeed.com",
                 source: str = "indeed.co.uk", currency: str = "GBP", **kwargs):
        super().__init__(client, delay, **kwargs)
        self.base_url = base_url
        self._source = source
        self.default_currency = currency

    @property
    def source_name(self) -> str:
        return self._source

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        params = [f"q={quote_plus(keyword)}", f"l={quote_plus(location)}", "sort=date"]
        if page > 1:
            params.append(f"start={(page - 1) * 10}")
        if job_type in JOB_TYPE_PARAM:
            params.append(f"jt={JOB_TYPE_PARAM[job_type]}")
        if salary_min:
            params.append(f"salary={int(salary_min)}")
        return f"{self.base_url}/jobs?" + "&".join(params)

    def _parse_search(self, html: str, soup: BeautifulSoup) -> tuple[list[dict], bool]:
        jobs = self._extract_mosaic(html)
        if not jobs:
            jobs = self._extract_jsonld_jobs(soup)
        if not jobs:
            jobs = self._parse_cards(soup)
        has_next = bool(soup.select_one('a[aria-label="Next Page"], a[data-testid="pagination-page-next"]')) or len(jobs) >= 10
        return jobs, has_next and bool(jobs)

    def _extract_mosaic(self, html: str) -> list[dict]:
        data = _json_after(html, _MOSAIC_MARK)
        if data is None:
            data = _json_after(html, _JOBCARDS_MARK)
        if not data:
            return []
        results = data if isinstance(data, list) else (
            ((data.get("metaData") or {}).get("mosaicProviderJobCardsModel") or {}).get("results") or [])
        jobs = []
        for it in results:
            if not isinstance(it, dict):
                continue
            title = clean_text(it.get("title") or it.get("displayTitle"))
            if not title:
                continue
            jk = it.get("jobkey") or it.get("jk") or ""
            job = {
                "source": self.source_name, "title": title,
                "company": clean_text(it.get("company") or it.get("companyName")),
                "location": clean_text(it.get("formattedLocation") or it.get("jobLocationCity")),
                "job_id": jk, "url": f"{self.base_url}/viewjob?jk={jk}" if jk else "",
                "snippet": clean_text(re.sub(r"<[^>]+>", " ", it.get("snippet") or ""))[:500],
                # pubDate is epoch milliseconds; normalize_date handles ms, but
                # keep the readable relative text as the fallback.
                "date_posted": it.get("pubDate") or it.get("formattedRelativeTime") or "",
            }
            sal = it.get("formattedSalarySnippet") or (it.get("salarySnippet") or {}).get("text") or ""
            est = it.get("extractedSalary") or {}
            if isinstance(est, dict) and est.get("min"):
                period = {"yearly": "annum", "monthly": "month", "weekly": "week", "daily": "day", "hourly": "hour"}.get(
                    str(est.get("type", "yearly")).lower(), "annum")
                apply_salary(job, {"raw": sal or f"{est['min']} - {est.get('max', est['min'])} per {period}",
                                   "min": float(est["min"]), "max": float(est.get("max") or est["min"]),
                                   "currency": self.default_currency, "period": period})
            elif sal:
                apply_salary(job, parse_salary(sal, self.default_currency))
            types = it.get("jobTypes") or []
            job["employment_type"] = detect_employment_type(" ".join(map(str, types)) if types else job["snippet"])
            job["work_mode"] = "Remote" if it.get("remoteLocation") else detect_work_mode(job["location"], job["snippet"])
            jobs.append(job)
        return jobs

    def _parse_cards(self, soup: BeautifulSoup) -> list[dict]:
        jobs = []
        cards = soup.select("div.job_seen_beacon, div[data-jk], li div[class*='cardOutline']")
        for card in cards:
            a = card.select_one("h2 a, a[data-jk], a[class*='jcs-JobTitle']")
            if not a:
                continue
            job = {"source": self.source_name, "title": clean_text(a.get_text()),
                   "url": strip_tracking(urljoin(self.base_url, a.get("href", "")))}
            jk = a.get("data-jk") or card.get("data-jk") or ""
            if jk:
                job["job_id"] = jk
                job["url"] = f"{self.base_url}/viewjob?jk={jk}"
            el = card.select_one('[data-testid="company-name"], [class*="companyName"]')
            job["company"] = clean_text(el.get_text()) if el else ""
            el = card.select_one('[data-testid="text-location"], [class*="companyLocation"]')
            job["location"] = clean_text(el.get_text()) if el else ""
            el = card.select_one('[class*="salary-snippet"], [class*="salaryText"], [data-testid="attribute_snippet_testid"]')
            if el:
                apply_salary(job, parse_salary(el.get_text(), self.default_currency))
            el = card.select_one('[class*="job-snippet"], [data-testid="jobsnippet_footer"]')
            job["snippet"] = clean_text(el.get_text(" ")) [:500] if el else ""
            el = card.select_one('[data-testid="myJobsStateDate"], span.date')
            job["date_posted"] = clean_text(el.get_text()) if el else ""
            job["work_mode"] = detect_work_mode(job["location"], job["snippet"])
            job["employment_type"] = detect_employment_type(job["snippet"])
            jobs.append(job)
        return jobs


class IndeedUKScraper(IndeedScraper):
    def __init__(self, client, delay: float = 1.5, **kwargs):
        super().__init__(client, delay, base_url="https://uk.indeed.com", source="indeed.co.uk", currency="GBP", **kwargs)
