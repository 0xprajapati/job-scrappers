


# Retrieval classification -----------------------------------------

## Sites searched by your role keywords
Shine, Indeed, Naukrigulf, SimplyHired, Foundit, Internshala (Using 20 role classification)


## Sites where a whole section is taken and filtering happens afterwards:
Naukri, Reed, Himalayas
## Sites taken whole (everything they post):
Manipal, DHA, PharmaRecruiter, Vaidyog, Nextenti, PublicHealthCareer.
(Retired 2026-08-25/26, so no longer crawled: Apollo, Fortis, Max, Narayana,
the Gulf hospital group, CareCareers, PharmaBharat.)



# System classification -----------------------------------------

## Fully on the new system, verified, old data recleaned (24 still active):
Shine, Shine-roles, Naukri, Naukri-roles, Naukrigulf, Indeed, Foundit, Reed,
Himalayas, Freshersworld, GulfTalent-roles, Docthub, Docthub-roles, Vaidyog,
Nextenti, PharmaRecruiter, PharmaRecruiter-roles, Internshala, Manipal, DHA,
and — migrated 2026-08-26 — SimplyHired, Jobberman, MichaelPage,
PublicHealthCareer.

Also migrated, but since **retired** to `retired-scrappers/` because they yielded
almost nothing under the new taxonomy (bedside-only boards): Apollo, Fortis, Max,
Medcare, PureHealth, SEHA, Sidra, HMG, Dubai Health, MOH, PHCC, CareCareers,
GulfTalent. They still run and test in place; one `git mv` revives any of them.

## Who is NOT using _shared — 3 scrapers
These still use their old private doctors/nurses/pharmacists labels: apna,
nhm, swaasa. apna's parser is broken (no club export at all), nhm returns 0
rows as its steady state, and swaasa scored 5.4% live and hasn't been asked
for.

Down from 17 over 2026-08-25/26: `internshala` was widened and migrated;
nine were retired to `retired-scrappers/` rather than migrated —
dubailivejobs, dubizzle, hziegler, kfshrc, narayanahealth, profco, workindia
and zulekhahospitals all scored 0-1% in scope when re-run live, and
`pharmabharat` was retired by your own decision despite scoring 78%; and on
2026-08-26 simplyhired, jobberman, michaelpage and publichealthcareer were
migrated. hziegler and profco were skipped in that batch on purpose —
migrating them would have meant reviving two scrapers retired the day before.

`instructions/taxonomy-migration-status.md` is the authoritative list — this file
is a summary of it, not a second source of truth.















