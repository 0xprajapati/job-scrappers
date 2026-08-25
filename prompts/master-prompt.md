# Master prompt — create or update a job-site scraper

Copy everything below the line, fill in the INPUTS block, and paste it as the task.

---

## INPUTS

```
MODE         = create | update        # create a new scraper, or update an existing one
SITE_NAME    = <short lowercase name, e.g. medindia>
SITE_URL     = <https://... — the job listing/search page, not just the homepage>
NOTES        = <optional: anything you already know — login walls, country focus,
                what to change if MODE=update>
```

## TASK

You are working in the `job-scrappers` repo. Build (or update) the scraper for
**SITE_NAME** so it can run daily and feed the HealthCareers.club pipeline.

**Read these first, in order — they are binding:**

1. `instructions/master-scraper-spec.md` — the project spec (data-source
   discovery order, salary rules, watermark/incremental logic, dedup, output
   contract, etiquette).
2. `scrappers/_shared/classification.py` — the ONE classifier a new scraper
   must use (`classify_job`, `extract_qualification`, `CLUB_COLUMNS`). It
   composes `role_families.py` (weighted in-scope scoring) and
   `taxonomy_keywords.py` (negative-keyword veto + sub-category split).
   Import it; never copy-paste or re-invent regexes or enum lists.
3. `instructions/taxonomy-migration-status.md` — the authoritative list of
   scrapers **excluded** from the taxonomy migration. If MODE=update and
   SITE_NAME is on that list, it deliberately keeps its old per-scraper
   classifier (legacy profession enum) and old club schema: do not migrate it
   as a side effect of whatever you were asked to change. Anything not on that
   list, and everything created with MODE=create, follows the contract below.
4. If MODE=update: the existing `scrappers/SITE_NAME/` folder — its README
   documents the data source and quirks; its `*_scraper.py` config constants
   are the tuning surface. Read both before changing anything.
5. `SCRAPPERS.md` — the site registry table you must keep current.
6. One recent scraper as a style reference: `scrappers/himalayas/` (simple
   HTTP), `scrappers/shine_roles/` (SSR `__NEXT_DATA__`), or
   `scrappers/foundit/` (browser-capture for bot-walled sites).

## SCOPE — what counts as an in-scope job

Two categories, each with ten sub-categories, decided ONLY by
`classify_job(title, skills, description)` from `_shared/classification.py`
(full keyword lists: `Jobs_keywords/keywords_for_jobs.md`):

- **Non Clinical** sub-categories: Clinical Data Management, Clinical
  Research, Medical Writer, TMF, Medical Coding, Pharmacovigilance,
  Regulatory Affairs, Medical Reviewer, MSL, HEOR.
- **Public Health** sub-categories: Epidemiology, Public Health Program
  Management, Monitoring & Evaluation, Community Health, Health Promotion &
  Education, Disease Programs, Public Health Nutrition, Infection Prevention
  & Control, Health Informatics & Data, Public Health Research.

Rules that follow from this:
- Prefer filtering at the source (category facet, department URL) and THEN
  classify with `classify_job` — source filters are recall, the classifier is
  precision. No per-scraper category regexes, enums, or ALLOW/DENY
  classification lists.
- `in_scope == False` → drop the job and count it as `excluded_out_of_scope`
  (print the counter in the run summary). `needs_review == True` → keep the
  row AND append it to `needs_review.csv` — never silently drop it.
- No salary filtering, ever. Capture `salary_raw` verbatim + normalized
  monthly INR min/max when parseable; `"Not Disclosed"` otherwise.

## DISCOVERY PLAYBOOK (MODE=create)

Work down this ladder and stop at the first rung that works. Confirm the
schema on ONE page before writing the scraper.

1. Underlying JSON API (watch network requests on the listing page).
2. SSR payloads: `__NEXT_DATA__`, RSC payload, JobPosting JSON-LD.
3. Sitemap + detail pages (check for a jobs/latest sitemap).
4. HTML card parsing.
5. Browser-capture (see below) — only when every HTTP client is blocked.

**robots.txt is binding.** Fetch it first; honor per-URL disallows even for
APIs you found in devtools. If the crawl target itself is disallowed, stop and
report — do not work around it.

**Bot-walled sites** (403/Akamai/Cloudflare to all HTTP clients): use the
browser-capture pattern from `scrappers/foundit/` — a `capture.js` the user
runs in the real browser plus a localhost `receiver.py` that writes captures
to disk. Budget pacing delays; a full run can take 30–45 min. Some sites are
known dead ends (jadarat.sa 403s everywhere; MNGHA serves a block page as
HTTP 200; Glassdoor is unscrapable) — check the site isn't one before
sinking time in.

**Known traps to test for explicitly:**
- Timezone date bugs: a "posted today" job can be tomorrow/yesterday in local
  time (KFSHRC's Riyadh-midnight trap). Normalize to a single timezone and
  unit-test the boundary.
- Repost re-dating: some boards (Shine) re-date old posts; dedup on the
  stable job id, not the date.
- One agency spamming hundreds of near-identical city-clone posts
  (Freshersworld): note it in the README, consider a dedup-by-fingerprint.

## DELIVERABLES

`scrappers/SITE_NAME/` containing:

- `SITE_NAME_scraper.py` — config constants at top; flow: discover/paginate →
  parse → classify (`classify_job`) → time window (watermark) → dedup →
  append rich CSV → regenerate the club CSV → print run summary (scanned /
  out-of-scope / old / needs-review / new / dupes). CLI: `--output`,
  `--max-pages`/`--limit`, `--verbose`.
- `test_filters.py` — plain-python-runnable tests for the salary parser
  (real examples from this site), date parsing incl. timezone boundary, and
  the cutoff logic. Classifier tests live in `_shared/` — don't duplicate.
- `README.md` — data source, quirks found during discovery, usage, and any
  known failure modes.
- `requirements.txt`.

Then:
- The scraper itself writes `jobs_csv/<DD-MM-YYYY>/SITE_NAME.csv` on the
  22-column club contract (`CLUB_COLUMNS` imported from
  `_shared/classification.py`), regenerated from the full rich store each run.
- Add/refresh the SITE_NAME row in `SCRAPPERS.md` (name, remote?, country).

## VERIFICATION (both modes — do not skip)

1. Unit tests pass (`python test_filters.py`).
2. Sample run capped at 2–3 pages: eyeball every admitted row's
   `role_family`/`matched_in` for false positives, and `needs_review.csv`
   for false negatives.
3. Full first run (or normal incremental run if updating): report the run
   summary numbers.
4. Run the run again immediately: it must add 0 rows (idempotency).
5. Confirm the site's file appears under today's `jobs_csv/` folder with the
   exact `CLUB_COLUMNS` header and valid `category` ("Non Clinical" /
   "Public Health") and `sub_category` values.

## MODE=update SPECIFICS

- State what you changed and why before touching code; keep the change
  minimal — this is a working pipeline.
- Never rewrite or reorder the existing CSV; the watermark and dedup depend
  on it. If a schema change forces a migration, write a one-off migration
  script and keep a `.bak` of the original.
- If tuning the classifier: the change goes in `_shared/`
  (`classification.py` / `role_families.py` / `taxonomy_keywords.py`, with a
  test), never in one scraper's local copy — and re-run other scrapers'
  sample checks, since every migrated scraper shares it.
- Update the folder README with what changed and the date.
