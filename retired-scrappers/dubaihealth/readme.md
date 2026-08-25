# Dubai Health scraper

Scrapes job openings of **Dubai Health** (https://www.dubaihealth.ae/ — the
Dubai government academic health system: Rashid, Latifa, Dubai, Hatta and
Al Jalila Children's hospitals plus health centers).

## Data source

dubaihealth.ae hosts no listings itself. Its `/careers` page redirects to
**Dubai Careers**, the Dubai Government recruitment portal, where Dubai Health
is employer/organization `4105100456`:

- Portal: Oracle **Taleo** career section at `jobs.dubaicareers.ae`
  (career section `dubaicareers`, portal code `40505100456`).
- Listing: `POST /careersection/rest/jobboard/searchjobs?lang=en&portal=40505100456`
  with an advanced-search filter `ORGANIZATION=4105100456`, sorted by posting
  date descending. Requires a session cookie — the scraper GETs
  `jobsearch.ftl` once to bootstrap (a cookie-less POST returns HTTP 500) and
  sends `tz`/`tzname` headers.
- Detail (on by default, `--no-details` to skip): each job's
  `jobdetail.ftl?job=<jobNumber>` page embeds a serialized Taleo state blob
  (`!|!` / `!*!` separated, percent-encoded HTML) carrying the description +
  qualifications, department, education, contract type, job level, required
  nationality, monthly salary and schedule, plus the unposting date
  (`expires_at`).
- robots.txt: none on `jobs.dubaicareers.ae` (HTTP 404) — nothing disallowed;
  the scraper still checks at startup.

## Quirks

- Classification is the shared two-level taxonomy
  (`scrappers/_shared/classification.py`): `category` is
  `Non Clinical` | `Public Health`, plus a `sub_category`. The organization
  filter at the source is a crawl-side saver only — it makes every row
  healthcare-sector, but a Dubai Health board is mostly bedside/clinical, so
  most requisitions come back out of scope and are dropped (counted as
  `excluded_out_of_scope` in the run summary). Jobs are classified **after**
  the detail fetch so the description and department can be scored; the Taleo
  `department` is passed as the classifier's `skills` signal and stays in the
  rich CSV as a raw source column — it never decides the category itself.
  `needs_review` now comes from the shared classifier (in scope but the title
  reads like another profession).
- Salaries are **AED**; the club schema allows only INR/USD, so club CSV
  salary fields stay blank and AED amounts live in the rich CSV. Most
  postings show "Unspecified" → `salary_raw = "Not Disclosed"`.
- Dubai Health posts a handful of openings at a time (single-digit counts),
  many restricted to UAE nationals (`nationality_requirement`).
- The dedup key `job_id` is the Taleo job number (e.g. `26000730`), which is
  also the `job=` parameter of the public detail URL.

## Usage

```bash
python scraper.py                 # incremental daily run (details on)
python scraper.py --no-details    # listing fields only
python scraper.py --max-pages 1   # test run
python test_filters.py            # unit tests (offline)
```

## Outputs

* `dubaihealth_jobs.csv` — rich cumulative store of the **in-scope** jobs.
* `out-of-scope.csv` — requisitions `classify_job` rejected, archived verbatim
  (same columns) rather than discarded, so the decision stays reversible.
* `needs_review.csv` — rows a human should confirm.
* `../../jobs_csv/<DD-MM-YYYY>/dubaihealth.csv` — HealthCareers.club 22-column
  schema.

## Taxonomy migration (25-08-2026)

The 3 stored requisitions were re-run through the shared classifier:
**0 kept, 3 moved** to `out-of-scope.csv` (Clinical Dietitian, Flex Campus
Coordinators Lead, Senior Analyst – Media Relations). A small or 0-row steady
state is expected here — Dubai Health posts a handful of mostly bedside and
generic-corporate openings at a time.
