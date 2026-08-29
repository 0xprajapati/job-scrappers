# veristat scraper

Scrapes Veristat's external careers board from the **Workday CXS JSON
API** at `veristat.wd503.myworkdayjobs.com` (tenant `veristat`, site
`Veristatcareers`). ~19 open postings on 2026-08-28 — a small CRO /
regulatory consultancy, Medical-Writer-heavy and ~70% in-scope for this
fleet (medical writing, regulatory affairs, clinical trial management).
Tiny board (≤ EXHAUSTIVE_BOARD_MAX): every run walks the whole listing —
one or two listing pages — and the incremental time window gates the
detail fetches.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/veristat/Veristatcareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id in `bulletFields`
  (`R-202607825`-style, with a hyphen). `total` is only reliable on the
  offset=0 page. `searchText` is never used for scoping (the
  Piramal-mirage lesson).
- **Detail**: `GET /wday/cxs/veristat/Veristatcareers/job/<externalPath>`
  → full HTML description, exact `startDate`, `timeType`, `remoteType`,
  country descriptor + ISO `alpha2Code`, canonical `externalUrl` (used
  verbatim).
- **robots.txt**: `Allow: /Veristatcareers/`, `Disallow: /refreshFacet/`
  only. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Quirks vs the other Workday scrapers

- **Fully custom facets**: the listing's facets array is a
  `CF_LRV_-_Job_Posting_Anchor_-_*_Extended` set — no standard
  `jobFamilyGroup` facet at all. Irrelevant to the crawl (the whole board
  is walked with empty `appliedFacets`), but worth knowing before anyone
  tries facet-scoping this tenant.
- **State/country-anchored locations, never a US city**: `North
  Carolina`, `Massachusetts`, `Australia`, `Manila`, `3 Locations`. Bare
  US state segments are dropped by the city parser (a state is not a
  city), so US rows export a blank city — or `Remote`, since…
- **`remoteType` says "Fully Remote"** (not plain "Remote") — remoteness
  is a substring test on the label, and most postings here are remote.
- Requisition ids keep their internal hyphen (`R-202607825`) — the
  externalPath-tail fallback would mangle them, another reason
  `bulletFields` wins.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary
  columns blank.

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

Outputs: `veristat_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/veristat.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
