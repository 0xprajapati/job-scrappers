# himalayas.app — remote healthcare jobs

Scrapes **remote healthcare** listings from [himalayas.app](https://himalayas.app),
a remote-only job board. Because every listing on the board is remote, REMOTE is
guaranteed at the source rather than inferred from a title or a work-mode field.

## Why not Glassdoor?

This scraper was commissioned as "remote healthcare jobs from
`glassdoor.co.in`". Glassdoor cannot be scraped within the
[master spec](../../instructions/master-scraper-spec.md), on two independent
grounds:

**Its robots.txt forbids every path a scraper needs** (§7 makes robots.txt
binding):

| Rule | Blocks |
|---|---|
| `Disallow: /graph`, `/api/`, `/api-web/` | the entire JSON API layer (spec §1's preferred source) |
| `Disallow: /Job/*_IP*`, `/Jobs/*_P*.htm*` | all job-search pagination — at most page 1 of any search |
| `Disallow: /job-listing/*_IE*.htm`, `/job-listing/details.htm?*` | the job detail pages |
| `Disallow: /` under `ClaudeBot` / `Claude-Web` / `anthropic-ai` / `GPTBot` | AI agents, site-wide |

**And the site is 403-walled anyway.** Every URL — including
`https://www.glassdoor.co.in/index.htm` — returns HTTP 403 with a
`Security | Glassdoor` interstitial to any non-browser client. Retrieving data
would require defeating that bot detection (spoofed browser fingerprints,
stealth headless, proxy rotation), which the spec does not sanction.

Two other candidates were measured and rejected before settling here:

- **Remotive** — its public API now ignores every query parameter
  (`category=`, `search=`, `limit=`) and returns a fixed 36-job slice, of which
  2 were healthcare. Not a viable feed.
- **RemoteOK** — public API works, but the feed is capped at 100 jobs and
  skews almost entirely to tech.

## Data source

Public JSON feed, newest-first, offset-paginated, 20 jobs per page:

```
GET https://himalayas.app/jobs/api?offset=N
-> {updatedAt, offset, limit, totalCount, jobs: [...20...]}
```

Per-job fields: `title`, `excerpt`, `companyName`, `companySlug`,
`companyLogo`, `employmentType`, `minSalary`, `maxSalary`, `salaryPeriod`,
`currency`, `seniority`, `locationRestrictions`, `timezoneRestrictions`,
`categories`, `parentCategories`, `description` (HTML), `pubDate`,
`expiryDate`, `applicationLink`, `guid`.

`robots.txt` is `User-Agent: * / Allow: / / Disallow: /apply`. `/jobs/api` is
allowed and `/apply` is never requested. A plain descriptive User-Agent works —
no browser impersonation.

### Quirks (all verified against a 1,560-job sample, 2026-07-29)

- **`pubDate` is strictly descending across offsets.** This is what makes the
  watermark early-stop safe: once a page is entirely older than the cutoff,
  everything below it is too, so a daily run touches ~1 page instead of the
  94k-job archive.
- **The feed accepts no filtering.** `category=`, `categories=`, `search=`,
  `q=`, `keyword=`, `market=` are all silently ignored and return the identical
  unfiltered page. Healthcare has to be selected client-side.
- **`limit=` is ignored too** — pages are always 20.
- **Missing values arrive as the *string* `"None"`,** not JSON `null`, in
  `minSalary` / `maxSalary` / `currency`.
- **List fields are Python-repr strings**, not JSON arrays:
  `"['United States']"`. Parsed with `ast.literal_eval`.
- The feed grows by roughly 1,000 jobs/day, ~2.4% of them healthcare.

## Healthcare filter

The spec (§2) prefers filtering at the source, but this feed offers none, so
the gate is a **union of three signals**, sized against the sample:

1. `parentCategories` contains `"Healthcare"` — high precision, **low recall**:
   only 38 of the sample's 218 healthcare jobs had it; the field is empty on
   most postings.
2. A healthcare slug in `categories` (`Licensed-Mental-Health-Counselor`,
   `Telehealth-Therapy`, …) — this recovered **all 180** healthcare jobs that
   signal 1 missed.
3. A healthcare term in the title — the safety net for empty category lists.

Which signal fired is recorded per row in `match_signal`.

A job matching none of the three is counted `excluded_non_healthcare` — this is
a general job board and ~96% of it is software/sales. Bare `care` and `wellness`
are deliberately **not** healthcare terms (they would swallow "Customer Care
Representative"); `healthcare`, `patient care` and `home health` are.

### The deny list — healthcare employers hiring non-healthcare roles

The `categories` signal has a known failure mode: healthcare companies tag
ordinary tech and commercial openings with healthcare slugs, which passes the
gate. Real examples from the first run:

| Title | Employer | Slug that let it through |
|---|---|---|
| Freelance WordPress Developer | Insight Therapy Solutions | `Healthcare-Web-Developer` |
| Front-End Software Engineer (React/Typescript) | Beacon Biosignals | `Healthcare-Technology` |
| Latvian Interpreter | LanguageLine Solutions | `Healthcare-Interpretation` |
| Senior Business Intelligence Analyst | Imagine Pediatrics | `Healthcare-Analytics` |

`DENY_TITLE_KEYWORDS` catches these occupations (software/web/mobile dev,
data engineering, BI, QA, translation/interpretation, design). They are **not
dropped** — the spec forbids silent drops, and whether an industry-adjacent
tech role belongs on HealthCareers.club is an editorial call, not the
scraper's. They are kept and forced to `needs_review` so a human decides.

Without the deny list "Senior Business Intelligence Analyst" matched the
generic `analyst` bucket and looked *confidently* classified, which was the
actual bug: not that it was included, but that nothing marked it as doubtful.

A job that *is* healthcare but whose club category can't be pinned down from
its title is likewise **kept**, flagged `needs_review`, and logged to
`needs_review.csv` — never silently dropped, per the spec.

### Category mapping caveat

The club `category` enum is `doctors | nurses | pharmacists | non_clinical`.
This board's healthcare supply is overwhelmingly US licensed behavioural-health
and telehealth work — LCSWs, LMFTs, LMHCs, psychologists, therapists. There is
no allied-health bucket in the enum, so **licensed non-physician clinicians map
to `non_clinical`**. That is a schema limitation, not a claim that the roles are
non-clinical; the untouched job title is in every row.

## Salary

Master spec §3 — capture, never filter, never invent. No job is ever excluded
for its salary or for not disclosing one.

`salary_raw`, `salary_min`, `salary_max` and the real `salary_currency` are
stored verbatim in the rich CSV. The club schema's `salary_currency` enum
allows only `INR`/`USD` and `salary_period` only `per_annum`/`per_month`, so the
club salary columns are filled **only** for USD/INR annual or monthly pay.
CAD/EUR/GBP/PLN/ZAR figures and hourly rates keep their values in the rich CSV
and leave the club columns empty rather than being misdeclared as USD.

About 45% of listings disclose a salary; the rest record `Not Disclosed` with
empty numerics.

## Field mapping notes

| Club column | Source | Note |
|---|---|---|
| `job_type` | — | always `remote`; the enum can't express "remote **and** part-time", so `employmentType` (Full Time / Contractor / Part Time / Intern / …) lives in the rich CSV |
| `city_name` | — | always `Remote`; these roles have no city, and inventing one would be a fabrication |
| `country_name` | `locationRestrictions[0]` | remote roles are scoped by *hiring region*, often several. The full list is in the rich CSV's `locations`; the club row takes the primary one |
| `country_code` / `country_dial_code` | lookup table | empty for countries outside the table rather than guessed |
| `country_name` | — | blank on the ~0.5% of rows whose `locationRestrictions` is empty — genuinely unrestricted worldwide roles. Left blank rather than filled with an invented country; the importer can treat blank as "no restriction" |
| `min_experience` / `max_experience` | — | always empty. The feed exposes `seniority` (Senior / Mid-level / …), which is a level, not a number of years — converting it would be inventing data |
| `posted_at` / `expires_at` | `pubDate` / `expiryDate` | unix epoch seconds → `YYYY-MM-DD` (UTC) |
| `application_url` | `applicationLink` | the himalayas job page, which links out to the employer's ATS |

## Usage

```bash
python himalayas_scraper.py
```

Options:

| Flag | Purpose |
|---|---|
| `--output PATH` | rich cumulative CSV (default `himalayas_jobs.csv`) |
| `--max-pages N` | stop after N API pages — test runs |
| `--limit N` | stop after N new jobs — test runs |
| `--run-date DD-MM-YYYY` | target `jobs_csv/<date>/` folder (default: today) |
| `--reclassify` | re-apply the classifier to the stored CSV and rewrite the outputs — **no network requests**. `category`/`needs_review` are pure functions of the job title, so tuning the keyword lists never requires re-crawling |
| `--verbose` | debug logging |

Unit tests (parsers, healthcare gate, classifier, cutoff, club mapping):

```bash
python test_filters.py
```

## Outputs

- `himalayas_jobs.csv` — rich cumulative store, dedup key `job_id`, and the
  source of truth for the incremental watermark.
- `../../jobs_csv/<DD-MM-YYYY>/himalayas.csv` — the same jobs in the shared
  22-column HealthCareers.club schema.
- `needs_review.csv` — healthcare jobs whose category couldn't be classified.

## Time window & idempotency

- **First run**: jobs posted in the last `INITIAL_WINDOW_DAYS` (7).
- **Later runs**: jobs newer than the newest stored `posted_date` minus
  `WATERMARK_GRACE_DAYS` (2) of overlap, which dedup absorbs.
- Dedup key is `job_id`, taken from the guid's trailing numeric id when it has
  one, otherwise `<company-slug>/<job-slug>` — the bare job slug is **not**
  unique across employers.
- Running twice in a row adds 0 rows.

Intended cadence is one run per day.

## First run (2026-07-29)

| | |
|---|---|
| Jobs scanned | 19,160 (7-day window, stopped at offset 19,140) |
| Excluded — non-healthcare | 15,148 |
| Excluded — older than cutoff | 37 |
| **Healthcare jobs written** | **3,835** |
| Flagged `needs_review` | 582 (15%) |
| Transient retries | 3 (all recovered; 0 pages lost) |

Breakdown: `non_clinical` 2,876 · `nurses` 492 · `doctors` 395 ·
`pharmacists` 72. Match signal: title 1,792 · categories 1,548 ·
parent_category 495. 45% disclose a salary. Top hiring regions: United States
3,068, Australia 105, Canada 83, United Kingdom 68, Philippines 43, India 40.

Healthcare density is far higher deeper in the feed (~4% in the first few
hundred jobs, ~23% by offset 2,000) because employers such as OptiMindHealth
post dozens of near-identical listings per city in a single batch.
