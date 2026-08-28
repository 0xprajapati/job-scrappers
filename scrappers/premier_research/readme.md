# premier_research scraper

Scrapes Premier Research's external careers board from the **Workday CXS
JSON API** at `premierresearch.wd12.myworkdayjobs.com` (tenant
`premierresearch`, site `PremierResearch`). ~73 open postings on
2026-08-28 — a mid-size CRO, roughly a third in-scope for this fleet
(CRAs, clinical operations, medical scientists) among back-office FSP
roles (site payments, HR, IT) the shared classifier drops. Small board
(≤ EXHAUSTIVE_BOARD_MAX): every run walks the whole listing; the
incremental time window still gates the detail fetches.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/premierresearch/PremierResearch/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id in `bulletFields`;
  `total` is only reliable on the offset=0 page. `searchText` is never
  used for scoping (the Piramal-mirage lesson).
- **Detail**: `GET /wday/cxs/premierresearch/PremierResearch/job/<externalPath>`
  → full HTML description, exact `startDate`, `timeType`, `remoteType`,
  country descriptor + ISO `alpha2Code`, canonical `externalUrl` (used
  verbatim).
- **robots.txt**: allows the site paths, disallows only `/refreshFacet/`.
  It also lists **sibling Workday sites** (`Remarque` etc.) in its sitemap
  lines — different published boards on the same tenant that this scraper
  never touches. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Quirks vs the other Workday scrapers

- **Country-only locations**: `India`, `United States of America`,
  `Bulgaria` — no city anywhere on most postings, so `city` is usually
  blank (or `Remote`: the detail's `remoteType` often says "Remote" even
  when the listing shows a bare country). Regional aggregates appear as
  externalPath segments like `Regional_India_48`.
- The listing rows do **not** carry `remoteType` (unlike worldwide_ct);
  only the detail does.
- Titles can drift between listing and detail on reposts ("Fixed Term 6
  Months" listed vs "Fixed Term 9 Months" in the externalPath/detail) —
  the detail title wins.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary
  columns blank. `hiring_org` keeps the per-country legal entity ("30
  Premier Research Group (India) Private Limited"); `company` is the
  brand.

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

Outputs: `premier_research_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/premier_research.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
