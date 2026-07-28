# medcare.ae scraper

Scrapes job openings from [medcare.ae/en/careers.html](https://www.medcare.ae/en/careers.html) —
Medcare Hospitals & Medical Centres, the premium UAE healthcare division of
Aster DM Healthcare (hospitals & medical centres in Dubai and Sharjah).

## Data source

The careers page itself is static; its **Current Openings** button links to the
Aster DM group's **Oracle Recruiting Cloud** candidate portal
(`hcdt.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX`), which
exposes the standard public Oracle CE REST API:

- **Listing**: `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions`
  with `finder=findReqs;siteNumber=CX,sortBy=POSTING_DATES_DESC,limit,offset,`
  `selectedOrganizationsFacet=300003648617854`. The org facet isolates
  **"Medcare Medical Hospitals and Medical Centres"** (~21 open requisitions)
  from the ~2,900 group-wide jobs (other Aster brands in India/Oman/Qatar).
- **Details** (per NEW job, on by default; `--no-details` skips):
  `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails` with
  `finder=ById;Id="<id>",siteNumber=CX` — Oracle category facet, job schedule,
  full HTML description, and the org blurb used as `company_about`.
- **robots.txt**: medcare.ae allows the careers page; the Oracle host serves no
  robots.txt (404 ⇒ allowed). Both are checked at startup.

## Quirks

- **No salary data** anywhere on the portal → `salary_raw = "Not Disclosed"`,
  numeric/club salary fields blank (never invented).
- **Requisition titles** come in several shapes — `Role.Dept.Facility`
  (`Registered Nurse.Endoscopy.Medcare Hospital Sharjah (Br)`),
  `Role for Facility`, or plain (`Medical Coder`) — parsed into
  `title` / `department` / `facility` columns; legal suffixes (`(Br)`,
  `(Br of Aster DM…)`) are stripped and run-together names re-spaced.
  The club CSV's `company_name` is the facility when one is named, else the
  Medcare group name.
- **Category**: Oracle's facet (Nursing / Clinicians / Paramedical / Enabling
  & Support / …) maps onto the club enum; titles decide when the facet is
  missing or ambiguous; unmatched titles are kept as `non_clinical` and
  flagged `needs_review` (→ `needs_review.csv`).
- **Time window**: the ATS lists only *open* requisitions and Medcare keeps
  postings live for years, so unlike feed-style sources the **first run keeps
  all open jobs** (`INITIAL_WINDOW_DAYS = None`); later runs use the standard
  watermark (newest stored `posted_date` − 2 days grace).
- All jobs are UAE (Dubai/Sharjah); country fields are fixed to AE / +971.

## Usage

```bash
python scraper.py                # incremental daily run
python scraper.py --no-details   # listing data only (no per-job requests)
python scraper.py --max-pages 1 --verbose
python test_filters.py           # unit tests
```

## Outputs

- `medcare_jobs.csv` — rich cumulative store, dedup key `(source, job_id)`.
- `../../jobs_csv/<DD-MM-YYYY>/medcare.csv` — HealthCareers.club 22-column CSV.
- `needs_review.csv` — titles the classifier couldn't place.
