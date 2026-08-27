# nhsjobs scraper

Scrapes [jobs.nhs.uk](https://www.jobs.nhs.uk) — the NHS's central vacancy
board for England and Wales, and the fleet's only UK clinical-system
source. The board is overwhelmingly clinical (nursing, medical, allied
health), so this is a textbook **fetch wide, filter tight** source: the
whole board is enumerated and the shared classifier drops the great
majority. See "Measured in-scope rate" below before judging the yield.

## How it works

1. **Discovery: the `staffGroup` partition.** `keyword=` (empty) returns
   the whole board — 12,972 live adverts on 2026-08-27 — so no keyword is
   used. Keyword matching here is a relevance ranker, not a filter
   (`keyword=public health` alone matched 9,307 of the 12,972), which is
   exactly why scoping on one would be wrong.

   The board is walked through the search form's own `staffGroup` facet,
   whose nine values were measured to partition it **exactly**:

   | staffGroup | adverts | staffGroup | adverts |
   |---|---:|---|---:|
   | `NURSING_AND_MIDWIFERY_REGD` | 2,808 | `CLINICAL_SERVICES` | 1,537 |
   | `ADMINISTRATIVE_AND_CLERICAL` | 2,325 | `ESTATES_AND_ANCILLARY` | 1,477 |
   | `MEDICAL_AND_DENTAL` | 2,097 | `PROF_SCIENTIFIC_AND_TECHNICAL` | 366 |
   | `ALLIED_HEALTH_PROF` | 2,042 | `HEALTHCARE_SCIENTISTS` | 316 |
   | `STUDENTS` | 4 | **sum** | **12,972** |

   Crawling all nine *is* the whole board — not a health facet — at the
   same page cost as an unfaceted crawl (each advert sits in exactly one
   group), and it gives every row the site's own curated occupational tag
   for free. `assert_partition()` re-checks the sum against the unfaceted
   total on every run and warns loudly if NHS adds a group.

2. **Listing pages** are walked `sort=publicationDateDesc` (newest first),
   10 results per page (`pageSize` is accepted and ignored), and each group
   is retired after two consecutive pages with nothing on/after the
   cutoff. Deep pagination is not capped — page 1,298 of 1,298 served
   normally. The nine walks are **interleaved a page at a time**: the full
   crawl reads the same pages either way, but a run cut short by `--limit`,
   a network failure or an interrupt then holds a balanced slice of the
   board instead of all of nursing and none of the administrative and
   scientific groups that carry most of the in-scope supply.

3. **The date gate runs before any detail fetch.** The card carries title,
   employer, location, salary, posted date, closing date, contract type and
   working pattern, so out-of-window adverts cost zero detail requests,
   ever. (This is why there is no `seen_old_ids.csv` — the
   devnetjobsindia layout needs one only because its posted date lives on
   the detail page.)

4. **Detail pages** are plain SSR HTML with clean element ids
   (`heading`, `employer_name`, `job_overview`, `job_description`,
   `job_description_large`, `essential/desirable_skill_N_criteria_M`,
   `date_posted`, `payscheme-type`, `payscheme-band`, `range_salary`,
   `contract_type`, `employer_town/county/postcode/country`). There is
   **no JSON-LD anywhere**, and **no robots.txt**: `/robots.txt`,
   `/sitemap.xml` and every unknown path return the same 200 "Service
   Domain Information" page. The scraper still requests `/robots.txt` and
   enforces it only if the response really is `text/plain`, rather than
   letting `RobotFileParser` "allow everything" after being fed HTML.

5. **The one pre-fetch skip, and why it isn't a second classifier.** The
   board publishes ~1,000 adverts a day and most are clinical, so detail
   fetches dominate the cost. `taxonomy_keywords.classify_subcategory`
   applies the negative-keyword veto to `title + " " + skills` and
   **never** to the description — so for a card whose title trips it
   ("Staff Nurse …"), `classify_job(title, group, "")` and
   `classify_job(title, group, <description>)` return the identical
   verdict and the fetch cannot change the outcome. Those rows are dropped
   at the listing stage with `vetoed_by` recorded. Nothing else is dropped
   early: a card that merely fails to *score* without a description still
   earns its detail fetch, because the description is what would score it.

6. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`), and only that:
   * `skills` = the human-readable staffGroup label ("Administrative and
     Clerical", "Allied Health Professional", …) — the site's own curated
     occupational tag, the same use `reed` makes of its taxonomyLevel1/2.
     Kept in the rich CSV as a raw source column; never decides a category
     on its own.
   * `description` = job summary + main duties + job responsibilities +
     the Person Specification criteria. The person-spec bullets are folded
     into the description (weight ×1) rather than passed as `skills`
     (weight ×2) — they are requirement prose, not tags. The "About us"
     block is excluded entirely: it describes the trust, not the role.
   * `in_scope: False` → dropped, counted, full row appended to
     `out-of-scope.csv`, **never** the main CSV; `needs_review` → kept AND
     flagged.

7. **Skip lists.** Dropped ids live in `out-of-scope.csv` (full rows,
   reversible) and are consulted before fetching, so a dropped advert is
   never re-fetched while it stays in window.

`band` and `pay_scheme` are captured raw ("Band 7", "Agenda for change")
as source columns; they are **never** used to decide scope. Salaries are
GBP, and the club schema's `salary_currency` enum admits only INR/USD, so
the club CSV's salary columns stay blank while the verbatim range lives in
the rich CSV (the `reed` precedent — captured, never filtered on, never
converted). NHS publishes no years-of-experience field, so
`min_experience`/`max_experience` are blank rather than inferred;
`qualification` is the shared grounded extraction from the description.

## Measured in-scope rate

Fetch wide, filter tight is not a slogan here. Every run prints an
`In-scope rate` line — `new / (new + excluded_out_of_scope)`. Two probes
were run through the shared classifier on 2026-08-27 before the first
full crawl:

**Probe A — the newest 20 adverts in each of the nine groups (164
classified): 3.0% in scope**, ≈2.6% once weighted by group size (≈340 of
the 12,972 live adverts, ≈26 a day of new supply). Every keep came from
two groups:

| staff group | kept / seen | what was kept |
|---|---|---|
| `ALLIED_HEALTH_PROF` | 3 / 20 | three dietitians → Public Health Nutrition |
| `PROF_SCIENTIFIC_AND_TECHNICAL` | 2 / 20 | Band 7 Population Health Data Analyst; one Band 4 pharmacy technician (see below) |
| the other seven | 0 / 124 | — |

**Probe B — the first five pages of the three least-clinical groups
(`ADMINISTRATIVE_AND_CLERICAL`, `PROF_SCIENTIFIC_AND_TECHNICAL`,
`HEALTHCARE_SCIENTISTS`; 150 classified): 5 kept.** Band-graded adverts
across Band 2–9 were sampled; what the classifier dropped is exactly what
the 2026-08-27 "back-office at health orgs is OUT" ruling says should be
dropped — receptionists, medical secretaries, patient administrators,
employee-relations advisors, pensions technicians, facilities and project
managers — plus clinical delivery (psychologists, CBT therapists, ward and
dispensary pharmacists). What survived was on-taxonomy: a Band 6 Clinical
Trials Data Specialist, a Band 7 Population Health Data Analyst, a Band 8b
Lead Pharmacist for Community Health, an embryologist Clinical Scientist.

**Two things to watch.**

* Narrowing the crawl to the "non-clinical" staff groups would look
  tempting and would be wrong: the dietitians — in scope by explicit
  ruling — all sit in `ALLIED_HEALTH_PROF`.
* One weak admission: "Pharmacy Technician (with ward duties)" (Band 4)
  entered as **Non Clinical → Clinical Research** on `Clinical Research=3`,
  against 6–7 for every other keep. It is a ward dispensing role. This is
  a classifier-tuning question, not a scraper one — `taxonomy_keywords.py`
  and `role_families.py` are never edited from here.

The board also barely exercises the listing-stage veto: only 1 of 164
adverts in probe A tripped a negative keyword ("Band 3 Phlebotomist" in
probe B was the other). NHS titles are "Registered Nurse", "Ward Sister",
"Healthcare Assistant" — none of which are negative keywords. The
short-circuit is still correct and free, but it is not what makes the
crawl affordable; the pre-fetch **date** gate is.

## Outputs

| File | Purpose |
|---|---|
| `nhsjobs_jobs.csv` | Rich cumulative store (dedup key: `job_id`, the NHS advert reference) |
| `../../jobs_csv/<DD-MM-YYYY>/nhsjobs.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged |

## Running

```bash
python scraper.py                                    # incremental run
python scraper.py --limit 50 --no-partition-check    # quick test run
python scraper.py --staff-group ADMINISTRATIVE_AND_CLERICAL
python scraper.py --since 2026-08-01
python test_filters.py                               # unit tests (no network)
```

Time window: first run keeps the last 7 days; later runs use
`max(stored posted_date) − 2 days`. Seven days is deliberately narrow —
the board turns over ~1,000 adverts a day, so a 7-day first run is roughly
7,000 adverts ≈ 700 listing pages plus a detail fetch for every advert the
title veto does not already reject. Widen with `--since` only when
seeding a bigger corpus, and expect the run to take hours at the 1 s
delay.

Probed and built 2026-08-27.
