# lonza scraper

Scrapes Lonza's global careers board from the **Workday CXS JSON API** at
`lonza.wd3.myworkdayjobs.com` (tenant `lonza`, site `Lonza_Careers`). **683
open postings on 2026-08-28.** Lonza is the Swiss CDMO — contract development
and manufacturing for pharma and biotech, plus capsules and life-science
ingredients — so supply is plant-shaped: production operators, QC/QA analysts,
process and automation engineers, maintenance technicians, supply chain. The
in-scope slice (regulatory affairs, clinical research, medical writing,
pharmacovigilance) is a **small minority of the board, and that low keep rate
is correct** — the shared classifier decides, and this scraper adds no filter
of its own to inflate it.

Note that GMP **plant** quality — "Quality Assurance Manager", "Analyst, QC
(Microbiology)", "Senior QA Specialist, QA Analytics" — is *not* in the shared
taxonomy's clinical/regulatory scope and is correctly dropped. The first
capped run kept **0 of 112** in-window postings for exactly this reason; a
positive control confirmed the classifier still keeps Regulatory Affairs,
Clinical Research and Medical Writer titles, so this is a supply outcome, not
broken wiring.

The board *does* carry in-scope roles, just not in any 7-day slice: the
`--since 2026-06-01` backfill kept **9 of 571** fetched (1.6%), **all
Regulatory Affairs** — eight in IN - Hyderabad (Lonza's regulatory hub,
including a "Veeva RIM Specialist") and one in JP - Tokyo. No Clinical
Research, PV, medical writing or data management surfaced anywhere in the
live board. Expect this scraper to contribute a trickle — a row every few
days at best — and to be almost entirely Regulatory Affairs out of Hyderabad.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/lonza/Lonza_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0 — re-confirmed on this tenant at offsets 100 and 200).
- **Detail**: `GET /wday/cxs/lonza/Lonza_Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` is `null` on this
  tenant rather than an empty list (handled).
- **robots.txt**: `Allow: /Lonza_Careers/`, `Disallow: /refreshFacet/` only,
  plus a sitemap line — the CXS API paths are allowed. Checked at startup,
  honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness (probe-verified 2026-08-28)

With `searchText=""` the `postedOn` labels age **monotonically** with offset:

| offset | labels observed |
|--------|-----------------|
| 0   | Posted Today → Posted Yesterday |
| 100 | Posted 7 Days Ago → Posted 8 Days Ago |
| 200 | Posted 15 Days Ago → Posted 16 Days Ago |

Newest-first ordering **holds**, and `total` (683) is not a round display
cap. The template's consecutive-old-pages early stop is therefore sound on
this tenant and is kept unchanged.

## Why the window matters here

Those offsets imply roughly **13–14 requisitions a day**, so the 7-day window
closes around offset ~100 (page 5). The relative `postedOn` label decides the
window **before** any detail request is spent: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the walk stops after 2 consecutive pages
with nothing in the window. The detail's exact `startDate` re-checks the
window after the fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace.

**The first run here was a standing-inventory backfill, not a 7-day slice.**
A capped `--max-pages 15` run kept **0 of 112** in-window postings, which
left the store empty — and an empty store has no watermark, so
`compute_cutoff` would fall back to the same 7-day window on every
subsequent run. The board's in-scope roles were all 8–16 days old, so that
standing inventory would never have been reachable. The fleet remedy is a
one-off backfill:

```bash
../../.venv/bin/python scraper.py --since 2026-06-01 --max-pages 40
```

June 1 covers essentially the whole live board (683 postings at ~13/day is
~50 days of supply). It walked all 683, fetched 571 details (113 already
known out-of-scope were skipped) and kept **9**. The watermark now starts
from real inventory: the next run's cutoff is **2026-08-18**.

## Location format quirk

This board prefixes **every** location with an ISO-style code: `IN -
Hyderabad`, `CN - Nansha`, `SG - Tuas, Singapore`.
`parse_city` skips bare all-uppercase 2–3 letter segments (the fleet-standard
amendment) so the city, not the code, reaches `city_name` — otherwise
"IND-Bengaluru" would export as "IND" and "USA - WI - Mequon" as "WI".

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review — Lonza's
Swiss and German sites post many vacancies untranslated ("(m/w/d)",
"80-100%").

**Known gap (shared, not fixed here):** `looks_non_english` is a FR/ES/DE
stopword sniffer, so it does **not** catch CJK. Lonza's JP and CN sites post
Japanese- and Chinese-language descriptions (a probed Tokyo "Regulatory
Affairs Specialist" has a wholly Japanese description) and those rows are
kept *without* the review flag. This is **live in the stored data**: the
Tokyo row in `lonza_jobs.csv` has a Japanese description and
`needs_review = False`. Fixing that belongs in `_shared/classification`,
not in this scraper.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Lonza); the Workday legal
entity (e.g. "IN06 Lonza India Systems Private Ltd") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21   # explicit window
python test_filters.py                            # unit tests (61)
```

Outputs: `lonza_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/lonza.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
