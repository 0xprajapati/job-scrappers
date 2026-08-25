# Taxonomy migration — COMPLETE (2026-08-25), extended 2026-08-26

> **2026-08-26 update.** Four of the excluded scrapers were migrated at the
> user's request: `publichealthcareer`, `jobberman`, `michaelpage`,
> `simplyhired`. The active fleet is now **24 migrated + 3 legacy**
> (apna, nhm, swaasa), with 22 scrapers retired. Fleet verification after
> the change: everything compiles, **1,421 scraper tests + 56 `_shared`
> tests pass, zero failures**.
>
> Per-scraper result of the stored-data reclassification:
>
> | scraper | kept | of | % | top sub-categories |
> |---|---:|---:|---:|---|
> | simplyhired | 111 | 2,011 | 5.5% | Medical Coding 47, Public Health Nutrition 46, IPC 5, MSL 4 |
> | jobberman | 10 | 197 | 5.1% | Public Health 9, Clinical Research 1 |
> | publichealthcareer | 3 | 12 | 25% | Monitoring & Evaluation 2, Public Health Research 1 |
> | michaelpage | 2 | 47 | 4.3% | Regulatory Affairs 1, Pharmacovigilance 1 |
>
> **One pattern worth reusing:** `jobberman` and `michaelpage` can only run
> the scope gate *after* the per-job detail fetch, because the description
> and the exact date live there. Without a skip list they would re-fetch
> every dropped card on every run, so both now record dropped rows in
> `out-of-scope.csv` and consult it before fetching — the same idiom as their
> existing `seen_old_ids.csv`, and reversible because the full row is kept.
> `publichealthcareer` and `simplyhired` need no such list: their listing
> payloads already carry everything the classifier reads.


**All 32 in-scope scrapers were migrated, tested and reclassified.** 13 of
them were retired on 2026-08-25 (decision 5 below), leaving 19 migrated
scrapers in the active fleet. The 17
scrapers in the EXCLUDED list below were deliberately left on the legacy
scheme by the user's instruction.

Verified fleet-wide on completion:

- every `.py` under `scrappers/` compiles;
- **1,431 tests pass, zero failures** after the 2026-08-25 classifier decisions
  (1,467 at migration completion, before the 13 retirements) (note: `apna`, `docthub`, `docthub_roles`,
  `dubailivejobs`, `nextenti`, `publichealthcareer` use bare `test_*` functions
  with a custom runner, so they need `python test_filters.py`, not
  `unittest discover`, which silently reports 0 tests for them);
- no migrated scraper contains the retired enum in logic, defines a local
  `CLUB_COLUMNS`, or lacks the `excluded_out_of_scope` gate;
- 29 migrated scrapers carry an `out-of-scope.csv`; the rest dropped nothing
  or have no stored data;
- every kept row carries a valid `category`: **4,191 kept** (Non Clinical
  3,857 / Public Health 334) vs **18,124 moved** to out-of-scope.

Top sub-categories in the kept set: Clinical Research 969, Medical Coding 890,
Clinical Data Management 681, Pharmacovigilance 497, Regulatory Affairs 305,
Medical Writer 155, MSL 141.

## Decisions — RESOLVED 2026-08-25

All seven are resolved. #7 turned out to be partly a false alarm
(simplyhired's work was never lost); internshala is widened and migrated, and
workindia has been measured and found structurally out of scope.

### Done

1. **"Fire and Safety Officer" is not Pharmacovigilance.** `officer` removed
   from the PV `safety (?:...)` alternative in `_shared/role_families.py`.
   Real PV titles ("Drug Safety Officer", "Pharmacovigilance Officer") still
   match via `drug safety` / `pharmacovigilance`.

2. **Payer-side utilization review is out unless the job also reads as MSL.**
   `utilization (?:review|management)` and `peer reviewer` removed from the
   Medical Reviewer family pattern, so those phrases no longer admit a job on
   their own; anything matching MSL vocabulary is unaffected.
   *Still in scope and NOT changed:* titles literally called "Medical
   Reviewer" at US payers ("Medical Reviewer III (Medicare/DRG)", ~16 himalayas
   rows) — they match the family's core `medical review\w*` term. Ask the user
   before touching that, since removing it would gut the family.

