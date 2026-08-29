# trinitylifesciences scraper

Scrapes Trinity Life Sciences' careers board from the **Workday CXS JSON API**
at `trinitylifesciences.wd108.myworkdayjobs.com` (tenant `trinitylifesciences`,
site `Trinity`). **76 open postings on 2026-08-28.** Trinity is a life-sciences
strategy consultancy — commercial strategy, HEOR, market access, medical
affairs and real-world evidence for pharma and biotech clients — with offices
in the US (Waltham HQ, New York, San Francisco), the UK (London) and India
(Bangalore, Gurgaon, Chennai).

Supply is consultancy-shaped, so the in-scope share is thinner than a CRO's:
the HEOR / market access / medical-affairs consulting roles land inside the
taxonomy, while the strategy, analytics, engineering, IT and corporate-services
tail does not. The 2026-06-01 backfill kept **6 of 69** detail-fetched
postings (~9%).

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/trinitylifesciences/Trinity/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (probed: `limit=100` → HTTP 400). Each
  posting carries title, externalPath, locationsText, a *relative* `postedOn`
  label ("Posted Today" … "Posted 30+ Days Ago") and the requisition id
  (`JR1006xx`) in `bulletFields`. Unlike IQVIA, this tenant repeats the same
  `total` (76) on every page, including past-the-end offsets that return an
  empty `jobPostings[]` — the crawler latches the first non-zero value, so
  either behaviour is safe.
- **Detail**: `GET /wday/cxs/trinitylifesciences/Trinity/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and the
  canonical `externalUrl`. Single-location postings omit `additionalLocations`
  entirely (IQVIA sends a list) — read defensively, as the template already is.
- **robots.txt**: `Allow: /Trinity/`, `Disallow: /refreshFacet/` only — the CXS
  API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness

Probed 2026-08-28 with `searchText=""`. The board is only 76 postings, so
offsets 100/200 are past the end (0 postings); the check was run at the
board's real depth instead — offsets 0, 20, 40, 60:

| offset | postedOn labels |
| ------ | --------------- |
| 0  | Posted Yesterday → Posted 22 Days Ago |
| 20 | Posted 22 Days Ago → Posted 30+ Days Ago |
| 40 | all Posted 30+ Days Ago |
| 60 | all Posted 30+ Days Ago |

Labels age **monotonically** with offset, so **newest-first holds** and the
template's consecutive-old-pages early stop is sound. `total` (76) is a real
count, not a round display cap. In practice the early stop never fires here:
76 ≤ `EXHAUSTIVE_BOARD_MAX` (200), so all 4 listing pages are walked every run.

## Why the window matters here

Posting velocity is **low** — on 2026-08-28 only 3 postings were newer than 4
days and just 6 were inside 7 days; everything from offset 26 onward was
already "Posted 30+ Days Ago". The relative `postedOn` label decides the window
**before** any detail request, so a daily run costs 4 listing pages plus one
detail per genuinely new posting, and normally adds nothing.

First run keeps the last 7 days; later runs use the newest stored `posted_date`
minus 2 days grace. **The 7-day first run kept 0 rows** (the 6 in-window
postings were all out-of-scope corporate roles), which would have left the
store with no watermark — `compute_cutoff` would fall back to a rolling 7 days
forever and the older live inventory would be permanently unreachable. It was
therefore immediately re-seeded with `--since 2026-06-01`, which fetched 69
details and kept 6. The watermark (2026-08-07) takes over from run 2.

No `--max-pages` cap was used: 4 pages is already the whole board.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review. The board is
English-only in practice — 0 rows flagged on the backfill.

All 6 kept rows classify as **Non Clinical** (HEOR ×4, MSL ×1, Clinical
Research ×1). Salary is never shown on this board → `salary_raw =
"Not Disclosed"`, club salary columns blank. `company` is the brand (Trinity
Life Sciences); the Workday legal entity ("Trinity Partners, LLC") is kept raw
in `hiring_org`.

## Location formats on this board

Four shapes, all covered by `parse_city` + the fleet-standard ISO-token skip:
`San Francisco, CA` → San Francisco · `India - Gurgaon` → Gurgaon ·
`London - Lime Street` → London · `Waltham, MA - Headquarters` → Waltham ·
`5 Locations` (placeholder) → "".

## Usage

```bash
../../.venv/bin/python scraper.py                      # daily incremental run
../../.venv/bin/python scraper.py --limit 5            # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # the backfill this board was seeded with
python test_filters.py                                 # unit tests (63)
```

Outputs: `trinitylifesciences_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/trinitylifesciences.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
