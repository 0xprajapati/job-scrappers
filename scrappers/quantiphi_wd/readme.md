# quantiphi scraper

Scrapes Quantiphi's careers board from the **Workday CXS JSON API** at
`quantiphi.wd1.myworkdayjobs.com` (tenant `quantiphi`, site
`Careers_at_Quantiphi`). **120 open postings on 2026-08-28.** Quantiphi is an
AI-first digital engineering and consulting firm whose practices include
healthcare and life sciences.

Supply is services-company shaped — ML/data engineers, solution architects,
platform and client-partner roles — split between US/Canada remote positions
(43) and Indian delivery centres (Bengaluru, Mumbai Eureka, Trivandrum, 30).
Only the healthcare/life-sciences-facing tail is in scope for this fleet.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/quantiphi/Careers_at_Quantiphi/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0).
- **Detail**: `GET /wday/cxs/quantiphi/Careers_at_Quantiphi/job/<externalPath>`
  → `jobPostingInfo` with the full HTML description (2–10 kB stripped), the
  exact posted date (`startDate`), `timeType`, the country descriptor + ISO
  `alpha2Code`, and the canonical `externalUrl`. This tenant sends
  `additionalLocations: null` rather than `[]`.
- **robots.txt**: `Allow: /Careers_at_Quantiphi/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL. One site slug is advertised and it is the one
  configured.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Pagination quirk — this board never ends

Probe-verified 2026-08-28, and **specific to this tenant**: once `offset`
reaches `total` (120) the API *wraps around* and serves the offset=0 page
again, forever. Offsets 120, 140, 160 … 300 each returned the same 20 newest
requisitions (`JR11867` … `JR11608`), never an empty page. Across offsets
0–100 the id set holds exactly 120 unique requisitions, so `total` is honest;
it is the paging past the end that lies.

A crawler that walks "until an empty page" would loop indefinitely here. What
bounds this walk is the latched `total` from the offset=0 page — the
template's `offset >= total` break. Worth knowing before anyone raises
`DEFAULT_MAX_PAGES` or relaxes that stop condition for this tenant.

## Ordering soundness

With `searchText=""`, `postedOn` labels age monotonically with offset:

| offset | label |
|-------:|-------|
| 0   | Posted Today |
| 20  | Posted 8 Days Ago |
| 40  | Posted 29 / 30 Days Ago |
| 100 | Posted 30+ Days Ago |
| 115 | Posted 30+ Days Ago |

Newest-first holds, and `total` (120) is the true unique-id count rather than
a round display cap. It makes no practical difference here: 120 is under
`EXHAUSTIVE_BOARD_MAX` (200), so the board is **walked completely** every run
(6 listing pages) and the consecutive-old-pages early stop never engages. The
`postedOn` window gate still runs first, so out-of-window postings cost no
detail request.

## Why the window matters here

This is a long-lived board refreshed in batches: of 120 postings, **74 are
"Posted 30+ Days Ago"** and only 17 were inside the 7-day window at probe
time. The relative label keeps the detail spend proportional — the first run
fetched 17 details for a 120-posting board, and steady state is one fetch per
genuinely new requisition.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. No `--max-pages` cap is needed on a board
this small.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review (none seen — the
board is all-English).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Quantiphi); the Workday legal
entity (e.g. "Quantiphi Analytics Solutions Private Limited") is kept raw in
`hiring_org`.

## First run (2026-08-28)

| scanned | fetched | kept | out-of-scope | out-of-window | detail failures |
|--------:|--------:|-----:|-------------:|--------------:|----------------:|
| 120 | 17 | 1 | 16 | 103 | 0 |

The walk ended cleanly on `offset >= total` at 120 — the wrap-around hazard
above never engaged. All 16 drops carry an empty `matched_in` (a real
"no in-scope signal" verdict, not a parse fault; descriptions are 2.3–10 kB
and locations parse). The single keep is
`Senior Business Analyst - Life Science` (Bengaluru, India → Non Clinical /
Clinical Research, matched in the description).

Known quirks recorded from that run:

- **Space-separated ISO prefixes leak into `city`.** Quantiphi writes Indian
  and some US locations as `<ISO> <state> <city>` — "IN KA Bengaluru"
  (14 postings), "IN MH Mumbai Eureka" (14), "US NJ Princeton",
  "CA NB Fredericton". `parse_city` splits only on commas and hyphens, so
  the whole string survives as the city and the fleet ISO-token skip (which
  matches a *bare* 2–3 letter segment) never fires. The one kept row
  therefore exports `city_name = "IN KA Bengaluru"`. Not patched here — a
  fleet-wide `parse_city` amendment is the right fix; a characterization
  test pins the current behaviour in `test_filters.py`.
- `Senior Business Analyst (Payer)` and `Senior Business Analyst (Provider)`
  — US healthcare payer/provider BA roles — were dropped as no-match while
  the near-identical `Senior Business Analyst - Life Science` was kept.
  Possible taxonomy gap; this scraper adds no filters of its own.

## Usage

```bash
../../.venv/bin/python scraper.py                      # daily incremental run
../../.venv/bin/python scraper.py --limit 5            # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window / backfill
python test_filters.py                                 # unit tests (65)
```

Outputs: `quantiphi_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/quantiphi.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