3. **"Clinical Coding Officer" now matches Medical Coding.** Added
   `clinical coding` and `coding (?:...|officer)` to the family pattern and
   `clinical cod(?:er|ing)` / `coding (?:...|officer)` to the sub-category
   titles. Recovers the two dropped PHCC vacancies.

4. **Dieticians are in scope under Public Health → Public Health Nutrition.**
   `dieti[ct]ian` added to the PH family pattern and to the Public Health
   Nutrition sub-category, and the `"clinical dietitian"/"hospital dietitian"`
   vetoes retired from `NEGATIVE_KEYWORDS`. The animal/poultry/cattle/sports
   guards still hold.

5. **The 13 bedside-only ATS boards are retired.** Moved to
   `retired-scrappers/` (see its README for the per-scraper yield table and the
   one-command revival): apollohospitals, carecareers, dubaihealth, fortis,
   gulftalent, hmg, maxhealthcare, medcare, moh, phcc, purehealth, seha, sidra.
   The active fleet is now **19 migrated scrapers + 17 excluded legacy ones**.
   `gulftalent` was additionally a strict crawl-subset of `gulftalent_roles`.

Fleet verification after 1-4: **1,431 tests pass, zero failures.** Two scraper
tests asserted the old dietician behaviour and were inverted
(`dubaihealth/test_filters.py`, `himalayas/test_filters.py`); new tests cover
all four decisions in `_shared/test_role_families.py` and
`_shared/test_taxonomy_keywords.py`.

### Notes

6. **DONE — `pharmarecruiter_roles` crawl bug fixed and its term list
   widened.** The newest-first early stop (`if page_all_old or
   len(posts) < PAGE_SIZE: break`) exited the whole `while True` loop instead
   of advancing `term_idx`, so only the first term ("clinical research") was
   ever searched. It now advances the cursor. In the same change SEARCH_TERMS
   went from 17 queries to 54, to the shine_roles standard: all eleven role
   families (Medical Reviewer previously had **no** query at all) and all ten
   Public Health sub-categories (previously 2 of 10). Every candidate was
   probed live against `X-WP-Total` on 2026-08-25; "heor" (matches "theory"),
   "hmis", bare "hiv" (matches "archive") and the bare acronyms were rejected
   as substring noise, and a test now blocks any unvetted query under five
   characters.

   Measured A/B on the same 2-day window, live:

   | | terms queried | requests | posts scanned | new jobs |
   |---|---|---|---|---|
   | before | 1 | 3 | 100 | 8 |
   | after | 54 | 84 | 3,250 | 14 |

   Run time 3m33s vs 7s. The earlier "~17x crawl" estimate was pessimistic:
   the date watermark bounds each term to about one page per run, so breadth
   costs roughly one request per term rather than a full walk of all 7,335
   job posts.

7. **DONE (internshala) / EVIDENCE FOR A DECISION (workindia).**

   "Fetch-wide / filter-tight" is the house crawl pattern: widen what the
   scraper *asks the site for*, and let `classify_job` do all the rejecting.
   It is a crawl-layer change — it never touches `taxonomy_keywords.py` or
   `role_families.py`.

   * **simplyhired — was never lost.** Its widening is committed in the
     user's own `3c6b264`: `{"q": "healthcare"}` became a 31-entry
     `SEARCH_QUERIES` list plus the per-query pagination loop. The earlier
     claim in this file that a `git checkout` destroyed it was wrong.
   * **internshala — WIDENED AND MIGRATED 2026-08-25.** Now crawls **all 173
     live job categories** (derived from internshala's own
     sitemap-categories.xml + sitemap-virtual-categories.xml, each probed
     live; the 34 dead slugs omitted) instead of 13 hand-picked healthcare
     slugs — 4 of which were themselves dead (`hospitals-healthcare-jobs`,
     `medical-jobs`, `pharma-jobs`, `biotechnology-jobs`). Its category facet
     is far too loose to scope a crawl with: `biostatistics-jobs` returns
     maths teachers, `pharmacovigilance-jobs` returns sales analysts,
     `nurse-jobs` returns biology teachers. It also moved off the legacy enum
     onto `classify_job` + the shared `CLUB_COLUMNS`. Stored data
     reclassified: **50 kept / 478 moved** to `out-of-scope.csv` (Public
     Health Nutrition 14, Medical Coding 12, Clinical Research 9, CDM 4, PV 4,
     RA 3). 35 tests pass.
   * **workindia — widening is NOT worth doing; recommend retiring it.**
     Its daily latest-JD sitemap was pulled live on 2026-08-25: **15,614 job
     URLs, 3,250 distinct title slugs, and essentially nothing in scope.**
     The only candidate slugs were `medical_representative` (15 — pharma
     sales, an explicit negative keyword), `clinical_nurse_specialist` (3)
     and `clinical_pharmacist` (3), both bedside, and ~4 dietician /
     nutritionist posts. Scoring its 924 stored rows through `classify_job`
     gives **9 in scope (1.0%)**. WorkIndia is a blue-collar board — shop
     helper, machine operator, delivery — and structurally does not carry
     clinical-research or public-health professional roles. **Retired
     2026-08-25** at the user's instruction — moved to `retired-scrappers/`,
     where its 27 tests still pass. It stays on the legacy profession enum;
     migrating it was never worth doing.

