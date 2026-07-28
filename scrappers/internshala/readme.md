# internshala scraper

Scrapes healthcare job listings from [internshala.com/jobs](https://internshala.com/jobs/)
into a deduplicated cumulative CSV plus the HealthCareers.club 22-column
import file.

## Data source

internshala.com is a general jobs portal, so healthcare filtering happens at
the source by crawling only healthcare-related category listing pages:

| kind | slugs |
|------|-------|
| core healthcare | `hospitals-healthcare`, `medical`, `medicine`, `nurse`, `pharmacist`, `pharma`, `pharmaceutical`, `dietetics-nutrition` |
| adjacent (kept + flagged) | `psychology`, `biotechnology`, `biotech`, `bioinformatics`, `biology` |

- **Listing pages** `https://internshala.com/jobs/<slug>-jobs/` and
  `.../page-N/` are server-rendered HTML cards (50 on page 1, 40 after).
  Cards carry title, company, locations, annual-CTC salary, experience and a
  coarse posted-age string ("Today", "3 weeks ago").
- **Detail pages** `/job/detail/<slug>` embed a schema.org **JobPosting
  JSON-LD** with the authoritative data: exact `datePosted`, full
  description, `baseSalary` (INR, YEAR/MONTH), `employmentType`,
  `jobLocation`, `validThrough`, skills. Fetched for NEW in-window jobs only.

### robots.txt compliance

Checked at startup with `urllib.robotparser`. For generic agents the site
disallows `/api/`, `/job/search/`, `/job/details/` (plural — the singular
`/job/detail/` pages used here are allowed), any URL containing `?` or `,`
(enforced by an extra guard on every request). The JSON search API is off
limits, hence HTML parsing.

## Quirks

- A category with no live jobs **301-redirects to `/jobs/`** (all jobs).
  The scraper detects the redirect and skips the category — otherwise an
  empty category would crawl the whole site.
- Card posted-ages are coarse; they only pre-filter jobs clearly older than
  the cutoff (with 7 days slack). The exact JSON-LD `datePosted` makes the
  final in-window decision; card-estimated dates are marked
  `posted_date_is_estimate=True` (only when a detail fetch fails).
- The same job appears under several categories; cards are merged by job id
  and all source categories recorded in `site_categories`.
- Card salaries are annual CTC; occasionally a foreign symbol (£) appears —
  captured raw, numeric amounts kept only for INR/USD (club contract).
- Jobs from adjacent categories whose title has no healthcare keyword are
  kept and flagged `needs_review` (never silently dropped).

## Outputs

- `internshala_jobs.csv` — rich cumulative store, dedup key `job_id`
- `../../jobs_csv/<DD-MM-YYYY>/internshala.csv` — HealthCareers.club schema
- `needs_review.csv` — flagged titles from the latest run

## Usage

```bash
# from scrappers/internshala/, venv at repo root
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --max-pages 1 --limit 5   # quick test
../../.venv/bin/python scraper.py --no-details   # card data only (faster)
../../.venv/bin/python test_filters.py           # unit tests
```

First run keeps the last 30 days; later runs keep jobs newer than the stored
watermark minus 2 days grace. Running twice in a row adds 0 rows.
