# halma scraper

Scrapes Halma's group careers board from the **Workday CXS JSON API** at
`halma.wd3.myworkdayjobs.com` (tenant `halma`, site `Halma`). **156 open
postings on 2026-08-28** — one of the smaller boards in this fleet. Halma plc
is a UK group of ~45 autonomous safety, health and environmental technology
companies; the medical arm is what earns it a place here — ophthalmic
diagnostics (Keeler, Volk, CenterVue), patient monitoring and blood-pressure
devices (SunTech Medical), surgical and dental equipment, and IVD /
life-science instruments (Meditech, Diba, Argus). Supply is
engineering-and-manufacturing shaped (design, production, field service,
quality) with a thin seam of regulatory affairs, QA/RA and
clinical-application roles the shared classifier picks out.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/halma/Halma/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText and a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago"). `total` is only reliable on the
  offset=0 page (offsets 100 and 140 reported `total: 0`).
- **Detail**: `GET /wday/cxs/halma/Halma/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, `jobReqId`, `location`, `additionalLocations`,
  the country descriptor + ISO `alpha2Code`, and the canonical
  `externalUrl`. All confirmed present. `additionalLocations` arrives as
  **`null`**, not `[]`, on single-site postings.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### robots.txt — read this before touching the scraper

```
User-agent: *
Disallow: /Halma/
Disallow: /refreshFacet/
```

This tenant **disallows the human-facing `/Halma/` site path** — unusual, and
the opposite of every other Workday tenant in the fleet, which emit
`Allow: /<Site>/`. The `/wday/cxs/` API paths this scraper actually fetches
are **not** disallowed, so the startup per-URL check passes legitimately.
This is the same shape as the **thermofisher** tenant, the fleet's other
instance, and it is handled the same way: no page under `/Halma/` is ever
requested; that prefix appears only inside the stored `job_url` for a human
to click. A compliance test pins that both fetched URL templates stay outside
the disallowed prefix, and `check_robots` would abort the run if Workday ever
added a `Disallow: /wday/`.

### bulletFields is four entries here

`bulletFields` on this tenant is
`[jobReqId, coarse job family, "0", subsidiary legal entity]`, e.g.
`["JR26_000961", "Engineering/R&D & Science", "0", "MEDITECH … Kft."]`.
`listing_job_id` takes `[0]`, so ids stay clean. The job-family string is the
only category-ish field any Workday tenant in this fleet exposes, but it is
far too coarse to label a row ("Engineering/R&D & Science" covers most of the
board), so `skills` is still passed **empty** to the shared classifier, as
everywhere else in the fleet.

### Pagination quirk — offsets past `total` wrap to page 0

An offset beyond the board size does **not** return an empty page: offset 200
(total 156) returned the offset-0 rows verbatim and re-reported `total: 156`.
A crawler that stops only on an empty page would loop forever here. The
template stops on `offset >= total`, which fires at offset 160 first, so the
wrap is never reached — but it is worth knowing before anyone "improves" the
stop condition.

## Tenant scope — why one site and no exclusions

`halma` is a group-wide tenant and the `Halma` site lists every subsidiary's
requisitions together. Unlike the elsevier/relx case there is **no per-brand
sibling site to prefer**: robots.txt advertises `/Halma/` and nothing else,
so the group board is the only board and the parent cannot double-count
against a subsidiary board that does not exist. `WORKDAY_SITES = ['Halma']`,
no exclusions. `company` stays the group brand (Halma); the operating company
is kept raw in `hiring_org` ("SunTech Medical, Inc.", "MEDITECH … Kft.").

## Ordering soundness

Probed 2026-08-28 with `searchText=""`, comparing `postedOn` across offsets:

| offset | labels observed |
|---|---|
| 0 | `Posted Today` ×5, `Posted Yesterday` ×8, `Posted 2 Days Ago` ×7 |
| 100 | `Posted 30+ Days Ago` (all 20) |
| 140 | `Posted 30+ Days Ago` (all 16 — last populated page) |

Labels age **monotonically** with offset, so newest-first holds, and `total`
(156) is not a round display cap. (Offset 200 was also sampled and wrapped to
page 0 — see the quirk above.) At 156 postings the board is under
`EXHAUSTIVE_BOARD_MAX` (200) anyway, so it is **walked completely** in 8
pages and the consecutive-old-pages early stop never engages; the `postedOn`
window gate on detail fetches is what keeps the run cheap.

## Why the window matters here

Velocity is roughly **6–7 requisitions a day** (page 0's 20 rows span 3 days;
offset 100 is already 30+ days old), so the 7-day window is about the first
two or three listing pages. The relative `postedOn` label decides the window
**before** any detail request is spent: out-of-window ids go to
`seen_old_ids.csv` unfetched. The detail's exact `startDate` re-checks the
window after the fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. No `--max-pages` cap is needed on a board
this small — 8 listing pages is the whole thing.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)`. `in_scope:
False` rows are archived to `out-of-scope.csv` (never the main CSV).
`needs_review` rows are kept and logged to `needs_review.csv`. Postings are
written in English throughout, including from the Hungarian and Belgian
subsidiaries, so the FR/ES/DE sniffer is quiet on this board — a test pins
that the real captured payload is *not* flagged.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank.

## Location formats on this board

Mostly a **single bare city** (`Budapest`, `Morrisville`, `Bengaluru`,
`Tucson`, `Abingdon`), which `parse_city` passes through whole. The group's
sites are often named after the subsidiary rather than the town
(`TSI Half Moon Bay CA Office`, `Fortress Wolverhampton`) — those survive
whole too, which is the CorroHealth "Noida Luminaire" convention.
Multi-site requisitions use the `2 Locations` placeholder, which is not
treated as a city. The detail's `jobRequisitionLocation.descriptor` is
`<facility>, <ISO-3>` (`Meditech Budapest, HUN`); `build_row` prefers the
plain `location` field, and the fleet's ISO-token skip keeps `HUN` out of the
city column either way.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run (walks all 8 pages)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window / re-seed
python test_filters.py                            # unit tests (68)
```

Outputs: `halma_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/halma.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
