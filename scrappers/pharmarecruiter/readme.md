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

## Classification

Every candidate post goes through the one shared classifier,
`scrappers/_shared/classification.py`:

```python
verdict = classify_job(title, site_categories, description)
```

- `title` — the post title; `skills` — the site's own category slugs
  (`jobs; production-jobs; qc-jobs`, …); `description` — the HTML-stripped
  body.
- `in_scope == False` → the post is **dropped** and counted as
  `excluded_out_of_scope` in the run summary. This site is pharma-industry
  but mostly manufacturing / QC / QA / R&D, none of which is one of the
  eleven in-scope role families, so a large share of posts is now dropped.
- In-scope posts get `category` (`Non Clinical` | `Public Health`) and
  `sub_category`, plus the audit trail (`role_family`, `all_families`,
  `family_scores`, `family_confidence`, `matched_in`) in the rich CSV.
- `needs_review == True` → the row is kept **and** appended to
  `needs_review.csv`.

The site's category slugs are a raw source column (`site_categories`) and a
scoring signal only — they never decide the category. This scraper defines
no category regexes or profession enums of its own.

## Quirks

- One post often advertises a multi-role walk-in drive; the post title is
  kept as the job title, and `position` holds the labelled role when given.
- Salary is almost never disclosed → `salary_raw = "Not Disclosed"`, numeric
  fields empty. When present, LPA / k / Indian-grouping amounts are parsed;
  no salary filtering ever (master spec).
- `company_type` defaults to `pharma` (hospital-keyword companies become
  `hospital`).
- `_NEWSY_TITLE_RE` is **junk detection only**: a listicle / guide / exam-result
  title that slipped into the jobs category, or a post with no parseable
  company, is kept and flagged `needs_review` — it never decides a category.
- Club `qualification` uses the labelled `Qualification:` bullet when the post
  has one, else `extract_qualification(description)`; it is never inferred.
  `is_active` and `expires_at` are retired from the club schema.

## Outputs

- `pharmarecruiter_jobs.csv` — rich cumulative store, deduped on WP post id.
- `../../jobs_csv/<DD-MM-YYYY>/pharmarecruiter.csv` — the shared 23-column
  club schema (`CLUB_COLUMNS`, imported from `_shared/classification.py`).
- `needs_review.csv` — flagged rows from the latest run.
- `out-of-scope.csv` — rows the one-off stored-data reclassification moved
  out of the rich store; reversible, never silently discarded.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 2   # test run (2 pages of 50)
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests
```

First run keeps the last 30 days; later runs use the newest stored
`posted_date` minus 2 days grace as the cutoff, stopping pagination once a
whole page is older (the API is newest-first).

Etiquette: descriptive User-Agent, robots.txt check at startup, ≥1s delay
between requests, exponential backoff (3s → 24s) on 429/5xx.
