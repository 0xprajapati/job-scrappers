# Apollo Hospitals careers scraper

Scrapes open requisitions from the Apollo Hospitals group careers portal
(<https://www.apollohospitals.com/careers/>).

## Data source

The marketing page at `apollohospitals.com/careers/` is a static shell whose
host rejects non-browser clients (Azure Application Gateway 403). Its
**Job Search** button points at Oracle Recruiting Cloud, Candidate Experience
site `CX_2`, on `cgs.fa.ap2.oraclecloud.com` — and that host exposes the
standard Oracle public REST API with no bot-blocking and no robots.txt
(404 = no crawl rules):

- **Listing** — `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions`
  with `finder=findReqs;siteNumber=CX_2,limit=…,offset=…,sortBy=POSTING_DATES_DESC`.
  Returns `Id`, `Title`, `PostedDate`, `PrimaryLocation`, newest-first.
  The whole index is small (~90 open requisitions).
- **Detail** (fetched for new jobs only, `--no-details` to skip) —
  `GET …/recruitingCEJobRequisitionDetails` with
  `finder=ById;siteNumber=CX_2,Id="<Id>"`. Adds `RequisitionType`
  (Nursing / Medical / Paramedical / Administration …), `JobSchedule`,
  `StudyLevel`, the HTML description, and `workLocation` with the concrete
  hospital unit ("Apollo Hospitals, Bannerghatta Road, Bangalore"), which is
  used as `company_name`.

The per-city `apollo.taleo.net` career sections also linked from the marketing
page are the legacy platform and are not crawled.

## Quirks

- **No salaries anywhere** — every listing gets `salary_raw = "Not Disclosed"`
  (master spec: capture, never filter).
- Descriptions are populated for some requisitions (mostly Nursing) and
  genuinely empty for many others.
- All postings are Apollo group hospital jobs, so everything is healthcare-
  industry by construction; non-clinical hospital roles (marketing,
  engineering, call center) are kept with `category = non_clinical` and are
  **not** flagged for review. Only junk/empty titles land in
  `needs_review.csv`.
- `ExternalPostedEndDate` is exported as `expires_at`.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 1   # test run, first page only
python scraper.py --no-details    # skip detail fetches (no category/description)
python test_filters.py            # unit tests
```

## Outputs

- `apollohospitals_jobs.csv` — rich cumulative store (dedup key: requisition `Id`)
- `../../jobs_csv/<DD-MM-YYYY>/apollohospitals.csv` — HealthCareers.club
  22-column import schema
- `needs_review.csv` — junk/empty titles flagged during the run

First run keeps the last 30 days; later runs keep only jobs newer than the
newest stored `posted_date` minus 2 days of grace (dedup absorbs the overlap).
