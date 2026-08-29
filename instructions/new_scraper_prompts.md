# Paste-ready prompts — one per new chat

Open each chat in the `job-scrappers` repo (memory is per-project, so the
probe notes load automatically). **Paste one whole numbered block** — each is
self-contained and repeats the house rules, so nothing else is needed.

Already built — do NOT rebuild: devnetjobsindia, reliefweb, ngobox,
impactpool, pharmatutor, vaidyog.

---

## 1. DevNetJobs.org (global twin) — highest value, cheapest build

Build a scraper for devnetjobs.org, the global twin of the already-built
devnetjobsindia scraper. Memory `new-source-probe-2026-08` has the probe
notes; `scrappers/devnetjobsindia/` is the reference implementation and
memory `devnetjobsindia-scraper-source` documents its traps.

Probe findings (2026-08-26): same ASP.NET platform, same `job_id` URL
scheme, ~50 SSR jobs on the homepage including "Senior Health Specialist,
Maternal and Newborn Health". Global development/UN-adjacent supply — expect
a higher in-scope rate than the India board.

Start by checking whether devnetjobs.org exposes the same `sitemap.aspx`
that robots.txt names on the India site. If it does, this may be a config
switch on the existing scraper rather than a new file — evaluate that first
and tell me which way you went and why. Watch for the two India-site bugs:
the JSON-LD trailing `}` (parse with `raw_decode`) and `lblPostedDate`
actually holding the apply-by deadline, not the posted date.

House rules:
- Fetch wide, filter tight. Find the site's widest cheap enumeration
  (sitemap / feed / API), NOT its health category pages — source facets lie.
- The scraper NEVER assigns a category. Extract raw fields, then call
  `classify_job(title, skills, description)` from
  `scrappers/_shared/classification.py`; copy its `category` /
  `sub_category` / `role_family` verdict. `in_scope: False` rows go to
  `out-of-scope.csv`, never the main CSV. Keep and flag `needs_review` rows.
