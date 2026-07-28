# Sidra Medicine scraper

Scrapes job openings from **Sidra Medicine** (Doha, Qatar) — the Qatar
Foundation academic medical centre for paediatric and women's healthcare — via
the career site published at
[sidra.org/careers](https://www.sidra.org/careers).

## Data source

Sidra recruits on an **Oracle Recruiting Cloud (ORC) Candidate Experience**
site:

```
https://fa-epxn-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Sidra-Career-Site/jobs
```

That SPA is backed by Oracle's public, token-free REST API, which is what this
scraper uses (master spec §1.1 — an underlying JSON API is the best source).
The tenant is Sidra-only (23 open requisitions at first run), so no organization
facet is needed:

| Call | Endpoint |
|---|---|
| List | `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=Sidra-Career-Site,limit=100,offset=0,sortBy=POSTING_DATES_DESC` |
| Detail | `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="<Id>",siteNumber=Sidra-Career-Site` |

- The **listing** carries only `Id / Title / PostedDate / PrimaryLocation` plus
  a short description.
- The **detail** call (per NEW job, on by default; `--no-details` skips it)
  supplies Oracle's category facet, requisition type, job schedule, study level,
  the posting end date (`expires_at`) and the full HTML description +
  responsibilities + qualifications.
- **Job URL**: `.../hcmUI/CandidateExperience/en/sites/Sidra-Career-Site/job/<id>`
  — a real per-job deep link (verified HTTP 200).
- **robots.txt**: sidra.org publishes `User-agent: * / Disallow:` (everything
  allowed); the Oracle host serves no robots.txt (404 ⇒ allowed). Both are
  checked at startup and the run aborts if either turns into a disallow.

## Quirks

- **No salary data** anywhere on the portal → `salary_raw = "Not Disclosed"`,
  numeric/club salary fields blank (never invented, master spec §3).
- **Oracle's category facet is populated on every live requisition** —
  `Physician` / `Nursing` / `Allied Health` / `Enabling Function: <function>` —
  so it drives the club category, with the title classifier as fallback.
  `Enabling Function: …` is matched by **prefix**, so a new sub-function (IT,
  Finance, …) still maps to `non_clinical`. `Allied Health` also has no club
  bucket of its own and lands in `non_clinical` (same treatment SEHA gives it).
  An unmistakable title still overrides a coarse facet: `Specialist -
  Medication Management and Pharmacy Quality` (facet *Allied Health*) →
  `pharmacists`.
- **Talent-pool campaigns are not vacancies.** `CM0055` is a
  "Join Our Talent Pool - Future opportunities" campaign requisition
  (`RequisitionType = Campaigns`) with an empty description — an expression of
  interest, not a live opening. It is kept, marked `talent_pool = True` and
  flagged `needs_review` so a human decides (§2 — never silently dropped).
- **Bilingual portal**: that same campaign returns the Arabic schedule label
  `كل الوقت` (full time) instead of the English one, so `map_job_type` maps the
  Arabic labels explicitly.
- **Qatarization markers live in the title** (`(Nationals only)`,
  `(Qatarized)`) → captured as `nationals_only` (2 of 23 jobs); the marker stays
  in the title as posted.
- **Title parentheticals are always role qualifiers**, never a facility (one
  hospital): `(EP)`, `(Operating Room)`, `(Arabic Speaker)`, `(PhD)`. They stay
  in the title. Only dashes acting as a *separator* are normalised
  (`Manager – Ethics` → `Manager - Ethics`); an intra-word hyphen
  (`(Non-Invasive)`) survives untouched.
- **Experience is free text inside the qualifications table**, in Sidra's house
  style "5+ years" / "2+ Years specialized experience" / "A minimum of 1 year".
  The parser reads the table's **Experience** section only, so education years
  ("Diploma in Practical Nursing (2 years)") can't leak in, and records the
  **lowest** stated bar as `min_experience` (`max_experience` is filled only for
  an explicit range like "3 - 5 years" — none present so far). Populated on
  22/23 jobs. Outside an Experience section it falls back to cue-anchored
  matches only ("minimum of 8 years", "… years … experience").
- **Location is country-level**: every requisition carries only `Qatar`
  (`workLocation` is empty), so `city` falls back to `Doha`, where the single
  campus sits in Education City.
- **Time window**: an ATS lists only OPEN requisitions (Sidra's oldest live post
  dates to 2026-06-11), so the first run keeps all of them
  (`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
  (newest stored `posted_date` − `WATERMARK_GRACE_DAYS = 2`) and stop
  paginating once a page is entirely older — the second run takes ~2 s instead
  of ~35 s.

## Usage

```bash
../../.venv/bin/python scraper.py
```

Useful flags:

- `--limit N` / `--max-pages N` — small test runs.
- `--no-details` — listing only (much faster, but no category facet, schedule,
  education, experience, expiry or full description).
- `--output PATH` — alternate rich CSV (default `sidra_jobs.csv`).
- `--run-date DD-MM-YYYY` — target `jobs_csv/` folder (default: today).
- `--verbose` — debug logging.

Unit tests (parsers, classifier, cutoff logic — all examples taken from real
Sidra listings):

```bash
../../.venv/bin/python test_filters.py
```

## Outputs

- `sidra_jobs.csv` — rich cumulative store, deduplicated on `job_id`. Running
  twice in a row adds 0 rows and leaves the file byte-identical.
- `../../jobs_csv/<DD-MM-YYYY>/sidra.csv` — HealthCareers.club 22-column schema.
- `needs_review.csv` — requisitions the classifier could not confidently place.

## First run (2026-07-27)

All 23 open requisitions captured: 4 `doctors`, 4 `nurses`, 1 `pharmacists`,
14 `non_clinical` (1 flagged `needs_review` — the talent-pool campaign).
All in Doha, Qatar; 22 full-time and 1 part-time. Descriptions on 22/23
(mean ~6.5k chars), experience on 22/23, education and expiry dates on 22/23.
The 23rd is the empty talent-pool campaign requisition.
