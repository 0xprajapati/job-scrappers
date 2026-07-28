# PureHealth (purehealth.ae) scraper

Scrapes open positions for **PureHealth Group**, the UAE's largest integrated
healthcare platform (SEHA, Sheikh Shakhbout Medical City, Daman, …).

## Data source

`purehealth.ae` itself is a WordPress corporate/investor site — it has **no
careers section and no job pages in its sitemap**. Every careers link on it
points to the group's talent platform `talentone.ae`, whose "Explore
opportunities" buttons open PureHealth's **Oracle Fusion Recruiting (ORC)
Candidate Experience** site:

```
https://fa-eutv-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_6007
```

That SPA is backed by Oracle's public, token-free REST API, which is what this
scraper uses (master spec §1.1 — an underlying JSON API is the best source):

| Call | Endpoint |
|---|---|
| List | `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.workLocation,requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX_6007,limit=25,offset=0,sortBy=POSTING_DATES_DESC` |
| Detail | `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=<Id>,siteNumber=CX_6007` |

The list response wraps everything in `items[0]`: `TotalJobsCount` plus
`requisitionList[]` (`Id`, `Title`, `PostedDate`, `PrimaryLocation`,
`workLocation[]`, teaser `ShortDescriptionStr`). Pagination is `limit`/`offset`
inside the finder string. The detail call adds `Category`, `JobSchedule`,
`JobShift`, `StudyLevel`, `ExternalPostedEndDate` and the full HTML
`ExternalDescriptionStr`.

`CX_6007` is the only ORC site referenced anywhere on purehealth.ae or
talentone.ae, and it currently carries **14 open requisitions**, all in Abu
Dhabi / UAE.

## Quirks

* **`Category` from the ATS is unreliable.** "Cath Lab Staff Nurse" and
  "Assistant Nurse" are both filed under *Administration*. The **title decides**
  the club category; `Category` is only the fallback (master spec §2). A title
  the patterns miss and a `Category` the map misses are kept as `non_clinical`,
  flagged `needs_review` and logged to `needs_review.csv` — never dropped.
  `Psychologist` / `Technologist` / `Audiologist` are deliberately excluded from
  the `-ologist` doctor pattern (allied health, not physicians).
* **No salary anywhere** — no pay fields, no flexfields, no skills on any
  requisition. `salary_raw = "Not Disclosed"`, all numeric salary columns empty
  (master spec §3). Nothing is invented.
* **No structured experience field.** `min/max_experience` are parsed
  conservatively out of the description text ("Minimum 2–3 years…", "two (2)
  years", "10-12 years of experience") and left **empty** when the requirement
  is qualitative ("Clinical experience preferably in…"). Durations without an
  experience/minimum context are ignored.
* **Details are fetched by default**, unlike the master-spec `--enrich`
  convention: the list endpoint only carries a teaser, and the site posts a
  couple of dozen requisitions at most, so the full-description call is cheap.
  Use `--no-details` for a listing-only run.
* **`company_name` is always "PureHealth"** in the club CSV. The ATS work
  location is kept verbatim in the rich CSV's `facility` column, because some
  values are real employers ("SEHA", "Sheikh Shakhbout Medical City (SSMC)")
  while others are addresses ("Al Dar, Abu Dhabi").
* **Time window:** an ATS only lists currently *open* requisitions (one here has
  been open since 2025-11), so the first run keeps **all** of them
  (`INITIAL_WINDOW_DAYS = None`). Later runs use the master-spec watermark —
  newest stored `posted_date` minus `WATERMARK_GRACE_DAYS = 2`.
* **Cloudflare:** `purehealth.ae/robots.txt` and `talentone.ae/robots.txt` are
  blocked by Cloudflare for non-browser clients (their sitemaps are not). This
  scraper never crawls either host — the Oracle ATS host is the only target, and
  it serves no robots.txt (404 ⇒ everything allowed), re-checked at startup.

## Usage

```bash
pip install -r requirements.txt
python scraper.py
```

Flags:

| Flag | Meaning |
|---|---|
| `--output PATH` | rich cumulative CSV (default `purehealth_jobs.csv`) |
| `--max-pages N` | stop after N listing pages of 25 (test runs) |
| `--limit N` | process at most N requisitions (test runs) |
| `--no-details` | skip the per-job detail call (no full description/category/education) |
| `--run-date DD-MM-YYYY` | target `jobs_csv/<date>/` folder (default: today) |
| `--verbose` | debug logging |

## Outputs

* `purehealth_jobs.csv` — rich cumulative store, deduped on `job_id`.
* `../../jobs_csv/<DD-MM-YYYY>/purehealth.csv` — HealthCareers.club 22-column
  schema.
* `needs_review.csv` — only written when a title/category pair can't be mapped.

Re-running the same day adds 0 rows (master spec §5).

## Tests

```bash
python test_filters.py
```

36 tests covering the category classifier (including the wrong-ATS-category
cases), the free-text experience parser, job-type mapping, location/city
fallbacks, the watermark cutoff, HTML flattening and the club-row mapping. All
worked examples are copied verbatim from live `CX_6007` requisitions.
