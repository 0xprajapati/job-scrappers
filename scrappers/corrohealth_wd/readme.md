# corrohealth scraper

Scrapes CorroHealth's careers boards from the **Workday CXS JSON API** at
`corrohealth.wd1.myworkdayjobs.com` (tenant `corrohealth`). CorroHealth is a
US revenue-cycle-management / clinical coding company, so the in-scope seam
is medical coding, auditing, CDI and utilization management.

**One scraper, TWO Workday sites on the same tenant**, iterated in order and
recorded per row in the `workday_site` column:

| site | what it is | size (2026-08-27) |
|---|---|---|
| `Corro` | US board — mostly `US - Remote` coding/auditing/UM roles | ~26 postings |
| `CorroHealthIndia` | India board — Noida/Chennai RCM operations | ~124 postings |

The tenant's third site (`Virtix`, a sister brand) has its own tiny board
and is deliberately not crawled.

## Data source

- **Listing**: `POST /wday/cxs/corrohealth/<site>/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400); `total` is only
  reliable on the offset=0 page.
- **Detail**: `GET /wday/cxs/corrohealth/<site>/job/<externalPath>` → full
  HTML description, exact `startDate`, `timeType`, country descriptor + ISO
  `alpha2Code`, canonical `externalUrl`.
- **robots.txt**: `Allow: /Corro/ /Virtix/ /CorroHealthIndia/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Quirks

- Both boards are ≤200 postings, so each is walked **completely** every run
  (2 + 7 listing pages) rather than trusting newest-first early-stopping;
  the time window still decides which ids get a detail fetch.
- US locations are alias-style (`US - Remote`) → country United States,
  city `Remote` (himalayas convention). India locations are city+building
  strings (`Noida Luminaire`, `Chennai Prince Infocity II`) — kept whole as
  the city; the building name is honest source data.
- The India board bulk-(re)posts: whole pages sharing one posted date are
  normal. Dedup on `job_id` and the watermark absorb it.
- India management titles are RCM-flavored (`AGM/DGM - PMO`,
  `AM - RCM Services`) — many are genuinely out of scope for the taxonomy
  and land in `out-of-scope.csv`; the coding/auditing floor roles are the
  in-scope seam.
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank. `company_type` is `hospital` (club enum has only
  hospital | pharma; RCM services sit on the provider side).

## Window & classification

First run keeps the last 7 days; later runs use the newest stored
`posted_date` (across both sites) minus 2 days grace. Out-of-window ids →
`seen_old_ids.csv`, never detail-fetched; the detail's exact `startDate`
re-checks the window.

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` (no curated
skills field on Workday CXS). `in_scope: False` → `out-of-scope.csv`;
`needs_review` rows kept and logged to `needs_review.csv`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run (both sites)
../../.venv/bin/python scraper.py --limit 5       # smoke test
python test_filters.py                            # unit tests (58)
```

Outputs: `corrohealth_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/corrohealth.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
