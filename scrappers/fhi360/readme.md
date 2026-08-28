# fhi360 scraper

Scrapes FHI 360's external careers board from the **Workday CXS JSON API**
at `fhi.wd1.myworkdayjobs.com` (tenant `fhi`, site
`FHI_360_External_Career_Portal`). FHI 360 is a global health and
development NGO (fhi360.org); ~38 open postings on 2026-08-28, posted from
country offices across Africa and Asia (Philippines, Indonesia, Morocco...).
Roughly 1 in 5 titles is in-scope — MEL, HIS/HMIS, surveillance, MNCH — and
unlike the Wave-1 CRO boards the **Public Health** category dominates keeps.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/fhi/FHI_360_External_Career_Portal/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id in `bulletFields`;
  `total` is only reliable on the offset=0 page.
- **Detail**:
  `GET /wday/cxs/fhi/FHI_360_External_Career_Portal/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl` (same host) — used verbatim.
- **robots.txt**: `Allow: /FHI_360_External_Career_Portal/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL. robots.txt must be requested with
  `Accept: text/plain` (the session's `Accept: application/json` makes
  Workday answer 406 for it).
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Quirks (vs. the Wave-1 Workday four)

- **Requisition ids carry a prefix**: `bulletFields`/`jobReqId` say
  `Requisition - 2026201200` while the `externalPath` tail says
  `Requisition-2026201200` (different spacing). The prefix is stripped and
  the bare number (`2026201200`) is the stable `job_id` dedup key, whichever
  source supplied it.
- **Locations are "City, Country"** (`Manila, Philippines` — city *first*,
  the reverse of PATH), plus `USA-Remote (Any)` and per-state remote hubs
  `US-REMOTE-DC` / `US-REMOTE-NC` for US-remote rows. The remote segment's
  `(Any)` qualifier is matched too, and a bare two-letter state
  abbreviation is never taken as the city — so those rows export city
  `Remote` / job_type `remote` (himalayas convention). Multi-hub rows list
  `locationsText: "3 Locations"`; the extra hubs land in
  `additional_locations`. The
  `jobRequisitionLocation` descriptor carries building noise
  (`Philippines-Manila (F; Paseo De Roxas Bldg)`) — the cleaner top-level
  `location` field is what gets parsed.
- `hiringOrganization.name` is the legal entity "Family Health
  International" — kept raw in `hiring_org`; `company` is the FHI 360 brand.
- `company_type` is `hospital` — the fleet convention for NGOs in the
  club's two-value enum.
- Details carry an `endDate` / "days left to apply" (application deadline) —
  ignored; `startDate` is the posted date.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank.

## Window & classification

~38 postings ≤ `EXHAUSTIVE_BOARD_MAX` (200), so **every run walks the whole
board** (2 listing pages) — Workday's default ordering can pin a stale row
at the top, making early-stopping unsound on small boards. The relative
`postedOn` label still decides the time window before any detail request
(out-of-window ids → `seen_old_ids.csv`, unfetched). First run keeps the
last 7 days; later runs use the newest stored `posted_date` minus 2 days
grace.

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` (no curated
skills field on Workday CXS). `in_scope: False` → `out-of-scope.csv`;
`needs_review` (including non-English FR/ES/DE descriptions — FHI 360 posts
francophone country-office vacancies untranslated, the UNDP-01 convention)
kept and logged to `needs_review.csv`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (62)
```

Outputs: `fhi360_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/fhi360.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
