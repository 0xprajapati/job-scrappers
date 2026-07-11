# Master spec: healthcare job scrapers

This is the single source of truth for every job-site scraper in this project.
To add a new site, the only input needed is the website URL — everything else
follows this spec. Existing scrapers: `Docthub/`, `jobslly/`, `apna/`.

## Goal

Given a job-site URL, deliver a self-contained scraper the user can run
repeatedly (manually or on a daily schedule) without further prompting. Each
run appends the latest healthcare jobs to a deduplicated CSV and refreshes the
HealthCareers.club import files.

## 1. Data-source discovery (first step for any new site)

Investigate before writing code, preferring the most robust source available:

1. **Underlying JSON API** (check network requests / Next.js `__NEXT_DATA__` /
   JS bundles for endpoints) — best.
2. **Embedded structured data** in server-rendered HTML: `__NEXT_DATA__`
   page props, RSC payload JSON, or schema.org JobPosting JSON-LD.
3. **Sitemap + detail pages** when listing APIs are off-limits.
4. **HTML card parsing** only if nothing structured exists.
5. **Playwright/headless** only as a last resort for JS-gated sites.

**robots.txt is binding**: if it disallows an API path (e.g. jobslly.in
disallows `/api/`), do not call it — use what the site exposes to crawlers
instead. Confirm the listing schema on ONE page before building.

## 2. Healthcare-only filter

- PREFER filtering at the source: a category/department field in the API, or
  a healthcare-specific listing URL (e.g. apna's
  `dep_healthcare_doctor_hospital_staff-jobs`).
- FALLBACK: classify titles with configurable `ALLOW_TITLE_KEYWORDS` /
  `DENY_TITLE_KEYWORDS` constants. Titles matching neither list are KEPT,
  flagged `needs_review=True`, and logged to `needs_review.csv` — never
  silently dropped.

## 3. Salary: capture, don't filter

**There is NO salary threshold.** Never exclude a job for its salary or for
not disclosing one.

- When salary is shown, store it: `salary_raw` verbatim, plus normalized
  integer `salary_min_monthly` / `salary_max_monthly` (INR per month;
  K = 1,000, L/LPA = 100,000, P.A ÷ 12) and `salary_period_original`.
- When absent/undisclosed: `salary_raw = "Not Disclosed"`, numeric fields
  empty. Never invent values.
- Where a site splits fixed pay vs. incentives (apna), the normalized fields
  hold the FIXED range; the raw string keeps the displayed one.

## 4. Time window & daily incremental scraping

- **First run** (no existing CSV): keep only jobs posted within the last
  `INITIAL_WINDOW_DAYS = 30` days.
- **Every later run**: keep only jobs newer than the **watermark** = the
  newest `posted_date` already in the CSV, minus `WATERMARK_GRACE_DAYS = 2`
  days of overlap (protects against late-appearing posts; dedup absorbs the
  overlap). Jobs at or after the cutoff are kept, older ones are counted as
  `excluded_old`.
- When the source lists jobs newest-first, stop paginating once a page is
  entirely older than the cutoff (don't crawl the whole site daily).
- Intended cadence: one run per day (see §8).

## 5. Idempotency / dedup

- Dedup key: `(source, job_id)` where `job_id` comes from the job URL / API id.
- Read the existing CSV at startup; append only unseen jobs; never modify or
  duplicate existing rows. Running twice in a row adds 0 rows.
- Print a run summary: scanned, excluded-non-healthcare, excluded-old,
  needs-review, new added, duplicates skipped.

## 6. Output

Each scraper maintains a rich per-source CSV (`<site>_jobs.csv`) with at least:

```
source, job_id, title, company, location, salary_raw, salary_min_monthly,
salary_max_monthly, salary_period_original, job_type, needs_review,
posted_date (YYYY-MM-DD), job_url, scraped_at
```

plus whatever the site exposes (experience, work_mode, education, description,
category, …). `export_club_csv.py` (project root) converts all source CSVs to
the HealthCareers.club 22-column contract
(github.com/0xprajapati/job-scrappers) at `jobs_csv/<DD-MM-YYYY>/<site>.csv`;
`run_daily.sh` runs every scraper then the export in one command.

## 7. Robustness & etiquette

- Check robots.txt at startup and honor it per-URL; abort if the crawl target
  is disallowed.
- Descriptive User-Agent with contact address.
- ≥1s delay between requests; retries with exponential backoff (3s → 24s) on
  429/5xx/network errors.
- Fail gracefully per-field and per-page: log and continue; one bad card or
  page must never crash the run. Tolerate transient empty pages (stop only
  after 3 consecutive).

## 8. Structure, deliverables & scheduling

For each site, a folder `Jobs/<site>/` containing:

- `<site>_scraper.py` — config constants at top, flow:
  config → discover/paginate → parse → healthcare filter → time window →
  dedup/append → write CSV. CLI flags: `--output`, `--max-pages`/`--limit`
  (test runs), `--enrich` (detail-page extras, off by default), `--verbose`.
- `test_filters.py` — unit tests for the salary parser (with worked examples
  from real listings), the classifier, and the cutoff logic; runnable with
  plain `python test_filters.py`.
- `README.md` — data source, quirks, usage.
- `requirements.txt` (`requests`, `pandas`; shared venv at `Jobs/.venv`).

Build order for a new site: discover source → unit-test parsers → wire
pagination/filter/dedup → 2–3 page sample run → full first run (last 30 days).

Daily scheduling (macOS cron example, 08:00):

```cron
0 8 * * * /Users/gaganakki/Documents/SahiLabs/HealthCareers/Jobs/run_daily.sh >> /Users/gaganakki/Documents/SahiLabs/HealthCareers/Jobs/run_daily.log 2>&1
```
