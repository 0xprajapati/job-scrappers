# Taxonomy migration — COMPLETE (2026-08-25)

**All 32 in-scope scrapers are migrated, tested and reclassified.** The 17
scrapers in the EXCLUDED list below were deliberately left on the legacy
scheme by the user's instruction.

Verified fleet-wide on completion:

- every `.py` under `scrappers/` compiles;
- **1,467 tests pass, zero failures** (note: `apna`, `docthub`, `docthub_roles`,
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

## Open decisions (not actioned — need the user's call)

1. **Reverted work.** `simplyhired`, `internshala` and `workindia` had
   uncommitted "fetch-wide" widening from before the migration; it was
   discarded by the `git checkout` used to revert them when they became
   excluded. Not recoverable from git — but reconstructable from the
   descriptions in SCRAPER_GUIDE.md.
2. **False positive:** "Fire and Safety Officer" scores Pharmacovigilance
   (title-only, high confidence) on the word "safety" — manipalhospitals.
3. **False positive:** US payer-side "Utilization Review / Utilization
   Management / disability peer reviewer" admit as Medical Reviewer/MSL
   (~24 rows on himalayas) — the old local list excluded them deliberately.
4. **False negative:** `role_families.py` Medical Coding does not match
   "Clinical Coding Officer" (standard Commonwealth/Gulf title); two genuine
   PHCC coding vacancies were dropped. Suggested: add `clinical coding`,
   `coding officer`.
5. **Behavior note:** "Clinical Nutritionist / Dietician" is now in scope as
   Public Health → Public Health Nutrition (5 indeed rows). Old indeed list
   excluded it. Confirm this is wanted.
6. **Pre-existing crawl bug (documented, unfixed):** `pharmarecruiter_roles`
   ends the whole crawl instead of advancing its search-term cursor, so only
   the first term is walked. Fixing widens crawl ~17x.
7. **Yield observation:** hospital-operator ATS boards (apollohospitals,
   carecareers, dubaihealth, fortis, hmg, maxhealthcare, medcare, moh, phcc,
   purehealth, seha, sidra, gulftalent) now keep 0–3 rows each — they post
   bedside clinical jobs, which are out of scope by definition. Worth deciding
   which stay scheduled. `gulftalent` is also now a strict crawl-subset of
   `gulftalent_roles`.

---

# Original status & resume plan (kept for history)

## EXCLUDED from the migration — THE authoritative list

By the user's instruction (2026-08-25) these **17 scrapers are out of scope for
this migration**. They keep their **old per-scraper classification** (the
legacy profession enum `doctors | nurses | pharmacists | non_clinical`) and
their **old club schema**:

```
apna          dubailivejobs   dubizzle       hziegler      internshala
jobberman     kfshrc          michaelpage    narayanahealth
nhm           pharmabharat    profco         publichealthcareer
simplyhired   swaasa          workindia      zulekhahospitals
```

Do not migrate them, do not reclassify their stored data, and do not list them
as outstanding work. `pharmabharat` was additionally reverted to its
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

`git diff -- scrappers/apollohospitals/scraper.py` is the canonical pattern:
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
