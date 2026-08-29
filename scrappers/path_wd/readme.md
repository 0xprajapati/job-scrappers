# path scraper

Scrapes PATH's external careers board from the **Workday CXS JSON API** at
`path.wd1.myworkdayjobs.com` (tenant `path`, site `External`). PATH is a
global health NGO (path.org); ~64 open postings on 2026-08-28, posted from
country program offices across Africa and Asia (Nigeria, Kenya, Uganda,
India...). Roughly 4 in 10 titles are in-scope — malaria M&E, epidemiology,
health informatics, HEOR, MNCH — and unlike the Wave-1 CRO boards the
**Public Health** category dominates keeps.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/path/External/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id (`JR2764`) in
  `bulletFields`; `total` is only reliable on the offset=0 page.
- **Detail**: `GET /wday/cxs/path/External/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl` (same host, `/External/...`) — used verbatim.
- **robots.txt**: `Allow: /External/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
  robots.txt must be requested with `Accept: text/plain` (the session's
  `Accept: application/json` makes Workday answer 406 for it).
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Quirks (vs. the Wave-1 Workday four)

- **Duty stations are office names, country FIRST**:
  `Nigeria, Birnin Kebbi Sub-Office`,
  `India, New Delhi Country Program Office`,
  `Bangladesh, Dhaka Project Office`,
  `United States, Seattle Headquarters Office`,
  `Vietnam, Hanoi Regional Program Office`. The city parser strips the
  trailing office suffix *before* segment splitting (the suffix's own
  hyphen would otherwise be split apart), then drops the country segment —
  leaving the bare city (`Birnin Kebbi`, `New Delhi`, `Dhaka`, `Seattle`). PATH abbreviates the Democratic
  Republic of the Congo as **"DRC"** (`DRC, Kinshasa Country Program
  Office`) — added to the country-alias drop list.
- Some listing rows have `locationsText: null` and an empty detail
  `location` (statewide India postings, e.g. "..., Andhra Pradesh" in the
  title) → city stays blank, country comes from the detail's country block.
- `hiringOrganization.name` is the local legal entity ("Organization For
  Innovation in Public Health Limited by Guarantee - Nigeria") and is often
  blank — kept raw in `hiring_org`; `company` is always the PATH brand.
- `company_type` is `hospital` — the fleet convention for NGOs in the
  club's two-value enum.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank.

## Window & classification

~64 postings ≤ `EXHAUSTIVE_BOARD_MAX` (200), so **every run walks the whole
board** (4 listing pages) — Workday's default ordering can pin a stale row
at the top, making early-stopping unsound on small boards. The relative
`postedOn` label still decides the time window before any detail request
(out-of-window ids → `seen_old_ids.csv`, unfetched). First run keeps the
last 7 days; later runs use the newest stored `posted_date` minus 2 days
grace.

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` (no curated
skills field on Workday CXS). `in_scope: False` → `out-of-scope.csv`;
`needs_review` (including non-English FR/ES/DE descriptions — PATH posts
francophone country-office vacancies untranslated, the UNDP-01 convention)
kept and logged to `needs_review.csv`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (62)
```

Outputs: `path_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/path.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
