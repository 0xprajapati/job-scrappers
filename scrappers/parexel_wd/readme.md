# parexel scraper

Scrapes Parexel's external careers board from the **Workday CXS JSON API**.
Parexel (global CRO) lives on the *shared* Workday host
`wd1.myworkdaysite.com` (tenant `parexel`, site `Parexel_External_Careers`)
rather than a vanity `*.myworkdayjobs.com` domain. ~360 open postings on
2026-08-27, and the majority are in-scope for this fleet — clinical
operations, data management, statistical programming, regulatory, PV — the
highest in-scope rate of the four Workday boards.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/parexel/Parexel_External_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Postings carry a
  *relative* `postedOn` label and the requisition id in `bulletFields`;
  `total` is only reliable on the offset=0 page.
- **Detail**: `GET /wday/cxs/parexel/Parexel_External_Careers/job/<externalPath>`
  → full HTML description, exact `startDate`, `timeType`, country
  descriptor + ISO `alpha2Code`, and the canonical `externalUrl` — which on
  this host lives under `/recruiting/parexel/Parexel_External_Careers/…`
  and is used verbatim as the application URL (do not hand-build it from
  the host root).
- **robots.txt**: the host answers **HTTP 422** for `/robots.txt` — no
  robots file, no restrictions. The startup check still runs so a
  later-added file would be honored.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 without them); ≥1.5 s between
  requests, exponential backoff on 429/5xx.

## Quirks

- **Hyphen-separated locations**: `Germany-Berlin-Remote`,
  `United Kingdom-London-Gridiron-Remote` (country, city, sometimes a
  building, then a Remote marker). The city parser splits on hyphens/commas
  and drops country aliases, ISO codes and remote markers; a "Remote"-only
  string exports city `Remote` (himalayas convention).
- `timeType` is often empty on this tenant → defaults to `full_time`
  (or `remote` when the location says so).
- Salary never shown → `salary_raw = "Not Disclosed"`, club salary columns
  blank.

## Window & classification

Newest-first walk; the relative `postedOn` label decides the time window
before any detail request (out-of-window ids → `seen_old_ids.csv`,
unfetched), early-stop after 2 consecutive fully-old pages. First run keeps
the last 7 days; later runs use the newest stored `posted_date` minus 2 days
grace.

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` (no curated
skills field on Workday CXS). `in_scope: False` → `out-of-scope.csv`;
`needs_review` (including non-English DE/FR/ES descriptions) kept and
logged to `needs_review.csv`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 5       # smoke test
python test_filters.py                            # unit tests (58)
```

Outputs: `parexel_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/parexel.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
