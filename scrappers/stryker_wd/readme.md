# stryker scraper

Scrapes Stryker's global careers board from the **Workday CXS JSON API** at
`stryker.wd1.myworkdayjobs.com` (tenant `stryker`, site `StrykerCareers`).
~1,186 open postings on 2026-08-28. Stryker is one of the world's largest
medical-device companies (orthopaedics, surgical equipment, neurotechnology);
supply is device-shaped: quality/regulatory, clinical affairs, R&D
engineering, plus a long tail of sales/finance/IT/manufacturing the shared
classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/stryker/StrykerCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400, probe-verified
  2026-08-28). Each posting carries title, externalPath, locationsText, a
  *relative* `postedOn` label ("Posted Today" … "Posted 30+ Days Ago") and
  the requisition id in `bulletFields`. `total` is only reliable on the
  offset=0 page (deeper pages report 0).
- **Detail**: `GET /wday/cxs/stryker/StrykerCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /StrykerCareers/`, `Allow: /StrykerAgencySite/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- **Sites**: only `StrykerCareers` is crawled. robots.txt also advertises a
  `StrykerAgencySite` — that is the recruitment-agency submission portal,
  not a candidate board for the same brand's public vacancies, so it is
  excluded per the fleet convention (no University/Referral/agency sites).
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

The board posts roughly **40–50 requisitions a day** (probed 2026-08-28:
offset 100 was 2 days old, offset 200 was 4 days old, offset 300 already
8 days old), so detail-fetching the whole ~1,186-posting board would be
wasteful. Instead the relative `postedOn` label decides the time window
**before** any detail request: out-of-window ids go to `seen_old_ids.csv`
unfetched, and the newest-first walk stops after 2 consecutive pages with
nothing in the window. The detail's exact `startDate` re-checks the window
after the fetch (the relative label has only day granularity).

**Ordering soundness (checked 2026-08-28)**: newest-first holds — labels
age monotonically with offset (offset 20 "Posted Yesterday" → 100 "2 Days
Ago" → 200 "4 Days" → 300 "8 Days" → 400 "11 Days" → 600 "21–22 Days" →
900+ "30+ Days"), and `total` (1,186) is not a round display cap. One
quirk: the offset=0 page pins ~8 stray older "featured" rows (8–30+ days
old) ahead of the newest postings. That does not break the early stop
(page 1 always also contains in-window rows); the pinned old rows are
simply recorded to `seen_old_ids.csv` unfetched.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings ≈ the last ~7–8 days) to keep
the initial burst modest — the watermark takes over from there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Stryker posts country-office vacancies untranslated ("(m/w/d)" roles etc.).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Stryker); the Workday legal
entity (e.g. "Stryker European Operations Limited - Dutch Branch Office")
is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (61)
```

Outputs: `stryker_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/stryker.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
