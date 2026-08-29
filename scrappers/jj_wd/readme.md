# jj scraper

Scrapes Johnson & Johnson's global careers board from the **Workday CXS JSON
API** at `jj.wd5.myworkdayjobs.com` (tenant `jj`, site `JJ`). ~1,726 open
postings on 2026-08-28 — one of the biggest Workday boards in the fleet.
Supply spans Innovative Medicine and MedTech: clinical research, medical
affairs (MSLs), regulatory, quality/QC, pharmacovigilance, plus a long tail
of sales/supply-chain/IT/finance the shared classifier drops.

The tenant's robots.txt lists three sibling sites — `DisplacedEmployees`,
`BorderUnionHiring`, `NonCompetetiveHiring`. All are internal/special hiring
programmes, not the public brand board, so only `JJ` is crawled.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/jj/JJ/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, `timeType`, a *relative* `postedOn`
  label ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (e.g. `R-095868`). `total` is only reliable on the
  offset=0 page (deeper pages report 0).
- **Detail**: `GET /wday/cxs/jj/JJ/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`
  (on `jobRequisitionLocation`, whose own descriptor is a site code like
  "IN004 Bangalore"), and the canonical `externalUrl`.
  `additionalLocations` can be `null` on this tenant; a `remoteType` field
  exists but is not relied on.
- **robots.txt**: `Allow: /JJ/` (and the three sibling sites),
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

The board posts roughly **100 requisitions a day** — at probe time
(2026-08-28) offset 0–99 was all "Posted Today", offset 100 was already
"Posted Yesterday" and offset 200 "Posted 2 Days Ago". Labels age
monotonically with offset, so **newest-first ordering holds** and the
template's early-stop is sound: the relative `postedOn` label decides the
time window **before** any detail request, out-of-window ids go to
`seen_old_ids.csv` unfetched, and the walk stops after 2 consecutive pages
with nothing in the window. In steady state a daily run is a handful of
listing pages plus one detail per genuinely new posting. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has
only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 8`, i.e. 160 newest postings ≈ the last ~1–2 days at this
velocity) to keep the initial burst modest — the watermark takes over from
there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review — J&J
posts country-office vacancies untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Johnson & Johnson); the
Workday legal entity (e.g. "8078-Johnson & Johnson Surgical Vision India
Private Limited Legal Entity") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --max-pages 8  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (61)
```

Outputs: `jj_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/jj.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
