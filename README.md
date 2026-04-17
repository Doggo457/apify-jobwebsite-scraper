# International Jobs Board Scraper

Search **14+ job boards** across the UK, US, EU and remote markets in a single run. Get one clean, deduplicated dataset with parsed salaries and optional salary benchmarks.

## Job Boards Covered

### UK Boards
| Board | What It Covers |
|-------|---------------|
| **Reed.co.uk** | UK's #1 job site — huge range of industries |
| **Totaljobs.com** | 280,000+ live jobs across all sectors |
| **CV-Library.co.uk** | 120,000+ jobs, strong outside London |
| **CWJobs.co.uk** | IT & Tech specialist roles |
| **Indeed UK** | Global aggregator with massive UK reach |
| **GOV.UK Find a Job** | Public sector & government roles |

### US Boards
| Board | What It Covers |
|-------|---------------|
| **USAJobs.gov** | US federal government jobs (free API, no key needed) |
| **Indeed US** | Largest US job aggregator |

### EU Boards
| Board | What It Covers |
|-------|---------------|
| **Indeed DE** | Indeed Germany |
| **Indeed FR** | Indeed France |
| **Indeed NL** | Indeed Netherlands |
| **Arbeitnow** | EU & remote tech jobs (free API) |

### Global / Remote
| Board | What It Covers |
|-------|---------------|
| **Adzuna** | Multi-country API — UK, US, DE, FR, NL, AU (free API key) |
| **RemoteOK** | Remote-first jobs worldwide (free API) |
| **Indeed AU** | Indeed Australia |

## What You Get

Every result includes structured, ready-to-use data:

```json
{
    "title": "Junior Software Engineer",
    "company": "TechCorp Ltd",
    "location": "London",
    "salary_raw": "£30,000 - £40,000 per annum",
    "salary_min": 30000,
    "salary_max": 40000,
    "salary_period": "annum",
    "snippet": "We are looking for a passionate junior developer...",
    "employment_type": "Permanent",
    "date_posted": "2026-03-05",
    "url": "https://www.reed.co.uk/jobs/junior-software-engineer/12345",
    "job_id": "12345",
    "source": "reed.co.uk"
}
```

Salaries are automatically parsed into min/max numbers with currency detection (GBP, USD, EUR). Duplicates posted across multiple boards are removed automatically.

### Salary Benchmarks (Optional)

Enable the **Include Salary Benchmarks** toggle to get aggregated salary stats alongside your job listings:

```json
{
    "_type": "salary_benchmark",
    "benchmark_title": "software engineer",
    "benchmark_location": "london",
    "count": 42,
    "salary_mean": 55000,
    "salary_median": 52000,
    "salary_p25": 42000,
    "salary_p75": 65000,
    "salary_min": 30000,
    "salary_max": 95000
}
```

All salaries are annualised (daily/hourly rates converted) for fair comparison.

## How to Use

1. Click **Start** on the actor page
2. Pick a **country** (UK, US, Germany, France, Netherlands, Australia, or Remote)
3. Select a **job role** from the dropdown, or type a custom keyword
4. Pick a **location** from the dropdown, or type a custom city/town
5. Set how many results you want (default: 50, or 0 for unlimited)
6. Hit **Run** — results are ready in a few minutes

Export your results as **JSON, CSV, or Excel** directly from the Apify dashboard.

## Input Options

| Setting | Default | What It Does |
|---------|---------|-------------|
| **Job Role Preset** | Software Engineer | Pick from 25 popular roles or choose "Custom" |
| **Custom Keyword** | — | Type your own search keyword (overrides preset) |
| **Location Preset** | London | Pick from 27 cities across UK/US/EU/AU or "Remote" |
| **Custom Location** | — | Type your own location (overrides preset) |
| **Country** | UK | Determines which boards are auto-selected |
| **Max Results** | 50 | Total jobs to return. Set 0 for unlimited |
| **Minimum Salary** | — | Pick a preset threshold or type a custom number |
| **Job Type** | All | Filter by Permanent, Temporary, Contract, or Part-time |
| **Job Boards** | Auto | Leave empty to auto-select based on country, or pick specific boards |
| **Fetch Full Descriptions** | Off | Fetches each job's detail page. More data but costs more |
| **Remove Duplicates** | On | Removes the same job appearing on multiple boards |
| **Salary Benchmarks** | Off | Output salary summary statistics per role/location |

## Country Defaults

When you leave the boards selection empty, boards are auto-picked based on country:

| Country | Boards |
|---------|--------|
| UK | Reed, Totaljobs, CV-Library, CWJobs, Indeed UK, GOV.UK, Adzuna |
| US | USAJobs, Indeed US, Adzuna, RemoteOK |
| Germany | Indeed DE, Adzuna, Arbeitnow |
| France | Indeed FR, Adzuna |
| Netherlands | Indeed NL, Adzuna |
| Australia | Indeed AU, Adzuna |
| Remote | RemoteOK, Arbeitnow, Adzuna |

## Enabling Adzuna (Optional)

Adzuna uses a free API instead of scraping — it's the cheapest source to run. To include it:

1. Sign up at [developer.adzuna.com](https://developer.adzuna.com) (free)
2. Create an app to get your **App ID** and **App Key**
3. Paste them into the input fields

Without these keys, Adzuna is simply skipped — everything else works fine. Adzuna supports multiple countries and automatically switches based on your country selection.

## Use Cases

- **Job seekers** — Search once, see results from everywhere
- **Recruiters** — Monitor the market for specific roles and locations
- **Salary benchmarking** — Compare pay by role and location with structured stats
- **Market analysis** — Track hiring trends across countries
- **Lead generation** — Find companies that are actively hiring
- **Remote job hunting** — Search RemoteOK + Arbeitnow + Adzuna in one go

## Tips

- Use the **country selector** to auto-pick the right boards — no need to configure boards manually
- The **salary filter** works best on boards that show salary upfront (Reed, CV-Library, Adzuna, USAJobs)
- **GOV.UK Find a Job** is great for public sector and NHS roles
- **USAJobs** is great for US federal government positions — no API key needed
- Results are sorted by date — newest listings first
- Enable **salary benchmarks** when doing market research to get median/mean/percentile stats
