# pharmabharat.com scraper

Scrapes pharma job postings from [pharmabharat.com](https://pharmabharat.com/)
into the HealthCareers.club CSV contract.

## Data source

WordPress site with a fully open REST API — no HTML listing pages are parsed:

```
GET https://pharmabharat.com/wp-json/wp/v2/posts
    ?per_page=50&page=N
    &_fields=id,date,link,title,content,categories
```

- Posts return newest-first with the full article HTML (~11,500 posts,
  a few dozen per day).
- Unlike pharmarecruiter.in there is **no jobs/news split**: every category
  on the site is a job-type taxonomy (production-jobs, qc-jobs,
  clinical-research-jobs, pharmacovigilance-jobs, …), so all posts are
  scraped and the slugs stored in `site_categories`.
- robots.txt is empty (checked at startup anyway) — everything is allowed.

## Field extraction

Posts embed their facts in one of **three shapes**, all handled by
`extract_labeled_fields()` (first value per field wins):

1. A "Job Details" table — `<tr><td>Company</td><td>Sandoz</td></tr>`
2. Labelled bullets — `<li><strong>Experience:</strong> 2–6 Years</li>`
3. Labelled paragraphs — a `Label:` line with the value on the same or the
   following text line (common in walk-in posts)

| Label                                        | Mapped to |
| -------------------------------------------- | --------- |
| Company / Company Name / Organization        | `company` |
| Position / Designation / Job Role(s)         | `position` |
| Location / Job Location                      | `city` + country (default India) |
| Experience (`2–6 Years`, `Freshers`, …)      | `min_experience` / `max_experience` |
| Qualification / Eligibility                  | `qualification` |
| Job Type / Employment Type + Work Mode       | `job_type` enum |
| Salary / Estimated Salary / Stipend / CTC    | `salary_*` fields |
| Application Deadline (`28 July 2026`)        | `expires_at` |

## Quirks

- Many "Salary" values are the **site's own estimates** written in prose
  ("Based on industry standards…"). Only compact values containing a real
  amount (₹ / LPA / digits) are parsed; the raw string keeps qualifiers
  like "(Estimated)". Prose-only or absent salary → `"Not Disclosed"`,
  numeric fields empty; no salary filtering ever (master spec).
- Parentheticals are stripped before amount extraction so "(based on 2025
  standards)" is never read as a salary number.
- One post often advertises a multi-role walk-in drive; the post title is
  kept as the job title, and `position` holds the labelled role when given.
