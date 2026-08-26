# shine.com role-scoped job scraper (`shine_roles`)

Scrapes the in-scope role families from [shine.com](https://www.shine.com)
(HT Media's India job board) per `instructions/master-scraper-spec.md`.
The `shine` sibling crawls healthcare broadly; this variant asks the site
for the specific roles instead of crawling all of healthcare and
discarding ~85% of it.

## Data source

shine.com is a Next.js site. Every `/job-search/<query>-jobs` page is
server-rendered with the full 20-result payload embedded in
`<script id="__NEXT_DATA__">` at
`props.pageProps.initialState.jsrp.searchresult.data.results` — title,
company, salary string, full description HTML, locations, posted/expiry
datetimes, experience range, industry, keywords and the detail-page slug.
No detail-page fetches and no headless browser are needed.

- **robots.txt**: `/api/*` is disallowed, so the JSON API is never called
  (master spec §1). `/job-search/` pages are allowed and are all we fetch;
  the scraper re-checks robots.txt per-URL at startup. A descriptive
  User-Agent gets the same payload as a browser UA.
- **URL scheme**: page 1 is `/job-search/<query>-jobs?sort=1`, page N is
  `/job-search/<query>-jobs-<N>?sort=1`. `sort=1` = newest-first (the
  default is relevance, which surfaces months-old jobs on page 1).
  `ind=13` selects the "Medical / Healthcare" industry facet and works as
  a URL param (~21k jobs vs ~26k for the bare keyword search; the param
  is `ind`, **not** the facet field name `jIndID`, which is ignored).
- **Queries** (`SEARCH_QUERIES`, 55 slugs): four industry-facet browses
  first — `jobs?ind=13` Medical / Healthcare, `ind=63` Pharma / Biotech,
  `ind=31` NGO / Social Work, `ind=61` KPO / Analytics — which catch
  garbage-titled jobs no keyword query can find and let the classifier's
  skills/description rescue do the reading. `ind=20` (BPO / Call Center,
  19k rows of telecalling) is deliberately **not** browsed: the
  medical-coding keyword queries already search across all industries.
  Then one keyword slug per role family plus the synonyms each family is
  advertised under (`clinical-research`, `pharmacovigilance`,
  `regulatory-affairs`, `medical-coding`, `heor`, `trial-master-file`, …)
  and the Public Health slug set. Every PH slug was probed live on
  2026-08-24 and only those with real PH density on page 1 were kept —
  shine's multi-word matching is erratic (`health-program` → 69,829
  OR-noise results, `monitoring-and-evaluation` → 46,585 rows of IT
  monitoring, `community-health` → 5,684 with zero in-scope on page 1).
  Finally three broad catch-all nets — `healthcare`, `hospital`, `medical`
  — inherited from the `shine` sibling on 2026-08-26. They cover the one
  gap the role slugs leave: an in-scope job whose card says only
  "hospital"/"medical" and matches no family slug. Probed live 2026-08-26
  over pages 1-2: `medical` 19/40 in scope, `healthcare` 11/40, `hospital`
  2/40 (weakest — mostly bedside roles the classifier vetoes). The keeps
  skew heavily to Medical Coding, which the family slugs also reach, so
  dedup absorbs most of the overlap.
  Dedup by job id makes query overlap free. Override with
  `--queries a,b,c` (a query may carry extra params after `?`).

## Quirks (verified live)

- **shine re-dates reposts.** With `sort=1`, page 30 of the healthcare
  query was still entirely dated "today" — 600+ jobs share the same posted
  date. The date watermark therefore cannot bound crawl depth on its own;
  `--max-pages` (default 50/query) caps each query and the run summary
  prints a NOTE when a query was cut off by the cap instead of the date
  window. Dedup absorbs the re-served reposts across runs.
- Newest-first ordering is only approximate (promoted cards interleave),
  so early-stop triggers only when an **entire page** is older than the
  cutoff.
- Every record carries its industry name in `jInd` (cards returned by the
  `ind=13` browse all carry `jInd = "Medical / Healthcare"`, verified).
  Since the taxonomy migration this is a **raw source column only** — it is
  stored in the rich CSV's `industry` field and decides nothing.
- Salary strings: `Rs 4.0 - 4.5 Lakh/Yr`, `< Rs 50,000 - 2.5 Lakh/Yr`
  (mixed absolute + lakh in one string) or `[Salary Hidden]` (majority).
  Comma-numbers are absolute rupees; bare numbers < 1000 are lakhs.
- `jLoc` may be `["All India"]` — exported as city "All India" rather than
  inventing a city.

## Classification (taxonomy migration 2026-08-25)

The only keep/drop and labeling decision is the shared
`_shared/classification.py`:

```python
classify_job(jJT, jKwd, strip_html(jJD))
```

- `title` = `jJT`, `skills` = `jKwd`, `description` = the HTML-stripped
  `jJD`. All three matter: title-only matching drops roughly a third of
  genuine hits, because Indian CRO listings often carry a generic title
  ("Senior Executive") and name the domain only in the keyword tags.
- `in_scope == False` → dropped and counted `excluded_out_of_scope`.
- In-scope rows get `category` (`Non Clinical` | `Public Health`) and
  `sub_category`. **The role family moved out of `category` into its own
  `role_family` column** in this migration; `all_families`,
  `family_scores`, `family_confidence` and `matched_in` still carry the
  score trace so any admission can be audited.
- `needs_review == True` → kept **and** appended to `needs_review.csv`.

The club CSV's 23 columns come from `CLUB_COLUMNS` in
`_shared/classification.py` — `role_family` sits after `sub_category`
there since 2026-08-26, so the family survives into the club export as
well as the rich store (`is_active`/`expires_at` retired; `qualification`
is a grounded extraction from the description only).

## Outputs

- `shine_roles_jobs.csv` — rich cumulative store, dedup key `job_id`;
  watermark source of truth.
- `needs_review.csv` — in-scope rows whose title looks like a different
  profession (kept, flagged).
- `out-of-scope.csv` — rows the classifier rejected during the one-off
  migration of the stored data; nothing is silently discarded.
- `../../jobs_csv/<DD-MM-YYYY>/shine_roles.csv` — HealthCareers.club
  schema plus `role_family` (23 columns), rewritten every run.

## Usage

```bash
# daily incremental run (first run keeps the last 2 days)
../../.venv/bin/python shine_scraper.py

# test run
../../.venv/bin/python shine_scraper.py --max-pages 2 --limit 40

# recover a window (e.g. after downtime)
../../.venv/bin/python shine_scraper.py --since 2026-08-01

# unit tests (salary/experience parsers, classifier wiring, cutoff)
../../.venv/bin/python test_filters.py
```

Etiquette: ≥1.2s between requests, 3 retries with exponential backoff on
429/5xx, per-card failure isolation, page cap + explicit truncation NOTE.
