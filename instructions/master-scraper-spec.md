# Master spec: healthcare job scrapers

This is the single source of truth for every job-site scraper in this project.
To add a new site, the only input needed is the website URL — everything else
follows this spec. Scrapers live under `scrappers/<site>/`.

## Goal

Given a job-site URL, deliver a self-contained scraper the user can run
repeatedly (manually or on a daily schedule) without further prompting. Each
run appends the latest in-scope jobs to a deduplicated CSV and refreshes the
HealthCareers.club import files.

## 1. Data-source discovery (first step for any new site)

Investigate before writing code, preferring the most robust source available:

1. **Underlying JSON API** (check network requests / Next.js `__NEXT_DATA__` /
   JS bundles for endpoints) — best.
2. **Embedded structured data** in server-rendered HTML: `__NEXT_DATA__`
   page props, RSC payload JSON, or schema.org JobPosting JSON-LD.
3. **Sitemap + detail pages** when listing APIs are off-limits.
4. **HTML card parsing** only if nothing structured exists.
5. **Playwright/headless** only as a last resort for JS-gated sites.

**robots.txt is binding**: if it disallows an API path, do not call it — use
what the site exposes to crawlers instead. Confirm the listing schema on ONE
page before building.

## 2. Classification — `_shared/classification.py` is mandatory

Mandatory for every **new** scraper and for every scraper that has been
migrated. Every candidate job goes through the shared classifier; it alone
makes the final keep/drop and labeling decision:

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

verdict = classify_job(title, skills, description)
if not verdict["in_scope"]:
    counters["excluded_out_of_scope"] += 1
    continue          # dropped — never exported
