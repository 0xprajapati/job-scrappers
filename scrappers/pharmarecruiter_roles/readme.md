# pharmarecruiter.in — role-targeted scraper

Search-driven scrape of [pharmarecruiter.in](https://pharmarecruiter.in/) that
asks the site only for the **eleven in-scope role families** instead of walking
its whole `jobs` category.

Sibling of `../pharmarecruiter`, which does the full-category crawl. Both read
the same site and share the same field parsers; they differ only in *how much*
they fetch and in their output filenames, so the two can be run independently
without clobbering each other.

## Data source

WordPress site with a fully open REST API — no HTML listing pages are parsed:

```
GET https://pharmarecruiter.in/wp-json/wp/v2/posts
    ?categories=<jobs-id>&search=<term>&per_page=50&page=N
    &_fields=id,date,link,title,content,categories
```

- WordPress's `search` parameter is **full text over the title AND the body**,
  so a listing that names the domain only in its requirements still comes back.
- `SEARCH_TERMS` holds one query per role family — `clinical research`,
  `clinical trials`, `clinical data management`, `pharmacovigilance`,
  `drug safety`, `regulatory affairs`, `medical writing`, `medical writer`,
  `medical coding`, `medical science liaison`, `medical affairs`,
  `medical monitor`, `health economics`, `market access`,
  `trial master file`, `public health`, `epidemiology`. The crawl works one
  term at a time.
- The `jobs` category id is resolved from its slug at startup via
  `/wp-json/wp/v2/categories`, so `pharma-news` articles are skipped at source.
- Posts return newest-first with the full article HTML.
- robots.txt (checked at startup) allows everything except `/wp-admin/` for
  generic agents; the API path is permitted.

## Time window

Narrower than the master-spec default: **first run keeps the last 2 days**
(`INITIAL_WINDOW_DAYS = 2`). These feeds post fast, so every extra day of
first-run window costs a lot of crawl for jobs that are already stale by import
time. Later runs ignore it entirely and use the newest stored `posted_date`
minus 2 days of grace (`WATERMARK_GRACE_DAYS`) as the cutoff.

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

`SEARCH_TERMS` is a **crawl-side filter that only saves requests** — it never
labels anything. Full-text search matches the body, so plenty of production /
QC posts come back that merely *mention* "clinical research". The keep/drop and
labelling decision belongs entirely to the one shared classifier,
`scrappers/_shared/classification.py`:

```python
verdict = classify_job(title, site_categories, description)
```

- `title` — the post title; `skills` — the site's own category slugs
  (`jobs; production-jobs`, …); `description` — the HTML-stripped body.
- `in_scope == False` → the post is **dropped** and counted as
  `excluded_out_of_scope` in the run summary.
- In-scope posts get `category` (`Non Clinical` | `Public Health`) and
  `sub_category`, plus the audit trail (`role_family`, `all_families`,
  `family_scores`, `family_confidence`, `matched_in`) in the rich CSV.
- `needs_review == True` → the row is kept **and** appended to
  `needs_review.csv`.

This scraper defines no category regexes or profession enums of its own.

## Quirks

- One post can be returned by several search terms; `job_id` (the WP post id)
  dedupes them in the rich store.
- One post often advertises a multi-role walk-in drive; the post title is
  kept as the job title, and `position` holds the labelled role when given.
- Salary is almost never disclosed → `salary_raw = "Not Disclosed"`, numeric
  fields empty. When present, LPA / k / Indian-grouping amounts are parsed;
  no salary filtering ever (master spec).
- `company_type` defaults to `pharma` (hospital-keyword companies become
  `hospital`).
- `_NEWSY_TITLE_RE` is **junk detection only**: a listicle / guide / exam-result
  title, or a post with no parseable company, is kept and flagged
  `needs_review` — it never decides a category.
- Club `qualification` uses the labelled `Qualification:` bullet when the post
  has one, else `extract_qualification(description)`; it is never inferred.
  `is_active` and `expires_at` are retired from the club schema.
- **Known limitation (pre-existing).** The main loop's stop condition
  (`page_all_old or len(posts) < PAGE_SIZE`) ends the *whole* crawl instead of
  advancing to the next search term, so in practice only the first term is
  fully walked unless a term's result count happens to be an exact multiple of
  `PAGE_SIZE`. The term cursor only advances when the API returns an empty
  page. Fixing that is a crawl-breadth change and was deliberately left out of
  the taxonomy migration.

## Outputs

- `pharmarecruiter_roles_jobs.csv` — rich cumulative store, deduped on WP post id.
- `../../jobs_csv/<DD-MM-YYYY>/pharmarecruiter_roles.csv` — the shared 23-column
  club schema (`CLUB_COLUMNS`, imported from `_shared/classification.py`).
- `needs_review.csv` — flagged rows from the latest run.
- `out-of-scope.csv` — rows the one-off stored-data reclassification moved out
  of the rich store; reversible, never silently discarded.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 2   # test run (2 pages of 50 per term)
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests
```

Etiquette: descriptive User-Agent, robots.txt check at startup, ≥1s delay
between requests, exponential backoff (3s → 24s) on 429/5xx.