---

# Original status & resume plan (kept for history)

## EXCLUDED from the migration — THE authoritative list

By the user's instruction these scrapers are out of scope for this migration.
They keep their **old per-scraper classification** (the legacy profession enum
`doctors | nurses | pharmacists | non_clinical`) and their **old club schema**.
**3 remain:**

```
apna          nhm          swaasa
```

`apna`'s parser is broken (it writes no club export at all), `nhm` returns
0 rows as its steady state, and `swaasa` scored 5.4% live and has not been
asked for.

**Migrated 2026-08-26** at the user's instruction: `publichealthcareer`,
`jobberman`, `michaelpage`, `simplyhired` — see "Migrated 2026-08-26" below.
`hziegler` and `profco` were considered in the same batch and **deliberately
skipped**: both were retired the day before on live scores (hziegler 0/61,
profco 0.5%), and migrating them would have meant reviving them.

(History: on 2026-08-25 `internshala` was widened and migrated; `workindia`,
`pharmabharat` and seven low-yield sources — kfshrc, narayanahealth, profco,
dubailivejobs, dubizzle, hziegler, zulekhahospitals — were retired to
`retired-scrappers/`. `pharmabharat` is **permanently ignored** by user
decision despite scoring 78%; do not propose reviving it.)

Do not migrate the three above, do not reclassify their stored data, and do
not list them as outstanding work. `pharmabharat` was additionally reverted to its
pre-migration state (`git checkout`) after an agent had begun editing it;
verified compiling, its original 45 tests passing, no shared classifier import.

**This file is the single place that names the excluded set.** Other docs
(README.md, SCRAPER_GUIDE.md, docs/HOW-SCRAPERS-WORK.md,
instructions/master-scraper-spec.md, prompts/master-prompt.md) must point here
rather than repeat the list — one hand-maintained copy, not six.

Everything not listed above is either migrated or still owed the migration; the
sections below are the worklist.

The fleet-wide migration to the two-level taxonomy (see
`taxonomy-migration-spec.md` — the binding contract) was interrupted mid-run
when the account hit its monthly spend limit. Every file still compiles;
nothing is lost. This file is the worklist for the resumed run.

## Reference implementation

`git diff -- retired-scrappers/apollohospitals/scraper.py` is the canonical
pattern (the scraper is retired, but the diff is still the reference):
imports + `apply_classification(row)` helper + RICH_COLUMNS additions +
CLUB_COLUMNS import + club-row changes (sub_category, qualification, no
is_active/expires_at) + `excluded_out_of_scope` counter/gate/summary line.
Replicate it. `_shared/classification.py` is finished and tested
(`_shared/test_classification.py`, 9 passing).

## FULLY DONE (code + tests + README + stored-data reclassification)

Completed and verified 2026-08-25 by the surviving board-API coordinator:

- **himalayas** — 75 tests pass; store 1030 kept / 95 moved (out-of-scope.csv
  now 7037 rows); slugs passed as skills; --reclassify uses classify_job.
- **docthub** — 8 tests pass; 21 kept / 1714 moved; gained its first club
  export + needs_review.csv; EXCLUDE_CATEGORIES narrowed to crawl-side only.
- **docthub_roles** — verified migrated; 8 tests pass; no stored CSV exists.
- **naukrigulf** — 31 tests pass; 32 kept / 1304 moved; IndustryType/
  FunctionalArea passed as skills.
