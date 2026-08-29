# omega_healthcare scraper

Scrapes Omega Healthcare (medical coding / revenue-cycle-management BPO)
vacancies from the **Oracle HCM Candidate Experience REST API** on
`fa-equm-saasfaprod1.fa.ocs.oraclecloud.com`. The in-scope seam is the
coding side of the house — medical coders, coding auditors, HCC/CDI
roles; the vast AR-calling / billing / patient-interaction voice floor is
out of taxonomy scope and is dropped by the shared classifier (that is
correct behavior, not a bug).

## Two sites, one scraper

| Site | Board | Size (2026-08-27) | Character |
|---|---|---|---|
| `CX_1001` | United States | ~69 reqs | Rich details: real `skills`, `Category`, experience + pay flex fields; ~half are coders |
| `CX_2001` | Offshore delivery — **India AND the Philippines** | ~834 reqs | Opaque BPO grades ("Clinical Executive", "Executive - AR"), mostly description-less |

The CX site does **not** imply the country — CX_2001 mixes India and
Philippines duty stations, so the location is read from each
requisition's `PrimaryLocation`/`PrimaryLocationCountry`, never assumed.
Job URLs carry the requisition's own site
(`…/CandidateExperience/en/sites/CX_2001/job/<id>`, verified HTTP 200).

## How it works

Same platform and pattern as `../undp/` (read that readme for the
platform deep-dive); this folder is self-contained per the fleet rule.

1. **Discovery.** The public candidate API is unauthenticated:

   ```
   GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
       ?onlyData=true
       &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
       &finder=findReqs;siteNumber=<site>,limit=200,offset=<n>,sortBy=POSTING_DATES_DESC
   ```

   **Oracle caps a listing page at 200 rows on this tenant** (limit=200
   returned exactly 200 of CX_2001's 834), so CX_2001 is
   offset-paginated. POSTING_DATES_DESC ordering lets the crawl stop as
   soon as a whole page predates the cutoff — in steady state the
   ~834-row board costs one listing request. `finder` needs literal `;`
   and `,` — URLs are built as strings (a `params` dict would
   percent-encode them and Oracle answers 400).
2. **THE DETAIL CARRIES NO DATES.** Unlike the undp/Novotech tenants,
   `ExternalPostedStartDate` and `ExternalPostedEndDate` are null on
   every sampled detail here. The listing's plain-date `PostedDate` is
   the **only** posted date and decides the time window before any detail
   fetch. Oracle dates can be tenant-timezone (the kfshrc
   Riyadh-midnight lesson); the 2-day watermark grace absorbs any
   boundary off-by-one. `valid_through` is captured only if a
   `PostingEndDate` ever appears — never invented.
3. **Detail, one request per new in-window requisition** —
   `recruitingCEJobRequisitionDetails?expand=all&finder=ById;Id="<id>",siteNumber=<site>`.
4. **CX_2001 rows are mostly description-less** — description,
   responsibilities, qualifications and even the short description are
   all empty on sampled rows. The real signal lives in
   `requisitionFlexFields`: **Speciality** ("HCC", "Hospital Billing",
   "Oncology"), **Service Line** ("Coding", "Patient Interaction",
   "Coverage & Authorization") and **Sub Job Categorization**
   ("Voice/AR", "Support"). `build_skills()` joins these (minus this
   tenant's "Default" placeholder, and minus the operational prompts
   `RFH For` / `Resource Type` / `Required By Date`, which say nothing
   about the work) with the detail's `skills` list and `Category` into
   the classifier's `skills` signal.
5. **CX_1001 rows are rich**: curated `skills` ("CPT Coding", "ICD10
   Diagnostic Coding"), `Category` ("Coding"), flex **Required Years of
   Experience** (structured — parsed before the prose) and **Minimum
   Pay / Maximum Pay** (e.g. `21` / `28.25` for an hourly US coder). The
   pay numbers are bare — **no period, no currency** — so they are
   captured verbatim in the rich CSV (`pay_min_raw`/`pay_max_raw`) and
   the club salary columns stay blank rather than being normalized on a
   guess.
6. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`) and nothing else. Out of scope →
   dropped, counted, full row (with its `vetoed_by` trace) appended to
   `out-of-scope.csv`; `needs_review` → kept AND flagged. Expect the
   classifier to drop most of CX_2001: AR calling, billing,
   patient-interaction and generic support grades either miss every role
   family or hit the billing/revenue-cycle vetoes. The run summary
   prints a per-veto breakdown so that load stays auditable.
7. The flex **Full-Time/Part-Time** field drives the club `job_type`
   enum; `company_type` exports as `hospital` (fleet convention for
   non-pharma employers — Omega is an RCM services firm, the club enum
   has no better value).

## First-run detail cap

Steady-state runs fetch details only for ids newer than the watermark —
a handful. But a first run (no rich CSV yet) with CX_2001's backlog could
try hundreds of fetches, so **when no store exists, detail fetches are
capped at `FIRST_RUN_DETAIL_CAP = 150`, newest first**. Anything
in-window beyond the cap is counted as `deferred_by_cap`, loudly logged,
and NOT recorded in any skip list — re-run (the watermark grace re-offers
the newest overlap) or use `--since` to backfill. `--limit N` overrides
the cap for test runs. The 2026-08-27 first run needed only 54 details
(14-day window), so the cap did not bind.

## robots.txt

The Oracle host serves **no robots.txt (HTTP 404 → no restrictions)** —
the same precedent `../undp/readme.md` documents for this platform.
`check_robots()` still runs at startup so a later-added robots.txt stops
the scraper instead of being ignored.

## Outputs

| File | Purpose |
|---|---|
| `omega_healthcare_jobs.csv` | Rich cumulative store (dedup key: `job_id`) |
| `../../jobs_csv/<DD-MM-YYYY>/omega_healthcare.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids and their dates |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged |

## Running

```bash
../../.venv/bin/python scraper.py                # incremental run
../../.venv/bin/python scraper.py --limit 20     # test run: ≤20 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-01
../../.venv/bin/python test_filters.py           # 49 unit tests, no network
```

Time window: first run keeps the last 14 days; later runs use
`max(stored posted_date) − 2 days`.

**Cost.** Listing requests (1 for CX_1001 + 1–2 for CX_2001 in steady
state) + 1 detail per new in-window requisition, at a 1.5 s delay. First
run (2026-08-27, `first_run.log`): 269 requisitions scanned, 54 in-window
details fetched, ~100 s wall clock. **Measured yield: 16 kept, all of
them CX_1001 US coding roles; all 23 in-window CX_2001 details dropped —
7 on the "Revenue Cycle" veto, 1 on "rcm", 15 with no role-family match
(AR/voice/HRSS grades). A near-zero CX_2001 keep rate is the expected
steady state, not a failure.**

Probed and built 2026-08-27.
