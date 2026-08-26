# DoctHub Healthcare Job Scraper

Scrapes healthcare job listings from [jobs.docthub.com](https://jobs.docthub.com),
filters them by role type and salary, and appends new jobs to a deduplicated CSV.
Re-runnable: every run only adds jobs it hasn't seen before, so it can be
scheduled as-is.

## How it gets the data

DoctHub is a Next.js app backed by a **public JSON API** (no HTML parsing, no
headless browser needed):

```
GET https://api.docthub.com/jobcenter/jobs?pageNumber=N&pageSize=100&categories=<id>
GET https://api.docthub.com/jobcenter/jobs/facets          # category list + counts
GET https://api.docthub.com/jobcenter/jobs/<code>          # job detail (--enrich)
```

Job objects are fully structured: numeric salary with a `Monthly`/`Yearly` type
(undisclosed salaries appear as type `"0"` with zero amounts), exact publish
timestamps, and a `code` slug ending in the job ID (`dermatologist-J120239`).
The 18 job categories exactly partition all ~16k jobs, so the scraper crawls
**per category** — every job carries its raw facet in `source_category`, but
the facet never decides anything.

## Classification (taxonomy migration 2026-08-25)

1. **The one classifier**: every scanned job goes through
   `classify_job(title, skills, description)` from
   `_shared/classification.py` — the old `INCLUDE_CATEGORIES` /
   `ALLOW_TITLE_KEYWORDS` / `DENY_TITLE_KEYWORDS` machinery is gone. The
   verdict alone decides keep/drop: `in_scope == False` rows are counted
   `excluded_out_of_scope` and never exported. In-scope rows carry
   `category` ("Non Clinical" | "Public Health"), `sub_category` (one of
   the 20 shared sub-categories) and the score-trace columns
   (`role_family`, `all_families`, `family_scores`, `family_confidence`,
   `matched_in`, `needs_review`). `needs_review == True` rows are kept AND
   logged to `needs_review.csv`. The only remaining `EXCLUDE_CATEGORIES`
   entries (Engineering / Maintenance, Housekeeping) are pure crawl-side
   scoping to save requests. Note: this board is overwhelmingly
   bedside/clinical — the 2026-08-25 stored-data reclassification kept 21
   of 1,735 rows (the rest moved to `out-of-scope.csv`, reversible).
2. **Time window (no salary filter)**: salaries are captured and normalized to
   INR/month but never filtered on; undisclosed salaries stay as blank fields.
   The first run keeps jobs posted in the last `INITIAL_WINDOW_DAYS` (30);
   every later run keeps only jobs newer than the newest `posted_date` already
   in the CSV minus `WATERMARK_GRACE_DAYS` (2) of overlap, and pagination
   stops early once a page is entirely older (listings are newest-first) —
   so daily runs are fast. See `../specs/master-scraper-spec.md`.

## Setup & usage

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Unit tests (salary parser + classifier, incl. the spec's worked examples)
./.venv/bin/python test_filters.py

# Small test run: 2 pages of 50 per category
./.venv/bin/python docthub_scraper.py --max-pages 2 --page-size 50 --output docthub_jobs_sample.csv

# Full crawl (~110 requests, a few minutes at the default 1 req/sec)
./.venv/bin/python docthub_scraper.py

# Full crawl + fetch each NEW job's detail page for description/apply_url
./.venv/bin/python docthub_scraper.py --enrich
```

Options: `--output PATH` (default `docthub_jobs.csv`), `--max-pages N` (per
category, for testing), `--page-size N` (default 100), `--enrich` (off by
default; only enriches jobs that are new *and* passed both filters), `--verbose`.

Tunables at the top of the script: `INITIAL_WINDOW_DAYS`,
`WATERMARK_GRACE_DAYS`, crawl-side `EXCLUDE_CATEGORIES`,
`REQUEST_DELAY_SECONDS`, `MAX_RETRIES`, `PAGE_SIZE`, `USER_AGENT`.

## Output

- `docthub_jobs.csv` — one row per in-scope job, deduplicated by `job_id`.
  Columns: `job_id, title, company, location, experience_min_years,
  experience_max_years, salary_raw, salary_min_monthly, salary_max_monthly,
  salary_period_original (P.M/P.A), job_type, source_category (raw docthub
  facet), category ("Non Clinical" | "Public Health"), sub_category,
  role_family, all_families, family_scores, family_confidence, matched_in,
  needs_review, posted_date (YYYY-MM-DD), job_url, scraped_at`
  (+ `description, apply_url` with `--enrich`).
- `../../jobs_csv/<DD-MM-YYYY>/docthub.csv` — the 23-column
  HealthCareers.club export (`CLUB_COLUMNS` imported from
  `_shared/classification.py`; `is_active`/`expires_at` retired;
  `qualification` extracted from the description, never inferred),
  regenerated from the full rich store each run.
- `needs_review.csv` — in-scope rows whose title looks like a different
  profession (kept AND flagged, deduplicated).
- `out-of-scope.csv` — rows the shared classifier ruled out during the
  one-off stored-data reclassification (reversible).
- A run summary is printed: scanned / excluded per reason (including
  `excluded_out_of_scope`) / new / duplicates.

## Idempotency

On startup the scraper reads the output CSV and collects known `job_id`s; only
new IDs are appended, and existing rows are never modified. Running it twice in
a row adds 0 rows. Deleting the CSV starts fresh.

## Etiquette

Checks `robots.txt` on both hosts before crawling (aborts if disallowed), sends
a descriptive User-Agent with contact address, waits 1s between requests, and
retries failures with exponential backoff (3s → 24s). Note the site's
robots.txt disallows many individual *expired* job slug pages; `--enrich` uses
the API host (which publishes no robots.txt) rather than those HTML pages.

## Scheduling

**cron (macOS/Linux)** — daily at 07:30:

```cron
30 7 * * * cd /Users/gaganakki/Documents/SahiLoan/HealthCareers && ./.venv/bin/python docthub_scraper.py >> scraper.log 2>&1
```

(On macOS, prefer a `launchd` agent if the Mac may be asleep at the scheduled
time; cron jobs don't run retroactively.)

**Windows Task Scheduler**: create a Basic Task → daily → Action "Start a
program" with program `C:\path\to\.venv\Scripts\python.exe` and arguments
`docthub_scraper.py`, "Start in" set to the project folder.

**GitHub Actions** — commit this folder to a repo and add
`.github/workflows/scrape.yml`:

```yaml
name: scrape-docthub
on:
  schedule:
    - cron: "0 2 * * *"   # 02:00 UTC daily
  workflow_dispatch:
jobs:
  scrape:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: python docthub_scraper.py
      - run: |
          git config user.name "scraper-bot"
          git config user.email "actions@github.com"
          git add docthub_jobs.csv needs_review.csv
          git diff --cached --quiet || git commit -m "Update job data"
          git push
```
