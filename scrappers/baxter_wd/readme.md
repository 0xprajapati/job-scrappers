# baxter scraper

Scrapes Baxter's global careers board from the **Workday CXS JSON API** at
`baxter.wd1.myworkdayjobs.com` (tenant `baxter`, site `baxter` — note the
lowercase site slug). ~565 open postings on 2026-08-28 — a mid-sized board
for this fleet. Baxter is a medical products & therapies company (IV fluids,
infusion systems, renal care, advanced surgery), so supply is
device/manufacturing-shaped: quality, regulatory affairs, clinical/medical
affairs and field service, plus a long tail of sales/engineering/finance the
shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/baxter/baxter/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` — spelled with spaces on this tenant (`"JR - 197008"`).
  `total` is only reliable on the offset=0 page (deeper pages report 0).
- **Detail**: `GET /wday/cxs/baxter/baxter/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /baxter/`, `Disallow: /refreshFacet/` only — the
  CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness & why the window matters

Probed 2026-08-28 with `searchText=""`: offset 0 was "Posted Today /
Posted Yesterday", offset 100 "Posted 4 Days Ago", offset 200 "Posted 11
Days Ago" … "Posted 14 Days Ago" — labels age **monotonically** with
offset, and `total` (565) is not a suspicious round cap, so newest-first
ordering holds and the template's consecutive-old-pages early stop is
sound.

The board posts roughly **25 requisitions a day** (offset 100 was already 4
days old at probe time). The relative `postedOn` label decides the time
window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has
only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings ≈ the last ~2 weeks of
velocity) to keep the initial burst modest — the watermark takes over from
there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Baxter posts country-office vacancies untranslated ("(m/w/d)" roles etc.).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Baxter); the Workday legal
entity (e.g. "8000 Baxter Deutschland GmbH DEU") is kept raw in
`hiring_org`.

Location quirks: some remote rows carry the marker in parentheses
("United Kingdom (remote)"); one FR site is spelled "Pluvigner, FRA" — the
fleet-standard ISO-token skip in `parse_city` keeps bare 2-3 letter
uppercase codes from leaking out as cities.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests
```

Outputs: `baxter_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/baxter.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
