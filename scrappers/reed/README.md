# reed scraper

Scrapes UK **Health & Medicine** jobs from
[reed.co.uk/jobs/health-jobs](https://www.reed.co.uk/jobs/health-jobs).

Agreed window: **last 10 days** (`INITIAL_WINDOW_DAYS = 10`). The source is
filtered with `datecreatedoffset=LastTwoWeeks` (the tightest available
superset) and the exact 10-day cutoff is applied client-side.

## Data source

Next.js app — every listing page embeds the results in
`<script id="__NEXT_DATA__">` → `props.pageProps.searchResults`
(25 jobs/page, `count`, plus a separate `promotedJobs` array). Plain
numeric pagination via `pageno`. No browser impersonation needed — Reed
serves a plain descriptive User-Agent happily.

| Request | Purpose |
|---|---|
| `GET /jobs/health-jobs?datecreatedoffset=LastTwoWeeks&sortby=DisplayDate&pageno=N` | Listing (sector 36 = Health & Medicine) |
| `GET /jobs/<slug>/<id>` | Detail page (only with `--enrich`) |

Listing fields: `jobId`, `jobTitle`, `jobDescriptionSnippet`,
`displayDate`/`expiryDate` (ISO), `displayLocationName`, `countyLocation`,
`ouName` (posting recruiter/employer), `isFullTime`/`isPartTime`,
`remoteWorkingOption` (On-Site/Remote/Hybrid), `salaryFrom/To`,
`salaryType` (5 = per annum, 1 = per hour), `salaryDescription` (type id),
`salaryCurrencyId` (1 = GBP), `taxonomyLevel1/2`, `isPromoted`, `url`.

Detail adds: `description` (HTML), `jobSalary.displaySalary`
(authoritative), `jobContractType`, `jobLocation.regionName`, `jobSector`,
`isAgency`/`isEmployer`/`isReed`.

## Quirks

- **Listing salary numbers lie for undisclosed salaries**: rows whose
  `salaryDescription` type is `64` ("Competitive salary") still carry
  search-band numbers (e.g. 20000–70000). The scraper drops those as
  `Not Disclosed`; with `--enrich` the detail page's `displaySalary`
  string is used instead. **`--enrich` is the recommended mode** (a 10-day
  window is only ~500 jobs ≈ 9 min at 1 req/s).
- **GBP salaries**: the club schema's `salary_currency` enum only allows
  INR/USD, so the club CSV's salary columns stay empty; verbatim amounts
  and periods live in the rich CSV.
- **`expiryDate` is real** — this is the first scraper that populates the
  club CSV's `expires_at` column.
- **Promoted cards** come in a separate `promotedJobs` array with arbitrary
  dates; they are ingested (deduped) but don't vote on the newest-first
  early stop.
- `displayDate` (not `dateCreated`) is the "posted" date the site displays
  and sorts by — reposted jobs get bumped, which matches what a jobseeker
  sees.
- **robots.txt**: `/jobs/` is allowed; `/api/` is disallowed, so Reed's
  internal/public APIs are never called (master spec: robots is binding).
- `ouName` is the posting organisation — for agency posts (most of the
  board) that's the recruiter (e.g. "Nurse Seekers"), not the end employer.
  `company_kind` in the rich CSV distinguishes agency/employer/reed.
- Many UK health roles are care/support work: `category` maps those to
  `non_clinical`; `taxonomy_l1/l2` and `sector` are kept in the rich CSV
  for finer mapping later.

## Usage

```bash
pip install -r requirements.txt

# sample run: 2 pages with detail enrichment
python reed_scraper.py --max-pages 2 --enrich

# full daily run (recommended)
python reed_scraper.py --enrich

# fast listing-only run (snippet descriptions, listing salaries only)
python reed_scraper.py
```

Flags: `--output`, `--max-pages N` / `--limit N` (test runs), `--enrich`,
`--run-date DD-MM-YYYY`, `--verbose`.

Tests: `python test_filters.py`

## Outputs

| File | What |
|---|---|
| `reed_jobs.csv` | Rich cumulative store, dedup key `job_id`, watermark source |
| `../../jobs_csv/<DD-MM-YYYY>/reed.csv` | HealthCareers.club 22-column schema (with `expires_at`) |
| `needs_review.csv` | Titles the category classifier could not place (kept as `non_clinical`, never dropped) |

## Etiquette

robots.txt checked at startup; ≥1s delay between requests; exponential
backoff (3s → 24s) on 429/5xx; 4xx fail fast; descriptive User-Agent with
contact URL; one bad job/page never crashes the run.
