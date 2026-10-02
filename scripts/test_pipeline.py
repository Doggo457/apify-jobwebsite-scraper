"""Standalone regression tests for the v0.6 pipeline fixes.

Run:  py -3 scripts/test_pipeline.py
Exits non-zero on the first failed assertion. No pytest dependency.
Each test maps to a bug the Fable review found in v0.5.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pipeline  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def job(**kw):
    base = {"source": "reed", "url": "https://x/1", "company": "Acme Ltd",
            "title": "Software Engineer", "location": "London"}
    base.update(kw)
    return base


print("Salary parsing")
s = pipeline.extract_salary_from_text("£30-40k per annum")
check("£30-40k -> 30000/40000", s and s["min"] == 30000 and s["max"] == 40000, s)
s = pipeline.extract_salary_from_text("€55.000 - €65.000 pro Jahr")
check("€55.000 EU-thousands -> 55000/65000", s and s["min"] == 55000 and s["max"] == 65000, s)
check("£25 monthly wellness allowance -> no salary",
      pipeline.extract_salary_from_text("£25 monthly wellness allowance") is None)
check("£500 referral bonus -> no salary (floor)",
      pipeline.extract_salary_from_text("£500 referral bonus") is None)
s = pipeline.extract_salary_from_text("£12.50 per hour")
check("£12.50/hr -> 12.5", s and abs(s["min"] - 12.5) < 0.001, s)
j = job(salary_currency="USD", salary_raw="£30k - £40k")
pipeline.enrich_salary(j)
check("hourly annualises to ~26000", pipeline.annualize(12.5, "hour") == 26000)
check("text currency overrides board default USD->GBP", j.get("salary_currency") == "GBP", j.get("salary_currency"))
check("£12.50/hr annual field present",
      (lambda x: (pipeline.enrich_salary(x), x.get("salary_annual_min"))[1])(job(salary_raw="£12.50 per hour")) == 26000)

print("Dedup / merge")
m = pipeline.merge_jobs([job(title="Group Financial Accountant", company="Acme Ltd"),
                         job(title="Financial Accountant", company="Acme Ltd", url="https://x/2")])
check("'Group Financial Accountant' != 'Financial Accountant'", len(m) == 2, len(m))
m = pipeline.merge_jobs([job(company="", url="https://a/1"), job(company="", url="https://b/1", title="Analyst")])
check("two blank-company jobs stay separate", len(m) == 2, len(m))
m = pipeline.merge_jobs([job(source="reed", url="https://reed/1"),
                         job(source="reed", url="https://reed/2")])
check("same board + diff url stays separate", len(m) == 2, len(m))
m = pipeline.merge_jobs([job(source="reed", url="https://reed/1"),
                         job(source="totaljobs", url="https://tj/1")])
check("cross-board dup merges to 1", len(m) == 1 and m[0]["source_count"] == 2, m)

print("Incremental")
a = job(source="reed", url="https://reed/1")
b = job(source="totaljobs", url="https://tj/1")
merged = pipeline.merge_jobs([a, b])          # one record, 2 sources
seen = set(pipeline.updated_seen_list([], merged))
check("first run records all source fingerprints", len(seen) >= 2, len(seen))
# same job re-seen next run via ONLY the totaljobs source -> still recognised
again = pipeline.merge_jobs([job(source="totaljobs", url="https://tj/1")])
check("merged job re-seen via one source is filtered", pipeline.filter_unseen(again, seen) == [])
fresh = pipeline.merge_jobs([job(source="reed", url="https://reed/NEW", title="New Role")])
check("genuinely new job passes", len(pipeline.filter_unseen(fresh, seen)) == 1)
# eviction trims from the FRONT (oldest), preserving newest
big = ["old"] + [f"fp{i}" for i in range(pipeline._SEEN_CAP)]
trimmed = pipeline.updated_seen_list(big, [], cap=pipeline._SEEN_CAP)
check("cap evicts oldest from front", "old" not in trimmed and len(trimmed) == pipeline._SEEN_CAP)

print("Dates")
now = datetime(2026, 7, 6, tzinfo=timezone.utc)
_, age = pipeline.parse_posted("30+ days ago", now)
check("'30+ days ago' -> 720h", age == 720, age)

print("Location matching")
check("US !~ Australia", not pipeline.location_matches("US", "Australia"))
check("US !~ Austria", not pipeline.location_matches("US", "Austria"))
check("UK !~ Ukraine", not pipeline.location_matches("UK", "Ukraine"))
check("US ~ United States", pipeline.location_matches("US", "United States"))
check("anywhere always matches", pipeline.location_matches("US", "Anywhere"))


# ── v0.12: sub-annual periods without a currency are not salaries ──
check("40 hours per week is not a salary", pipeline.extract_salary_from_text("Full time, 40 hours per week. Salary negotiable.") is None)
check("5 days a week is not a salary", pipeline.extract_salary_from_text("Working pattern: 5 days a week in the office") is None)
check("GBP hourly still parses", (pipeline.extract_salary_from_text("Pay: £18.50 per hour") or {}).get("period") == "hour")
check("EUR day rate still parses", (pipeline.extract_salary_from_text("€450 - €500 per day") or {}).get("max") == 500.0)
check("annual without currency still parses", (pipeline.extract_salary_from_text("45,000 per annum") or {}).get("min") == 45000.0)
check("hours plural no longer matches period", (pipeline.extract_salary_from_text("£30,000 per annum, 37.5 hours") or {}).get("period") == "annum")


# ── v0.12: every record must satisfy the registered dataset schema ──
# actor.json registers .actor/dataset_schema.json under storages.dataset, so
# the platform validates each push_data() item and REJECTS the whole batch on
# a single mismatch. Run realistic records (no salary, no ATS, benchmark row)
# through the real pipeline and validate them the way the platform does.
print("Dataset schema")
import json  # noqa: E402
try:
    from jsonschema import Draft7Validator  # noqa: E402
except ImportError:  # keep the suite dependency-free; this check is optional
    Draft7Validator = None
if Draft7Validator is None:
    print("  skip  jsonschema not installed (pip install jsonschema)")
else:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from src import main  # noqa: E402  (needs the apify package, same as the Actor)
    schema = json.load(open(os.path.join(os.path.dirname(__file__), "..", ".actor", "dataset_schema.json")))
    validator = Draft7Validator(schema["fields"])
    raw = [
        job(source="reed.co.uk", url="https://www.reed.co.uk/jobs/x/1", snippet="No salary stated", date_posted="2 days ago"),
        job(source="cv-library.co.uk", url="https://www.cv-library.co.uk/job/2", salary_raw="£30,000 - £40,000 per annum",
            salary_min=30000, salary_max=40000, salary_period="annum", date_posted="2026-07-01"),
        job(source="remoteok.com", url="https://remoteok.com/remote-jobs/3", location="Remote", title="Senior Engineer",
            salary_raw="$150k", salary_min=150000.0, salary_max=None, salary_period="annum", salary_currency="USD"),
        job(source="totaljobs.com", url="https://www.totaljobs.com/job/4", title="Nurse", date_posted="", company=""),
    ]
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    records = [pipeline.enrich_job(main.normalize_job(r), {"description_format": "markdown"}, now) for r in raw]
    records = [pipeline.finalize(r) for r in pipeline.merge_jobs(records)]
    records += main.compute_salary_benchmarks([
        {"title": "Dev", "location": "London", "salary_annual_min": 50000, "salary_annual_max": 60000},
        {"title": "Dev", "location": "London", "salary_annual_min": 55000, "salary_annual_max": 65000},
    ])
    check("benchmark row produced", any(r.get("_type") == "salary_benchmark" for r in records))
    for r in records:
        errs = [f"{'.'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(json.loads(json.dumps(r)))]
        check(f"schema-valid {r.get('source') or r.get('_type')}", not errs, "; ".join(errs)[:200])
    check("ats null is allowed", "null" in schema["fields"]["properties"]["ats"]["type"])

# ── v0.12: top-up counts against the deduplicated total ──
print("Merged count")
import copy  # noqa: E402
import random  # noqa: E402
rng = random.Random(7)
boards = ["reed.co.uk", "totaljobs.com", "cv-library.co.uk", "jobs.service.gov.uk"]
sample = []
for i in range(400):
    k = rng.randrange(120)   # 120 distinct roles spread over 400 listings => plenty of dupes
    b = rng.choice(boards)
    sample.append(job(source=b, url=f"https://{b}/job/{i}", title=f"Engineer {k}", company=f"Firm {k % 37} Ltd",
                      location=rng.choice(["London", "Greater London", "Manchester", ""])))
sample.append(job(source="reed.co.uk", url="https://reed/nocompany", company=""))
expected = len(pipeline.merge_jobs(copy.deepcopy(sample)))
check("merged_count == len(merge_jobs) with dedup", pipeline.merged_count(sample, True) == expected,
      f"{pipeline.merged_count(sample, True)} vs {expected}")
check("merged_count == len(jobs) without dedup", pipeline.merged_count(sample, False) == len(sample))
check("merged_count leaves jobs untouched", "sources" not in sample[0])

# ── v0.12: Arbeitnow honours location='Remote' (used by the Remote task) ──
print("Arbeitnow location filter")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.boards.arbeitnow import ArbeitnowScraper  # noqa: E402
toks = ["software", "engineer"]
berlin = {"title": "Software Engineer", "description": "", "tags": [], "location": "Berlin", "remote": False}
remote = {"title": "Software Engineer", "description": "", "tags": [], "location": "Berlin", "remote": True}
other = {"title": "Chef", "description": "", "tags": [], "location": "Berlin", "remote": True}
check("remote keeps only remote listings", ArbeitnowScraper.wanted(remote, toks, "remote") and not ArbeitnowScraper.wanted(berlin, toks, "remote"))
check("empty location keeps everything matching", ArbeitnowScraper.wanted(berlin, toks, "") and ArbeitnowScraper.wanted(remote, toks, ""))
check("city keeps that city plus remote", ArbeitnowScraper.wanted(berlin, toks, "berlin") and ArbeitnowScraper.wanted(remote, toks, "munich") and not ArbeitnowScraper.wanted(berlin, toks, "munich"))
check("keyword tokens still required", not ArbeitnowScraper.wanted(other, toks, "remote"))

# ── v0.12: StepStone UK sweep never paginates past page 4 and Indeed never asks for page 2 ──
print("StepStone sweep / Indeed variants")
import asyncio  # noqa: E402
from src.boards.totaljobs import TotaljobsScraper  # noqa: E402
from src.boards.stepstone_de import StepStoneDEScraper  # noqa: E402
from src.boards.indeed import IndeedUKScraper  # noqa: E402
from urllib.parse import urlparse, parse_qs  # noqa: E402

class _NoClient:  # the sweep is driven through stubs; no network
    pass

tj = TotaljobsScraper(_NoClient())
tj.max_pages = 40
requested = []
async def fake_get_html(url):
    requested.append(url)
    return url  # the "html" is the URL; the stub parser derives jobs from it
def fake_parse(html, soup):
    q = parse_qs(urlparse(html).query); path = urlparse(html).path
    page = int(q.get("page", ["1"])[0]); variant = path.split("/jobs/")[1].split("/")[0] + "|" + ("sort=" + q["sort"][0] if "sort" in q else "date")
    # 25 ids per page, unique per variant and page; 5 of them collide with the previous variant to exercise dedup
    jobs = [{"job_id": f"{variant}-{page}-{i}", "title": "x", "url": f"https://t/{variant}-{page}-{i}"} for i in range(25)]
    return jobs, page < 42
tj._get_html = fake_get_html
tj._parse_search = fake_parse
async def _polite(): pass
tj._polite_delay = _polite
got = asyncio.run(tj.search("software engineer", "London", max_results=1000))
pages = [parse_qs(urlparse(u).query).get("page", ["1"])[0] for u in requested]
check("sweep requests 28 pages (7 searches x 4)", len(requested) == 28, len(requested))
check("sweep never asks for page 5+", all(int(p) <= 4 for p in pages), sorted(set(pages)))
check("sweep yields 700 unique rows", len(got) == 700 and len({j["job_id"] for j in got}) == 700, len(got))
check("sweep uses the contract path segment", any("/jobs/contract/software-engineer/in-london" in u for u in requested))
check("sweep uses salary-desc and relevance sorts", any("sort=4" in u for u in requested) and any("sort=1" in u for u in requested))
check("sweep marks the board exhausted at the end", tj.exhausted)
check("no employmenttype= param anywhere", not any("employmenttype=" in u for u in requested))
# resumability: a small first call, then a top-up call continues the sweep
tj2 = TotaljobsScraper(_NoClient()); tj2._get_html = fake_get_html; tj2._parse_search = fake_parse; tj2._polite_delay = _polite
requested.clear()
first = asyncio.run(tj2.search("software engineer", "London", max_results=60))
second = asyncio.run(tj2.search("software engineer", "London", max_results=60))
check("sweep resumes without repeating pages", len(requested) == 6 and len(set(requested)) == 6 and len(first) == 75 and len(second) == 75 and not tj2.exhausted, (len(requested), len(first), len(second)))
check("job_type fixed -> only that segment, three sorts", [v[0] for v in tj2._variants("contract")] == ["contract"] * 3)
check("StepStone.de paginates plainly", not StepStoneDEScraper.sweep and "page=7" in StepStoneDEScraper(_NoClient())._build_url("software engineer", "Berlin", "all", None, 7))
check("salary_min becomes the annual facet", "salary=50000&salarytypeid=1" in tj._search_url("a", "b", "", "sortby=Date", 50000, 1))

ind = IndeedUKScraper(_NoClient())
urls = [ind._build_url("software engineer", "London", "all", None, p) for p in (1, 2, 3)]
check("Indeed never requests start= (sign-in wall)", not any("start=" in u for u in urls), urls)
check("Indeed variants differ", len(set(urls)) == 3 and "sort=date" in urls[0] and "sort=date" not in urls[1] and "radius=25" in urls[2])
from src.utils import LazySoup  # noqa: E402
ind._page = 3
check("Indeed stops after its third variant", ind._parse_search("<html><body></body></html>", LazySoup("<html><body></body></html>"))[1] is False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
