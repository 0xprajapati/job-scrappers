# evolent scraper

Scrapes Evolent Health's careers board from the **Workday CXS JSON API** at
`evolent.wd1.myworkdayjobs.com` (tenant `evolent`, site `External`). **29
open postings on 2026-08-28** — the smallest board in this fleet. Evolent
is a US value-based / specialty care management company: it partners with
health plans and provider groups on oncology, cardiology, musculoskeletal
and advanced-imaging specialty care, running utilization management and
clinical review on their behalf. Supply is unusually clinical for a Workday
tenant — Field Medical Directors and physician reviewers by specialty
(radiology, oncology, cardiology, MSK surgery, vascular), clinical APP
managers, medical-policy roles — sitting over a Pune engineering / finance /
HR back office.

`External` is the tenant's only public site (it is the slug named in
`robots.txt` and in the sitemap), so `WORKDAY_SITES` has one entry.

## Data source

- **Listing** (paged): `POST /wday/cxs/evolent/External/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id
  (`JR-……`) in `bulletFields`. `total` is only reliable on the offset=0
  page (deeper pages report 0).
- **Detail**: `GET /wday/cxs/evolent/External/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` arrives as `null`, not
  `[]`, on single-site postings.
- **robots.txt**: `Allow: /External/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness — UNSOUND, and why that is harmless here

Probed 2026-08-28 with `searchText=""`, walking the whole board:

| row | 0 | 1 | 2 | 3 | 5 | 7 | 14 | 17 | 20 | 28 |
|---|---|---|---|---|---|---|---|---|---|---|
| label | 30+ Days | 30+ Days | **Today** | Yesterday | 2 Days | 7 Days | 8 Days | 15 Days | 25 Days | 30+ Days |

Two 30+-day-old requisitions are **pinned above** the newest posting, so
newest-first does **not** hold and the fleet's consecutive-old-pages early
stop would be unsound on this tenant.

It never runs. At 29 postings the board is far under
`EXHAUSTIVE_BOARD_MAX` (200), so `crawl_site` takes the walk-everything
branch: both listing pages are read to the end on every run and the early
stop is unreachable. `DEFAULT_MAX_PAGES` (40) already exceeds the
`ceil(29/20)+5 = 7` a full walk needs, so no constant was changed.

**Maintenance note:** if this board ever grows past `EXHAUSTIVE_BOARD_MAX`
the early stop would engage — and would be unsound. Re-probe the ordering
before that happens. The frozen `total` in `test_filters.py` documents the
assumption but cannot detect growth at runtime.

## Tenant quirk — "Work at Home" is the remote marker

The board has exactly two location strings: `Work at Home` (21 postings)
and `Pune` (8). The fleet remote pattern (`remote` / `home-based` /
`work from home`) does **not** match "Work at Home", so without an
amendment every US row would export city `Work at Home` and job_type
`full_time`. `_REMOTE_RE` is therefore widened here to
`work\s+(?:from|at)\s+home`; those rows now resolve to city `Remote` and
job_type `remote` under the himalayas convention. `parse_city` also carries
the fleet-standard ISO/state-token skip (no location on this board needs
it today, but it is the fleet default).

## Why the window matters here

Velocity is low — offset 10 was already 7 days old at probe time, i.e. a
handful of requisitions a week. The relative `postedOn` label still decides
the time window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, so a daily run costs 2 listing requests plus
one detail per genuinely new posting. The detail's exact `startDate`
re-checks the window after the fetch (the relative label has only day
granularity, and "30+ Days Ago" is a floor, not a date).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. `--max-pages` was **not** used on the
first run — the board is two pages long.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review (this board is
US/India English-only in practice).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Evolent Health); the Workday
legal entity (e.g. "Evolent Specialty Services, Inc") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                     # daily incremental run
../../.venv/bin/python scraper.py --limit 5           # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01  # explicit window / backfill
python test_filters.py                                # unit tests (65)
```

Outputs: `evolent_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/evolent.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
