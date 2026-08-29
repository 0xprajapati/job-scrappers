# solenis scraper

Scrapes Solenis' global careers board from the **Workday CXS JSON API** at
`solenis.wd1.myworkdayjobs.com` (tenant `solenis`, site `Solenis`). **567
open postings on 2026-08-28.** Solenis is a specialty-chemicals maker for
water treatment and industrial process markets; since absorbing Diversey in
2023 its portfolio also covers professional hygiene, cleaning and
infection-prevention products sold into hospitals, food service and food
processing. Supply is chemicals-and-field-service shaped: sales reps, service
technicians, plant operators, chemical/process engineers, supply chain, with
only a thin regulatory / quality / EHS slice the shared classifier can keep.
**A low or zero keep rate on this board is the correct outcome, not a filter
fault** — no compensating filters are added here.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/solenis/Solenis/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label, a
  `remoteType` ("Remote" / "On Site") and `bulletFields`. `total` is only
  reliable on the offset=0 page (offsets 100 and 200 both reported
  `total: 0`).
- **Detail**: `GET /wday/cxs/solenis/Solenis/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, `remoteType`, `endDate`/`timeLeftToApply`, the
  country descriptor + ISO `alpha2Code`, and the canonical `externalUrl`.
  Tenant quirks: **no `additionalLocations` key** and
  **`hiringOrganization.name` is an empty string** — both degrade to blank
  columns.
- **robots.txt**: `Allow: /Solenis/`, `Disallow: /refreshFacet/` only — the
  CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## The one deviation from the fleet template: `listing_job_id`

**On this tenant `bulletFields` does not lead with the requisition id.**
Solenis sends three bullets — `[location, location, requisition id]`:

```json
["Santo Domingo, Dominican Republic",
 "Santo Domingo, Dominican Republic", "R0029512"]
```

The stock template returns `bulletFields[0]` as the `job_id`, which here is a
**city name**. Probed across 60 postings (offsets 0/100/200, 2026-08-28):
three bullets every time, **0 of 60** leading bullets requisition-shaped, and
those 60 postings carried only **39 distinct** leading bullets. Shipping the
stock version would have made a city the dedup key and silently collapsed
~35% of the board into duplicate rows — and poisoned `known_ids` so every
later posting in an already-seen city was skipped as a duplicate.

`listing_job_id` therefore takes the first bullet that is *requisition-shaped*
(a compact token containing a digit) and otherwise falls through to the
template's existing externalPath tail. The change is a **no-op on every other
board in the fleet** — `"R1564910"` (iqvia) and `"885928"` (kimberlyclark)
both match — and is pinned by four tests in `test_filters.py`. Nothing else
in the file differs from `scrappers/iqvia/scraper.py` beyond the config block
and the fleet-standard ISO-token skip in `parse_city`.

## Ordering soundness

Probed 2026-08-28 with `searchText=""`:

| offset | postedOn labels |
| --- | --- |
| 0 | all "Posted Today" / "Posted Yesterday" |
| 100 | "Posted 7 Days Ago" → "Posted 8 Days Ago" |
| 200 | "Posted 18 Days Ago" → "Posted 23 Days Ago" |

Labels age monotonically with offset and `total` (567) is not a round display
cap, so **newest-first ordering holds** and the template's early stop is
sound. It is left enabled.

## Why the window matters here

At 567 postings the board is well over `EXHAUSTIVE_BOARD_MAX`, so the early
stop does real work. The relative `postedOn` label decides the time window
**before** any detail request: out-of-window ids go to `seen_old_ids.csv`
unfetched, and the walk stops after 2 consecutive pages with nothing in the
window. Velocity is roughly **13–15 requisitions a day** (offset 100 was
already a week old at probe time), so a steady-state daily run is a couple of
listing pages plus one detail per genuinely new posting. The detail's exact
`startDate` re-checks the window after the fetch.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was capped at
`--max-pages 15` (300 newest postings ≈ the last ~3 weeks) to keep the
initial burst modest.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`. A
large share of this board's LatAm field-service postings are written wholly
in Spanish or Portuguese; those are kept but forced into review by the shared
language sniff rather than published untranslated.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Solenis); `hiring_org` is blank
on this tenant.

### Known cosmetic limitation

Location strings put the remote marker in parentheses on the *country-only*
rows ("Austria (Remote)"), so `parse_city` returns `"Austria (Remote)"` where
a cleaner result would be `"Remote"`. The row is still correctly typed
`job_type = remote`. This is stock template behaviour shared with the rest of
the fleet, not a Solenis-specific bug, so it was **not** patched here — it
belongs in a fleet-wide template change if it is worth fixing. Separately,
this tenant exposes an explicit `remoteType` field the template does not
read; `is_remote` infers the same thing from the location text instead.

## Usage

```bash
../../.venv/bin/python scraper.py                      # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15       # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5            # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window / re-seed
python test_filters.py                                 # unit tests (64)
```

Outputs: `solenis_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/solenis.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
