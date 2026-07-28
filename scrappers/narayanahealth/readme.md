# Narayana Health scraper

Scrapes job listings from **jobs.narayanahealth.org** — the careers portal
of Narayana Hrudayalaya Limited (Narayana Health hospital group).
`www.narayanahealth.org/careers` is a dead link; the live portal is the
`jobs.` subdomain, an SAP SuccessFactors Career Site Builder (RMK) site.

## Data source

robots.txt disallows the JSON backends (`/services/`, `/applybutton/`, …)
but allows `/search/` and job detail pages, so the scraper uses the
server-rendered HTML (master spec §1, option 2/4):

- **Listing** — `GET /search/?q=&sortColumn=referencedate&sortOrder=desc&startrow=N`
  (10 rows/page, newest first). Each `<tr class="data-row">` carries title,
  job URL, location (`City, ST, CC, Postal`), posted date (`24 Jul 2026`)
  and the requisition id.
- **Detail pages** (new jobs only; `--no-details` skips) — schema.org
  JobPosting microdata: `datePosted`, `validThrough` (→ `expires_at`),
  `hiringOrganization`, plus the full description in
  `<span class="jobdescription">`.

## Quirks

- **No salaries anywhere** on the site → `salary_raw` is always
  `Not Disclosed`, numeric salary fields stay empty (spec §3).
- No employment-type field → `job_type` defaults to `full_time`.
- The visible "requisition id" (e.g. 14998) is display-only; dedup uses the
  stable numeric id from the job URL (e.g. 58185744).
- Hospital chain's own ATS → healthcare at source; nothing is dropped.
  Generic corporate titles with no healthcare word (e.g. "Junior
  Executive") are kept but flagged `needs_review` and logged to
  `needs_review.csv`.
- Newest-first sort lets the daily run stop as soon as a page is entirely
  older than the watermark cutoff.

## Usage

```bash
python scraper.py                 # incremental daily run (with descriptions)
python scraper.py --max-pages 2   # quick test run
python scraper.py --no-details    # skip detail pages (no descriptions)
python test_filters.py            # unit tests
```

## Outputs

- `narayanahealth_jobs.csv` — rich cumulative store (dedup key: URL job id)
- `../../jobs_csv/<DD-MM-YYYY>/narayanahealth.csv` — HealthCareers.club
  22-column schema
- `needs_review.csv` — flagged titles from the latest run
