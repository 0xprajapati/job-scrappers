# apna.co Healthcare Job Scraper

Scrapes the **Healthcare / Doctor / Hospital Staff** department of
[apna.co](https://apna.co) (a general job board with ~91k jobs; the department
URL pre-filters to ~3,600 healthcare roles, so no keyword classification is
needed) and appends new jobs to a deduplicated CSV. Schema is shared with the DoctHub scraper —
every row carries `source = "apna.co"`, and dedup is on `(source, job_id)`,
so the CSVs can be concatenated into one dataset.

## How it gets the data

Listing pages are Next.js **App Router** pages (apna dropped the old
pages-router markup around Aug 2026 — `__NEXT_DATA__` is gone):

    https://apna.co/jobs/dep_healthcare_doctor_hospital_staff-jobs?page=N

The crawl is two-phase:

1. **Listing sweep** — job cards are plain server-rendered HTML
   (`<a data-testid="job-card" href="/job/…-<id>">` with an `<h2>` title).
   One cheap request per page (25 cards/page, ~144 pages) collects
   URL + title stubs until the first empty page. The listing carries no
   dates or structured salary, and its RSC flight stream has no job JSON.
2. **Detail fetch** — only for stubs that pass the deny-title gate and are
   not already in the CSV. Each detail page embeds the complete job object
   (the same shape the old listing JSON had: **fixed vs. incentive salary
   split** (`fixed_min_salary`/`fixed_max_salary` vs. `earning_potential`),
   description, education, shift, gender, `created_on`/`last_updated`,
   ui_tags, organization) in its `self.__next_f.push` flight stream; the
   scraper reassembles the stream and brace-matches the object out.

`posted_date` is `last_updated` (what apna itself publishes as `datePosted`
in the page's structured data; `created_on` can be months older for re-upped
postings), falling back to `created_on`. Because dates are only known after
the detail fetch and the listing is relevance-ordered, jobs older than the
cutoff are excluded but not stored — they may be re-fetched on later runs
(bounded: active postings expire ~10 days after their last re-up).

The department slug list is a constant (`DEPARTMENT_SLUGS`) — append e.g.
`dep_beauty_fitness_personal_care-jobs` to widen coverage later.

## Salary capture & time window (no salary filter)

- Salaries are already monthly — no conversion. They are captured, never
  filtered on: `salary_raw` stores the displayed range verbatim (may include
  incentives); `salary_min_monthly` / `salary_max_monthly` store the FIXED
  range (`fixed_min_salary`/`fixed_max_salary`, incentives excluded). A ₹0
  floor or missing salary = undisclosed → fields left blank, job kept.
- The first run keeps jobs posted in the last `INITIAL_WINDOW_DAYS` (30);
  every later run keeps only jobs newer than the newest `posted_date` already
  saved minus `WATERMARK_GRACE_DAYS` (2). apna's listing is not strictly
  date-sorted, so all pages are scanned each run and the window is applied
  per job. See `../specs/master-scraper-spec.md`.

## Usage

Uses the shared virtualenv at `../.venv` (create with `python3 -m venv .venv`
in the parent `Jobs/` folder + `pip install -r requirements.txt`):

```bash
# Unit tests (incl. the spec's worked salary examples)
../.venv/bin/python test_filters.py

# Sample run: first 3 pages
../.venv/bin/python apna_scraper.py --max-pages 3 --output apna_jobs_sample.csv

# Full crawl: ~144 listing pages + one detail fetch per NEW job.
# First run ≈ 1.5–2 h at the default 1 req/sec (≈3k details); later runs are
# incremental (known job ids are skipped before the detail fetch) and fast.
../.venv/bin/python apna_scraper.py

# --enrich is now a no-op (detail data incl. role_category is captured on
# every run); the flag is accepted so old cron lines keep working.
```

Options: `--output PATH` (default `apna_jobs.csv`), `--max-pages N`,
`--enrich` (off by default), `--verbose`. Tunables at the top of the script:
`INITIAL_WINDOW_DAYS`, `WATERMARK_GRACE_DAYS`, `DEPARTMENT_SLUGS`,
`REQUEST_DELAY_SECONDS`, `MAX_RETRIES`.

## Output

One row per job inside the time window, deduplicated by `(source, job_id)`
(the numeric ID from the job URL). Columns: `source, job_id, title, company,
location, salary_raw, salary_min_monthly, salary_max_monthly, work_mode,
job_type, experience_raw, english_level, department, role_category, education,
degree_specialisation, shift, gender, posted_date, description, job_url,
apply_url, scraped_at` (+ `company_address` with `--enrich`).

Even without `--enrich`, `education`, `shift`, `gender`, `posted_date`, and
`description` are filled from the listing data (free); enrichment adds
`role_category`, `degree_specialisation`, `company_address`, refines
`education`/`gender` labels, and sets `apply_url`.

The run summary prints: pages scanned, listings seen, excluded-salary, new
added, duplicates skipped. Re-running immediately adds 0 rows.

## Etiquette

Checks robots.txt before crawling (apna.co only disallows `*.infra.apna.co`
hosts), descriptive User-Agent with contact address, 1s delay between
requests, exponential-backoff retries (3s → 24s), and per-card/per-page error
handling that logs and continues instead of crashing.

## Scheduling

Same pattern as the other scrapers — cron example (daily 08:00):

```cron
0 8 * * * cd /Users/gaganakki/Documents/SahiLabs/HealthCareers/Jobs/apna && ../.venv/bin/python apna_scraper.py >> scraper.log 2>&1
```

See the DoctHub README for launchd / Windows Task Scheduler / GitHub Actions
variants.

## Merging sources

All three scrapers share the core columns, so:

```python
import pandas as pd
merged = pd.concat([
    pd.read_csv("../Docthub/docthub_jobs.csv").assign(source="jobs.docthub.com"),
    pd.read_csv("../apna/apna_jobs.csv"),
], ignore_index=True)
merged.drop_duplicates(subset=["source", "job_id"]).to_csv("all_jobs.csv", index=False)
```
