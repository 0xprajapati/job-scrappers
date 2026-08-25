# shine.com healthcare job scraper

Scrapes healthcare job listings from [shine.com](https://www.shine.com)
(HT Media's India job board) per `instructions/master-scraper-spec.md`.

## Data source

shine.com is a Next.js site. Every `/job-search/<query>-jobs` page is
server-rendered with the full 20-result payload embedded in
`<script id="__NEXT_DATA__">` at
`props.pageProps.initialState.jsrp.searchresult.data.results` — title,
company, salary string, full description HTML, locations, posted/expiry
datetimes, experience range, industry, keywords and the detail-page slug.
No detail-page fetches and no headless browser are needed.

- **robots.txt**: `/api/*` is disallowed, so the JSON API is never called
  (master spec §1). `/job-search/` pages are allowed and are all we fetch;
  the scraper re-checks robots.txt per-URL at startup. A descriptive
  User-Agent gets the same payload as a browser UA.
- **URL scheme**: page 1 is `/job-search/<query>-jobs?sort=1`, page N is
  `/job-search/<query>-jobs-<N>?sort=1`. `sort=1` = newest-first (the
  default is relevance, which surfaces months-old jobs on page 1).
  `ind=13` selects the "Medical / Healthcare" industry facet and works as
  a URL param (~21k jobs vs ~26k for the bare keyword search; the param
  is `ind`, **not** the facet field name `jIndID`, which is ignored).
- **Queries**: the `healthcare?ind=13` industry browse first (guaranteed
  coverage of everything filed under Medical / Healthcare, spec §2
  "filter at the source"), then the broad `healthcare`, `hospital` and
  `medical` nets to recover in-scope jobs filed under other industries.
  Dedup by job id makes the overlap free. Override with `--queries a,b,c`
  (a query may carry extra params after `?`).
  The bedside-role slugs (`staff-nurse`, `doctor`, `pharmacist`,
  `physiotherapist`, `lab-technician`) were dropped in the 2026-08-25
  taxonomy migration — every card they return is titled with a vetoed
  clinical profession, so the classifier dropped essentially all of them.

## Quirks (verified live)

- **shine re-dates reposts.** With `sort=1`, page 30 of the healthcare
  query was still entirely dated "today" — 600+ jobs share the same posted
  date. The date watermark therefore cannot bound crawl depth on its own;
  `--max-pages` (default 50/query) caps each query and the run summary
  prints a NOTE when a query was cut off by the cap instead of the date
  window. Dedup absorbs the re-served reposts across runs.
- Newest-first ordering is only approximate (promoted cards interleave),
  so early-stop triggers only when an **entire page** is older than the
  cutoff.
- Every record carries its industry name in `jInd` (cards returned by the
  `ind=13` browse all carry `jInd = "Medical / Healthcare"`, verified).
  Since the taxonomy migration this is a **raw source column only** — it is
  stored in the rich CSV's `industry` field and decides nothing.
- Salary strings: `Rs 4.0 - 4.5 Lakh/Yr`, `< Rs 50,000 - 2.5 Lakh/Yr`
  (mixed absolute + lakh in one string) or `[Salary Hidden]` (majority).
  Comma-numbers are absolute rupees; bare numbers < 1000 are lakhs.
- `jLoc` may be `["All India"]` — exported as city "All India" rather than
  inventing a city.

## Classification (taxonomy migration 2026-08-25)

The old five-step `is_healthcare` gate and the per-scraper category regexes
are **retired**. The only keep/drop and labeling decision is the shared
`_shared/classification.py`:

```python
classify_job(jJT, jKwd, strip_html(jJD))
```

- `title` = `jJT`, `skills` = `jKwd` (shine's curated keyword tags),
  `description` = the HTML-stripped `jJD`.
- `in_scope == False` → the card is dropped and counted as
  `excluded_out_of_scope` in the run summary. It is never exported.
- In-scope rows get `category` (`Non Clinical` | `Public Health`),
  `sub_category`, plus the `role_family` / `all_families` /
  `family_scores` / `family_confidence` / `matched_in` score trace in the
  rich CSV so any admission can be audited.
- `needs_review == True` → the row is kept **and** appended to
  `needs_review.csv`.
- Rows that were in the store before the migration and no longer classify
  in scope were moved to `out-of-scope.csv` (same columns, reversible).

The club CSV's 22 columns come from `CLUB_COLUMNS` in
`_shared/classification.py`; `is_active`/`expires_at` are retired
(shine's expiry still lives in the rich CSV's `expires_date`), and
`qualification` is a grounded extraction from the description only.

## Outputs

- `shine_jobs.csv` — rich cumulative store, dedup key `job_id`; watermark
  source of truth.
- `needs_review.csv` — in-scope rows whose title looks like a different
  profession (kept, flagged).
- `out-of-scope.csv` — rows the classifier rejected during the one-off
  migration of the stored data; nothing is silently discarded.
- `../../jobs_csv/<DD-MM-YYYY>/shine.csv` — HealthCareers.club 22-column
  schema, rewritten every run.

## Usage

```bash
# daily incremental run (first run keeps the last 7 days)
../../.venv/bin/python shine_scraper.py

# test run
../../.venv/bin/python shine_scraper.py --max-pages 2 --limit 40

# recover a window (e.g. after downtime)
../../.venv/bin/python shine_scraper.py --since 2026-08-01

# unit tests (salary/experience parsers, classifier wiring, cutoff)
../../.venv/bin/python test_filters.py
```

Etiquette: ≥1.2s between requests, 3 retries with exponential backoff on
429/5xx, per-card failure isolation, page cap + explicit truncation NOTE.
