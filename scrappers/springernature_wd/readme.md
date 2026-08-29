# springernature scraper

Scrapes Springer Nature's global careers board from the **Workday CXS JSON
API** at `springernature.wd3.myworkdayjobs.com` (tenant `springernature`,
site `SpringerNatureCareers`). **66 open postings on 2026-08-28** — a small
board that fits in 4 listing pages. Springer Nature is one of the world's
largest scientific/academic publishers (Nature, Springer, BMC, Scientific
American); in-scope supply is editorial/publishing-shaped — scientific
editors for BMC and Nature journals, research-integrity screening,
medical-writing-adjacent roles — plus a long tail of sales, IT, finance and
publishing-operations titles the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/springernature/SpringerNatureCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0 — probe-verified on this tenant).
- **Detail**: `GET /wday/cxs/springernature/SpringerNatureCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /SpringerNatureCareers/`, `Disallow:
  /refreshFacet/` only — the CXS API paths are allowed. Checked at startup,
  honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering & why the window barely matters here

Ordering probe (2026-08-28, searchText="", offsets 0/20/40/60): postedOn
labels aged strictly monotonically — Yesterday → 2 → 3 → 4 → 7 → 8 → …
→ 28 → 30+ Days Ago, with the final page ending in six "Posted 30+ Days
Ago" rows. **Newest-first ordering holds**, so the template's early-stop
logic is sound — but with only 66 postings the board is below
`EXHAUSTIVE_BOARD_MAX` (200) and is walked completely anyway (4 listing
pages). The relative `postedOn` label still gates detail fetches: only
in-window postings cost a detail request, out-of-window ids go straight to
`seen_old_ids.csv` unfetched. Velocity is modest — the newest ~18 postings
spanned 4 days at probe time (roughly 4-5 requisitions/day), so a steady-
state daily run is 4 listing pages plus a handful of detail fetches.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. No `--max-pages` cap was needed on the
first run (small board).

Quirk: an externalPath tail can carry a "-1" republish suffix
(`...JR106589-1`) while `bulletFields` holds the clean requisition id
(`JR106589`) — bulletFields-first id extraction keeps the dedup key stable.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Springer Nature posts German-office vacancies with "(m/w/d)" titles and
occasionally untranslated bodies.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Springer Nature); the Workday
legal entity (e.g. "0290 Springer-Verlag GmbH") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (61)
```

Outputs: `springernature_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/springernature.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
