# lilly scraper

Scrapes Eli Lilly's external careers board from the **Workday CXS JSON
API** at `lilly.wd115.myworkdayjobs.com` (tenant `lilly`, site `LLY`).
~610 open postings on 2026-08-28; the in-scope slice lives in three
jobFamilyGroup facet values, so the crawl is **facet-scoped** — and because
the R&D group is bench-heavy, the shared classifier's bench vetoes do most
of the dropping afterwards.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/lilly/LLY/jobs` with
  `{"appliedFacets":{"jobFamilyGroup":[ids…]},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id (`R-xxxxxx`) in
  `bulletFields`; `total` is only reliable on the offset=0 page and, with
  facets applied, is the *filtered* total. The response also carries a
  `facets[]` array with jobFamilyGroup descriptors + ids + counts.
- **Detail**: `GET /wday/cxs/lilly/LLY/job/<externalPath>` → full HTML
  description, exact `startDate` (plus an `endDate`), `timeType`, country
  descriptor + ISO `alpha2Code`, canonical `externalUrl` (under `/LLY/`,
  used verbatim).
- **robots.txt**: `Allow: /CMP/ /LLY/ /MAA/`, `Disallow: /refreshFacet/`
  only — the CXS API paths are allowed. Checked at startup, honored
  per-URL. (Only `LLY` — the external site — is crawled.)
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Facet-scoped crawl (with fallback)

`TARGET_JOB_FAMILY_GROUPS` holds facet **names** — ids differ per tenant and
can change, so each run resolves names → ids from one unfiltered offset=0
request, then walks with `appliedFacets {"jobFamilyGroup": [ids…]}`. On
2026-08-28 the scope resolved to 170 of 608 postings:

| name | count | id (2026-08-28) |
|---|---|---|
| Research & Development | 155 | `99c6e09d03e801a7d2687e7ff04aec33` |
| Medical Affairs | 13 | `99c6e09d03e801c6f900847ff04a0034` |
| Health Outcomes and Real World Evidence | 2 | `99c6e09d03e801faaecc807ff04af433` |

Several of lilly's group names carry a literal `(JFG)` suffix ("Sales
(JFG)", "Quality (JFG)") — name matching is punctuation-insensitive but the
configured names must still match the live descriptors, so a rename logs a
**loud warning**. If no name resolves, or the facet-scoped listing
fails/comes back empty, the run warns loudly and **falls back** to the
unfiltered newest-first window crawl (page cap 40). `searchText` is never
used for scoping (the Piramal lesson). Facets are crawl-side scoping only:
keep/drop still belongs entirely to `_shared/classification.classify_job`.

**Expect a low keep rate (~35%)**: "Research & Development" is mostly
discovery chemistry, protein science, post-docs, ADME, device engineering —
the classifier's bench-R&D vetoes drop those (the Bench Research Associate
over-admission ruling), keeping clinical development, data management,
regulatory and MSL roles.

## Quirks

- **Comma-separated, country-first location strings**: `India, Bengaluru`,
  `US, Pleasant Prairie WI` → the city parser takes the **last** surviving
  segment (`CITY_SEGMENT = "last"`); a US city keeps its state suffix
  ("Pleasant Prairie WI") — best-effort, never guessed away.
- Remote rows arrive as a single segment (`US: USA Remote`) — any segment
  containing a remote marker is disqualified as a city, so those rows
  export city `Remote` (himalayas convention) with `job_type = remote`.
- Titles sometimes carry non-breaking spaces and Japanese/Portuguese
  postings appear untranslated (kept, flagged `needs_review` when the
  description is non-English).
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank. `company` is the brand (Eli Lilly); the legal entity ("550 Eli
  Lilly Services India Pvt Ltd") stays raw in `hiring_org`.
- The facet scope is ≤ 200, so every run walks the whole 170-posting scope
  (9 listing pages).

## Window & classification

The relative `postedOn` label decides the time window before any detail
request (out-of-window ids → `seen_old_ids.csv`, unfetched). First run
keeps the last 7 days; later runs use the newest stored `posted_date` minus
2 days grace.

First run (2026-08-28, 7-day window): 170 scanned / 31 details / 11 kept /
20 out-of-scope (bench vetoes hard at work — dropped: ADME leads,
post-docs, analytical chemists, protein scientists); sub_categories
Clinical Research 7, Clinical Data Management 2, Regulatory Affairs 1,
MSL 1; 1/11 India. Immediate second run: 0 details fetched, 0 added.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (65)
```

Outputs: `lilly_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/lilly.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
