# Impactpool scraper

Scrapes impact-sector jobs from [impactpool.org](https://www.impactpool.org) —
the UN/NGO/multilateral aggregator (UNICEF, WHO, World Bank, CHAI, UNOPS, …).
Strong Public Health supply: health specialists, epidemiologists, nutrition &
WASH officers, M&E, health financing. Built 2026-08-26.

## Data source

Server-rendered HTML only — `/jobs/<id>.json` and `/search.json` answer 406,
there is no JobPosting JSON-LD, and the sitemap holds only articles.

- **Listing**: `GET /search?per_page=100&page=N` with a stock UA. The
  unfiltered search lists the **entire live board** (~3,700 jobs / 37 pages
  on 2026-08-26), ordered by job id **descending ≈ newest first** (page 1
  mixes in a few promoted older cards; pages 2+ are strictly descending).
  Cards carry title, organization, location and grade in color-coded divs.
- **Detail**: `GET /jobs/<id>` — deadline line
  (`Application deadline: August 31, 2026 (5 days)`), an Impactpool-written
  summary + Candidate Requirements card (sometimes empty), and the full JD
  in `<div class='main-content'>` (extracted by `<div>` depth balancing).
- **robots.txt** allows everything (checked at startup anyway).

## Quirks

- **No posted date anywhere** (cards only get a "New" badge, details only a
  deadline). `posted_date`/`posted_at` therefore stay **empty** — never
  invented — and incrementality keys on the **descending job id**, not a
  date watermark: the listing crawl stops at the first page with zero
  unseen ids.
- Every unseen id's detail page is fetched **exactly once, ever**: in-scope
  rows go to the rich store, out-of-scope rows go to `out-of-scope.csv`
  (full rows — the skip list, and reversible). Detail failures are not
  recorded so they retry next run. First run ≈ 37 list pages + ~3.7k detail
  fetches (~1.5 h at the 1 s delay); steady state is 1–2 list pages plus
  the day's new postings.
- **Location** is a `|`-separated mix of cities and countries
  (`Islamabad | Pakistan`, `Remote | Nigeria | Kenya`, plain `Pretoria`).
  Country tokens are matched against the country table (with sector
  shorthand aliases: CAR, DRC, oPt, …); city-only cards get their country
  from the `DUTY_STATION_COUNTRIES` capital/UN-hub table or stay blank —
  never guessed.
- No salary, no experience fields; grade strings (`P-4, International
  Professional …`) stay verbatim in the rich CSV. The club `job_type` maps
  remote-flagged duty stations to `remote`, everything else to `full_time`
  (the enum can't express Consultancy/Internship).
- Some JDs are non-English (UNV posts in Spanish/French); the keyword
  classifier mostly drops those.

## Classification

Fetch wide, filter tight: the crawl takes the whole board and
`_shared/classification.classify_job` alone decides scope. No curated
role/sector field exists, so `skills` is empty; the classifier scores the
JD text plus Impactpool's own summary card. Measured on a mixed 136-title
sample (2026-08-26): ~12% in scope.

## Usage

```bash
../../.venv/bin/python impactpool_scraper.py               # incremental run
../../.venv/bin/python impactpool_scraper.py --max-pages 2 --limit 20   # sample
../../.venv/bin/python impactpool_scraper.py --reclassify  # re-score stored rows, no network
```

## Outputs

- `impactpool_jobs.csv` — rich cumulative store (dedup key: `job_id`)
- `out-of-scope.csv` — dropped rows (skip list + review), full detail
- `needs_review.csv` — flagged in-scope titles
- `../../jobs_csv/<DD-MM-YYYY>/impactpool.csv` — HealthCareers.club schema
  (CLUB_COLUMNS imported from `_shared/classification.py`)
