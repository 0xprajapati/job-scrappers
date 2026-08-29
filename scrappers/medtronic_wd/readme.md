# medtronic scraper

Scrapes Medtronic's global careers board from the **Workday CXS JSON API** at
`medtronic.wd1.myworkdayjobs.com` (tenant `medtronic`, site
`MedtronicCareers`). ~1,141 open postings on 2026-08-28. Medtronic is the
medical-device multinational (cardiac devices, surgical robotics,
neuromodulation, diabetes tech); supply is device-industry-shaped —
engineering, manufacturing, sales and IT the shared classifier drops, plus
in-scope clinical specialists, field clinical/study roles, regulatory
affairs and quality.

The tenant also hosts a `RedeploymentMedtronicCareers` site — internal
redeployment, and **disallowed by robots.txt** — so it is excluded from
`WORKDAY_SITES`; only the public brand board is crawled.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/medtronic/MedtronicCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0 — probe-verified on this tenant too).
- **Detail**: `GET /wday/cxs/medtronic/MedtronicCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` is `null` (not `[]`)
  on single-site postings here — handled.
- **robots.txt**: `Allow: /MedtronicCareers/`,
  `Disallow: /RedeploymentMedtronicCareers/`, `Disallow: /refreshFacet/` —
  the CXS API paths for the public board are allowed. Checked at startup,
  honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

**Newest-first ordering holds** (ordering soundness probe, 2026-08-28,
searchText=""): offset 0 → "Posted Today", offset 100 → "Posted Yesterday",
offset 200 → "Posted 2 Days Ago" — labels age monotonically with offset, and
`total` (1,141) is not a suspicious round display cap, so the template's
consecutive-old-pages early stop is sound.

Those same labels show the board turns over on the order of **~100
requisition labels a day**, so detail-fetching the whole board would cost
>1,100 requests. Instead the relative `postedOn` label decides the time
window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. In steady state a daily run is
a handful of listing pages plus one detail per genuinely new posting. The
detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings ≈ the last ~3 days) to keep the
initial burst modest — the watermark takes over from there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Medtronic posts country-office vacancies untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Medtronic); the Workday legal
entity (e.g. "IE5 COV - Nellcor Puritan Bennett IRL") is kept raw in
`hiring_org`.

## Outputs

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests
```

Outputs: `medtronic_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/medtronic.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
