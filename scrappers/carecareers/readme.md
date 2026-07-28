# carecareers.peoplestrong.com scraper

Scrapes job listings from the [Quality Care India](https://carecareers.peoplestrong.com/job/joblist)
candidate portal — the CARE Hospitals / KIMS Health / CIIGMA hospital group's
own ATS, hosted on PeopleStrong (Alt Recruit). Because it is a hospital
chain's career site, every posting is healthcare-industry at the source.

## Data source

The Angular SPA is fully API-driven (no robots.txt exists — the server
returns the SPA shell for `/robots.txt`, so nothing is disallowed):

| Purpose | Endpoint |
|---|---|
| Job list | `POST /api/cp/rest/altone/cp/jobs/v1?offset=N&limit=45`, body `{}` |
| Job detail | `GET /api/cp/rest/altone/cp/job/<ID>/v2?part=basic,organisational,descriprion,skill,qualification,language&isReqId=false` |

The list returns `{totalRecords, response: [...]}` with title, jobCode,
requisitionId (dedup key), posted/closure dates, org hierarchy
(`Group>Entity>Department>Sub-dept`), location hierarchy
(`Country>State>City>Site>...`), experience range, openings, skills and
**annual INR budget salary** (`minBudgetSalary`/`maxBudgetSalary`).

`<ID>` is the jobCode with `/` → `_` (e.g. `QCI_P_1806731`), same as the
public detail URL `/job/detail/<ID>`. The detail call adds the full HTML
description, qualifications, languages and `employmentType`; it is fetched
for **new jobs only** (default on — the index is tiny; `--no-details` skips).

## Quirks

* The list is **not sorted by date**, but the whole index is only a few
  dozen jobs, so every run scans all pages and applies the time-window
  cutoff per job (no newest-first early stop; `MAX_PAGES_SAFETY` guards).
* Salary comes as annual INR numbers; `CTCRange` is null in practice.
  0/null budget values → `Not Disclosed` (never invented). `misSalary`/
  `maxSalary` in the detail payload are 0 even when budget values exist —
  only the budget fields are used.
* Titles carry typos ("Counsultant"); classification also uses the
  designation and the department segment of the org hierarchy. Consultant-
  style titles map to doctors (clinical depts); generic corporate titles
  with no healthcare word in title/designation/department are kept and
  flagged `needs_review` (also logged to `needs_review.csv`).
* `employmentType` "Regular" → `full_time`; part-time/contract variants are
  mapped if they ever appear.

## Outputs

* `carecareers_jobs.csv` — rich cumulative store, deduped on `requisitionId`;
  append-only across runs.
* `../../jobs_csv/<DD-MM-YYYY>/carecareers.csv` — HealthCareers.club
  22-column schema (all cumulative rows), refreshed every run.
* `needs_review.csv` — titles with no healthcare signal, for manual review.

## Usage

```bash
python scraper.py                 # incremental daily run (details on)
python scraper.py --max-pages 1   # sample run
python scraper.py --no-details    # skip description enrichment
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests (no network)
```

Time window per the master spec: first run keeps the last 30 days; later
runs keep jobs newer than the newest stored `posted_date` minus 2 grace
days (dedup absorbs the overlap). Running twice in a row adds 0 rows.
