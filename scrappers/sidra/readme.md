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
- **Classification is the shared two-level taxonomy**
  (`scrappers/_shared/classification.py`): `category` is `Non Clinical` |
  `Public Health`, plus a `sub_category` (Clinical Data Management, Clinical
  Research, Medical Coding, Infection Prevention & Control, …). Sidra is a
  specialist hospital, so most requisitions (physicians, nursing, allied
  health, generic corporate roles) are **out of scope and dropped**, counted as
  `excluded_out_of_scope` in the run summary.
- **Oracle's category facet is populated on every live requisition** —
  `Physician` / `Nursing` / `Allied Health` / `Enabling Function: <function>` —
  but it no longer decides anything. It is kept verbatim in the rich CSV as
  `category_original` and, together with `requisition_type` and `job_function`,
  passed to the classifier as its `skills` signal only.
- **Qualification**: Oracle's structured `StudyLevel` when present, else a
  grounded extraction from the description — never inferred.
- **Talent-pool campaigns are not vacancies.** `CM0055` is a
  "Join Our Talent Pool - Future opportunities" campaign requisition
  (`RequisitionType = Campaigns`) with an empty description — an expression of
  interest, not a live opening. One that is in scope is kept, marked
  `talent_pool = True` and **always** flagged `needs_review` so a human decides.
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

Unit tests — 37 covering the parsers, the classification wiring (in-scope role
gets the right category/sub_category; clinical titles are dropped; an in-scope
talent-pool row is kept and flagged) and the cutoff logic; all examples from real
Sidra listings):

```bash
../../.venv/bin/python test_filters.py
```

## Outputs

- `sidra_jobs.csv` — rich cumulative store, deduplicated on `job_id`. Running
  twice in a row adds 0 rows and leaves the file byte-identical.
- `../../jobs_csv/<DD-MM-YYYY>/sidra.csv` — HealthCareers.club 22-column schema
  (`CLUB_COLUMNS` imported from `_shared/classification.py`; `is_active` /
  `expires_at` are retired, `sub_category` and `qualification` are in).
- `needs_review.csv` — in-scope rows whose title looks like another profession,
  plus every in-scope talent-pool campaign.
- `out-of-scope.csv` — rows the shared classifier rejected during the one-off
  stored-data reclassification (reversible; never silently discarded).

## Coverage note

Earlier crawls captured every open requisition (all in Doha, Qatar; mostly
full-time, descriptions ~6.5k chars). Under the two-level taxonomy nearly all
of them are clinical or generic-corporate and therefore out of scope: the
stored rich CSV keeps only the Non Clinical / Public Health roles, with the
rest preserved in `out-of-scope.csv`.
