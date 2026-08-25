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
- **Classification**: the shared two-level taxonomy
  (`scrappers/_shared/classification.py`) decides everything — `category` is
  `Non Clinical` | `Public Health`, plus a `sub_category` (Medical Coding,
  Clinical Research, Infection Prevention & Control, …). Oracle's own facet
  (Nursing / Clinicians / Paramedical / Enabling & Support / …) is kept in the
  rich CSV as the raw source column `category_original` and is fed to the
  classifier as its `skills` signal together with the parsed department — it
  can no longer decide the category. Medcare is a hospital operator, so most
  requisitions (bedside nursing, clinicians, paramedical) are **out of scope
  and dropped**, counted as `excluded_out_of_scope` in the run summary.
  In-scope rows whose title still looks like another profession are kept and
  flagged `needs_review` (→ `needs_review.csv`).
- **Qualification**: Oracle's structured `StudyLevel` when present, else a
  grounded extraction from the description — never inferred.
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
python test_filters.py           # 19 unit tests (title parsing, classification
                                 # wiring, cutoff, club row)
```

## Outputs

- `medcare_jobs.csv` — rich cumulative store, dedup key `(source, job_id)`.
- `../../jobs_csv/<DD-MM-YYYY>/medcare.csv` — HealthCareers.club 22-column CSV
  (`CLUB_COLUMNS` imported from `_shared/classification.py`; `is_active` /
  `expires_at` are retired, `sub_category` and `qualification` are in).
- `needs_review.csv` — in-scope rows whose title looks like another profession.
- `out-of-scope.csv` — rows the shared classifier rejected during the one-off
  stored-data reclassification (reversible; never silently discarded).
