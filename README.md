# Job Scrapers

Scrapers collect healthcare job listings from external portals and write them into a shared CSV format for import into HealthCareers.club.

**Goal:** scrape jobs from each portal → write rows matching [`job_samples.csv`](./job_samples.csv) → save under `jobs_csv/<DD-MM-YYYY>/<site_name>.csv`.

<!-- Add  -->

## Folder layout

```
scrapers/
├── jobslly/                 # one folder per job site
│   ├── scraper.py
│   ├── readme.md            # site-specific notes (URL, selectors, quirks)
│   └── requirements.txt
├── another_site/
│   ├── scraper.py
│   ├── readme.md
│   └── requirements.txt
└── jobs_csv/
    └── 10-07-2026/          # run date: DD-MM-YYYY
        ├── jobslly.csv
        └── another_site.csv
```

| Path | Purpose |
|||
| `<site_name>/` | Scraper for one portal (e.g. `jobslly`) |
| `jobs_csv/<date>/` | Output folder for that day’s runs |
| `jobs_csv/<date>/<site_name>.csv` | Scraped jobs from that site |

## Output CSV format

Every scraper **must** write the same columns as [`job_samples.csv`](./job_samples.csv). Column order and names must match exactly.

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
| `category`          | Yes      | Enum — see below                        |
| `application_url`   | Yes      | URL where the candidate applies         |
| `posted_at`         | Yes      | `YYYY-MM-DD`                            |
| `min_experience`    | No       | Years (integer)                         |
| `max_experience`    | No       | Years (integer)                         |
| `qualification`     | No       | Qualification or education requirements |
| `min_salary`        | No       | Full amount (e.g. `1500000`, not lakhs) |
| `max_salary`        | No       | Full amount                             |
| `salary_period`     | No       | Enum — see below                        |
| `salary_currency`   | No       | Enum — see below                        |

Leave optional fields empty when the source page does not provide them. Do not invent salary or experience.

### Allowed enum values

Use these exact strings (Postgres / Prisma enums). Do **not** use display labels like `Full Time` or numeric codes.

| Field          | Allowed values                                   |
| -------------- | ------------------------------------------------ |
| `company_type` | `hospital`, `pharma`                             |
| `job_type`     | `full_time`, `part_time`, `remote`, `hybrid`     |
| `category`     | `Clinical Data Management`, `Clinical Research`, |

`Medical Writer`, `TMF`, `Medical Coding`, `Pharmacovigilance`, `Regulatory Affairs`,
`Medical Reviewer`, `MSL`, and `HEOR` |
| `salary_period` | `per_annum`, `per_month` |
| `salary_currency` | `INR`, `USD` |

Full sample file: [`job_samples.csv`](./job_samples.csv).

## Adding a new site scraper

1. Create `scrapers/<site_name>/` with `scraper.py`, `readme.md`, and `requirements.txt`.
2. In `readme.md`, document the portal URL, how listing/detail pages work, and any rate-limit notes.
3. Map scraped fields → the CSV columns above (including enum mapping).
4. Write output to:

    ```
    scrapers/jobs_csv/<DD-MM-YYYY>/<site_name>.csv
    ```

    Example for Jobslly on 10 Jul 2026:

    ```
    scrapers/jobs_csv/10-07-2026/jobslly.csv
    ```

5. Create the date folder if it does not exist. Overwrite or append only as agreed for that site’s run.

### Suggested `scraper.py` behaviour

```text
1. Fetch job listing pages from the portal
2. For each job, open detail page (if needed) and extract fields
3. Normalize enums / dates / salary to the format above
4. Ensure jobs_csv/<today>/ exists
5. Write <site_name>.csv with the header row from job_samples.csv
```

## Checklist before handing off a CSV

- [ ] Header matches `job_samples.csv` exactly
- [ ] Enum fields use allowed values only
- [ ] `application_url` is a working apply link
- [ ] `posted_at` is `YYYY-MM-DD`
- [ ] File path is `jobs_csv/<DD-MM-YYYY>/<site_name>.csv`
- [ ] No invented salary/experience when the source is silent
- [ ] Text with commas/newlines is properly CSV-quoted
