# hziegler.com healthcare scraper

Scrapes [Helen Ziegler and Associates](https://www.hziegler.com) (HZA), a
North-American recruitment agency placing nurses, allied-health professionals
and physicians with hospitals in **Saudi Arabia, the UAE and Canada**. The
entire site is healthcare recruitment, so every posting is healthcare at the
source — the classifier only has to map roles onto the club's category enum.

Live inventory at the time of writing: **54 jobs** (48 nursing, 5 physicians,
1 allied health).

## Data source

Small hand-built static site: no JSON API, no `/sitemap.xml`, and
`/robots.txt` returns **404** (nothing disallowed — the scraper still parses
it per-URL at startup and aborts if that ever changes).

| Purpose | Source |
|---|---|
| Job list | Server-rendered directory on `/` and the three `/locations/*.html` pages |
| Job detail | schema.org **JobPosting JSON-LD** on `/jobs/<slug>.html` |
| Employer | First `featureBox` of the detail page's right column (`/employers/<slug>.html`) |

Index pages group jobs under `<h2 id="jobs_<section>">` (nursing /
allied-health-clinical-services / physicians) and `<h3>` role groups
("RN - Perioperative Services"), with `Title - City, Country` link labels.
All four index pages are scanned and merged on URL, so a layout change on one
page still leaves the run with a full inventory:

```
/                                     54 jobs
/locations/saudi-arabia.html          49
/locations/united-arab-emirates.html   2
/locations/canada.html                 3
```

The detail JSON-LD is authoritative for `title`, `datePosted`,
`employmentType` and `jobLocation`. Its `description` copy starts at
"Requirements:", so the scraper prefers the page's own left-column markdown
blocks, which also include the employer intro paragraph.

Dedup key: the URL slug (`rn-nicu--king-faisal-medina`), which is also the
JSON-LD `identifier`.

## Quirks

* **No salary anywhere on the site** (agency practice) → `salary_raw = "Not
  Disclosed"` and all numeric salary fields stay empty, in both CSVs.
  Postings do advertise benefits ("Tax-free income", housing, return airfare,
  36 days annual leave) and those stay in the description.
* `hiringOrganization` is always "Helen Ziegler and Associates" (the
  recruiter), so the **real employer** is taken from the right-column
  employer box — mostly King Faisal Specialist Hospital (Riyadh/Medina),
  RVH Barrie, Hinton Medical Clinic. Confidential mandates name a location
  there instead ("Riyadh", "Abu Dhabi", "Canada (Confidential)"); those are
  detected and relabelled `Confidential Client (via Helen Ziegler &
  Associates)` with `is_confidential=True` (6 of 54 today).
* **Title beats section** in classification: HZA files *Psychologist* under
  PHYSICIANS, and the broad `-ologist` doctor rule would otherwise mislabel
  it, so allied-health titles (therapist, technologist, psychologist,
  dietitian, …) are matched first and land in `non_clinical`. The section is
  only a fallback for titles no regex can place.
* HTTP `Last-Modified` is a **site-wide rebuild timestamp** — identical on
  every page — and is deliberately not used as a date. `datePosted` is.
* Index links carry no date, so the window can only be applied after a detail
  fetch; out-of-window jobs go to `seen_old_ids.csv` so later runs skip them.
* `min_experience` is parsed from the requirements prose ("a minimum of two
  years", "Minimum three years", "a minimum of 10 years") — first match wins,
  blank when the posting states none. `max_experience` is never stated.
* Postings occasionally state client restrictions (nationality/gender); these
  are left verbatim in the description, not modelled as fields.

## Time window

An agency's live inventory keeps mandates open for years (dates today span
**2017-08-22 → 2026-06-29**), so as with the ATS-backed scrapers the first
run keeps every open job (`INITIAL_WINDOW_DAYS = None`). Later runs use the
master-spec watermark: newest stored `posted_date` minus
`WATERMARK_GRACE_DAYS = 2`. Dedup on the slug makes repeat runs no-ops — a
second run adds 0 rows and makes only the 4 index requests.

## Usage

```bash
../../.venv/bin/python scraper.py                 # full run (~1.5 min first time)
../../.venv/bin/python scraper.py --limit 3       # sample run, 3 new detail pages
../../.venv/bin/python scraper.py --verbose       # debug logging
../../.venv/bin/python test_filters.py            # 38 unit tests, no network
```

Flags: `--output` (rich CSV path), `--limit N` (cap new detail fetches),
`--run-date DD-MM-YYYY` (target `jobs_csv/` folder), `--verbose`.

Etiquette: descriptive User-Agent, 1 s between requests, 3 retries with
exponential backoff (3 s → 24 s) on 429/5xx/network errors, per-page failures
logged and skipped rather than fatal.

## Outputs

| File | Contents |
|---|---|
| `hziegler_jobs.csv` | Rich cumulative store, 29 columns, dedup on `job_id` |
| `../../jobs_csv/<DD-MM-YYYY>/hziegler.csv` | HealthCareers.club 22-column import schema |
| `needs_review.csv` | Titles with no healthcare signal (none so far) |
| `seen_old_ids.csv` | Slugs found out-of-window, skipped on later runs |

## Daily schedule

```cron
0 8 * * * cd /Users/gaganakki/Documents/SahiLabs/HealthCareers/job-scrappers/scrappers/hziegler && ../../.venv/bin/python scraper.py >> hziegler.log 2>&1
```
