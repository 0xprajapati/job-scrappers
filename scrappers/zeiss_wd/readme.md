# zeiss scraper

Scrapes ZEISS's global careers board from the **Workday CXS JSON API** at
`zeissgroup.wd3.myworkdayjobs.com` (tenant **`zeissgroup`**, site
`External`). **814 open postings on 2026-08-28.** ZEISS is the German optics
and medical-technology group — ophthalmic diagnostics and surgical devices,
microscopy and microsurgery, alongside semiconductor lithography optics and
industrial metrology. Supply is engineering-heavy (development, production,
software, semiconductor manufacturing) with a medical-technology seam —
medical devices, diagnostics field service, regulatory/quality, clinical
applications — that the shared classifier picks out of a long
sales/finance/IT tail.

> **Tenant trap:** the tenant slug is `zeissgroup`, *not* `zeiss`. The
> directory name and `SITE` are `zeiss`; `TENANT` is `zeissgroup`. A
> compliance test pins both.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/zeissgroup/External/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id
  (`JR_1052576`-shaped) in `bulletFields`. `total` is only reliable on the
  offset=0 page — offsets 100 and 200 both reported `total: 0`.
- **Detail**: `GET /wday/cxs/zeissgroup/External/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, `jobReqId`, `location`, `additionalLocations`,
  the country descriptor + ISO `alpha2Code` (under
  `jobRequisitionLocation.country`), and the canonical `externalUrl`. All
  confirmed present on this tenant. Note `additionalLocations` arrives as
  **`null`**, not `[]`, on single-site postings.
- **robots.txt**: `Allow: /External/`, `Disallow: /refreshFacet/` only, plus
  a sitemap line — the CXS API paths are allowed. Checked at startup,
  honored per-URL.
- **Single site.** robots.txt advertises only `/External/`; no sibling brand
  site exists on this tenant, so `WORKDAY_SITES = ['External']` needs no
  same-brand exclusions.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness

Probed 2026-08-28 with `searchText=""`, comparing `postedOn` across offsets:

| offset | labels observed |
|---|---|
| 0 | `Posted Today` (all 20 rows) |
| 100 | `Posted 7 Days Ago` … `Posted 9 Days Ago` |
| 200 | `Posted 17 Days Ago` … `Posted 18 Days Ago` |

Labels age **monotonically** with offset, so newest-first holds and the
template's consecutive-old-pages early stop is sound on this board — it is
left enabled. `total` (814) is not a round display cap, which is the other
tell for unsound ordering.

## Why the window matters here

Velocity is roughly **13–15 requisitions a day** (offset 100 was already
7–9 days old at probe time), so the 7-day window is about the first five or
six listing pages. The relative `postedOn` label decides the window
**before** any detail request is spent: out-of-window ids go to
`seen_old_ids.csv` unfetched. The detail's exact `startDate` re-checks the
window after the fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was capped at
`--max-pages 15` (300 newest postings ≈ the last ~3 weeks of supply, well
past the 7-day window) to keep the initial burst modest.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`.

**This board is German-first.** Most postings sit at the German sites
(Oberkochen, Jena, Göttingen, Aalen, Braunschweig, Wetzlar) and are written
wholly in German, with `(m/w/x)` title suffixes. The shared FR/ES/DE
stopword sniffer catches them — verified against a real captured German
payload in `test_filters.py`, not just a hand-written sample — so they are
**kept but forced into needs_review** rather than published untranslated.
Expect a high needs_review share here compared with the English-only boards
in the fleet.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (ZEISS); the Workday legal
entity (e.g. "Carl Zeiss CMP GmbH") is kept raw in `hiring_org`.

## Location formats on this board

The dominant format is a **single bare city** (`Oberkochen`, `Jena`,
`Göttingen`, `Bangalore`, `Shanghai`, `Tokyo`) which `parse_city` passes
through whole. US sites use `Chesterfield, MO` / `Dublin, CA`; multi-site
requisitions use the `2 Locations` / `62 Locations` placeholder, which is
not treated as a city.

Known limitation: the remote format `Remote - USA AZ` yields
`city = "USA AZ"` — the segment is not a bare 2–3 letter token, so the
fleet's ISO-token skip does not catch it, and the remote fallback only
applies when no city survives. It is rare (1 of 40 sampled rows) and left
alone rather than patched per-tenant; the row is still correctly typed
`job_type = remote`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window / re-seed
python test_filters.py                            # unit tests (65)
```

Outputs: `zeiss_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/zeiss.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