- The one judgement call is what you pass as `skills` — the site's own
  curated role signal if it has one (devnetjobsindia passes "Relevant
  Sectors" tags). Keep it as a raw column; never let it decide the category.
- NEVER edit `taxonomy_keywords.py` / `role_families.py`. If a role can't be
  placed, report it to me — don't widen the keywords.
- Mirror the devnetjobsindia file layout (`scraper.py`, `readme.md`,
  `requirements.txt`, `test_filters.py`, `<name>_jobs.csv`,
  `out-of-scope.csv`, `seen_old_ids.csv`, `needs_review.csv`) and its exact
  CSV columns.
- Report the measured in-scope rate from a real run before we call it done.

---

## 2. wp-json trio — Rasayanika / Biotecnika / IndianPharmaJobs

Build scraper(s) for these WordPress pharma/life-sciences job sites, all of
which expose an open REST API (`/wp-json/wp/v2/posts?search=<term>`),
confirmed live 2026-08-26:
- www.rasayanika.com  (search=pharmacovigilance returned a live PV job)
- www.biotecnika.org
- www.indianpharmajobs.in

Memory `new-source-probe-2026-08` has the probe notes. Same species as the
existing `scrappers/pharmarecruiter/` — read that scraper AND memory
`wp-search-term-vetting` first: WP search is a substring LIKE, so short terms
generate noise, and that memory records which terms are safe.

Key risk: these sites mix job posts with news/editorial in the same post type
(Rasayanika's feed returned "Chemistry Salary in India" next to a real PV
job). Find the category/taxonomy that separates jobs from news rather than
relying on search terms alone. Report the in-scope rate you measure through
classify_job for each of the three before I decide whether to keep all three.

House rules:
- Fetch wide, filter tight. Widest cheap enumeration, not health facets.
- The scraper NEVER assigns a category. Extract raw fields, then call
  `classify_job(title, skills, description)` from
  `scrappers/_shared/classification.py`; copy its `category` /
  `sub_category` / `role_family` verdict. `in_scope: False` rows go to
  `out-of-scope.csv`, never the main CSV. Keep and flag `needs_review` rows.
- The one judgement call is what you pass as `skills` — the site's own
  curated role signal (here: WP categories/tags) if it has one. Keep it as a
  raw column; never let it decide the category.
- NEVER edit `taxonomy_keywords.py` / `role_families.py`. If a role can't be
  placed, report it to me — don't widen the keywords.
- Mirror the `scrappers/devnetjobsindia/` file layout and its exact CSV
  columns.

---

## 3. jobs.ac.uk — public health academia

Build a scraper for jobs.ac.uk. Memory `new-source-probe-2026-08` has the
probe notes: SSR search at `/search/?keywords=`, detail links `/job/<id>`,
no JSON-LD on the search page (check the detail pages).

Supply is UK academic public health — "Associate Lecturer in Public Health",
"Postdoctoral Research Associate in Public Health Nutrition", "Lecturer /
Senior Lecturer in Public Health Management". These should land in Public
Health Research and Public Health Program Management. Fetch wide across
health-relevant keyword searches; let classify_job reject the rest.

Note: UK academic titles are unlike anything in the current fleet, so this
source is a likely trigger for "the taxonomy can't place this". If that
happens, report the cases to me rather than widening keywords.

House rules:
- Fetch wide, filter tight. Widest cheap enumeration, not health facets.
- The scraper NEVER assigns a category. Extract raw fields, then call
  `classify_job(title, skills, description)` from
  `scrappers/_shared/classification.py`; copy its `category` /
  `sub_category` / `role_family` verdict. `in_scope: False` rows go to
  `out-of-scope.csv`, never the main CSV. Keep and flag `needs_review` rows.
- The one judgement call is what you pass as `skills` — the site's own
  curated role signal if it has one. Keep it as a raw column; never let it
  decide the category.
- NEVER edit `taxonomy_keywords.py` / `role_families.py`.
- Mirror the `scrappers/devnetjobsindia/` file layout and its exact CSV
  columns.
- Report the measured in-scope rate from a real run.

---

## 4. NHS Jobs (UK)

Build a scraper for jobs.nhs.uk. Memory `new-source-probe-2026-08` has the
probe notes: fully SSR — search at `/candidate/search/results?keyword=`,
details at `/candidate/jobadvert/<ref>`, title in `<h1>`, no JSON-LD.

Measured during the probe: keyword="public health" returns 9,324 results, so
the site's matching is very loose — a textbook fetch-wide-filter-tight
source. Expect a low in-scope rate and report it.

Before scaling the crawl up, confirm with me that band-graded NHS
non-clinical roles are actually in scope — this is the fleet's only UK
clinical-system source and I want to make that call explicitly.

House rules:
- Fetch wide, filter tight. Widest cheap enumeration, not health facets.
- The scraper NEVER assigns a category. Extract raw fields, then call
  `classify_job(title, skills, description)` from
  `scrappers/_shared/classification.py`; copy its `category` /
  `sub_category` / `role_family` verdict. `in_scope: False` rows go to
  `out-of-scope.csv`, never the main CSV. Keep and flag `needs_review` rows.
- Remember the negative-keyword veto is real: clinical titles like "Staff
  Nurse" are vetoed outright. Do not try to route around it.
- NEVER edit `taxonomy_keywords.py` / `role_families.py`.
- Mirror the `scrappers/devnetjobsindia/` file layout and its exact CSV
  columns.

---

## 5. globaljobs.org

Build a scraper for globaljobs.org. Memory `new-source-probe-2026-08`: SSR,
~226 job links on the landing page, international development/policy supply
("Policy and Advocacy Manager", "Senior Manager - International Programs").

Health is a minority of this board's inventory. Score it through
classify_job EARLY — if it lands near the 0-0.5% that got seven sources
retired on 2026-08-25, retire it instead of finishing the build and tell me.
See memory `fetch-wide-filter-tight` for the scoring rule (score LIVE data,
never a stored snapshot).

House rules:
- Fetch wide, filter tight. Widest cheap enumeration, not health facets.
- The scraper NEVER assigns a category. Extract raw fields, then call
  `classify_job(title, skills, description)` from
  `scrappers/_shared/classification.py`; copy its `category` /
  `sub_category` / `role_family` verdict. `in_scope: False` rows go to
  `out-of-scope.csv`, never the main CSV. Keep and flag `needs_review` rows.
- NEVER edit `taxonomy_keywords.py` / `role_families.py`.
- Mirror the `scrappers/devnetjobsindia/` file layout and its exact CSV
  columns.

---

## 6. talent.com — measure duplicate rate BEFORE building

Evaluate, then maybe build, a scraper for in.talent.com. Memory
`new-source-probe-2026-08`: SSR listing at `/jobs?k=<kw>&l=India`, details at
`/view?id=<id>`, real PV-India titles confirmed live 2026-08-26.

IMPORTANT — do this first: talent.com is an aggregator that re-lists postings
already carried by naukri, indeed and foundit. Sample ~50 of its listings and
measure how many duplicate rows already in the existing job CSVs. If the
unique-supply rate is low, say so and STOP — I'd rather skip this source than
pollute the store with dupes. Only build the full scraper if the unique rate
justifies it.

House rules (if you do build):
- Fetch wide, filter tight. Widest cheap enumeration, not health facets.
- The scraper NEVER assigns a category. Extract raw fields, then call
  `classify_job(title, skills, description)` from
  `scrappers/_shared/classification.py`; copy its `category` /
  `sub_category` / `role_family` verdict. `in_scope: False` rows go to
  `out-of-scope.csv`, never the main CSV. Keep and flag `needs_review` rows.
- NEVER edit `taxonomy_keywords.py` / `role_families.py`.
- Mirror the `scrappers/devnetjobsindia/` file layout and its exact CSV
  columns.

---

## 7. UNDP — medium effort, Oracle HCM

Build a scraper for jobs.undp.org. Memory `new-source-probe-2026-08`: the
listing page (`cj_view_jobs.cfm`) is SSR, but every job links out to Oracle
HCM Candidate Experience requisitions at
`estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/job/<id>`.

Investigate whether Oracle CE exposes a JSON REST endpoint for those
requisitions (it usually does — look for
`/hcmRestApi/resources/.../recruitingCEJobRequisitions`) before resorting to
browser capture. Report which path you found and the cost of the run.

House rules:
- Fetch wide, filter tight. Widest cheap enumeration, not health facets.
- The scraper NEVER assigns a category. Extract raw fields, then call
  `classify_job(title, skills, description)` from
  `scrappers/_shared/classification.py`; copy its `category` /
  `sub_category` / `role_family` verdict. `in_scope: False` rows go to
  `out-of-scope.csv`, never the main CSV. Keep and flag `needs_review` rows.
- NEVER edit `taxonomy_keywords.py` / `role_families.py`.
- Mirror the `scrappers/devnetjobsindia/` file layout and its exact CSV
  columns.

---

## 8 & 9. USAJobs + Adzuna — blocked until I register

Free API keys needed:
- USAJobs: 401 without a key — register at developer.usajobs.gov
- Adzuna: 400 without app_id/app_key — register at developer.adzuna.com
  (India endpoint: `/v1/api/jobs/in/search/1`)

Once the keys exist these are the cheapest builds on the list. Same house
rules as every block above.
