# hcahealthcare scraper

Scrapes HCA Healthcare's careers board from the **Workday CXS JSON API** at
`wd3.myworkdaysite.com` (tenant `hcahealthcare`, site `hcacareers`).
**115 open postings on 2026-08-28.**

> ### ⚠️ Host variant: `myworkdaysite.com`, not `myworkdayjobs.com`
> Unlike every other Workday scraper in this fleet, this tenant is **not** on
> `{tenant}.wdN.myworkdayjobs.com`. It is on the shared-host
> `wd3.myworkdaysite.com` variant, where the tenant lives in the *path*
> rather than the hostname. Two things follow, both probe-verified 2026-08-28:
>
> | | this tenant | usual fleet shape |
> |---|---|---|
> | CXS listing | `POST wd3.myworkdaysite.com/wday/cxs/hcahealthcare/hcacareers/jobs` | `POST {tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` |
> | public job page | `wd3.myworkdaysite.com/**recruiting/hcahealthcare**/hcacareers/job/…` | `{host}/{site}/job/…` |
>
> The template's default `HOST/{site}/job/…` answers **HTTP 500** on this
> host, so `PUBLIC_JOB_URL` carries the extra `/recruiting/<tenant>/`
> segment. `jobPostingInfo.externalUrl` confirms that shape and is what the
> scraper actually stores; the template is only the fallback. A live GET of
> the stored `job_url` returns 200.

The board is HCA Healthcare **UK** — the London private-hospital group
(The Harley Street Clinic, The Wellington, The Princess Grace, The Portland,
London Bridge, HCA UK at The Shard, The Christie Private Care, HCA
Laboratories, Roodlane Medical, The Harborne, The Lister) plus **Sarah
Cannon Research Institute UK**, the oncology-trials arm that puts this
tenant in scope at all. Supply is overwhelmingly bedside/clinical — staff
nurses, resident doctors, theatre practitioners, radiographers,
physiotherapists — which the shared classifier correctly drops. In-scope
yield is low by construction and that is the right behaviour; no
tenant-local filters were added.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/hcahealthcare/hcacareers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0).
  **Quirk:** an offset *past the end* wraps around to page 0 instead of
  answering empty — offset 200 on this 115-posting board returned exactly
  the offset-0 page, `total` and all. Harmless here: the crawl stops at
  `offset >= total` and never reaches it.
- **Detail**: `GET /wday/cxs/hcahealthcare/hcacareers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `wd3.myworkdaysite.com/robots.txt` answers **HTTP 422**
  with a JSON error body — no robots file is served, so nothing is
  disallowed. Checked at startup and honored per-URL, so a later-added
  `Disallow` would stop the scraper rather than be ignored.
- **Workday sites**: only `hcacareers` was supplied and probe-verified
  (HTTP 200, 115 postings). No alternate/other-brand site slugs were
  offered or found, so `WORKDAY_SITES = ['hcacareers']`.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Ordering soundness

Checked with `searchText=""` across the board (2026-08-28). Labels age
**monotonically** with offset:

| offset | `postedOn` labels |
|---|---|
| 0 | Posted Yesterday → Posted 3 Days Ago |
| 40 | Posted 9 Days Ago → Posted 16 Days Ago |
| 100 | Posted 30+ Days Ago (all 15) |

Newest-first therefore **holds**, and `total = 115` is not a suspiciously
round display cap, so the template's consecutive-old-pages early stop is
sound and was left enabled unchanged. In practice it never fires: 115 is
below `EXHAUSTIVE_BOARD_MAX` (200), so the board is walked to its end —
six listing pages — on every run.

## Why the window matters here

Velocity is slow, roughly **3–6 requisitions a day** (offset 40 was already
9–16 days old at probe time). The relative `postedOn` label still decides
the window **before** any detail request is spent: out-of-window ids go to
`seen_old_ids.csv` unfetched. On the first run that meant 115 postings
scanned but only 30 detail fetches. The detail's exact `startDate`
re-checks the window afterwards (the relative label has only day
granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. **No `--max-pages` cap was needed** — six
listing pages is the whole board.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions would be kept but forced into review —
this board is entirely English, so none occurred.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (HCA Healthcare); the Workday
legal entity ("HCA Healthcare UK") is kept raw in `hiring_org`.

**Location quirk:** HCA UK names the *hospital site*, not the town —
`location` is "The Princess Grace Hospital", "HCA UK at The Shard",
"Sarah Cannon Research Institute". Those survive whole into `city` (the
CorroHealth "Noida Luminaire" convention). The board also posts the bare
token **"LOC"** (London Oncology Centre) as a whole location string; the
fleet-standard ISO/state-code skip in `parse_city` keeps that out of the
city column. Country is uniformly `GB` / United Kingdom.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run (whole board, 6 pages)
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (62)
```

Outputs: `hcahealthcare_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/hcahealthcare.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.

First run (2026-08-28): 115 scanned, 30 details fetched, **1 kept**
(SCRI "Director of Research" → Non Clinical / Clinical Research), 29
dropped out of scope, 85 out of window, 0 needs_review, 0 detail failures.
