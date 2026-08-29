# zelis scraper

Scrapes Zelis' careers board from the **Workday CXS JSON API** at
`zelis.wd1.myworkdayjobs.com` (tenant `zelis`, site `ZelisCareers`). **98
open postings on 2026-08-28** — a small, concentrated board. Zelis is a US
healthcare-payments technology company: claims pricing, network analytics,
payment integrity and provider payment delivery for 750+ payers. Supply is
payer-tech shaped (engineering, product, security, finance, claims and
pricing operations), roughly two thirds United States — Atlanta,
St. Petersburg, Boston, Phoenix, Morristown, St. Louis and a lot of
state-level remote — and about a quarter Hyderabad, India. Only a modest
slice is in scope for the club taxonomy; the shared classifier drops the
rest.

`ZelisCareers` is the tenant's only public site (it is the slug named in
`robots.txt` and in the sitemap), so `WORKDAY_SITES` has one entry.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/zelis/ZelisCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id
  (`JR……`) in `bulletFields`. `total` is only reliable on the offset=0
  page (deeper pages report 0).
- **Detail**: `GET /wday/cxs/zelis/ZelisCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. This tenant also sends `endDate` /
  `jobPostingEndDateAsText` (unused) and `additionalLocations: null` rather
  than `[]` on single-site postings.
- **robots.txt**: `Allow: /ZelisCareers/`, `Disallow: /refreshFacet/` only
  — the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness (probe-verified 2026-08-28)

With `searchText=""` the `postedOn` labels age monotonically with offset:

| offset | 0 | 20 | 40 | 60 | 80 |
|---|---|---|---|---|---|
| label | Posted Today | Posted 7 Days Ago | Posted 16 Days Ago | Posted 30 Days Ago | Posted 30+ Days Ago |

Newest-first holds, so the template's consecutive-old-pages early stop
would be sound. It never actually fires here: at 98 postings the board is
under `EXHAUSTIVE_BOARD_MAX` (200) and is walked completely — all 5
listing pages, every run.

## Why the window matters here

Velocity is low: offset 20 was already 7 days old at probe time, so the
board turns over a handful of requisitions a week, not a day. The relative
`postedOn` label still decides the time window **before** any detail
request — out-of-window ids go to `seen_old_ids.csv` unfetched — so a daily
run costs 5 listing requests plus one detail per genuinely new posting.
The detail's exact `startDate` re-checks the window after the fetch (the
relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. `--max-pages` was **not** used on the
first run — the board is small enough to walk whole.

## Tenant quirk — space-joined location strings

Zelis writes locations with **no separator at all**, just spaces:
`US GA Atlanta`, `US FL St. Petersburg`, `US UT Remote`, `India Hyderabad`,
`India Hyderabad (Nexity)`. All 21 distinct location strings on the board
use that shape; not one contains a comma or a hyphen. The fleet splitter
(comma / hyphen / en-dash) therefore sees a single segment and would export
the country and state prefix as the city.

`parse_city` here carries the fleet-standard ISO-token skip **plus** one
tenant amendment: a *leading* run of country-alias / 2–3-letter-uppercase
tokens is peeled off each segment before it is judged. `US GA Atlanta` →
`Atlanta`, `India Hyderabad` → `Hyderabad`, `US UT Remote` → `` (and
`build_row` then stamps `Remote` by the himalayas convention). Multi-word
cities survive intact (`US FL St. Petersburg` → `St. Petersburg`), as do
campus suffixes (`Hyderabad (Nexity)`), matching the existing
"Noida Luminaire" convention. This is the same defect cencora's `>`
-hierarchy board hit, with a different separator.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review (this board is
US/India English-only in practice).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Zelis); the Workday legal
entity (e.g. "LE100 Zelis Healthcare India Private Limited", "LE018 Zelis
Healthcare, LLC") is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                     # daily incremental run
../../.venv/bin/python scraper.py --limit 5           # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01  # explicit window / backfill
python test_filters.py                                # unit tests (66)
```

Outputs: `zelis_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/zelis.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
