# elanco scraper

Scrapes Elanco's global careers board from the **Workday CXS JSON API** at
`elanco.wd5.myworkdayjobs.com` (tenant `elanco`, site `External_Career`).
371 open postings on 2026-08-28 — a mid-size board. Elanco is an
animal-health pharma (NYSE: ELAN — vaccines, parasiticides and
therapeutics for farm animals and pets), so supply is manufacturing/QA
heavy: bio-manufacturing operators, quality assurance/control, R&D
scientists, regulatory, plus a long tail of sales/marketing/IT/finance the
shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/elanco/External_Career/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0 — probe-verified on this tenant).
- **Detail**: `GET /wday/cxs/elanco/External_Career/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` is absent on this
  tenant's payloads (the template tolerates that).
- **robots.txt**: `Allow: /External_Career/`, `Disallow: /refreshFacet/`
  only — the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.
- Only one Workday site is configured: `External_Career` is the tenant's
  sole public board (no alt sites were listed for this tenant).

## Why the window matters here

**Ordering soundness (probed 2026-08-28, searchText="")**: postedOn labels
age monotonically with offset — offset 0 was "Posted Today"/"Posted
Yesterday", offset 100 was "Posted 8–10 Days Ago", offset 200 was "Posted
28–30+ Days Ago" — so newest-first holds and the template's early stop is
sound. The one quirk: the board pins **two stray "Posted 30+ Days Ago"
rows at the very top** of offset 0 (R0022769, R0024627). They are harmless
to the 2-consecutive-old-pages early stop because their page still
contains in-window postings; they simply land in `seen_old_ids.csv`
unfetched. `total` = 371 is not a suspicious round number (no display cap).

The board posts roughly **5–20 requisitions a day**, so in steady state a
daily run is a handful of listing pages plus one detail per genuinely new
posting. The relative `postedOn` label decides the time window **before**
any detail request: out-of-window ids go to `seen_old_ids.csv` unfetched.
The detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was capped
(`--max-pages 15`, i.e. 300 newest postings ≈ well past the 7-day window
at this velocity) to keep the initial burst modest — the watermark takes
over from there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Elanco posts country-office vacancies untranslated (German "(m/w/d)"
Ausbildung roles in Kiel, Spanish sales roles in LatAm, French QC roles in
Huningue).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Elanco); the Workday legal
entity (e.g. "US01 Elanco US Inc.") is kept raw in `hiring_org`.

Location quirk: international rows use an ISO-prefix format ("CO -
Bogota", "UK - Hook", "MX - Guadalajara") while US rows are "City, ST"
("Elwood, KS"). The fleet-standard `parse_city` amendment skips bare 2–3
letter uppercase tokens so the prefix never leaks out as a city.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (60)
```

Outputs: `elanco_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/elanco.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
