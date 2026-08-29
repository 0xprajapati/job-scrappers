# netsmart scraper

Scrapes Netsmart's careers board from the **Workday CXS JSON API** at
`ntst.wd1.myworkdayjobs.com` (tenant `ntst`, site `Careers`). **57 open
postings on 2026-08-28.** Netsmart is a US healthcare IT company — for 50+
years an EHR / care-coordination software vendor for behavioral health,
human services and post-acute providers.

Note the **tenant slug `ntst` does not match the brand**: the scraper
directory, `SITE` and every CSV are named `netsmart`.

Supply is software-company shaped — engineering, architecture, QA, product,
sales and admin, split between Overland Park, KS (39) and Bengaluru, India
(14). Only the clinically-adjacent tail (clinical consulting, coding/OASIS
review, pharmacy solution delivery) is even a candidate for this fleet, and
on the probe date the shared classifier kept **none** of the 57 — see
"First run" below.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/ntst/Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0).
- **Detail**: `GET /wday/cxs/ntst/Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description (3–6 kB stripped), the
  exact posted date (`startDate`), `timeType`, the country descriptor + ISO
  `alpha2Code`, and the canonical `externalUrl`. `hiringOrganization` is
  "Netsmart Technologies, Inc.".
- **robots.txt**: `Allow: /Careers/`, `Allow: /Other/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- **Site slugs**: robots.txt advertises a second slug, `Other`. It answers
  HTTP 200 with `total: 0` and an empty `jobPostings[]`, so it is
  **excluded** — `WORKDAY_SITES = ['Careers']`.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness

Probe-verified on 2026-08-28 with `searchText=""`: `postedOn` labels age
monotonically with offset —

| offset | label |
|-------:|-------|
| 0  | Posted Yesterday |
| 20 | Posted 8 Days Ago |
| 40 | Posted 8 Days Ago |
| 55 | Posted 8 Days Ago |

Newest-first holds, and `total` (57) is not a round display cap. It makes no
practical difference here: at 57 postings the board is under
`EXHAUSTIVE_BOARD_MAX` (200), so it is **walked completely** every run — 3
listing pages — and the consecutive-old-pages early stop never engages. The
`postedOn` window gate still runs first, so out-of-window postings cost no
detail request.

## Why the window matters here

Velocity is low and bursty: at probe time 16 of 57 postings were inside the
7-day window (3 "Yesterday", 4 two days, 7 three days, 2 seven days) and the
other 41 were all "Posted 8 Days Ago" — one batch of requisitions a week
rather than a steady drip. A full walk is only 3 listing pages, so the cost
of a daily run is one detail fetch per genuinely new posting.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. No `--max-pages` cap is needed on a board
this small.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review (none seen on this
board — it is all-English).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Netsmart); the Workday legal
entity is kept raw in `hiring_org`.

## First run (2026-08-28)

Two runs, because the 7-day window kept nothing and an empty store has no
watermark to advance from:

| run | scanned | fetched | out-of-scope | out-of-window | kept |
|-----|--------:|--------:|-------------:|--------------:|-----:|
| default (7-day window, cutoff 2026-08-21) | 57 | 16 | 16 | 41 | 0 |
| `--since 2026-06-01` backfill | 57 | 41 | 41 | 0 | 0 |

Between them the **whole 57-posting board was fetched and classified**, and
every row was dropped with an empty `matched_in` — i.e. a real "no in-scope
signal" verdict, not a parse fault (descriptions are 2.8–6.1 kB, cities and
countries parse correctly). This board is genuinely out of scope today: it
is a software company hiring engineers. `netsmart_jobs.csv` and the club
export are therefore **not created** until a run keeps its first row.

Known quirks recorded from that sweep:

- Netsmart marks distributed roles with the primary location string
  `Remote - Other`. `parse_city` drops the "Remote" segment and returns
  **"Other"** as the city on those rows (4 of 57). The row is still flagged
  `job_type: remote`. Fleet-wide `parse_city` behaviour — not patched here.
- `Coding & OASIS Reviewer (PRN)` (ICD-10 coding + OASIS review, explicitly
  "certified clinical documentation professional") was dropped as a no-match
  — not vetoed, simply unmatched. A plausible Medical Coding false negative
  for the shared taxonomy to consider; this scraper adds no filters of its
  own.

## Usage

```bash
../../.venv/bin/python scraper.py                      # daily incremental run
../../.venv/bin/python scraper.py --limit 5            # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window / backfill
python test_filters.py                                 # unit tests (64)
```

Outputs: `netsmart_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/netsmart.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
