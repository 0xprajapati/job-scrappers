# seha.ae scraper

Scrapes job openings from [seha.ae/careers](https://www.seha.ae/careers) —
**SEHA (Abu Dhabi Health Services Company)**, the largest healthcare network in
the UAE, operating Abu Dhabi's public hospitals, specialty centres and
ambulatory clinics (SKMC, Tawam, Corniche, Al Ain, Al Rahba, Sakina, …).

## Data source

The careers page is static; recruiting runs on SEHA's **Oracle Recruiting Cloud**
candidate portal (`fa-eutv-saasfaprod1.fa.ocs.oraclecloud.com/.../sites/CX_1`),
which exposes the standard public, token-free Oracle CE REST API:

- **Listing**: `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions`
  with `finder=findReqs;siteNumber=CX_1,limit,offset,sortBy=POSTING_DATES_DESC`.
  The tenant is SEHA-only (~131 open requisitions), so no organization facet is
  needed. Returns just `Id / Title / PostedDate / PrimaryLocation`.
- **Details** (per NEW job, on by default; `--no-details` skips):
  `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails` with
  `finder=ById;Id="<id>",siteNumber=CX_1` — Oracle category facet, job schedule,
  study level, work location and the full HTML description.
- **Job URL**: `.../hcmUI/CandidateExperience/en/sites/CX_1/job/<id>` (real
  per-job deep link, unlike the Zulekha portal).
- **robots.txt**: seha.ae publishes `User-agent: * / Disallow:` (everything
  allowed); the Oracle host serves no robots.txt (404 ⇒ allowed). Both are
  checked at startup and the run aborts if either turns into a disallow.

## Quirks

- **No salary data** anywhere on the portal → `salary_raw = "Not Disclosed"`,
  numeric/club salary fields blank (never invented, master spec §3).
- **Oracle's CATEGORIES facet is populated on only ~40% of requisitions**
  (Medical / Nursing / Allied Health / Administration), so the title classifier
  carries most of the load. Order of decision: facet → pharmacy/nursing title →
  ambiguous → allied/corporate → clinical keywords → flag. `Allied Health` and
  `Administration` both map to the club's `non_clinical` (no closer bucket).
- **Corporate titles borrow clinical words** — SEHA heads head-office roles
  "Specialist - Talent & Performance", "Assistant Manager - Business
  Development". Allied/corporate keywords are therefore tested *before*
  `Consultant`/`Specialist`/`Physician`, so those don't land in `doctors`.
- **Behavioural-health clinicians have no club bucket** (Clinical/Child
  Psychologist, Genetic Counsellor, Neuro Physiologist, Health Care Assistant).
  They are kept as `non_clinical` **and** flagged `needs_review` → written to
  `needs_review.csv` for a human to place — never silently dropped (§2).
- **A trailing parenthetical is ambiguous**: it is either the facility
  (`Sonographer (Tawam Fertility Center)`, `Consultant Dermatology (STMC)`) or a
  role qualifier (`Embryologist (IVF)`, `Consultant Neurology (Arabic Speaker)`).
  Only facility-looking parentheticals are peeled into `facility`; qualifiers
  stay in the title. Acronyms are expanded (`SKMC` → Sheikh Khalifa Medical
  City) and redundant tails collapsed (`Sheikh Khalifa Medical City - SKMC`,
  `Tawam - TWM`) so one facility can't become two club `company_name` values.
- **Experience is free text, not a field** — `WorkYears`/`WorkMonths` are always
  null. It is parsed out of the description, which uses SEHA's house style
  "NLT 2 years" (not less than) plus tiered requirements
  ("Tier 1: NLT 2 years … Tier 2: NLT 8 years"). The **lowest** stated bar is
  recorded as `min_experience`; `max_experience` stays blank. Numbers not tied
  to a requirement cue or to the word "experience" are ignored, so
  "2 year contract" / "25 years of age" don't leak in. Populated on ~89/131 jobs.
- **~5 requisitions have empty description fields** on the portal itself; those
  rows carry a blank description rather than a fabricated one.
- **Some requisitions carry only "United Arab Emirates"** with no city →
  `city` falls back to `Abu Dhabi`.
- **Time window**: an ATS lists only OPEN requisitions (SEHA keeps some live
  since 2024-06), so the first run keeps all of them
  (`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
  (newest stored `posted_date` − `WATERMARK_GRACE_DAYS = 2`) and stop
  paginating once a page is entirely older — the second run takes ~4 s instead
  of ~3 min.

## Usage

```bash
../../.venv/bin/python scraper.py
```

Useful flags:

- `--limit N` / `--max-pages N` — small test runs.
- `--no-details` — listing only (much faster, but no category facet, schedule,
  education, experience or description).
- `--output PATH` — alternate rich CSV (default `seha_jobs.csv`).
- `--run-date DD-MM-YYYY` — target `jobs_csv/` folder (default: today).
- `--verbose` — debug logging.

Unit tests (parsers, classifier, cutoff logic — all examples taken from real
SEHA listings):

```bash
../../.venv/bin/python test_filters.py
```

## Outputs

- `seha_jobs.csv` — rich cumulative store, deduplicated on `job_id`. Running
  twice in a row adds 0 rows and leaves the file byte-identical.
- `../../jobs_csv/<DD-MM-YYYY>/seha.csv` — HealthCareers.club 22-column schema.
- `needs_review.csv` — titles the classifier could not confidently place.

## First run (2026-07-27)

131 open requisitions, all captured: 72 `doctors`, 20 `nurses`,
2 `pharmacists`, 37 `non_clinical` (4 of them flagged `needs_review`).
Cities: Abu Dhabi 94, Al Ain 33, Al Dhafra 4. Descriptions on 126/131
(mean ~3.7k chars), experience on 89/131, education on 50/131.
