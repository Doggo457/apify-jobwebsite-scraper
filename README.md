# UK Jobs Board Scraper

Search **7 major UK job boards** in a single run and get one clean, deduplicated dataset. No more checking each site manually — just enter a keyword and location, and get structured results from across the UK jobs market.

**Only ~$2 per 500 results.**

## Job Boards Covered

| Board | What It Covers |
|-------|---------------|
| **Reed.co.uk** | UK's #1 job site — huge range of industries |
| **Totaljobs.com** | 280,000+ live jobs across all sectors |
| **CV-Library.co.uk** | 120,000+ jobs, strong outside London |
| **CWJobs.co.uk** | IT & Tech specialist roles |
| **Indeed UK** | Global aggregator with massive reach |
| **GOV.UK Find a Job** | Public sector & government roles |
| **Adzuna** | Smart search engine — requires free API key |

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

Salaries are automatically parsed into min/max numbers so you can filter and sort easily. Duplicates posted across multiple boards are removed automatically.

## How to Use

1. Click **Start** on the actor page
2. Enter your **job keyword** (e.g. "data analyst", "nurse", "marketing manager")
3. Pick a **location** from the dropdown, or type a custom city/town
4. Set how many results you want (default: 50)
5. Hit **Run** — results are ready in a few minutes

Export your results as **JSON, CSV, or Excel** directly from the Apify dashboard.

## Input Options

| Setting | Default | What It Does |
|---------|---------|-------------|
| **Job Keyword** | software engineer | The role or skill you're searching for |
| **Location** | United Kingdom | Region or city — pick from the list or enter your own |
| **Max Results** | 50 | Total jobs to return (up to 1,000) |
| **Minimum Salary** | — | Only show jobs paying at least this much per year (GBP) |
| **Job Type** | All | Filter by Permanent, Temporary, Contract, or Part-time |
| **Job Boards** | All 7 | Remove boards you don't need to speed things up |
| **Scraping Rounds** | 2 | How many passes over the boards — more rounds = more results but longer run |
| **Remove Duplicates** | On | Removes the same job appearing on multiple boards |

## Enabling Adzuna (Optional)

Adzuna uses a free API instead of scraping. To include it:

1. Sign up at [developer.adzuna.com](https://developer.adzuna.com) (free)
2. Create an app to get your **App ID** and **App Key**
3. Paste them into the input fields

Without these keys, Adzuna is simply skipped — everything else works fine.

## Cost

Runs cost roughly **$2 per 500 results**. A typical 50-result search costs just a few cents. You can reduce costs by selecting fewer boards or using 1 scraping round instead of 2.

## Use Cases

- **Job seekers** — Search once, see results from everywhere
- **Recruiters** — Monitor the market for specific roles and locations
- **Salary research** — Compare pay across boards with structured salary data
- **Market analysis** — Track hiring trends by keyword, location, or industry
- **Lead generation** — Find companies that are actively hiring

## Tips

- Use **1 scraping round** for quick searches, **2 rounds** if you want to hit your target count
- Remove boards you don't need to speed up the run
- The **salary filter** works best on boards that show salary upfront (Reed, CV-Library, Adzuna)
- **GOV.UK Find a Job** is great for public sector and NHS roles
- Results are sorted by date — newest listings first
