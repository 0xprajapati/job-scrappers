# illumina scraper

Scrapes Illumina's global careers board from the **Workday CXS JSON API** at
`illumina.wd1.myworkdayjobs.com` (tenant `illumina`, site `illumina-careers`).
**152 open postings on 2026-08-28** — a small board, walked end to end on
every run. Illumina builds DNA sequencing platforms (NovaSeq, MiSeq), so
supply is R&D- and manufacturing-shaped: bioinformatics, process and hardware
engineering, lab operations, software, plus a modest in-scope tail (clinical
affairs, regulatory, quality, scientific roles). The shared classifier drops
most of the engineering, commercial and G&A titles.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/illumina/illumina-careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (e.g. `43166-JOB`). `total` is only reliable on the
  offset=0 page (deeper pages report 0 — confirmed at offsets 60/100/140).
- **Detail**: `GET /wday/cxs/illumina/illumina-careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /illumina-careers/`,
  `Allow: /illumina-universityrecruiting/`,
  `Allow: /illumina-earlycareers-europe/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Sibling sites (not crawled)

The tenant exposes two more Workday sites, both listed in robots.txt:
`illumina-universityrecruiting` and `illumina-earlycareers-europe`. Probed on
2026-08-28, both answered **HTTP 200 with `total: 0` and no postings** — they
are separate student/early-careers programmes rather than part of the main
brand board, so `WORKDAY_SITES` holds `illumina-careers` alone.

## Why the window matters here

Ordering is **sound**. With `searchText=""` the `postedOn` labels age
monotonically across the whole board (probed 2026-08-28):

| offset | first label | last label |
|--------|-------------|------------|
| 0   | Posted Yesterday    | Posted 7 Days Ago   |
| 20  | Posted 7 Days Ago   | Posted 11 Days Ago  |
| 40  | Posted 11 Days Ago  | Posted 17 Days Ago  |
| 60  | Posted 17 Days Ago  | Posted 23 Days Ago  |
| 80  | Posted 23 Days Ago  | Posted 30+ Days Ago |
| 100–140 | Posted 30+ Days Ago | Posted 30+ Days Ago |

`total` (152) is not a round display cap. The template's consecutive-old-pages
early stop is therefore sound, though it never actually fires on this board:
152 ≤ `EXHAUSTIVE_BOARD_MAX` (200), so all 8 listing pages are walked every run
regardless.

What keeps the run cheap is the **posted-date gate on detail fetches**: the
relative `postedOn` label decides the window *before* any detail request is
spent, so out-of-window ids go to `seen_old_ids.csv` unfetched. Velocity is
low — the 20 newest postings spanned 7 days at probe time, roughly 3
requisitions a day — so a steady-state daily run costs 8 listing pages plus
one detail per genuinely new posting. The detail's exact `startDate`
re-checks the window after the fetch (the relative label has only day
granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The board is small enough that the first
run needed **no `--max-pages` cap**.

## Board quirks

- Locations read `"<country or alias> - <state/district> - <city or campus>"`:
  `US - California - San Diego`, `Singapore - Woodlands - NorthTech`,
  `India - Bengaluru - Manyata`. For non-US rows the second segment is the
  city, which `parse_city` returns correctly. For **US rows the state name is
  spelled out in full**, so `parse_city` returns `California` rather than
  `San Diego` — full state names are not ISO tokens, so the fleet-standard
  ISO-code skip (`[A-Z]{2,3}`) does not catch them. Pinned by
  `test_illumina_us_rows_yield_the_spelled_out_state` so a future fleet-wide
  fix shows up as a visible test change.
- **Paging past the end wraps instead of ending.** Offset 140 returns the last
  12 real postings; offsets 160/180/200+ re-serve the offset-0 page verbatim
  (same ids, `total` reappearing as 152) rather than an empty `jobPostings`.
  Fetching offsets 0–280 yielded 292 rows that collapse to exactly **152
  unique ids**. The `offset >= total` break is what actually ends this crawl —
  the "empty page" break never fires — with `DEFAULT_MAX_PAGES` (40) as the
  backstop should `total` ever go missing. Anything a wrap re-serves is a
  known id and is counted as a duplicate, so it cannot corrupt the store.
- `additionalLocations` is sometimes JSON `null` rather than a list; multi-site
  postings show `locationsText: "2 Locations"` (never treated as a city).
- `jobRequisitionLocation.descriptor` can name a different site from
  `location` (a San Diego posting carried `US - Arizona - Remote`); only the
  ISO `alpha2Code` is read from that block, as in the rest of the fleet.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review. The
board is English-only in practice (US/Singapore/India/EU sites all post in
English).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Illumina); the Workday legal
entity (e.g. "US01 Illumina, Inc.", "IN01 Illumina India") is kept raw in
`hiring_org`.

### Expect a low yield

Illumina is an instrument and consumables maker, not a CRO or a pharma
sponsor. On 2026-08-28 the first run scanned all 152 postings, detail-fetched
the 22 inside the 7-day window and the shared classifier dropped **all 22** —
strategic finance, field marketing, compute/software/process engineering,
manufacturing supervision, SAP, internal audit, supply chain. A listing-only
sweep of all 152 titles found **zero** in-scope on title alone. Supply is
software/hardware/manufacturing/commercial with an R&D-bench science tail
(bioinformatics, biochemistry, lab specialists) that the taxonomy treats as
out of scope. This board is worth keeping for the occasional clinical-affairs,
regulatory or quality requisition, but a run that adds no rows is the normal
case here, not a fault.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (62)
```

Outputs: `illumina_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/illumina.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
