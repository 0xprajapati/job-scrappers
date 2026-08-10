# WorkIndia scraper

Scrapes healthcare jobs from [workindia.in](https://www.workindia.in/) —
India's largest blue/grey-collar job platform (nurses, pharmacists, lab &
ward staff, clinic/hospital support roles, medical reps).

## Data source

- **robots.txt** (`www.workindia.in/robots.txt`) welcomes all crawlers; the
  only rule is `Disallow: /*?*` / `/*&*` (no query-string URLs). The scraper
  only ever requests clean paths and hard-asserts that.
- **Discovery**: the sitemap index `crawlsitemap.workindia.in/sitemap.xml`
  links a daily `latest-jd-pages.xml` (~14k job-detail URLs, refreshed
  daily). Each URL encodes title/area/city/id:
  `/jobs/<title_slug>-<area>-<city>-<job_id>/`.
- **Job data**: every detail page embeds a full **schema.org JobPosting
  JSON-LD** — title, company, `datePosted`, `validThrough`, INR monthly
  salary range (`baseSalary`), address (locality/region/postal), employment
  type, months of experience, and a structured description
  (`Salary Range : …`, `Work Arrangement : …`, `Gender Preference : …`).

Because the job id is in the URL, already-scraped jobs are skipped **before**
any request — a daily run costs 2 sitemap fetches plus one request per new
healthcare job (typically a few dozen).

## Healthcare filter

Source-level filter on the URL title slug:

- `ALLOW_SLUG_RE` — clearly healthcare (nurse, pharmacist, lab_technician,
  medical_*, hospital_* [not hospitality], physio, dental, ward_boy, …).
- `AMBIGUOUS_SLUG_RE` — healthcare-adjacent (caretaker, *_therapist spa
  variants, health_insurance, …): scraped and kept, flagged
  `needs_review=True`, logged to `needs_review.csv`. Real example: a
  "Caretaker" posting by a fish market.
- Everything else counts as `excluded_non_healthcare`.

No salary filter — salaries are captured verbatim (`salary_raw`) plus
normalized monthly INR (`salary_min_monthly`/`salary_max_monthly`;
`unitText: YEAR` values are ÷12). Undisclosed → `"Not Disclosed"`, numeric
fields empty.

## Quirks

- **CloudFront UA filtering**: plain curl / bot User-Agents get 403 even for
  robots.txt. The scraper uses a browser UA string with
  `HealthCareersJobScraper/1.0 (+contact)` appended — transparent and
  accepted.
- The "latest" sitemap is a rolling mix of new and refreshed old postings;
  the `datePosted` window + id dedup absorb the old ones.
- Salaries are always monthly INR ranges in practice; descriptions carry the
  same figures as text.

## Usage

```bash
pip install -r requirements.txt
python test_filters.py          # unit tests, no network
python scraper.py --limit 15    # sample run
python scraper.py               # full/daily run
```

Flags: `--output` (rich CSV path), `--limit N` (max detail fetches),
`--run-date DD-MM-YYYY` (jobs_csv folder), `--verbose`.

## Outputs

- `workindia_jobs.csv` — rich cumulative store, deduped on `job_id`;
  append-only (running twice adds 0 rows).
- `../../jobs_csv/<DD-MM-YYYY>/workindia.csv` — HealthCareers.club 22-column
  schema, regenerated from the full store each run.
- `needs_review.csv` — ambiguous-slug rows from the latest run.

Time window: first run keeps the last 30 days; later runs keep jobs newer
than the stored watermark minus 2 days grace (dedup absorbs the overlap).
