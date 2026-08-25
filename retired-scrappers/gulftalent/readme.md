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
* **No structured experience or qualification field.** `min_experience` /
  `max_experience` are mined from the description ("Minimum 7 years of
  experience …" → 7); `qualification` comes from the shared grounded
  `extract_qualification(description)`. Both are blank when the description
  says nothing — never inferred.
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

## Classification (shared, not local)

The industry listing is **crawl-side scoping only** — it saves requests, it
does not label anything. Every candidate goes through
`_shared/classification.classify_job(title, skills, description)`:

* **`skills`** = GulfTalent's own **Job Function** grid value — the site's
  curated role taxonomy, the closest thing it offers to a skills tag list. The
  raw value stays in the rich CSV as the `job_function` source column; it never
  decides the category.
* **`description`** = the JSON-LD description, HTML-stripped.
* `in_scope == False` → the row is **dropped** and counted as
  `excluded_out_of_scope` in the run summary. Because GulfTalent's healthcare
  industry is mostly bedside and hospital-operations advertising, most rows are
  dropped; what survives is clinical research, pharmacovigilance, regulatory
  affairs, medical writing, medical coding, HEOR, MSL and the public-health
  families.
* `needs_review == True` → the row is **kept** and appended to
  `needs_review.csv`.

The rich CSV records `category` (`Non Clinical` / `Public Health`),
`sub_category`, `role_family` and the `all_families` / `family_scores` /
`family_confidence` / `matched_in` score trace, so every admission stays
auditable. The club CSV carries `category` + `sub_category`.

This scraper defines **no** category regexes, profession enums or fallback
maps. The only local classifier left is `classify_company_type()`
(`hospital` | `pharma`), which fills the club's separate `company_type` field
and is not a category.

> **Overlap note:** `scrappers/gulftalent_roles/` is a fork of this scraper
> that crawls five countries per run (`uae,saudi-arabia,qatar,oman,kuwait`)
> with a 2-day first-run window. Now that both use the same shared classifier
> and the same club schema, the two overlap heavily — this one's single-country
> crawl is a subset of the fork's. Consolidating them is a live option.

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
| `gulftalent_jobs.csv` | rich cumulative store, dedup key = numeric job id; carries `category`, `sub_category`, `role_family` and the family score trace |
| `../../jobs_csv/<DD-MM-YYYY>/gulftalent.csv` | HealthCareers.club schema, the 22 shared `CLUB_COLUMNS` |
| `needs_review.csv` | in-scope rows whose title looks like another profession (kept, flagged) |
| `out-of-scope.csv` | rows a reclassification moved out of the rich store — reversible, never silently discarded |

Running twice in a row adds 0 rows (verified). Dependencies: `requests`,
`pandas` — the shared venv at `../../.venv` already has both.
