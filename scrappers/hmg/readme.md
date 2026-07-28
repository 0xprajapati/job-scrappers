# hmg.com scraper

Scrapes job openings from the careers hub of **Dr. Sulaiman Al Habib Medical
Group (HMG)** — one of the largest private healthcare providers in the Middle
East (hospitals, medical centres, pharmacies and labs across Saudi Arabia and
the UAE).

## Data source

`https://hmg.com/en/careers` (the URL this scraper was requested for) is a dead
SharePoint path — it 404s. The live **Careers** link on hmg.com points at
**https://hmg.elevatus.io/**, a landing page that links out to one
**Elevatus (EVA-REC) career portal per group company**; the hospitals themselves
post on **https://talents.hmg.com/**.

All of those portals are the same Next.js app, fed by Elevatus' public,
token-free JSON API (master spec §1.1 — an underlying JSON API is the preferred
source):

```
GET https://dammam-core-api.elevatus.io/api/v1/jobs
    ?language_profile_uuid=14613182-02fa-449b-b78f-296f9d6be59a  (English)
    &limit=50&page=<0-based>
    headers: Accept-Company: <portal company uuid>
-> results.{jobs[], total, page, pages}
```

`Accept-Company` is mandatory (the API answers `406 "The Accept-Company not
found"` without it) and is **discovered at runtime** from each portal's
`__NEXT_DATA__` (`props.portalLayout.company_uuid.id`), falling back to the uuid
pinned in `PORTALS` if the page shape ever changes.

The listing response already carries the **full `description` + `requirements`
HTML**, category, degree, major, career level, structured `years_of_experience`
and the public apply URL, so there is no detail call — the master spec's
`--enrich` flag does not apply here (`/api/v1/jobs/<uri>` 404s; the job pages
fetch the same listing endpoint client-side).

### Portals scraped (all 8, `--portal <host>` narrows it)

| Portal host | Company | Typical postings |
|---|---|---|
| `talents.hmg.com` | Dr. Sulaiman Al Habib Medical Group | hospitals — physicians, nursing, allied health, Tamheer |
| `hmccareers.elevatus.io` | Habib Medical Centers | consultants / senior specialists |
| `ajajitalents.elevatus.io` | Dr. Abdulaziz Al Ajaji Dental Clinics | dentists, dental assistants |
| `mepcareer.elevatus.io` | Middle East Pharmacy | pharmacists |
| `mdlabcareers.elevatus.io` | MDLAB (diagnostic laboratories) | (currently none open) |
| `taswyatcareers.elevatus.io` | Taswayat Company | (currently none open) |
| `wrass.elevatus.io` | WRASS (group support services) | admin / facilities |
| `talents.cloudsolutions.com.sa` | Cloud Solutions (HMG health-tech) | IT / analytics |

The corporate portal already aggregates the hospital companies (63 postings
spanning 10 internal company uuids); the rest hold their own small sets, and
none of them overlap. `company` is therefore the portal's brand — the API
exposes no company-name lookup for the internal uuids.

## Quirks

* **ATS category is unreliable.** Real clinical roles ("Registered Nurse",
  "Consultant IVF") are filed under the literal category `Default`, so the
  **title decides** the club category and the ATS category (`Physicians`,
  `Doctor`, `Nursing`, `Pharmacy`, `Paramedical`, `Administration`, `Default`)
  is only the fallback (master spec §2).
* **needs_review**: anything that lands in `non_clinical` without a healthcare
  signal in its title / ATS category / major / industry is kept and logged to
  `needs_review.csv` — never dropped. In practice that is the WRASS and Cloud
  Solutions support roles (graphic designer, AC technician, developer). A
  hospital housekeeper tagged with the `Hospital & Health Care` industry passes
  unflagged, which is intended.
* **No salary anywhere.** Every posting carries `salary: {min: 0, max: 0}` →
  `salary_raw = "Not Disclosed"`, numeric fields empty (master spec §3 — never
  invent). `parse_salary()` still normalises a real monthly SAR range if HMG
  ever publishes one.
* **Polymorphic locations.** `location` is a facility label (`name.en`:
  "Jeddah - Al Mohammdiya", "Sewedi - Riyadh"), or `{city, country}`, or
  nothing. `normalize_city()` picks the known city out of the label; postings
  with no location at all fall back to `DEFAULT_CITY = Riyadh` (HMG's
  headquarters) so the club schema always has a city.
* **Missing dates.** A few postings have `posted_at: null`; they are kept
  (never dropped for a missing date) and, being deduped on `uuid`, are added
  exactly once. Their club `posted_at` is blank.
* **Tamheer Program** postings are Saudi government-funded graduate training
  placements. The club `job_type` enum has no internship value, so they map to
  `full_time` and keep the program name in the title.
* Country defaults to Saudi Arabia (`SA`, `+966`) when the posting omits it;
  UAE / Bahrain / Egypt are mapped if they ever appear.
* `robots.txt`: neither the portals nor the API host serve one (every host
  answers the SPA's 404 page) → allowed. Checked at startup for the API host and
  every portal; a portal that ever disallows `/jobs` is skipped, a disallowed
  API path aborts the run.

## Time window & dedup

* **First run**: keeps every open posting (`INITIAL_WINDOW_DAYS = None`) — an
  ATS lists only currently open requisitions and the oldest here has been open
  since 2025-09.
* **Later runs**: master-spec watermark — newest stored `posted_date` minus
  `WATERMARK_GRACE_DAYS = 2`.
* Dedup key `(source, job_id)` where `job_id` is the Elevatus `uuid`. Running
  twice in a row adds 0 rows (verified).

## Usage

```bash
cd scrappers/hmg
../../.venv/bin/python scraper.py                 # full daily run, all portals
```

```bash
../../.venv/bin/python scraper.py --limit 5 --output /tmp/sample.csv --run-date sample-test
```

Flags: `--output` (rich CSV path), `--max-pages N` (per portal), `--limit N`
(total postings), `--portal HOST` (repeatable), `--run-date DD-MM-YYYY`,
`--verbose`.

Tests:

```bash
../../.venv/bin/python test_filters.py
```

## Outputs

* `hmg_jobs.csv` — rich cumulative store (portal, company, city, ATS category,
  career level, major, degree, skills, experience, description, …).
* `needs_review.csv` — postings kept but flagged for a human look.
* `../../jobs_csv/<DD-MM-YYYY>/hmg.csv` — HealthCareers.club 22-column schema.

First full run (2026-07-27): **86 jobs** — 31 doctors, 12 nurses, 2 pharmacists,
41 non_clinical; 9 flagged `needs_review`.
