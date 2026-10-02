# Jobs Board Scraper: Indeed, Reed, Adzuna, RemoteOK & More

Search **17+ job boards** across the UK, US, EU and remote markets in a single run. Get one clean dataset where the **same job seen on several boards becomes a single record that cites every source** — with detected apply links, standardised salaries, and smart filters for remote work, job type and recency.

Unlike a plain aggregator, this Actor doesn't just pile listings together. It **merges cross-board duplicates**, spots the **real apply URL** behind each posting (Greenhouse, Lever, Workday, Ashby and more), **standardises every salary** to annual and hourly figures, and can run in **incremental mode** so scheduled runs only return what's new.

## Job Boards Covered

### UK Boards
| Board | What It Covers |
|-------|---------------|
| **Reed.co.uk** | UK's #1 job site, huge range of industries. Add a free Reed API key for full descriptions |
| **Totaljobs.com** | 280,000+ live jobs across all sectors |
| **CV-Library.co.uk** | 120,000+ jobs, strong outside London (best-effort: heavy bot protection) |
| **CWJobs.co.uk** | IT & Tech specialist roles |
| **Indeed UK** | Global aggregator with massive UK reach (best-effort: heavy bot protection) |
| **GOV.UK Find a Job (Work Hub)** | Public sector, NHS & government roles via jobs.service.gov.uk |

### US Boards
| Board | What It Covers |
|-------|---------------|
| **USAJobs.gov** | US federal government jobs (free API key required) |
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
| **The Muse** | Global roles from thousands of companies (free API, no key) |
| **Remotive** | Curated worldwide remote jobs (free API, no key) |
| **Jobicy** | Worldwide remote jobs (free API, no key) |
| **Indeed AU** | Indeed Australia |

