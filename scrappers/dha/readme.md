# DHA (Dubai Health Authority) scraper

Scrapes the healthcare vacancies published on the **Dubai Health Authority's
Sheryan Opportunities board** —
<https://services.dha.gov.ae/sheryan/wps/portal/home/opportunities> — where
DHA-licensed healthcare facilities in Dubai advertise openings to licensed
health professionals.

> **About the requested URL.** This scraper was requested for
> `https://careers.dha.gov.ae`, which does **not exist** (NXDOMAIN on public
> DNS) — DHA runs no careers host of its own, and `dha.gov.ae` has no careers
> section. The two live DHA job sources are the Sheryan board above (scraped
> here) and the Dubai Government portal `jobs.dubaicareers.ae`, whose Dubai
> Health organisation is already covered by [`../dubaihealth`](../dubaihealth).
> Sheryan is the healthcare-specific one and does not overlap with it.

## Data source

Sheryan is an IBM WebSphere portal. Its Opportunities portlet renders its
cards client-side from one public, token-free JSON resource URL:

- **Listing**: `GET <base>/p0/IZ7_…=CZ6_…=NJsearchOpportunities=/`
  `?opportunitiesSearchVO={"string":"","vacancyType":[],"category":[],`
  `"facilityName":[],"nationality":[],"locale":"en","pageSize":100,"pageNo":1}`
  → `{"opportunitySummaryResponseDTO": "<json string>"}` carrying
  `opportunities[]`, `filters` and `pagination`. The server caps `pageSize`
  at **100** (185 open jobs ⇒ 2 pages), and returns them newest-first.
  Each row has `jobID`, `jobTitle`, `vacancyType` (Medical/Admin), `category`,
  `facilityID`, `facilityName`, `national` and an `interested` counter.
- **Detail** (on by default, `--no-details` to skip):
  `…=MEjobId!<id>=locale!en=action!viewOpportunityDetails==/?jobId=<id>&locale=en`
  embeds the description and the requirements list as JS string literals
  (`var description = '…'`, `var jobRequirement = '[…]'`) plus the facility's
  contact email and phone numbers.
- `<base>` carries WebSphere's `!ut/p/z1/…` navigation-state token, so
  **nothing is hardcoded**: the scraper reads the token and the portlet paths
  off the landing and results pages at startup (2 bootstrap requests) and
  aborts loudly if their shape changes.
- **robots.txt**: `services.dha.gov.ae/robots.txt` returns portal HTML, i.e.
  no robots file and nothing disallowed. Checked at startup every run.

## Quirks

- **No posting dates.** The board publishes none, anywhere. So there is no
  time window to apply (master spec §4): the board lists only *currently
  open* opportunities, the first run keeps all of them, and later runs are
  incremental purely through dedup on `job_id`. The rich CSV records
  `first_seen_date` (the date this scraper first saw the job) and leaves
  `posted_date` empty; the club CSV's `posted_at` uses `first_seen_date`,
  which is the closest honest value — no date is ever invented.
- **Early stop.** Job ids are sequential (`OPP-<year>-<counter>`) and returned
  newest-first, so pagination stops at the first page holding only known jobs.
  `--full` walks every page (used for the initial crawl).
- **No salary** is published → `salary_raw = "Not Disclosed"`, all numeric and
  club salary fields blank.
- **Healthcare filter** is satisfied at the source: every posting is a job at
  a DHA-licensed healthcare facility. The portal's own `category`
  (Physician / Dentist / Nurse and Midwife / Allied Health / T&CM) is
  authoritative — it comes from the licence register and is set on every
  `Medical` vacancy — and maps onto the club enum: allied health has no club
  bucket and lands in `non_clinical` (as in `export_club_csv.py`), T&CM
  follows the project's "AYUSH/Alternative Therapy → doctors" convention.
  One title overrides it: **Pharmacist**, which the portal files under
  "Allied Health" and which would otherwise lose the `pharmacists` bucket.
  Titles otherwise decide only where the portal is silent — its `Admin`
  vacancies, all of which are `non_clinical`. Trusting the regulator's bucket
  over the title is what keeps e.g. "Clinical Psychologist" and "Speech and
  Language Pathologist" out of `doctors`. Anything unmappable is **kept** as
  `non_clinical` and flagged `needs_review` → `needs_review.csv` (spec §2).
- **No structured location / schedule / experience.** These are read out of
  the free text and only when it says so explicitly: `city` falls back to
  Dubai (DHA licenses Dubai facilities) but honours a stated emirate — a few
  facilities advertise Abu Dhabi or Sharjah branches; `job_type` is
  `full_time` unless the posting is unambiguously part-time or remote ("PART
  TIME OR FULL TIME" stays `full_time`, the wider offer); experience is
  parsed from phrasings like "at least 4 year experience" and left blank
  otherwise.
- **Facility names** come from the licence register, shouty and with legal
  suffixes (`BELLA ROMA SPECIALTY HOSPITAL L.L.C`); they are re-cased and
  stripped for `company` (`raw_facility_name` keeps the original) and become
  the club `company_name`. Pharmacies, labs and diagnostic centres map to
  club `company_type = pharma`, every other facility to `hospital`.
- **Applying** is by phone/email to the facility, not through a form — the
  contacts are captured in `contact_email` / `contact_phones`, and
  `application_url` is the public job-detail page.

## Usage

```bash
python scraper.py                 # incremental daily run (details on)
python scraper.py --full          # walk every page, no early stop
python scraper.py --no-details    # listing fields only (fast)
python scraper.py --limit 5       # test run (5 new jobs)
python test_filters.py            # 33 offline unit tests
```

Outputs: `dha_jobs.csv` (rich cumulative store, dedup key `job_id`) and
`../../jobs_csv/<DD-MM-YYYY>/dha.csv` (HealthCareers.club 22-column schema).
Running twice in a row adds 0 rows.

Etiquette: descriptive User-Agent, ≥1 s between requests, retries with
exponential backoff (3 s → 24 s) on 429/5xx, and per-field/per-page failures
are logged and skipped rather than crashing the run.
