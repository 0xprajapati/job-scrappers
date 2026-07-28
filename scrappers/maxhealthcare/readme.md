# maxhealthcarecareers.peoplestrong.com scraper

Scrapes job listings for [Max Healthcare](https://www.maxhealthcare.in/careers)
— `www.maxhealthcare.in/careers` redirects to
`maxhealthcarecareers.peoplestrong.com`, the group's own candidate portal on
PeopleStrong (Alt Recruit), covering Max Healthcare Institute Ltd and group
entities (Alps Hospital, Starlit Medical Centre, Nanavati Max, BLK-Max,
Max Lab, Max@Home, ...). Because it is a hospital chain's career site, every
posting is healthcare-industry at the source.

Same platform as the `carecareers` scraper; the code is adapted from it.

## Data source

The Angular SPA is fully API-driven (`/robots.txt` returns the SPA shell,
so nothing is disallowed; the corporate `www.maxhealthcare.in` robots.txt
does not apply to this host — and its Akamai edge blocks plain HTTP clients
anyway, which is why the portal API is the right source):

| Purpose | Endpoint |
|---|---|
| Job list | `POST /api/cp/rest/altone/cp/jobs/v1?offset=N&limit=45`, body `{}` |
| Job detail | `GET /api/cp/rest/altone/cp/job/<ID>/v2?part=basic,organisational,descriprion,skill,qualification,language&isReqId=false` |

The list returns `{totalRecords, response: [...]}` with title, jobCode,
requisitionId (dedup key), posted/closure dates, org hierarchy
(`MHC>Entity>Department>Sub-dept`), location hierarchy
(`India>STATE>City>...>Site`), experience range, openings and `CTCRange`.

`<ID>` is the jobCode with `/` → `_` (e.g. `MHC_28734`), same as the public
detail URL `/job/detail/<ID>`. The detail call adds the full HTML
description, qualifications, `minSalary`/`maxSalary` and `employmentType`;
it is fetched for **new jobs only** (default on — the index is ~66 jobs;
`--no-details` skips).

## Quirks

* **Mixed salary units**: `CTCRange` `"200000.0000-400000.0000"` is absolute
  annual INR, but `"13.0000-15.0000"` means 13–15 **lakhs** per annum (the
  detail API mirrors this: `minSalary "13"`). Values below 1,000 are treated
  as lakhs (× 100,000). `minBudgetSalary`/`maxBudgetSalary` are 0 in
  practice. 0/absent → `Not Disclosed` (never invented).
* States come ALL-CAPS (`UTTAR PRADESH`) and are title-cased; the location
  hierarchy is 8 levels deep — only country/state/city are used.
* Entity names carry legal boilerplate ("Alps Hospital Limited (Formerly
  known as ...)"); the parenthetical is stripped for `company_name`.
* Consultant-style titles map to doctors only in clinical departments;
  generic corporate titles with no healthcare word in
  title/designation/department are kept and flagged `needs_review` (also
  logged to `needs_review.csv`) — nothing is dropped.
* `employmentType` `"Employee"` → `full_time`; part-time/contract variants
  are mapped if they ever appear.
* The list is roughly newest-first but tiny, so every run scans all pages
  and applies the time-window cutoff per job (`MAX_PAGES_SAFETY` guards).

## Outputs

* `maxhealthcare_jobs.csv` — rich cumulative store, deduped on
  `requisitionId`; append-only across runs (watermark = newest stored
  `posted_date` − 2 days grace; first run keeps the last 30 days).
* `../../jobs_csv/<DD-MM-YYYY>/maxhealthcare.csv` — HealthCareers.club
  22-column import schema, regenerated from the full store each run.
* `needs_review.csv` — titles with no healthcare signal, for manual review.

## Usage

```bash
python scraper.py                 # normal daily run
python scraper.py --max-pages 1   # quick test (45 jobs)
python scraper.py --no-details    # skip description fetches
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests (no network)
```
