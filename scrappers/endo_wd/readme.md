# endo scraper

Scrapes Endo's global careers board from the **Workday CXS JSON API** at
`endo.wd1.myworkdayjobs.com` (tenant `endo`, site `External`). **98 open
postings on 2026-08-28** — one of the smallest boards in this fleet. Endo is
a US specialty pharmaceutical company (branded and sterile injectable
products, generics via Par Pharmaceutical / Par Health, plus the
Mallinckrodt businesses it combined with). Supply is manufacturing-plant
shaped: QA/QC, microbiology, analytical R&D, production and packing at the
Indian sites (Indore, Chennai/Pudupakkam, Alathur) and the US plants
(Raleigh NC, Greenville IL, Rochester MI, St. Louis MO), plus sales, HR and
engineering the shared classifier drops.

## Data source

- **Listing** (paged):
  `POST /wday/cxs/endo/External/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0).
- **Detail**: `GET /wday/cxs/endo/External/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /External/`, `Allow: /Paladin/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Sites on this tenant

`External` is the only site scraped. The tenant also serves **`Paladin`**
(Paladin Labs, Endo's separately-branded Canadian subsidiary): probed
2026-08-28, it returned a single "Posted 30+ Days Ago" posting. Excluded —
different brand, and nothing inside any window this scraper uses.

## Ordering soundness (probed 2026-08-28)

Ordering is **not strictly newest-first**. Page 0 pins a block of six
"Posted 30+ Days Ago" rows in the middle (positions 5–10) and then returns
to "Posted Yesterday":

```
offset   0: 2d, 2d, 7d, 8d, 30+, 30+, 30+, 30+, 30+, 30+,
            Yesterday, Yesterday, Yesterday, 2d, 2d, 3d, 3d, 3d, 7d, 10d
offset  20: 10d, 10d, 11d, 14d, 14d, 17d, 17d, 18d, 23d, 24d, 25d, 25d,
            28d, 30d, 30d, 30+ …
offset  40: all 30+
offset  60: all 30+
offset  80: all 30+ (18 rows — the board ends at 98)
```

So labels age at **page** granularity but not within page 0. The
consecutive-old-pages early stop would therefore be unsound here — and it
never runs: `total` (98) is well under `EXHAUSTIVE_BOARD_MAX` (200), so this
board takes the **exhaustive path** and every listing page is walked to the
end. Five listing pages is the whole cost. No tenant-specific override was
needed; `DEFAULT_MAX_PAGES` (40) already exceeds `ceil(98/20)+5 = 10`.

Second quirk: offsets **past `total` wrap around** — `offset=100` on this
98-posting board returned the offset-0 page again (with `total` back to 98).
Harmless, because the walk breaks at `offset >= total` first, but a page
fetched past the end must never be trusted.

## Why the window matters here

Endo posts roughly **2–4 requisitions a day**, so the board turns over
slowly. The relative `postedOn` label decides the time window **before** any
detail request: out-of-window ids go to `seen_old_ids.csv` unfetched, so the
~70 stale "30+ Days Ago" rows cost nothing but the listing pages they sit
on. The detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. No `--max-pages` cap was needed — the
whole board is five listing pages.

### Seed run was a `--since` backfill

The seed run (`first_run.log`) was
`scraper.py --since 2026-06-01`, not a plain first run. Rationale: this
scraper keeps zero rows (see below), so `endo_jobs.csv` is never created,
so `compute_cutoff` has no watermark and every subsequent run would re-use
the same rolling 7-day `INITIAL_WINDOW_DAYS` gate forever — anything in
scope but older than 7 days would be permanently unreachable. The `--since`
backfill widened the date gate over the entire live board (including the
30+-day tail) as a one-off standing inventory. It cost 86 detail fetches;
the board is small enough that this is cheap to repeat if the taxonomy
changes. **This remains true until the first row is kept** — re-run with
`--since` after any classifier change rather than assuming the daily
incremental will find the backlog.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Endo); the Workday legal
entity — which varies across the merged group ("Par Formulations",
"Mallinckrodt Enterprises LLC") — is kept raw in `hiring_org`.
`additionalLocations` is `null` (not `[]`) on this tenant.

### Current yield: zero

The `--since 2026-06-01` standing inventory fetched **86 details and kept
0**. All 71 rows in `out-of-scope.csv` re-classify as `in_scope: False`, so
these are genuine shared-classifier verdicts, not a scraper fault. Endo's
board is plant-floor and back-office: manufacturing and packing operators,
maintenance/mechanical trades, warehouse, EHS, security, finance, HR,
supply chain, inside sales.

**Open taxonomy question (not a scraper change):** 18 of the 71 drops are
pharma QA/QC and analytical-lab roles — `Sr Analyst, Quality Control`,
`Analyst, Microbiology (QC)`, `Analyst, Global Stability`, `Quality
Assurance Senior Associate`, `Sr Mgr, Quality Assurance`, `Research
Associate, Analytical R&D` — mostly at Pudupakkam and Raleigh. If the
shared taxonomy ever takes in GMP quality/analytical roles, this board goes
from zero to roughly a quarter in scope overnight. Do not add local filters
here; the shared classifier owns that decision.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # standing inventory (seed run)
python test_filters.py                           # unit tests (62)
```

Outputs: `endo_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/endo.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
