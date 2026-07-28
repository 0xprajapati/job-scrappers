# michaelpage.co.in healthcare scraper

Scrapes the [healthcare vertical](https://www.michaelpage.co.in/jobs/healthcare)
of Michael Page India (PageGroup's executive-search brand, Drupal site).
Because the crawl target is the healthcare sector listing, every job is
healthcare-industry at the source. Roles are executive/corporate mandates
(sales heads, GMs, medical affairs, plant heads) — mostly `non_clinical`.

## Data source

No public JSON API. Two structured sources instead:

| Purpose | Source |
|---|---|
| Job list | Server-rendered cards at `/jobs/healthcare?page=N` (30/page, ~5 pages) |
| Job detail | schema.org **JobPosting JSON-LD** on `/job-detail/<slug>/ref/<jn-ref>` |

Cards carry title, detail URL (`ref/jn-XXXXXX-XXXXXXX` = dedup key), internal
id, location, contract type (Permanent/Temporary), teaser and highlight
bullets — but **no posted date and no salary**. The detail JSON-LD adds
`datePosted`, `employmentType`, `industry`, the full HTML description,
`jobLocation` and `baseSalary` (literal annual INR, e.g. 8000000–10000000;
empty for the many confidential mandates).

robots.txt allows these paths (`*/jobs/*/*/*/` only matches deeper facet
URLs; `?page=` is fine, the disallowed `item_per_pages` param is not used).

## Quirks

* The listing is **not date-sorted** (featured jobs first), so every run
  scans all pages; the time-window cutoff is applied per job.
* Posted dates exist **only on detail pages**, so new refs must be detail-
  fetched before the cutoff check. Refs that turn out to be out-of-window
  are recorded in `seen_old_ids.csv` so later runs never re-fetch them —
  a steady-state daily run makes ~5 listing requests + one detail request
  per genuinely new job.
* The JSON-LD contains raw control characters → parsed with a non-strict
  JSON decoder.
* Hiring companies are confidential recruiter mandates: `company` is
  "Michael Page"; the client blurb bullets ("Leading Private Equity Firm")
  go to `company_about`, and the description keeps the "About Our Client"
  section.
* `location` may be "India" or "International" (no city) — city left blank,
  country defaults to India (.co.in market).
* Titles with no healthcare word in title+industry+summary are kept and
  flagged `needs_review` (logged to `needs_review.csv`), never dropped.

## Outputs

* `michaelpage_jobs.csv` — rich cumulative store, deduped on the JN ref;
  append-only across runs.
* `../../jobs_csv/<DD-MM-YYYY>/michaelpage.csv` — HealthCareers.club
  22-column schema (all cumulative rows), refreshed every run.
* `seen_old_ids.csv` — out-of-window refs (detail-fetch skip list).
* `needs_review.csv` — titles with no healthcare signal, for manual review.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 1   # sample run (first 30 cards)
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests (no network)
```

Time window per the master spec: first run keeps the last 30 days; later
runs keep jobs newer than the newest stored `posted_date` minus 2 grace
days (dedup absorbs the overlap). Running twice in a row adds 0 rows.
