# gulftalent_roles scraper

A **role-scoped fork of the `gulftalent` scraper**. Same site, same HTML/JSON-LD
parsing; what differs is the crawl breadth, the time window and — the point of
the fork — that every candidate is judged by the shared two-level classifier
(`scrappers/_shared/classification.py`) instead of a local profession enum.

| | `gulftalent` | `gulftalent_roles` (this one) |
| --- | --- | --- |
| countries per run | 1 (`--country`, default `kuwait`) | **5** — `uae,saudi-arabia,qatar,oman,kuwait` |
| first-run window | every live posting | **last 2 days** (`INITIAL_WINDOW_DAYS = 2`) |
| keep/drop decision | local title regexes, nothing dropped | **`classify_job`** — out-of-scope rows dropped |
| `category` values | old profession enum | **`Non Clinical` / `Public Health`** + `sub_category` |
| club columns | 22, hand-copied, with `is_active`/`expires_at` | 22, imported from `_shared` `CLUB_COLUMNS` |

The two scrapers now overlap heavily (`gulftalent`'s single-country crawl is a
subset of this one's five); consolidating them is a live option.

## Data source

GulfTalent renders one listing per country + industry, server-side:

```
https://www.gulftalent.com/<country>/jobs/industry/healthcare        # page 1
https://www.gulftalent.com/<country>/jobs/industry/healthcare/<N>    # page N
```

Industry 15 is "Healthcare, Pharmaceuticals & Medical Services". That facet is
**crawl-side scoping only** — it saves requests, it does not label anything.
25 rows per page, sorted newest-first, and the pager exposes the page count
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

## Classification (shared, not local)

Every candidate goes through
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

This scraper defines **no** category regexes, profession enums or fallback
maps. The only local classifier left is `classify_company_type()`
(`hospital` | `pharma`), which fills the club's separate `company_type` field
and is not a category.

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
  90), so the listing only ever holds live jobs. `validThrough` is kept in the
  rich CSV's `expires_at` column; the club schema no longer carries it.
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
* **Listing volumes differ wildly** (2026-07-27 counts): UAE ~1,388, Saudi
  Arabia 125, Qatar 54, Oman 53, Kuwait 1. The default country order is
  largest-first so `--limit` smoke runs hit real data immediately.

## Time window

The first run keeps the last **`INITIAL_WINDOW_DAYS = 2`** days, matching the
rest of the re-scoped fleet (the parent `gulftalent` scraper keeps every live
posting instead). Later runs use the master-spec watermark: newest stored
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
python scraper.py                             # all 5 countries, incremental
python scraper.py --country uae               # one slug…
python scraper.py --country uae,qatar         # …or a comma-separated list
python scraper.py --max-pages 2 --limit 5     # smoke test (--limit is a TOTAL)
python scraper.py --run-date 25-08-2026       # jobs_csv/<DD-MM-YYYY>/ folder
python scraper.py --no-club-csv --verbose     # rich CSV only, debug logging
python test_filters.py                        # 37 unit tests, no pytest needed
```

Country slugs: `kuwait`, `uae`, `saudi-arabia`, `qatar`, `oman`, `bahrain`,
`egypt`, `jordan`, `lebanon`, `iraq`, `morocco`. Unknown slugs abort with a
usage error.

## Outputs

| File | Contents |
| --- | --- |
| `gulftalent_roles_jobs.csv` | rich cumulative store, dedup key = numeric job id; carries `category`, `sub_category`, `role_family` and the `all_families` / `family_scores` / `family_confidence` / `matched_in` score trace |
| `../../jobs_csv/<DD-MM-YYYY>/gulftalent_roles.csv` | HealthCareers.club schema, the 22 shared `CLUB_COLUMNS` |
| `needs_review.csv` | in-scope rows whose title looks like another profession (kept, flagged) |
| `out-of-scope.csv` | rows a reclassification moved out of the rich store — reversible, never silently discarded |

Running twice in a row adds 0 rows. Dependencies: `requests`, `pandas` — the
shared venv at `../../.venv` already has both.
