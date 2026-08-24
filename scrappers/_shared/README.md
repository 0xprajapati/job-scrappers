# `_shared/` — the eleven role families

`role_families.py` is the single definition of the in-scope role families,
imported by every re-scoped scraper. Eleven regexes copy-pasted into four
files would drift the first time one was tuned.

    Public Health · Clinical Data Management · Clinical Research ·
    Medical Writer · TMF · Medical Coding · Pharmacovigilance ·
    Regulatory Affairs · Medical Reviewer · MSL · HEOR

These strings are the club CSV's `category` values verbatim (repo README →
*Allowed enum values*), so no scraper has to translate.

## Scoring, not a yes/no title match

The `himalayas` scraper gates on the job title alone. Precise, but it leaves
yield behind: in a 307-job Naukri reference set, only **9 rows matched on
title alone** and **106 (35%) had no title match at all**, surfacing purely
through skills tags or the description body.

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

Not just the winner, so a questionable row can always be traced:

| Column | Example |
|---|---|
| `category` / `role_family` | `Pharmacovigilance` |
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
cd scrappers/_shared && python test_role_families.py
```

22 tests, including one asserting the set is exactly the eleven and one
proving every family is reachable from a real listing title — so a declared
category can never become unemittable.
