# fortrea scraper

Scrapes job listings from **careers.fortrea.com** — Fortrea, the global CRO
spun off from Labcorp (2023). ~368 openings (Aug 2026), US-heavy with China,
India, Brazil, UK, Poland, Korea and ~15 more countries. A CRO board, so it
is dense in exactly this project's taxonomy: CRA / clinical operations,
clinical data management, pharmacovigilance, regulatory, TMF, biometrics —
measured ~80% of the 100 newest cards pass the shared classifier.

## Data source

The site runs on the **Phenom People** platform (same as `scrappers/swaasa`,
whose widgets mechanics this scraper clones). One master search index behind
a JSON widget endpoint — a plain unauthenticated POST, no cookies or tokens
(probe-verified 2026-08-27):

```
POST https://careers.fortrea.com/widgets
{"ddoKey": "refineSearch", "from": N, "size": 50,
 "sort": {"order": "desc", "field": "postedDate"}, ...}
```

Cards carry title, jobId (= Workday requisition number), jobSeqNo, category,
type, city/state/country, `location`, ml_skills, descriptionTeaser,
postedDate, dateCreated, applyUrl. Detail pages `/us/en/job/<jobId>` embed
`phApp.ddo.jobDetail` with the **full HTML description** (~5-6k chars) and a
`remote` field — fetched for jobs the classifier keeps on card evidence.

robots.txt disallows only `*/px-widgets`, apply/chatbot/tracking paths;
`/widgets` and `/us/en/job/*` are allowed (checked at startup, abort if not).

## Classification

Migrated scheme — every candidate goes through
`_shared/classification.classify_job`; the scraper defines no category logic:

* **pass 1** on card evidence (title, sanitized ml_skills, teaser) decides
  whether a job earns a detail fetch; out-of-scope cards drop to
  `out-of-scope.csv`;
* **pass 2** re-runs classify_job with the full detail description — its
  verdict is final (a flip to out-of-scope also drops the row).

## Quirks

* **postedDate is re-dated** (Phenom repost/re-index refresh — only 31 of
  the 100 newest cards had postedDate == dateCreated; same trap as shine).
  The watermark still runs on postedDate (it is the sort key, so the
  newest-first early stop stays correct) and jobId dedup absorbs re-dated
  reposts. `dateCreated` is kept in the rich CSV as the honest first-seen
  date.
* **Remote roles carry an HQ placeholder city** (Durham, NC). The real
  signal is the card's `location` == "Remote United States" / the detail's
  `remote` == "Remote". Remote rows export `job_type` "remote" and
  `city_name` "Remote" (himalayas convention).
* **ml_skills' bare "business development" tag is sanitized** before
  classification: Phenom auto-tags JDs that merely liaise with BD, and the
  tag alone vetoed 4 in-scope roles among the 100 newest cards (clinical PM,
  RWE/HEOR principal, medical director). Real BD jobs still drop on their
  titles. The raw tag list stays in the rich CSV's `ml_skills` column
  (per-board sanitize idiom — devnetjobs does the same for its
  funding-sector tag).
* **No structured salary field**, but US descriptions often state
  "Pay Range: $90,000-$120,000 USD" — extracted from the description only
  when explicitly written (annual USD; hourly figures stay raw-only).
  Everything else: `salary_raw` = "Not Disclosed", numerics blank.
* `country` is a full name ("United States of America", "Korea, Republic
  of") — ISO-mapped via `COUNTRY_META`; an unmapped name exports verbatim
  with code/dial blank, never guessed.
* Single-employer board: company is always "Fortrea", company_type "pharma"
  (CRO — fleet convention for the club's hospital|pharma enum).

## Outputs

* `fortrea_jobs.csv` — rich cumulative store (dedup key: Phenom jobId);
  source of truth for the watermark.
* `../../jobs_csv/<DD-MM-YYYY>/fortrea.csv` — HealthCareers.club export,
  exactly the `CLUB_COLUMNS` imported from `_shared/classification.py`.
* `out-of-scope.csv` — dropped rows + skip list (reversible).
* `needs_review.csv` — kept but flagged.

Time window: first run keeps the last 7 days; later runs keep jobs newer
than the newest stored posted_date minus 2 days of grace, stopping the
newest-first pagination once a whole page predates the cutoff.

## Usage

```bash
cd scrappers/fortrea
../../.venv/bin/python scraper.py                 # normal incremental run
../../.venv/bin/python scraper.py --max-pages 1   # 1-page test run
../../.venv/bin/python scraper.py --no-details    # teaser-only, faster
../../.venv/bin/python scraper.py --since 2026-08-01   # window override
../../.venv/bin/python test_filters.py            # unit tests
```

Politeness: descriptive UA, ≥1.5s between requests, exponential backoff on
429/5xx, 4xx (incl. 403) never retried.
