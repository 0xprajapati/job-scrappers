# swaasa.com scraper

Scrapes healthcare jobs from [swaasa.com](https://www.swaasa.com/in/en/search-results),
an India-focused healthcare job portal built on the Phenom People platform.

## Data source

- **Primary**: Phenom's JSON widget endpoint —
  `POST https://www.swaasa.com/widgets` with `ddoKey: refineSearch`,
  `sort: {order: desc, field: postedDate}`, `from`/`size` pagination
  (50 jobs per page, newest first). Returns the same structured payload the
  search page embeds in `phApp.ddo`: title, jobId, category, location,
  salary string, teaser, company, postedDate, jobSeqNo.
- **Details**: `https://www.swaasa.com/in/en/job/<jobSeqNo>` embeds
  `phApp.ddo.jobDetail` with the full description. Fetched for NEW jobs only
  (on by default; `--no-details` skips — daily volume is only a handful of
  jobs so this is cheap).
- **robots.txt**: checked at startup; only tracking/apply widget paths are
  disallowed (`*/px-widgets`, `*/apply`, …) — the widgets endpoint and job
  pages are allowed.

## Why no sub-pages

The nav's sub-pages (Abroad Jobs, Healthcare Jobs, Freshers Jobs, Latest
Jobs, `c/doctor-jobs`, …) are all filtered views over the same single search
index (~22,900 jobs; verified: `c/doctor-jobs` totalHits = the Doctor slice
of the master facet). Scraping the master index once covers every sub-page;
`site_category` and `country` columns preserve the segment info.

## Quirks

- **Salary strings are free-form**: `"₹3,00,000 – ₹5,00,000 per Year"`,
  `"18000-30000 monthly"`, `"25,000"`, `"890000"`, `"Best in Industry"`,
  empty. When no period is stated, amounts ≥ 100,000 are read as per-year,
  smaller as per-month. Raw string always kept; nothing invented or filtered.
- **Dirty `country` field**: states/cities leak into it ("Telangana",
  "Ahmedabad"); anything not in the known-country map collapses to India.
- **Category taxonomy is healthcare-native** (Doctor, Nursing, Pharmacist,
  Lab Technician, …). Adjacent categories (Sales/Marketing, Insurance, Other,
  …) are kept and flagged `needs_review` when the title shows no healthcare
  keyword — never silently dropped.
- No employment-type field in the data; `job_type` defaults to `full_time`.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 2   # test run
python scraper.py --no-details    # skip full-description fetches
python test_filters.py            # unit tests
```

Outputs: `swaasa_jobs.csv` (rich cumulative store, dedup on jobId) and
`../../jobs_csv/<DD-MM-YYYY>/swaasa.csv` (HealthCareers.club 22-column
schema). First run keeps the last 30 days; later runs use the watermark
(newest stored date − 2 days grace).
