# msd scraper

Scrapes MSD's global careers board from the **Workday CXS JSON API** at
`msd.wd5.myworkdayjobs.com` (tenant `msd`, site `SearchJobs`). MSD is the
name Merck & Co. trades under outside North America — a global
research-driven pharma. ~885 open postings on 2026-08-28, worldwide: a mix
of research/clinical development, regulatory, pharmacovigilance,
manufacturing/quality, plus a commercial/IT/finance tail the shared
classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/msd/SearchJobs/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. On this tenant deeper pages repeat the real `total`
  (unlike IQVIA's, which report 0); the first non-zero value is latched
  either way.
- **Detail**: `GET /wday/cxs/msd/SearchJobs/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /SearchJobs/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
- The branded careers site (jobs.merck.com) is **geo-blocked from India**,
  but this Workday tenant responds fine — the CXS route is the compliant
  data source for this fleet.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

Posting velocity is high for a board of ~885: at probe time (2026-08-28)
offset 100 was uniformly "Posted 2 Days Ago" and offset 200 uniformly
"Posted 3 Days Ago" — roughly **60–70 requisitions a day**. Ordering aged
monotonically with offset (Today → 2 Days → 3 Days), so newest-first holds
and the template's early stop is sound: the relative `postedOn` label
decides the time window **before** any detail request, out-of-window ids go
to `seen_old_ids.csv` unfetched, and the walk stops after 2 consecutive
pages with nothing in the window. Page 1 can pin a couple of stray older
rows ("Posted 8 Days Ago" sat at position 3 on probe day) among the newest —
harmless, since the early stop needs whole consecutive pages outside the
window. The detail's exact `startDate` re-checks the window after the fetch
(the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The seeding run was
`--since 2026-08-21 --max-pages 40`: it walked 23 pages (460 postings) and
stopped on the early-stop rule — 2 consecutive pages outside the window —
so the full 7-day window is genuinely closed, not page-starved. An earlier
`--max-pages 15` attempt ran out of pages after only ~4 days of backlog;
at this board's velocity a 7-day seed needs ~23 pages.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review — MSD
posts country-office vacancies untranslated.

**Locations (tenant adaptation).** Every detail location on this board is
the three-segment form `<ISO alpha-3> - <state/region> - <city>` — 94 of 94
distinct strings probed on 2026-08-28 ("USA - Pennsylvania - North Wales
(Upper Gwynedd)", "SGP - Singapore - Singapore (Boulevard Towers)"). So
`parse_city` here takes the **last** segment on that shape, not the first
survivor, which would publish the *state* as the city; two-segment and
comma forms keep the fleet's first-survivor rule. Three details matter:

- The three-segment split is on `" - "`, not the fleet's bare-hyphen
  regex — the latter cuts site names like "Bangkok (Tha-Central World)" in
  half and would yield "Central World)".
- The gate is the raw three-segment shape, **not** a surviving-segment
  count: "USA" is already dropped by the fleet's country-alias rule, so
  "USA - Pennsylvania - Rahway" leaves only two survivors.
- Where the region and city segments are identical ("USA - Florida -
  Florida", "IND - India - India", 21 of the 94 forms) the posting names no
  city, so the city is left blank rather than exporting a bare state. Cost:
  a genuine city-region repeat like "MYS - Kuala Lumpur - Kuala Lumpur"
  also blanks — preferred over publishing a wrong city.

The fleet-standard bare-uppercase-token skip is also in place, so the
alpha-3 prefix never leaks on any path.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (MSD); the Workday legal
entity (e.g. "1422 MSD Pharma (Singapore) Pte. Ltd.") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21 --max-pages 40   # the seeding run
python test_filters.py                            # unit tests (64)
```

Outputs: `msd_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/msd.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
