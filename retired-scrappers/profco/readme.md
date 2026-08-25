# profco scraper

Scrapes healthcare vacancies from **profco.com** (Professional Connections),
an international healthcare recruitment agency placing nurses, midwives,
allied-health staff and consultant doctors into hospitals in **Saudi Arabia,
Australia and the UK**.

## Data source

No JSON API, no sitemap, no robots.txt — but the site's own front-end drives
three form-encoded AJAX endpoints that return small HTML fragments, and the
scraper uses exactly those (master spec §1):

| Call | Purpose |
| --- | --- |
| `POST /ajax.php?task=getJobSearchResult` with `freetext`, `country_id`, `jobcat_id`, `jobspeciality_id` | `<ul class="joblist">` of `index.php?job_id=N` links (job id + title) |
| `POST /ajax.php` `task=getJobSpecialitySelect&jobcat_id=N` | the specialities that exist under a category |
| `POST /ajax.php` `task=getContent&job_id=N` | the job detail fragment (3 KB, vs 40 KB for the full page) |

Detail fragments are a `<th>`/`<td>` table with a fixed field set:
`Category`, `Speciality`, `Location`, `Salary min`, `Salary max`, `Hospital`,
`Description`, `Benefits`, `Requirements`, plus an `<h1>#<id> Title</h1>`.

`robots.txt` returns 404 (nothing disallowed). It is still fetched at startup
and honoured per-URL if the site ever adds one.

## Quirks & deviations from the master spec

* **The search caps every result set at 50 rows.** An unfiltered search
  returns 50 of the ~200 live jobs. Enumeration is therefore adaptive: one
  query per category, and only a category that hits the cap gets drilled down
  its speciality list (and a capped speciality gets drilled per country).
  Today only "Nursing and Midwifery" is capped, so a run makes ~340 listing
  requests. The category-only query is always kept too — a few jobs have no
  speciality assigned (e.g. `#15770`) and appear in no speciality slice.
* **No posted date exists anywhere on the site** (master spec §4 deviation,
  same as the `nhm` scraper): `posted_date` is the date this scraper FIRST
  SAW the listing, so the watermark/time-window logic never excludes
  anything and dedup alone provides idempotency. Running twice adds 0 rows.
* **No country field.** It is resolved from the salary currency
  (`SAR`→Saudi Arabia, `AUD`→Australia, `GBP`→UK), then the location city,
  then a country word prefixed onto the listing title. Saudi jobs are quoted
  in *either* SAR or USD, so USD is deliberately not treated as a country
  signal. A job whose country cannot be resolved is kept and flagged
  `needs_review`.
* **Salaries** are usually the placeholder `On Application SAR` →
  `salary_raw = "Not Disclosed"` with empty numbers (master spec §3: never
  invented, never filtered on). Australian posts carry real figures
  (`86434 AUD` – `103979 AUD`). The site never states a period, so
  `salary_period_original` stays empty and `salary_period` is *inferred* from
  magnitude (≥ 20,000 → `per_annum`). The club schema's `salary_currency`
  enum allows INR/USD only, so SAR/AUD amounts live in the rich CSV and the
  club salary columns stay blank — amounts are never converted.
* **The employer is confidential** ("our client hospital" — agency mandates),
  so `company` is `Profco (Professional Connections)` and the `Hospital`
  blurb becomes `company_about`, the same convention as the `michaelpage`
  scraper.
* **Healthcare filter** (master spec §2) is at the source: every listing is
  healthcare recruitment and the site's own category maps onto the club enum
  (Nursing and Midwifery → `nurses`, Medical Doctor → `doctors`, Pharmacist →
  `pharmacists`, Allied Health Professionals → `non_clinical`). Listings in
  the generic categories (Other / Administration - Management / Engineering /
  Career with Profco) are classified from the title; a title with no
  healthcare signal at all is KEPT, flagged `needs_review` and logged to
  `needs_review.csv`.
* **Locum / 90-day contracts** have no club `job_type` value, so they stay
  `full_time` with the site's wording preserved in `contract_type`.
* **City names** are used verbatim apart from a small alias table for the
  site's own typos and suffixes (`Riaydh`→Riyadh, `Al Qassim`→Qassim,
  `Medina`→Al Madinah, `Sydney Eastern Surburbs`→Sydney, trailing commas
  stripped).
* **Open-vacancy board:** listings that disappear are closed postings. They
  stay in the rich CSV with their last `last_seen` date and are exported with
  `is_active=false` — but only when the run enumerated the whole board
  cleanly, so a partial/failed crawl never mass-deactivates jobs.

## Usage

```bash
python scraper.py                 # full run, appends to profco_jobs.csv
python scraper.py --limit 5       # test run: detail pages for 5 new jobs
python scraper.py --run-date 27-07-2026   # club CSV folder (default: today)
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests (56, no network)
```

Etiquette (master spec §7): descriptive User-Agent with a contact URL, 1 s
between requests, 4 retries with 3 s → 24 s exponential backoff on
429/5xx/network errors, and per-job error isolation (one bad fragment is
logged and skipped, never fatal). A full first run is ~540 requests and takes
15–45 minutes: the server intermittently drops connections
(`Connection reset by peer` / `Remote end closed connection`), which the retry
loop absorbs. Later runs are the same enumeration plus detail fetches for the
handful of new jobs only.

## Outputs

| File | Contents |
| --- | --- |
| `profco_jobs.csv` | rich cumulative store, dedup key `job_id` |
| `../../jobs_csv/<DD-MM-YYYY>/profco.csv` | HealthCareers.club 22-column import file |
| `needs_review.csv` | kept-but-unclassified titles / unresolved countries (only written when a run flags something — the 27-07-2026 first run flagged none) |

Rich columns: `source, job_id, title, company, company_about, city, country,
country_code, country_dial_code, site_category, site_speciality,
contract_type, salary_raw, salary_min, salary_max, salary_period,
salary_period_original, salary_currency, job_type, category, company_type,
min_experience_years, needs_review, posted_date, last_seen, description,
benefits, requirements, hospital, job_url, scraped_at`.

## Daily schedule

```cron
0 8 * * * cd /Users/gaganakki/Documents/SahiLabs/HealthCareers/job-scrappers/scrappers/profco && ../../.venv/bin/python scraper.py >> profco.log 2>&1
```
