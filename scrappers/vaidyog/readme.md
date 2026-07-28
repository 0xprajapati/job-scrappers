# Vaidyog scraper

Scrapes healthcare job listings from
[healthcarejobs.vaidyog.com](https://healthcarejobs.vaidyog.com/) — Vaidyog's
public healthcare job board (India; heavy on Tamil Nadu, Karnataka and
West Bengal clinics/hospitals).

## Data source

The board is a Vite/React SPA whose landing page loads job cards from a
public, unauthenticated JSON API on the backend host:

```
GET https://jobs.vaidyog.com/api/jobs-for-u?page=N&limit=M&jobTitle=&location=&keySkills=
```

Jobs come back newest-first with the full description inline:
`_id, jobTitle, jobDescription, keySkills[], experienceLevel{min,max},
location{city,state,office}, salaryRange{min,max}, institutionName`.
Roughly ~900 jobs total; only a handful post per day, so the watermark stop
keeps daily runs to a page or two.

Neither host serves a robots.txt (404 / SPA shell), so nothing is
disallowed; the scraper still checks at startup. Per-job detail endpoints
require a login token (HTTP 401) and are **not** used — the listing already
carries everything public.

## Quirks

- **No posted-date field.** `posted_date` is derived from the creation
  timestamp embedded in the Mongo ObjectId `_id` (first 8 hex chars).
- **Salary shorthand.** `salaryRange` is always numeric but employers type
  `{0,0}` (undisclosed), thousands shorthand (`30-40` = ₹30–40k/month) and
  the occasional junk (`0-2`). Sub-1000 values are ×1000; anything still
  under ₹5,000/month keeps the raw string only. No period field: max ≥
  ₹100,000 reads per-year, else per-month. Currency is always INR.
- **Dirty state names** ("Maharastra", "Tamilnadu", "west bengal ") are
  fixed by a small alias map.
- **No public per-job URL** — Apply Now is login-gated, so `job_url` /
  `application_url` point at the public board.
- The board is healthcare-only, so there is no extra healthcare filter;
  junk/test postings ("test", "testing") are kept but flagged
  `needs_review` and logged to `needs_review.csv`.

## Usage

```bash
cd scrappers/vaidyog
../../.venv/bin/python scraper.py              # daily incremental run
../../.venv/bin/python scraper.py --max-pages 2   # quick test run
../../.venv/bin/python test_filters.py         # unit tests
```

Outputs:

- `vaidyog_jobs.csv` — rich cumulative store (dedup key: `_id`)
- `../../jobs_csv/<DD-MM-YYYY>/vaidyog.csv` — HealthCareers.club 22-column
  schema

First run keeps the last 30 days; later runs keep only jobs newer than the
newest stored `posted_date` minus 2 days of grace (dedup absorbs the
overlap).
