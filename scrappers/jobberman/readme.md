# Jobberman (Nigeria) healthcare scraper

Scrapes healthcare jobs from https://www.jobberman.com/jobs/healthcare and
maintains the per-source rich CSV plus the HealthCareers.club export, per
`instructions/master-scraper-spec.md`.

## Data source

- **Listing**: `https://www.jobberman.com/jobs/healthcare` — server-rendered
  HTML, 16 cards/page, healthcare-industry-filtered at the source, sorted
  newest-first with a few FEATURED cards injected at the top. Cards carry
  title, detail URL, internal id, company, location / job-type / salary
  chips, function ("Sales", "Medical & Pharmaceutical", …) and a coarse
  relative age ("4 days ago").
- **Detail pages**: `https://www.jobberman.com/listings/<slug>` embed a
  schema.org `@graph` whose `JobPosting` node has exact `datePosted`,
  `validThrough`, `employmentType`, `occupationalCategory`, HTML
  description, NGN `baseSalary` (usually monthly, often absent), and
  `experienceRequirements.monthsOfExperience`. Employer name and address
  sit in sibling `Organization`/`PostalAddress` nodes referenced by `@id`.

## robots.txt constraints (binding)

- `/api/` and `/ajax/` are disallowed — the scraper never calls the JSON
  endpoints, only the crawler-visible HTML.
- `/job/` is disallowed but detail pages live under `/listings/` (allowed).
- `Disallow: /*page=*` with explicit `Allow` for `page=2`..`page=10` only:
  **pagination is hard-capped at 10 pages** (~160 jobs). Python's
  robotparser can't evaluate those wildcard rules, so the cap is enforced
  in code (`MAX_PAGES = 10`). With daily runs and a newest-first listing
  this cap is comfortably more than a day's postings.

## Quirks

- Exact posted dates exist only on detail pages. Cards' coarse ages are
  used only as an *optimistic* bound: a card is skipped as old only when
  even the newest date it could mean ("1 month ago" → 28 days) is before
  the cutoff; such slugs go to `seen_old_ids.csv` so later runs skip them.
- Salaries are **NGN**; the club schema's `salary_currency` enum only
  allows INR/USD, so club CSV salary columns stay blank while the rich CSV
  keeps the NGN amounts (`salary_raw`, `salary_min/max`, period, currency).
- The vertical includes sector back-office roles (accountants, drivers,
  sales reps). They are kept as `non_clinical`; titles with no healthcare
  signal in title+company+function are flagged `needs_review` (see
  `needs_review.csv`), never dropped.
- `PostalAddress` fields are shuffled (`streetAddress` holds the state,
  e.g. "Lagos"); the card's location chip is used as the city.
- Contract / "Internship & Graduate" roles map to `full_time` in the club
  CSV (its enum has no such values); the site's label is preserved in the
  rich CSV's `site_job_type` column.

## Usage

```bash
pip install -r requirements.txt
python scraper.py                 # daily incremental run
python scraper.py --max-pages 2   # quick test run
python scraper.py --verbose       # debug logging
python test_filters.py            # offline unit tests
```

## Outputs

| File | Purpose |
| --- | --- |
| `jobberman_jobs.csv` | Rich cumulative store, dedup key = listing slug |
| `../../jobs_csv/<DD-MM-YYYY>/jobberman.csv` | HealthCareers.club 22-column export |
| `seen_old_ids.csv` | Out-of-window slugs (skip re-fetching their details) |
| `needs_review.csv` | Kept-but-unclassified titles for manual review |

Time window: first run keeps the last 30 days; later runs keep jobs newer
than the newest stored `posted_date` minus 2 days of overlap (dedup absorbs
the overlap). Running twice in a row adds 0 rows.
