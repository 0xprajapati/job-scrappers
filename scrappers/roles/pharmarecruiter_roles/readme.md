# pharmarecruiter.in scraper

Scrapes pharma job postings from [pharmarecruiter.in](https://pharmarecruiter.in/)
into the HealthCareers.club CSV contract.

## Data source

WordPress site with a fully open REST API — no HTML listing pages are parsed:

```
GET https://pharmarecruiter.in/wp-json/wp/v2/posts
    ?categories=<jobs-id>&per_page=50&page=N
    &_fields=id,date,link,title,content,categories
```

- Posts return newest-first with the full article HTML (~6,500 job posts,
  a handful per day).
- Category ids are resolved from slugs at startup via
  `/wp-json/wp/v2/categories`. Only the `jobs` category is fetched, which
  skips `pharma-news` articles at the source.
- robots.txt (checked at startup) allows everything except `/wp-admin/` for
  generic agents; the API path is permitted.

## Field extraction

Every post embeds a labelled bullet list under a **Job Details** heading:

| Bullet label                                | Mapped to |
| ------------------------------------------- | --------- |
| Company Name / Organization / Employer      | `company` |
| Position / Designation / Post               | `position` |
| Location                                    | `city` + country (default India; abroad posts name the country) |
| Experience (`0-7 Years`, `Fresher Only`, …) | `min_experience` / `max_experience` |
| Qualification                               | `qualification` |
| Work Type (`Full-time, On-site`, …)         | `job_type` enum |
| Salary / Stipend / CTC (rare)               | `salary_*` fields |

The first value seen per label wins (the Job Details list precedes venue /
eligibility lists in the body).

## Quirks

- One post often advertises a multi-role walk-in drive; the post title is
  kept as the job title, and `position` holds the labelled role when given.
- Salary is almost never disclosed → `salary_raw = "Not Disclosed"`, numeric
  fields empty. When present, LPA / k / Indian-grouping amounts are parsed;
  no salary filtering ever (master spec).
- Club `category`: the site is pharma-industry, so production / QC / QA /
  R&D / regulatory titles are `non_clinical`; explicit pharmacist / doctor /
  nurse titles map to their clinical buckets. `company_type` defaults to
  `pharma` (hospital-keyword companies become `hospital`).
- Posts with no parseable company, or news-shaped titles that slipped into
  the jobs category, are kept and flagged `needs_review` (never dropped).

## Outputs

- `pharmarecruiter_jobs.csv` — rich cumulative store, deduped on WP post id.
- `../../jobs_csv/<DD-MM-YYYY>/pharmarecruiter.csv` — 22-column club schema.
- `needs_review.csv` — flagged rows from the latest run.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 2   # test run (2 pages of 50)
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests
```

First run keeps the last 2 days; later runs use the newest stored
`posted_date` minus 2 days grace as the cutoff, stopping pagination once a
whole page is older (the API is newest-first).

Etiquette: descriptive User-Agent, robots.txt check at startup, ≥1s delay
between requests, exponential backoff (3s → 24s) on 429/5xx.
