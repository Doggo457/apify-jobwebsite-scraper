# International Jobs Board Scraper

Search **14+ job boards** across the UK, US, EU and remote markets in a single run. Get one clean, deduplicated dataset with parsed salaries, ISO dates, work mode (remote / hybrid / on-site) and optional salary benchmarks.

Typical UK run (100 results across 6 boards): **a few seconds**, no browser needed.

## Job Boards Covered

### UK Boards
| Board | What It Covers |
|-------|---------------|
| **Reed.co.uk** | UK's #1 job site, huge range of industries. Add a free Reed API key for descriptions too |
| **Totaljobs.com** | 280,000+ live jobs across all sectors |
| **CWJobs.co.uk** | IT & Tech specialist roles |
| **GOV.UK Find a Job (Work Hub)** | Public sector, NHS and government roles via jobs.service.gov.uk |
| **CV-Library.co.uk** | 120,000+ jobs, strong outside London. Best-effort (heavy bot protection) |
| **Indeed UK** | Global aggregator. Best-effort (heavy bot protection) |

### US Boards
| Board | What It Covers |
|-------|---------------|
| **USAJobs.gov** | US federal government jobs (free API key required, see below) |
| **Indeed US** | Largest US job aggregator. Best-effort |

### EU Boards
| Board | What It Covers |
|-------|---------------|
| **Arbeitnow** | EU & remote tech jobs (free API) |
| **Indeed DE / FR / NL** | Indeed Germany, France, Netherlands. Best-effort |

### Global / Remote
| Board | What It Covers |
|-------|---------------|
| **Adzuna** | Multi-country API: UK, US, DE, FR, NL, AU (free API key) |
| **RemoteOK** | Remote-first jobs worldwide (free API) |
| **Indeed AU** | Indeed Australia. Best-effort |

## What You Get

Every result has the same fields, so exports to CSV or Excel are tidy:

```json
{
    "title": "Backend Software Engineer",
    "company": "Lloyds Banking Group",
    "location": "London",
    "salary_raw": "£76,760 to £95,950 a year",
    "salary_min": 76760,
    "salary_max": 95950,
    "salary_currency": "GBP",
    "salary_period": "annum",
    "employment_type": "Permanent, full time",
    "work_mode": "Hybrid",
    "snippet": "We are looking for a pragmatic and hands-on engineer...",
    "date_posted": "2026-09-11",
    "valid_through": "2026-10-28",
    "url": "https://www.jobs.service.gov.uk/jobs/6aa39ec481d17404e72eaee6",
    "job_id": "6aa39ec481d17404e72eaee6",
    "source": "jobs.service.gov.uk",
    "category": "Software Development"
}
```

* Salaries are parsed into min/max numbers with currency (GBP, USD, EUR, AUD) and period (annum, day, hour, week, month).
* `date_posted` and `valid_through` are normalised to ISO dates (`YYYY-MM-DD`) wherever the board gives a readable date ("3 days ago", "18 September", "Sep 18, 2026" all become real dates).
* `work_mode` is Remote, Hybrid or On-site when the listing states it.
* `category` comes from the board where it provides one, otherwise it is inferred from the title (Software Development, Data & Analytics, Healthcare & Medical, and so on).
* Duplicates posted across boards are removed (same title, company and town).
* URLs have tracking parameters stripped so the same job always has the same link.

### Salary Benchmarks (Optional)

Enable **Include Salary Benchmarks** to get aggregated salary stats alongside your job listings:

```json
{
    "_type": "salary_benchmark",
    "benchmark_title": "software engineer",
    "benchmark_location": "london",
    "salary_currency": "GBP",
    "count": 42,
    "salary_mean": 55000,
    "salary_median": 52000,
    "salary_p25": 42000,
    "salary_p75": 65000,
    "salary_min": 30000,
    "salary_max": 95000
}
```

Daily, hourly, weekly and monthly rates are annualised before comparison.

## How to Use

1. Click **Start** on the actor page
2. Pick a **country** (UK, US, Germany, France, Netherlands, Australia, or Remote)
3. Select a **job role** from the dropdown, or type a custom keyword
4. Pick a **location** from the dropdown, or type a custom city or town
5. Set how many results you want (default 100), or turn on **Unlimited Mode**
6. Hit **Run**

Export your results as **JSON, CSV, or Excel** directly from the Apify dashboard.

## Input Options

