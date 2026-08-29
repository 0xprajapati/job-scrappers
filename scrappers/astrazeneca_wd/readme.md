# astrazeneca scraper

Scrapes AstraZeneca's global careers board from the **Workday CXS JSON API**
at `astrazeneca.wd3.myworkdayjobs.com` (tenant `astrazeneca`, site
`Careers`). ~1,234 open postings on 2026-08-28. Supply is big-pharma-shaped:
clinical development, medical affairs / MSL, pharmacovigilance, regulatory,
biometrics, plus a long tail of commercial/IT/manufacturing the shared
classifier drops.

The tenant hosts three sibling sites that are **deliberately excluded**:
`Emerging-Talent` (internships / early-careers programmes),
`Alexion` (the separate Alexion, AstraZeneca Rare Disease brand board) and
`broadbean_external` (a job-distribution feed). Only `Careers` is the
AstraZeneca-brand professional board.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/astrazeneca/Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, timeType, a *relative* `postedOn`
  label ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (e.g. `R-259028`). Unlike some tenants this one reports
  `total` on deeper pages too, but only the offset=0 value is trusted.
  A reposted requisition's externalPath tail can grow a `-1` suffix
  (`…R-253931-1`) while `bulletFields` keeps the canonical req id — the id
  always comes from `bulletFields`.
- **Detail**: `GET /wday/cxs/astrazeneca/Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code` (on
  `jobRequisitionLocation`), and the canonical `externalUrl`.
- **robots.txt**: `Allow: /Careers/` (and the three sibling sites),
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

The board posts roughly **50–65 requisitions a day** (at probe time offset
100 was 2 days old and offset 200 was 3–4 days old), so detail-fetching the
whole board would cost ~1,200+ requests. Instead the relative `postedOn`
label decides the time window **before** any detail request: out-of-window
ids go to `seen_old_ids.csv` unfetched, and the newest-first walk stops
after 2 consecutive pages with nothing in the window. In steady state a
daily run is a handful of listing pages plus one detail per genuinely new
posting. The detail's exact `startDate` re-checks the window after the
fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings ≈ the last ~5 days) to keep the
initial burst modest — the watermark takes over from there.

Location strings are country-first hyphen format (`UK - Cambridge`,
`Portugal - Lisboa - Av. Dom João II`) plus field-force placeholders
(`Field PS`) that carry no real city.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
AstraZeneca posts country-office vacancies untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (AstraZeneca); the Workday
legal entity (e.g. "UK10 AstraZeneca UK Ltd Company") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21   # explicit window
python test_filters.py                            # unit tests (60)
```

Outputs: `astrazeneca_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/astrazeneca.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
