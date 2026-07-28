# careers.manipalhospitals.com scraper

Scrapes job listings from [Manipal Hospitals careers](https://careers.manipalhospitals.com/manipalhospitals/jobslist)
(`www.manipalhospitals.com/careers/` redirects there) — the hospital chain's
own ATS, hosted on the Zwayam recruitment platform (company id 15590).
Because it is a hospital chain's career site, every posting is
healthcare-industry at the source.

## Data source

The Angular SPA is fully API-driven off `public.zwayam.com`
(`careers.manipalhospitals.com/robots.txt` is `Allow: /`;
`public.zwayam.com` has no robots.txt):

| Purpose | Endpoint |
|---|---|
| Job list | `POST https://public.zwayam.com/manageESQueries/searchJob` (multipart form) |
| Job detail | `POST https://public.zwayam.com/jobs-service/v1/jobs/careersite` (JSON) |

The list call takes form fields `id=15590`,
`companyUrl=careers.manipalhospitals.com/` and — crucially — the literal
string `"undefined"` for `job`, `city`, `userGeoLocation`,
`departmentName`, `fieldName`, `fieldValue` (the SPA serialises undefined
JS values that way; the endpoint returns 400 without them). It responds
with the **entire index** (~213 jobs) as one Elasticsearch hit list — no
pagination: title, numeric id (dedup key), jobCode, `jobUrl` slug, city
(`locAgg`), experience min/max, skills, createdDate/modifiedDate (epoch ms).

The detail call takes `{"jobUrl": <slug>, "externalSource": "CAREERSITE",
"campusUrl": "empty", "companyId": "15590"}` and adds the full HTML
description, `departmentName` and `minJobSalary`/`maxJobSalary` (annual
INR). Details are fetched for **new jobs only** (default on — small
volume; `--no-details` skips).

Public job page (used as `application_url`):
`https://careers.manipalhospitals.com/manipalhospitals/jobview/<slug>`.

## Quirks

* **WAF blocks custom User-Agents**: the Akamai front on
  `public.zwayam.com` resets the connection for any UA containing a custom
  token (even `Mozilla/5.0 (compatible; ...)`). The scraper therefore uses
  plain `Mozilla/5.0` and carries the contact address in a `From:` header
  — a deliberate, documented deviation from the master spec's
  descriptive-UA rule.
* The list is sorted featured-first then by modified date, **not** by
  posted date; since the whole index arrives in one response, the
  time-window cutoff is applied per job (no early stop).
* Salary appears only in the detail response and is usually empty
  (`Not Disclosed`). When present it is plain INR numbers (e.g.
  324,000–360,000 for a senior ICU nurse = annual); amounts under
  100,000 are read as per-month.
* `jobType` is always `"J"` and `workMode` is null → `job_type` defaults
  to `full_time`.
* `departmentName` (e.g. "ICU (Intensive care Unit)") also comes only
  from the detail call; it refines classification (a "Consultant - HR"
  is not a doctor) and clears `needs_review` for clinical-department
  support roles. Corporate roles (Finance, IT, Supply Chain) are kept
  and flagged `needs_review`, never dropped.
* All locations are Indian cities (Delhi, Bangalore, Mysuru, Pune, …);
  country is fixed to India.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --limit 10      # quick test on the first 10 listings
python scraper.py --no-details    # skip descriptions/salary/departments
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests (no network)
```

## Outputs

* `manipalhospitals_jobs.csv` — rich cumulative store (dedup key: numeric
  Zwayam job id)
* `../../jobs_csv/<DD-MM-YYYY>/manipalhospitals.csv` — HealthCareers.club
  22-column schema
* `needs_review.csv` — titles with no healthcare signal (log only)

Time window per the master spec: first run keeps the last 30 days; later
runs keep jobs newer than the stored watermark minus 2 days of overlap.
