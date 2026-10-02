"""
Post-processing pipeline shared by every board.

Boards do the scraping and return jobs in the unified dict format; this module
takes that raw output and adds the value that a bare aggregator can't:

  1. Cross-board dedup + MERGE — the same role seen on 3 boards becomes ONE
     record that cites all 3 sources (``sources`` + ``source_count``). Only
     cross-board copies merge; distinct same-board listings stay separate.
  2. ATS / direct-apply detection — spot Greenhouse/Lever/Ashby/Workday/... from
     the apply URL and expose a ``direct_apply_url`` that skips the aggregator.
  3. Salary normalisation + insights — every salary standardised to annual and
     hourly figures (``salary_annual_min/max``, ``salary_hourly``), parsed from
     free text (incl. €-thousands and currency codes) with a plausibility floor.
  4. Client-side parity filters — job type, remote-only, posted-within, etc.

Everything here is pure/stateless except the incremental helpers, which read and
write a fingerprint list in the Actor key-value store.
"""

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup


# ─────────────────────────────────────────────────────────────────────────────
# ATS / direct-apply detection
# ─────────────────────────────────────────────────────────────────────────────
# Ordered list of (ats_name, compiled host/path pattern). URL-pattern matching
# only — no per-job HTTP fetches — so it's free and instant. resolveApplyUrl
# (opt-in) follows redirects first to turn aggregator links into real ATS links.
ATS_PATTERNS = [
    ("greenhouse", re.compile(r"(?:^|\.)greenhouse\.io|boards\.greenhouse\.io|job-boards\.greenhouse\.io|grnh\.se", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co|(?:^|\.)lever\.co/", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com|(?:^|\.)ashbyhq\.com", re.I)),
    ("workday", re.compile(r"\.myworkdayjobs\.com|myworkdaysite\.com|\.wd\d+\.myworkdayjobs", re.I)),
    ("workable", re.compile(r"apply\.workable\.com|\.workable\.com", re.I)),
    ("recruitee", re.compile(r"\.recruitee\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs\.|careers\.)?smartrecruiters\.com", re.I)),
    ("successfactors", re.compile(r"\.successfactors\.(?:com|eu)|\.sapsf\.(?:com|eu)|career\d*\.sapsf", re.I)),
    ("oraclecloud", re.compile(r"\.oraclecloud\.com|\.fa\.[a-z0-9]+\.oraclecloud", re.I)),
    ("icims", re.compile(r"\.icims\.com", re.I)),
    ("taleo", re.compile(r"\.taleo\.net|taleo\.net", re.I)),
    ("adp", re.compile(r"workforcenow\.adp\.com|recruiting\.adp\.com|\.adp\.com/mascsr", re.I)),
    ("jazzhr", re.compile(r"\.applytojob\.com", re.I)),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com|\.paylocity\.com/recruiting", re.I)),
    ("bamboohr", re.compile(r"\.bamboohr\.com", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com|\.jobvite\.com", re.I)),
    ("teamtailor", re.compile(r"\.teamtailor\.com", re.I)),
    ("breezy", re.compile(r"\.breezy\.hr", re.I)),
    ("personio", re.compile(r"\.jobs\.personio\.(?:com|de)", re.I)),
]


def detect_ats(urls) -> str | None:
    """Return the ATS name for the first URL that matches a known pattern."""
    for url in urls:
        if not url:
            continue
        for name, pat in ATS_PATTERNS:
            if pat.search(url):
                return name
    return None


def pick_direct_apply(urls, board_url: str) -> tuple[str | None, str]:
    """Choose the best apply URL.

    Returns (ats_name, direct_apply_url). When an ATS link is found it becomes
    the direct-apply URL (skips the aggregator); otherwise we fall back to the
    board's own listing URL so the field is always populated.
    """
    for url in urls:
        if not url:
            continue
        for name, pat in ATS_PATTERNS:
            if pat.search(url):
                return name, url
    return None, board_url or (urls[0] if urls else "")


# ─────────────────────────────────────────────────────────────────────────────
# Salary normalisation + insights
# ─────────────────────────────────────────────────────────────────────────────
# Standard annualisation multipliers (2080 work hours / 260 work days a year).
_PERIOD_TO_ANNUAL = {
    "annum": 1, "year": 1, "yr": 1, "annual": 1, "": 1,
    "month": 12, "monthly": 12,
    "week": 52, "weekly": 52,
    "day": 260, "daily": 260,
    "hour": 2080, "hourly": 2080,
}
HOURS_PER_YEAR = 2080
DAYS_PER_YEAR = 260

# Plausibility window for an *annualised* salary. Anything outside is treated as
# a non-salary number (bonus, allowance, headcount, "30,000 users", …).
SALARY_ANNUAL_FLOOR = 5000
SALARY_ANNUAL_CEIL = 500000

_SYMBOL_TO_CURRENCY = {"£": "GBP", "$": "USD", "€": "EUR"}
_CODE_TO_CURRENCY = {"gbp": "GBP", "usd": "USD", "eur": "EUR",
                     "aud": "AUD", "cad": "CAD", "chf": "CHF"}

# A currency symbol OR an ISO code (word-bounded); a number that may carry
# thousands separators and an optional trailing "k"; an optional second number
# for a range; an optional trailing ISO code; an optional period.
_SALARY_RE = re.compile(
    r"(?P<c1>[£$€]|\b(?:gbp|usd|eur|aud|cad|chf)\b)?\s*"
    r"(?P<n1>\d[\d,]*(?:\.\d+)?)\s*(?P<k1>k)?\b"
    r"(?:\s*(?:-|–|—|to)\s*(?P<c2>[£$€])?\s*(?P<n2>\d[\d,]*(?:\.\d+)?)\s*(?P<k2>k)?\b)?"
    r"(?:\s*(?P<c3>\b(?:gbp|usd|eur|aud|cad|chf)\b))?"
    r"(?:\s*(?:per\s+|pro\s+|/|an?\s+|p\.?\s*)?\s*"
    r"(?P<period>annum|annual|year|yr|jahr|month|monthly|week|weekly|day|daily|hour|hourly|hr|pa|p/a|p/h|p/d)\b)?",
    re.I,
)

_EU_THOUSANDS = re.compile(r"^\d{1,3}(\.\d{3})+$")
_PERIOD_NORM = {
    "yr": "annum", "year": "annum", "annual": "annum", "pa": "annum",
    "p/a": "annum", "jahr": "annum",
    "monthly": "month",
    "weekly": "week",
    "daily": "day",
    "hr": "hour", "hourly": "hour", "p/h": "hour", "p/d": "day",
}


def _num(raw: str, is_k: bool) -> float:
    """Parse a numeric salary token, handling English (30,000) and European
    dot-grouped thousands (55.000) notation, plus a trailing 'k'."""
    s = raw.replace(",", "")
    if _EU_THOUSANDS.match(s):          # 55.000 / 1.500.000 → strip dot groups
        s = s.replace(".", "")
    val = float(s)
    if is_k:
        val *= 1000
    return val


def _resolve_currency(*groups) -> str | None:
    for g in groups:
        if not g:
            continue
        g = g.strip()
        if g in _SYMBOL_TO_CURRENCY:
            return _SYMBOL_TO_CURRENCY[g]
        if g.lower() in _CODE_TO_CURRENCY:
            return _CODE_TO_CURRENCY[g.lower()]
    return None


def _norm_period(period: str | None) -> str:
    p = (period or "").lower()
    return _PERIOD_NORM.get(p, p or "annum")


def _build_salary(m: re.Match) -> dict | None:
    n1 = m.group("n1")
    if not n1:
        return None
    k1, k2 = bool(m.group("k1")), bool(m.group("k2"))
    currency = _resolve_currency(m.group("c1"), m.group("c2"), m.group("c3"))
    period = _norm_period(m.group("period"))
    has_period = bool(m.group("period"))

    # Require a real salary signal — a currency, a period, or a "k". Otherwise a
    # bare number ("30,000 users") is not a salary.
    if not (currency or has_period or k1 or k2):
        return None
    # Sub-annual periods need a currency too: "40 hours per week", "5 days a
    # week" and "3 month contract" are everywhere in descriptions and are not
    # pay figures ("40 hour" used to become £40/h = £83,200 a year).
    if period in ("hour", "day", "week", "month") and not (currency or k1 or k2):
        return None

    lo = _num(n1, k1)
    n2 = m.group("n2")
    if n2:
        hi = _num(n2, k2)
        # "£30-40k": the k on the upper bound applies to the lower bound too.
        if k2 and not k1 and lo < 1000:
            lo *= 1000
    else:
        hi = lo
    if lo <= 0:
        return None
    if hi < lo:
        hi = lo

    # Plausibility floor/ceiling on the *annualised* figure.
    amin = annualize(lo, period)
    amax = annualize(hi, period)
    if amin is None or amin < SALARY_ANNUAL_FLOOR or amax > SALARY_ANNUAL_CEIL:
        return None

    return {
        "raw": re.sub(r"\s+", " ", m.group(0)).strip(),
        "min": lo,
        "max": hi,
        "currency": currency,       # may be None → caller keeps its default
        "period": period,
    }


def extract_salary_from_text(text: str) -> dict | None:
    """Pull the first plausible salary out of free text, skipping non-salary
    numbers (bonuses, allowances, headcounts) via a plausibility floor."""
    if not text:
        return None
    for m in _SALARY_RE.finditer(text):
        parsed = _build_salary(m)
        if parsed:
            return parsed
    return None


def annualize(value, period: str):
    """Convert a salary figure of the given period into an annual figure."""
    if value is None:
        return None
    mult = _PERIOD_TO_ANNUAL.get((period or "annum").lower(), 1)
    return round(float(value) * mult)


def derive_salary_insights(job: dict) -> None:
    """(Re)compute the standardised annual + hourly fields from the current
    salary_min/max/period. Idempotent — safe to call again after a merge."""
    period = job.get("salary_period") or "annum"
    ann_min = annualize(job.get("salary_min"), period)
    ann_max = annualize(job.get("salary_max"), period)
    job["salary_annual_min"] = ann_min
    job["salary_annual_max"] = ann_max
    annuals = [a for a in (ann_min, ann_max) if a]
    job["salary_hourly"] = round(sum(annuals) / len(annuals) / HOURS_PER_YEAR, 2) if annuals else None


def enrich_salary(job: dict) -> None:
    """Fill structured salary from free text when the board gave none, then
    derive the standardised annual + hourly fields."""
    if not job.get("salary_min") and not job.get("salary_max"):
        # description holds the (already-formatted) body; full_description is
        # dropped by enrich_job before this runs to save memory.
        for field in ("salary_raw", "description", "snippet"):
            parsed = extract_salary_from_text(job.get(field, "") or "")
            if parsed:
                job["salary_min"] = parsed["min"]
                job["salary_max"] = parsed["max"]
                job["salary_period"] = parsed["period"]
                # A symbol/code parsed from text OVERRIDES the board default
                # currency (normalize_job pre-fills one, so a plain guard is dead).
                if parsed.get("currency"):
                    job["salary_currency"] = parsed["currency"]
                if not job.get("salary_raw"):
                    job["salary_raw"] = parsed.get("raw", "")
                break
    derive_salary_insights(job)


# ─────────────────────────────────────────────────────────────────────────────
# Description formatting
# ─────────────────────────────────────────────────────────────────────────────
def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_markdown(html: str) -> str:
    """Lightweight HTML→Markdown for job descriptions (no external deps)."""
    if not html:
        return ""
    if "<" not in html:
        return html.strip()
    soup = BeautifulSoup(html, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    def render(node) -> str:
        from bs4 import NavigableString
        if isinstance(node, NavigableString):
            return re.sub(r"\s+", " ", str(node))
        name = node.name
        inner = "".join(render(c) for c in node.children)
        if name in ("strong", "b"):
            return f"**{inner.strip()}**"
        if name in ("em", "i"):
            return f"*{inner.strip()}*"
        if name == "a":
            href = node.get("href", "")
            return f"[{inner.strip()}]({href})" if href else inner
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            return f"\n\n{'#' * level} {inner.strip()}\n\n"
        if name == "li":
            return f"- {inner.strip()}\n"
        if name in ("ul", "ol"):
            return f"\n{inner}\n"
        if name in ("p", "div", "section"):
            return f"\n\n{inner.strip()}\n\n"
        return inner

    md = render(soup)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def format_description(raw: str, fmt: str) -> str:
    """Return the description in the requested format (markdown/html/plaintext)."""
    if not raw:
        return ""
    if fmt == "html":
        return raw.strip()
    if fmt == "plaintext":
        return html_to_text(raw)
    # default: markdown
    return html_to_markdown(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Location matching (token-based, alias-aware) — shared by the remote boards
# ─────────────────────────────────────────────────────────────────────────────
_LOC_ALIASES = {
    "us": {"us", "usa", "united states", "america"},
    "usa": {"us", "usa", "united states", "america"},
    "united states": {"us", "usa", "united states", "america"},
    "uk": {"uk", "united kingdom", "britain", "gb", "england", "scotland", "wales"},
    "united kingdom": {"uk", "united kingdom", "britain", "gb"},
    "england": {"uk", "england", "britain"},
    "usa/canada": {"us", "usa", "canada"},
}


def location_matches(query: str, geo: str) -> bool:
    """True when a remote board's geo string covers the requested location.

    Token-based (not substring) so 'US' does not match 'Australia'/'Austria'
    and 'UK' does not match 'Ukraine'. Worldwide/anywhere always match.
    """
    q = (query or "").lower().strip()
    if not q or q in ("remote", "anywhere", "worldwide"):
        return True
    g = (geo or "").lower()
    if not g or "anywhere" in g or "worldwide" in g:
        return True
    tokens = {t for t in re.split(r"[^a-z]+", g) if t}
    for alias in _LOC_ALIASES.get(q, {q}):
        if " " in alias:
            if alias in g:
                return True
        elif alias in tokens:
            return True
    if " " in q:
        return q in g
    return q in tokens


# ─────────────────────────────────────────────────────────────────────────────
# Remote / job-type inference
# ─────────────────────────────────────────────────────────────────────────────
_REMOTE_BOARDS = {"remoteok.com", "remotive.com", "jobicy.com"}
_REMOTE_TERMS = ("remote", "anywhere", "work from home", "wfh", "worldwide",
                 "fully remote", "distributed", "home based", "home-based",
                 "telecommute")


def is_remote_job(job: dict) -> bool:
    if job.get("source") in _REMOTE_BOARDS:
        return True
    hay = " ".join([
        str(job.get("location", "")), str(job.get("title", "")),
        str(job.get("employment_type", "")), str(job.get("remote", "")),
    ]).lower()
    return any(t in hay for t in _REMOTE_TERMS)


def normalize_job_type(job: dict) -> str:
    """Map a board's messy type into fulltime/parttime/contract/internship."""
    emp = str(job.get("employment_type", "")).lower()
    title = str(job.get("title", "")).lower()
    strong = emp + " " + title  # high-confidence signals

    if re.search(r"\bintern(ship)?\b|\bplacement\b", strong):
        return "internship"
    if re.search(r"part[\s-]?time", strong):
        return "parttime"
    if re.search(r"\bcontract|\bfreelance|\btemporary|\btemp\b|fixed[\s-]?term|\bc2c\b", strong):
        return "contract"
    if re.search(r"full[\s-]?time|\bpermanent\b|\bperm\b|\bfulltime\b", strong):
        return "fulltime"
    # Last resort: scan the description body.
    body = str(job.get("description", "") or job.get("snippet", "")).lower()
    if re.search(r"internship", body):
        return "internship"
    if re.search(r"part[\s-]?time", body):
        return "parttime"
    if re.search(r"full[\s-]?time|permanent", body):
        return "fulltime"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Posted-date parsing
# ─────────────────────────────────────────────────────────────────────────────
# Handles "3 days ago" and Indeed's stale "30+ days ago" marker (the '+' means
# "at least", so we take the number as a lower bound on the age).
_REL_RE = re.compile(r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month|year|yr)s?\s*ago", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_posted(value: str, now: datetime) -> tuple[str | None, float | None]:
    """Parse a posted date (ISO or relative) → (iso_utc, age_hours)."""
    if not value:
        return None, None
    v = str(value).strip()
    low = v.lower()

    if low in ("today", "just now", "just posted", "new"):
        return now.isoformat(), 0.0
    if low == "yesterday":
        dt = now - timedelta(days=1)
        return dt.isoformat(), 24.0

    rel = _REL_RE.search(low)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2)
        hours = {"minute": n / 60, "min": n / 60, "hour": n, "hr": n,
                 "day": n * 24, "week": n * 24 * 7, "month": n * 24 * 30,
                 "year": n * 24 * 365, "yr": n * 24 * 365}[unit]
        dt = now - timedelta(hours=hours)
        return dt.isoformat(), round(hours, 2)

    # ISO 8601 (with or without timezone / trailing Z)
    iso = v.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (now - dt).total_seconds() / 3600
        return dt.astimezone(timezone.utc).isoformat(), round(age, 2)
    except ValueError:
        pass

    # "15 March 2026" / "Mar 5, 2026"
    m = re.search(r"(\d{1,2})\s+([a-z]{3})[a-z]*\s+(\d{4})", low) or \
        re.search(r"([a-z]{3})[a-z]*\s+(\d{1,2}),?\s+(\d{4})", low)
    if m:
        try:
            g = m.groups()
            if g[0].isdigit():
                day, mon, year = int(g[0]), _MONTHS.get(g[1]), int(g[2])
            else:
                mon, day, year = _MONTHS.get(g[0]), int(g[1]), int(g[2])
            if mon:
                dt = datetime(year, mon, day, tzinfo=timezone.utc)
                age = (now - dt).total_seconds() / 3600
                return dt.isoformat(), round(age, 2)
        except (ValueError, TypeError):
            pass
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment (per-job)
# ─────────────────────────────────────────────────────────────────────────────
def enrich_job(job: dict, opts: dict, now: datetime) -> dict:
    """Add the derived fields to a single normalised job dict (in place)."""
    # Description (formatted). Prefer full_description, fall back to snippet.
    raw_desc = job.get("full_description") or job.get("description") or job.get("snippet") or ""
    job["description"] = format_description(raw_desc, opts.get("description_format", "markdown"))
    # Drop the raw description now the formatted one exists — cuts peak memory.
    job.pop("full_description", None)

    # Salary normalisation + insights (always on).
    enrich_salary(job)

    # ATS / direct-apply detection (URL-pattern only).
    candidate_urls = [
        job.get("apply_url", ""), job.get("company_url", ""),
        job.get("resolved_url", ""), job.get("url", ""),
    ]
    ats, direct = pick_direct_apply(candidate_urls, job.get("url", ""))
    job["ats"] = ats
    job["direct_apply_url"] = direct

    # Remote + normalised job type.
    job["is_remote"] = is_remote_job(job)
    job["job_type"] = normalize_job_type(job)

    # Posted date → ISO + age.
    posted_iso, age = parse_posted(job.get("date_posted", ""), now)
    job["posted_at"] = posted_iso or ""
    job["_posted_age_hours"] = age

    return job


def redetect_ats(job: dict) -> None:
    """Re-run ATS detection after resolveApplyUrl fills in resolved_url."""
    candidate_urls = [
        job.get("apply_url", ""), job.get("company_url", ""),
        job.get("resolved_url", ""), job.get("url", ""),
    ]
    ats, direct = pick_direct_apply(candidate_urls, job.get("url", ""))
    job["ats"] = ats
    job["direct_apply_url"] = direct


# ─────────────────────────────────────────────────────────────────────────────
# Client-side parity filters
# ─────────────────────────────────────────────────────────────────────────────
def filter_reason(job: dict, opts: dict) -> str | None:
    """Return the name of the first filter a job fails, or None if it passes.

    Unknown values are kept (benefit of the doubt) so a missing field never
    silently empties the dataset.
    """
    if opts.get("remote_only") and not job.get("is_remote"):
        return "remote_only"

    want_type = opts.get("job_type_norm")
    if want_type and want_type != "any":
        jt = job.get("job_type")
        if jt and jt != want_type:
            return "job_type"

    within = opts.get("posted_within_hours")
    if within:
        age = job.get("_posted_age_hours")
        if age is not None and age > within:
            return "posted_within"

    smin = opts.get("salary_min")
    if smin:
        # Compared in the job's OWN currency (documented in README).
        top = job.get("salary_annual_max") or job.get("salary_annual_min")
        if top is not None and top < smin:
            return "salary_min"

    return None


def passes_filters(job: dict, opts: dict) -> bool:
    return filter_reason(job, opts) is None


# ─────────────────────────────────────────────────────────────────────────────
# Cross-board dedup + merge
# ─────────────────────────────────────────────────────────────────────────────
# Legal/company suffixes stripped ONLY from the END of a company name (so
# "Acme Ltd" == "Acme Inc" == "Acme"). Generic descriptor words (group, services,
# solutions, uk, co, …) are deliberately NOT here — they cause false merges.
_COMPANY_SUFFIX_WORDS = {
    "inc", "incorporated", "ltd", "limited", "llc", "llp", "plc", "corp",
    "corporation", "company", "gmbh", "ag", "sa", "srl", "bv", "nv", "pty",
    "holdings", "kg", "oy", "ab", "sarl", "spa",
}


def _nfkd(s: str) -> str:
    """Strip accents/diacritics so 'Zürich' == 'Zurich'."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")


def _norm_title(s: str) -> str:
    """Title normaliser — punctuation + whitespace only. NO suffix stripping,
    so 'Group Financial Accountant' != 'Financial Accountant'."""
    s = re.sub(r"[^\w\s]", " ", _nfkd(s).lower())
    return re.sub(r"\s+", " ", s).strip()


def _norm_company(s: str) -> str:
    """Company normaliser — strips trailing legal suffixes only."""
    s = re.sub(r"[^\w\s]", " ", _nfkd(s).lower())
    tokens = re.sub(r"\s+", " ", s).strip().split()
    while tokens and tokens[-1] in _COMPANY_SUFFIX_WORDS:
        tokens.pop()
    return " ".join(tokens)


_REMOTE_LOC_RE = re.compile(r"\b(remote|anywhere|worldwide|distributed|wfh)\b")


def _norm_location(s: str) -> str:
    """Location normaliser — first locality, accent-folded, remote-canonicalised.
    Strips 'city of'/'greater' prefixes so they don't split real duplicates."""
    first = _nfkd(s or "").lower().split(",")[0]
    first = re.sub(r"[^\w\s]", " ", first)
    first = re.sub(r"\b(city of|greater)\b", " ", first)
    first = re.sub(r"\s+", " ", first).strip()
    if not first:
        return ""
    if _REMOTE_LOC_RE.search(first):
        return "remote"
    return first


def job_fingerprint(job: dict) -> str:
    """title|company|location fingerprint. Returns '' when the company is empty
    after normalisation — the caller then treats the job as unique (never
    merges different companies just because both have a blank company)."""
    company = _norm_company(job.get("company", ""))
    if not company:
        return ""
    title = _norm_title(job.get("title", ""))
    location = _norm_location(job.get("location", ""))
    return f"{title}|{company}|{location}"


def _richer(a: str, b: str) -> str:
    return a if len(a or "") >= len(b or "") else b


def _merge_into(base: dict, job: dict, src: dict) -> None:
    """Fold a cross-board duplicate into the base record."""
    base["description"] = _richer(base.get("description", ""), job.get("description", ""))
    base["snippet"] = _richer(base.get("snippet", ""), job.get("snippet", ""))

    # Salary is adopted as an ATOMIC block (min+max+currency+period together) and
    # only when the base has NEITHER figure — never mix currencies/periods across
    # boards into a fabricated range.
    if (not base.get("salary_min") and not base.get("salary_max")
            and (job.get("salary_min") or job.get("salary_max"))):
        for f in ("salary_min", "salary_max", "salary_currency", "salary_period", "salary_raw"):
            base[f] = job.get(f)

    base["is_remote"] = bool(base.get("is_remote")) or bool(job.get("is_remote"))
    for f in ("employment_type", "job_type", "category", "date_posted",
              "posted_at", "valid_through"):
        if not base.get(f) and job.get(f):
            base[f] = job[f]

    if not base.get("ats") and job.get("ats"):
        base["ats"] = job["ats"]
        base["direct_apply_url"] = job.get("direct_apply_url", base.get("direct_apply_url"))

    existing = {(s["board"], s["url"]) for s in base["sources"]}
    if (src["board"], src["url"]) not in existing:
        base["sources"].append(src)
        base["source_count"] = len(base["sources"])


def merge_jobs(jobs: list[dict], do_merge: bool = True) -> list[dict]:
    """Collapse CROSS-BOARD duplicates into single records that cite every
    source. Distinct same-board listings (different URLs) stay separate."""
    if not do_merge:
        for job in jobs:
            job["sources"] = [{"board": job.get("source", ""), "url": job.get("url", "")}]
            job["source_count"] = 1
        return jobs

    merged: dict[str, dict] = {}
    order: list[str] = []

    for idx, job in enumerate(jobs):
        src = {"board": job.get("source", ""), "url": job.get("url", "")}
        fp = job_fingerprint(job) or f"__uniq_{idx}__"
        base = merged.get(fp)

        if base is None:
            job["sources"] = [src]
            job["source_count"] = 1
            merged[fp] = job
            order.append(fp)
            continue

        # Only merge across DIFFERENT boards. Same board + same title/company/city
        # is a distinct listing → salt with its URL/id so it stays separate.
        if src["board"] in {s["board"] for s in base["sources"]}:
            salt = job.get("url") or job.get("job_id") or str(idx)
            fp2 = f"{fp}##{salt}"
            if fp2 in merged:
                _merge_into(merged[fp2], job, src)
            else:
                job["sources"] = [src]
                job["source_count"] = 1
                merged[fp2] = job
                order.append(fp2)
            continue

        _merge_into(base, job, src)

    return [merged[fp] for fp in order]


# ─────────────────────────────────────────────────────────────────────────────
# Incremental / scheduled mode (persistent fingerprint list in the KV store)
# ─────────────────────────────────────────────────────────────────────────────
_SEEN_CAP = 50000  # cap the stored list so scheduled runs never grow unbounded


def incremental_key(config: dict) -> str:
    """Stable KV-store key for a given search config, so scheduled runs of the
    SAME search accumulate their own fingerprint history."""
    basis = "|".join([
        ",".join(sorted(config.get("search_terms", []))),
        (config.get("location") or "").lower(),
        (config.get("country") or "").lower(),
        ",".join(sorted(config.get("boards", []))),
    ])
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
    return f"seen_fingerprints_{digest}"


def incremental_fingerprints(job: dict) -> list[str]:
    """All stable fingerprints for a (possibly merged) job — one per source URL,
    plus the direct-apply URL. A hit on ANY means the job was seen before, so a
    merged job re-seen via a different source next run is still recognised."""
    urls = []
    for s in job.get("sources", []) or []:
        if s.get("url"):
            urls.append(s["url"])
    if job.get("url"):
        urls.append(job["url"])
    if job.get("direct_apply_url"):
        urls.append(job["direct_apply_url"])
    fps = ["u:" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:20] for u in dict.fromkeys(urls)]
    if not fps:
        jf = job_fingerprint(job)
        if jf:
            fps = ["f:" + hashlib.sha1(jf.encode("utf-8")).hexdigest()[:20]]
    return fps


def job_is_seen(job: dict, seen: set) -> bool:
    return any(fp in seen for fp in incremental_fingerprints(job))


def filter_unseen(jobs: list[dict], seen: set) -> list[dict]:
    """Return only jobs whose fingerprints aren't in ``seen``. Safe on first run
    (empty ``seen`` ⇒ everything is new)."""
    return [job for job in jobs if not job_is_seen(job, seen)]


def updated_seen_list(prior: list, pushed_jobs: list[dict], cap: int = _SEEN_CAP) -> list[str]:
    """Insertion-ordered fingerprint list = prior + fingerprints of the jobs we
    ACTUALLY pushed, de-duplicated, trimmed from the FRONT (evict oldest)."""
    ordered = list(prior or [])
    for job in pushed_jobs:
        ordered.extend(incremental_fingerprints(job))
    ordered = list(dict.fromkeys(ordered))
    if len(ordered) > cap:
        ordered = ordered[-cap:]
    return ordered


# ─────────────────────────────────────────────────────────────────────────────
# Output shaping
# ─────────────────────────────────────────────────────────────────────────────
# Internal-only keys stripped before a record is pushed to the dataset.
_INTERNAL_KEYS = ("full_description", "apply_url", "company_url", "resolved_url",
                  "remote", "_posted_age_hours", "_card_text", "_segments",
                  "_company_link_text")


def finalize(job: dict) -> dict:
    """Strip internal bookkeeping keys just before pushing to the dataset."""
    for k in _INTERNAL_KEYS:
        job.pop(k, None)
    return job
