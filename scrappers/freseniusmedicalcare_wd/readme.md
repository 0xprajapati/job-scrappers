# freseniusmedicalcare scraper

Scrapes Fresenius Medical Care's global careers board from the **Workday CXS
JSON API** at `freseniusmedicalcare.wd3.myworkdayjobs.com` (tenant
`freseniusmedicalcare`, site `fme`). The board reported `total = 2000` on
2026-08-28 — a display cap rather than a count (see *Board quirks*). FMC is
the world's largest dialysis company: dialysis products plus a ~4,000-clinic
outpatient care network. Supply is overwhelmingly clinic-side patient care
(dialysis RNs, patient care technicians, clinical managers, dietitians,
social workers) plus a corporate tail (finance, IT, supply chain, HR); the
in-scope slice is clinical research / regulatory / quality / medical affairs
/ medical coding out of the corporate and shared-services sites (Waltham MA,
Bad Homburg DE, Bonifacio Global City PH, Mumbai / Delhi IN). A high
`excluded_out_of_scope` ratio is expected and correct here.

`WORKDAY_SITES = ['fme']` — the single site the tenant serves; no alternate
sites were supplied or found.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/freseniusmedicalcare/fme/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0).
- **Detail**: `GET /wday/cxs/freseniusmedicalcare/fme/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. This tenant also exposes `remoteType`
  (Onsite / Hybrid / Remote), which the fleet schema does not carry.
- **robots.txt**: `Allow: /fme/`, `Disallow: /refreshFacet/` only (plus a
  sitemap line) — the CXS API paths are allowed. Checked at startup,
  honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Board quirks (probe-verified 2026-08-28)

- **`total = 2000` is a DISPLAY CAP, not a count.** Offsets 2020 and 2400
  both return the offset=0 page verbatim — same requisition ids
  (R0257813 / R0253692 / R0235434), `total` back to 2000 — i.e. the API
  wraps instead of ending. The real board is at least 2000 postings and its
  oldest tail is unreachable through the unfiltered listing.
- **Ordering IS newest-first**, so the cap is harmless here: it can only
  hide rows older than the deepest reachable page. `postedOn` ages strictly
  monotonically with offset:

  | offset | 0 | 100 | 200 | 400 | 1000 | 1500 | 1960 | 1980 |
  |---|---|---|---|---|---|---|---|---|
  | label | Today | Yesterday | 2 Days | 3 Days | 9 Days | 16–17 Days | 23–24 Days | 24 Days |

  The fleet's consecutive-old-pages early stop is therefore **sound on this
  tenant and is kept enabled** (`CONSECUTIVE_OLD_PAGES_STOP = 2`,
  `DEFAULT_MAX_PAGES = 40` — template defaults, unchanged). The deepest
  reachable page is ~24 days old, far outside any window this scraper uses.
- **Location shapes.** Mostly `"City, ST"` / `"City, ST, USA"` /
  `"City, Country"`. Three others matter: an `"N Locations"` placeholder
  (2 … 19 — the detail's `location` carries the real primary one), US clinic
  codes (`"AZ204 FMCNA Western Skies - Clinic"`, `"CT001 Central
  Connecticut Dialysis - Clinic"`) which contain no plain city, and a
  clinic-name-first international form (`"Dialcentro, Madrid, Spain"`) where
  the leading segment is the clinic, not the city. `parse_city` returns the
  leading surviving segment in the last two cases rather than inventing a
  city — a known, documented imprecision, not a silent guess.
- `additionalLocations` is `null` (not `[]`) on most postings and
  `locationsText` is occasionally `null`; both are handled.
- Reposted requisitions carry a `-1` suffix (`R0247884-1`); the suffix is
  part of the dedup key.

## Why the window matters here

The board posts roughly **100–130 requisitions a day** (offset 100 was 1 day
old, offset 400 3 days, offset 1000 9 days at probe time), so
detail-fetching the reachable board would cost ~2,000 requests. Instead the
relative `postedOn` label decides the time window **before** any detail
request: out-of-window ids go to `seen_old_ids.csv` unfetched, and the
newest-first walk stops after 2 consecutive pages with nothing in the
window. In steady state a daily run is a handful of listing pages plus one
detail per genuinely new posting. The detail's exact `startDate` re-checks
the window after the fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 8`, i.e. the 160 newest postings ≈ the last ~1.5 days) to keep
the initial burst modest on a board this size — the watermark takes over
from run 2.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review — FMC
posts its German and Spanish clinic vacancies untranslated
("Enfermera/o (Dialcentro)", Pflegefachkraft roles etc.).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Fresenius Medical Care); the
Workday legal entity (e.g. "BPA USA Bio-Med Appl of Pennsylvania") is kept
raw in `hiring_org`. `company_type` is **`hospital`**: FMC runs a ~4,000-clinic
outpatient dialysis network, and the supply this board actually yields is
clinic-side care (the first run's kept rows were overwhelmingly clinic
dietitians), so `hospital` is the truthful club value even though FMC also
sells dialysis products.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --max-pages 8  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (67)
```

Outputs: `freseniusmedicalcare_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/freseniusmedicalcare.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
