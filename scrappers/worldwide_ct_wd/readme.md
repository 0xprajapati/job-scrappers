# worldwide_ct scraper

Scrapes Worldwide Clinical Trials' external careers board from the
**Workday CXS JSON API** at `worldwide.wd1.myworkdayjobs.com` (tenant
`worldwide`, site `External`). ~183 open postings on 2026-08-28 — a
mid-size global CRO, CRA-heavy, and roughly two thirds of titles in-scope
for this fleet (clinical operations, project management, biostatistics,
medical writing). Small board (≤ EXHAUSTIVE_BOARD_MAX): every run walks
the whole listing; the incremental time window still gates the detail
fetches, so no facet or early-stop is needed.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/worldwide/External/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label, the requisition id in `bulletFields`, and —
  unusually for the fleet's Workday tenants — a curated `remoteType`
  field. `total` is only reliable on the offset=0 page. `searchText` is
  never used for scoping (the Piramal-mirage lesson).
- **Detail**: `GET /wday/cxs/worldwide/External/job/<externalPath>` →
  full HTML description, exact `startDate`, `timeType`, `remoteType`,
  country descriptor + ISO `alpha2Code`, canonical `externalUrl` (used
  verbatim).
- **robots.txt**: `Allow: /External/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Quirks vs the other Workday scrapers

- **"Virtual" locations with no delimiter**: `Virtual United States
  Michigan` (space-separated — no comma, no hyphen). The city parser
  strips the `Virtual` marker and the country name as *prefixes* before
  segment-splitting, and drops bare US state names/codes; what remains is
  usually nothing, so remote rows export city `Remote` (himalayas
  convention). "Virtual" also counts as a remote marker.
- **`remoteType` is authoritative** for remoteness (`"Remote"`), on both
  the listing row and the detail — used alongside the location-string
  regex.
- `bulletFields` can disagree with the externalPath tail on reposts
  (`JR102535` vs `..._JR102535-1`) — the bulletFields requisition id wins,
  as everywhere in the fleet.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary
  columns blank. `hiring_org` keeps the Workday legal entity ("Worldwide
  Clinical Trials Holdings, Inc."); `company` is the brand.

## Window & classification

The relative `postedOn` label decides the time window before any detail
request (out-of-window ids → `seen_old_ids.csv`, unfetched); the detail's
exact `startDate` re-checks it. First run keeps the last 7 days; later
runs use the newest stored `posted_date` minus 2 days grace.

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` (no curated
skills field on Workday CXS). `in_scope: False` → `out-of-scope.csv` with
the veto breakdown tallied in the run summary; `needs_review` (including
non-English DE/FR/ES descriptions) kept and logged to `needs_review.csv`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (70)
```

Outputs: `worldwide_ct_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/worldwide_ct.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
