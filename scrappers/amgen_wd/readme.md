# amgen scraper

Scrapes Amgen's external careers board from the **Workday CXS JSON API** at
`amgen.wd1.myworkdayjobs.com` (tenant `amgen`, site `Careers`). ~1,830 open
postings on 2026-08-28 — the biggest board of this Workday batch, dominated
by Sales/IS/Technology/Manufacturing supply, with roughly half the postings
in India (the Hyderabad hub). The in-scope slice lives in a handful of
jobFamilyGroup facet values, so the crawl is **facet-scoped**.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/amgen/Careers/jobs` with
  `{"appliedFacets":{"jobFamilyGroup":[ids…]},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id (`R-xxxxxx`) in
  `bulletFields`; `total` is only reliable on the offset=0 page and, with
  facets applied, is the *filtered* total. The response also carries a
  `facets[]` array with jobFamilyGroup descriptors + ids + counts.
- **Detail**: `GET /wday/cxs/amgen/Careers/job/<externalPath>` → full HTML
  description, exact `startDate`, `timeType`, country descriptor + ISO
  `alpha2Code`, canonical `externalUrl` (under `/Careers/`, used verbatim).
- **robots.txt**: `Allow: /Careers/`, `Disallow: /refreshFacet/` only — the
  CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 without them); ≥1.5 s between
  requests, exponential backoff on 429/5xx.

## Facet-scoped crawl (with fallback)

`TARGET_JOB_FAMILY_GROUPS` holds facet **names** — ids differ per tenant and
can change, so each run resolves names → ids from one unfiltered offset=0
request, then walks the listing with
`appliedFacets {"jobFamilyGroup": [ids…]}`. On 2026-08-28 the scope resolved
to 258 of 1,831 postings:

| name | count | id (2026-08-28) |
|---|---|---|
| Clinical | 151 | `5d5ff483caeb105761523f2e031aa82f` |
| Regulatory & Compliance | 36 | `5d5ff483caeb105761529c1cb7fea851` |
| Medical Science Liaison | 33 | `428261700d6a012037db1f17d621eee4` |
| Medical Scientist | 21 | `5d5ff483caeb10576152872606f9a849` |
| Safety | 12 | `5d5ff483caeb10576152a0ef6a34a853` |
| Medical Affairs | 5 | `396f2f0ed1c901ca9434b775ff400c88` |

A configured name missing from the live facet list logs a **loud warning**
(scope has silently shrunk); if no name resolves, or the facet-scoped
listing fails/comes back empty, the run warns loudly and **falls back** to
the iqvia-style unfiltered newest-first window crawl (page cap 40).
`searchText` is never used for scoping — Workday matches it against whole
descriptions and the result set is a mirage (the Piramal lesson).

Facets are crawl-side scoping only: keep/drop still belongs entirely to
`_shared/classification.classify_job(title, "", description)`. About 45% of
the in-window slice survived the classifier on the first run — the rest is
biostatistics leadership, clinical-systems ops, etc.

## Quirks

- **Country-first location strings**, sometimes with a state segment:
  `US - Maryland - Baltimore`, `India - Hyderabad` → the city parser takes
  the **last** surviving segment (`CITY_SEGMENT = "last"`), unlike bms which
  writes city-first.
- Descriptions open with an embedded `Career Category` heading (the family
  group name) before the actual JD text — stripped along with the HTML.
- Some externalPaths start with `XMLNAME-` (an export artifact); the
  requisition id in `bulletFields` is the stable job_id.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank. `company` is the brand (Amgen); the Workday legal entity
  ("1074 Amgen Technology Pvt Ltd.") stays raw in `hiring_org`.

## Window & classification

Newest-first walk; the relative `postedOn` label decides the time window
before any detail request (out-of-window ids → `seen_old_ids.csv`,
unfetched); the facet scope (~258) is above `EXHAUSTIVE_BOARD_MAX` (200) so
the walk early-stops after 2 consecutive fully-old pages. First run keeps
the last 7 days; later runs use the newest stored `posted_date` minus 2
days grace. `needs_review` rows (including non-English DE/FR/ES
descriptions) are kept and logged to `needs_review.csv`.

First run (2026-08-28, 7-day window): 80 scanned / 22 details / 17 kept /
5 out-of-scope; sub_categories MSL 8, Clinical Research 5, Regulatory
Affairs 2, Pharmacovigilance 2; 2/17 India (the Hyderabad hub is mostly
IS/finance, which the facet scope already excludes). Immediate second run:
0 details fetched, 0 added.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (65)
```

Outputs: `amgen_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/amgen.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
