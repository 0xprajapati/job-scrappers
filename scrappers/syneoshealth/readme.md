# syneoshealth scraper

Scrapes Syneos Health's global careers board from the **Workday CXS JSON
API** at `syneoshealth.wd12.myworkdayjobs.com` (tenant `syneoshealth`, site
`Syneos_Health_External_Site`). ~658 open postings on 2026-08-28. Syneos
Health is a large fully-integrated CRO (clinical development +
commercialization), so supply is heavily in-scope: CRAs, clinical data
management/review, site contracts, clinical operations, PV, regulatory —
plus a commercial/sales/IT tail the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/syneoshealth/Syneos_Health_External_Site/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (numeric, e.g. `25111571`). `total` is only reliable on
  the offset=0 page (deeper pages report 0).
- **Detail**: `GET /wday/cxs/syneoshealth/Syneos_Health_External_Site/job/<externalPath>`
  → `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. A `remoteType` field exists ("Remote
  (Exception)") but is not part of the shared row contract.
- **robots.txt**: `Allow: /Syneos_Health_External_Site/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

The board posts roughly **10–20 requisitions a day** (page 1 of 20 spanned
"Posted Today" + "Posted Yesterday" at probe time on 2026-08-28), so
detail-fetching the whole 658-posting board would waste hundreds of
requests on stale requisitions. The relative `postedOn` label decides the
time window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. In steady state a daily run
is a handful of listing pages plus one detail per genuinely new posting.
The detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings) to keep the initial burst
modest — the watermark takes over from there.

## Known quirk: alpha-3 location prefixes (handled locally)

Every location string on this board is prefixed with the ISO **alpha-3**
country code: `TWN-Taipei`, `USA-AL-Remote`, `DEU-Remote`,
`USA-NY-225 Liberty Street-Hybrid`. The fleet template's `parse_city`
drops only alpha-2 codes, country names and known aliases, so the alpha-3
token (or a leaked US state code) would surface as the `city`. This
scraper's `parse_city` therefore carries a **one-line tenant deviation**:
segments that are bare all-uppercase 2–3 letter ISO-style tokens
(`re.fullmatch(r"[A-Z]{2,3}", part)`) are skipped, so `TWN-Taipei` →
`Taipei` and `USA-NC-Remote` → `""` (the remote fallback then applies).
Real city names survive (they are not bare all-caps 2–3 letter tokens).
Country name/code are unaffected either way — they come from the detail's
ISO `alpha2Code`. Behavior is pinned in `test_filters.py`.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
country-office vacancies can be posted untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Syneos Health); the Workday
legal entity (e.g. "1001 Syneos Health, LLC", "5068 Taiwan Syneos Health
Company Limited") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests
```

Outputs: `syneoshealth_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/syneoshealth.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
