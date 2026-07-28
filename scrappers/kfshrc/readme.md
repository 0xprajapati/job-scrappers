# kfshrc.edu.sa scraper

Scrapes job openings from
[kfshrc.edu.sa/en/careers/jobs-listing](https://www.kfshrc.edu.sa/en/careers/jobs-listing)
— **King Faisal Specialist Hospital & Research Centre (KFSH&RC)**, Saudi
Arabia's tertiary/quaternary referral hospital and biomedical research centre,
with campuses in Riyadh, Jeddah and Madinah.

## Data source

The jobs-listing page ships an empty `<div class="job-list">` and fills it
client-side. Its widget config sits in the page itself
(`<div id="card" data-rfkid="rfkid_16" data-entity="job" data-sources="1201178"
data-fields="...">`), and the theme bundle
(`/-/media/themes/kfshrc/.../pre-optimized-min.js`) posts that config to a
first-party proxy in front of **Sitecore Search**:

```
POST https://www.kfshrc.edu.sa/kfhapis/CustomSearch/GetJobSearchResults
{"rfkId":"rfkid_16","entity":"job","sources":["1201178"],
 "fields":["id","jobtitle","departmentsection","location","branch","applyby",
           "duties","experience","education","otherrequirements","startdate",
           "summary"],
 "country":"us","language":"en","sortField":"recently_posted",
 "facetLocations":"All","limit":50,"offset":0}

-> {"widgets":[{"total_item":24,"limit":50,"offset":0,"content":[ … ]}]}
```

- **No detail request is needed.** The listing carries the complete posting —
  title, department/section, branch, audience, posting date, apply-by date and
  the full HTML of summary / duties / education / experience / other
  requirements. Querying the same endpoint with `jobID=<id>` (what the detail
  page does) returns the byte-identical record, so one paged listing call per 50
  postings is the entire crawl.
- **Job URL**: `/en/careers/jobs-listing/job?id=<joben…>` — a real per-job deep
  link, used as `application_url`.
- **Sorting**: `sortField=recently_posted` gives newest posting date first, so
  the daily run can stop as soon as a page is entirely older than the cutoff.
- **robots.txt**: kfshrc.edu.sa disallows only `/intranet/*` and its `/en/`,
  `/ar/` variants; `/kfhapis/` and `/en/careers/` are allowed. Checked at
  startup, and the run aborts if either target turns into a disallow.

## Quirks

- **A rejected payload returns HTTP 200 with an EMPTY body**, not an error. One
  unknown name in `fields` — `applyurl`, `jobrole` and `posteddate` are all
  unindexed for this entity — or a blank `sortField` empties the response
  entirely. So `API_FIELDS` holds exactly the 12 indexed names, and an
  empty/unparseable body is retried like a 5xx rather than read as "no jobs".
  The same empty body shows up occasionally on a transient upstream hiccup,
  which the retry also covers.
- **Past the last page `content` is `null`**, not `[]` — normalised to `[]`.
- **Dates are Microsoft-JSON epochs stored at midnight Riyadh time**
  (`/Date(1785013200000)/` = 2026-07-25T21:00Z = 2026-07-26 00:00 +03), so
  they are converted in **UTC+3**. Parsing in UTC would date every posting one
  day early. `startdate` → `posted_date`, `applyby` → `expires_at`.
- **`location` is not a place** — it holds the audience, `External Job` or
  `Internal Job` (internal postings are open to current KFSH&RC staff only).
  The place is `branch` (Riyadh / Jeddah / Madinah), which becomes `city`. Both
  audiences are kept by default and tagged in the `audience` column;
  `--external-only` drops the internal ones.
- **Titles and departments are stored in ALL CAPS.** `raw_title` /
  `raw_department` keep the source verbatim; `title` / `department` are
  title-cased for the board, preserving KFSH&RC grade numerals ("Staff Nurse
  I", "Polysomnography Technologist II") and clinical acronyms (ICU, MRI, ENT,
  …). Already-mixed-case input is left untouched, so a CMS change won't be
  re-mangled.
- **Department strings carry internal bookkeeping**: `(Sec)` / `(Dpt)` /
  `(Unit)` markers and a trailing branch letter — `CASE MANAGEMENT (Sec)-R` →
  `Case Management`; two-level values keep both levels —
  `ADMIN (Sec)-R/BILLING & ACCOUNTS RECEIVABLE (Dpt)-R` →
  `Admin / Billing & Accounts Receivable`.
- **No salary data** anywhere on the portal → `salary_raw = "Not Disclosed"`,
  numeric and club salary fields blank (never invented, master spec §3).
- **Some postings carry title/branch/dates only.** 3 of the 24 live postings
  have no summary, duties, education or experience text at all; those rows get
  an empty `description` and blank experience rather than a guess.
- **Source text has glued words** — `"training inspecialty"`,
  `"Bachelor’sDegree"` — because the CMS strips the line breaks of the
  underlying Word documents. This is left as-is: any splitting heuristic
  general enough to fix `Bachelor’sDegree` also breaks `PhD` and `Pharm.D.`.
  The one text repair applied is narrow: the CMS mangles some curly
  apostrophes into `?`, so `Bachelor?s` → `Bachelor’s` (possessive-`s` only, a
  genuine question mark survives).

## Healthcare filter & category mapping

Everything KFSH&RC posts is healthcare-sector employment, so **nothing is
dropped** — the classifier only picks the club bucket
(`doctors | nurses | pharmacists | non_clinical`), reading the title *and* the
department (grades like `STAFF NURSE I` carry no specialty, and `LOCUM TENENS`
carries no discipline). Decision order:

1. pharmacy → `pharmacists`
2. nursing / midwifery → `nurses`
3. allied-health & technical (technologist, therapist, sonographer,
   polysomnography, …) → `non_clinical`
4. corporate / administrative / academic-research (billing, accounts, IT,
   quality, professor, case management, …) → `non_clinical`
5. medical-staff rank or physician specialty → `doctors`
6. generic support role (assistant, coordinator, manager, …) → `non_clinical`
7. anything left → `non_clinical` **+ `needs_review`**, logged to
   `needs_review.csv` (master spec §2 — never silently dropped)

Steps 3 and 4 deliberately run *before* step 5: at KFSH&RC "Consultant",
"Associate/Assistant Consultant", "Specialist" and "Registrar" are **medical
grades**, and head-office posts reuse the same words
("Consultant, Information Technology"), so the unambiguous discipline keyword
has to win first. "Locum Tenens" is classified as `doctors` — its own posting
requires graduation from an accredited medical school. All 24 live postings
classify without a flag.

## Experience parsing

KFSH&RC states requirements in a rigid house style that spells the number and
repeats it in digits, listing alternative qualification pathways:

> "Two (2) years of related experience with Master's, or four (4) years with
> Pharm.D./Bachelor's Degree is required." → `min 2`, `max 4`

So the lowest figure is the entry bar and the highest the published upper
bound (blank when the posting states one figure). Only sentences about
experience or training are read, and a figure introduced by a look-back or
ceiling phrase is ignored — `"for the last four (4) years"` is an appraisal
window and `"less than one (1) year of experience"` a programme threshold,
neither a requirement.

## Time window & dedup

- The portal lists only **open** postings (24 at the time of writing, the
  oldest posted in February), so the first run keeps all of them:
  `INITIAL_WINDOW_DAYS = None`.
- Later runs use the master-spec watermark — newest stored `posted_date` minus
  `WATERMARK_GRACE_DAYS = 2` — and stop paginating once a page is entirely
  older than the cutoff.
- Dedup key: `job_id` (the feed's `joben…` id). Running twice in a row adds 0
  rows.

## Outputs

| file | contents |
| --- | --- |
| `kfshrc_jobs.csv` | rich cumulative store, 27 columns (see `RICH_COLUMNS`) |
| `../../jobs_csv/<DD-MM-YYYY>/kfshrc.csv` | HealthCareers.club 22-column schema |
| `needs_review.csv` | titles the classifier couldn't place (only written when non-empty) |

`is_active` in the club CSV is `false` when the stored `applyby` date has
passed (the portal normally drops closed postings, but a stored row can age out
between runs).

## Usage

```bash
python scraper.py
```

Test/inspection runs:

```bash
python scraper.py --limit 5 --output /tmp/sample.csv --run-date sample-test
```

Flags: `--output` (rich CSV path), `--max-pages N`, `--limit N`,
`--external-only` (skip staff-only postings), `--run-date DD-MM-YYYY`,
`--verbose`.

Unit tests (parsers, classifier, cutoff logic — every example copied from a
real posting):

```bash
python test_filters.py
```

Dependencies: `requests`, `pandas` (shared venv at the repo root: `.venv`).
