# nextenti.ai scraper

Scrapes healthcare job listings from [nextenti.ai](https://nextenti.ai/search-jobs)
and writes them to the shared HealthCareers.club import schema.

## Data source

nextenti.ai is a client-rendered React SPA (the HTML is an empty `<div id="root">`),
so there is nothing to parse in the page markup. Jobs come from a JSON
microservice API that needs a short-lived **anonymous** bearer token the site
hands out with no credentials:

1. `GET https://authenticator.nextenti.ai/token`
   → `{"data": {"userId": "...", "accessToken": "gAAAA…"}}` (Fernet token)
2. `POST https://job-maintenance.nextenti.ai/api/search?page=<N>`
   - headers: `Authorization: Bearer <accessToken>`, `userId: <userId>`
   - body: `{}` (an empty filter object returns all jobs)
   - → `{"data": [ …30 jobs… ], "totalCount": 792}`

Pagination is a **fixed 30 jobs/page** (`size` is ignored); results are sorted
**newest-first by `postDate`**, so incremental runs stop as soon as they reach
jobs older than the watermark. Each listing job exposes: `jobId`, `jobTitle`,
`organizationName`, `city`, `country`, `salaryRange` ("66000 - 83000"),
`salaryType` (Monthly/Annual/None), `experience` ("2 - 5 years"), `jobType`,
`profession`, `postDate` (YYYY-MM-DD), `jobDescription`, `organizationLogo`,
`verifiedOrganization`. There is no per-job apply link, so `application_url` is
reconstructed as the site's public detail URL (`/jobs/<slug>--<jobId>`), which
resolves (200) because the SPA reads the trailing job id.

**The listing truncates `jobDescription` to 250 characters.** To get the full
text, the scraper also calls the detail endpoint for each NEW job:

    GET https://job-maintenance.nextenti.ai/job-details?jobId=<jobId>
        (same anonymous Bearer + userId headers)

which returns the complete description (plus cleaner integer
`experienceMin`/`experienceMax`). This is **on by default**; pass
`--no-details` to skip it and keep the fast 250-char preview. Because details
are fetched only for jobs not already stored, the first run makes ~1 extra
request per job (~10 min for 30 days of jobs) but daily incremental runs add
only a handful.

`robots.txt` allows `/search-jobs` (only auth/candidate/corporate pages are
disallowed).

## Filtering & classification (per ../../instructions/master-scraper-spec.md)

- **No salary filter** — salaries are captured, never filtered on. Blank /
  undisclosed salaries stay blank.
- **Classification (taxonomy migration 2026-08-25)**: every candidate job
  goes through the shared two-level classifier —
  `_shared/classification.classify_job(title, skills, description)` — with
  the source `profession` field passed as the `skills` signal only (it no
  longer decides the category; it stays in the rich CSV as a raw source
  column). The verdict fills `category` ("Non Clinical" | "Public Health"),
  `sub_category` (20-way split) and the trace columns (`role_family`,
  `all_families`, `family_scores`, `family_confidence`, `matched_in`,
  `needs_review`). This is a clinical board, so most listings (doctors,
  nurses, technicians, billing, sales) are now out of scope and dropped,
  counted `excluded_out_of_scope` — intended. The 2026-08-25 stored-data
  reclassification kept 1 of 196 rows and moved 195 to `out-of-scope.csv`
  (reversible). `needs_review == True` rows are kept AND appended to
  `needs_review.csv` (dedup on job_id). Classification runs after the
  detail fetch so the full description is a signal.
- **company_type**: `pharma` for pharma/CRO/lab/diagnostics names, else `hospital`.
- **Time window**: first run keeps the last 30 days (`INITIAL_WINDOW_DAYS`);
  later runs keep only jobs newer than the newest stored `postDate` minus
  `WATERMARK_GRACE_DAYS` (2), stopping pagination early on the first fully-old
  page.

## Usage

```bash
pip install -r requirements.txt

# unit tests (parsers, classifier, cutoff)
python test_filters.py

# small test run: first 2 pages
python scraper.py --max-pages 2

# full run (first time: last 30 days; after that: only new jobs)
python scraper.py
```

Options: `--output PATH` (rich CSV, default `nextenti_jobs.csv`),
`--max-pages N`, `--run-date DD-MM-YYYY` (jobs_csv folder, default today),
`--verbose`. Tunables at the top of `scraper.py`: `INITIAL_WINDOW_DAYS`,
`WATERMARK_GRACE_DAYS`, `REQUEST_DELAY_SECONDS`, `MAX_RETRIES`.

## Outputs

- `nextenti_jobs.csv` — rich cumulative store (dedup key `job_id`), the source
  of truth for the incremental watermark. Not committed as a deliverable.
- `../../jobs_csv/<DD-MM-YYYY>/nextenti.csv` — the same jobs in the 23-column
  `CLUB_COLUMNS` contract imported from `_shared/classification.py`
  (`is_active`/`expires_at` retired; `qualification` extracted from the
  description, never inferred), the actual HealthCareers.club deliverable.
- `needs_review.csv` — in-scope rows whose title looks like a different
  profession (kept AND flagged, never silently dropped).
- `out-of-scope.csv` — rows the shared classifier ruled out during the
  one-off stored-data reclassification (reversible).

A run prints a summary: scanned, excluded-old, excluded-out-of-scope,
needs_review, new, duplicates.
Re-running the same day adds 0 new rows (idempotent).
