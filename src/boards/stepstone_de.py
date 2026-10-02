"""StepStone.de - Germany's largest job board (same StepStone platform as
Totaljobs and CWJobs, so the shared result-list parser applies unchanged).

URL form: /jobs/<keyword>/in-<city>?sort=2&page=N. Unlike the UK sites, deep
pages are served normally (probed to page 22 of 23 without a stall), and the
site answers plain HTTP from datacenter IPs."""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from .stepstone import StepStoneScraper

# StepStone.de facet codes (from the page's own filter links):
#   ct = contract type (222 permanent, 223 fixed-term, 225 freelance/project)
#   wfh = work from home (1 remote only, 2 partly)
_CONTRACT_PARAM = {"permanent": "ct=222", "temporary": "ct=223", "contract": "ct=225"}


class StepStoneDEScraper(StepStoneScraper):
    base_url = "https://www.stepstone.de"
    default_currency = "EUR"
    sweep = False           # deep pages are served normally here

    @property
    def source_name(self) -> str:
        return "stepstone.de"

    def _build_url(self, keyword, location, job_type, salary_min, page) -> str:
        slug = lambda s: quote_plus(re.sub(r"\s+", "-", s.strip().lower()))
        url = f"{self.base_url}/jobs/{slug(keyword)}"
        if location and location.strip().lower() not in ("remote", "germany", "deutschland"):
            url += f"/in-{slug(location)}"
        params = ["sort=2"]   # newest first, same code the site's own sort links use
        if page > 1:
            params.append(f"page={page}")
        if job_type in _CONTRACT_PARAM:
            params.append(_CONTRACT_PARAM[job_type])
        if location and location.strip().lower() == "remote":
            params.append("wfh=1")
        # No salary facet on the German site; the pipeline enforces salary_min.
        return url + "?" + "&".join(params)
