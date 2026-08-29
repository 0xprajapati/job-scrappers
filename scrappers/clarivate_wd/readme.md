# clarivate scraper

Scrapes Clarivate's global careers board from the **Workday CXS JSON API** at
`clarivate.wd3.myworkdayjobs.com` (tenant `clarivate`, site
`Clarivate_Careers`). **160 open postings on 2026-08-28** — a small board,
walked completely every run (≤ `EXHAUSTIVE_BOARD_MAX`). Clarivate is an
analytics & IP company; the in-scope supply comes from its life-sciences /
real-world-evidence division (Cortellis, DRG): RWE/HEOR analysts, medical
writers, regulatory/consulting roles, plus a long tail of software, sales
and IP roles the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/clarivate/Clarivate_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (`JREQ...`). Quirk vs. other tenants: deeper pages keep
  reporting the true `total` (160) instead of 0 — harmless, only the
  offset=0 value is latched anyway.
- **Detail**: `GET /wday/cxs/clarivate/Clarivate_Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, a `remoteType` field (`Hybrid`/…), the country
  descriptor + ISO `alpha2Code`, and the canonical `externalUrl`.
  `hiringOrganization.name` is empty on this tenant.
- **robots.txt**: `Allow: /Clarivate_Careers/`, `Allow: /jobs/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering & why the window matters here

Newest-first ordering was **probe-verified on 2026-08-28**: offset 0 labels
ran `Posted Today → Posted 4 Days Ago` monotonically, offsets 100 and 140
were uniformly `Posted 30+ Days Ago`, and offset 200 (> total) returned an
empty page. The template's early-stop would be sound — but at 160 postings
the board is under `EXHAUSTIVE_BOARD_MAX` and is simply walked to the end
(8 listing pages) every run.

The relative `postedOn` label still gates detail fetches: out-of-window ids
go to `seen_old_ids.csv` unfetched, so a run costs 8 listing pages plus one
detail per genuinely new posting. Clarivate posts ~4–5 requisitions a day
(the 7-day window covered roughly the first 2 pages at probe time). The
detail's exact `startDate` re-checks the window after the fetch. Steady
state keeps the newest stored `posted_date` minus 2 days grace. No
`--max-pages` cap is needed — the whole board is 8 listing pages.

**Seed run was a `--since` backfill.** The default 7-day first-run window
kept 0 rows here (that week's postings were all software/IP/sales), and
with an empty store `compute_cutoff` has no watermark to advance — every
subsequent run would have re-applied the same 7-day window and never
reached the older RWE/HEOR roles standing on the board. The store was
therefore seeded with a standing-inventory backfill,
`scraper.py --since 2026-06-01` (the fleet's remedy for a zero-keep board;
`first_run.log` is that run), which kept 3 rows and established a watermark
at 2026-07-23. Daily incremental runs work normally from here.

## Locations quirk

Locations are prefixed with ISO-3 country codes and office tags:
`"IND - Bangalore (DRG)"`, `"USA - Philadelphia"`. The fleet-standard
`parse_city` amendment (skip bare 2-3 letter all-uppercase tokens) stops
the `IND`/`USA` leak; the office suffix survives whole ("Bangalore (DRG)").
This scraper's local `parse_city` additionally handles Clarivate's
*internal office codes*: the skip pattern is widened to
`[A-Z]{2,3}|[A-Z]\d{2,4}` so a leading code like `R155` in
`"R155-Belgrade"` is never taken as the city (→ "Belgrade"), and a
trailing parenthesized office code is stripped before segmenting, so
`"Remote (121- Massachusetts)"` → `"Remote"` → the remote logic stamps
`city = "Remote"`. Both behaviors are pinned in `test_filters.py`.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV) — on this board that is the majority (software/IP/sales-heavy).
`needs_review` rows are kept and logged to `needs_review.csv`; non-English
(DE/FR/ES) descriptions are kept but forced into review.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Clarivate); `hiring_org` is
blank here (the tenant publishes no legal-entity name).

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # the seed backfill
python test_filters.py                           # unit tests
```

Outputs: `clarivate_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/clarivate.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
