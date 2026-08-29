# clarioclinical scraper

Scrapes Clario's global careers board from the **Workday CXS JSON API** at
`clarioclinical.wd1.myworkdayjobs.com` (tenant `clarioclinical`, site
`clarioclinical_careers`). Clario is a clinical-trial endpoint-data
technology company (eCOA, cardiac safety, medical imaging for drug trials;
part of Thermo Fisher Scientific). **77 open postings on 2026-08-28** — the
smallest Workday board in this fleet. Supply is trial-technology-shaped:
imaging research associates, image QC/analysis, project management, plus a
software/IT tail the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/clarioclinical/clarioclinical_careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, timeType, a *relative* `postedOn`
  label ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (e.g. `R17087`). Quirk: this tenant reports the real
  `total` (77) on deeper pages too (some tenants report 0 there) — the
  offset=0 latch is simply a no-op here.
- **Detail**: `GET /wday/cxs/clarioclinical/clarioclinical_careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. Quirk: `hiringOrganization.name` is **empty**
  on this tenant — `hiring_org` exports blank; `company` carries the brand
  (Clario).
- **robots.txt**: `Allow: /clarioclinical_careers/`, `Disallow:
  /InviteToApply/` and `/refreshFacet/` only — the CXS API paths are
  allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

**Ordering soundness (probed 2026-08-28)**: `postedOn` labels age
monotonically with offset — `Posted Today`/`Posted Yesterday`/`Posted 2-3
Days Ago` at offsets 0-19, `Posted 11-22 Days Ago` at offset 40, `Posted
30`/`30+ Days Ago` at offset 60 (the board ends at 77, so deeper offsets
don't exist). Newest-first holds and the template's early-stop logic is
sound — though at 77 postings the board sits below `EXHAUSTIVE_BOARD_MAX`
(200) and is **walked completely every run** (4 listing pages), so the
big-board early stop never engages here.

The relative `postedOn` label still decides the time window **before** any
detail request: out-of-window ids go to `seen_old_ids.csv` unfetched. The
board posts roughly **5-7 requisitions a day**, so a daily run is 4 listing
pages plus one detail per genuinely new posting. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has
only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. No `--max-pages` cap was needed on the
first run — the whole board is only 4 pages.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests
```

Outputs: `clarioclinical_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/clarioclinical.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
