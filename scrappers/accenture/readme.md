# accenture scraper

Scrapes Accenture's global careers board from the **Workday CXS JSON API** at
`accenture.wd103.myworkdayjobs.com` (tenant `accenture`, site
`AccentureCareers`). The unfiltered API reports exactly **2,000 open
postings** on 2026-08-28 — probe-verified to be a **display cap**, not a
count: the real board is larger, and rows beyond 2000 are only reachable
through facets (the India country facet alone returns 658). Supply is
consulting-shaped: the vast majority is IT / consulting / finance / sales
titles the shared classifier drops, with a thin in-scope stream from
Accenture's life-sciences BPO operations (pharmacovigilance, regulatory
services, clinical data). A **high excluded_out_of_scope ratio is expected
and correct** on this board.

## Data source

- **Listing** (paged; **not** reliably ordered — see the deviation below):
  `POST /wday/cxs/accenture/AccentureCareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, a *relative* `postedOn` label ("Posted Today" …
  "Posted 30+ Days Ago") and `bulletFields` = [requisition id, city].
  **Quirk:** this tenant sends **no `locationsText`** on listing rows — the
  detail's `location` field (a bare city, e.g. "Brasov") fills the gap via
  the shared fallback. `total` is only reliable on the offset=0 page
  (deeper pages report 0).
- **Detail**: `GET /wday/cxs/accenture/AccentureCareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`
  (on `jobRequisitionLocation.country`), and the canonical `externalUrl`.
  `additionalLocations` is absent on this tenant.
- **robots.txt**: `Allow: /AccentureCareers/` (plus the sibling sites),
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- **Sibling sites not crawled**: robots.txt reveals three other boards on
  this tenant — `AccentureLeadershipCareers` (separate executive board) and
  `AvanadeCareers` / `AvanadeLeadershipCareers` (the Avanade joint-venture
  brand, not Accenture). All three are excluded: different brand/board, not
  the general Accenture careers site.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Tenant deviation: two listing walks, no early stop

Two probe-verified quirks (2026-08-28) make the fleet's standard newest-first
walk unsound on this tenant:

1. **The unfiltered view is display-capped at exactly 2,000 rows.** In-scope
   postings can hide beyond the cap — e.g. Chennai/Bengaluru
   "Pharmacovigilance Services" AIOC-* requisitions posted *today* were
   absent from the capped view's first 100 rows.
2. **Ordering is NOT newest-first**, neither unfiltered nor within a facet:
   the India facet showed offset 0 = 8 days old, offset 100 = 3 days old,
   offset 300 = 18 days, offset 640 = 30+ days — loosely aging,
   non-monotonic. Early-stopping on consecutive old pages misses fresh rows.

So `LISTING_WALKS` configures two walks that share one dedup state:

| walk | appliedFacets | why |
|---|---|---|
| `global-capped` | `{}` | everything the capped global view exposes |
| `india-facet` | `{"locationCountry": ["bc33aa3152ec42d4995f4791a106ed09"]}` | full India coverage (658 rows) — the fleet's primary sourcing target |

The **consecutive-old-pages early stop is disabled**; each walk runs to its
end (empty page or offset ≥ total) with a hard cap of `DEFAULT_MAX_PAGES =
110` pages per walk (2000-cap / 20 = 100 pages max anyway; ~133 listing
pages per full run). The relative `postedOn` label still gates **before**
any detail request: out-of-window ids go to `seen_old_ids.csv` unfetched, so
details are spent only on in-window new postings. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has
only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace — from run 2 the watermark plus the
seen-old/known-id skip lists make a full two-walk pass cheap.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV) — on this consulting-heavy board that is most rows, by design.
`needs_review` rows are kept and logged to `needs_review.csv`; non-English
(DE/FR/ES) descriptions are kept but forced into review — Accenture posts
country-office vacancies untranslated (Brazilian PT, German "(all genders)"
roles etc.).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Accenture); the Workday legal
entity (e.g. "5301 Accenture Services S.R.L Company") is kept raw in
`hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # full two-walk run (first run too)
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (61)
```

Outputs: `accenture_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/accenture.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
