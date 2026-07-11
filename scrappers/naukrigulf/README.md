# naukrigulf scraper

Scrapes healthcare jobs from [naukrigulf.com](https://www.naukrigulf.com) —
the Gulf (UAE / Saudi / Qatar / Kuwait / Bahrain / Oman) arm of Naukri.

Source listing URL (agreed filters):
`https://www.naukrigulf.com/healthcare-jobs?freshness=1,3,7,15&industryType=30,37`
→ industries **Medical (30) + Pharmaceutical (37)**, posted within the
**last 15 days**, keyword `healthcare`.

## Data source

The site is a client-rendered SPA over a public JSON API at `/spapi/`
(allowed by robots.txt). Both endpoints need two static headers, without
which they return HTTP 400:

```
appid: 205
systemid: 2323
```

| Endpoint | Purpose |
|---|---|
| `GET /spapi/jobapi/search?ClusterInd=30,37&Freshness=1,3,7,15&Keywords=healthcare&SortPreference=date&Limit=30&Offset=N&pageNo=P` | Listing, 30 jobs/page, newest-first |
| `GET /spapi/jobs/<JobId>` | Detail (only with `--enrich`) |

Listing fields: `Designation`, `Location` (`"City - Country (UAE)"`),
`jobInfo` (short summary, often null), `Experience {Min,Max}`,
`Company {Name,Id}`, `JobId`, `JdURL` (absolute apply/detail URL),
`LatestPostedDate` (unix epoch), `Vacancies`, `LogoUrl`.

The listing has **no salary and no employment type**. The detail API adds:
`Description` (HTML), `IndustryType`, `FunctionalArea`,
`Compensation {jobMinCurrency "AED 3,000", jobMaxCurrency, IsCtcHidden, salaryTimeBrand}`,
`Other.currLabel`, `Company.Profile`, `DesiredCandidate.Education`,
`employmentType` (`Full Time`/`Part Time`), `locationType`
(`On Site`/`Remote`/`Hybrid`).

## Quirks

- **Akamai bot protection**: the site resets/blackholes connections from
  non-browser TLS fingerprints *and* non-browser User-Agents (plain
  `requests`/`curl` hang or die with HTTP/2 stream errors). The scraper uses
  `curl_cffi` with `impersonate="chrome"`; contact info travels in the
  standard `From` header since a descriptive UA gets blocked.
- **PascalCase vs camelCase**: with the `appid`/`systemid` headers the API
  returns `{"Jobs": [{"Job": {PascalCase...}}]}`; the browser (cookie)
  variant returns camelCase. The scraper handles the header-based shape.
- **Salary is Gulf-currency** (AED/SAR/QAR...). The club schema only allows
  `INR`/`USD`, so the club CSV's salary columns are filled **only** when the
  source currency is INR/USD *and* the period is known; otherwise they stay
  empty and the verbatim string lives in the rich CSV's `salary_raw`
  (master spec: capture, don't filter — never invent).
- `Location` sometimes repeats the country (`"Qatar - Qatar"`); the country
  is then used as `city_name` too.
- `SortPreference=date` sorts newest-first, letting incremental runs stop
  paginating once a whole page is older than the watermark.
- Some `jobInfo` values are null; without `--enrich` those rows have an
  empty description.

## Usage

```bash
pip install -r requirements.txt

# sample run: 2 pages, with detail enrichment
python naukrigulf_scraper.py --max-pages 2 --enrich

# full daily run (recommended)
python naukrigulf_scraper.py --enrich

# listing-only (fast, no descriptions/salary/employment type)
python naukrigulf_scraper.py
```

Flags: `--output` (rich CSV path), `--max-pages N` / `--limit N` (test
runs), `--enrich` (detail calls, ~1 req/s, off by default), `--run-date
DD-MM-YYYY`, `--verbose`.

Tests: `python test_filters.py`

## Outputs

| File | What |
|---|---|
| `naukrigulf_jobs.csv` | Rich cumulative store, dedup key `job_id`, watermark source |
| `../../jobs_csv/<DD-MM-YYYY>/naukrigulf.csv` | HealthCareers.club 22-column schema |
| `needs_review.csv` | Titles the category classifier could not place (kept as `non_clinical`, never dropped) |

## Category / enum mapping

- `category`: title regexes → `nurses` / `pharmacists` / `doctors`
  (incl. `*ologist`, registrar, anaesthetist...); known admin/allied
  titles → `non_clinical`; anything else → `non_clinical` + flagged in
  `needs_review.csv`.
- `company_type`: pharma/labs/diagnostics/CRO keywords in the company name
  (or a pharma `IndustryType` via `--enrich`) → `pharma`, else `hospital`.
- `job_type`: from detail `locationType`/`employmentType`; defaults to
  `full_time` without `--enrich` (the board is overwhelmingly full-time).

## Etiquette

robots.txt is checked at startup; ≥1s delay between requests; exponential
backoff (3s → 24s) on 429/5xx; descriptive User-Agent; one bad job/page
never crashes the run.
