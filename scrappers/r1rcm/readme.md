# r1rcm scraper

Scrapes R1 RCM's external careers board from the **Workday CXS JSON API**
at `r1rcm.wd1.myworkdayjobs.com` (tenant `r1rcm`, site `R1RCM`). ~505
open postings on 2026-08-28: ~380 US, ~100 India (Noida / Gurugram /
Hyderabad / Chennai / Bengaluru), ~25 Philippines. R1 is a US
revenue-cycle giant with an India delivery arm — the board is dominated
by out-of-scope US patient-registration / customer-service / RCM-ops spam
(headline in-scope rate ~4%), but carries a steady stream of genuine
**medical coding / HIM roles** (IP-DRG, certified freshers, DRG-validation
auditors, CDI), mostly in India. `company_type` is `hospital` (the
corrohealth convention for RCM services firms).

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/r1rcm/R1RCM/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id in `bulletFields`.
  `total` is only reliable on the offset=0 page.
- **Detail**: `GET /wday/cxs/r1rcm/R1RCM/job/<externalPath>` → full HTML
  description, exact `startDate`, `timeType`, country descriptor + ISO
  `alpha2Code`, canonical `externalUrl` (used verbatim). No `remoteType`
  key on this tenant — remoteness comes from the location strings
  ("Remote, USA", "Remote, CA").
- **robots.txt**: `Allow: /R1RCM/`, `Disallow: /R1RCMcwr/` (the
  contingent-worker site — never touched) and `/refreshFacet/`. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why no facet scoping

The `jobFamilyGroup` facet **cannot isolate coding/HIM** (probed
2026-08-28): medical coding sits inside "Revenue Cycle Operations" (417
of 505 postings) together with all the registration spam, and no
coding/HIM/job-family value exists to select. The remaining facets
(`Job_Requisition`, `timeType`, `Location_*`, `Management_Level`) don't
carry role semantics either. `searchText` is never used for scoping (the
Piramal-mirage lesson: Workday full-text silently over- and
under-matches). So: **fetch wide, filter tight**, with one cheap
optimization —

## Title pre-screen (this board only)

With ~96% of postings out of scope by title alone, fetching every
in-window detail would spend hundreds of requests only to drop nearly all
of them. `TITLE_PRESCREEN = True` gates the detail fetch:

1. `classify_job(title, "", "")` — title already in scope → fetch.
2. Otherwise, a coding/HIM token in the title (`PRESCREEN_RESCUE_RE`:
   HIM, health information, medical record, CDI, clinical documentation,
   coder/coding, DRG) → **fetch anyway** — those roles' scope signal
   usually lives only in the description (e.g. "HIM Specialist", "DRG
   Validation Auditor I").
3. Everything else → archived to `out-of-scope.csv` **from the listing
   alone** (no detail request), `matched_in = "title_prescreen"`,
   description empty — reversible: the id is retained, so deleting the
   row from out-of-scope.csv re-admits it on the next run.

The rescue forces a *fetch*, never a keep: the full-text shared
classifier owns every keep/drop decision for fetched rows, and its
vetoed_by breakdown (revenue-cycle/AR vetoes, proven on the
omega_healthcare board) is tallied in the run summary.

## Crawl strategy

505 postings > EXHAUSTIVE_BOARD_MAX (200), so the newest-first walk
early-stops after 2 consecutive pages with nothing inside the time
window, with the `--max-pages` / DEFAULT_MAX_PAGES (40) cap as a
backstop. **Beware pinned evergreens**: old requisitions (some with
2024 `startDate`s, re-listed "(Evergreen)" rows) sit scattered near the
top of the listing — the seen_old bookkeeping plus the detail
`startDate` re-check keep them out, and the early-stop only counts fully
old pages so a stray fresh row deep in the board can't stall it. The
first run was capped at `--max-pages 15` (300 newest postings ≈ well past
the 7-day window; the watermark takes over from there).

## Window & classification

The relative `postedOn` label decides the time window before any
pre-screen or detail request (out-of-window ids → `seen_old_ids.csv`,
unfetched); the detail's exact `startDate` re-checks it. First run keeps
the last 7 days; later runs use the newest stored `posted_date` minus 2
days grace.

Kept rows go through
`_shared/classification.classify_job(title, "", description)` (no curated
skills field on Workday CXS — and R1's "Working in an evolving healthcare
setting" boilerplate mentions revenue cycle everywhere, which the
classifier's coding keywords out-score for genuine coder roles).
`needs_review` rows are kept and logged to `needs_review.csv`.

Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
blank.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (70)
```

Outputs: `r1rcm_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/r1rcm.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv` (includes the title-prescreen
archive), `needs_review.csv`.
