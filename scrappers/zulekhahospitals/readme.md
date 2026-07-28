# zulekhahospitals.com scraper

Scrapes job openings from [zulekhahospitals.com/careers](https://www.zulekhahospitals.com/careers) —
the Zulekha Healthcare Group (multidisciplinary hospitals in Dubai and Sharjah
plus UAE medical centres and pharmacies).

## Data source

The careers page is static; recruiting happens on the group's portal
`zulekhacareers.com`, whose **Vacancies** button opens the **Adrenalin MAX
HRIS** candidate portal
(`myhrismax1.myadrenalin.com/CandidateMAX/#/?CompanyID=ZULEKHA`). The Angular
SPA loads all open positions from one public, token-free JSON endpoint —
no pagination, the whole list comes back at once:

- **Listing**: `POST /CandidateMAX/CPVacancyDetails/GetVacancyInformationWithoutToken`
  with body `{"CompanyID": "ZULEKHA", "Flag": "OP"}` (OP = open positions;
  the SPA also uses RJ/SJ for recommended/saved jobs, which need a login).
- **robots.txt**: zulekhahospitals.com allows `/careers`; the Adrenalin host
  serves no robots.txt (redirects to its error page ⇒ allowed). Both are
  checked at startup.

## Quirks

- **No per-job deep link** — vacancy selection is in-page SPA state, so every
  row's `job_url` / `application_url` is the portal URL; dedup uses the stable
  `FUNCTION_ID` as `job_id`.
- **No salary data** anywhere on the portal → `salary_raw = "Not Disclosed"`,
  numeric/club salary fields blank (never invented).
- **Titles** are often ALL-CAPS with an internal requisition suffix
  (`CARE ADVISOR_608`) — the suffix is stripped and shouty titles re-cased
  (licensing acronyms like DHA/MOH/ICU stay uppercase); `raw_title` keeps the
  original.
- **Category**: title keywords decide clinical roles; the portal's
  `FUNCTIONAL_AREA` ("Non Clinical") / `VAC_EMP_CATG_CODE` ("NC") confirm
  non-clinical ones; anything else is kept as `non_clinical` and flagged
  `needs_review` (→ `needs_review.csv`).
- **Experience** strings like `3 - 7  Year(s)` are parsed into
  `experience_min/max_years` (months floored to years).
- **Time window**: the ATS lists only *open* vacancies, so the **first run
  keeps all of them** (`INITIAL_WINDOW_DAYS = None`); later runs use the
  standard watermark (newest stored `posted_date` − 2 days grace).
- All jobs are UAE (Dubai/Sharjah); country fields are fixed to AE / +971.
  `company_name` in the club CSV is the facility (`Zulekha Hospital Dubai`),
  cleaned of legal suffixes.

## Usage

```bash
python scraper.py                # incremental daily run
python scraper.py --limit 5 --verbose
python test_filters.py           # unit tests
```

## Outputs

- `zulekhahospitals_jobs.csv` — rich cumulative store, dedup key `(source, job_id)`.
- `../../jobs_csv/<DD-MM-YYYY>/zulekhahospitals.csv` — HealthCareers.club 22-column CSV.
- `needs_review.csv` — titles the classifier couldn't place.
