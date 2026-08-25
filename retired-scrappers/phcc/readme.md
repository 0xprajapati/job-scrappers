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
| No closing date on the portal | no expiry data exported (the club schema retired `expires_at`) |

## Classification

Classification is the shared two-level taxonomy
(`scrappers/_shared/classification.py`): `category` is
`Non Clinical` | `Public Health`, plus a `sub_category` from the 20-name list.
`classify_job(title, skills, description)` makes the only keep/drop decision;
this scraper defines no category regexes or keyword lists of its own.

Everything PHCC posts is healthcare-sector employment, but the great majority
of it is bedside primary care, which is **out of scope** — those rows are
dropped and counted as `excluded_out_of_scope` in the run summary.

The portal's own **Job Category** (`ProfessionalArea`) facet no longer decides
anything. It is passed to the classifier as the curated `skills` signal and
kept verbatim in the rich CSV as `category_original`, a raw source column.
The job requirements are folded into the `description` signal, so rows are
classified **after** the detail fetch.

`needs_review` is the union of two things: the classifier's own flag (in scope
but the title reads like another profession), and the HR-code title unpacking
flag (a reconstructed title a human should confirm).

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

* `phcc_jobs.csv` — rich cumulative store of the **in-scope** vacancies,
  deduped on `job_id` (the `IRC…` requisition code). Re-running adds 0 rows.
* `out-of-scope.csv` — vacancies `classify_job` rejected, archived verbatim
  (same columns) rather than discarded, so any admission decision is
  reversible.
* `needs_review.csv` — rows a human should confirm.
* `../../jobs_csv/<DD-MM-YYYY>/phcc.csv` — HealthCareers.club 22-column schema.

## Run history

* **First run (27-07-2026)** — 15 open vacancies captured (7 physician, 2
  nursing, 1 pharmacy, 5 allied/technical). 1 flagged `needs_review`
  (IRC40694, the HR-code title). Posted dates span 2025-12-17 → 2026-07-23.
  All in Doha, all `full_time`, all salary undisclosed.
* **Taxonomy migration (25-08-2026)** — the stored 15 rows were re-run through
  the shared classifier: **0 kept, 15 moved** to `out-of-scope.csv`. PHCC's
  open board at that point was entirely bedside primary care plus lab/radiology
  technologists. The two `Clinical Coding Officer` rows were the closest calls —
  see the note below.

> **Known classifier gap.** `Clinical Coding Officer` (PHCC's wording for a
> medical coder, and the standard Commonwealth/Gulf title) is *not* matched by
> `_shared/role_families.py`, whose Medical Coding pattern covers
> `medical cod*`, `\bcoder\b` and `coding (specialist|auditor|analyst|manager)`
> but not `coding officer` / `clinical coding`. Both PHCC rows were dropped as
> out of scope for that reason. Fixing it is a deliberate `_shared` change.
