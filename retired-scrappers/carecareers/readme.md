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
* Titles carry typos ("Counsultant"); the classifier therefore also gets the
  designation and the department segment of the org hierarchy as its
  `skills` signal.
* `employmentType` "Regular" → `full_time`; part-time/contract variants are
  mapped if they ever appear.

## Classification (shared taxonomy)

Every candidate job goes through the shared classifier
(`scrappers/_shared/classification.py`) — the scraper defines no category
rules of its own:

```python
verdict = classify_job(title, skills=designation + department, description=description)
```

* `in_scope == False` → the job is **dropped** and counted as
  `excluded_out_of_scope` in the run summary. On this hospital-chain ATS that
  is most postings (consultants, nursing, technicians): the scope is
  **Non Clinical + Public Health** roles only.
* In-scope jobs get `category` (`Non Clinical` | `Public Health`) and
  `sub_category` (one of the 20 sub-categories), plus the rich-CSV trace
  columns `role_family`, `all_families`, `family_scores`,
  `family_confidence`, `matched_in`.
* The PeopleStrong department/designation fields never decide the category;
  they stay raw in the rich CSV and travel only as the classifier's `skills`
  signal.
* `needs_review == True` rows are kept **and** appended to `needs_review.csv`.

## Outputs

* `carecareers_jobs.csv` — rich cumulative store, deduped on `requisitionId`;
  append-only across runs.
* `../../jobs_csv/<DD-MM-YYYY>/carecareers.csv` — the 22 `CLUB_COLUMNS`
  imported from `_shared/classification.py` (all cumulative rows, refreshed
  every run). It carries `sub_category` and `qualification`; the old
  `is_active`/`expires_at` columns are retired. `qualification` is the
  structured `qualifications` detail field when present, else
  `extract_qualification(description)` — never inferred.
* `needs_review.csv` — in-scope rows the classifier flagged, for manual review.
* `out-of-scope.csv` — rows the classifier dropped from the rich store during
  the one-off taxonomy migration (reversible archive).

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
