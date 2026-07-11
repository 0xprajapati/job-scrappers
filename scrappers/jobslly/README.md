# Jobslly Healthcare Job Scraper

Scrapes job listings from [jobslly.in](https://jobslly.in) (India-focused
healthcare/medical job board, ~50 active listings), filters them by role type
and salary, and appends new jobs to a deduplicated CSV. Companion to the
DoctHub scraper in the parent folder — same filtering rules and CSV shape.

## How it gets the data (and why it's different from the DoctHub scraper)

jobslly.in's `robots.txt` **disallows `/api/` for all user agents**, so this
scraper deliberately never calls the site's JSON API. Instead it uses the two
things the site explicitly exposes to crawlers:

1. `https://jobslly.in/sitemap.xml` — lists every job detail URL (updated
   daily). The trailing 8-hex-char slug id (`...-3e71038c`) is the dedup key.
2. Each job page embeds a **schema.org `JobPosting` JSON-LD** block with
   title, company, location, description, `datePosted`, `employmentType`,
   and `baseSalary` — parsed with a regex + `json.loads`, no HTML parsing.

Because dedup happens on the sitemap slug *before* any page fetch, a re-run
only downloads pages for jobs it hasn't already saved. (Pages that were
fetched but *excluded* by the filters are re-checked on each run, which keeps
the script stateless beyond the output CSV; at ~25 pages that costs ~30s.)

## Filtering

1. **Role filter**: jobslly is healthcare-only but includes corporate roles.
   Titles are classified against `ALLOW_TITLE_KEYWORDS` /
   `DENY_TITLE_KEYWORDS` (constants at the top of the script; the ALLOW list
   adds jobslly-typical roles like medical writer, pharmacovigilance, MSL,
   regulatory affairs). DENY drops the job; titles matching *neither* list
   are kept but flagged `needs_review=True` and logged to `needs_review.csv`
   for list tuning.
2. **Time window (no salary filter)**: salaries are captured and normalized
   but never filtered on. Jobslly publishes yearly salaries in **lakhs**
   (`minValue: 18, unitText: "YEAR"` = 18 LPA); values ≥ 1000 are treated as
   raw INR (`LAKH_HEURISTIC_THRESHOLD`); undisclosed stays blank. The first
   run keeps jobs posted in the last `INITIAL_WINDOW_DAYS` (30); later runs
   keep only jobs newer than the newest saved `posted_date` minus
   `WATERMARK_GRACE_DAYS` (2). See `../specs/master-scraper-spec.md`.

## Usage

Uses the shared virtualenv in the parent folder (or any env with
`requirements.txt` installed):

```bash
# Unit tests
../.venv/bin/python test_filters.py

# Full run (~53 pages first time, then only new jobs)
../.venv/bin/python jobslly_scraper.py

# Test run: fetch at most 5 new pages
../.venv/bin/python jobslly_scraper.py --limit 5
```

## Output

- `jobslly_jobs.csv` — one row per passing job, deduplicated by `job_id`.
  Columns: `job_id, title, company, location, salary_raw, salary_min_monthly,
  salary_max_monthly, salary_period_original (LPA/P.A/P.M), job_type,
  needs_review, posted_date, job_url, description, scraped_at`.
  (No experience columns — jobslly's structured data doesn't publish them.)
- `needs_review.csv` — ambiguous titles for keyword-list tuning.
- A run summary is printed (scanned / skipped / excluded / new).

## Etiquette

Honors `robots.txt` (never touches `/api/`, re-checks every URL before
fetching), descriptive User-Agent with contact address, 1s delay between
requests, exponential-backoff retries (3s → 24s).

## Scheduling

Same as the DoctHub scraper — e.g. cron:

```cron
45 7 * * * cd /Users/gaganakki/Documents/SahiLoan/HealthCareers/jobslly && ../.venv/bin/python jobslly_scraper.py >> scraper.log 2>&1
```

See the parent [README](../README.md) for launchd / Task Scheduler / GitHub
Actions variants.