- **vaidyog** — 26 tests pass; 0 kept / 106 moved (all bedside/admin).
- **nextenti** — 9 tests pass; 1 kept / 195 moved.

Completed 2026-08-25 (Oracle ORC group):

- **medcare** — 19 tests pass; 2 kept / 19 moved; ATS RequisitionType facet +
  parsed department passed as skills; StudyLevel captured as `study_level`.
- **purehealth** — 33 tests pass; 0 kept / 14 moved (all bedside/allied).
- **seha** — 34 tests pass; 1 kept / 145 moved; Category facet + JobFunction
  passed as skills.
- **sidra** — 37 tests pass; 0 kept / 36 moved; Category facet +
  RequisitionType + JobFunction passed as skills; talent-pool rows still force
  needs_review.

⚠️ Decision item from that run: the shared classifier READMITS US payer-side
noise on himalayas that the old local list excluded ("Utilization Review
Nurse", "Director of Utilization Management", IME/disability "Physician
Reviewer" → Medical Reviewer/MSL, ~24 rows). If unwanted, add negative
keywords (utilization review/management, disability peer review, "field
medical director") to _shared/taxonomy_keywords.py — a deliberate _shared
change, get user confirmation or note it prominently.

## CODE DONE (imports classification, no old enum; still needs: verify tests,
README, and stored-data reclassification)

apollohospitals, carecareers (has out-of-scope.csv), dha, fortis, foundit,
indeed, naukri, naukri_roles

## PARTIAL (code half-edited — finish these first)

- **shine** — docstring/imports/queries migrated, but the old
  `classify_category` (old enum) still exists near line 292 and the club row
  near line 632 still defaults `category` to `non_clinical`; main-loop gate,
  RICH_COLUMNS, club columns unverified.
- **gulftalent_roles** — imports migrated; 1 old-enum hit; confirm the
  `_PHARMA_RE` NameError fix landed (classify_company_type must not call an
  undefined regex).
- **shine_roles** — 1 old-enum hit remains; otherwise close (was already
  role_families-based); needs family→role_family + sub_category + shared
  CLUB_COLUMNS.

## TODO (untouched — full migration per spec)

Excluded scrapers have been removed from this list; see the EXCLUDED section.

PeopleStrong sibling of carecareers: maxhealthcare
Other ATS: hmg, manipalhospitals
Boards/WordPress: pharmarecruiter, pharmarecruiter_roles
SSR/browser: reed
HTML/JSON-LD: freshersworld, gulftalent
Gov/special: dubaihealth, moh, phcc

## Data reclassification (every migrated scraper, including the DONE ones)

Per the spec's "Stored-data reclassification": rewrite each `<site>_jobs.csv`
with the new columns; out-of-scope rows move to `<site>/out-of-scope.csv`.
A single central throwaway script over all rich stores is the efficient way
(title/skills/description column names vary per scraper). Do not write
`jobs_csv/<date>/` files during migration.

## Docs

- DONE (verify): README.md, SCRAPPERS.md, instructions/master-scraper-spec.md
  edited; instructions/export_club_csv.py deleted; jobslly deleted everywhere
  in scrappers/.
- DONE 2026-08-25 (second pass): docs/HOW-SCRAPERS-WORK.md finished,
  SCRAPER_GUIDE.md post-migration pass, scrappers/_shared/README.md now
  documents classification.py + CLUB_COLUMNS, SCRAPPERS.md totals corrected to
  the 49 real scraper folders, prompts/master-prompt.md stale path note removed,
  jobslly gone from every *.md outside explicitly historical notes.
- Every doc now describes the two-level taxonomy as the standard for migrated
  scrapers and mandatory for new ones, and points HERE for the excluded set.

## Final verification checklist (run after all migrations)

1. `find scrappers -name "*.py" ! -path "*_shared*" -exec .venv/bin/python -m py_compile {} +`
2. Run every scraper's test file with the venv python.
3. `grep -rn '"doctors"\|"nurses"\|"pharmacists"\|"non_clinical"' scrappers/`
   → outside the EXCLUDED scrapers, only raw-source-data column names or
   historical comments may remain.
4. Confirm every non-excluded scraper imports classification and uses
   CLUB_COLUMNS.
5. Confirm out-of-scope.csv exists wherever rows were dropped.
