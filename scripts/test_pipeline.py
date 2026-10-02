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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
