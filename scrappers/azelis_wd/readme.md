# azelis scraper

Scrapes Azelis' global careers board from the **Workday CXS JSON API** at
`azelis.wd3.myworkdayjobs.com` (tenant `azelis`, site `Azelis_Careers`).
**64 open postings on 2026-08-28** across ~25 countries in Europe, APAC, the
Americas and Africa. Azelis is a Belgian specialty chemicals and food-ingredients
distributor with a Life Sciences division (pharma excipients and APIs, personal
care, food & health, animal nutrition) alongside its Industrial Chemicals arm.

**Expected yield is very low, by design.** Azelis is a *distributor*, not a
pharma or CRO employer: the board is overwhelmingly sales, business development,
technical/lab support, supply chain, HR and finance. The 2026-06-01 backfill
kept **1 of 64** postings (a Product Stewardship & Regulatory Affairs Officer in
Vietnam) — the remaining 63 are archived in `out-of-scope.csv` with empty
`matched_in`, i.e. genuine "no in-scope signal" verdicts from the shared
classifier rather than a parse fault. This scraper earns its place by catching
the occasional regulatory-affairs or product-safety opening, not by volume.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/azelis/Azelis_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (probed: `limit=100` → HTTP 400). Each
  posting carries title, externalPath, locationsText, a *relative* `postedOn`
  label ("Posted Today" … "Posted 30+ Days Ago") and the requisition id
  (`R7xxx`) in `bulletFields`. This tenant repeats the same `total` (64) on
  every page.
- **Quirk — past-the-end offsets wrap.** Offsets 64, 100 and 200 do **not**
  return an empty page; they re-serve page 0 verbatim. The crawl is unaffected
  because the `offset >= total` break fires first, and the `job_id` dedup would
  absorb any wrapped rows regardless — but a future tenant that reports no
  `total` **and** wraps like this would page until `DEFAULT_MAX_PAGES` on
  duplicates. Worth knowing before the empty-page break is ever relied on alone.
- **Detail**: `GET /wday/cxs/azelis/Azelis_Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and the
  canonical `externalUrl`. Single-location postings omit `additionalLocations`
  entirely. The `jobRequisitionLocation` descriptor sometimes names a
  *different* site than the posting `location` (the Bresso posting reports
  "Lodi - Via Einstein") — the posting `location` is what the row uses.
- **robots.txt**: `Allow: /Azelis_Careers/`, `Disallow: /refreshFacet/` only —
  the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent; ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness

Probed 2026-08-28 with `searchText=""`. Offsets 100 and 200 are past the end of
this 64-posting board and wrap to page 0 (see the quirk above), so the check was
run at the board's real depth — offsets 0, 20, 40, 60:

| offset | postedOn labels |
| ------ | --------------- |
| 0  | Posted Today → Posted 4 Days Ago |
| 20 | Posted 4 Days Ago → Posted 17 Days Ago |
| 40 | Posted 17 Days Ago → Posted 30+ Days Ago |
| 60 | all Posted 30+ Days Ago (4 rows) |

Labels age **monotonically** with offset, and the four pages return **64
distinct requisition ids with zero overlap** — paging is honored inside the
board and **newest-first holds**. `total` (64) is a real count, not a round
display cap. The template's consecutive-old-pages early stop is therefore
sound; in practice it never fires, since 64 ≤ `EXHAUSTIVE_BOARD_MAX` (200)
means all 4 listing pages are walked every run.

## Why the window matters here

Velocity is moderate for a board this size — 24 of 64 postings were inside 7
days on 2026-08-28. The relative `postedOn` label decides the window **before**
any detail request, so a daily run costs 4 listing pages plus one detail per
genuinely new posting.

First run keeps the last 7 days; later runs use the newest stored `posted_date`
minus 2 days grace. **The 7-day first run kept 0 rows** (all 24 in-window
postings were out-of-scope commercial roles), which would have left the store
with no watermark — `compute_cutoff` would fall back to a rolling 7 days
forever and the older live inventory would be permanently unreachable. It was
therefore immediately re-seeded with `--since 2026-06-01`, which fetched the
remaining 40 details and kept 1. The watermark (2026-07-22) takes over from
run 2.

No `--max-pages` cap was used: 4 pages is already the whole board.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review. 0 rows were flagged
on the backfill — the board's postings are English even in non-English
countries.

The single kept row classifies as **Non Clinical / Regulatory Affairs**
(`matched_in: title|description`). Salary is never shown on this board →
`salary_raw = "Not Disclosed"`, club salary columns blank. `company` is the
brand (Azelis); the Workday legal entity (e.g. "Azelis Italia S.r.l.",
"MKVN Chemicals Co. Ltd") is kept raw in `hiring_org`.

## Location formats on this board

The most varied in the fleet, and the one where the ISO-token skip earns its
keep — this board suffixes **ISO-3** country codes:
`Bresso, ITA` → Bresso · `Shah Alam , MYS` → Shah Alam · `Poznan, POL` → Poznan ·
`Lagos – Tunde Gafar Close` (EN DASH) → Lagos · `Warwick, RI - USA` → Warwick ·
`Remote-Arizona` → Arizona (and `is_remote` true) ·
`Sydney - Old Pittwater Road (Azelis Australia Pty Ltd)` → Sydney ·
`2 Locations` (placeholder) → "".

## Usage

```bash
../../.venv/bin/python scraper.py                      # daily incremental run
../../.venv/bin/python scraper.py --limit 5            # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # the backfill this board was seeded with
python test_filters.py                                 # unit tests (66)
```

Outputs: `azelis_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/azelis.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
