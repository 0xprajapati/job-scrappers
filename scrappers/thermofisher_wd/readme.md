# thermofisher scraper

Scrapes Thermo Fisher Scientific's global careers board from the **Workday
CXS JSON API** at `thermofisher.wd5.myworkdayjobs.com` (tenant
`thermofisher`, site `ThermoFisherCareers`). ~3,204 open postings on
2026-08-28 — the biggest Workday board in the fleet. Thermo Fisher is a
life-sciences tools & services giant and includes PPD, its CRO arm, so the
supply mixes a large out-of-scope tail (manufacturing, engineering, sales,
IT, finance) with an in-scope slice of clinical research, pharmacovigilance,
regulatory and lab-services roles the shared classifier keeps.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/thermofisher/ThermoFisherCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, timeType, a *relative* `postedOn`
  label ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. Unlike some tenants, `total` stays non-zero on deeper
  pages here (3204 at offset 200 when probed); the first value is latched
  anyway.
- **Detail**: `GET /wday/cxs/thermofisher/ThermoFisherCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`
  (on `jobRequisitionLocation`), and the canonical `externalUrl`.
  `additionalLocations` is `null` (not `[]`) on single-location rows.
- **robots.txt**: `Disallow: /ThermoFisherCareers/` and
  `Disallow: /refreshFacet/` — i.e. the *human-facing* site paths are
  disallowed, but the `/wday/cxs/` API paths this scraper actually fetches
  are **not**. The startup check verifies the real fetched URLs
  (list + detail) per-request and would abort if Workday ever disallowed
  `/wday/`.
- Quirk: some `externalPath` tails carry a `-1` suffix after the requisition
  id (`Engineer-II--Mechanical_R-01365564-1`) while `bulletFields` holds the
  clean `R-01365564` — `bulletFields` wins, so ids stay clean.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

The board posts roughly **100+ requisitions a day** (offset 200 was already
1–2 days old at probe time), so detail-fetching the whole board would cost
~3,200 requests. Instead the relative `postedOn` label decides the time
window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. In steady state a daily run is
a handful of listing pages plus one detail per genuinely new posting. The
detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 8`, i.e. 160 newest postings ≈ the last ~1–2 days) to keep the
initial burst modest — the watermark takes over from there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Thermo Fisher posts country-office vacancies untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Thermo Fisher Scientific); the
Workday legal entity (e.g. "2100 FEI Electron Optics B.V.") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --max-pages 8  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21   # explicit window
python test_filters.py                           # unit tests (60)
```

Outputs: `thermofisher_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/thermofisher.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
