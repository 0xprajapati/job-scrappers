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
  detail fetches are on by default (`--no-details` skips them but then
  category mapping relies on the title alone).

## Category mapping

Title regexes (nurse / pharmacist / doctor) first, then the Fortis job
family as fallback: Clinicians → `doctors`, Nursing → `nurses`, everything
else → `non_clinical`. "Consultant …" titles count as doctors only in the
Clinicians family. Generic corporate titles in Other Functions with no
healthcare keyword are kept but flagged `needs_review` (logged to
`needs_review.csv`).

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
- `../../jobs_csv/<DD-MM-YYYY>/fortis.csv` — HealthCareers.club 22-column
  schema
- `needs_review.csv` — titles the classifier could not place

Time window per master spec §4: first run keeps the last 7 days; later
runs keep jobs newer than the stored watermark minus 2 days grace.
Pagination stops at the first page entirely older than the cutoff
(newest-first source).
