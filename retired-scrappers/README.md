# Retired scrapers

These scrapers still work. They were removed from the active fleet on
2026-08-25 because, under the two-level taxonomy adopted that day
(`scrappers/_shared/classification.py`), they no longer yield anything.

Two groups, retired for the same reason by two different measurements.

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
