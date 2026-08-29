# alcon scraper

Scrapes Alcon's global careers board from the **Workday CXS JSON API** at
`alcon.wd5.myworkdayjobs.com` (tenant `alcon`, site `careers_alcon`). ~393
open postings on 2026-08-28. Alcon is the world's largest eye-care company
(surgical ophthalmic devices, contact lenses, ocular-health pharma). Supply
is device/manufacturing-shaped: QA/QC, regulatory operations, R&D quality
and medical affairs in scope, plus a long tail of field sales, production
operators, engineering, IT and shared-services roles the shared classifier
drops. Big hubs: Fort Worth, Tuas (Singapore), Batam, Johor, Grosswallstadt
(Germany), Bangalore.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/alcon/careers_alcon/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (format `R-2026-49200`). `total` is only reliable on the
  offset=0 page (deeper pages report 0 — probe-verified on this tenant).
- **Detail**: `GET /wday/cxs/alcon/careers_alcon/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /careers_alcon/`, `Disallow: /refreshFacet/` only
  — the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

**Newest-first ordering was probe-verified on 2026-08-28**: postedOn labels
age monotonically with offset — "Posted Today" at offset 0, "Posted 9–11
Days Ago" at offset 100, "Posted 21–22 Days Ago" at offset 200 — so the
template's consecutive-old-pages early stop is sound on this tenant. The
board posts roughly **10–11 requisitions a day**, so a daily incremental run
closes its 7-day/watermark window within the first few listing pages: the
relative `postedOn` label decides the window **before** any detail request,
out-of-window ids go to `seen_old_ids.csv` unfetched, and the walk stops
after 2 consecutive pages with nothing in the window. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has
only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings ≈ the last ~4 weeks of a 393-post
board) to keep the initial burst modest — the watermark takes over from
there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review — Alcon posts
country-office vacancies untranslated (German "(m/w/d)" roles from
Grosswallstadt, plus Japanese and Chinese titles the DE/FR/ES stopword
sniff does not catch).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Alcon); the Workday legal
entity (e.g. "D007 CIBA Vision GmbH Company") is kept raw in `hiring_org`.

Location quirk: some strings carry a trailing all-uppercase site/state token
("Ciudad de Mexico – AGS", "Remote - Texas") — the fleet-standard ISO-token
skip in `parse_city` keeps those out of the city column.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21   # explicit window
python test_filters.py                            # unit tests
```

Outputs: `alcon_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/alcon.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
