# himalayas.app — remote clinical-research & pharma-regulatory jobs

Scrapes [himalayas.app](https://himalayas.app), a remote-only job board, for
**eleven role families**. Because every listing on the board is remote, REMOTE
is guaranteed at the source rather than inferred from a title or work-mode
field.

| Family | Stored | Example title |
|---|--:|---|
| Clinical Research | 399 | Senior Clinical Research Associate - UK - Remote |
| Medical Coding | 132 | Medical Coder (SC Upstate Residents) |
| MSL | 87 | Medical Science Liaison - Metabolism |
| Medical Writer | 82 | Principal Medical Writer - Publications |
| Pharmacovigilance | 61 | Executive Director, Pharmacovigilance (PV) |
| Clinical Data Management | 58 | Senior Clinical Data Manager |
| Regulatory Affairs | 52 | Senior Associate, Regulatory Affairs (US) |
| HEOR | 50 | Senior Director, HEOR & Evidence Strategy |
| Public Health | 32 | Population Health Program Coordinator |
| Medical Reviewer | 21 | Medical Monitor (Gastroenterology) |
| TMF | 14 | Associate Director, TMF Operations Lead |

Counts are from the 2026-08-21 re-scope of the stored corpus.

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

## Scope filter — the eleven role families

The gate matches the **job title** against `ROLE_FAMILIES`. The first family
that matches labels the row in `role_family`; everything else is counted
`excluded_out_of_scope`. Re-scoping the 7,930-job stored corpus kept 988 (12%)
— the rest was the general telehealth / behavioural-health / nursing market
the earlier broad healthcare gate admitted.

### Why the title, and not the feed's own category slugs

This looked like the ideal source-side signal (master spec §1 prefers it), and
the feed does carry exact tags: `Clinical-Data-Management`, `Pharmacovigilance`,
`Medical-Science-Liaison`, `Regulatory-Affairs`, `Medical-Coding`. Measured
against the stored corpus, **roughly half the slug-only admissions were wrong**:

| Title admitted by slug alone | Slug |
|---|---|
| Registered Dietitian | `Clinical-Research` |
| Lead Account Manager - Clinical Services | `Clinical-Research` |
| Nurse Practitioner (Remote, SC License Required) | `Public-Health` |
| Perinatal Social Worker | `Public-Health` |
| Open Application | `Medical-Affairs` |
| Senior IT Project Manager - Enterprise Platforms | `Regulatory-Affairs` |

The tagging is automated and loose, so slugs are **not** an admission path.
`match_role_family()` accepts a `categories` argument and ignores it.

### Precedence

Families are ordered specific → broad, and the first match wins:

`TMF → HEOR → Medical Reviewer → MSL → Pharmacovigilance → Medical Coding →
Medical Writer → Regulatory Affairs → Clinical Data Management →
Public Health → Clinical Research`

So "Medical Writer - Clinical Regulatory Documentation" is a Medical Writer,
and "Principal Clinical Trial Regulatory Affairs" is Regulatory Affairs.

### Boundaries that took measurement to get right

Each of these was a real false positive found in the corpus:

| Trap | Why it matters | Handling |
|---|---|---|
| `CDM` | In US revenue-cycle listings it means **Charge Description Master** ("Revenue Integrity & CDM Operations Manager") | requires a clinical/trial/EDC context |
| bare `data management` | Swallows "Manager, Client Data Management", "Configuration / Data Management Analyst - Federal Health" | same clinical-context requirement |
| `regulatory compliance` | Usually **revenue-cycle** compliance ("Senior Regulatory Compliance and Revenue Cycle Analyst"), not pharma RA | excluded from Regulatory Affairs |
| `field medical` | In this feed it appears in IME titles: "Physician Reviewer - Field Medical Director, Radiology" | excluded from MSL |
| `MSL` | Also means **Medical Stop Loss**, an insurance product | kept, flagged for review |
| `biostatistics` | Pharma biometrics, not public health, and not in the requested scope | excluded from Public Health |

**Medical Reviewer is the pharma sense only** — medical monitoring and medical
review at sponsors and CROs. It deliberately excludes the two adjacent US
markets that share the words: payer-side utilization review/management
("Utilization Review Nurse-LVN/LPN", 43 roles in the corpus) and IME /
disability peer review ("Board Certified Physician Disability Peer Reviewer").
Widen `ROLE_FAMILIES["Medical Reviewer"]` if you want them.

**Medical Coding is mostly US revenue-cycle work** — hospital and profee
coders, DRG reviewers, risk adjustment — rather than clinical-trial coding
(MedDRA/WHODrug). That is the market as it exists on this board.

### Kept but flagged, never dropped

A title carrying an in-scope term inside a plainly different profession is
**kept** and written to `needs_review.csv` — the spec forbids silent drops, and
whether these belong on the site is an editorial call:

- Senior Counsel, Global Commercial Legal - U.S. Market Access and Pricing
- Business Development Director (Clinical Research)
- Regional Account Manager, Medical Stop Loss (MSL) Distribution
- Clinical Research Patient Recruiter

### Club `category` vs `role_family`

They answer different questions and both are stored:

- `role_family` — which of the eleven in-scope families the job belongs to.
  This is the new scope dimension and lives in the rich CSV.
- `category` — the club enum `doctors | nurses | pharmacists | non_clinical`,
  which describes the *profession*. Most of these roles are `non_clinical`
  (907 of 988) because clinical-research and regulatory work is not bedside
  care; `pharmacists` (41), `doctors` (27) and `nurses` (13) pick up the
  PharmD/MD/RN-credentialed postings.

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
| `qualification` | `description` | credentials lifted **verbatim** from the posting (PharmD, MBBS, PhD, MPH, CPC, CCS, RHIA, CDISC, "Bachelor's degree", "Life Sciences" …). Grounded extraction, never inferred from the title — empty when the posting names none (populated on 79% of rows). Bare `MD` and `DO` are deliberately not matched: "Remote, MD" is Maryland and "do" is a verb; only `M.D.`, `MD/DO` and `MD degree` count |
| `description` | `description` | HTML stripped to plain text. Stored **in full** — real descriptions run 600–9,900 chars (median ~3,900). `DESCRIPTION_MAX_CHARS` (20,000) is a safety valve against a pathological row, not a content budget, and never fires in practice; if it ever does, the text is cut on a word boundary and marked with a trailing `…` so a shortened description can't be mistaken for a complete one |

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
| `--since YYYY-MM-DD` | override the watermark and re-walk the full window against an existing CSV. Use after the feed regenerates to recover jobs an earlier snapshot never served — see **Feed pagination** above |
| `--reclassify` | re-apply the **scope gate** and category classifier to the stored CSV and rewrite the outputs — **no network requests**. `role_family`, `category` and `needs_review` are pure functions of the job title, so re-scoping never requires re-crawling. Rows that no longer match any family are moved to `out-of-scope.csv` |
| `--verbose` | debug logging |

Unit tests (parsers, healthcare gate, classifier, cutoff, club mapping):

```bash
python test_filters.py
```

## Club schema migration (2026-08-21)

`job_samples.csv` changed after this scraper was first written: it gained
`qualification` and dropped `is_active` and `expires_at`, going from 22 columns
to 21. This scraper now emits the current 21-column contract, verified byte-for
-byte against `job_samples.csv` and against
`jobs_csv/19-08-2026/pharmabharat_categories.csv`.

**Heads-up on a repo inconsistency**: `jobs_csv/21-08-2026/naukri.csv` still
writes the *old* 22-column header (`is_active`, `expires_at`, no
`qualification`), so the two scrapers currently disagree. `job_samples.csv` is
the documented source of truth per the repo README, which is what this scraper
follows — but naukri looks like it still needs migrating.

`expires_date` is still captured in the rich CSV; it simply no longer has a
column in the club export.

## Outputs

- `himalayas_jobs.csv` — rich cumulative store, dedup key `job_id`, and the
  source of truth for the incremental watermark. Carries `role_family`.
- `out-of-scope.csv` — rows dropped by a `--reclassify` scope change, kept so
  a narrowing is reversible and reviewable rather than destructive.
- `../../jobs_csv/<DD-MM-YYYY>/himalayas.csv` — the same jobs in the shared
  22-column HealthCareers.club schema.
- `needs_review.csv` — healthcare jobs whose category couldn't be classified.

## Feed pagination — a single pass is NOT complete

**This is the most important limitation of this scraper.** The API's deep
`offset` pagination is unstable: past a few thousand rows it re-serves jobs
already returned at earlier offsets and correspondingly omits others. There is
no cursor to page by instead — `before=`, `since=`, `page_token=` do not exist,
and every query parameter is ignored.

Measured directly: offsets 9,000–9,980 returned **1,000 slots containing only
651 distinct jobs — 35% re-serves**. No two pages were identical, so the
repeats are interleaved, not duplicated pages. That is the classic signature of
`OFFSET` paging over a sort key with ties (`pubDate`) and no stable tiebreaker.

### It is deterministic per snapshot — re-running immediately gains nothing

The feed is a periodically regenerated snapshot (see `updatedAt` in every
response). Within one snapshot the instability is *deterministic*: two full
passes three hours apart returned byte-identical counts (19,500 scanned /
15,521 excluded / 20 old) and the second added **0 new jobs**. Re-running
against the same snapshot is pure waste.

Coverage only improves once the feed regenerates. Across two different
snapshots the same day:

| | jobs |
|---|---|
| Snapshot A pass | 3,835 |
| Snapshot B pass | 2,684 |
| Found only in B | 103 |
| Found only in A | 1,254 |
| **Union** | **3,938** |

Snapshot A's extra 1,254 jobs were spread evenly across all 7 days, so they
were not aging out — snapshot B simply never served them.

**Do not treat one run's output as the complete window.** The daily cadence is
what converges: each run overlaps the previous by `WATERMARK_GRACE_DAYS` and
dedup on `job_id` makes accumulation safe, so jobs missed by one snapshot get
picked up from a later one. `--since` forces a full-window re-walk when you
want to catch up after a regeneration:

```bash
python himalayas_scraper.py --since 2026-08-01
```

The run summary flags the condition automatically when re-serves exceed 2% of
scanned slots.

## Time window & idempotency

- **First run**: jobs posted in the last `INITIAL_WINDOW_DAYS` (**2**).
  Narrowed from the spec's default of 7: the feed posts ~2,400 jobs/day, so
  each extra day of first-run window costs ~2,400 offsets (~9 min) to collect
  jobs that are already stale by import time.
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
