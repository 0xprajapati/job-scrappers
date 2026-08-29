# jobsacuk scraper

Scrapes academic and research vacancies from [jobs.ac.uk](https://www.jobs.ac.uk),
the UK's academic job board (Jisc). The board's supply is university posts —
lecturers, senior lecturers, professors, postdoctoral research associates,
research fellows, PhD studentships and university professional services —
across every discipline. The health-relevant slice is **UK public health
academia** ("Associate Lecturer in Public Health", "Postdoctoral Research
Associate in Public Health Nutrition", "Lecturer / Senior Lecturer in Public
Health Management") plus the odd clinical-trials methodologist; the large
majority (physics studentships, finance faculty, estates officers) is dropped
by the shared classifier.

## How it works

1. **Discovery: the board's own search, run with no keyword and no facet.**
   `/search/?keywords=&sortOrder=1&pageSize=25&startIndex=N`. `sortOrder=1`
   is **date-placed descending** (verified 2026-08-27: page 1 all "26 Aug",
   `startIndex=2001` "24 Jul"), so the crawl walks the whole board
   newest-first and stops at the first page with no card inside the window.
   This is the widest cheap enumeration the site offers and a strict superset
   of any union of health keyword searches — an empty search reports
   **"2,294 Jobs Found"**, i.e. the entire board.

   `sitemap0.xml` (the only sitemap robots.txt names, served via a 302 to S3)
   carries 2,216 `/job/<ref>/<slug>` URLs — the same board — but has **no
   `<lastmod>`** on any entry, only `<changefreq>daily</changefreq>`. Using it
   would force a detail fetch for all 2,216 ids on every run. The search cards
   carry "Date Placed", so the date gate runs before any detail request; the
   sitemap is documented here but not used.

2. **Detail pages** (`/job/<ref>/<slug>`) embed a complete schema.org
   JobPosting JSON-LD block: `datePosted`, `validThrough`, clean `title`,
   the full HTML `description` (verified complete, not truncated),
   `hiringOrganization` (name, logo, department) and a `jobLocation` address
   (locality / region / country). The visible advert-details table adds
   Location, Salary, Hours, Contract Type and Job Ref — none of which are in
   the JSON-LD.

3. **Advert information** is the board's curated role signal: a "Type / Role"
   tag (`Academic or Research`, `PhDs`, `Professional or Managerial`, …) plus
   one or more "Subject Area(s)" tags from the site's academic-discipline
   taxonomy (`Health & Medical` > `Medicine & Dentistry`, `Nutrition`, …).
   They are joined and passed to `classify_job` as `skills`, kept verbatim in
   the rich CSV's `sectors` column, and **never allowed to decide the
   category** on their own.

4. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`): out of scope → dropped, counted, full row
   appended to `out-of-scope.csv`; `needs_review` → kept AND flagged. The
   scraper defines no category regexes and never touches
   `taxonomy_keywords.py` / `role_families.py`.

5. **Skip lists.** Out-of-window ids go to `seen_old_ids.csv` and dropped ids
   live in `out-of-scope.csv` — both consulted before fetching, so each
   advert is fetched at most once, ever. Steady state ≈ 2-3 listing pages
   plus one detail request per genuinely new advert.

6. **Studentships.** PhD and Masters studentships share the board, the URL
   space and the same JobPosting JSON-LD, but they are study places, not
   jobs. A row whose Type / Role is `phds`/`masters`, or whose title says
   studentship / scholarship / doctoral training, is marked
   `is_studentship` and force-flagged into `needs_review.csv` when the
   classifier keeps it — the same treatment devnetjobsindia gives RFPs.

## Site quirks handled

| Quirk | Handling |
|---|---|
| `pageSize` capped server-side at 25 (`50`/`100`/`200` all return 25 cards) | page count fixed at `ceil(window / 25)`; `MAX_PAGES` backstop |
| Search cards stamp **"Date Placed: 25 Aug"** — day and month, **no year** | year inferred (current, rolled back one if that post-dates the advert) and used only as a ±3-day pre-filter; the JSON-LD `datePosted` is the authoritative gate |
| Second date row is "Closes:" on most adverts but "Expires:" on others | both read from the same table parser |
| "Type / Role" renders as a *disabled* `<input type="button">` on jobs but a live `<input type="submit">` on studentships | read from the enclosing `<form action="/search/<slug>">`, stable across both |
| "Job tools" sidebar also has `<input type="submit">` buttons ("Apply", "Create Job Alert") | subject areas only read from forms carrying an `academicDisciplineFacet` / `subDisciplineFacet` hidden input, inside the Advert information block |
| Salaries are free text (`£39,906 to £46,049 per annum`, `£69.68 per hour`, `Competitive`) | parsed to `salary_raw` + min/max/currency/period; grade numbers (`Grade 7`) excluded, decimals kept |
| GBP is not in the club schema's `salary_currency` enum (INR/USD only) | club salary columns stay blank, verbatim values live in the rich CSV — the **reed** precedent. Nothing converted or invented |
| Board is UK-centric but not UK-only (Chengdu, Germany, …) | club country columns come from the advert's `addressCountry`, not a constant; unknown countries keep their name and leave code/dial blank |

`min_experience` and `qualification` are grounded extractions from the
description ("at least 3 years of research experience", PhD/MPH/MSc
credentials), never inferred.

## Outputs

| File | Purpose |
|---|---|
| `jobsacuk_jobs.csv` | Rich cumulative store (dedup key: `job_id`, the `/job/<ref>` reference) |
| `../../jobs_csv/<DD-MM-YYYY>/jobsacuk.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids — detail-fetch skip list |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged (other-profession titles, studentships) |

## Running

```bash
python scraper.py                # incremental run
python scraper.py --limit 25     # test run: at most 25 detail fetches
python scraper.py --max-pages 5  # cap the listing crawl
python scraper.py --since 2026-08-01
python test_filters.py           # 40 unit tests (no network)
```

Time window: first run keeps the last 14 days; later runs use
`max(stored posted_date) − 2 days`. The board places roughly 200 adverts a
day, so the first run is ~55 listing pages and ~1,300 detail fetches
(~45 min at the 1 s delay); later runs cost a handful of requests.

robots.txt is `User-agent: *` with only `/job/feedback/` and `/enhanced/fp/`
disallowed, and is verified at startup. Probed and built 2026-08-27.

## Taxonomy note

UK academic titles are unlike anything else in the fleet, and the shared
taxonomy has no honest home for the **teaching** half of public health
academia. `classify_job` admits "Associate Lecturer in Public Health" and
"Lecturer / Senior Lecturer in Public Health Management" to the Public Health
family but the ten-way sub-category split cannot place them, so
`sub_category` comes back blank — `classification.py` leaves it blank
deliberately ("there is no honest fallback"). Research-side titles
("Postdoctoral **Research Associate** in Public Health Nutrition") do place,
via the Public Health Research title pattern. See the run report for the
measured counts; nothing here was worked around by widening keywords.
