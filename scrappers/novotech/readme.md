# novotech scraper

Scrapes Novotech (APAC-headquartered contract research organization)
vacancies from the **Oracle HCM Candidate Experience REST API** on
`fa-euzi-saasfaprod1.fa.ocs.oraclecloud.com`. The supply is CRO delivery
roles — CRAs, clinical operations, clinical data, biostatistics, project
management — with a meaningful in-scope seam (clinical research /
clinical data management under the shared taxonomy).

## Two sites, disjoint boards

The tenant publishes **two** CE sites that serve **disjoint** requisition
sets (probed 2026-08-27: 99 + 62 rows, zero id overlap) and both are
crawled every run:

| Site | Board | Sample footprint |
|---|---|---|
| `CX_1` | Global/APAC | AU, US, GB, JP, SG, KR, IN, NZ, TH, … |
| `CX_4` | Mainland China | CN (Shanghai, Beijing, …) |

Job URLs carry the requisition's own site
(`…/CandidateExperience/en/sites/CX_4/job/<id>`, verified HTTP 200), so
`site_number` is a rich-CSV column.

## How it works

Same platform and same pattern as `../undp/` (read that readme for the
platform deep-dive); this folder is self-contained per the fleet rule.

1. **Discovery — one request per site.** The public candidate API is
   unauthenticated:

   ```
   GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
       ?onlyData=true
       &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
       &finder=findReqs;siteNumber=<site>,limit=500,sortBy=POSTING_DATES_DESC
   ```

   Each board fits one page; `TotalJobsCount` is asserted against the row
   count so the board outgrowing a page warns loudly. `finder` needs
   literal `;` and `,` — URLs are built as strings (a `params` dict would
   percent-encode them and Oracle answers 400).
2. **The window is decided before any detail fetch.** The listing's plain
   `PostedDate` matches the date half of the detail's
   `ExternalPostedStartDate` (sampled), so no detail request is spent on
   an out-of-window posting. Oracle dates can be tenant-timezone (the
   kfshrc Riyadh-midnight lesson); the 2-day watermark grace absorbs any
   boundary off-by-one. `PostingEndDate`/`ExternalPostedEndDate` are null
   everywhere sampled → `valid_through` captured when present, never
   invented.
3. **Detail, one request per new in-window requisition** —
   `recruitingCEJobRequisitionDetails?expand=all&finder=ById;Id="<id>",siteNumber=<site>`.
4. **The posting text is SPLIT across three fields.** On many rows
   (especially CX_4) `ExternalDescriptionStr` is only an intro and the
   bulk sits in `ExternalResponsibilitiesStr` +
   `ExternalQualificationsStr` (e.g. requisition 4380: 356 + 2230 + 587
   chars). `build_description()` concatenates all three in reading order,
   falling back to `ShortDescriptionStr`.
5. **Oracle's taxonomy columns are dead on this tenant** — `JobFamily`,
   `JobFunction`, `Category` null, `skills` `[]`, `requisitionFlexFields`
   empty on every sampled detail. So `classify_job` gets title +
   description only; `sectors` stays blank unless a `Category` ever
   appears (captured just in case).
6. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`) and nothing else. Out of scope →
   dropped, counted, full row (including its `vetoed_by` trace) appended
   to `out-of-scope.csv`; `needs_review` → kept AND flagged. Roughly a
   quarter of the board survives — CRA/clinical-operations/clinical-data
   roles — and BD/finance/IT/proposals rows drop.
7. **Language.** CX_4 sampled postings are English, but any
   Chinese-language description is kept and force-flagged into
   `needs_review.csv` via a cheap CJK-ratio check (`looks_cjk`) rather
   than published untranslated.
8. **Location.** `PrimaryLocation` is "City, Region, Country" or just
   "Country"; `PrimaryLocationCountry` is ISO alpha-2, mapped through the
   fleet-vetted table (extended with HK/TW/MO). An unknown code exports a
   blank country, never a guess. Novotech publishes **no salary** →
   salary columns stay blank.

`company_type` exports as `pharma` (CRO) in the club schema.

## robots.txt

The Oracle host serves **no robots.txt (HTTP 404 → no restrictions)** —
the same precedent `../undp/readme.md` documents for this platform.
`check_robots()` still runs at startup so a later-added robots.txt stops
the scraper instead of being ignored.

## Outputs

| File | Purpose |
|---|---|
| `novotech_jobs.csv` | Rich cumulative store (dedup key: `job_id`) |
| `../../jobs_csv/<DD-MM-YYYY>/novotech.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids and their dates |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged |

## Running

```bash
../../.venv/bin/python scraper.py                # incremental run
../../.venv/bin/python scraper.py --limit 20     # test run: ≤20 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-01
../../.venv/bin/python test_filters.py           # 47 unit tests, no network
```

Time window: first run keeps the last 14 days; later runs use
`max(stored posted_date) − 2 days`.

**Cost.** 2 listing requests + 1 detail per new in-window requisition, at
a 1.5 s delay. First run (2026-08-27, `first_run.log`): 161 requisitions
scanned across both sites, 39 in-window details fetched (27 kept, 12
dropped out of scope), ~80 s wall clock. Steady state: 2 listing requests
+ a handful of details.

Probed and built 2026-08-27.
