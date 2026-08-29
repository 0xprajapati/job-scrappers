# gsk scraper

Scrapes job listings from **jobs.gsk.com** — GSK, global pharma. ~745
openings total (Aug 2026), but the index is dominated by Sales /
Manufacturing / Marketing, so the crawl is **scoped to the three
refineSearch category facets** that hold the in-scope slice (~131 jobs,
~90% card-level in-scope): US-heavy with UK, Japan, Korea, Belgium,
Germany and ~15 more countries.

## Data source

The site runs on the **Phenom People** platform (refNum GHVGPAGB, locale
en_gb — same platform as `scrappers/fortrea`, whose widgets mechanics this
scraper clones). One master search index behind a JSON widget endpoint — a
plain unauthenticated POST, no cookies or tokens (probe-verified
2026-08-28):

```
POST https://jobs.gsk.com/widgets
{"ddoKey": "refineSearch", "from": N, "size": 50,
 "selected_fields": {"category": ["Medical and Clinical",
                                  "Epidemiology and Health Outcomes",
                                  "Regulatory"]},
 "sort": {"order": "desc", "field": "postedDate"}, ...}
```

Facet strings verified **verbatim** from a live unfiltered aggregation
response 2026-08-28 (counts 106 / 13 / 12 of 745). They are re-verified at
startup from an unfiltered counts request; if any name disappears from the
live vocabulary the run **falls back to the unfiltered newest-first crawl
with a loud warning** (never a silent miss). `--unfiltered` forces that
mode manually.

Cards carry title, jobId, jobSeqNo, category, type, city/state/country,
`remoteType`, ml_skills, descriptionTeaser, postedDate, dateCreated,
applyUrl. Detail pages `/gb/en/job/<jobId>` embed `phApp.ddo.jobDetail`
with the **full HTML description** (~8-9k chars) — fetched for jobs the
classifier keeps on card evidence.

robots.txt disallows only `*/px-widgets`, apply/chatbot/tracking paths;
`/widgets` and `/gb/en/job/*` are allowed (checked at startup, abort if not).

## Classification

Migrated scheme — every candidate goes through
`_shared/classification.classify_job`; the scraper defines no category
logic (the facet only scopes the crawl):

* **pass 1** on card evidence (title, sanitized ml_skills, teaser) decides
  whether a job earns a detail fetch; out-of-scope cards drop to
  `out-of-scope.csv`;
* **pass 2** re-runs classify_job with the full detail description — its
  verdict is final (a flip to out-of-scope also drops the row).

## Quirks

* **postedDate is re-dated** (Phenom repost/re-index refresh — only 35 of
  the 131 slice cards had postedDate == dateCreated; one Health Economist
  card was "posted 2026-08-28" but created 2026-05-12; same trap as
  fortrea/shine). The watermark still runs on postedDate (the sort key, so
  the newest-first early stop stays correct) and jobId dedup absorbs
  re-dated reposts. `dateCreated` is kept in the rich CSV as the honest
  first-seen date.
* **ml_skills tags containing "business development" are sanitized** before
  classification (substring match — unlike fortrea's exact-tag set,
  because GSK also emits "business development support"). Measured
  2026-08-28: exactly 3 of the 131 slice cards were dropped **solely** by
  such a tag (Senior Medical Affairs Manager, Global Regulatory Strategy
  Team Lead, Global Regulatory Affairs Lead) — all admitted with the tag
  stripped; real BD jobs still drop on their own titles. The raw tag list
  stays in the rich CSV's `ml_skills` column.
* **Remote is the card `remoteType` field** — only the exact value
  "Remote" (4 of 131) counts; "Hybrid (Remote & On-site)" (71), "Field
  worker" (33) and "On-Site" keep their city. Remote rows export
  `job_type` "remote" and `city_name` "Remote" (himalayas convention).
* **Global board — non-English JDs kept + flagged.** GSK posts
  local-language JDs (2 of 131 slice teasers were Japanese; French titles
  seen in the drops). Kept, `non_english` stamped in the rich CSV, and
  routed to `needs_review.csv` with reason `non_english`.
* **No structured salary field**, but US descriptions state "The US annual
  base salary for new hires in this position ranges from $X to $Y" —
  extracted from the description only when explicitly written (annual USD;
  hourly figures stay raw-only). Everything else: `salary_raw` = "Not
  Disclosed", numerics blank.
* `country` is a full name — ISO-mapped via `COUNTRY_META` (fortrea's map
  extended with UAE, Saudi Arabia, "Türkiye", "China's Mainland",
  Pakistan, ...); an unmapped name exports verbatim with code/dial blank,
  never guessed.
* Single-employer board: company is always "GSK", company_type "pharma".
* Known classifier vocabulary gap seen on this board: titles spelled
  "Medical **Scientific** Liaison" (GSK Italy/vaccines wording) score no
  role family and land in out-of-scope.csv, while "Medical Science
  Liaison" is kept — visible and reversible there.

## Outputs

* `gsk_jobs.csv` — rich cumulative store (dedup key: Phenom jobId);
  source of truth for the watermark.
* `../../jobs_csv/<DD-MM-YYYY>/gsk.csv` — HealthCareers.club export,
  exactly the `CLUB_COLUMNS` imported from `_shared/classification.py`.
* `out-of-scope.csv` — dropped rows + skip list (reversible).
* `needs_review.csv` — kept but flagged (classifier flag or non-English).

Time window: first run keeps the last 7 days; later runs keep jobs newer
than the newest stored posted_date minus 2 days of grace, stopping the
newest-first pagination once a whole page predates the cutoff.

## Usage

```bash
cd scrappers/gsk
../../.venv/bin/python scraper.py                 # normal incremental run
../../.venv/bin/python scraper.py --max-pages 1   # 1-page test run
../../.venv/bin/python scraper.py --no-details    # teaser-only, faster
../../.venv/bin/python scraper.py --unfiltered    # whole-index crawl
../../.venv/bin/python scraper.py --since 2026-08-01   # window override
../../.venv/bin/python test_filters.py            # unit tests
```

Politeness: descriptive UA, ≥1.5s between requests, exponential backoff on
429/5xx, 4xx (incl. 403) never retried.
