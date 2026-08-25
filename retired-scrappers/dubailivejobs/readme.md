# dubailivejobs.com scraper

Scrapes healthcare job postings from [dubailivejobs.com](https://www.dubailivejobs.com)
(a UAE / Dubai WordPress jobs board) into the shared HealthCareers.club schema.

## Data source

The site is WordPress. Its custom `job_listing` type is not exposed via REST,
but the standard **posts** endpoint is, and posts are tagged with a clean
**"Healthcare" category (id 86, ~92 posts)** — hospitals, clinics, pharmacies:

    GET https://www.dubailivejobs.com/wp-json/wp/v2/posts
        ?categories=86&per_page=100&page=N&orderby=date&order=desc
        &_fields=id,date,link,title,content,excerpt

Posts come newest-first by date, so incremental runs stop at the watermark.
`robots.txt` only disallows `/wp-admin/`, so the REST API is fair game.

**Each post is a company *careers page*** (e.g. "Danat Al Emarat Hospital
Careers"), not a single job — it covers many roles. So:

- `company` is parsed from the post title (the text before "Careers"/"Jobs").
- `title` is the cleaned post title.
- club `category` is a best-effort guess from the title: an explicit
  Pharmacy → `pharmacists`, Nurse → `nurses`, Doctor/Physician/Dentist →
  `doctors`; a whole-hospital page that names no specific role maps to
  `non_clinical` and is flagged in `needs_review.csv` (most posts land here —
  that's inherent to a company-level source, not a bug).

### Salary is AED — deliberately blank in the club CSV

Per-post salary lives only in a schema.org JobPosting JSON-LD in each post's
HTML `<head>` (not in REST), and only ~1/3 of posts carry it. The amounts are
in **AED**, which the shared `salary_currency` enum (INR/USD only) can't
represent — so the club CSV's salary fields are left **blank** and no currency
is invented. Pass `--details` to fetch the JSON-LD and capture the raw AED
figure (plus employment type / expiry) in the *rich* CSV; it's **off by
default** because the value is low and it adds one HTML request per post.

## Filtering (per ../../instructions/master-scraper-spec.md)

- **Healthcare**: the WP "Healthcare" category (86) is the source-side filter.
  (Note: a few hospital posts are mis-tagged under other categories, e.g.
  HSE/HR, and are not captured — widening the category set is a future tweak.)
- **No salary filter** — salaries are captured (rich CSV, when `--details`),
  never filtered on.
- **Time window**: first run keeps the last `INITIAL_WINDOW_DAYS` (30); later
  runs keep only posts newer than the newest stored date minus
  `WATERMARK_GRACE_DAYS` (2), stopping pagination early on the first fully-old
  page.

## Usage

```bash
pip install -r requirements.txt
python test_filters.py                 # unit tests

python scraper.py                      # full run (fast, ~3s: REST only)
python scraper.py --details            # + per-post JSON-LD (raw AED salary)
python scraper.py --max-pages 1        # test run
```

Options: `--output PATH` (rich CSV, default `dubailivejobs_jobs.csv`),
`--details`, `--max-pages N`, `--run-date DD-MM-YYYY`, `--verbose`. Tunables at
the top of `scraper.py`: `HEALTHCARE_CATEGORY_ID`, `INITIAL_WINDOW_DAYS`,
`WATERMARK_GRACE_DAYS`, `REQUEST_DELAY_SECONDS`.

## Outputs

- `dubailivejobs_jobs.csv` — rich cumulative store (dedup key `post_id`), the
  watermark source of truth.
- `../../jobs_csv/<DD-MM-YYYY>/dubailivejobs.csv` — shared `job_samples.csv`
  schema. Country is UAE (`AE` / `+971`); salary fields blank (AED, see above).
- `needs_review.csv` — company-level posts that mapped to `non_clinical` by
  default, for review.

Re-running the same day adds 0 rows (idempotent).
