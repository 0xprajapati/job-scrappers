# Taxonomy migration spec (2026-08-25)

> **EXCLUSION — pharmabharat is out of scope for this migration.**
> By the user's instruction (2026-08-25), `scrappers/pharmabharat/` is not to be
> touched: not `scraper.py`, not `daily_scraper.py`, not its tests, README or
> CSVs. It keeps its existing classification and output schema. Do not
> reclassify its stored data. Leave it out of migration sweeps and do not count
> it as outstanding work.

The old profession enum (`doctors | nurses | pharmacists | non_clinical`) and the
old-generation pattern (rich CSV only, hand-maintained ALLOW/DENY profession
keyword lists) are **retired fleet-wide**. Every scraper now classifies through
one shared module and exports one club schema. `jobslly` is deleted.

## The one classifier

Every scraper imports the shared module and classifies every candidate job:

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
needs_review  = verdict["needs_review"]    # keep AND flag (out-of-scope-looking title)
```

Rules:
- **No scraper defines its own category regexes, profession enums, ALLOW/DENY
  *classification* lists, or category fallback maps.** Delete them. (Crawl-side
  scoping that merely saves requests — e.g. URL slug filters, source-side
  category facets, junk/test-title detection — is allowed to stay, but the final
  keep/drop and labeling decision is `classify_job`'s alone.)
- Signals: `title` is required; pass `skills` and `description` whenever the
  source provides them (multi-field scoring is the point). Strip HTML from the
  description first. For sources with a curated role/function/department field,
  pass it as `skills`.
- `in_scope == False` → drop the job and count it as `excluded_out_of_scope`
  (print the counter in the run summary).
- `needs_review == True` → keep the row AND append it to `needs_review.csv`.
- ATS/site categories (Oracle facets, department fields, etc.) may no longer
  decide the category. They stay in the rich CSV as raw source columns only.

## Rich CSV changes

- The `category` column now holds `"Non Clinical" | "Public Health"`.
- Add columns (after `category`, order flexible but keep per-scraper order
  stable): `sub_category`, `role_family`, `all_families`, `family_scores`,
  `family_confidence`, `matched_in`, `needs_review`.
  (Scrapers that already have family trace columns keep them; the family value
  moves from `category` into `role_family`.)
- Everything else in each scraper's rich schema stays as-is.

## Club CSV — one contract for everyone

Every scraper (including `apna` and `docthub`, which previously had no club
export) writes `../../jobs_csv/<DD-MM-YYYY>/<site>.csv`, regenerated from the
full rich store each run, with exactly the 22 `CLUB_COLUMNS` from
`_shared/classification.py`:

```
country_name, country_code, country_dial_code, city_name, company_name,
company_type, company_logo, company_about, title, description, job_type,
category, sub_category, application_url, posted_at, min_experience,
max_experience, qualification, min_salary, max_salary, salary_period,
salary_currency
```

- `is_active` and `expires_at` are retired everywhere (drop from club rows;
  rich CSVs may keep source expiry data in their own columns).
- `qualification`: the source's structured qualification field when it has one,
  else `extract_qualification(description)`; never inferred.
- Import `CLUB_COLUMNS` from `_shared/classification.py` — do not hand-copy the
  list into the scraper.
- All other club-row conventions are unchanged (salary only for INR/USD, dates
  never invented, `Not Disclosed`, etc.).

## Stored-data reclassification (one-off, per scraper)

The rich store `<site>_jobs.csv` must not keep old enum values:

1. Load the CSV with pandas (`dtype=str, keep_default_na=False`).
2. For each row, call `classify_job` using the row's title + whatever
   skills/description columns that scraper stores (missing → "").
3. Rows with `in_scope == False` move to `<site>/out-of-scope.csv`
   (same columns; append + dedupe on `job_id` if the file exists) — reversible,
   never silently discarded.
4. In-scope rows get the new `category`, `sub_category`, `role_family`,
   trace and `needs_review` values; rewrite the rich CSV with the new column
   set.
5. Do NOT write a new dated club CSV during migration; the next real run
   regenerates it.
6. Run the reclassification as a throwaway script (do not commit it), and
   report kept/moved counts.

## Tests

Each scraper's `test_*.py` must be updated: delete tests of the removed
per-scraper classifiers; keep/adapt tests of parsing, salary, dates, robots,
etc.; add at least two wiring tests (an in-scope role classifies with the right
`category`/`sub_category`; a vetoed/out-of-scope title is dropped). The shared
engine itself is covered by `_shared/test_classification.py` — don't re-test
its internals per scraper.

## Sanity checks per scraper (agents: run these)

- `python3 -m py_compile <scraper>.py`
- run the scraper's test file
- `grep -n "doctors\|nurses\|pharmacists\|non_clinical" <scraper dir>` → must
  only match, at most, raw *source* data fields or comments explaining source
  quirks — no classification logic, no enum values in output columns.
