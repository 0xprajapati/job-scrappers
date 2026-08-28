# haleon scraper

Scrapes Haleon's global careers board from the **Workday CXS JSON API** at
`gsknch.wd3.myworkdayjobs.com` (tenant `gsknch`, site `GSKCareers`). **373
open postings on 2026-08-28.** Haleon is the consumer-healthcare company
demerged from GSK in 2022 (Sensodyne, Panadol, Advil, Voltaren, Centrum,
Otrivin). Supply is commercial-heavy — sales/expert reps, brand marketing,
supply chain and a large Bengaluru tech-and-shared-services campus — with a
smaller in-scope slice (regulatory affairs/CMC, medical, quality, R&D
formulation, pharmacists) that the shared classifier picks out.

## Legacy naming — read this before touching the config

The tenant slug (`gsknch`, "GSK Nutrition & Consumer Healthcare") and the
site slug (`GSKCareers`) both **predate the demerger and were never
renamed**. The board is Haleon's:

- `robots.txt` on this host disallows `/haleon_internal_aw/`;
- `hiringOrganization` reads e.g. `12205 Haleon Hungary Kft.`;
- descriptions open "Welcome to Haleon".

This is a **different tenant** from GSK plc's board (`gsk.wd5`, scraped by
`scrappers/gsk`) — the two share no requisitions. The fleet uses `haleon`
for the directory, CSV names and the `source` column; only HOST/TENANT/
WORKDAY_SITES carry the legacy strings.

`WORKDAY_SITES` holds one site: `GSKCareers`. No alt sites were supplied or
found — the internal site referenced by robots.txt (`haleon_internal_aw`) is
explicitly Disallowed and is not crawled.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/gsknch/GSKCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the numeric requisition id in
  `bulletFields` (e.g. `546217`). `total` is only reliable on the offset=0
  page (deeper pages report 0 — confirmed here: offsets 100/200 returned
  `total: 0` with 20 postings each).
- **Detail**: `GET /wday/cxs/gsknch/GSKCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code` on
  `jobRequisitionLocation.country`, and the canonical `externalUrl`.
  `additionalLocations` is absent on this tenant's payloads.
- **robots.txt**: `Allow: /GSKCareers/`, `Disallow: /haleon_internal_aw/`,
  `Disallow: /refreshFacet/` — the CXS API paths are allowed. Checked at
  startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness (probe-verified 2026-08-28)

With `searchText=""` the `postedOn` labels age **monotonically** with offset:

| offset | labels observed |
| ------ | --------------- |
| 0      | all 20 "Posted Today" |
| 100    | all 20 "Posted 4 Days Ago" |
| 200    | "Posted 10 / 11 / 14 Days Ago" |

Newest-first therefore holds, and `total` (373) is not a round display cap,
so **the template's consecutive-old-pages early stop is sound on this
tenant** and is left enabled as shipped.

## Why the window matters here

Velocity works out to roughly **25 postings a day** (offset 100 was already
4 days old, offset 200 was 10–14 days old). The relative `postedOn` label
decides the time window **before** any detail request is spent: out-of-window
ids go to `seen_old_ids.csv` unfetched, and the newest-first walk stops after
2 consecutive pages with nothing in the window. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has only
day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was capped
(`--max-pages 15`, i.e. the 300 newest postings — comfortably more than the
7-day window needs) to keep the initial burst modest; the watermark takes
over from run 2.

## Location formats on this board

Four shapes show up, all handled by `parse_city`:

- country-first hyphen hierarchies —
  `China - Shanghai - HuangPu District - The Headquarters Building` → `Shanghai`,
  `Malaysia - Kuala Lumpur` → `Kuala Lumpur`, `UK - London` → `London`;
- campus names — `Bengaluru Campus 31`, `London Bankside`,
  `Poznan Business Garden` — kept whole (the CorroHealth "Noida Luminaire"
  convention);
- `N Locations` placeholders → no city;
- a few site descriptors with no separator at all (`Hungary Field Worker`),
  which `parse_city` returns whole — nothing in the string distinguishes the
  site name from a locality.

Two known limitations, both inherited from the shared template and left
unchanged: hierarchies that name a **full** state/province before the city
(`USA - New Jersey - Warren`, `India - Haryana - Gurgaon`) yield the
state/province, and the fleet-standard ISO-token skip
(`re.fullmatch(r"[A-Z]{2,3}", part)`) only catches 2–3 letter uppercase
codes.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV); on a consumer-health board that is the majority — sales reps,
brand managers, IT and finance. `needs_review` rows are kept and logged to
`needs_review.csv`; non-English (DE/FR/ES) descriptions are kept but forced
into review — Haleon posts some European country-office vacancies
untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Haleon); the Workday legal
entity (e.g. "12205 Haleon Hungary Kft.") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15   # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5        # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20  # explicit window
python test_filters.py                             # unit tests (62)
```

Outputs: `haleon_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/haleon.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
