# roche scraper

Scrapes job listings from **careers.roche.com** — Roche, global pharma.
~1,216 openings total (Aug 2026), but the index is dominated by IT / Sales
& Marketing / Vocational programs, so the crawl is **scoped to the three
refineSearch category facets** that hold the in-scope slice (~162 jobs,
~46% card-level in-scope — the R&D facet is bench/IT-heavy and the
classifier drops most of it): China-, Switzerland- and US-heavy with ~25
more countries.

**Genentech (Roche's US twin) runs a separate Phenom site that was probed
and deliberately SKIPPED — do not add its refNum here.**

## Data source

The site runs on the **Phenom People** platform (refNum ROCHGLOBAL, locale
en_global — same platform as `scrappers/fortrea`, whose widgets mechanics
this scraper clones). One master search index behind a JSON widget
endpoint — a plain unauthenticated POST, no cookies or tokens
(probe-verified 2026-08-28):

```
POST https://careers.roche.com/widgets
{"ddoKey": "refineSearch", "from": N, "size": 50,
 "selected_fields": {"category": ["Medical Affairs", "Regulatory Affairs",
                                  "Research & Development"]},
 "sort": {"order": "desc", "field": "postedDate"}, ...}
```

Facet strings verified **verbatim** from a live unfiltered aggregation
response 2026-08-28 (counts 38 / 20 / 104 of 1,216). They are re-verified
at startup from an unfiltered counts request; if any name disappears from
the live vocabulary the run **falls back to the unfiltered newest-first
crawl with a loud warning** (never a silent miss). `--unfiltered` forces
that mode manually.

Cards carry title, jobId ("202608-121963" — Workday-style, month-prefixed),
jobSeqNo, category, subCategory, type, city/state/country, ml_skills,
descriptionTeaser, postedDate, dateCreated, applyUrl. Detail pages
`/global/en/job/<jobId>` — **the jobId keeps its dash; the dashless URL is
HTTP 410** — embed `phApp.ddo.jobDetail` with the full HTML description
(~5-6k chars), fetched for jobs the classifier keeps on card evidence.

robots.txt disallows only `*/px-widgets`, apply/chatbot/tracking paths;
`/widgets` and `/global/en/job/*` are allowed (checked at startup, abort
if not).

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

* **postedDate is re-dated** (Phenom repost/re-index refresh — only 49 of
  the 162 slice cards had postedDate == dateCreated; one Tokyo Regulatory
  Affairs Specialist was "posted 2026-07-23" but created 2026-04-14; same
  trap as fortrea/shine). The watermark still runs on postedDate (the sort
  key, so the newest-first early stop stays correct) and jobId dedup
  absorbs re-dated reposts. `dateCreated` is kept in the rich CSV as the
  honest first-seen date.
* **ml_skills' bare "business development" tag is sanitized** before
  classification (substring match, like gsk). Measured 2026-08-28: 6 of
  the 162 slice cards were vetoed by the bare tag; with it stripped, 4 are
  real keeps through the full pipeline (Medical Manager Cardiac → MSL, VP
  Global Head MS & Neuroimmunology → Clinical Research, AI Product Manager
  → MSL, Head of Healthcare Transformation → HEOR) and 2 still drop on
  their own signals. Real BD jobs still drop on their titles. The raw tag
  list stays in the rich CSV's `ml_skills` column.
* **No remote marker on this board** — no `remoteType` field, no
  "Remote ..." locations, no remote placeholders anywhere in the 162-card
  slice (checked 2026-08-28). The generic Phenom signals (card `location`
  prefix, detail `remote`/`remoteType`) stay wired in case Roche starts
  publishing them.
* **Global board — non-English JDs kept + flagged.** Roche posts
  local-language JDs and even local-language ml_skills (all-Korean tag
  lists on Seoul jobs; 3 of 162 slice teasers were Korean/Japanese). Kept,
  `non_english` stamped in the rich CSV, and routed to `needs_review.csv`
  with reason `non_english`. Note the Seoul "Field Medical Partner" JD
  itself is English boilerplate — the flag fires on the actual description
  text, not the tags.
* **No structured salary field**; US descriptions state an explicit
  "expected salary range ... $X to $Y" — extracted only when explicitly
  written (annual USD; hourly figures stay raw-only). Everything else:
  `salary_raw` = "Not Disclosed", numerics blank.
* `country` is a full name — including **"China's Mainland"** (45 of 162
  slice cards) and **"Türkiye"** — ISO-mapped via `COUNTRY_META`
  (fortrea's map extended); an unmapped name exports verbatim with
  code/dial blank, never guessed.
* Single-employer board: company is always "Roche", company_type "pharma".

## Outputs

* `roche_jobs.csv` — rich cumulative store (dedup key: Phenom jobId);
  source of truth for the watermark.
* `../../jobs_csv/<DD-MM-YYYY>/roche.csv` — HealthCareers.club export,
  exactly the `CLUB_COLUMNS` imported from `_shared/classification.py`.
* `out-of-scope.csv` — dropped rows + skip list (reversible).
* `needs_review.csv` — kept but flagged (classifier flag or non-English).

Time window: first run keeps the last 7 days; later runs keep jobs newer
than the newest stored posted_date minus 2 days of grace, stopping the
newest-first pagination once a whole page predates the cutoff.

## Usage

```bash
cd scrappers/roche
../../.venv/bin/python scraper.py                 # normal incremental run
../../.venv/bin/python scraper.py --max-pages 1   # 1-page test run
../../.venv/bin/python scraper.py --no-details    # teaser-only, faster
../../.venv/bin/python scraper.py --unfiltered    # whole-index crawl
../../.venv/bin/python scraper.py --since 2026-08-01   # window override
../../.venv/bin/python test_filters.py            # unit tests
```

Politeness: descriptive UA, ≥1.5s between requests, exponential backoff on
429/5xx, 4xx (incl. 403) never retried.
