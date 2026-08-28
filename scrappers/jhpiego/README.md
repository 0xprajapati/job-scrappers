# jhpiego — Jhpiego careers (jobs-jhpiego.icims.com)

Jhpiego is the Johns Hopkins–affiliated global-health NGO. The board (65
postings on the 2026-08-28 probe) is heavily African/Asian/LatAm country
offices: TB/CPHC program officers in India, épidémiologie/surveillance
advisors in francophone Africa, global-health-security roles in
Guatemala — plus the out-of-scope back office (finance, HR, procurement,
drivers). Title-only in-scope measured **6%** on the probe sweep, an
undercount — the health signal lives in the descriptions, which is why
every new id's description is fetched before the classifier rules.

## Data source

The board runs on the **iCIMS** ATS. robots.txt advertises the sitemap
and allows `/jobs/<id>/<slug>/job` detail paths while disallowing the
referral/login/candidate/reminder and `/connect` paths — never touched
(`fetch()` hard-asserts it, and a slug merely containing a disallowed
token is skipped loudly):

```
https://jobs-jhpiego.icims.com/sitemap.xml     (65 job URLs)
    -> /jobs/<numeric id>/<title-slug>/job     (id = stable job id)
```

Detail pages are fetched with **`?in_iframe=1`** (serves the full posting
without the portal chrome). Each embeds one schema.org **JobPosting
JSON-LD** block: `title`, full HTML `description`, exact `datePosted`, a
`jobLocation` list with real ISO alpha-2 `addressCountry` + city
`addressLocality`, `occupationalCategory` (Jhpiego's hire-type tag,
"Local"), `employmentType` (the constant "OTHER" — the page's own
"Employment Status" header field is parsed as the truthful supplement).

Flow: fetch the sitemap (1 request) → skip ids already in the rich CSV /
seen_old_ids.csv / out-of-scope.csv (known ids cost zero requests) →
fetch detail pages for new ids only, highest id first (~newest first) →
parse JSON-LD (`raw_decode(strict=False)`, the iconplc hygiene) →
time-window on `datePosted` → shared classifier → append. Steady state ≈
1 sitemap request + 1 detail request per genuinely new posting.

## Quirks (all probe-verified 2026-08-28)

* The sitemap's `<lastmod>` is a modification stamp, **not** the posting
  date — ignored (the iconplc lesson holds on iCIMS). `datePosted` lives
  only in the detail JSON-LD, so each new id costs one detail fetch
  before its window can be judged — out-of-window ids land in
  `seen_old_ids.csv` and are never fetched again.
* `validThrough` is **exactly datePosted + 1 year on every probed page**
  — an iCIMS auto-stamp, not a real deadline. Captured verbatim, treated
  as synthetic.
* iCIMS writes the literal string **"UNAVAILABLE"** in unused address
  fields (streetAddress, addressRegion, postalCode) — never read as
  data.
* ~20 of the 65 postings are **wholly French or Spanish** (francophone
  Africa, Guatemala). Kept-and-flagged, never dropped on language: the
  undp stopword heuristic (`looks_non_english`) forces `needs_review`.
  Classifier inputs are **accent-folded** (`fold_diacritics`) so
  "Surveillance Épidémiologique" can reach the English keyword engine —
  pure normalization, the vocabulary stays the shared classifier's.
* `occupationalCategory` ("Local") is a hire-type tag, not a role signal
  — captured as `hire_type` in the rich CSV, **not** passed to the
  classifier (title + description only).
* No `baseSalary` anywhere; the schema.org shape is parsed if it ever
  appears. Absent → `salary_raw "Not Disclosed"`, blank club salary
  columns. Only USD/INR annual|monthly pay is club-exportable.
* 403/404 are permanent — never retried. Throttle ≥1.5 s/request.

## Known limit — description-only Public Health

The current `_shared` engine requires Public Health to match in the
**title or skills** (`FAMILY_REQUIRE_TITLE_OR_SKILLS`), and this board
has no curated skills signal. So "Senior Program Officer -TB" /
"Program Officer - CPHC" rows **drop** under today's shared code, even
though the 2026-08-27 scope ruling (taxonomy-classifier-decisions memory:
"Jhpiego TB coordinators ARE in scope") reads them as keepers. They land
**full and reversible** in `out-of-scope.csv`. If `_shared` grows the
ruled NGO-board description-only-PH path, delete `out-of-scope.csv` and
re-run with `--since` to re-adjudicate (the unit test
`test_description_only_ph_drops_under_the_current_shared_engine` will
fail loudly when that happens — that failure is the signal).

## Outputs

* `jhpiego_jobs.csv` — rich cumulative store (dedup key: `job_id`).
* `../../jobs_csv/<DD-MM-YYYY>/jhpiego.csv` — HealthCareers.club
  `CLUB_COLUMNS` schema (imported from `_shared/classification.py`).
* `seen_old_ids.csv` — out-of-window ids (detail-fetch skip list).
* `out-of-scope.csv` — classifier-dropped rows, full and reversible.
* `needs_review.csv` — kept but flagged.

Company is always Jhpiego (club `company_type` "hospital" — the
fleet-wide convention for NGO employers in the hospital|pharma enum).

## Usage

```bash
cd scrappers/jhpiego
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --limit 10      # capped test run
../../.venv/bin/python scraper.py --since 2026-01-01   # backlog sweep
../../.venv/bin/python test_filters.py            # 38 unit tests, no network
```

Time window: first run keeps the last 30 days (NGO postings are
long-lived); later runs use `max(stored posted_date) − 2 days`. The
first run (2026-08-28, `first_run.log`) walked all 65 ids uncapped —
each id is fetched at most once, ever.

Probed and built 2026-08-28.
