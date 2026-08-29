# Retired scrapers

These scrapers still work. They were removed from the active fleet on
2026-08-25 because, under the two-level taxonomy adopted that day
(`scrappers/_shared/classification.py`), they no longer yield anything.

Four groups, retired for the same reason by different measurements.

## Hospital-operator ATS boards (13)

They advertise bedside clinical vacancies — staff nurses, consultants, technicians — which the taxonomy
excludes by definition. Across their whole stored history they kept **7 rows
between them** and dropped 713:

| scraper | kept | dropped | board |
|---|---|---|---|
| fortis | 3 | 194 | Fortis Healthcare (IN) |
| medcare | 2 | 19 | Medcare / Aster (AE) |
| maxhealthcare | 1 | 87 | Max Healthcare (IN) |
| phcc | 1 | 15 | Primary Health Care Corp (QA) |
| apollohospitals | 0 | 71 | Apollo Hospitals (IN) |
| carecareers | 0 | 26 | CareCareers (AU) |
| dubaihealth | 0 | 3 | Dubai Health (AE) |
| hmg | 0 | 100 | Dr. Sulaiman Al Habib (SA) |
| moh | 0 | 2 | Ministry of Health (AE) |
| purehealth | 0 | 14 | PureHealth (AE) |
| seha | 0 | 145 | SEHA / Abu Dhabi Health Services (AE) |
| sidra | 0 | 36 | Sidra Medicine (QA) |
| gulftalent | 0 | 1 | GulfTalent (crawl-subset of `gulftalent_roles`) |

`gulftalent` is retired for a second reason: its crawl is a strict subset of
`gulftalent_roles`, which stays in the active fleet.

## Blue-collar board (1)

| scraper | kept | dropped | board |
|---|---|---|---|
| workindia | 9 | 915 | workindia.in (IN) |

**workindia** is not an ATS — it is a general blue-collar job board, and it was
retired on measurement rather than on category. Its daily latest-JD sitemap was
pulled live on 2026-08-25: **15,614 job URLs across 3,250 distinct title
slugs**, of which the only in-scope-looking ones were `medical_representative`
(15 — pharma sales, an explicit negative keyword), `clinical_nurse_specialist`
(3) and `clinical_pharmacist` (3), both bedside, plus about four
dietician/nutritionist posts. Scoring its 924 stored rows through
`classify_job` keeps **9 (1.0%)**.

The postings it actually carries are shop helper, printing machine operator and
grocery delivery. No amount of widening its `ALLOW_SLUG_RE` surfaces
clinical-research or public-health roles, because they are not posted there —
which is why it was retired instead of widened. It is still on the **legacy
profession enum**, not the two-level taxonomy; migrating it was never worth
doing. Contrast `internshala`, measured the same way at 9.5%, which was widened
to all 173 categories and migrated instead.

## They still run where they sit

`_shared` here is a symlink to `../scrappers/_shared`, so each retired scraper
imports the live classifier exactly as before — `python -m unittest discover`
inside any of these folders passes (426 tests across the 13). They are retired
from the fleet, not broken.

## Reviving one

`git mv retired-scrappers/<name> scrappers/<name>`. Each folder is unchanged
and self-contained — scraper, tests, readme and stored CSVs travelled with it.
Nothing outside these folders imports them.

Worth revisiting if the taxonomy ever widens back toward bedside roles, or if
one of these operators starts posting clinical-research / public-health roles.


## Low-yield sources retired 2026-08-25 (7)

Measured against **live** data, not just the stored snapshot — a lesson from
`publichealthcareer`, whose stored rows scored 0% while its live board scored
50% (it rotates; the store was a stale rotation). Each of these was re-run
fresh on 2026-08-25:

| scraper | live rows | in scope | % |
|---|---|---|---|
| narayanahealth | 206 | 1 | 0.5% |
| dubailivejobs | 64 | 0 | 0.0% |
| hziegler | 61 | 0 | 0.0% |
| kfshrc | 32 | 0 | 0.0% |
| zulekhahospitals | 2 | 0 | 0.0% |
| profco | partial crawl | — | 0.5% on 198 stored rows |
| dubizzle | not re-run (needs a browser session) | — | 0/11 stored |

`profco`'s crawl was cut short by a timeout, but its own source categories
settle it — "Medical Doctor", "Nursing and Midwifery", "Engineering": it is a
clinical staffing agency. `dubizzle` is a Gulf classifieds site and needs
`--headless` browser automation to run, so it was judged on its stored rows
alone; both are noted here rather than claimed as verified.

All seven remain on the legacy profession enum — none was ever migrated.

## shine — subset of a sibling, retired 2026-08-26 (1)

| scraper | kept | board |
|---|---|---|
| shine | subset of `shine_roles` | shine.com (IN) |

Retired for the same reason as `gulftalent`: **its crawl is a subset of
`shine_roles`**, which stays in the active fleet. Same site, same parser, same
`classify_job` gate — the only difference was the query list. `shine` asked
shine.com for four broad things (`healthcare?ind=13`, `healthcare`, `hospital`,
`medical`) and discarded ~85% of what came back; `shine_roles` asks for the
role families by name plus four industry-facet browses, and reaches jobs
`shine` structurally cannot see — medical coders filed under BPO, clinical
programmers filed under IT Services.

Measured across the two stores: of the 653 jobs `shine` had that `shine_roles`
did not, **643 (98.5%) carried `industry = Medical / Healthcare`** — i.e.
`ind=13`, which `shine_roles` browses wholesale via `jobs?ind=13`, a superset
of `shine`'s `healthcare?ind=13`. The two stores were scraped in different
windows (shine 08-08/08-10, shine_roles 08-24/08-25), so the raw overlap of 103
rows is an artifact of timing, not of coverage.

Of the 10 exclusive rows **outside** `ind=13`, nine were classifier noise —
"Admin Executives Delhi", "Multilingual Geography and Travel Expert" and
"Executive Assistant - Admin Operations" all labelled Clinical Data Management.
One was a genuine catch ("Lead R&D Technologist - Statistical Programming",
filed under IT Services).

**Nothing was lost.** `shine`'s three broad slugs — `healthcare`, `hospital`,
`medical` — were added to `shine_roles.SEARCH_QUERIES` (now 55 slugs) before
the retirement, closing that last gap. Probed live on 2026-08-26 over pages
1-2: `medical` 19/40 in scope, `healthcare` 11/40, `hospital` 2/40. The keeps
skew heavily to Medical Coding, which the family slugs already reach, so dedup
by job id absorbs most of the overlap.

`shine` was also sitting half-migrated to the two-level taxonomy (an open
PARTIAL item in `instructions/taxonomy-migration-status.md` — a stale
`classify_category` and a club row defaulting `category` to `non_clinical`).
Retiring it closed that item rather than spending the migration on a scraper
whose output was already covered. Its 34 tests still pass where it now sits.

## pharmabharat — permanently ignored (1)

Retired 2026-08-25 **by explicit user decision**, not by measurement. Recorded
here because the measurement pointed the other way: it scored **50 of 64
stored rows (78%) in scope** — the densest source in the whole fleet, strong in
Pharmacovigilance (11), Clinical Data Management (10), Regulatory Affairs (9)
and Clinical Research (8).

**Do not propose reviving or migrating this one.** The user asked to ignore it
permanently, and the high yield above is exactly the argument that would
otherwise keep resurfacing it.
