# harris scraper

Scrapes the Harris Computer group careers board from the **Workday CXS JSON
API** at `harriscomputer.wd3.myworkdayjobs.com` (tenant `harriscomputer`,
site **`1`**). **232 open postings on 2026-08-28.**

Harris (Harris Computer, part of Constellation Software) is a vertical-market
software group that buys and holds dozens of operating businesses. Several are
healthcare/EHR vendors — Altera Digital Health, Picis, iMDsoft, AmazingCharts,
Harris Healthcare — so the group board carries some clinical-informatics and
health-IT work alongside a much larger body of software engineering, sales,
finance and support roles. Most titles are engineering/commercial and the
shared classifier drops them; a high `excluded_out_of_scope` ratio is expected
and correct here.

## The site slug really is `1`

Probe-verified 2026-08-28. This is not a placeholder:

- `robots.txt` advertises `Sitemap: .../1/siteMap.xml` and `Allow: /1/`;
- `POST /wday/cxs/harriscomputer/1/jobs` answers **HTTP 200, `total: 232`**;
- the detail payload's own canonical `externalUrl` is
  `https://harriscomputer.wd3.myworkdayjobs.com/1/job/...`.

No more meaningful alias exists — every other slug on this tenant is a
per-brand board, not a rename of the group board. `test_filters.py` pins the
built listing / detail / public URLs so a stringly-typed regression (site
coerced to an int, or the slug "helpfully" replaced) fails loudly.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/harriscomputer/1/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label and the
  requisition id in `bulletFields`. `total` is only reliable on the offset=0
  page (offsets 100 and 200 both report `total: 0`).
- **Detail**: `GET /wday/cxs/harriscomputer/1/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow:` for 28 sites including `/1/`;
  `Disallow: /Confidential/`, `/TR/`, `/gtechna/`, `/GBS*/`, `/refreshFacet/`.
  The CXS paths for site `1` are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Sibling sites (not crawled)

robots.txt advertises 27 further sites, one per portfolio brand: `Cayenta`,
`Acceo`, `AUS`, `S_and_S`, `Smartworks`, `PG`, `HTN`, `AmazingCharts`,
`CityView`, `Affinity`, `iMDsoft`, `HarrisGovern`, `HOPEM`, `BobCAD`, `Picis`,
`QuickSilva`, `SIV`, `HHC_CONN`, `HAG`, `I2`, `Cogsdale`, `Everwin`,
`eScholar`, `SilverBlaze`, `Globys`, `Interfile`, `Altera`. These are separate
brands and are excluded per the fleet convention (main board only). They cost
nothing to skip: portfolio-company requisitions surface on the group board
anyway, with the operating company carried in `hiring_org` (e.g. "TouchBistro
Incorporated"). If the fleet later wants the healthcare BUs at full depth,
`AmazingCharts`, `iMDsoft`, `Picis`, `Altera`, `HTN` and `HHC_CONN` are the
health-relevant slugs to consider as their own tenants.

## Why the window matters here

Ordering is **sound**. With `searchText=""` the `postedOn` labels age
monotonically with offset (probed 2026-08-28):

| offset | first label |
|--------|-------------|
| 0   | Posted Today       |
| 100 | Posted 30 Days Ago |
| 200 | Posted 30+ Days Ago |

Velocity is **low — roughly 3–4 requisitions a day** (offset 100 was already
30 days old), so a 7-day window closes around offset 30 and a steady-state run
is 3–4 listing pages plus a handful of detail fetches. At 232 postings the
board is just over `EXHAUSTIVE_BOARD_MAX` (200), so the consecutive-old-pages
early stop is live — and sound. The first run needed **no `--max-pages` cap**.

The relative `postedOn` label decides the window **before** any detail request:
out-of-window ids go to `seen_old_ids.csv` unfetched. The detail's exact
`startDate` re-checks the window after the fetch.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace.

**Seeding note (2026-08-28) — the store is still empty.** The 7-day first run
scanned 100 postings, fetched 51 details and kept **0**. A zero-keep run
leaves no watermark (`compute_cutoff` falls back to a rolling 7 days forever),
so the store was immediately re-seeded with `--since 2026-06-01`: that walked
all 232 postings and fetched the 181 not already archived — and still kept
**0**. All 192 detail-fetched titles were out of scope. The board is genuinely
software/commercial (developers, sales, product, support, service desk); the
few health-adjacent titles it does carry — "Clinical Business Analyst" and
"Clinical Training & Enablement Lead" (Altera / Quadramed), "Nurse Care
Manager (RN/LPN)" and "Chronic Care Manager" (Gateway EMM), "Enterprise
Account Executive – Healthcare Data & AI" — are all dropped by the shared
classifier: health-IT / clinical-informatics and care-management titles match
no in-scope family, and bedside nursing hits the clinical veto. That verdict
belongs to `_shared/classification` and is **not** overridden here.

Consequence: with no rows, `harris_jobs.csv` and the club export are not
written, and `compute_cutoff` will keep using a rolling 7-day window until the
board posts something in scope. That is the correct behaviour for this tenant.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the group brand (Harris); the Workday legal
entity / operating company is kept raw in `hiring_org`.

### Location quirks (known, not patched)

This board's location strings are unusually messy and `parse_city` is
fleet-shared, so it is **not** forked here. Two known leaks, worth flagging to
the fleet docs rather than fixing per-tenant:

| raw location | `city` | note |
|---|---|---|
| `Ontario, Canada` | `Ontario` | the board posts province/state, not city, on many rows |
| `50 Locations` | `""` | handled — multi-location placeholder is skipped |
| `Office - Nanterre` | `Office` | **leak** — internal facility label |
| `Office - Raleigh-North Hills (Wake, NC)` | `Office` | **leak** — same |
| `Remote Pune-Baroda, India` | `Remote Pune` | **leak** — "Remote " prefix is not a bare remote marker |

The fleet-standard ISO/state-code skip is applied, so a bare `NC`/`IND`
segment can never leak into `city`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window
python test_filters.py                           # unit tests (66)
```

Outputs: `harris_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/harris.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
