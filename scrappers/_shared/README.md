# `_shared/` — the one classifier every scraper must use

Three modules live here. Only one of them is a scraper-facing API:

| Module | Role |
|---|---|
| **`classification.py`** | **The mandatory entry point.** `classify_job`, `extract_qualification`, `CLUB_COLUMNS`. |
| `role_families.py` | Engine: weighted in-scope scoring across the eleven role families. |
| `taxonomy_keywords.py` | Engine: negative-keyword veto + the finer sub-category split. |

Scrapers import **`classification.py` only** — never `role_families` or
`taxonomy_keywords` directly, and never a local copy of either's regexes.

> This is the standard for migrated scrapers and mandatory for new ones. A set
> of scrapers has **not** been migrated and still runs its own classifier on
> the legacy profession enum (`doctors | nurses | pharmacists | non_clinical`)
> with the older club schema; `instructions/taxonomy-migration-status.md` is
> the authoritative list.

## `classification.py` — the contract

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

verdict = classify_job(title, skills, description)   # title required
if not verdict["in_scope"]:
    counters["excluded_out_of_scope"] += 1
    continue                       # dropped — never exported
```

`classify_job(title, skills="", description="")` returns a dict:

| Key | Type | Meaning |
|---|---|---|
| `in_scope` | bool | `False` → **drop** the job, count it as `excluded_out_of_scope` |
| `category` | str | `"Non Clinical"` or `"Public Health"` (`""` when out of scope) |
| `sub_category` | str | one of the 20 sub-category names; `""` only for a Public Health job the finer split could not place |
| `sub_category_basis` | str | `"title"` \| `"skills"` \| `"family"` \| `""` — what decided the sub-category |
| `role_family` | str | the winning role family — rich-CSV trace |
| `needs_review` | bool | in scope, but the title reads like a different profession → **keep AND flag** into `needs_review.csv` |
| `vetoed_by` | str | the negative keyword that blocked the row, else `""` |
| `all_families` / `family_scores` / `family_confidence` / `matched_in` | str | score trace from `role_families` — rich CSV, so every admission stays auditable |

The two-level taxonomy it emits (keyword lists:
`Jobs_keywords/keywords_for_jobs.md`):

| `category` | `sub_category` values |
|---|---|
| Non Clinical | Clinical Data Management · Clinical Research · Medical Writer · TMF · Medical Coding · Pharmacovigilance · Regulatory Affairs · Medical Reviewer · MSL · HEOR |
| Public Health | Epidemiology · Public Health Program Management · Monitoring & Evaluation · Community Health · Health Promotion & Education · Disease Programs · Public Health Nutrition · Infection Prevention & Control · Health Informatics & Data · Public Health Research |

The ten Non Clinical family names *are* their sub-categories, so a Non Clinical
job always gets both. Public Health is one family but ten sub-categories, so a
Public Health job whose finer split is undecidable keeps `category` and leaves
`sub_category` blank rather than guessing.

### `CLUB_COLUMNS`

The same module owns the one club-CSV contract — the 23 columns of
`jobs_csv/<DD-MM-YYYY>/<site>.csv`:

```
country_name, country_code, country_dial_code, city_name, company_name,
company_type, company_logo, company_about, title, description, job_type,
category, sub_category, application_url, posted_at, min_experience,
max_experience, qualification, min_salary, max_salary, salary_period,
salary_currency
```

Import the list; never hand-copy it — a copy is how a header drifts. The old
`is_active`/`expires_at` columns are retired (rich CSVs may keep source expiry
data in their own columns). `qualification` is the source's structured field
when it has one, else `extract_qualification(description)` — never inferred.

### `extract_qualification(description)`

Re-exported from `role_families` so everything classification-related comes
from one import. Lifts credentials (MBBS, PharmD, MPH, CPC, …) verbatim.

---

## The engines

`role_families.py` is the single definition of the eleven in-scope role
families. Eleven regexes copy-pasted into four scrapers would drift the first
time one was tuned.

    Public Health · Clinical Data Management · Clinical Research ·
    Medical Writer · TMF · Medical Coding · Pharmacovigilance ·
    Regulatory Affairs · Medical Reviewer · MSL · HEOR

### Scoring, not a yes/no title match

Gating on the job title alone is precise, but it leaves yield behind: in a
307-job Naukri reference set, only **9 rows matched on title alone** and
**106 (35%) had no title match at all**, surfacing purely through skills tags
or the description body. Hence `skills` and `description` are passed to
`classify_job` whenever the source provides them.

Matching those fields naively floods the results with boilerplate
("…supports our clinical research division…"), so fields are weighted and a
job must clear a threshold:

| Field | Weight | Why |
|---|--:|---|
| `title` | 5 | the employer's own name for the role — strongest signal |
| `skills` | 2 | curated tag lists (`pharmacovigilance,drug safety,gcp`) |
| `description` | 1 | free text, weakest and easiest to trip |

`MIN_SCORE_KEEP = 3`. So one title hit qualifies on its own; description text
needs **three distinct** family terms. A keyword scores once per field, so
repeating a term twenty times cannot manufacture a match, and only the first
`DESCRIPTION_SCAN_CHARS` (4,000) are scanned — EEO statements and benefits
blurbs at the foot of a posting name-drop domains for unrelated roles.

Measured against that reference set, the multi-field scorer keeps **252 of
307** rows versus **224** for title-only — about **+12% yield**.

## What every scraper records

Not just the winner, so a questionable row can always be traced. `category`
and `sub_category` go to both CSVs; the trace columns are rich-CSV only:

| Column | Example |
|---|---|
| `category` / `sub_category` | `Non Clinical` / `Pharmacovigilance` |
| `role_family` | `Pharmacovigilance` |
| `all_families` | `Pharmacovigilance\|Clinical Research` |
| `family_scores` | `Pharmacovigilance=19;Clinical Research=3` |
| `family_confidence` | `high` (≥5) / `medium` (≥3) |
| `matched_in` | `title\|skills\|description` |

## Boundaries that took measurement to get right

Each was a real false positive found in a stored corpus:

| Trap | Reality | Handling |
|---|---|---|
| `CDM` | Charge Description Master in revenue-cycle listings | needs a clinical/trial/EDC context |
| `MSL` | also Medical Stop Loss (insurance) | kept, flagged |
| `regulatory compliance` | usually revenue-cycle, not pharma RA | excluded from Regulatory Affairs |
| `CRA` | also EU Cyber Resilience Act | flagged via the engineering deny-list |
| `biostatistics` | pharma biometrics, not public health | excluded from Public Health |

Titles carrying an in-scope term inside a plainly different profession
(counsel, sales, recruiter, engineer) are **kept and flagged**, never silently
dropped — master spec §2.

## Tests

```bash
cd scrappers/_shared
python test_classification.py     # 9  — the classify_job contract
python test_role_families.py      # 38 — the in-scope scoring engine
python test_taxonomy_keywords.py  # the sub-category split + veto list
```

`test_role_families.py` includes one test asserting the set is exactly the
eleven and one proving every family is reachable from a real listing title, so
a declared family can never become unemittable; `test_taxonomy_keywords.py`
asserts every one of the 20 sub-categories is reachable from a canonical title.

The engines are covered here, so a **scraper's** `test_*.py` should not
re-test them — it needs only two wiring tests: an in-scope role gets the right
`category`/`sub_category`, and an out-of-scope title is dropped.
