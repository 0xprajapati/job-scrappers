# iconplc — ICON plc careers (careers.iconplc.com)

ICON plc is a world-scale CRO; its whole board is Non-Clinical CRO supply:
CRAs, clinical trial management, clinical data management, regulatory
affairs, medical writing, pharmacovigilance, biostatistics. Title-only
in-scope rate measured at **68%** on the 2026-08 probe sweep — the best
board of the sweep. Global: US, Mexico, Poland, Brazil, Bulgaria, UK,
Ireland, India (Chennai/Bangalore), China, Japan, and ~40 more countries.

## Data source

The site runs on the **Attrax ATS**. robots.txt advertises three sitemaps
and **disallows `/jobs?*`** (the faceted search pages) while allowing
`/job/...` detail paths — so the crawl is **sitemap only**:

```
https://careers.iconplc.com/vacanciessitemap.xml    (~880 job URLs)
    -> /job/<title-slug>-jid-<numeric id>           (jid = stable job id)
```

Each detail page embeds one schema.org **JobPosting JSON-LD** block:
`title`, full HTML `description`, exact `datePosted`/`validThrough`, the
JR-prefixed requisition id (`identifier`), an `industry` list (ICON's own
department tag — passed to `classify_job` as `skills`, unmodified), an
`employmentType` list, and a `jobLocation` list whose only address field
is a combined `addressLocality` string.

Flow: fetch the sitemap (1 request) → skip jids already in the rich CSV /
seen_old_ids.csv / out-of-scope.csv (known jids cost zero requests) →
fetch detail pages for new jids only, highest jid first (~newest first) →
parse JSON-LD → time-window on `datePosted` → shared classifier →
append. Steady state ≈ 1 sitemap request + 1 detail request per genuinely
new posting.

## Quirks (all probe-verified 2026-08-27)

* The sitemap's `<lastmod>` is a **modification stamp, not the posting
  date** (a Tokyo job with datePosted 2025-08-25 carried lastmod
  2026-08-25). It is ignored; `datePosted` lives only in the detail
  JSON-LD, so each new jid costs one detail fetch before its window can
  be judged — out-of-window jids land in `seen_old_ids.csv` and are never
  fetched again (the devnetjobsindia idiom).
* `addressLocality` shapes: `"India, Chennai"`, `"United States of
  America"` (country only), `"California"` (bare US state), `"Chicago,
  IL"`, `"US, Blue Bell (ICON)"` / `"Singapore, Singapore (Labs)"`
  (parenthetical site tags, stripped), `"Regional United States (PRA)"`
  (home-based coverage region → marked remote, exported with city_name
  **"Remote"**, the himalayas convention). A bare `"Georgia"` is read as
  the US state — the country always arrives as `"Georgia, Tbilisi"`.
  Unknown tokens export blank country code/dial, never guessed.
* `baseSalary` was **null on every probed page** (even US postings);
  parsing supports the schema.org shape anyway. Absent → `salary_raw
  "Not Disclosed"`, blank club salary columns. Only USD/INR
  annual|monthly pay is club-exportable.
* JSON-LD parsed with `raw_decode(strict=False)` defensively (the blocks
  are currently clean, but Attrax gets no benefit of the doubt).
* 403/404 are permanent — **never retried**. Throttle ≥1.5 s/request.

## First run (2026-08-27)

Capped at `--limit 120` detail fetches (documented cap — the board holds
~880 mostly months-old postings; later runs continue incrementally through
the remaining jids via the seen-id stores). Log: `first_run.log`.

To sweep the whole backlog regardless of the watermark, use
`--since 2000-01-01` (each jid is still fetched at most once, ever).

## Outputs

* `iconplc_jobs.csv` — rich cumulative store (dedup key: `job_id` = jid).
* `../../jobs_csv/<DD-MM-YYYY>/iconplc.csv` — HealthCareers.club
  `CLUB_COLUMNS` schema (imported from `_shared/classification.py`).
* `seen_old_ids.csv` — out-of-window jids (detail-fetch skip list).
* `out-of-scope.csv` — classifier-dropped rows, full and reversible.
* `needs_review.csv` — kept but flagged.

Company is always ICON (club `company_type` "pharma" — it's a CRO).

## Usage

```bash
cd scrappers/iconplc
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 120     # capped run
../../.venv/bin/python scraper.py --since 2026-07-01   # widen the window
../../.venv/bin/python test_filters.py            # unit tests
```
