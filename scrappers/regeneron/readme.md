# regeneron scraper

Scrapes Regeneron Pharmaceuticals' global careers board from the **Workday
CXS JSON API** at `regeneron.wd1.myworkdayjobs.com` (tenant `regeneron`,
site `Careers`). **578 open postings on 2026-08-28.** Regeneron is a US
biotechnology company (Dupixent, Eylea) with R&D in Tarrytown NY,
manufacturing in Rensselaer NY and Limerick, Ireland, and a large captive
site in Hyderabad, India. Supply is biotech-shaped: R&D and preclinical
science, GMP manufacturing / QC / QA, clinical development, biostatistics
and statistical programming, regulatory, medical affairs, plus a long tail
of IT/finance/commercial the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/regeneron/Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (probe-verified: `limit: 50` → HTTP 400).
  Each posting carries title, externalPath, locationsText, `timeType`, a
  *relative* `postedOn` label ("Posted Today" … "Posted 30+ Days Ago") and
  the requisition id in `bulletFields`. Unlike IQVIA, this tenant repeats
  `total` on every page rather than reporting 0 on deep offsets — the code
  latches the first non-zero value either way, so this needs no special
  handling.
- **Detail**: `GET /wday/cxs/regeneron/Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` is `null` (not `[]`) on
  single-site requisitions.
- **robots.txt**: `Allow: /Careers/`, `Allow: /IrelandPrivate/`,
  `Allow: /US_Only_University_Relations_Career/`, `Disallow: /refreshFacet/`
  only — the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Workday sites: why only `Careers`

`robots.txt` advertises three sites on this tenant; all three were probed:

| site | total | verdict |
| --- | --- | --- |
| `Careers` | 578 | **included** — the public global board |
| `IrelandPrivate` | 3 | excluded — a restricted Limerick-only private board (its own private-referral channel, not the public brand board) |
| `US_Only_University_Relations_Career` | 0 | excluded — university-relations/intern site, and empty at probe time |

## Ordering soundness (probe-verified 2026-08-28)

With `searchText=""` the `postedOn` labels **age monotonically with offset**,
so newest-first ordering holds:

| offset | observed labels |
| --- | --- |
| 0 | Posted Today ×7, Posted Yesterday ×8, Posted 2 Days Ago ×5 |
| 100 | Posted 10 Days Ago ×18, Posted 11 Days Ago ×2 |
| 200 | Posted 17 Days Ago ×7, Posted 18 Days Ago ×8, Posted 21 Days Ago ×5 |

`total` (578) is not a suspiciously round display cap either, so the
template's **consecutive-old-pages early stop is sound on this board** and is
left enabled as shipped.

## Why the window matters here

Those offsets put posting velocity at roughly **10 requisitions a day**, so a
7-day window is about the first 4–5 listing pages. The relative `postedOn`
label decides the time window **before** any detail request: out-of-window
ids go to `seen_old_ids.csv` unfetched, and the newest-first walk stops after
2 consecutive pages with nothing in the window. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has only
day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was capped
(`--max-pages 15`, i.e. the 300 newest postings — far more than the window
needs) to keep the initial burst modest; in practice the early stop fired
first. The watermark takes over from run 2.

## Location formats and their quirks

This board does not use one consistent location string:

- bare city — `Hyderabad`, `Limerick`, `Dublin`, `Munich`, `Tokyo`
- `City, ST` — `Houston, TX`, `Brooklyn, NY`
- remote — `Remote - United States`, `Remote - Germany` → `city = "Remote"`
- `N Locations` placeholder → detail resolves a real primary `location`
- **US campus codes in caps** — `RENSS - TEMPEL LN`, `RENSS - TECH VALLEY`,
  `RENSS - GLOBAL VIEW`, `RENSSELAER`, `SLEEPY HOLLOW`, `TARRYTOWN`
- a few cities carry a Workday location-code digit — `Amsterdam3`,
  `Uxbridge1`

`parse_city` carries the fleet-standard skip for bare 2–3 letter uppercase
ISO/state tokens (so `Houston, TX` can never yield `TX`). The hyphenated
campus codes are longer than that and pass through as their first segment
(`RENSS - TECH VALLEY` → `RENSS`) — a **known quirk left as-is**, since the
fleet template forbids per-tenant city rewriting. `country` and
`country_code` come from the detail's ISO `alpha2Code` and are always
correct regardless.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review. In
practice this board posts in English throughout, including its German and
Japanese vacancies.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Regeneron); the Workday legal
entity (e.g. "Regeneron India Private Limited", "Regeneron Healthcare
Solutions, Inc (USA)") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20    # explicit window
python test_filters.py                            # unit tests (63)
```

Outputs: `regeneron_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/regeneron.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
