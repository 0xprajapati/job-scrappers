# Freshersworld healthcare job scraper

Scrapes healthcare jobs from [freshersworld.com](https://www.freshersworld.com),
a general Indian fresher-jobs board, using its two healthcare-specific
category listings as the source-side filter (master spec §2 preferred):

- `https://www.freshersworld.com/jobs/category/health-care-job-vacancies`
- `https://www.freshersworld.com/jobs/category/pharma-job-vacancies`

## Data source

Server-rendered HTML, no login, no JS needed.

**Listing pages** carry 20 cards per page, paginated with
`?limit=20&offset=N`. Each card's container div has machine-readable
`job_id="..."` and `job_display_url="..."` attributes, plus title, company,
location, salary ("100000 - 150000 Monthly"), experience, qualification and
a relative "Posted: N days ago" stamp. An `ItemList` JSON-LD block mirrors
the card URLs.

**Detail pages** embed a schema.org **JobPosting JSON-LD** block with the
exact `datePosted` / `validThrough` (IST timestamps), a clean `title`, full
HTML `description`, `employmentType`, `qualifications`,
`experienceRequirements.monthsOfExperience`, structured `baseSalary`
(INR, min/max, unit `Month`/`Year`) and `jobLocation` postal addresses.
Detail pages are fetched **only for new in-window jobs** (a handful per day
after the first run), so there is no `--enrich` flag — the detail fetch is
the primary source of `posted_date`/`description` and is already minimal.

### robots.txt

`Allow:/` with specific disallows. Everything this scraper touches —
`/jobs/category/*` and `/jobs/<slug>-<id>` — is allowed. The disallowed
search/AJAX layer (`/jobsearch`, `/jobs/jobsearch/`, `/jobs/getjobs`,
`/*ajax_*`) is never called, and pagination uses `limit=20` (only
`/*limit=25` is disallowed). Compliance is checked at startup with
`urllib.robotparser` and the run aborts if any target path is disallowed.

## Quirks (verified 2026-08-08)

- The active inventory is tiny: ~22 health-care + ~25 pharma jobs, with
  heavy overlap (medical-rep jobs are tagged with both categories). Every
  run crawls both categories fully; dedup by `job_id` absorbs the overlap.
- Cards are roughly newest-first, but premium/"HOT JOB" cards are pinned to
  the top out of order — ordering is never relied on for early-stopping.
- The card's relative age ("3 days ago", "1 months ago") is an estimate
  only. It is used to skip detail-fetching jobs far outside the window
  (with a 5-day safety margin); the keep/exclude decision uses the exact
  JSON-LD `datePosted`.
- The card's salary line shares its CSS class with the qualification line;
  they are told apart by content pattern, not class.
- The `qualifications` field on detail pages can be a huge degree list
  (every bachelor's degree); it is stored verbatim.

## Filtering & classification

- **Healthcare filter**: source-side via the two categories — nothing is
  excluded as non-healthcare by this scraper.
- Titles are still classified into the club enum
  (`doctors`/`nurses`/`pharmacists`/`non_clinical`). Titles matching no
  healthcare pattern, or matching `DENY_TITLE_KEYWORDS` (tech/admin roles
  posted into the category), are **kept**, flagged `needs_review=True` and
  logged to `needs_review.csv` — never silently dropped.
- **Salary** (spec §3): captured, never filtered. `salary_raw` verbatim;
  `salary_min_monthly`/`salary_max_monthly` in INR/month (Year ÷ 12);
  the club export restores original annual amounts with `per_annum`.
  Missing salary → `"Not Disclosed"`, empty numerics.
- **Time window** (spec §4): first run = last 7 days
  (`INITIAL_WINDOW_DAYS`); later runs = newest stored `posted_date` minus 2
  days grace (`WATERMARK_GRACE_DAYS`). `--since YYYY-MM-DD` overrides.

## Outputs

- `freshersworld_jobs.csv` — rich cumulative store (dedup key
  `(source, job_id)`), source of truth for the watermark.
- `../../jobs_csv/<DD-MM-YYYY>/freshersworld.csv` — the same jobs in the
  HealthCareers.club 22-column schema (rewritten every run).
- `needs_review.csv` — titles the classifier couldn't confidently place.

## Usage

```bash
# from scrappers/freshersworld/, using the shared venv at repo root
../../.venv/bin/python freshersworld_scraper.py            # daily run
../../.venv/bin/python freshersworld_scraper.py --since 2026-07-09  # backfill
../../.venv/bin/python freshersworld_scraper.py --max-pages 1 --limit 3  # smoke test
../../.venv/bin/python test_filters.py                     # unit tests
```

Flags: `--output`, `--max-pages` (per category), `--limit`, `--run-date`,
`--since`, `--verbose`.

Etiquette: descriptive User-Agent with contact URL, ≥1s between requests,
exponential backoff (3s → 24s) on 429/5xx, per-job fail-soft (one bad card
or detail page never crashes the run).
