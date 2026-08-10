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
  "filter at the source"), then a broad `healthcare` net plus
  role-specific slugs (`staff-nurse`, `doctor`, `pharmacist`,
  `physiotherapist`, `lab-technician`, `hospital`, `medical`) to recover
  healthcare jobs filed under other industries. Dedup by job id makes the
  overlap free. Override with `--queries a,b,c` (a query may carry extra
  params after `?`).

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
- Every record carries its industry name in `jInd`; the healthcare gate
  uses it for cards found via the keyword queries (cards returned by the
  `ind=13` browse all carry `jInd = "Medical / Healthcare"`, verified).
- Salary strings: `Rs 4.0 - 4.5 Lakh/Yr`, `< Rs 50,000 - 2.5 Lakh/Yr`
  (mixed absolute + lakh in one string) or `[Salary Hidden]` (majority).
  Comma-numbers are absolute rupees; bare numbers < 1000 are lakhs.
- `jLoc` may be `["All India"]` — exported as city "All India" rather than
  inventing a city.

## Healthcare filter (spec §2)

1. `DENY_TITLE_KEYWORDS` (telesales, counsellors, IT/engineering, …) →
   excluded, even at a healthcare employer.
2. `ALLOW_TITLE_KEYWORDS` (clinical + healthcare-business vocabulary) → kept.
3. `jInd == "Medical / Healthcare"` → kept.
4. `jInd` empty or `"Others"` with an unreadable title → kept, flagged
   `needs_review` (never silently dropped).
5. Any other named industry (IT, BFSI, BPO, …) → excluded.

## Outputs

- `shine_jobs.csv` — rich cumulative store, dedup key `job_id`; watermark
  source of truth.
- `needs_review.csv` — titles the classifier could not confidently place.
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

# unit tests (salary/experience parsers, classifier, cutoff)
../../.venv/bin/python test_filters.py
```

Etiquette: ≥1.2s between requests, 3 retries with exponential backoff on
429/5xx, per-card failure isolation, page cap + explicit truncation NOTE.
