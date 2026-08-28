# ferring scraper

Scrapes Ferring Pharmaceuticals' global careers board from the **Workday CXS
JSON API** at `ferring.wd3.myworkdayjobs.com` (tenant `ferring`, site
`Ferring`). **79 open postings on 2026-08-28** — a small board. Ferring is a
privately owned, Swiss-headquartered biopharmaceutical company working in
reproductive medicine and maternal health, gastroenterology and
urology/uro-oncology. Supply is commercial-heavy (medical and sales
representatives, key account managers, territory managers) with a thinner
seam of in-scope roles: regulatory affairs, biometrics/statistics, quality
control, clinical and nurse-liaison positions.

The tenant exposes a single Workday site (`Ferring`); no alternate or
brand-sibling sites were found, so `WORKDAY_SITES = ['Ferring']`.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/ferring/Ferring/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0 — confirmed here: offsets 20/40/60 all reported `total: 0`).
- **Detail**: `GET /wday/cxs/ferring/Ferring/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /Ferring/`, `Disallow: /refreshFacet/` only — the
  CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness (probed 2026-08-28)

Newest-first ordering **holds**. With `searchText=""` the `postedOn` labels
aged monotonically with offset:

| offset | first label | last label |
|--------|-------------|------------|
| 0      | Posted Today | Posted 10 Days Ago |
| 20     | Posted 10 Days Ago | Posted 24 Days Ago |
| 40     | Posted 24 Days Ago | Posted 30+ Days Ago |
| 60     | Posted 30+ Days Ago | Posted 30+ Days Ago (19 rows, end of board) |

`total` (79) is a real count, not a round display cap, so the template's
consecutive-old-pages early stop is sound and is left enabled. In practice
it rarely fires: 79 < `EXHAUSTIVE_BOARD_MAX` (200), so the board is walked
to its end anyway — four listing pages.

## Why the window matters here

The board turns over slowly — roughly **2 requisitions a day** (the newest 20
rows spanned "Posted Today" back to "Posted 10 Days Ago"). The relative
`postedOn` label still decides the time window **before** any detail request
is spent: out-of-window ids go to `seen_old_ids.csv` unfetched, so a full
4-page walk costs only a handful of detail fetches. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has
only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run needed no `--max-pages` cap —
the whole board is four pages.

## Tenant quirks

- Locations are `City, Region, Country` ("Denver, Colorado, United States")
  or `City, Country` ("Shanghai, China"). Multi-site requisitions show only
  an `N Locations` placeholder in `locationsText`; the real city comes from
  the detail's `location`, and the rest land in `additional_locations`.
- `additionalLocations` is `null` (not `[]`) on single-site rows.
- `hiringOrganization.name` is frequently `""` and is a bare legal-entity
  code ("US000 USA LE") when present — `hiring_org` is often blank; the
  brand always comes from `COMPANY_NAME`.
- A few APAC requisitions are posted wholly in Chinese and their
  `externalPath` slug loses the title (`/job/Shanghai-China/_R0038211`).
  The requisition id still arrives in `bulletFields`, so `job_id` is safe.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Ferring Pharmaceuticals); the
Workday legal entity is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (65)
```

Outputs: `ferring_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/ferring.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
