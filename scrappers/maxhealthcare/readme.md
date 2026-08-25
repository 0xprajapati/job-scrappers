# maxhealthcarecareers.peoplestrong.com scraper

Scrapes job listings for [Max Healthcare](https://www.maxhealthcare.in/careers)
— `www.maxhealthcare.in/careers` redirects to
`maxhealthcarecareers.peoplestrong.com`, the group's own candidate portal on
PeopleStrong (Alt Recruit), covering Max Healthcare Institute Ltd and group
entities (Alps Hospital, Starlit Medical Centre, Nanavati Max, BLK-Max,
Max Lab, Max@Home, ...). Because it is a hospital chain's career site, every
posting is healthcare-industry at the source — but most are bedside/clinical
roles, so the shared classifier drops them (see **Classification** below).

Same platform as the `carecareers` scraper; the code is adapted from it.

## Classification

Every candidate job goes through the shared two-level taxonomy in
`scrappers/_shared/classification.py` — the scraper defines **no** category
regexes of its own:

* `category` is `Non Clinical` or `Public Health`, `sub_category` one of the
  20 sub-categories; `role_family` plus the score trace (`all_families`,
  `family_scores`, `family_confidence`, `matched_in`) land in the rich CSV.
* Signals: the job title, the description (HTML stripped), and — as the
  curated `skills` signal — the PeopleStrong **designation**, the org-unit
  **department** and the requisition's **skill tags** joined together.
  Those raw fields stay in the rich CSV as source columns; they never decide
  the category themselves.
* Out-of-scope jobs are **dropped**, not exported, and counted as
  `Excluded (out of scope)` in the run summary. Being a hospital chain's
  ATS, most requisitions (nursing, clinicians, paramedical, hospital admin)
  fall out this way.
* `needs_review = True` rows are kept and logged to `needs_review.csv`.
* Jobs are classified **after** the detail fetch so the description counts
  towards the score.

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
* `employmentType` `"Employee"` → `full_time`; part-time/contract variants
  are mapped if they ever appear.
* The list is roughly newest-first but tiny, so every run scans all pages
  and applies the time-window cutoff per job (`MAX_PAGES_SAFETY` guards).

## Outputs

* `maxhealthcare_jobs.csv` — rich cumulative store, deduped on
  `requisitionId`; append-only across runs (watermark = newest stored
  `posted_date` − 2 days grace; first run keeps the last 30 days).
* `../../jobs_csv/<DD-MM-YYYY>/maxhealthcare.csv` — HealthCareers.club
  22-column import schema (`CLUB_COLUMNS` imported from
  `_shared/classification.py`; `is_active`/`expires_at` are retired),
  regenerated from the full store each run.
* `needs_review.csv` — in-scope rows the classifier flagged, for manual
  review.
* `out-of-scope.csv` — rows the classifier dropped during the one-off
  stored-data reclassification (reversible; nothing is silently discarded).

## Usage

```bash
python scraper.py                 # normal daily run
python scraper.py --max-pages 1   # quick test (45 jobs)
python scraper.py --no-details    # skip description fetches
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests (no network)
```
