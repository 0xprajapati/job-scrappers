# reliefweb — humanitarian public-health jobs from reliefweb.int

ReliefWeb (UN OCHA) is the humanitarian sector's primary job board. Its
NGO/UN listings are dense in the Public Health side of the shared taxonomy:
epidemiologists, M&E officers, community health programme managers,
nutrition specialists, disease-programme coordinators. Probed and built
2026-08-26.

## Data source: the public RSS feed, unioned across facets

```
GET https://reliefweb.int/jobs/rss.xml[?advanced-search=(<FACET>)]
```

No key, no registration, no browser impersonation. The JSON API is **not**
an option: v1 is decommissioned, v2 requires a registered appname
(arbitrary appnames answer 403).

Verified feed facts (all 2026-08-26):

* **Each feed serves exactly its latest 20 items.** No pagination
  (`?page=` is ignored), no `limit=`. The unfiltered feed turns over
  ~28 jobs/day, so on its own it spans well under a day.
* **`?advanced-search=(<code><id>)` filters the feed.** The codes are the
  /jobs river UI's own filter vocabulary (readable at
  `window.reliefweb.advancedSearch` on the /jobs page): `C`=country.id,
  `T`=theme.id, `CC`=career_categories.id, `TY`=type.id, `S`=source.id.
  A filtered feed still serves 20 items, but a low-traffic facet's 20 items
  span days-to-weeks — that is how the scraper reaches past the 20-item
  cap: it unions the unfiltered feed with several in-scope-leaning facet
  feeds (`FEEDS` in the scraper: Health, HIV/Aids, Food & Nutrition and
  WASH themes; Monitoring & Evaluation and Information Management career
  categories) and dedupes on job id.
* **An empty valid feed is normal**, for two verified reasons: a cold facet
  combination transiently serves 0 items (the Health feed served 0, then
  20 items minutes later), and a facet can genuinely have zero open
  postings (HIV/Aids showed 0 on the HTML river too). Logged, never an
  error; the rolling window (below) guarantees a warmed-up feed's backlog
  is still captured on a later run.
* **Item `description` is the complete job body** (median ~10k chars of
  HTML) prefixed with tagged metadata divs (`Country:` / `Organization:` /
  `Closing date:`), so classification never needs a detail-page fetch.
* **Item `<category>` elements are an unlabeled mix** of country,
  organization, career category, job type and theme values. They are told
  apart by matching against the river's closed vocabularies plus the
  item's `<author>` organization; the remainder is countries (multi-country
  duty stations are common). Verified exact against the description's
  tagged country divs on 40/40 feed items.

robots.txt allows /jobs and /jobs/rss.xml (only /search/, /admin/ etc. are
disallowed); checked at startup regardless. Plain descriptive User-Agent,
1 s between requests, 4 retries with exponential backoff on 429/5xx, 4xx
permanent. A full run is exactly `len(FEEDS)` (currently 7) requests.

## Fetch wide, filter tight

The facet feeds are **recall only** — a way to surface more candidates than
one 20-item feed can carry. No facet ever decides scope or category: every
item from every feed goes through the shared
`_shared/classification.classify_job(title, skills, description)`, with the
site's career-category + theme tags as the weighted `skills` signal
(title x5 / skills x2 / description x1 — a tag alone cannot admit a job)
and the HTML-stripped body as `description`. `in_scope == False` rows are
dropped and counted `excluded_out_of_scope`; `needs_review == True` rows
are kept AND flagged into `needs_review.csv`. The raw facet values stay in
the rich CSV (`career_category` / `themes` / `job_type_raw`), and
`found_in_feeds` records which feeds served each admitted job so facet
recall stays auditable.

First live run: 109 unioned candidates → 13 in scope (~12%), all Public
Health (M&E, WASH/community programme management, epidemiology-adjacent
research).

## Time window: rolling, NOT a stored-max watermark

A deliberate deviation from the master spec's watermark, documented at
`WINDOW_DAYS` in the scraper. The watermark exists to early-stop deep
pagination; this source has none, and job_id dedup already prevents
re-adds — so a watermark could only *lose* jobs: a facet feed that served
0 items this run reaches back weeks once warm, and a stored-max watermark
would refuse everything it then serves. Instead every run keeps jobs
posted in the last `WINDOW_DAYS` (30) — the window's only job is keeping
ancient stale postings out (low-volume facet feeds still carry items years
old; theme-nutrition served a 2023 item). `--since YYYY-MM-DD` overrides
the window for a deeper backfill.

## What the feed does NOT have

Salary (club salary columns always empty — never invented), city (duty
stations are country-level; `city_name` stays empty), experience bounds,
company logo. `qualification` is `extract_qualification(description)`.
The club `job_type` enum cannot express Consultancy/Internship, so
everything exports as `full_time` with the source value kept in the rich
CSV (`job_type_raw`). ReliefWeb's formal UN country names are normalized
("Viet Nam" → "Vietnam", "occupied Palestinian territory" → "Palestine")
before the ISO/dial-code lookup; an unknown name exports verbatim with
empty codes.

## Outputs

| File | Purpose |
|---|---|
| `reliefweb_jobs.csv` | Rich cumulative store, dedup key `job_id`. |
| `../../jobs_csv/<DD-MM-YYYY>/reliefweb.csv` | The 23-column `CLUB_COLUMNS` export, regenerated from the full store every run. |
| `needs_review.csv` | In-scope rows the classifier flagged (append + dedupe). |
| `out-of-scope.csv` | Rows demoted by `--reclassify` — moved, never discarded. |

## Running

```bash
../../../.venv/bin/python reliefweb_scraper.py            # daily incremental
../../../.venv/bin/python reliefweb_scraper.py --since 2026-07-01   # deeper backfill
../../../.venv/bin/python reliefweb_scraper.py --reclassify  # re-score stored rows, no network
python test_filters.py                                    # 28 unit tests
```
