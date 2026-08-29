# sanofi scraper

Scrapes Sanofi's global careers board from the **Workday CXS JSON API** at
`sanofi.wd3.myworkdayjobs.com` (tenant `sanofi`, site `SanofiCareers`). ~790
open postings on 2026-08-28. Sanofi is the French multinational pharma
(vaccines, immunology, specialty care, general medicines); supply is
pharma-shaped: regulatory affairs, medical information, quality/manufacturing,
medical affairs, plus a long tail of sales/marketing/finance/IT the shared
classifier drops. Many country-office postings are untranslated (German
"Pharmaziepraktikant*in" batches, French, etc.).

**Alt-site decision**: PharmaBharat aggregator links also referenced an
`OpellaCareers` site on this tenant. Probed 2026-08-28: it answers **HTTP
404** (no postings). Opella is Sanofi's consumer-healthcare spin-off — a
separate brand — so it is excluded from `WORKDAY_SITES` on both grounds.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/sanofi/SanofiCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0 — probe-verified here too).
- **Detail**: `GET /wday/cxs/sanofi/SanofiCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. Quirk: `additionalLocations` is **null**
  (not a list) on single-location postings; `locationsText` is usually a
  bare city with no country segment ("Praha", "Frankfurt am Main").
- **robots.txt**: `Allow: /SanofiCareers/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

The board posts roughly **40–50 requisitions a day** (offset 100 was already
2 days old at probe time), so detail-fetching the whole ~790-posting board
would be wasteful. Instead the relative `postedOn` label decides the time
window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. In steady state a daily run is
a handful of listing pages plus one detail per genuinely new posting. The
detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings) to keep the initial burst
modest — the watermark takes over from there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review — Sanofi
posts country-office vacancies untranslated (German "(all genders)" /
"Pharmaziepraktikant*in" roles, French postings, etc.).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Sanofi); the Workday legal
entity (e.g. "(5070) Sanofi s.r.o.") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (59)
```

Outputs: `sanofi_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/sanofi.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
