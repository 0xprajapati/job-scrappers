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

- Every row is healthcare-sector by construction (organization filter at the
  source), so nothing is dropped; titles are classified into the club enum
  and pure-default `non_clinical` mappings are flagged `needs_review`.
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

Outputs: `dubaihealth_jobs.csv` (rich cumulative store) and
`../../jobs_csv/<DD-MM-YYYY>/dubaihealth.csv` (HealthCareers.club schema).
