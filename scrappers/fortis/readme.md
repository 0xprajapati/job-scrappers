# Fortis Healthcare careers scraper

Scrapes jobs for the Fortis hospital chain (fortishealthcare.com/careers).

## Data source

The Fortis careers landing page (`/careers-at-fortis`) links every job
category to Fortis's **Oracle Recruiting Cloud** (Candidate Experience)
site, which exposes a public JSON REST API — no HTML parsing, no headless
browser:

```
https://fa-ermg-saasfaprod1.fa.ocs.oraclecloud.com
  /hcmRestApi/resources/latest/recruitingCEJobRequisitions        (list)
  /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails  (detail)
```

Site number `CX_1`; ~1,200 live requisitions, sorted `POSTING_DATES_DESC`.
`robots.txt` on the Oracle host is a plain 404 (no crawl restrictions);
the fortishealthcare.com host itself is behind a Cloudflare challenge but
is never crawled.

Being the hospital chain's own ATS, every posting is healthcare-industry
at the source. Jobs carry one of five job families (the tiles on the
landing page): Clinicians, Nursing, Technicians, Medical Support, Other
Functions.

## Quirks

- The list endpoint returns an **empty** `requisitionList` unless
  `expand=requisitionList.secondaryLocations` is in the query, and caps
  `limit` at 25.
- **No salary anywhere** in the API → `salary_raw = "Not Disclosed"` on
  every row (master spec §3: capture, never filter, never invent).
- All external description/qualification fields are empty strings in
  practice — Fortis leaves them blank in the ATS. The club CSV falls back
  to `Hiring facility: <name>` so the description column is not empty.
- The ATS category codes contain typos (`TECHINICIANS`).
- The list payload has no job-family info (`JobFamily: null`); the family
  comes from the detail call (`JobFamilyId` / `Category`), which is why
  detail fetches are on by default (`--no-details` skips them, and then the
  classifier only sees the title).

## Classification (shared taxonomy)

Every candidate job goes through the shared classifier
(`scrappers/_shared/classification.py`) after the detail fetch, so the
description is part of the decision. The scraper defines no category rules
of its own:

```python
verdict = classify_job(title, skills=job_family + category_source, description=description)
```

- `in_scope == False` → the job is **dropped** and counted as
  `excluded_out_of_scope` in the run summary. On this hospital-chain ATS that
  is the large majority (Clinicians, Nursing, Technicians): the scope is
  **Non Clinical + Public Health** roles only.
- In-scope jobs get `category` (`Non Clinical` | `Public Health`) and
  `sub_category` (one of the 20 sub-categories), plus the rich-CSV trace
  columns `role_family`, `all_families`, `family_scores`,
  `family_confidence`, `matched_in`.
- The five Fortis job families (Clinicians, Nursing, Technicians, Medical
  Support, Other Functions) no longer decide anything: they stay in the rich
  CSV as the raw `job_family` / `category_source` source columns and travel
  to the classifier only as its curated `skills` signal.
- `needs_review == True` rows are kept **and** appended to `needs_review.csv`.

## Usage

```bash
cd scrappers/fortis
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 2   # quick test
../../.venv/bin/python scraper.py --no-details    # skip detail calls
../../.venv/bin/python test_filters.py            # unit tests
```

Outputs:

- `fortis_jobs.csv` — rich cumulative store (dedup key: requisition `Id`)
- `../../jobs_csv/<DD-MM-YYYY>/fortis.csv` — the 22 `CLUB_COLUMNS` imported
  from `_shared/classification.py`, including `sub_category` and
  `qualification`; the old `is_active`/`expires_at` columns are retired.
  `qualification` is the structured `StudyLevel` when present, else
  `extract_qualification(description)` — never inferred.
- `needs_review.csv` — in-scope rows the classifier flagged for review
- `out-of-scope.csv` — rows the classifier dropped from the rich store during
  the one-off taxonomy migration (reversible archive)

Time window per master spec §4: first run keeps the last 7 days; later
runs keep jobs newer than the stored watermark minus 2 days grace.
Pagination stops at the first page entirely older than the cutoff
(newest-first source).
