# philips scraper

Scrapes Philips' global careers board from the **Workday CXS JSON API** at
`philips.wd3.myworkdayjobs.com` (tenant `philips`, site `jobs-and-careers`).
~830 open postings on 2026-08-28. Philips is a health-technology company
(imaging, patient monitoring, image-guided therapy, sleep & respiratory
care, personal health); supply is medtech-shaped: clinical specialists,
clinical affairs/development scientists, field service, quality/regulatory,
plus a long tail of sales/software/marketing the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/philips/jobs-and-careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0). Quirk (probe-verified 2026-08-28): offset 0 can open
  with a "ghost" stub posting carrying only `bulletFields` — no title,
  externalPath or postedOn. Its detail fetch fails harmlessly and is
  counted `detail_failed`; the row is never stored.
- **Detail**: `GET /wday/cxs/philips/jobs-and-careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` and
  `hiringOrganization.name` are populated only on some requisitions
  (multi-location rows say "3 Locations" in locationsText and list the rest
  in `additionalLocations`; the org name is a cost-centre string like
  "0050 US00-REMOTE-US9A", not a legal entity) — a missing one degrades to
  a blank field.
- **Location quirk**: US remote requisitions carry
  "United States of America - Remote Based", which the shared `parse_city`
  reduces to the city `Remote Based` (`is_remote` / `job_type=remote` are
  still correct). Left as the fleet template has it — reported for the
  fleet docs rather than patched per-tenant.
- **Sites**: only `jobs-and-careers` is crawled. The tenant's other site,
  `Internal-Job-Postings-List-for-Philips-Contingent-Workers`, is an
  internal board and is explicitly disallowed by robots.txt — excluded.
- **robots.txt**: `Allow: /jobs-and-careers/`,
  `Disallow: /Internal-Job-Postings-List-for-Philips-Contingent-Workers/`,
  `Disallow: /refreshFacet/` — the public CXS API paths are allowed.
  Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Why the window matters here

Ordering soundness (probed 2026-08-28 with `searchText:""`): offset 0 was
"Posted Today"/"Posted Yesterday", offset 100 was "Posted 3 Days Ago",
offset 200 was "Posted 9/10 Days Ago" — labels age monotonically with
offset, so **newest-first holds** and the template's early-stop is sound.

The board posts roughly **25–35 requisitions a day**, so detail-fetching
the whole board would cost ~830 requests. Instead the relative `postedOn`
label decides the time window **before** any detail request: out-of-window
ids go to `seen_old_ids.csv` unfetched, and the newest-first walk stops
after 2 consecutive pages with nothing in the window. In steady state a
daily run is a handful of listing pages plus one detail per genuinely new
posting. The detail's exact `startDate` re-checks the window after the
fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was deliberately capped
(`--max-pages 15`, i.e. 300 newest postings ≈ the last ~9–10 days) to keep
the initial burst modest — the watermark takes over from there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Philips posts country-office vacancies untranslated ("(m/w/d)" roles,
Japanese sales titles etc.).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Philips); `hiring_org` keeps
whatever raw string the tenant publishes (often blank, sometimes a
cost-centre code).

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-21   # explicit window
python test_filters.py                            # unit tests (63)
```

Outputs: `philips_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/philips.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
