# pharmabharat.com scraper

Scrapes pharma job postings from [pharmabharat.com](https://pharmabharat.com/)
into the HealthCareers.club CSV contract.

## Data source

WordPress site with a fully open REST API — no HTML listing pages are parsed:

```
GET https://pharmabharat.com/wp-json/wp/v2/posts
    ?per_page=50&page=N
    &_fields=id,date,link,title,content,categories
```

- Posts return newest-first with the full article HTML (~11,500 posts,
  a few dozen per day).
- Unlike pharmarecruiter.in there is **no jobs/news split**: every category
  on the site is a job-type taxonomy (production-jobs, qc-jobs,
  clinical-research-jobs, pharmacovigilance-jobs, …), so all posts are
  scraped and the slugs stored in `site_categories`.
- robots.txt is empty (checked at startup anyway) — everything is allowed.

## Field extraction

Posts embed their facts in one of **three shapes**, all handled by
`extract_labeled_fields()` (first value per field wins):

1. A "Job Details" table — `<tr><td>Company</td><td>Sandoz</td></tr>`
2. Labelled bullets — `<li><strong>Experience:</strong> 2–6 Years</li>`
3. Labelled paragraphs — a `Label:` line with the value on the same or the
   following text line (common in walk-in posts)

| Label                                        | Mapped to |
| -------------------------------------------- | --------- |
| Company / Company Name / Organization        | `company` |
| Position / Designation / Job Role(s)         | `position` |
| Location / Job Location                      | `city` + country (default India) |
| Experience (`2–6 Years`, `Freshers`, …)      | `min_experience` / `max_experience` |
| Qualification / Eligibility                  | `qualification` |
| Job Type / Employment Type + Work Mode       | `job_type` enum |
| Salary / Estimated Salary / Stipend / CTC    | `salary_*` fields |
| Application Deadline (`28 July 2026`)        | `expires_at` |

## Quirks

- Many "Salary" values are the **site's own estimates** written in prose
  ("Based on industry standards…"). Only compact values containing a real
  amount (₹ / LPA / digits) are parsed; the raw string keeps qualifiers
  like "(Estimated)". Prose-only or absent salary → `"Not Disclosed"`,
  numeric fields empty; no salary filtering ever (master spec).
- Parentheticals are stripped before amount extraction so "(based on 2025
  standards)" is never read as a salary number.
- One post often advertises a multi-role walk-in drive; the post title is
  kept as the job title, and `position` holds the labelled role when given.
- Club `category`: the site is pharma-industry, so production / QC / QA /
  clinical research / PV titles are `non_clinical`; explicit pharmacist /
  doctor / nurse titles (or the site's `pharmacist` category) map to their
  clinical buckets. `company_type` defaults to `pharma`.
- Posts with no parseable company (facts only in prose), or news-shaped
  titles, are kept and flagged `needs_review` (never dropped).

## Outputs

- `pharmabharat_jobs.csv` — rich cumulative store, deduped on WP post id.
- `../../jobs_csv/<DD-MM-YYYY>/pharmabharat.csv` — 22-column club schema.
- `needs_review.csv` — flagged rows from the latest run.

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
