# propharma scraper

Scrapes ProPharma Group's careers board from the **Workday CXS JSON API** at
`propharmagroup.wd1.myworkdayjobs.com` (tenant `propharmagroup`, site
`ppgcareers`). ProPharma is a pharmacovigilance / regulatory / medical
information consultancy, so about half the board is in-scope. Small board:
~67 open postings on 2026-08-27.

## Data source

- **Listing**: `POST /wday/cxs/propharmagroup/ppgcareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400); `total` is only
  reliable on the offset=0 page.
- **Detail**: `GET /wday/cxs/propharmagroup/ppgcareers/job/<externalPath>`
  → full HTML description, exact `startDate`, `timeType`, country
  descriptor + ISO `alpha2Code`, canonical `externalUrl`.
- **robots.txt**: `Allow: /ppgcareers/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Quirks

- **The default ordering is NOT strictly newest-first**: the probe found a
  "Posted 30+ Days Ago" posting pinned *above* "Posted Today" rows. Boards
  with ≤200 postings are therefore walked **completely** every run (4
  listing pages here) instead of early-stopping on old pages — the time
  window still decides which ids get a detail fetch, so a full walk in
  steady state is 4 listing requests plus one detail per genuinely new
  posting.
- **Requisition ids contain a space** (`JR 10125`) — they are treated as
  opaque strings everywhere.
- **Locations are usually country-only** (`Netherlands`, `United States`,
  `Japan`) — there is no city to extract, so `city` is blank unless the
  posting is remote (then `Remote`, himalayas convention).
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank.

## Window & classification

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. Out-of-window ids are recorded in
`seen_old_ids.csv` and never detail-fetched; the detail's exact `startDate`
re-checks the window after the fetch.

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` (no curated
skills field on Workday CXS). `in_scope: False` → `out-of-scope.csv`;
`needs_review` (including non-English DE/FR/ES descriptions) kept and
logged to `needs_review.csv`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
python test_filters.py                            # unit tests (58)
```

Outputs: `propharma_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/propharma.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
