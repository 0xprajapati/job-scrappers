# kimberlyclark scraper

Scrapes Kimberly-Clark's global careers board from the **Workday CXS JSON
API** at `kimberlyclark.wd1.myworkdayjobs.com` (tenant `kimberlyclark`, site
`GLOBAL`). **183 open postings on 2026-08-28** — one of the smallest boards
in the fleet. Kimberly-Clark is a consumer health / personal-care
manufacturer (Huggies, Kleenex, Kotex, Depend, Poise, Cottonelle); its K-C
Professional arm also supplies hospitals and clinics with wiping, hygiene and
surgical/medical consumables. Supply is consumer-goods shaped: mill and plant
operations, process/mechanical engineering, supply chain, sales and
marketing, with only a thin regulatory/quality/EHS slice that the shared
classifier can keep. **A low or zero keep rate on this board is the correct
outcome, not a filter fault** — no compensating filters are added here.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/kimberlyclark/GLOBAL/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (`limit=50` → HTTP 400, probe-verified
  2026-08-28). Each posting carries title, externalPath, locationsText, a
  *relative* `postedOn` label ("Posted Today" … "Posted 30+ Days Ago") and
  the requisition id in `bulletFields`. `total` is only reliable on the
  offset=0 page (offset 100 reported `total: 0`).
- **Detail**: `GET /wday/cxs/kimberlyclark/GLOBAL/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code` under
  `jobRequisitionLocation`, and the canonical `externalUrl`.
  Tenant quirks: **no `additionalLocations` key** and
  **`hiringOrganization.name` is an empty string** — both degrade to blank
  columns.
- **robots.txt**: `Allow:` for each public site slug including `/GLOBAL/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked at
  startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Workday sites

`WORKDAY_SITES = ['GLOBAL']` — the public careers board. Robots.txt
advertises nine other slugs; three were spot-probed on 2026-08-28:

| slug | total | verdict |
| --- | --- | --- |
| `GLOBAL` | 183 | **scraped** — the public careers board |
| `LinkedIn` | 150 | mirror — same requisition ids in the same order (885928, 886919, …), a subset of GLOBAL |
| `XXX_NA` | 0 | empty aggregator feed |
| `Arbex` | 99 | **distinct slice — NOT scraped**, see below |

`Arbex` is worth flagging: its 99 postings do **not** appear on GLOBAL
(GLOBAL's `searchText` returns 0 for "Global Privacy Counsel" and
"Practicante de Ventas", both of which sit on `Arbex`, while the same search
correctly finds GLOBAL's own titles). It is a separate recruiting site on the
same tenant, and its own `searchText` is broken (it ignores the query), so it
was not cross-mapped further. It is excluded here because the tenant config
for this scraper is `GLOBAL` only. If the fleet later wants that inventory it
is a config-only change — but it should be a deliberate decision, since the
two slugs share no requisitions and adding it would roughly +54% the board.

## Ordering soundness

Probed 2026-08-28 with `searchText=""`:

| offset | postedOn labels |
| --- | --- |
| 0 | "Posted Today" / "Posted Yesterday" / "Posted 2 Days Ago" |
| 100 | "Posted 16 Days Ago" → "Posted 25 Days Ago" |
| 180 (last page, 3 rows) | all "Posted 30+ Days Ago" |

Labels age monotonically with offset and `total` (183) is not a round display
cap, so **newest-first ordering holds** and the template's early stop is
sound. In practice it never fires here: 183 ≤ `EXHAUSTIVE_BOARD_MAX` (200),
so the walk runs to the end of the board every run (~10 listing pages).

## Why the window matters here

The relative `postedOn` label decides the time window **before** any detail
request: out-of-window ids go to `seen_old_ids.csv` unfetched. On a 183-row
board that is the whole cost saving — the ten listing pages are walked
regardless, but only genuinely new postings cost a detail GET. The detail's
exact `startDate` re-checks the window after the fetch (the relative label
has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was **not** capped
(`--max-pages` omitted) — the board is small enough to walk whole.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Kimberly-Clark);
`hiring_org` is blank on this tenant.

Location strings are ISO-and-state-prefixed hyphen hierarchies
("USA-WI-Marinette", "KOR-Seoul", "GBR-Kent-Northfleet"). `parse_city` skips
bare 2–3 letter all-uppercase tokens so neither the ISO country prefix nor
the US state code leaks into the city column.

## Usage

```bash
../../.venv/bin/python scraper.py                      # daily incremental run
../../.venv/bin/python scraper.py --limit 5            # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window / re-seed
python test_filters.py                                 # unit tests (62)
```

Outputs: `kimberlyclark_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/kimberlyclark.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
