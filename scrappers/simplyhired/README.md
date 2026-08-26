# simplyhired scraper

Scrapes healthcare jobs (India) from
[simplyhired.co.in](https://www.simplyhired.co.in) — SimplyHired's Indian
site (Indeed family).

Source listing URL (agreed filters):
`https://www.simplyhired.co.in/search?q=healthcare&l=india&s=d&t=15`
→ location **India**, **newest-first**, posted within the **last 15 days**
(`t` accepts arbitrary day counts server-side). One search per keyword in
`SEARCH_QUERIES` (broad `healthcare` plus the Non Clinical role families
and, since 2026-08-25, terms covering all ten Public Health
sub-categories).

## Data source

Next.js app: every SERP page embeds the full result JSON in
`<script id="__NEXT_DATA__">` → `props.pageProps.jobs` (20 jobs/page,
`resultCount`, `pageCursors`). No separate API is needed — the scraper
parses the HTML it is allowed to crawl.

| Request | Purpose |
|---|---|
| `GET /search?q=healthcare&l=india&s=d&t=15[&cursor=…]` | Listing page |
| `GET /job/<jobKey>` | Detail page (only with `--enrich`) |

Listing fields: `jobKey`, `title`, `company`, `location`
(`"[Area, ]City, State"` or bare state), `salaryInfo`
(`"₹18,000 - ₹25,000 a month"`, `"From ₹18,00,000 a year"`, or null),
`dateOnIndeed` (epoch **milliseconds**), `jobTypes`, `remoteAttributes`,
`snippet` (truncated description), `sponsored`, `botUrl`.

Detail pageProps add: `jobDescriptionHtml`, `formattedLocation`,
`compensation`, `workSettings`, `datePublished`, `qualifications`,
`benefits`, `employerSquareLogoUrl`.

## Quirks

- **Sponsored interleaving**: with `s=d` only *organic* results are sorted
  newest-first; sponsored cards (sometimes months old) are mixed in on every
  page. Old jobs are excluded per-row, and the watermark early-stop only
  counts organic results — otherwise one stale sponsored card would never
  let the crawl stop (or stop it too early).
- **SERP depth cap** (Indeed family): the cursor chain ends around page
  ~73 (~1,400 scanned cards / ~1,000 unique jobs) regardless of
  `resultCount`, so one query cannot backfill a whole 15-day window
  (`resultCount` claimed 4.4k, the crawl surfaced the newest ~1k). Daily
  incremental runs are unaffected — a day's new jobs (~300–400) fit well
  under the cap, so coverage is complete from the first daily run onward.
- **Pagination is cursor-based**: each response's `pageCursors` maps the
  next few page numbers to opaque tokens; page N+1 = `&cursor=<token>`.
  There are no stable page URLs beyond page 1.
- **Bot protection** (Indeed family): plain `requests`/`curl` TLS
  fingerprints are rejected — `curl_cffi` with `impersonate="chrome"` is
  required, same as the naukrigulf scraper. Contact info goes in the `From`
  header.
- **robots.txt**: `/search` and `/job/<key>` are allowed for `User-agent: *`;
  `/serp`, `/job-id/`, `/a/job-details/`, `/c/jobs-api/` and the `/out?r=`
  apply-redirect are disallowed and never requested. `application_url` is
  therefore the public `/job/<jobKey>` page, not the tracking redirect.
- Salaries are INR with explicit periods, so unlike the Gulf boards they
  **are exported** to the club CSV (hour/day periods stay rich-CSV-only —
  the club enum has no slot for them).
- Without `--enrich`, `description` falls back to the listing `snippet`
  (~1–2 sentences, ends with "…").

## Usage

```bash
pip install -r requirements.txt

# sample run: 2 pages with detail enrichment
python simplyhired_scraper.py --max-pages 2 --enrich

# full daily run, full descriptions (1 req/s → ~1 s per new job)
python simplyhired_scraper.py --enrich

# fast listing-only run (snippet descriptions)
python simplyhired_scraper.py
```

Flags: `--output`, `--max-pages N` / `--limit N` (test runs), `--enrich`,
`--run-date DD-MM-YYYY`, `--verbose`.

Tests: `python test_filters.py`

## Outputs

| File | What |
|---|---|
| `simplyhired_jobs.csv` | Rich cumulative store, dedup key `job_id` (= `jobKey`), watermark source |
| `../../jobs_csv/<DD-MM-YYYY>/simplyhired.csv` | HealthCareers.club 23-column schema |
| `needs_review.csv` | Kept but flagged (title reads like a different profession) |
| `out-of-scope.csv` | Rows the taxonomy dropped; append-only, deduped on `job_id`, so a scope change is reversible without a re-crawl |

## Category / enum mapping

- **`category` / `sub_category`**: the shared two-level taxonomy
  (`../_shared/classification.py`) — `Non Clinical` | `Public Health` plus
  one of the 20 sub-categories. SimplyHired has **no role facet at all**:
  the 31 keyword walks are a *recall* device, and `classify_job` is the
  entire precision layer. Everything it rules out — bedside jobs, medical
  billing, wellness, back office — is dropped, counted as
  `excluded_out_of_scope` and moved to `out-of-scope.csv`. The site's own
  `qualifications` bullets are passed to it as the `skills` signal and kept
  in the rich CSV as a raw source column.
  Reclassifying the store on 2026-08-26 kept **111 of 2,011** rows (5.5%):
  Non Clinical 59 / Public Health 52 — top sub-categories Medical Coding 47,
  Public Health Nutrition 46, Infection Prevention & Control 5, MSL 4.
- `company_type`: pharma/labs/diagnostics keywords in company name →
  `pharma`, else `hospital`.
- `job_type`: `Full-time` beats `Part-time` when both are listed;
  remote/hybrid from `remoteAttributes`/`workSettings`.

## Etiquette

robots.txt checked at startup; ≥1s delay between requests; exponential
backoff (3s → 24s) on 429/5xx; 4xx fail fast; one bad job/page never
crashes the run.
