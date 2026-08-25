# Job Scrapers

Scrapers collect healthcare job listings from external portals and write them into a shared CSV format for import into HealthCareers.club.

**Goal:** scrape jobs from each portal → classify every job through `scrappers/_shared/classification.py` → write rows on the 22-column club schema → save under `jobs_csv/<DD-MM-YYYY>/<site_name>.csv`.

## Folder layout

```
scrappers/
├── _shared/                 # classification.py, role_families.py, taxonomy_keywords.py
├── shine_roles/             # one folder per job site
│   ├── shine_scraper.py
│   ├── README.md            # site-specific notes (URL, selectors, quirks)
│   ├── test_filters.py
│   └── requirements.txt
├── another_site/
│   └── ...
jobs_csv/
└── 25-08-2026/              # run date: DD-MM-YYYY
    ├── shine_roles.csv
    └── another_site.csv
```

| Path | Purpose |
|---|---|
| `scrappers/<site_name>/` | Scraper for one portal |
| `scrappers/<site_name>/<site>_jobs.csv` | Rich cumulative per-source store (source of truth) |
| `jobs_csv/<date>/` | Output folder for that day's runs |
| `jobs_csv/<date>/<site_name>.csv` | That site's club export, regenerated each run |

## Output CSV format — the club schema

Every new scraper **must** write exactly the 22 `CLUB_COLUMNS` defined in
[`scrappers/_shared/classification.py`](./scrappers/_shared/classification.py),
and every migrated scraper does. Import the list — never hand-copy it. Column
order and names must match exactly. (Scrapers not yet migrated still write an
older club schema — see the note at the end of this section.)

### Columns

| Column              | Required | Description                             |
| ------------------- | -------- | --------------------------------------- |
| `country_name`      | Yes      | e.g. `India`                            |
| `country_code`      | Yes      | ISO code, e.g. `IN`                     |
| `country_dial_code` | Yes      | e.g. `+91`                              |
| `city_name`         | Yes      | e.g. `Mumbai`                           |
| `company_name`      | Yes      | Employer name                           |
| `company_type`      | Yes      | Enum — see below                        |
| `company_logo`      | No       | Image URL                               |
| `company_about`     | No       | Short company description               |
| `title`             | Yes      | Job title                               |
| `description`       | No       | Full job details                        |
| `job_type`          | Yes      | Enum — see below                        |
| `category`          | Yes      | `Non Clinical` or `Public Health`       |
| `sub_category`      | Yes      | One of the 20 sub-categories below      |
| `application_url`   | Yes      | URL where the candidate applies         |
| `posted_at`         | Yes      | `YYYY-MM-DD`                            |
| `min_experience`    | No       | Years (integer)                         |
| `max_experience`    | No       | Years (integer)                         |
| `qualification`     | No       | Source's qualification field, else `extract_qualification(description)` — never inferred |
| `min_salary`        | No       | Full amount (e.g. `1500000`, not lakhs) |
| `max_salary`        | No       | Full amount                             |
| `salary_period`     | No       | Enum — see below                        |
| `salary_currency`   | No       | Enum — see below                        |

The old `is_active` and `expires_at` columns are retired everywhere (rich CSVs
may keep source expiry data in their own columns).

Leave optional fields empty when the source page does not provide them. Do not invent salary or experience.

### Allowed enum values

Use these exact strings. Do **not** use display labels like `Full Time` or numeric codes.

| Field          | Allowed values                                   |
| -------------- | ------------------------------------------------ |
| `company_type` | `hospital`, `pharma`                             |
| `job_type`     | `full_time`, `part_time`, `remote`, `hybrid`     |
| `category`     | `Non Clinical`, `Public Health`                  |
| `sub_category` | one of the 20 sub-categories below               |
| `salary_period` | `per_annum`, `per_month`                        |
| `salary_currency` | `INR`, `USD`                                  |

#### The two-level taxonomy

Two **categories**, each with ten **sub-categories** (full titles and keyword
lists in [`Jobs_keywords/keywords_for_jobs.md`](./Jobs_keywords/keywords_for_jobs.md),
machine-readable form in `scrappers/_shared/taxonomy_keywords.py`):

| `category` | `sub_category` values |
|---|---|
| `Non Clinical` | Clinical Data Management · Clinical Research · Medical Writer · TMF · Medical Coding · Pharmacovigilance · Regulatory Affairs · Medical Reviewer · MSL · HEOR |
| `Public Health` | Epidemiology · Public Health Program Management · Monitoring & Evaluation · Community Health · Health Promotion & Education · Disease Programs · Public Health Nutrition · Infection Prevention & Control · Health Informatics & Data · Public Health Research |

### Classification — one module

This is the **standard**: every migrated scraper classifies through
`scrappers/_shared/classification.py`, and **every new scraper must**. All
keep/drop and labeling decisions go through it:

```python
from classification import classify_job, extract_qualification, CLUB_COLUMNS

verdict = classify_job(title, skills, description)
if not verdict["in_scope"]:
    counters["excluded_out_of_scope"] += 1   # dropped, never exported
    continue
category, sub_category = verdict["category"], verdict["sub_category"]
```

- No scraper defines its own category regexes, enums, ALLOW/DENY classification
  lists, or fallback maps. (Crawl-side scoping that merely saves requests —
  URL slug filters, source category facets — may stay.)
- `in_scope == False` → drop and count as `excluded_out_of_scope`.
- `needs_review == True` → keep the row AND append it to `needs_review.csv`.
- The rich CSV additionally records `role_family` and the score-trace columns
  so every admission stays auditable.

> **Not the whole fleet yet.** A set of scrapers has not been migrated and
> still uses its own per-scraper classification — the legacy profession enum
> `doctors | nurses | pharmacists | non_clinical` — and the older club schema.
> `instructions/taxonomy-migration-status.md` holds the authoritative
> per-scraper list; it is deliberately not repeated here. Their output does
> **not** match the schema above until they are migrated.

## Adding a new site scraper

See `prompts/master-prompt.md` for the full playbook. In short:

1. Create `scrappers/<site_name>/` with `<site_name>_scraper.py`, `README.md`, `test_filters.py`, and `requirements.txt`.
2. In `README.md`, document the portal URL, how listing/detail pages work, and any rate-limit notes.
3. Classify every candidate job with `classify_job` and map scraped fields → `CLUB_COLUMNS` (including enum mapping).
4. Write output to `jobs_csv/<DD-MM-YYYY>/<site_name>.csv`, regenerated from the rich store each run.
5. Create the date folder if it does not exist.

### Suggested scraper behaviour

```text
1. Fetch job listing pages from the portal
2. For each job, open detail page (if needed) and extract fields
3. classify_job(title, skills, description) → drop out-of-scope, flag needs_review
4. Normalize enums / dates / salary to the format above
5. Ensure jobs_csv/<today>/ exists
6. Write <site_name>.csv with the CLUB_COLUMNS header
```

## Checklist before handing off a CSV

- [ ] Header is exactly `CLUB_COLUMNS` from `_shared/classification.py`
- [ ] `category` is `Non Clinical`/`Public Health`; `sub_category` is one of the 20
- [ ] Every exported row passed `classify_job` (out-of-scope rows dropped and counted)
- [ ] Enum fields use allowed values only
- [ ] `application_url` is a working apply link
- [ ] `posted_at` is `YYYY-MM-DD`
- [ ] File path is `jobs_csv/<DD-MM-YYYY>/<site_name>.csv`
- [ ] No invented salary/experience/qualification when the source is silent
- [ ] Text with commas/newlines is properly CSV-quoted
