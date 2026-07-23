# publichealthcareer.org scraper

Scrapes job listings from [publichealthcareer.org](https://publichealthcareer.org/jobs-list/)
(an India-focused public-health job board: NGO / research / program roles at
orgs like Wadhwani AI, PHFI, MAHAN Trust) into the shared HealthCareers.club
schema.

## Data source

WordPress (pxp job-board theme). A custom `job` post type is exposed through
the standard REST API — the primary source, no HTML parsing for core fields:

    GET https://publichealthcareer.org/wp-json/wp/v2/job
        ?per_page=100&page=N&orderby=date&order=desc
        &_fields=id,date,link,title,content,job_location,job_category,job_type,job_level

Jobs come newest-first (watermark early-stop works). The four taxonomy fields
hold term IDs, batch-resolved via `/wp-json/wp/v2/{taxonomy}?include=…`
(job_location → "Delhi", job_type → "Full Time", job_level → "Entry-Level").
The whole board is ~15 jobs, so a full run takes under a minute.
`robots.txt` only disallows `/wp-admin/`.

**Company & salary are not in REST.** Each job page has a labeled sidebar
(Experience / Employment Type / Salary "INR 20,000 per month" / Website) plus
a hiringOrganization name; the scraper fetches each NEW job's page (on by
default, `--no-details` to skip) and extracts those. Note: the page's
JobPosting JSON-LD is *malformed* (missing commas — a site plugin bug), so
extraction uses the sidebar markup and targeted regexes instead of a JSON
parse.

## Filtering & mapping (per ../../instructions/master-scraper-spec.md)

- **Healthcare**: the whole board is public-health domain, so all jobs are
  kept. Most roles are program/research/faculty → club category
  `non_clinical` (that's their real nature, not a review flag); explicit
  Nurse/Pharmacist/Doctor titles map to their enums.
- **No salary filter** — salaries captured, never filtered. INR/USD are valid
  club currencies, so club salary fields are populated when the sidebar
  states an amount; anything else stays blank (nothing invented).
- **company_type**: NGOs/institutes have no fitting value in the
  hospital|pharma enum → `hospital` default, `pharma` for lab/biotech names.
- **Time window**: first run keeps the last `INITIAL_WINDOW_DAYS` (30); later
  runs keep only jobs newer than the newest stored date minus
  `WATERMARK_GRACE_DAYS` (2).

## Usage

```bash
pip install -r requirements.txt
python test_filters.py            # unit tests (7)

python scraper.py                 # full run (REST + per-new-job detail pages)
python scraper.py --no-details    # REST only (no company/salary), faster
python scraper.py --max-pages 1   # test run
```

Options: `--output PATH` (rich CSV, default `publichealthcareer_jobs.csv`),
`--run-date DD-MM-YYYY`, `--verbose`.

## Outputs

- `publichealthcareer_jobs.csv` — rich cumulative store (dedup key: WP post
  id), watermark source of truth.
- `../../jobs_csv/<DD-MM-YYYY>/publichealthcareer.csv` — shared
  `job_samples.csv` schema (country India / IN / +91).

Re-running the same day adds 0 rows (idempotent).
