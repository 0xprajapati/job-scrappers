# abt scraper

Scrapes Abt Global (US-headquartered development implementer) vacancies
from the **Oracle HCM Candidate Experience REST API** on
`egpy.fa.us2.oraclecloud.com`. The densest health board of the 2026-08
probe sweep: 15–16 open requisitions, **60% title-only in-scope** —
epidemiologists, malaria chiefs-of-party, WASH specialists, global health
security consultants. Expect a heavily **Public Health** mix.

## One site, tiny board

The tenant publishes **one** CE site, `JoinAbt` (probed 2026-08-28: 16
requisitions, `TotalJobsCount` agrees). `SITE_NUMBERS` stays a tuple so a
second site (the novotech situation) is a one-line change. The board is
tiny and slow-cadence, so **every run walks it fully** and fetches details
for all in-window candidates — classification needs the descriptions.

Job URLs: `…/hcmUI/CandidateExperience/en/sites/JoinAbt/job/<id>`
(verified HTTP 200).

## How it works

Same platform and same pattern as `../novotech/` (built from the undp
pattern — read those readmes for the platform deep-dive); this folder is
self-contained per the fleet rule.

1. **Discovery — one request.** The public candidate API is
   unauthenticated:

   ```
   GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
       ?onlyData=true
       &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
       &finder=findReqs;siteNumber=JoinAbt,limit=500,sortBy=POSTING_DATES_DESC
   ```

   The whole board fits one page; `TotalJobsCount` is asserted against the
   row count so the board outgrowing a page warns loudly. `finder` needs
   literal `;` and `,` — URLs are built as strings (a `params` dict would
   percent-encode them and Oracle answers 400).
2. **The window is decided before any detail fetch.** The listing's plain
   `PostedDate` matches the date half of the detail's
   `ExternalPostedStartDate` (sampled). Oracle dates can be
   tenant-timezone (the kfshrc Riyadh-midnight lesson); the 2-day
   watermark grace absorbs any boundary off-by-one.
3. **`ExternalPostedEndDate` is REAL on this tenant** (unlike
   novotech/omega): application deadlines ~2–4 weeks out → `valid_through`
   is captured from the detail, never invented.
4. **Detail, one request per new in-window requisition** —
   `recruitingCEJobRequisitionDetails?expand=all&finder=ById;Id="<id>",siteNumber=JoinAbt`.
5. **`Category` and `JobFunction` are populated** ("Program Delivery",
   "Project and Program Management" / "Program Operations (Field)"…) —
   generic org-structure tags, nothing that mislabels a role as health
   (the skills-veto-is-per-board rule), so both are joined into the
   classifier's `skills` signal and stored in the rich `sectors` column.
   `JobFamily`, `skills` and `requisitionFlexFields` are dead. The
   posting text lives in `ExternalDescriptionStr` (up to ~30k chars of
   styled HTML, capped at 20k after stripping); the
   responsibilities/qualifications fields were empty everywhere sampled
   but are still concatenated (the novotech split-body lesson).
6. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`) and nothing else. Out of scope →
   dropped, counted, full row (with its `vetoed_by` trace) appended to
   `out-of-scope.csv`; `needs_review` → kept AND flagged.
7. **Language.** Abt has French-speaking country-office footprints (DRC,
   Madagascar, Côte d'Ivoire). Sampled postings are English, but any
   FR/ES description is kept and force-flagged into `needs_review.csv`
   via the undp stopword heuristic (`looks_non_english`) rather than
   published untranslated.
8. **EOI / talent pools.** "Expression of Interest" and "Technical
   Advisory Panel" calls share the requisition id space with real
   vacancies (3 of 16 on the probe). In-scope ones are kept but
   force-flagged `needs_review` (`is_eoi_title`, the undp RFP idiom).
9. **Location.** `PrimaryLocation` is "City, Country" or
   "City, ST, United States"; `PrimaryLocationCountry` is ISO alpha-2,
   mapped through the fleet-vetted table. The tenant writes country
   ALIASES in the location string ("Kinshasa, DR Congo-Kinshasa") —
   `COUNTRY_LOCATION_ALIASES` drops those instead of leaking them into
   the city. An unknown code exports a blank country, never a guess.
   Abt publishes **no salary** → salary columns stay blank.

`company_type` exports as `hospital` (the fleet-wide convention for
non-pharma employers in the club's hospital|pharma enum — Abt is a
development implementer).

## robots.txt

The Oracle host serves **no robots.txt (HTTP 404 → no restrictions)** —
the same precedent `../undp/readme.md` documents for this platform.
`check_robots()` still runs at startup so a later-added robots.txt stops
the scraper instead of being ignored.

## Outputs

| File | Purpose |
|---|---|
| `abt_jobs.csv` | Rich cumulative store (dedup key: `job_id`) |
| `../../jobs_csv/<DD-MM-YYYY>/abt.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids and their dates |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged |

## Running

```bash
../../.venv/bin/python scraper.py                # incremental run
../../.venv/bin/python scraper.py --limit 5      # test run: ≤5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-01
../../.venv/bin/python test_filters.py           # 55 unit tests, no network
```

Time window: first run keeps the last 30 days (tiny slow-cadence board —
the wider window seeds the whole live board); later runs use
`max(stored posted_date) − 2 days`.

**Cost.** 1 listing request + 1 detail per new in-window requisition, at
a 1.5 s delay. First run (2026-08-28, `first_run.log`): 16 requisitions
scanned, 16 details fetched, ~40 s wall clock. Steady state: 1 listing
request + a handful of details.

Probed and built 2026-08-28.
