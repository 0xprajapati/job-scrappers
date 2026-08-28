# alvotech scraper

Scrapes Alvotech's careers board from the **Workday CXS JSON API** at
`alvotech.wd103.myworkdayjobs.com` (tenant `alvotech`, site
`Alvotech_Careers`). Alvotech is an Icelandic fully-integrated specialty
biopharmaceutical company building and manufacturing **biosimilars** — plant
and headquarters in Reykjavik, plus a Stockholm office and an Indian
subsidiary (India home office / Bangalore).

**16 open postings on 2026-08-28** — the smallest Workday board in the fleet
and a single listing page. Supply is biomanufacturing-shaped: MSAT,
upstream/downstream operations, process engineering, commissioning &
qualification, analytical R&D, QC and manufacturing compliance, with the
occasional clinical/statistical role (e.g. "Director, Statistical
Programming"). Velocity is roughly **one requisition a day** — the 16 rows
span 17 days of `postedOn` labels.

`Alvotech_Careers` is the tenant's only Workday site (probe-verified); there
is no separate University/Referral/other-brand site to exclude.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/alvotech/Alvotech_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. Unlike IQVIA, this tenant reports `total` on deeper pages
  too (offsets 20 and 100 both answered `total: 16` with an empty
  `jobPostings[]`); latching the first non-zero value is correct either way.
- **Detail**: `GET /wday/cxs/alvotech/Alvotech_Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /Alvotech_Careers/`, `Disallow: /refreshFacet/`
  only — the CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

Barely, on this board — but the mechanism is the fleet's. At 16 postings the
whole board is **one listing page**, well under `EXHAUSTIVE_BOARD_MAX` (200),
so it is walked completely every run and the consecutive-old-pages early stop
can never engage. No `--max-pages` cap was needed for the first run.

**Ordering soundness (checked 2026-08-28):** newest-first holds. The single
page's `postedOn` labels age monotonically top to bottom — Today, Yesterday,
Yesterday, 2, 2, 4, 7, 9, 9, 9, 9, 10, 14, 16, 17 Days Ago — and `total`
(16) is a real count, not a round display cap. The deeper-offset comparison
(offsets 100/200) is not applicable: both return an empty page. The
template's early stop is therefore sound but dormant here.

The relative `postedOn` label still gates detail requests **before** they are
spent: out-of-window ids go to `seen_old_ids.csv` unfetched, so a daily run
costs one listing page plus one detail per genuinely new posting. The
detail's exact `startDate` re-checks the window after the fetch (the relative
label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

**Board quirks worth knowing:**

- Reykjavik roles are often posted **bilingually** — an Icelandic block, then
  `[English]`, with the Icelandic title first ("Sérfræðingur á viðhaldssviði
  / Maintenance Specialist", "Byrjaðu starfsferil hjá Alvotech / Entry Level
  Manufacturing Operator"). Icelandic is **not** covered by the fleet's
  FR/ES/DE stopword sniffer, so those rows are not auto-flagged; the English
  half normally carries the classification signal.
- `locationsText` is a **site name, not a city** ("Reykjavik Headquarters",
  "Stockholm Office", "India Home Office", "2 Locations"), so `city` lands as
  the site name — the most specific locality this board publishes.
- Requisition ids mix `JR1001xx` with Workday's raw
  `JOB_REQUISITION-3-32` form. Both are stable dedup keys.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Alvotech); the Workday legal
entity ("IS10 Alvotech hf", "IN10 Alvotech Biosciences India PVT Ltd") is
kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-01   # explicit window
python test_filters.py                           # unit tests (64)
```

Outputs: `alvotech_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/alvotech.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
