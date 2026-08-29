# cencora scraper

Scrapes Cencora's global careers board from the **Workday CXS JSON API** at
`myhrabc.wd5.myworkdayjobs.com` (tenant `myhrabc`, site `Global`). Cencora is
the pharmaceutical distribution & services company formerly known as
AmerisourceBergen (the group includes World Courier and PharmaLex). ~970 open
postings on 2026-08-28. Supply is distribution/pharmacy-shaped: warehouse and
equipment-operator roles (dropped by the classifier), pharmacy technicians,
pharmacists, patient services, plus an international tail (Boots apotheek
Netherlands, Turkey, Brazil, UK, Canada).

## Data source

- **Listing** (paged):
  `POST /wday/cxs/myhrabc/Global/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0).
- **Detail**: `GET /wday/cxs/myhrabc/Global/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /Distribution/`, `Allow: /Global/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- **Site slugs**: the tenant serves two sites. `Global` is the main board
  (~970 postings). `Distribution` held only **3 postings** at probe time
  (same brand — a Cencora pharmacist, a QA intern, a personal-care
  specialist) and its job details resolve under `Global` too; it is
  **excluded** as a negligible legacy slice of the same tenant.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering is NOT newest-first (why there is no early stop)

The fleet's Workday template early-stops big boards after 2 consecutive
listing pages outside the time window — sound only when the default ordering
is newest-first. **On this tenant it is not** (probed 2026-08-28 with
`searchText:""`):

- offset 0 page: `Posted Yesterday, 2 Days, 2 Days, …, 16 Days, 30+ Days …`
  and then back to `Posted Today` in its last rows;
- offset 100: `Posted 2 Days Ago …` (newer than rows on page 0);
- offset 200: `Posted 7 Days Ago …`.

Labels do **not** age monotonically with offset, so a fully-old page proves
nothing. The early stop is disabled for this scraper: each site's listing is
walked to its end (empty page / offset ≥ total) with `DEFAULT_MAX_PAGES`
raised to `ceil(970/20)+5 = 54` as the backstop. The board `total` (970) is
not a round display cap, and a full walk is cheap anyway: ~49 listing pages,
with the relative `postedOn` label still gating every detail fetch — only
in-window postings (~200 of 970 were ≤7 days old at probe time, ≈25–30
requisitions/day) cost a detail request. The detail's exact `startDate`
re-checks the window after the fetch.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. the first 300 listing rows) to keep the initial
burst modest — the watermark takes over from there.

## Locations

Two formats coexist: plain `"Denver, CO"` strings and a region-tier `>`
hierarchy — `"USA > KY > Brooks > 345 International"`,
`"WEMEA > Netherlands > Boots apotheek Den Haag"`,
`"NCEE > Turkey > Antalya > Caybasi"`, `"LATAM > Brazil > Remote"`.
`parse_city` therefore also splits on `>`, and the fleet's ISO-token skip is
widened to bare all-uppercase tokens of 2–5 letters so the region codes
(`USA`, `WEMEA`, `NCEE`, `LATAM`) and US state codes never leak as cities.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV) — on this board that is most of the volume (warehouse, drivers,
finance, IT). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES/NL) descriptions are kept but forced into review —
Cencora posts country-office vacancies untranslated (Dutch Boots apotheek
roles, French "STAGE … (H/F)" etc.).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Cencora); the Workday legal
entity (e.g. "AmerisourceBergen Drug Corporation", "Cencora Patient
Services, LLC", "Alliance Apotheek Nederland BV") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21   # explicit window
python test_filters.py                            # unit tests (63)
```

Outputs: `cencora_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/cencora.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
