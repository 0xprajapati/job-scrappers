# PHCC — Primary Health Care Corporation, Qatar (`phcc.gov.qa`)

Scrapes the open vacancies of Qatar's public primary-care provider.

## ⚠️ robots.txt: this scraper is disabled by default

`careers.phcc.gov.qa/robots.txt` publishes:

```
User-agent: *
Disallow: /
```

The master spec (§1, §7) makes robots.txt binding, so **`scraper.py` aborts on
startup** and scrapes nothing:

```
robots.txt at https://careers.phcc.gov.qa/robots.txt disallows
https://careers.phcc.gov.qa/OA_HTML/IrcVisitor.jsp — aborting.
```

Passing `--ignore-robots` overrides that and logs a loud warning on every run.
**Use it only with authorization from PHCC.**

Context for whoever makes that call: the file is Oracle E-Business Suite's
*stock shipped* `robots.txt`, and its own header says "Do not edit settings in
this file manually. They are managed automatically and will be overwritten when
AutoConfig runs" — so it plausibly reflects an Oracle default rather than a
deliberate PHCC decision. It is nonetheless what the site publishes today.

## Data source

`www.phcc.gov.qa` hosts **no vacancies**. Its `/careers` page is a landing page
whose only outbound link is PHCC's **Oracle EBS 12.2 iRecruitment** visitor
portal at `careers.phcc.gov.qa`. That portal has no JSON API (EBS renders
server-side OAF HTML), no sitemap, and no JobPosting JSON-LD, so the listing is
driven the way a visitor drives it:

1. `GET /OA_HTML/IrcVisitor.jsp` → follow the **Job Search** (`VisJobSchPG`) link.
2. `POST` the search form. It **rejects an empty search** ("Please fill in
   search criteria values in at least one of the following fields: Job
   Category"), but `ProfessionalAreaCode` is a *multi*-select — so submitting
   all 17 categories at once is the portal's own "match anything".
3. Parse the OAF result table. Cells carry stable span ids
   (`JobSearchTable:<Field>:<rowIndex>`), which is what the parser keys on.

Each row embeds `ppVacancyId=<n>`, giving a **session-free, bookmarkable**
detail URL used as both `job_url` and the description source:

```
https://careers.phcc.gov.qa/OA_HTML/OA.jsp?OAFunc=IRC_VIS_VAC_DISPLAY&p_svid=<n>&p_spid=0
```

## Quirks

| Quirk | Handling |
|---|---|
| Search form refuses an empty query | All 17 `ProfessionalAreaCode` options submitted together |
| OAF hidden fields (`_FORM`, `_AM_TX_ID_FIELD`, `FORM_MAC_LIST`) are per-render MAC tokens | Re-read from the page about to be submitted; never replayed |
| `IrcAction` carries a per-render token (`'IrcAction':'GompHDhtJ3'`) | Scraped off the Search button's `onclick` each run |
| `p_spid` must be present and integer-parseable | Hard-coded to `0`; omitting it yields "page is no longer active" |
| "Detached table" renders all rows at once but shows a "1-10 of 15" navigator | All rendered rows parsed; the `goto` navigation event is replayed only if fewer rows arrive than the reported total |
| ~4 of 15 vacancies have every description section blank | Empty `description` kept — never fabricated |
| Some titles are raw HR position paths (`063.Operations.Allied Health.Technologist`) | Unpacked to `Allied Health Technologist` **and** flagged `needs_review` |
| Some organizations carry a cost-centre code (`080000000.Operations`) | Prefix stripped → `Operations` |
| One vacancy lists location as bare `QA` | Falls back to `Doha` |
| `Minimum 1 year of internship/training` sits next to `Minimum 3 years of experience` | Training/residency/probation spans are discarded; only the experience bar is recorded |
| No salary anywhere on the portal | `salary_raw = "Not Disclosed"`, numeric salary fields blank |
| No closing date on the portal | `expires_at` blank |

## Healthcare filter & categories

Everything PHCC posts is healthcare-sector employment, so nothing is dropped.
Classification uses the portal's own **Job Category** facet (a source-side
field, which master spec §2 prefers over title guessing); it is populated on
every row. The title only *overrides* the facet where it is unmistakable — a
"Consultant Radiologist" filed under Radiology is a doctor, not a technologist.

Bare seniority words (`Consultant`, `Specialist`) are deliberately **not**
doctor keywords, because PHCC titles corporate roles that way too.

An unrecognised facet (PHCC adds a new Job Category) falls back to the title
classifier and flags the row `needs_review`. Nothing is ever silently dropped.

| Portal Job Category | Club category |
|---|---|
| Physicians, Dentist | `doctors` |
| Nursing | `nurses` |
| Pharmacy | `pharmacists` |
| Lab, Radiology, Dental Health, Other Allied Health Services, HIM, Administration & Support Services, Corporate Communications, Engineering, Executive Leadership, Governance, ICT, Supervisory, Technical Administration | `non_clinical` |

## Time window

An ATS lists only **open** vacancies (PHCC keeps some live for 7+ months), so
`INITIAL_WINDOW_DAYS = None` — the first run keeps all of them. Later runs use
the master-spec watermark: newest stored `posted_date` minus
`WATERMARK_GRACE_DAYS = 2`. Vacancies with an unparseable date are kept, not
dropped.

## Usage

```bash
python scraper.py --ignore-robots
```

| Flag | Effect |
|---|---|
| `--ignore-robots` | Required — otherwise the run aborts (see above) |
| `--output PATH` | Rich cumulative CSV (default `phcc_jobs.csv`) |
| `--limit N` | Process at most N vacancies (test runs) |
| `--max-pages N` | Stop after N result blocks (test runs) |
| `--no-details` | Skip per-job detail fetch (drops descriptions and experience) |
| `--run-date DD-MM-YYYY` | Target `jobs_csv/<date>/` folder (default: today) |
| `--verbose` | Debug logging |

Unit tests (no pytest needed):

```bash
python test_filters.py
```

## Outputs

* `phcc_jobs.csv` — rich cumulative store, deduped on `job_id` (the `IRC…`
  requisition code). Re-running adds 0 rows.
* `needs_review.csv` — rows a human should confirm.
* `../../jobs_csv/<DD-MM-YYYY>/phcc.csv` — HealthCareers.club 22-column schema.

## First run (27-07-2026)

15 open vacancies: 7 `doctors`, 5 `non_clinical`, 2 `nurses`, 1 `pharmacists`.
1 flagged `needs_review` (IRC40694, the HR-code title). Posted dates span
2025-12-17 → 2026-07-23. All in Doha, all `full_time`, all salary undisclosed.
