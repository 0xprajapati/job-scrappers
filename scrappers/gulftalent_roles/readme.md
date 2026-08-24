# gulftalent.com scraper

Scrapes healthcare job postings from
[gulftalent.com/kuwait/jobs/industry/healthcare](https://www.gulftalent.com/kuwait/jobs/industry/healthcare)
into the shared HealthCareers.club schema. Any GulfTalent country works via
`--country` (the listing path is the only thing that changes).

## Data source

GulfTalent renders one listing per country + industry, server-side:

```
https://www.gulftalent.com/<country>/jobs/industry/healthcare        # page 1
https://www.gulftalent.com/<country>/jobs/industry/healthcare/<N>    # page N
```

Industry 15 is "Healthcare, Pharmaceuticals & Medical Services", so the
healthcare filter runs **at the source** (master spec §2). 25 rows per page,
sorted newest-first, and the pager exposes the page count
(`data-cy="pagination-last-btn"`).

There *is* a JSON endpoint behind the filter sidebar
(`/api/jobs/search?filters[industry][0]=15&…`), but it only returns facet
counts — every `config[results]` variant tried returns HTTP 500 — so the
scraper uses the server-rendered listing plus each detail page's schema.org
`JobPosting` JSON-LD (master spec preference #2).

**Listing row** → numeric job id (`data-ga-label`, the dedup key), title,
detail URL, employer (linked to `/companies/…` or bare text), city, posted
date as `13 May` (no year), company logo, Easy-Apply flag.

**Detail page** → JSON-LD `JobPosting` (`datePosted`, `validThrough`,
`employmentType`, `hiringOrganization` + logo, `baseSalary`, `jobLocation`,
`industry`, full HTML description) plus an attribute grid (Job Type, Job
Location, Nationality, Salary, Gender, Arabic Fluency, Job Function, Company
Industry), the `Ref: …` code, and an "About the Company" blurb when the
employer has a profile.

## Quirks

* **Desktop User-Agent required.** The site 302-redirects non-browser UAs to
  `/mobile/…`, whose listing renders only the first 25 jobs and whose
  `/mobile/…/<N>` paths serve an unrelated FAQ page. The scraper's UA keeps a
  desktop platform token in front of its own name and contact URL; a run that
  lands on `/mobile/` logs a warning.
* **Listing dates have no year** (`13 May`). They are used only as a cheap
  pre-filter (skip clearly-old rows before paying for a detail fetch) and for
  the newest-first early stop; `posted_date` always comes from the detail
  page's `datePosted`. A date that would fall in the future is read as last
  year's.
* **Postings are purged ~90 days after posting** (`validThrough` = posted +
  90), so the listing only ever holds live jobs.
* **Salary is usually "Not Specified"**; when present it is a monthly
  Gulf-currency range (`7000 - 8000 AED`, also in JSON-LD `baseSalary`).
  Annual figures are normalised to monthly. The club schema's
  `salary_currency` enum allows only INR/USD, so AED/KWD/… amounts stay in
  the rich CSV and the club salary columns are left blank — same decision as
  the `dubizzle` and `dubailivejobs` scrapers. Nothing is ever invented.
* **No structured experience field.** `min_experience` / `max_experience` are
  mined from the description ("Minimum 7 years of experience …" → 7); blank
  when the description says nothing.
* **Employers are largely agencies** (MENA Recruit, TalentGrade, Michael
  Page, Robert Walters). `company_type` therefore looks only at the employer
  name, title and job function — an agency blurb name-drops every sector it
  staffs, which would make every row look like pharma.
* **Country-wide postings** carry `Job Location: Kuwait` / `UAE`; the rich CSV
  leaves `city` empty in that case and the club CSV falls back to the country
  name (the feed expects a place). Street-level JSON-LD localities
  ("Zabeel 2 - Zabeel - Dubai") lose to the listing's own city facet.
* **Kuwait's healthcare listing is tiny** — 1 live job on 2026-07-27
  (`Commercial Manager`, Eva Pharma, posted 13 May 2026). UAE has ~1,388,
  Saudi Arabia 125, Qatar 54, Oman 53 if a wider crawl is ever wanted.

## Healthcare filter & review flags

The listing is already industry-filtered, so the classifier only maps a job
onto the club category enum (`doctors` / `nurses` / `pharmacists` /
`non_clinical`) from the title, with the site's Job Function as context.
GulfTalent's healthcare industry also carries commercial and admin roles at
pharma companies; those are **kept** and flagged `needs_review` when neither
the title, job function, employer name nor description opening shows any
healthcare signal, and logged to `needs_review.csv` (master spec §2 — nothing
is silently dropped). The constant site industry string is deliberately left
out of that signal test, or nothing would ever be flagged.

## Time window

The listing only holds live postings, so the **first run keeps all of them**
(`INITIAL_WINDOW_DAYS = None`, as in the `sidra` / `seha` / `medcare`
scrapers). Later runs use the master-spec watermark: newest stored
`posted_date` minus `WATERMARK_GRACE_DAYS = 2`. Since the listing is
newest-first, pagination stops as soon as a whole page predates the cutoff.

## robots.txt

`www.gulftalent.com/robots.txt` blocks a named list of AI-training crawlers
(ClaudeBot, GPTBot, CCBot, …), throttles some SEO bots with `Crawl-delay: 30`,
and ends with `User-agent: * / Allow: /` — the rule this scraper's UA falls
under. It is fetched and checked per-URL at startup (listing page 1, a
paginated listing page, a detail page); a disallow aborts the run. Requests
are ≥1s apart with exponential backoff (3s → 24s) on 429/5xx.

## Usage

```bash
python scraper.py                          # Kuwait, full incremental run
python scraper.py --country uae            # any GulfTalent country slug
python scraper.py --max-pages 2 --limit 5  # smoke test
python scraper.py --run-date 27-07-2026    # jobs_csv/<DD-MM-YYYY>/ folder
python scraper.py --no-club-csv --verbose  # rich CSV only, debug logging
python test_filters.py                     # 37 unit tests, no pytest needed
```

Country slugs: `kuwait`, `uae`, `saudi-arabia`, `qatar`, `oman`, `bahrain`,
`egypt`, `jordan`, `lebanon`, `iraq`, `morocco`.

## Outputs

| File | Contents |
| --- | --- |
| `gulftalent_jobs.csv` | rich cumulative store, dedup key = numeric job id |
| `../../jobs_csv/<DD-MM-YYYY>/gulftalent.csv` | HealthCareers.club 22-column schema |
| `needs_review.csv` | titles with no healthcare signal (kept, flagged) |

Running twice in a row adds 0 rows (verified). Dependencies: `requests`,
`pandas` — the shared venv at `../../.venv` already has both.