- Club `category`: the site is pharma-industry, so production / QC / QA /
  clinical research / PV titles are `non_clinical`; explicit pharmacist /
  doctor / nurse titles (or the site's `pharmacist` category) map to their
  clinical buckets. `company_type` defaults to `pharma`.
- Posts with no parseable company (facts only in prose), or news-shaped
  titles, are kept and flagged `needs_review` (never dropped).

## Outputs

- `pharmabharat_jobs.csv` — rich cumulative store, deduped on WP post id.
- `../../jobs_csv/<DD-MM-YYYY>/pharmabharat.csv` — 22-column club schema.
- `needs_review.csv` — flagged rows from the latest run.

## Usage

```bash
python scraper.py                 # incremental daily run
python scraper.py --max-pages 2   # test run (2 pages of 50)
python scraper.py --verbose       # debug logging
python test_filters.py            # unit tests
```

First run keeps the last 30 days; later runs use the newest stored
`posted_date` minus 2 days grace as the cutoff, stopping pagination once a
whole page is older (the API is newest-first).

Etiquette: descriptive User-Agent, robots.txt check at startup, ≥1s delay
between requests, exponential backoff (3s → 24s) on 429/5xx.

---

# daily_scraper.py — six-category daily feed

A second, narrower scraper for running **every day** against a chosen set of
categories, keeping the **complete job description**. `scraper.py` (above)
still owns the whole-site → 22-column club schema job; this one is for the
non-clinical category feed.

## What it does differently

| | `scraper.py` | `daily_scraper.py` |
|---|---|---|
| Scope | every post on the site | only the categories you ask for (server-side `?categories=` filter) |
| Facts from | parsing prose/tables out of the article body | the site's **ACF custom fields** — `company_name`, `position_name`, `location`, `qualification`, `experience`, `salary`, `mode_of_inteview` |
| Description | flattened text | full body as readable text: `##` headings, `-` bullets, apply URLs inlined as `label [https://…]` |
| Output | club 22-column schema | 17-column rich schema (below) |

The ACF fields are the same values the site prints on its own job cards, so
company/location/experience come out clean instead of being re-derived from
sentences.

## Categories

Defaults to these ten non-clinical ones:

| Slug | Label in the CSV |
|------|------------------|
| `clinical-data-management-jobs` | CDM (Clinical Data Management) |
| `clinical-research-jobs` | Clinical Research |
| `medical-writer-jobs` | Medical Writer |
| `tmf` | TMF |
| `medical-coding-jobs` | Medical Coding |
| `pharmacovigilance-jobs` | Pharmacovigilance |
| `regulatory-affairs-jobs` | Regulatory Affairs |
| `medical-reviewer` | Medical Reviewer |
| `medical-science-liaison-jobs` | MSL (Medical Science Liaison) |
| `heor-rwe` | HEOR / RWE |

The site's nav labels "RWE/HEOR" and "MSL" map to the `heor-rwe` and
`medical-science-liaison-jobs` slugs respectively.

Slugs are resolved to ids at runtime, so if the site renames one the run fails
loudly instead of silently scraping the wrong bucket.

```bash
python daily_scraper.py --list-categories        # all ~40 slugs, with post counts
python daily_scraper.py --categories tmf,medical-coding-jobs
python daily_scraper.py --jobs-csv-dir /srv/exports    # write run CSVs elsewhere
```

## Usage

```bash
python daily_scraper.py                  # the daily run — resumes from last time
python daily_scraper.py --days 7         # last 7 days, ignore saved state
python daily_scraper.py --since 2026-08-01
python daily_scraper.py --no-master --no-state --days 1   # one-off, writes nothing persistent
```

## Incremental behaviour

- First run with no state: last **7 days** (`--first-run-days`).
- Later runs: from the previous run's timestamp minus a **48h grace window**,
  so posts that are backdated or edited after publishing still get picked up.
- Every fetched post is diffed against the cumulative CSV on WP post id:
  - id not seen before → **new**
  - id seen but `modified` changed → **updated** (row is refreshed in place)
  - otherwise ignored.
- Running twice in one day is safe: the day's file is **merged on post id**,
  never overwritten, so a later run that finds nothing cannot wipe out what an
  earlier one collected.

State lives in `.daily_state.json` (`last_run`, `total_known`).

## Outputs

- `../../jobs_csv/<DD-MM-YYYY>/pharmabharat_categories.csv` — just what was new
  or updated that day, in the repo's usual dated run folders.
- `pharmabharat_category_jobs.csv` — cumulative store, one row per post id,
  newest first. Stays in this folder; it is a working store, not a run output
  (the same way `scraper.py` keeps `pharmabharat_jobs.csv` here).

`scraper.py` writes `pharmabharat.csv` into those same dated folders, so this
one deliberately uses a different basename and the two never overwrite each
other.

Columns:

```
Post ID · Category (matched) · Date Posted · Last Modified · Job Title ·
Company · Position · Location · Qualification · Experience · Salary ·
Mode of Application · Apply Link(s) · Contact Email(s) · All Site Categories ·
Job Post URL · Full Description
```

CSV is UTF-8 with BOM and fully quoted, so Excel opens it cleanly and the
multi-line descriptions stay in one cell.

## Notes on the data

- **Salary** is populated on well under a tenth of posts — the site leaves the
  field blank on most listings. Not a scraping gap.
- A post cross-listed in several target categories appears **once**, with every
  matching label in `Category (matched)` (e.g. a regulatory-affairs post that is
  also filed under clinical research).
- The four newest categories are low-volume: Regulatory Affairs runs ~13 posts
  a week, while Medical Reviewer, MSL and HEOR/RWE typically add only one or
  two each. A day with zero from them is normal, not a failure.
- `Apply Link(s)` excludes the portal's own links and social channels, matched
  on hostname only — an employer link carrying `?source=Pharmabharat.com`
  is kept.
- Walk-in posts sometimes have no apply URL at all; venue and timing are in
  the description, and `Contact Email(s)` catches the email-application ones.

## Scheduling

cron, every morning at 08:00:

```cron
0 8 * * * cd /Users/gaganakki/Documents/SahiLabs/HealthCareers/job-scrappers/scrappers/pharmabharat && ../../.venv/bin/python daily_scraper.py >> daily/run.log 2>&1
```

On macOS, cron needs Full Disk Access for `cron` (or use a launchd agent) if
the repo lives under `~/Documents`.

Etiquette: descriptive User-Agent, 1s between requests, exponential backoff
(3s → 24s) on 429/5xx.

## Running on a server (AWS)

`daily_scraper.py` is built to run unattended. What that adds:

**Timezone is pinned to the site, not the host.** The WordPress `?after=`
filter compares against `post_date` in **site-local time (IST)** — verified
against the live API, not assumed. A UTC EC2 host asking for "the last day"
with its own clock would be asking about a window shifted 5h30m, and a host
*ahead* of IST would silently miss posts. The scraper computes its window in
IST regardless of `TZ`, so the same run on a UTC, IST or `us-east-1` host
produces a byte-identical window. Nothing about the instance's timezone needs
configuring.

**Writes are atomic.** Every CSV and JSON goes to a temp file, is `fsync`ed,
then `os.replace`d into place. A spot-instance reclaim or OOM kill mid-write
leaves the previous complete file, never a truncated master CSV.

**Only one run at a time.** A `flock` guard means an overrunning job cannot
interleave with the next scheduled one; the second exits immediately with
code 3 and does no work. Disable with `--no-lock` only for manual one-offs.

**Exit codes**, for CloudWatch alarms or `OnFailure=`:

| Code | Meaning |
|------|---------|
| 0 | success (including "nothing new") |
| 1 | unhandled error — traceback is logged |
| 2 | bad configuration, e.g. unknown category slug |
| 3 | another run holds the lock |

**Logs** are timestamped and go to stderr, which journald/CloudWatch collect
and rotate. `--log-file` exists for cron setups; don't use it under systemd
unless you also add logrotate, or it grows forever.

**`--summary-json PATH`** drops run stats (`new_jobs`, `updated_jobs`,
`total_known`, window) for a monitoring agent to pick up.

### Data location

Set `PHARMABHARAT_DATA_DIR` and all outputs — master CSV, `jobs_csv/`, state,
lock — move together, so a code deploy never touches scraped data. With it set,
run CSVs go to `$PHARMABHARAT_DATA_DIR/jobs_csv/<DD-MM-YYYY>/` rather than into
the checkout, which matters under `ProtectSystem=strict`. Override just that
path with `PHARMABHARAT_JOBS_CSV_DIR`:

```bash
PHARMABHARAT_DATA_DIR=/var/lib/pharmabharat python daily_scraper.py
```

Put that on a persistent volume. The master CSV grows roughly **6 KB per job**
(the full description dominates); at ~40 jobs/day that is ~90 MB/year.

### Install

```bash
sudo useradd --system --home /var/lib/pharmabharat --create-home scraper
sudo git clone <repo> /opt/job-scrappers
sudo python3 -m venv /opt/job-scrappers/.venv
sudo /opt/job-scrappers/.venv/bin/pip install -r \
     /opt/job-scrappers/scrappers/pharmabharat/deploy/requirements.txt
sudo chown -R scraper:scraper /var/lib/pharmabharat

sudo cp deploy/pharmabharat-daily.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pharmabharat-daily.timer
```

Check it:

```bash
systemctl list-timers pharmabharat-daily     # next run
systemctl start pharmabharat-daily.service   # run now
journalctl -u pharmabharat-daily -n 50       # last run's log
```

The unit runs as an unprivileged `scraper` user under `ProtectSystem=strict`
with `/var/lib/pharmabharat` as the only writable path. The timer fires at
08:00 IST with `Persistent=true` (catches up if the box was down) and a
5-minute jitter.

### First run on a fresh server

State starts empty, so the first run pulls **7 days** and every post counts as
new. Seed a deeper history first if you want one:

```bash
python daily_scraper.py --since 2026-01-01     # backfill, then let the timer take over
```

### Notes

- Only `requests` is needed (`deploy/requirements.txt`); everything else is
  stdlib. `pandas` is for `scraper.py`, not this one.
- The site sits behind Cloudflare. It has been fine from residential and cloud
  IPs, but if an EC2 range ever gets challenged you'll see HTTP 403 with an
  HTML body — that's a WAF block, not a bug in the parser.
- No credentials are involved; the WP REST API is public and read-only.
