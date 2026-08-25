# reed scraper

Scrapes UK jobs from the **Health & Medicine** and **Scientific** sector
listings on [reed.co.uk](https://www.reed.co.uk/jobs/health-jobs), then
gates them through the shared two-level taxonomy classifier.

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
| `GET /jobs/scientific-jobs?…` | Listing (Scientific — where reed files pharma/clinical R&D) |
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
- **`expiryDate` is real**, and stays in the rich CSV's `expires_date`.
  The club schema's `is_active`/`expires_at` columns were retired by the
  2026-08-25 taxonomy migration, so it is no longer exported.
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
- Most of reed's Health & Medicine sector is care/support work (care
  assistants, support workers, HCAs) plus nurses and locum doctors — all
  **out of scope** under the two-level taxonomy, so the run drops the bulk
  of what it scans. The in-scope yield comes mainly from the Scientific
  listing. `taxonomy_l1/l2` and `sector` are kept in the rich CSV as raw
  source columns and feed the classifier as its `skills` signal.

## Classification (taxonomy migration 2026-08-25)

Reed previously had **no scope gate at all** — every card in the sector
listings was kept and stamped with the old profession enum (in practice
`non_clinical`). It now runs the same gate as the rest of the fleet:

```python
classify_job(jobTitle, "taxonomyLevel1, taxonomyLevel2, jobSector",
             description)
```

- `in_scope == False` → the job is dropped and counted
  `excluded_out_of_scope` in the run summary.
- In-scope rows get `category` (`Non Clinical` | `Public Health`),
  `sub_category`, `role_family` and the `all_families` / `family_scores` /
  `family_confidence` / `matched_in` score trace in the rich CSV.
- `needs_review == True` → kept **and** written to `needs_review.csv`.
- The gate runs **after** `--enrich` fetches the detail page, so the
  classifier reads the full description rather than the snippet. That
  spends detail requests on jobs that are then dropped; it is the honest
  order — a pre-gate on the snippet would be a second, weaker classifier
  making the real keep/drop decision.
- Rows already in the store that no longer classify in scope were moved
  to `out-of-scope.csv` during the one-off migration (reversible).

The club CSV's 22 columns come from `CLUB_COLUMNS` in
`_shared/classification.py`; `qualification` is a grounded extraction from
the description only.

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
| `../../jobs_csv/<DD-MM-YYYY>/reed.csv` | HealthCareers.club 22-column schema (shared `CLUB_COLUMNS`) |
| `needs_review.csv` | In-scope rows whose title looks like a different profession (kept, flagged) |
| `out-of-scope.csv` | Rows the classifier rejected in the one-off migration of the stored data |

## Etiquette

robots.txt checked at startup; ≥1s delay between requests; exponential
backoff (3s → 24s) on 429/5xx; 4xx fail fast; descriptive User-Agent with
contact URL; one bad job/page never crashes the run.