| Setting | Default | What It Does |
|---------|---------|-------------|
| **Job Role Preset** | Software Engineer | Pick from 25 popular roles or choose "Custom" |
| **Custom Keyword** | | Type your own search keyword (overrides preset) |
| **Location Preset** | London | Pick from 27 cities across UK/US/EU/AU or "Remote" |
| **Custom Location** | | Type your own location (overrides preset) |
| **Country** | UK | Determines which boards are auto-selected, the proxy region and currency |
| **Max Results** | 100 | Total jobs to return. If a board comes up short, the others are asked for more so you get the number you requested |
| **Unlimited Mode** | Off | No cap; results are saved page by page so you keep everything if you stop early |
| **Minimum Salary** | | Annual salary floor, applied on the boards and again on the parsed numbers |
| **Job Type** | All | Permanent, Temporary, Contract, or Part-time |
| **Posted Within (days)** | | Only keep listings posted in the last N days |
| **Job Boards** | Auto | Leave empty to auto-select based on country, or pick specific boards |
| **Remove Duplicates** | On | Removes the same job appearing on multiple boards |
| **Salary Benchmarks** | Off | Output salary summary statistics per role/location |
| **Reed API Key** | | Optional. Reads Reed through its official API (includes descriptions) |
| **Adzuna App ID / Key** | | Optional. Enables the Adzuna board |
| **USAJobs API Key / Email** | | Required for the USAJobs board |
| **Max pages per board** | 40 | Hard cap on pages fetched per board; mainly bounds Unlimited Mode |
| **Proxy** | Residential | Residential (recommended), Datacenter (cheaper, more blocks) or None |

## Country Defaults

When you leave the boards selection empty, boards are auto-picked based on country:

| Country | Boards |
|---------|--------|
| UK | Reed, Totaljobs, CV-Library, CWJobs, Indeed UK, GOV.UK Find a Job, Adzuna |
| US | USAJobs, Indeed US, Adzuna, RemoteOK |
| Germany | Indeed DE, Adzuna, Arbeitnow |
| France | Indeed FR, Adzuna |
| Netherlands | Indeed NL, Adzuna |
| Australia | Indeed AU, Adzuna |
| Remote | RemoteOK, Arbeitnow, Adzuna |

## Free API Keys (Optional but Recommended)

API boards never get blocked and are the cheapest sources to run. All three keys are free.

**Reed** (recommended for UK runs): sign up at [reed.co.uk/developers](https://www.reed.co.uk/developers) and paste the key into **Reed API Key**. Reed then returns 100 jobs per request with full descriptions. Without a key Reed is still scraped from its web pages, just without descriptions.

**Adzuna**: register at [developer.adzuna.com](https://developer.adzuna.com), create an app and paste the **App ID** and **App Key**. Without keys Adzuna is skipped.

**USAJobs**: request a key at [developer.usajobs.gov/apirequest](https://developer.usajobs.gov/apirequest/) and enter the key plus the email you registered with. The USAJobs API rejects requests without a key, so this board is skipped until one is provided.

## How Boards Are Fetched (and Why It Is Cheap)

* Every board is tried over plain HTTP first. Reed, Totaljobs, CWJobs and GOV.UK all serve their results server-side, so a page costs about a second and a few dozen kilobytes.
* A headless browser is only launched if a board blocks the HTTP request, and only for that board. Runs where nothing is blocked never start Chromium.
* API boards (Adzuna, USAJobs, RemoteOK, Arbeitnow, GOV.UK) connect directly with no proxy.
* All boards run at the same time rather than one after another.
* Results are written to the dataset in batches rather than one row at a time.

Indeed and CV-Library sit behind aggressive bot protection (Cloudflare challenges). They are included as best-effort: the actor tries once per page through the browser and moves on, so a blocked board costs seconds, not minutes. When they are blocked, the other boards are asked for more results to make up the difference.

## Run Statistics

Each run stores a `RUN_STATS` record in its key-value store with per-board counts, pages fetched, fetch mode (http / browser / api), timings and any errors, plus how many rows were dropped as duplicates or by the salary and date filters.

## Use Cases

- **Job seekers**: search once, see results from everywhere
- **Recruiters**: monitor the market for specific roles and locations, filter to the last 7 days
- **Salary benchmarking**: compare pay by role and location with structured stats
- **Market analysis**: track hiring trends across countries
- **Lead generation**: find companies that are actively hiring
- **Remote job hunting**: RemoteOK + Arbeitnow + Adzuna in one go, with `work_mode` on every row

## Tips

- Use the **country selector** to auto-pick the right boards
- Add a free **Reed API key** for the most complete UK data
- The **salary filter** works best on boards that show salary upfront (Reed, GOV.UK, CV-Library, Adzuna, USAJobs)
- **GOV.UK Find a Job** is great for public sector and NHS roles
- Use **Posted Within** with a scheduled run to build a daily feed of new listings
- Enable **salary benchmarks** when doing market research to get median/mean/percentile stats
