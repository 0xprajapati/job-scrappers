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
- `ExternalPostedEndDate` is kept in the rich CSV as `posting_end_date`.

## Classification (shared taxonomy)

Every candidate job goes through the shared classifier
(`scrappers/_shared/classification.py`):

```python
verdict = classify_job(title, skills=site_category, description=description)
```

- `in_scope == False` → the job is **dropped** and counted as
  `excluded_out_of_scope` in the run summary. On this hospital board that is
  most requisitions (nursing, doctors, paramedical) — the scope is
  **Non Clinical + Public Health** roles only.
- In-scope jobs get `category` ("Non Clinical" | "Public Health") and
  `sub_category` (one of the 20 sub-categories), plus the rich-CSV trace
  columns `role_family`, `all_families`, `family_scores`,
  `family_confidence`, `matched_in`.
- The Oracle `RequisitionType` facet no longer decides the category; it is
  stored raw as `site_category` and passed to the classifier as its curated
  `skills` signal.
- `needs_review == True` rows are kept and also appended to
  `needs_review.csv`.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 1   # test run, first page only
python scraper.py --no-details    # skip detail fetches (no category/description)
python test_filters.py            # unit tests
```

## Outputs

- `apollohospitals_jobs.csv` — rich cumulative store (dedup key: requisition `Id`)
- `../../jobs_csv/<DD-MM-YYYY>/apollohospitals.csv` — club CSV with exactly
  the 22 `CLUB_COLUMNS` imported from `_shared/classification.py`
  (includes `sub_category` and `qualification`; the old
  `is_active`/`expires_at` columns are retired). `qualification` is the
  structured `StudyLevel` when present, else
  `extract_qualification(description)`.
- `needs_review.csv` — in-scope rows the classifier flagged for review
- `out-of-scope.csv` — rows moved out of the rich store by the one-off
  taxonomy migration (reversible archive)

First run keeps the last 30 days; later runs keep only jobs newer than the
newest stored `posted_date` minus 2 days of grace (dedup absorbs the overlap).
