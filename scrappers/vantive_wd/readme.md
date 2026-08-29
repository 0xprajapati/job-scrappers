# vantive scraper

Scrapes Vantive's global careers board from the **Workday CXS JSON API** at
`vantive.wd108.myworkdayjobs.com` (tenant `vantive`, site `Vantive`). **277
open postings on 2026-08-28** (278 an hour later, on the first run — the
board turns over a handful a day). Vantive is the vital organ therapy company
spun out of Baxter in 2025 — 70 years of kidney care (dialysis machines,
dialyzers, PD solutions and the services around them), now extending into
other organ therapies. Supply is medtech-shaped: commercial, manufacturing,
supply chain and field service dominate, with a smaller clinical affairs,
regulatory, quality and device-engineering tail that the shared classifier
keeps.

Only one Workday site exists on this tenant (`Vantive`); no alt sites were
found or configured.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/vantive/Vantive/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (offsets
  100 and 200 both reported 0 at probe time).
- **Detail**: `GET /wday/cxs/vantive/Vantive/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /Vantive/`, `Disallow: /refreshFacet/` only — the
  CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Tenant quirks

- The requisition id in `bulletFields` is **spaced** — `JR - 197428`, not
  `JR197428`. Stored verbatim as `job_id`.
- `additionalLocations` is JSON **null** on this tenant, not an empty array.
- `locationsText` spells US states out in full (`Deerfield, Illinois`), so
  the ISO/state-code skip in `parse_city` never fires on them. Remote rows
  read `<Country> (remote)` (e.g. `Italy (remote)`) and multi-site rows read
  `N Locations`.

## Why the window matters here

Ordering soundness was checked live with `searchText=""`: the `postedOn`
labels **age monotonically with offset** — offset 0 "Posted Today" /
"Posted Yesterday", offset 100 "Posted 15–18 Days Ago", offset 200 "Posted
30+ Days Ago" — and `total` (277) is not a round display cap. **Newest-first
holds**, so the template's consecutive-old-pages early stop is sound and
stays enabled.

The board adds roughly **6–7 requisitions a day**, so the 7-day window closes
inside the first ~3 listing pages. The relative `postedOn` label decides that
window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. The detail's exact `startDate`
re-checks the window after the fetch (the relative label has only day
granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. At 277 postings the board is small enough
that the first run needed **no `--max-pages` cap** — the window closes on its
own well before the `DEFAULT_MAX_PAGES` backstop.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review — Vantive
posts LATAM and EMEA country vacancies untranslated (e.g. "Analista de
Customer Service Jr").

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Vantive); the Workday legal
entity (e.g. "7735 Vantive Sdn. Bhd. MYS") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests
```

Outputs: `vantive_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/vantive.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
