# resmed scraper

Scrapes ResMed's global careers board from the **Workday CXS JSON API** at
`resmed.wd3.myworkdayjobs.com` (tenant `resmed`, site
`ResMed_External_Careers`). **224 open postings on 2026-08-28.** ResMed makes
sleep apnea and respiratory medical devices (CPAP, ventilation) plus
out-of-hospital care software, so supply is device-maker shaped: engineering,
manufacturing, supply chain, field sales and retail store roles dominate, and
the in-scope slice is the regulatory / quality / clinical / medical-affairs
tail. Countries skew Australia–New Zealand, Germany, the US and Southeast
Asia.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/resmed/ResMed_External_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** — `limit=50` answered **HTTP 400** when
  probed on 2026-08-28. Each posting carries title, externalPath,
  locationsText, a *relative* `postedOn` label ("Posted Today" … "Posted 30+
  Days Ago") and the requisition id in `bulletFields` (`JR_050759` form).
  `total` is only reliable on the offset=0 page — offsets 100 and 200 both
  reported `total: 0`.
- **Detail**: `GET /wday/cxs/resmed/ResMed_External_Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code` under
  `jobRequisitionLocation`, and the canonical `externalUrl`.
  `additionalLocations` is **absent** on single-site requisitions here (the
  parser treats a missing key as an empty list).
- **robots.txt**: `Allow: /ResMed_External_Careers/` (plus the three sibling
  sites), `Disallow: /myjobs/` and `/refreshFacet/` only — the CXS API paths
  are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Why only one Workday site

The tenant's robots.txt advertises four sites. The three siblings were probed
on 2026-08-28 and are **excluded** — they are separate ResMed-owned software
brands with their own boards, not the ResMed brand this scraper covers:

| site | postings | verdict |
|---|---|---|
| `ResMed_External_Careers` | 224 | **included** |
| `Brightree_External_Careers` | 5 | excluded — separate brand |
| `MatrixCare_External_Careers` | 4 | excluded — separate brand |
| `Aria_Health_Careers` | 0 | excluded — separate brand, empty |

## Ordering soundness (probe-verified 2026-08-28)

With `searchText=""`, `postedOn` labels **age monotonically with offset**:

| offset | labels observed |
|---|---|
| 0 | 11 × "Posted Today", 7 × "Posted Yesterday", 2 × "Posted 2 Days Ago" |
| 100 | 20 × "Posted 30+ Days Ago" |
| 200 | 20 × "Posted 30+ Days Ago" |

Newest-first ordering **holds**, and `total` (224) is not a round display cap,
so the template's consecutive-old-page early stop is sound on this tenant. No
per-tenant override was needed.

## Why the window matters here

The board posts roughly **7–10 requisitions a day** (the whole of page 0 was
inside 2 days, while offset 100 was already 30+ days old), so the 7-day window
closes within the first few listing pages. The relative `postedOn` label
decides the window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the newest-first walk stops after 2
consecutive pages with nothing in the window. The detail's exact `startDate`
re-checks the window after the fetch (the relative label has only day
granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. At 224 postings the board is small enough
that the first run needed **no `--max-pages` cap** — the early stop closes it
after a handful of pages on its own.

## Known limitation: space-prefixed locations

About **one posting in six** writes the location with a space-separated
country/state prefix and no comma or hyphen — `DE Neuss`, `DE Bundesweit`,
`DE Hildesheim`, `AU WA Ardross (Store)`, `AU QLD Aspley (Store)`,
`US VirtuOx (Remote)`, `US Field Non-Sales (Remote Workforce)`. The shared
`parse_city` splits only on `,` `-` `–`, so the fleet-standard ISO-token skip
cannot reach these and the prefix stays in `city` (e.g. `city = "DE Neuss"`).

This is left as-is on purpose: the parser is fleet-shared and space-splitting
is not one of the approved template amendments. Impact is contained —
`country` and `country_code` are always correct because they come from the
detail's ISO `alpha2Code`, never from the location string, and the majority
format (`Berlin, Germany`, `Sydney, NSW, Australia`) parses cleanly. The
behavior is pinned by `test_space_prefixed_iso_code_is_a_known_limitation` so
a future fleet-wide fix trips the test rather than passing silently.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV) — on a device maker this is most of the board. `needs_review` rows
are kept and logged to `needs_review.csv`; non-English (DE/FR/ES)
descriptions are kept but forced into review, which matters here because the
German subsidiaries (MEDIFOX DAN, Töchter & Söhne) post wholly in German.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (ResMed); the Workday legal
entity (e.g. "902 MEDIFOX DAN GmbH") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (64)
```

Outputs: `resmed_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/resmed.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