**The Muse, Remotive and Jobicy** are free, keyless boards that run over a fast direct connection — they add breadth at almost no cost and are included automatically in the relevant country/remote presets.

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
    "salary_currency": "GBP",
    "salary_period": "annum",
    "salary_annual_min": 30000,
    "salary_annual_max": 40000,
    "salary_hourly": 16.83,
    "job_type": "fulltime",
    "is_remote": false,
    "description": "**About the role**\n\nWe are looking for a passionate junior developer...",
    "snippet": "We are looking for a passionate junior developer...",
    "employment_type": "Permanent",
    "work_mode": "Hybrid",
    "date_posted": "2026-03-05",
    "posted_at": "2026-03-05T00:00:00+00:00",
    "ats": "greenhouse",
    "direct_apply_url": "https://boards.greenhouse.io/techcorp/jobs/12345",
    "sources": [
        { "board": "reed.co.uk", "url": "https://www.reed.co.uk/jobs/junior-software-engineer/12345" },
        { "board": "indeed.co.uk", "url": "https://uk.indeed.com/viewjob?jk=abc123" }
    ],
    "source_count": 2,
    "url": "https://www.reed.co.uk/jobs/junior-software-engineer/12345",
    "job_id": "12345",
    "source": "reed.co.uk"
}
```

Salaries are automatically parsed into min/max numbers with currency detection (GBP, USD, EUR, AUD) based on each job's source country, then standardised to annual and hourly figures. The example above was found on **two boards** and collapsed into a single record that cites both.

## What Makes This Different

Four features turn a raw list of postings into a clean, decision-ready dataset:

### 1. Cross-board dedup that *merges* — one job, one row, every source

Most aggregators show you the same job five times. This Actor recognises when the same role appears on multiple boards — even when the title punctuation, company suffix ("Ltd" vs "Inc") or location wording differ — and **merges them into one record**. That record carries a `sources` array listing every board and URL it was found on, plus a `source_count`. It keeps the richest description and fills in salary, type and remote details from whichever board had them. One job seen on 3 boards = **1 row citing 3 sources**, not 3 near-duplicate rows.

### 2. Direct-apply / ATS detection — skip the aggregator

For every job, the apply URL is checked against known Applicant Tracking Systems — **Greenhouse, Lever, Ashby, Workday, Workable, Recruitee, SmartRecruiters, iCIMS, Taleo, BambooHR, Jobvite, Teamtailor** and more. When one is recognised, you get the ATS name in `ats` and the real company apply link in `direct_apply_url` (otherwise `direct_apply_url` falls back to the listing URL, so it's always populated). Turn on **Resolve Real Apply URL** to follow redirects and uncover even more direct links.

> **Pairs with the ATS Jobs Exporter.** The `ats` and `direct_apply_url` fields drop straight into an ATS-focused workflow — use this Actor to discover which companies are hiring and which ATS they use, then feed those direct links into the **ATS Jobs Exporter** for deeper per-company extraction.

### 3. Salary insights — every pay figure, standardised

Whatever a board provides — a structured range, an hourly rate, or just a number buried in the description text — is normalised into `salary_currency`, `salary_min`, `salary_max` and `salary_period`, then standardised into **`salary_annual_min`**, **`salary_annual_max`** and **`salary_hourly`**. Now you can sort and compare pay fairly across boards and countries, even when one quotes "£450/day" and another "£90k/year". Enable **Salary Benchmarks** for median/mean/percentile stats on top.

### 4. Incremental mode — perfect for scheduled job alerts

Turn on **Incremental Mode** and each run remembers the jobs it has already seen for that search. Schedule it daily or weekly and every run returns **only the new jobs** since last time — no re-processing, no duplicate alerts. The first run returns everything and starts the memory; it's completely safe to run on a fresh schedule.

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
5. Set how many results you want (default: 100), or enable **Unlimited Mode**
6. Hit **Run** — results are ready in a few minutes

Export your results as **JSON, CSV, or Excel** directly from the Apify dashboard.

## Input Options

| Setting | Default | What It Does |
|---------|---------|-------------|
| **Job Role Preset** | Software Engineer | Pick from 25 popular roles or choose "Custom" |
| **Custom Keyword** | — | Type your own search keyword (overrides preset) |
| **Extra Search Terms** | — | Search several roles at once — each term is searched then merged into one deduplicated dataset |
| **Location Preset** | London | Pick from 27 cities across UK/US/EU/AU or "Remote" |
| **Custom Location** | — | Type your own location (overrides preset) |
| **Country** | UK | Determines which boards are auto-selected |
| **Max Results** | 1000 | Total jobs to return, spread across all boards (minimum 100). If a board comes up short, the others are asked for more so you get the number you requested |
| **Unlimited Mode** | Off | Ignores Max Results — every board paginates until it runs out of listings (safety cap: 2,000 jobs per board) |
| **Minimum Salary** | — | Keep only jobs paying at least this much per year (checked against the standardised annual figure) |
| **Contract Type** | All | Employment filter sent directly to boards that support it (Reed, Indeed, Adzuna, GOV.UK) |
| **Job Type** | Any | Normalized filter across every board — Full-time, Part-time, Contract/Freelance or Internship |
| **Remote Only** | Off | Keep only remote roles |
| **Posted Within (hours)** | — | Keep only jobs posted within this many hours (e.g. 24 = last day, 168 = last week) |
| **Search Radius (miles)** | — | Widen the search around your location (Indeed & Adzuna) |
| **Description Format** | Markdown | Format of the `description` field — Markdown, HTML or plain text |
| **Job Boards** | Auto | Leave empty to auto-select based on country, or pick specific boards |
| **Remove Duplicates** | On | Merges the same job across boards into one record with a `sources` list |
| **Resolve Real Apply URL** | Off | Follow redirects to reveal the real company/ATS apply link (slower) |
| **Incremental Mode** | Off | For scheduled runs — return only jobs not seen in previous runs of the same search |
| **Salary Benchmarks** | Off | Output salary summary statistics per role/location |
| **Reed API Key** | — | Optional. Reads Reed through its official JSON API: 100 results per request, full descriptions, never blocked, no proxy |
| **Adzuna App ID / Key** | — | Free API credentials to enable the Adzuna board |
| **USAJobs API Key / Email** | — | Free API credentials to enable the USAJobs board |
| **Max pages per board** | 40 | Hard cap on search pages fetched per board and term; mainly bounds Unlimited Mode |
| **Proxy** | Residential | Residential (recommended), Datacenter (cheaper, more blocks) or None. API boards never use a proxy |

## Country Defaults

When you leave the boards selection empty, boards are auto-picked based on country:

| Country | Boards |
|---------|--------|
| UK | Reed, Totaljobs, CV-Library, CWJobs, Indeed UK, GOV.UK, Adzuna, The Muse |
| US | USAJobs, Indeed US, Adzuna, RemoteOK, The Muse, Remotive |
| Germany | Indeed DE, Adzuna, Arbeitnow, The Muse |
| France | Indeed FR, Adzuna, The Muse |
| Netherlands | Indeed NL, Adzuna, The Muse |
| Australia | Indeed AU, Adzuna, The Muse |
| Remote | RemoteOK, Arbeitnow, Remotive, Jobicy, The Muse, Adzuna |

## Enabling the Reed API (Optional, recommended for UK runs)

Reed is scraped from its web pages by default and that works well, but Reed's search cards carry no job descriptions. A free key from [reed.co.uk/developers](https://www.reed.co.uk/developers) switches Reed to its official JSON API: 100 jobs per request with full descriptions, no bot protection in the way, and no proxy traffic at all. Paste it into **Reed API Key**.

## Enabling Adzuna (Optional)

Adzuna uses a free API instead of scraping — it's the cheapest source to run. To include it:

1. Sign up at [developer.adzuna.com](https://developer.adzuna.com) (free)
2. Create an app to get your **App ID** and **App Key**
3. Paste them into the input fields

Without these keys, Adzuna is simply skipped — everything else works fine. Adzuna supports multiple countries and automatically switches based on your country selection.

## Enabling USAJobs (Optional)

USAJobs.gov requires a free API key for its Search API. To include it:

1. Request a key at [developer.usajobs.gov](https://developer.usajobs.gov) (free)
2. Paste the **API Key** and the **email address** it was issued to into the input fields

Without a key, the USAJobs board is skipped (a warning is logged) — everything else works fine.

## Saving Money

The Actor is built to spend as little as possible by itself:

- **Plain HTTP first, browser only when blocked.** Reed, Totaljobs, CWJobs and GOV.UK all serve their results server-side, so each page costs about a second and a few dozen kilobytes. A headless browser is only launched if a board actually blocks the HTTP request, and only for that board. A typical UK run never starts Chromium at all.
- **Every board runs at the same time** rather than one after another, so the run takes as long as the slowest board, not the sum of all of them.
- **Free-API boards never touch a proxy.** RemoteOK, Arbeitnow, The Muse, Remotive, Jobicy, Adzuna, USAJobs (and Reed with an API key) run over a direct connection.
- **Browser pages are bandwidth-trimmed.** When a browser is needed, images, media, fonts and ad/tracking content are never downloaded, and the page is read as soon as the job cards render.
- **Every board has a time budget**, so a hung site cannot run up compute.

Tips to keep runs even cheaper:

- **Add a free Reed API key** (above). It is the cheapest and most complete way to read the biggest UK board.
- **Prefer the free-API boards** when they cover your market; a run selecting only those is the cheapest way to use this Actor.
- **Lower the memory for API-only runs.** If your board selection contains no browser-capable boards, set the run memory to **1024 MB** in the run options to roughly halve the compute cost. Keep the default (2048 MB) whenever Indeed or CV-Library is included.
- Set a reasonable **Max Results** limit and leave **Unlimited Mode** off unless you really want everything.
- Remove boards you don't need. Indeed and CV-Library sit behind aggressive bot protection and are best-effort: when they are blocked the run moves on after one attempt and the other boards make up the difference, but dropping them saves that attempt.
- Leave **Resolve Real Apply URL** off unless you need it; it adds a network request per job.

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
- **GOV.UK Find a Job (Work Hub)** is great for public sector and NHS roles
- **USAJobs** is great for US federal government positions — just add your free API key
- Results are sorted by date — newest listings first
- Enable **salary benchmarks** when doing market research to get median/mean/percentile stats

## FAQ & Limitations

**Does every board return results for every search?** No — coverage varies per board. Each board is strongest in its home market (Reed and Totaljobs in the UK, USAJobs for US federal roles, Arbeitnow for EU tech, and so on), and niche roles or small towns can return few or no listings on some boards while others deliver plenty. Your Max Results total is spread across the selected boards, so per-board counts will differ.

**Why did USAJobs return no jobs?** USAJobs requires a free API key — see [Enabling USAJobs](#enabling-usajobs-optional) above. Without a key (and the email it was issued to), the board is skipped and a warning is logged; every other board still runs.

**Is Unlimited Mode really unlimited?** It paginates every selected board until it runs out of matching listings, with a safety cap of 2,000 jobs per board (and the Max pages per board setting). Because duplicates are merged across boards at the end, results are written once collection finishes rather than page by page.

**Do I need to set up proxies or anti-blocking?** No — browser rendering, retries, and proxy rotation are handled automatically. There is nothing to configure.

**Can I schedule recurring searches?** Yes — use Apify Schedules to run it daily or weekly, and deliver results via the dataset export, webhooks, or the Apify API. Turn on **Incremental Mode** so each scheduled run returns only the jobs that are new since the last run — ideal for job alerts without duplicate noise.

**How does deduplication work now?** With **Remove Duplicates** on (the default), the same role found on several boards is merged into a single record. That record's `sources` array lists every board and link it appeared on, and `source_count` tells you how many boards carried it — a quick signal of how widely a job is being advertised. Turn the toggle off to keep every board's copy as its own row. Either way, an identical listing (same board, same URL) matched by more than one of your search terms is only ever returned once.

## Use with AI agents (MCP)

This Actor works as a tool for AI agents via the [Apify MCP server](https://mcp.apify.com). Add it to Claude, Cursor, or any MCP-compatible assistant with this server URL:

```text
https://mcp.apify.com?tools=doggo/uk-jobs-board-scraper
```

Then just ask something like *"Find remote Python jobs in the UK posted this week"* — the agent fills in the input, runs the Actor, and reads the results from the dataset. Standard pay-per-result pricing applies to the calling account.
