# bms scraper

Scrapes Bristol Myers Squibb's external careers board from the **Workday
CXS JSON API** at `bristolmyerssquibb.wd5.myworkdayjobs.com` (tenant
`bristolmyerssquibb`, site `BMS`). ~675 open postings on 2026-08-28; the
in-scope slice lives in four jobFamilyGroup facet values, so the crawl is
**facet-scoped** to them.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/bristolmyerssquibb/BMS/jobs` with
  `{"appliedFacets":{"jobFamilyGroup":[ids…]},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id (`R16xxxxx`) in
  `bulletFields`; `total` is only reliable on the offset=0 page and, with
  facets applied, is the *filtered* total. The response also carries a
  `facets[]` array with jobFamilyGroup descriptors + ids + counts.
- **Detail**: `GET /wday/cxs/bristolmyerssquibb/BMS/job/<externalPath>` →
  full HTML description, exact `startDate` (plus an `endDate` — BMS
  postings expire), `timeType`, country descriptor + ISO `alpha2Code`,
  canonical `externalUrl` (under `/BMS/`, used verbatim).
- **robots.txt**: `Allow: /BMS/`, `Disallow: /refreshFacet/` only — the CXS
  API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Facet-scoped crawl (with fallback)

`TARGET_JOB_FAMILY_GROUPS` holds facet **names** — ids differ per tenant and
can change, so each run resolves names → ids from one unfiltered offset=0
request, then walks with `appliedFacets {"jobFamilyGroup": [ids…]}`. On
2026-08-28 the scope resolved to 171 of 677 postings:

| name | count | id (2026-08-28) |
|---|---|---|
| Clinical Development | 69 | `149748d319111024cd219db22da54096` |
| Medical Affairs | 59 | `149748d319111024cd21dd6c129540bc` |
| Regulatory Affairs | 30 | `56d1e4178f8910013cc3668dbf510000` |
| Drug Safety | 13 | `56d1e4178f8910013cc3394553c20000` |

(The finer `jobFamilies` facet also exposes Pharmacovigilance — it sits
under Drug Safety, so the group-level scope already covers it.)

A configured name missing from the live facet list logs a **loud warning**;
if no name resolves, or the facet-scoped listing fails/comes back empty,
the run warns loudly and **falls back** to the unfiltered newest-first
window crawl (page cap 40). `searchText` is never used for scoping (the
Piramal lesson). Facets are crawl-side scoping only: keep/drop still
belongs entirely to `_shared/classification.classify_job`. About half of
the in-window slice survived the classifier on the first run.

## Quirks

- **City-first location strings**: `Hyderabad - TS - IN`,
  `Princeton - NJ - US` → the city parser takes the **first** surviving
  segment (`CITY_SEGMENT = "first"`), the opposite of amgen/pfizer/lilly.
- `Field - <country>` marks territory-based roles — "Field" is a
  non-city token, so such rows export a blank city (the row's country still
  comes from the detail's ISO code).
- The facet scope is ≤ `EXHAUSTIVE_BOARD_MAX` (200), so every run walks the
  whole 171-posting scope (9 listing pages) — early-stop ordering
  assumptions never come into play.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank. `company` is the brand; the legal entity ("0406 BMS (China)
  Investment Co") stays raw in `hiring_org`.

## Window & classification

The relative `postedOn` label decides the time window before any detail
request (out-of-window ids → `seen_old_ids.csv`, unfetched). First run
keeps the last 7 days; later runs use the newest stored `posted_date` minus
2 days grace. `needs_review` rows (including non-English DE/FR/ES
descriptions) are kept and logged to `needs_review.csv`.

First run (2026-08-28, 7-day window): 171 scanned / 26 details / 21 kept /
5 out-of-scope; sub_categories Clinical Research 11, MSL 4,
Pharmacovigilance 2, Medical Writer 1, Epidemiology 1, HEOR 1, Regulatory
Affairs 1; 1/21 India (BMS's Hyderabad hub posts mostly IT/quality, outside
the facet scope). Immediate second run: 0 details fetched, 0 added.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (65)
```

Outputs: `bms_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/bms.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
