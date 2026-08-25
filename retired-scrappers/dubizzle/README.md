# dubai.dubizzle.com scraper

Scrapes healthcare job postings from
[dubai.dubizzle.com/jobs/medical-healthcare/](https://dubai.dubizzle.com/jobs/medical-healthcare/)
into the shared HealthCareers.club schema.

## Data source

dubizzle's jobs vertical has a dedicated **Medical / Healthcare category**
(subcategories: Nurse / Healthcare Assistant, Other Medical Attendants), so
the healthcare filter runs at the source. Every listing page is
server-rendered and embeds the full Algolia result set in
`<script id="__NEXT_DATA__">` under the redux action
`listings/fetchListingDataForQuery/fulfilled`:

- `hits[]` — per ad: `name` (title), `uuid` (dedup key), `created_at`
  (epoch seconds; converted to a date in Asia/Dubai — it matches the date
  embedded in the ad URL), `absolute_url`, `location_list` (UAE →
  neighborhood), and `details_v2` with Monthly Salary (AED bucket),
  Employment Type, Remote Job, Min Work Experience, Min Education, Industry,
  Company Name, Benefits, Gender, Company Size.
- `pagination` — `{page, totalPages, hitsPerPage, totalHits}`.

Detail pages embed `listings/detailRequest/fulfilled` whose
`payload.listing.description` is the full plain-text description — fetched
only with `--enrich` (one extra page load per **new** job; the category is
small, so enrich is cheap and recommended).

## Why a headed browser

The whole site sits behind **Imperva/Incapsula Advanced Bot Protection**.
Tested and blocked: plain `requests`, `curl_cffi` with Chrome impersonation,
Playwright headless-shell, and full Chromium in new-headless mode. Only a
**headed** Chromium passes the challenge, so this scraper (unlike the others
in this repo) drives headed Playwright — the master spec's documented last
resort. A persistent profile in `.pw_profile/` reuses the Imperva cookies,
making later runs fast. A browser window pops up for ~30s per run.
`--headless` exists to re-test if Imperva ever relaxes.

robots.txt is honored: `/jobs/` is allowed; the disallowed `/api/` endpoints
are never called.

## Filtering (per ../../instructions/master-scraper-spec.md)

- **Healthcare**: source-side via the Medical/Healthcare category. Titles
  with no healthcare signal are still kept but flagged in `needs_review.csv`.
- **No salary filter** — salaries are captured, never filtered on.
- **Time window**: first run keeps the last `INITIAL_WINDOW_DAYS` (30);
  later runs keep jobs newer than the newest stored `posted_date` minus
  `WATERMARK_GRACE_DAYS` (2). The listing shows highlighted ads first (not
  date-sorted), so all pages are scanned and the cutoff applies per job.

### Salary is AED — blank in the club CSV

Salaries are monthly AED range buckets ("4,000 - 5,999"). The rich CSV keeps
the verbatim bucket plus parsed `salary_min_monthly`/`salary_max_monthly`
(AED). The club schema's `salary_currency` enum is INR/USD only, so the club
CSV's salary fields stay **blank** (same decision as dubailivejobs) — no
currency is invented.

Other quirks: `company_name` is usually "Confidential"
(`hide_company_name=True`); ads can stay listed for years, so the time
window (not the listing) bounds the crawl.

## Outputs

- `dubizzle_jobs.csv` — rich cumulative store (dedup key: ad `uuid`).
- `../../jobs_csv/<DD-MM-YYYY>/dubizzle.csv` — HealthCareers.club 22-column
  schema.
- `needs_review.csv` — kept-but-unclassifiable titles.

## Usage

```bash
# from scrappers/dubizzle/, using the repo venv
../../.venv/bin/python scraper.py --enrich          # daily run
../../.venv/bin/python scraper.py --max-pages 1     # quick test
python test_filters.py                              # unit tests (no network)
```

First-time setup: `pip install -r requirements.txt` then
`python -m playwright install chromium --no-shell` (full Chromium build —
the default headless shell is blocked by Imperva).

Note for cron: the run needs a logged-in GUI session (headed browser).