category      = verdict["category"]        # "Non Clinical" | "Public Health"
sub_category  = verdict["sub_category"]    # one of the 20 sub-categories
role_family   = verdict["role_family"]     # rich-CSV trace only
needs_review  = verdict["needs_review"]    # keep AND flag
```

Rules:

- **No scraper defines its own category regexes, enums, ALLOW/DENY
  classification lists, or category fallback maps.** Crawl-side scoping that
  merely saves requests (URL slug filters, source-side category facets,
  junk-title detection) may stay, but the final decision is `classify_job`'s
  alone.
- PREFER filtering at the source too (a category facet or healthcare listing
  URL) — source filters are recall, the classifier is precision.
- Pass `skills` and `description` whenever the source provides them
  (multi-field scoring is the point); strip HTML from the description first.
  A curated role/function/department field goes in as `skills`.
- `in_scope == False` → drop the job, count it as `excluded_out_of_scope`,
  and print the counter in the run summary.
- `needs_review == True` → keep the row AND append it to `needs_review.csv` —
  never silently drop it.
- ATS/site categories (Oracle facets, department fields, …) may not decide
  the category; they stay in the rich CSV as raw source columns only.

**Migration status.** A set of existing scrapers is excluded from the taxonomy
migration for now and still runs its own per-scraper classifier on the legacy
profession enum (`doctors | nurses | pharmacists | non_clinical`) with the
older club schema. The authoritative per-scraper list lives in
`instructions/taxonomy-migration-status.md` — do not copy it into other docs.
This section still binds anything new.

## 3. Salary: capture, don't filter

**There is NO salary threshold.** Never exclude a job for its salary or for
not disclosing one.

- When salary is shown, store it: `salary_raw` verbatim, plus normalized
  integer `salary_min_monthly` / `salary_max_monthly` (INR per month;
  K = 1,000, L/LPA = 100,000, P.A ÷ 12) and `salary_period_original`.
- When absent/undisclosed: `salary_raw = "Not Disclosed"`, numeric fields
  empty. Never invent values.
- Where a site splits fixed pay vs. incentives, the normalized fields hold
  the FIXED range; the raw string keeps the displayed one.
- Club columns carry salary only for INR/USD; other currencies stay in the
  rich CSV with the club salary columns blank. Nothing is currency-converted.

## 4. Time window & daily incremental scraping

- **First run** (no existing CSV): keep only jobs posted within the last
  `INITIAL_WINDOW_DAYS = 7` days.
- **Every later run**: keep only jobs newer than the **watermark** = the
  newest `posted_date` already in the CSV, minus `WATERMARK_GRACE_DAYS = 2`
  days of overlap (protects against late-appearing posts; dedup absorbs the
  overlap). Jobs at or after the cutoff are kept, older ones are counted as
  `excluded_old`.
- When the source lists jobs newest-first, stop paginating once a page is
  entirely older than the cutoff (don't crawl the whole site daily).
- Intended cadence: one run per day (see §8).

## 5. Idempotency / dedup

- Dedup key: `(source, job_id)` where `job_id` comes from the job URL / API id.
- Read the existing CSV at startup; append only unseen jobs; never modify or
  duplicate existing rows. Running twice in a row adds 0 rows.
- Print a run summary: scanned, excluded_out_of_scope, excluded_old,
  needs-review, new added, duplicates skipped.

## 6. Output

Each scraper maintains a rich per-source CSV (`<site>_jobs.csv`) with at least:

```
source, job_id, title, company, location, salary_raw, salary_min_monthly,
salary_max_monthly, salary_period_original, job_type, category, sub_category,
role_family, all_families, family_scores, family_confidence, matched_in,
needs_review, posted_date (YYYY-MM-DD), job_url, scraped_at
```

plus whatever the site exposes (experience, work_mode, education, description,
raw source category, …).

Each run the scraper also regenerates its club export from the full rich
store: `jobs_csv/<DD-MM-YYYY>/<site>.csv` with **exactly the 22
`CLUB_COLUMNS` imported from `_shared/classification.py`** (never hand-copy
the list):

```
country_name, country_code, country_dial_code, city_name, company_name,
company_type, company_logo, company_about, title, description, job_type,
category, sub_category, application_url, posted_at, min_experience,
max_experience, qualification, min_salary, max_salary, salary_period,
salary_currency
```

- `category` = `Non Clinical` | `Public Health`; `sub_category` = one of the
  20 sub-categories (see `Jobs_keywords/keywords_for_jobs.md`).
- The old `is_active` / `expires_at` columns are retired; rich CSVs may keep
  source expiry data in their own columns.
- `qualification`: the source's structured qualification field when present,
  else `extract_qualification(description)`; never inferred.
- Dates are never invented; salary rules per §3.

## 7. Robustness & etiquette

- Check robots.txt at startup and honor it per-URL; abort if the crawl target
  is disallowed.
- Descriptive User-Agent with contact address.
- ≥1s delay between requests; retries with exponential backoff (3s → 24s) on
  429/5xx/network errors.
- Fail gracefully per-field and per-page: log and continue; one bad card or
  page must never crash the run. Tolerate transient empty pages (stop only
  after 3 consecutive).

## 8. Structure, deliverables & scheduling

For each site, a folder `scrappers/<site>/` containing:

- `<site>_scraper.py` — config constants at top, flow:
  config → discover/paginate → parse → classify (shared module) → time window →
  dedup/append → write rich CSV + club CSV. CLI flags: `--output`,
  `--max-pages`/`--limit` (test runs), `--enrich` (detail-page extras, off by
  default), `--verbose`.
- `test_filters.py` — unit tests for the salary parser (with worked examples
  from real listings), date/cutoff logic, and at least two classification
  wiring tests (an in-scope role gets the right `category`/`sub_category`; an
  out-of-scope title is dropped). The shared engine itself is covered by
  `_shared/test_classification.py` — don't re-test its internals.
- `README.md` — data source, quirks, usage.
- `requirements.txt` (`requests`, `pandas`; shared venv at repo `.venv`).

Build order for a new site: discover source → unit-test parsers → wire
pagination/classifier/dedup → 2–3 page sample run → full first run.

Daily scheduling: there is **no fleet runner script yet** (a `run_daily.sh`
is aspirational — each scraper is invoked by hand today). A cron line per
scraper works, e.g.:

```cron
0 8 * * * cd <repo>/scrappers/<site> && ../../.venv/bin/python <site>_scraper.py >> run.log 2>&1
```
