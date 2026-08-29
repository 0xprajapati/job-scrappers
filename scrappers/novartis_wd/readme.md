# novartis scraper

Scrapes Novartis' global careers board from the **Workday CXS JSON API** at
`novartis.wd3.myworkdayjobs.com` (tenant `novartis`, site `Novartis_Careers`).
~959 open postings on 2026-08-28. Supply is pharma-shaped: research
scientists, clinical development, regulatory, medical affairs, plus a long
tail of commercial/IT/finance the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/novartis/Novartis_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (`REQ-10085163` style). `total` is only reliable on the
  offset=0 page (deeper pages report 0 — probe-verified 2026-08-28).
- **Detail**: `GET /wday/cxs/novartis/Novartis_Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /Novartis_Careers/`, `Disallow: /refreshFacet/`
  only — the CXS API paths are allowed. Checked at startup, honored per-URL.
- **Other sites on this tenant** (seen in robots.txt, deliberately NOT
  crawled): `SwissRAVSwitzerlandCareerSite` (a Swiss RAV / unemployment-office
  compliance duplicate of Swiss vacancies) and
  `Internal_Careers_for_Acquired_Entities` (internal-only). `WORKDAY_SITES`
  stays `['Novartis_Careers']`.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

The board posts roughly **25–30 requisitions a day** (offset 200 was already
7 days old at probe time), so detail-fetching the whole board would cost
~1,000 requests. Instead the relative `postedOn` label decides the time
window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. In steady state a daily run is
a handful of listing pages plus one detail per genuinely new posting. The
detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings ≈ the last ~10 days at observed
velocity) to keep the initial burst modest — the watermark takes over from
there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Novartis posts some country-office vacancies untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Novartis); the Workday legal
entity (e.g. "U175 (FCRS = US175) Novartis Institutes for BioMedical
Research, Inc.") is kept raw in `hiring_org`.

**Location quirk**: Novartis writes single locations as `City (Country)` /
`City (Office)` — `Cambridge (USA)`, `Hyderabad (Office)` — with no
comma/hyphen separator, so this scraper's `parse_city` strips one trailing
parenthetical before the segment scan (a tenant-local adaptation:
`Cambridge (USA)` → `Cambridge`). The club `country_name` / `country_code`
come from the detail's ISO `alpha2Code` either way and are exact.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21   # explicit window
python test_filters.py                            # unit tests (59)
```

Outputs: `novartis_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/novartis.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
