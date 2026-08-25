# Freshersworld healthcare job scraper

Scrapes healthcare jobs from [freshersworld.com](https://www.freshersworld.com),
a general Indian fresher-jobs board, using four healthcare/pharma category
listings as **crawl-side scoping** (they save requests; they do not label
anything):

- `https://www.freshersworld.com/jobs/category/health-care-job-vacancies`
- `https://www.freshersworld.com/jobs/category/pharma-job-vacancies`
- `https://www.freshersworld.com/jobs/category/regulatory-affairs-job-vacancies`
- `https://www.freshersworld.com/jobs/category/research-job-vacancies`

The keep/drop and labeling decision belongs to the shared two-level
classifier — see [Classification](#classification-shared-not-local).

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

## Classification (shared, not local)

Every candidate goes through
`_shared/classification.classify_job(title, skills, description)`:

- **`skills`** = the site's own category slugs, humanized by
  `humanize_fw_categories()` (`regulatory-affairs-job-vacancies` →
  `regulatory affairs`). This is the only curated role signal Freshersworld
  offers and it earns its keep: real RA openings are advertised under plain
  titles like "Executive" or "Manager" at pharma companies, invisible to any
  title keyword. The raw slugs stay in the rich CSV's `fw_categories` source
  column and never decide the category.
- **`description`** = the detail page's JSON-LD description, HTML-stripped.
- `in_scope == False` → the row is **dropped** and counted as
  `excluded_out_of_scope` in the run summary. Most of the health-care
  category is bedside/allied (Staff Nurse, Physiotherapist, Lab Technician)
  and goes; what survives is clinical research, pharmacovigilance, regulatory
  affairs, medical coding, medical writing and the public-health families.
- `needs_review == True` → the row is **kept** and appended to
  `needs_review.csv`.

The rich CSV records `category` (`Non Clinical` / `Public Health`),
`sub_category`, `role_family` and the `all_families` / `family_scores` /
`family_confidence` / `matched_in` score trace. This scraper defines **no**
category regexes, profession enums, ALLOW/DENY title lists or fallback maps —
the old `classify_category()` and `DENY_TITLE_KEYWORDS` are gone. The only
local classifier left is `classify_company_type()` (`hospital` | `pharma`),
which fills the club's separate `company_type` field and is not a category.

**Crawl slugs kept as-is.** None of the four can only yield vetoed bedside
jobs: `health-care` and `pharma` both carry real medical-coding / medical-rep
/ PV openings alongside the nursing noise, and `regulatory-affairs` /
`research` are almost entirely in scope. There is no `nurse`-only slug to
prune.

## Filtering

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
  HealthCareers.club schema, the 22 shared `CLUB_COLUMNS` (rewritten every
  run). `is_active` / `expires_at` are retired; `validThrough` stays in the
  rich CSV's `valid_through` column.
- `needs_review.csv` — in-scope rows whose title looks like another
  profession (kept, flagged).
- `out-of-scope.csv` — rows a reclassification moved out of the rich store;
  reversible, never silently discarded.

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
