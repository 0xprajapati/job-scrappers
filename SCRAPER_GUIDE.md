# Scraper Guide — how every scraper in this repo works

This document explains each scraper in `scrappers/`: what site it targets, what it collects, the scraping technique it uses, its full pipeline, and its notable quirks. It was compiled by reading the actual source of every scraper (2026-08-25).

---

## 1. The common architecture (almost every scraper follows this)

Nearly all scrapers share one skeleton, defined by `instructions/master-scraper-spec.md`:

```
load existing CSV → known_ids → compute cutoff (watermark) → crawl source
→ per-job: date gate → dedup → parse → classify → collect
→ append to rich CSV → regenerate club CSV → write needs_review.csv → print summary
```

**Outputs (per scraper):**

| File | Purpose |
|---|---|
| `scrappers/<site>/<site>_jobs.csv` | Rich cumulative store. Dedup key = `job_id`. Append-only; running twice adds 0 rows. Also the source of the incremental watermark. |
| `jobs_csv/<DD-MM-YYYY>/<site>.csv` | The club export. For a migrated scraper this is the **22-column schema** — exactly `CLUB_COLUMNS`, imported from `_shared/classification.py`, never hand-copied. Not-yet-migrated scrapers still write their older club schema. **Regenerated from the entire cumulative store every run** — it is a full dump, not a daily delta. |
| `scrappers/<site>/needs_review.csv` | In-scope rows the classifier flagged (`needs_review`) — kept in the output *and* written here. Usually overwritten per run (a few scrapers append+dedupe). |
| `scrappers/<site>/out-of-scope.csv` | Where a stored row demoted by a reclassification goes — reversible, never silently discarded. |

**The club schema (22 columns, `CLUB_COLUMNS`):**

```
country_name, country_code, country_dial_code, city_name, company_name,
company_type, company_logo, company_about, title, description, job_type,
category, sub_category, application_url, posted_at, min_experience,
max_experience, qualification, min_salary, max_salary, salary_period,
salary_currency
```

`category` is `Non Clinical` or `Public Health`; `sub_category` is one of the 20 sub-categories. `is_active` and `expires_at` are **retired** — rich CSVs may still keep source expiry data in their own columns. `qualification` is the source's structured field when it has one, else `extract_qualification(description)`; never inferred.

This is the target schema, not yet a universal fact: the scrapers still on the legacy scheme (§2) keep their older club schema, `is_active`/`expires_at` included.

**Incremental time window:** first run keeps `INITIAL_WINDOW_DAYS` (varies: 2–365, or `None` = keep everything for ATS portals that only list open requisitions); later runs use `max(stored posted_date) − 2 grace days`. Sources sorted newest-first allow early stop when a whole page predates the cutoff.

**Etiquette contract (everywhere):** descriptive `User-Agent` with contact/repo URL (except where a WAF forbids it — then contact moves to the `From:` header), `robots.txt` checked at startup with `sys.exit` on an explicit disallow, ≥1 s delay between requests, 3–4 retries with exponential backoff (3→24 s) on 429/5xx, 4xx treated as permanent, per-record `try/except` so one malformed job never kills a run.

**Principles enforced everywhere:**
- Nothing in scope is ever silently dropped — ambiguous rows are **kept + flagged** in `needs_review.csv`. Migrated scrapers *do* drop out-of-scope rows, but visibly: counted as `excluded_out_of_scope` and printed in the run summary. Legacy-scheme scrapers drop nothing and fall back to `non_clinical`.
- Salary is captured, never filtered on, never invented (`"Not Disclosed"` + blank numerics).
- Non-INR/USD salaries (AED, SAR, GBP, NGN, KWD…) stay in the rich CSV; club salary columns stay blank because the club enum only allows INR/USD. Nothing is ever currency-converted.
- Dates are never invented — boards with no dates (DHA, Profco, NHM) use a first-seen date and say so.

**There is no fleet runner.** Older notes mention a `run_daily.sh` that does not exist; each scraper is invoked by hand (`../../.venv/bin/python scraper.py`) or from one cron line per scraper. There is no central exporter either — the legacy `instructions/export_club_csv.py` was deleted in the 2026-08-25 migration, and every scraper writes its own club CSV.

---

## 2. Two schemes: the standard, and the legacy one still in use

**The migration is partial and in progress.** Two classification schemes run side by side in this repo.

### The standard (migrated scrapers, and mandatory for anything new)

A migrated scraper classifies through **`_shared/classification.py`** and exports the **22-column club schema**. It defines no category regexes, profession enums, ALLOW/DENY *classification* lists, or category fallback maps of its own.

```python
verdict = classify_job(title, skills, description)
if not verdict["in_scope"]:
    counters["excluded_out_of_scope"] += 1
    continue                       # dropped — never exported
category, sub_category = verdict["category"], verdict["sub_category"]
```

- **Two categories, 20 sub-categories.** `Non Clinical` (Clinical Data Management, Clinical Research, Medical Writer, TMF, Medical Coding, Pharmacovigilance, Regulatory Affairs, Medical Reviewer, MSL, HEOR) and `Public Health` (Epidemiology, Public Health Program Management, Monitoring & Evaluation, Community Health, Health Promotion & Education, Disease Programs, Public Health Nutrition, Infection Prevention & Control, Health Informatics & Data, Public Health Research).
- **Out of scope means dropped**, counted as `excluded_out_of_scope` and printed in the run summary. Bedside/clinical, billing, sales and admin roles are out of scope no matter which board they came from.
- **`needs_review == True` means kept AND flagged** into `needs_review.csv` — never silently dropped.
- **Crawl-side scoping still exists and is encouraged** (source category facets, healthcare listing URLs, slug filters, junk-title detection): source filters are recall, the classifier is precision. But a source facet may never *decide* the category — ATS/site categories stay in the rich CSV as raw source columns.
- The rich CSV additionally records `role_family`, `all_families`, `family_scores`, `family_confidence`, `matched_in` and `needs_review` so every admission is auditable.

### The legacy scheme (still in use — you will meet it)

A set of scrapers is **excluded from the migration for now** and keeps its own per-scraper classifier and its older club schema. Reading or touching one of those, this is what you are looking at:

- **`category` is the profession enum `doctors | nurses | pharmacists | non_clinical`**, decided by per-scraper title/department regexes (order matters in most of them — allied and corporate patterns run before the doctor pattern, because corporate titles borrow clinical words).
- **Nothing is dropped for scope.** A title the regexes cannot place is kept as `non_clinical` and flagged into `needs_review.csv`; there is no `excluded_out_of_scope` counter.
- **The older club schema**, `is_active`/`expires_at` included, and no `sub_category`.
- Their ATS/site category fields often *do* decide the club category, which the standard forbids.

**`instructions/taxonomy-migration-status.md` is the authoritative, single-copy list of which scrapers these are** — deliberately not repeated here or in any other doc. Check it before assuming a scraper's output shape.

> **Historical note (2026-08-25):** before this date the fleet ran three incompatible schemes — an old-generation group with no club export at all, a majority on the profession enum above, and the `*_roles`/`foundit`/`himalayas` group emitting eleven role-family names. The migration to the single two-level taxonomy is collapsing all three into the standard, scraper by scraper; the binding contract is `instructions/taxonomy-migration-spec.md`.

---

## 3. Shared modules (`scrappers/_shared/`)

A pure-Python library (no network, no output), imported via `sys.path.insert(..., "../_shared")`.

### `classification.py` — the only module a scraper imports

The scraper-facing API; `role_families` and `taxonomy_keywords` are its engines and are never imported directly.

- `classify_job(title, skills="", description="") -> dict` — runs the taxonomy negative veto, then the role-family in-scope gate, then the sub-category split. Returns `in_scope` (`False` → drop), `category`, `sub_category`, `sub_category_basis`, `role_family`, `needs_review`, `vetoed_by`, and the score trace (`all_families`, `family_scores`, `family_confidence`, `matched_in`).
- The ten Non Clinical family names *are* their sub-categories, so a Non Clinical job always gets both. Public Health is one family but ten sub-categories, so a Public Health job whose finer split is undecidable keeps the category and leaves `sub_category` blank rather than guessing.
- `CLUB_COLUMNS` — the 22-column club contract, imported not copied.
- `extract_qualification(description)` — re-exported from `role_families` so everything classification-related comes from one import.

**Consumers: every migrated scraper** (the legacy-scheme scrapers of §2 import nothing from `_shared`).

### `role_families.py` — "is this job in scope, and which family?"

- Eleven `(name, regex)` families; list order is tie-break precedence (specific before broad).
- **Weighted multi-field scoring**: `title ×5`, `skills ×2`, `description ×1`; `MIN_SCORE_KEEP = 3` (one title hit qualifies alone; description-only needs 3 distinct terms). Only *distinct* matched substrings count, so keyword-stuffing can't inflate a score. Only the first 4,000 chars of the description are scanned (EEO/benefits boilerplate name-drops other domains).
- **Public Health is tightened** (`FAMILY_MIN_SCORE=5`, must match in title or skills) because PH vocabulary ("infection control", "nutrition") appears as routine duty text everywhere; description-only PH admissions were measured to be all junk.
- Disambiguation baked into regexes: `\bcdm\b` needs a clinical/EDC context (else it's Charge Description Master); "regulatory compliance" excluded from Regulatory Affairs; PH regex rejects `environmental health and safety`, animal/sports nutritionists, etc.
- API: `classify(title, skills, description) → {family, score, confidence, all_families, family_scores, matched_in, needs_review}` (empty family = out of scope → drop); `extract_qualification(description)` lifts credentials (MBBS, PharmD, MPH, CPC…) verbatim, never inferred; `OUT_OF_SCOPE_TITLE` (counsel/sales/recruiter/software-engineer titles) sets `needs_review=True` — such rows are kept and flagged, never dropped.
- Measured value: +12% yield over title-only on a 307-job Naukri set; on 2,171 naukri cards, 12% of in-scope jobs had **no title match at all** (surfaced via skills/description).

**Consumed only through `classification.py`.**

### `taxonomy_keywords.py` — "which sub-category?"

Machine-readable form of `Jobs_keywords/keywords_for_jobs.md`: **Non Clinical** (10 sub-categories = the family names) and **Public Health** (Epidemiology, PH Program Management, M&E, Community Health, Health Promotion & Education, Disease Programs, PH Nutrition, IPC, Health Informatics & Data, PH Research). Four-tier matching: `NEGATIVE_KEYWORDS` veto (medical billing, AR caller, revenue cycle, MEP, telesales…) → title regex → ≥2 distinct strong keywords in skills+description → weak keywords tie-break only. Uses alphanumeric lookarounds instead of `\b` so `ART` never matches inside "particular" while `E2B`/`ICD-10-CM` still work.

**Consumed only through `classification.py`.**

### Tests

`test_classification.py` (9), `test_role_families.py` (38) and `test_taxonomy_keywords.py` cover the engines, so a scraper's own `test_*.py` needs only two wiring tests: an in-scope role gets the right `category`/`sub_category`, and an out-of-scope title is dropped.

---

## 4. Master table

⛔ = **retired 2026-08-25**, moved to `retired-scrappers/`. Thirteen are
hospital-operator ATS boards that kept 0–3 rows each under the two-level
taxonomy (they post bedside clinical vacancies, which are out of scope by
definition); `workindia` is a blue-collar board measured at 1.0% in scope. Code, tests
and stored CSVs are unchanged; revive with `git mv retired-scrappers/<name>
scrappers/<name>`. See `retired-scrappers/README.md`.

| Scraper | Site (market) | Technique | Scoping (which URLs/facets it walks) | Status notes |
|---|---|---|---|---|
| apna | apna.co (IN) | Next.js RSC flight-stream reassembly | department slugs | parser broken; rich CSV only |
| ⛔ apollohospitals | Oracle ORC API (IN) | public JSON REST | whole ATS board | **RETIRED 2026-08-25** (bedside-only yield); migration reference implementation |
| ⛔ carecareers | PeopleStrong API (IN) | JSON API | whole ATS board | **RETIRED 2026-08-25** (bedside-only yield) |
| dha | Sheryan/WebSphere portal (AE) | portlet emulation, runtime endpoint discovery | portal licence categories | no dates published |
| docthub | api.docthub.com (IN) | public JSON API, per-category crawl | category facets | gained a club export in the migration |
| docthub_roles | api.docthub.com (IN) | same, all 18 categories | all 18 categories | |
| ⛔ dubaihealth | Taleo REST (AE) | JSON API + state-blob parsing | Dubai Health organization id | **RETIRED 2026-08-25** (bedside-only yield) |
| ⛔ dubailivejobs | WordPress REST (AE) | posts API cat 86 + JSON-LD | WP Healthcare category (86) | posts are company pages, not jobs |
| ⛔ dubizzle | dubai.dubizzle.com (AE) | **headed Playwright** (`__NEXT_DATA__`) | category page | Imperva; needs GUI session |
| ⛔ fortis | Oracle ORC API (IN) | public JSON REST | whole ATS board | **RETIRED 2026-08-25** (bedside-only yield) |
| foundit | foundit.in (IN) | **browser capture** (capture.js + receiver.py) | ~25 keyword SERPs | Akamai TLS-blocked |
| freshersworld | freshersworld.com (IN) | HTML listing + JSON-LD detail | 4 category listings | |
| ⛔ gulftalent | gulftalent.com (Gulf) | HTML listing + JSON-LD | source-side industry facet | **RETIRED 2026-08-25** (bedside-only yield) |
| gulftalent_roles | gulftalent.com (Gulf) | same, 5 countries | 5 countries × healthcare industry facet | `_PHARMA_RE` NameError fixed |
| himalayas | himalayas.app API (remote) | public JSON API | whole feed (slugs passed as `skills`) | offset pagination unstable |
| ⛔ hmg | Elevatus ATS (SA) | JSON API, 8 portals | 8 brand portals | **RETIRED 2026-08-25** (bedside-only yield); `Accept-Company` header required |
| ⛔ hziegler | hziegler.com (agency, SA/AE/CA) | static HTML indexes + JSON-LD | 4 index pages (whole site) | no salaries (agency) |
| indeed | in.indeed.com (IN remote) | **browser capture** (console snippets, clipboard) | ~27 queries, page 1 each | Cloudflare; page 1 only per robots |
| internshala | internshala.com (IN) | listing HTML + detail JSON-LD | **all 173 live job categories** (fetch-wide) | migrated to the shared taxonomy 2026-08-25; category facet is loose, classifier does the filtering |
| jobberman | jobberman.com (NG) | HTML + `@graph` JSON-LD | healthcare vertical | migrated 2026-08-26; robots caps pagination at 10 pages |
| ⛔ kfshrc | kfshrc.edu.sa (SA) | Sitecore Search proxy POST | whole ATS board | Riyadh-midnight date trap |
| manipalhospitals | Zwayam ATS (IN) | multipart POST + JSON detail | whole ATS index (one hit list) | Akamai: UA must be bare `Mozilla/5.0` |
| ⛔ maxhealthcare | PeopleStrong API (IN) | JSON API | whole ATS board | **RETIRED 2026-08-25** (bedside-only yield); lakh-vs-absolute salary heuristic |
| ⛔ medcare | Oracle ORC API (AE) | JSON REST, org facet | Medcare organization facet | **RETIRED 2026-08-25** (bedside-only yield); `Role.Dept.Facility` title parsing |
| michaelpage | michaelpage.co.in (IN) | HTML regex + JSON-LD | healthcare vertical | migrated 2026-08-26; `seen_old_ids.csv` + `out-of-scope.csv` skip lists |
| ⛔ moh | moh.gov.sa (SA) | SharePoint sitemap + HTML | sitemap announcements + is-this-a-job gate | **RETIRED 2026-08-25** (bedside-only yield); Hijri→Gregorian conversion |
| ⛔ narayanahealth | SuccessFactors RMK (IN) | HTML search + microdata | whole `/search/` listing | JSON backends robots-blocked |
| naukri | naukri.com (IN) | **browser capture** (fetch/XHR interceptor) | healthcare functional-area capture | signed `nkparam` + Akamai |
| naukri_roles | naukri.com (IN) | same, per-family searches | 16 per-family search captures | |
| naukrigulf | naukrigulf.com (Gulf) | private SPA API via `curl_cffi` | facets + 13 keyword walks | `appid`/`systemid` headers required |
| nextenti | nextenti.ai (IN) | JSON API + anonymous bearer token | whole board (`profession` kept as a raw column) | reconstructs job URLs |
| nhm | nhm.gov.in (IN) | HTML anchor extraction | notice regexes | **0 rows is the steady state** |
| ⛔ pharmabharat | WordPress REST (IN) | posts API, 3 body shapes | whole site (every category is a job taxonomy) | plus hardened `daily_scraper.py` |
| pharmarecruiter | WordPress REST (IN) | posts API, `jobs` category | `jobs` WP category | |
| pharmarecruiter_roles | WordPress REST (IN) | WP full-text `?search=` per family | 54 full-text search terms | term-cursor bug fixed + list widened 2026-08-25; stale README |
| ⛔ phcc | Oracle EBS iRecruitment (QA) | server-rendered OAF form emulation | portal facet | **RETIRED 2026-08-25** (bedside-only yield); ⚠️ robots-blocked; needs `--ignore-robots` |
| ⛔ profco | profco.com (agency, SA/AU/UK/IE) | form-encoded AJAX fragments | adaptive drill-down | no dates; keeps a board-presence lifecycle in its rich CSV |
| publichealthcareer | WordPress custom REST (IN) | `job` post type + HTML detail | whole board crawled | migrated 2026-08-26; site's JSON-LD is malformed |
| ⛔ purehealth | Oracle ORC API (AE) | JSON REST | whole ATS board | **RETIRED 2026-08-25** (bedside-only yield); ATS category unreliable (clinical roles filed under Administration) |
| reed | reed.co.uk (UK) | `__NEXT_DATA__` SSR | both sector listings (Health & Medicine, Scientific) | GBP unexportable; scientific slug unverified |
| ⛔ seha | Oracle ORC API (AE) | JSON REST | whole ATS board | **RETIRED 2026-08-25** (bedside-only yield); facet only ~40% populated |
| ⛔ shine | shine.com (IN) | `__NEXT_DATA__` SSR | `ind=13` facet + 3 broad keywords | **RETIRED 2026-08-26** — crawl is a subset of `shine_roles` |
| shine_roles | shine.com (IN) | same, 55 slugs + 4 industry facets | 55 slugs + 4 industry facets | live-probed PH slug pruning; repost re-dating trap |
| ⛔ sidra | Oracle ORC API (QA) | JSON REST | whole ATS board | **RETIRED 2026-08-25** (bedside-only yield); talent-pool campaign flagging |
| simplyhired | simplyhired.co.in (IN) | `__NEXT_DATA__` + `curl_cffi impersonate` | 31 keyword cursor walks | migrated 2026-08-26; cursor pagination; ~page-73 depth cap |
| swaasa | Phenom People platform (IN) | `POST /widgets` + `phApp.ddo` | one master index | Mongo-style one-index board |
| vaidyog | jobs.vaidyog.com API (IN) | plain JSON API | board is healthcare-only | dates decoded from Mongo ObjectId |
| ⛔ workindia | workindia.in (IN) | daily latest-JD sitemap + JSON-LD | healthcare title slugs | **RETIRED 2026-08-25**: blue-collar board, 1.0% of stored rows in scope and ~0 in the daily sitemap |
| ⛔ zulekhahospitals | Adrenalin MAX HRIS (AE) | single token-free POST | whole board (one POST) | only 2 rows; worth a live probe |

---

Scrapers still on the legacy scheme classify locally on the profession enum rather than through `_shared` — §2, with the list in `instructions/taxonomy-migration-status.md`.

## 5. Per-scraper detail

Grouped by scraping strategy.

---

### A. Hospital-group ATS APIs (Oracle Recruiting Cloud family)

Six scrapers hit the same **Oracle ORC Candidate Experience REST API** pattern — a token-free public JSON API where the marketing site is skipped entirely (often because it's Cloudflare/Azure-blocked, while the Oracle host serves no robots.txt at all):

```
GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
    ?onlyData=true&expand=requisitionList.secondaryLocations
    &finder=findReqs;siteNumber=<SITE>,limit=N,offset=N,sortBy=POSTING_DATES_DESC
GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
    ?expand=all&onlyData=true&finder=ById;Id=<id>,siteNumber=<SITE>
```

Paging lives **inside the `finder` string**. Newest-first → early stop. Details fetched for new jobs only. None of them expose salary → `Not Disclosed` everywhere.

- ⛔ **apollohospitals** — site `CX_2` on `cgs.fa.ap2.oraclecloud.com`, ~90 requisitions. It is the **reference implementation** for the 2026-08-25 migration (now at `retired-scrappers/apollohospitals/scraper.py`) (imports + `apply_classification(row)` + `CLUB_COLUMNS` + the `excluded_out_of_scope` counter). The ATS `RequisitionType` is kept as a raw rich-CSV column only — most requisitions are bedside roles and are dropped. `workLocation[0].LocationName` (the concrete hospital unit) becomes `company`. The `apollohospitals.com` site 403s non-browsers and is never crawled.
- ⛔ **fortis** — site `CX_1` on `fa-ermg-saasfaprod1`, ~1,200 requisitions. Two API landmines: the list is **empty without `expand=requisitionList.secondaryLocations`**, and `limit` is silently capped at 25. `JobFamily` is null in the list, so details are on by default (the family is kept as a raw column — the ATS's own typo `TECHINICIANS` is normalized). Descriptions are empty in practice → club description falls back to `Hiring facility: <name>`. `fortishealthcare.com` is behind a Cloudflare challenge and never crawled.
- ⛔ **medcare** — Aster DM group tenant (`hcdt.fa.us2.oraclecloud.com`, site `CX`, ~2,900 group reqs); Medcare's ~21 isolated via `selectedOrganizationsFacet=300003648617854`. The interesting part is title parsing: requisitions arrive as `Role.Dept.Facility` (`Registered Nurse.Endoscopy.Medcare Hospital Sharjah (Br)`); `split_title()` splits on `.` but bails if any part is <2 chars (protects `(L.L.C)`), decides Dept-vs-Facility by regex, strips `(Br)` suffixes, re-spaces run-together names. Oracle's CATEGORIES facet is kept as a raw column.
- ⛔ **purehealth** — site `CX_6007` on `fa-eutv-saasfaprod1` (reached via purehealth.ae → talentone.ae, both Cloudflare-blocked and never crawled). **The ATS Category is unreliable** ("Cath Lab Staff Nurse" filed under *Administration*), which is one reason source facets may never decide the category. Experience mined from description sentences containing `experien|minimum|at least`. One of only two scrapers with a real ATS expiry date — it lives in the rich CSV now that the club `expires_at` column is retired.
- ⛔ **seha** — Abu Dhabi Health Services, site `CX_1` on `fa-eutv-saasfaprod1`, ~146 rows. Facet populated on only ~40% of reqs and kept as a raw column; corporate titles borrow clinical words ("Specialist - Talent & Performance"), a good example of why the shared classifier weighs skills and description rather than the title alone. Trailing parentheticals are disambiguated facility-vs-qualifier (`(Tawam Fertility Center)` peels into `facility` → club `company_name`; `(IVF)` stays in the title). Experience parsed from SEHA house style "NLT 2 years"; tiered roles record the lowest bar.
- ⛔ **sidra** — Sidra Medicine Doha, `siteNumber=Sidra-Career-Site` on `fa-epxn-saasfaprod1`, ~36 rows. Facet populated on **every** requisition (`Physician`/`Nursing`/`Allied Health`/`Enabling Function: <fn>`), parsed by prefix into a raw column. **Talent-pool campaigns** (`talent pool|future opportunit`, RequisitionType `Campaigns`) are kept, marked `talent_pool=True`, force-flagged. Bilingual quirk: Arabic schedule label `كل الوقت` mapped explicitly. Qatarization markers `(Nationals only)` captured. Experience parsed only from the qualifications table's Experience section so education years can't leak in.

### Other ATS platforms

- ⛔ **carecareers** — Quality Care India (CARE Hospitals, KIMS, CIIGMA) on **PeopleStrong**: `POST /api/cp/rest/altone/cp/jobs/v1?offset=N&limit=45` (body `{}`) + `GET /api/cp/rest/altone/cp/job/<ID>/v2?part=basic,organisational,descriprion,...` — the vendor's typo `descriprion` is the real param name. List not date-sorted → whole (tiny) index scanned each run. Only the **budget** salary fields are trusted (annual INR); `misSalary`/`maxSalary` are 0 even when budgets exist. The department is passed to the classifier as `skills`, which is what separates a clinical "Consultant" from "Consultant - HR". The host serves its SPA shell at `/robots.txt`; detected by `<html` sniffing.
- ⛔ **maxhealthcare** — same PeopleStrong platform (adapted from carecareers), `maxhealthcarecareers.peoplestrong.com`, ~66 jobs across Max group entities. Signature quirk: **mixed salary units** — `CTCRange "200000-400000"` is absolute annual INR but `"13-15"` means lakhs; `LAKH_THRESHOLD = 1000` decides. Org hierarchy `MHC>Entity>Dept>Sub-dept` and 8-level location hierarchy parsed. `coun?sultant` regex tolerates the source's own "Counsultant" typo.
- **manipalhospitals** — **Zwayam** ATS (company 15590). List is a **multipart form POST** to `public.zwayam.com/manageESQueries/searchJob` where undefined SPA values must be sent as the literal string `"undefined"` (the endpoint 400s otherwise); returns the entire index (~213 jobs) as raw Elasticsearch hits, no pagination. **Akamai WAF resets any request whose UA contains a custom token** — even `(compatible; ...bot...)` — so this scraper deviates from spec: `USER_AGENT = "Mozilla/5.0"`, contact in the `From:` header. Salary only in detail (plain INR); `apply_detail` re-runs the classifier once the department is known.
- ⛔ **hmg** — Dr. Sulaiman Al Habib Medical Group via **Elevatus (EVA-REC)**: `GET dammam-core-api.elevatus.io/api/v1/jobs?language_profile_uuid=…&limit=50&page=N` with a **mandatory `Accept-Company` header** (406 without it), whose uuid is **discovered at runtime** from each portal's `__NEXT_DATA__`. Eight brand portals walked; listing already carries full description/requirements so there's no detail call. The ATS category is useless here — real roles are filed under the literal category `Default` — another reason it is a raw column, not a decision. No salary (always `{min:0,max:0}`); polymorphic `location` field normalized against a known-cities list, default Riyadh.
- ⛔ **zulekhahospitals** — Zulekha Healthcare Group via **Adrenalin MAX HRIS**: one single token-free `POST /CandidateMAX/CPVacancyDetails/GetVacancyInformationWithoutToken` with `{"CompanyID":"ZULEKHA","Flag":"OP"}`, no pagination. No per-job deep links (SPA state) — every row's URL is the portal itself; dedup rides on `FUNCTION_ID`. ALL-CAPS titles with requisition suffixes (`CARE ADVISOR_608`) cleaned. **Currently near-empty (2 rows)** — unclear if the board is genuinely tiny or a run broke.
- ⛔ **kfshrc** — King Faisal Specialist Hospital & Research Centre. `POST /kfhapis/CustomSearch/GetJobSearchResults` — a first-party proxy in front of **Sitecore Search**. Three contract rules: (1) a rejected payload returns **HTTP 200 with an empty body** (one unknown field name empties everything), treated as a retryable 5xx; (2) past the last page `content` is `null`, not `[]`; (3) the listing carries the whole posting, so there is **no detail request**. **The date trap:** dates are Microsoft-JSON epochs (`/Date(1785013200000)/`) stored at **midnight Riyadh time (UTC+3)** — parsing in UTC dates every posting one day early; `parse_ms_date()` converts in `RIYADH_TZ`. **Legacy scheme**, and its category order is deliberate: pharmacy → nursing → allied/corporate **before** the doctor regex, because "Consultant"/"Specialist"/"Registrar" are medical grades at KFSH&RC but head office reuses the same words. ALL-CAPS source text re-cased with a ~60-acronym preservation list; `is_active` flips false when `applyby` passes.
- **swaasa** — India healthcare board on the **Phenom People** platform: `POST https://www.swaasa.com/widgets` with the same `ddoKey:"refineSearch"` payload the page embeds as `phApp.ddo`; offset/size, newest-first, 50/page. Detail pages parsed by `raw_decode`-ing the `phApp.ddo = {...}` assignment. All nav sub-pages are filtered views over one ~22,900-job index, so only the master index is crawled. Free-form salary strings; dirty `country` field (states/cities leak in) collapsed via allowlist.

### Standalone job-board APIs

- **himalayas** — `GET https://himalayas.app/jobs/api?offset=N`, the cleanest API in the fleet (remote-only board, commissioned as the Glassdoor replacement — Glassdoor is fully robots-blocked and 403s everything). Migrated to `classify_job` (title + the feed's category slugs as `skills` + description); the slugs are supporting evidence only, never an admission path on their own (measured ~50% wrong). ⚠️ The shared classifier **readmits US payer-side noise** the old local list excluded ("Utilization Review Nurse", IME/disability "Physician Reviewer" → Medical Reviewer/MSL, ~24 rows); suppressing it means adding negative keywords to `_shared/taxonomy_keywords.py`, a deliberate fleet-wide change. **Key quirk: deep offset pagination is unstable** — offsets 9,000–9,980 returned 35% re-serves (unstable sort, no cursor; every query param is silently ignored) — so one pass is never complete; coverage improves only after the feed regenerates. `--reclassify` re-applies the gate offline, moving demoted rows to `out-of-scope.csv` (reversible). Feed ships lists as Python-repr strings (`"['United States']"`) → `ast.literal_eval`.
- **vaidyog** — `GET https://jobs.vaidyog.com/api/jobs-for-u?page=N&limit=100`, public unauthenticated JSON (~900 jobs); per-job endpoints need a Bearer token and are skipped. **No posted-date field at all** — the date is decoded from the **first 8 hex chars of the Mongo ObjectId**. Salary shorthand normalization (`30-40` = ₹30–40k/month; implausible values dropped). No public per-job URL (apply is login-gated) → all rows link the board landing page.
- **nextenti** — the React SPA hands out a **short-lived anonymous bearer token with no credentials** (`GET authenticator.nextenti.ai/token`); the scraper mints one at startup, then `POST job-maintenance.nextenti.ai/api/search?page=N` with body `"{}"`. Listing truncates descriptions to 250 chars → detail fetch per new job. The source `profession` field is passed to the classifier as `skills` and kept as a raw column, but does not decide the category — this is a clinical board, so most listings are now dropped as out of scope. No apply link in the API — job URLs are **reconstructed** as `nextenti.ai/jobs/<company-title-city>--<jobId>` (the SPA reads the trailing id).
- **docthub** — `api.docthub.com/jobcenter/jobs`, public JSON, ~16k Indian healthcare jobs partitioned into 18 categories → crawled per-category (free category tags; excluded categories cost zero requests). `EXCLUDE_CATEGORIES` was narrowed in the migration to a purely **crawl-side** saving; everything crawled is decided by `classify_job`. `--enrich` hits the API host, not the HTML pages, because the site's robots disallows expired job slugs. Gained its first club export and `needs_review.csv` in the migration.
- **docthub_roles** — fork of docthub, re-scoped: `classify_job` replaces the keyword lists; **all 18 categories crawled** ("fetch-wide/filter-tight", justified because early-stop makes each extra category ≈1 request/run in steady state — it recovers garbage-titled RA/PV roles filed under "Others"/"Pharmaceuticals"). `INITIAL_WINDOW_DAYS = 2`.
- **naukrigulf** — the most anti-bot-heavy API scraper: **`curl_cffi` with `impersonate="chrome"`** (Akamai blackholes non-browser TLS fingerprints and UAs; contact moves to the `From:` header), plus two magic static headers **`appid: 205` and `systemid: 2323`** without which the API 400s. `GET /spapi/jobapi/search` with `ClusterInd=30,37` (Medical+Pharma) and `Freshness≤15` facets, then **13 keyword walks** (healthcare, clinical research, CDM, PV, drug safety, RA, medical writer/coding/affairs, market access, public health, epidemiology, infection control); `JobId` dedup absorbs overlap. AED/SAR/QAR salaries → club columns blank.

---

### B. WordPress REST scrapers

- **pharmarecruiter** — `GET pharmarecruiter.in/wp-json/wp/v2/posts?categories=<jobs-id>` (server-side category filter excludes `pharma-news`). Job facts parsed from every `<li>` in the post body as `Label: value` pairs (Company Name, Position, Experience, Qualification, Salary…); LPA/k/period salary heuristics; "Fresher Only" → (0,0).
- **pharmarecruiter_roles** — fork using **WP's full-text `?search=` param with 54 terms** (covers title AND body, so a listing naming the domain only in requirements still surfaces), one full pagination per term. `classify_job(title, description)` replaces the local classifier; out of scope → **dropped**. `INITIAL_WINDOW_DAYS = 2`. **Fixed 2026-08-25:** the newest-first early stop used to `break` the whole crawl instead of advancing the term cursor, so only the first term was ever searched (1 term / 100 posts / 8 new jobs per run → 54 terms / 3,250 posts / 14 new jobs, 3m33s). The list was widened in the same change from 17 terms to 54, covering all eleven families (Medical Reviewer had none) and all ten PH sub-categories (had two); short queries are live-vetted for substring noise — `heor` matches "theory", bare `hiv` matches "archive". ⚠️ Its README is a stale copy of the parent's.
- ⛔ **pharmabharat** — two scrapers. `scraper.py`: whole-site posts API (~11,500 posts; every category is a job taxonomy so nothing is filtered); body facts extracted from **three coexisting shapes** (Job Details tables, `<li><strong>Label:</strong>` bullets, labelled paragraphs). **Legacy scheme.** Known bug: it parses `expires_at` but its own `CLUB_COLUMNS` list omits the key, silently dropping the deadline from the club CSV. **`daily_scraper.py`** is the production path and the most operationally hardened script in the repo: server-side filter to the ten role-family categories (slugs resolved at runtime, failing loudly on rename), reads **ACF custom fields** instead of prose, IST timezone pinning for `?after=`, incremental `.daily_state.json` with a 48 h grace window, `fcntl` single-instance lock, atomic writes + fsync, cron exit codes, `--summary-json`, env-var relocation. Its dated output is `pharmabharat_categories.csv` (a **delta**, merged not overwritten — unique in the fleet) plus master `pharmabharat_category_jobs.csv`.
- ⛔ **dubailivejobs** — WP posts API, category 86 = "Healthcare" (~92 posts). Structural caveat: each post is a **company careers page**, not a job — titles are parsed to derive companies, and most rows land in `needs_review` by design (a whole-hospital page can't be assigned a role). `--details` reads per-post **JobPosting JSON-LD** (present on ~1/3) for salary/type; AED → club columns blank.
- **publichealthcareer** — WP **custom `job` post type** (`/wp-json/wp/v2/job`, pxp theme), ~15 jobs, taxonomy terms batch-resolved. Hybrid: REST for core fields + HTML detail scraping for company and salary, because **the site's JobPosting JSON-LD is malformed (missing commas — a plugin bug)** and is parsed by regex instead. Migrated to the shared taxonomy 2026-08-26. "Whole board is public-health domain" is not the same as every post being in scope: generic project-admin titles (Project Manager/Associate, Consultant – Project Technical Support) carry no role-family signal and are dropped — the store reclassified to **3 of 12 kept**. INR salaries do reach the club CSV.

---

### C. SSR payload parsers (`__NEXT_DATA__` and friends)

These fetch normal HTML pages but read the framework's embedded JSON instead of parsing the DOM.

- **shine_roles** — the only shine crawler since the `shine` sibling was retired 2026-08-26 (see `retired-scrappers/`). `shine.com/job-search/<query>-jobs?sort=1`, data from `__NEXT_DATA__ → jsrp.searchresult`. 55 search slugs: one cluster per role family, four industry-facet browses (`ind=13/63/31/61`; BPO `ind=20` deliberately skipped) and the three broad nets `healthcare`/`hospital`/`medical` inherited from `shine`. **The repost re-dating trap:** shine re-dates reposts, so 600+ jobs can share today's date and the watermark cannot bound crawl depth — `MAX_PAGES_PER_QUERY = 50` caps each query and the summary names queries cut off by the cap. Salary strings mix absolute rupees and lakhs, normalized to INR/month. Occupation beats employer: a telesales role *at a hospital* is out of scope. **Public Health slugs were live-probed and pruned** — `health-program` (69,829 hits) and `monitoring-and-evaluation` (46,585, IT monitoring) yielded ~zero in-scope jobs and were dropped; specific disease/program slugs kept. Filter = `classify_job(jJT, jKwd, jJD)` — the card's keyword tags are the `skills` signal. `INITIAL_WINDOW_DAYS = 2`.
- **reed** — `reed.co.uk/jobs/health-jobs` (+ an **unverified best-guess `scientific-jobs` slug** added 2026-08-25) via `__NEXT_DATA__ → searchResults`; source-side `datecreatedoffset=LastTwoWeeks`. **Listing salary numbers lie** — "Competitive salary" rows carry search-band numbers; placeholder type 64 is dropped; `--enrich` reads the real `displaySalary`. GBP → club salary always blank. `displayDate` re-dates reposts; `job_id` dedup absorbs them. It had **no in-scope gate at all** before the migration; `classify_job` is now the gate, which is what keeps the unverified Scientific-sector widening from admitting non-health jobs.
- **simplyhired** — simplyhired.co.in via `__NEXT_DATA__`, fetched with **`curl_cffi impersonate="chrome"`** (TLS fingerprint rejection, like naukrigulf). **Cursor-based pagination** — each response maps upcoming page numbers to opaque tokens; no stable page URLs. Sponsored cards (sometimes months old) interleave on every page, so only organic results vote on the newest-first early stop. The cursor chain dies around page ~73 regardless of the claimed result count, so one query can't backfill a full window. 31 keyword walks (2026-08-25 widening). **Migrated 2026-08-26.** It has no role facet at all, so the keyword walks are pure recall and `classify_job` is the entire precision layer — the store reclassified to **111 of 2,011 kept (5.5%)**, top sub-categories Medical Coding 47 and Public Health Nutrition 46. The site's `qualifications` bullets feed the classifier as `skills`.
- **apna** — the most exotic parser: apna.co dropped `__NEXT_DATA__` for the **Next.js App Router RSC flight stream**. Listing stubs come from HTML regex; each detail page's `self.__next_f.push([1,"…"])` chunks are concatenated, the job object is lifted by a string-aware balanced-brace scan around `"created_on"`, and RSC refs like `"$3d"` are resolved — including `T<hexbytelen>` text chunks located by **binary-searching the character count whose UTF-8 encoding matches the byte length**. The department/deny-list gate runs before the detail fetch, and dedup runs before the fetch too (known ids cost zero requests) — both crawl-side savings. **Legacy scheme**, and the oldest shape in the repo: rich CSV only, no `_shared` import, no club export (and the parser is broken — see §6).
- ⛔ **dubizzle** — see section F (browser-driven), but data-wise it's also an SSR parser: the whole Algolia result set is server-rendered into `__NEXT_DATA__` redux state.

---

### D. HTML + JSON-LD / microdata scrapers

Plain `requests`, honest UA (mostly), listing parsed from server HTML, detail facts from schema.org JobPosting structured data.

- **freshersworld** — four category listings (`health-care`, `pharma`, `regulatory-affairs`, `research`), cards regexed via their machine-readable `job_id=`/`job_display_url=` attributes; detail = JSON-LD with structured salary/experience/validThrough. Uses `limit=20` deliberately (only `limit=25` is robots-disallowed). An "ago" estimate pre-filter skips detail fetches only when clearly out of window (±5-day margin). A failed page is treated as empty and the crawl continues — breaking on first failure once hid 475 in-window jobs. Slow ~900 KB pages; one agency posts 250+ city clones.
- ⛔ **gulftalent** — `gulftalent.com/<country>/jobs/industry/healthcare` (industry 15 — source-side filter). The JSON search API exists but 500s on every results request, so listing rows are regexed from server HTML and details come from JSON-LD. **Desktop-UA trick:** non-browser UAs get 302'd to a crippled `/mobile/` site, so the UA leads with a `Mozilla/5.0 (Macintosh…)` platform token before its honest name. Listing dates have no year → pre-filter only. Postings purge at ~90 days → `INITIAL_WINDOW_DAYS = None`.
- **gulftalent_roles** — the five-country (`uae,saudi-arabia,qatar,oman,kuwait`) fork; `classify_job(title, job_function, description)` — the site's Job Function is the `skills` signal. ⚠️ `_PHARMA_RE` has been restored (an earlier refactor deleted it while `classify_company_type()` still called it, raising `NameError` on the first job of every run).
- ⛔ **hziegler** — Helen Ziegler & Associates (agency placing nurses/allied/physicians in SA/AE/CA), ~54 postings on a static site. Four index pages merged on URL; detail = JSON-LD; dedup key = URL slug. No salaries anywhere (agency practice — benefits stay in the description). `hiringOrganization` is always the recruiter, so the real employer comes from a page sidebar; confidential mandates become `Confidential Client (via Helen Ziegler & Associates)`. **Legacy scheme**, where title beats section — HZA files *Psychologist* under PHYSICIANS. Index links carry no date → out-of-window slugs are remembered in `seen_old_ids.csv`.
- **internshala** — **all 173 live job categories** (fetch-wide, derived from the site's own category sitemaps), because its category facet is far too loose to scope a crawl with: `biostatistics-jobs` returns maths teachers, `nurse-jobs` returns biology teachers. **Migrated to the shared taxonomy 2026-08-25**, which is what makes that widening safe — `classify_job` does all the rejecting. **Strictest robots handling in the fleet:** the site disallows any URL containing `?` or `,`, enforced both at startup and per-request (`url_is_clean`); pagination is path-based (`/page-N/`). An empty category **301-redirects to all-jobs** — detected by checking the final URL, otherwise one empty slug would crawl the whole site. Listings only roughly recency-ordered → every page crawled.
- **jobberman** — Nigeria's healthcare vertical. Details embed a schema.org **`@graph`** parsed with a lax JSON decoder (raw control characters) and `@id`-reference resolution. **Robots caps pagination**: `Disallow: /*page=*` with explicit Allows for pages 2–10 only — Python's robotparser can't evaluate that, so `MAX_PAGES = 10` is enforced in code. Card ages ("1 month ago") are treated as an *optimistic* bound for skipping. NGN salaries → club columns blank; `PostalAddress` fields are shuffled upstream so the card's location chip wins. **Migrated 2026-08-26:** the healthcare vertical is a *sector* facet carrying both back-office and bedside jobs, so the classifier drops most of it — **10 of 197 kept (5.1%)**. Because the description only exists on the detail page, dropped slugs are recorded in `out-of-scope.csv` and skipped on later runs, or every run would re-fetch them.
- **michaelpage** — Michael Page India's healthcare vertical (Drupal, executive search — mostly corporate, non-clinical mandates). Pure HTML-regex listing + JSON-LD detail (lax decoder for raw control chars). **The `seen_old_ids.csv` mechanism:** cards carry no date or salary, so a new ref's detail must be fetched before the cutoff can be judged; out-of-window refs are remembered and never re-fetched — steady state ≈ 5 listing requests + 1 detail per genuinely new job. Clients are confidential: `company` = "Michael Page", card highlight bullets become `company_about`. **Migrated 2026-08-26:** healthcare-industry at the source is not an in-scope role — medical-affairs/MSL, clinical-research and regulatory mandates survive, commercial and plant leadership do not (**2 of 47 kept**). `out-of-scope.csv` doubles as a detail-fetch skip list, mirroring `seen_old_ids.csv`.
- ⛔ **narayanahealth** — SAP **SuccessFactors Career Site Builder** (`jobs.narayanahealth.org`). Robots disallows the JSON backends but allows `/search/` and detail pages → HTML on purpose: search rows regexed from `<tr class="data-row">`, details from schema.org **microdata** `<meta itemprop=…>` tags. Two date formats parsed ("24 Jul 2026" and "Fri Jul 24 00:00:00 UTC 2026"). Dedup key is the numeric id in the URL, not the display requisition id. Covers India + Health City Cayman Islands.
- ⛔ **workindia** — **daily latest-JD sitemap + JSON-LD**: `crawlsitemap.workindia.in/sitemap.xml` → `latest-jd-pages.xml` (~14k URLs refreshed daily). The URL itself encodes `<title_slug>-<area>-<city>-<job_id>`, so slug filtering and id dedup run **before any detail fetch** — a daily run is 2 sitemap fetches + one request per new healthcare job. **CloudFront rejects bot UAs even for robots.txt** → the UA is a full Chrome string with the scraper name appended. Robots bans query strings; hard-asserted per request. Slug gate: ALLOW (nurse, ANM/GNM, pharma, `hospital(?!ity)`…) / AMBIGUOUS (caretaker, therapist… — the canonical example being a "Caretaker" posted by a fish market) → kept+flagged. **Legacy scheme**, so the slug lists are both the crawl-side saving and the classification. JSON-LD supplies datePosted, validThrough, baseSalary, monthsOfExperience.

---

### E. Browser-capture scrapers (anti-bot walls too high for HTTP)

These cannot fetch at all from a script; a human-driven real browser captures the JSON and an offline script transforms it.

- **foundit** — foundit.in (ex-Monster). Akamai TLS fingerprinting 403s every plain client; the private `/middleware/` API is robots-disallowed anyway. **Ship-with-repo workflow:** (1) `python3 receiver.py captures/<date>.json 8765` — a tiny localhost HTTP sink whose `Access-Control-Allow-Private-Network: true` header is what lets a public https page POST to 127.0.0.1; (2) paste `capture.js` into the DevTools console on any foundit page — it fetches 25 keyword SERPs same-origin (inheriting the real TLS fingerprint), extracts the **RSC flight stream** (`self.__next_f.push` chunks), brace-matches `jobSearchAPIData` out, and resolves `$xx` description refs whose length prefix is in UTF-8 **bytes** via binary search over character counts; Akamai pacing = 4 s + jitter per page, 45 s breather every 12 pages with partial POSTs, 90 s sleep on 403. `window.__cap` exposes progress/abort/resume. (3) `foundit_scraper.py` transforms offline: date window (IST) → `classify_job` → dedup → rich CSV + 22-column club CSV. SERPs are relevance-ordered and any query param breaks the page suffix → no early stop; pages past ~20 return HTTP 410. A full run is ~30–45 min of babysitting the browser tab.
- **indeed** — in.indeed.com remote India. Cloudflare 403s every scripted client; robots allows `/jobs?q=…` but **forbids pagination** (`/*&start=`) and `/viewjob` → **search page 1 only, 27 queries**, ≤15 organic cards each. No capture.js/receiver here — console snippets in the README read `window.mosaic.providerData["mosaic-provider-jobcards"]` and transfer via `copy()` to the clipboard (or save pages as HTML for `--from-html`, which brace-matches the mosaic object). **Full descriptions via the `vjk` trick:** `/jobs?...&vjk=<jobkey>` is an *allowed* URL that renders the job's description pane; a console snippet stashes each into `sessionStorage`, collected into `<date>-descriptions.json` and merged with `--descriptions`. `pubDate` is epoch ms pinned ~05:00 UTC — take the UTC date, don't localize to IST or "Just posted" rolls into tomorrow. ~50 rapid loads trigger a Turnstile a human must click.
- **naukri** — naukri.com. Two compounding blocks: `/jobapi/v3/search` requires a **per-request signed `nkparam` header** computed by the site's own JS (406 "recaptcha required" without it), and Akamai resets non-browser TLS anyway. So a **fetch/XHR interceptor** pasted once on the listing page accumulates `jobDetails` by `jobId` into `window.__jobStore` while a human pages through; the dump goes to `captures/<DD-MM-YYYY>.json` and `naukri_scraper.py` ingests it offline (shape-tolerant loader; no network — `requests` isn't even a dependency). Cards are self-sufficient (salary, skills, description, epoch-ms dates) — `tagsAndSkills` is the `skills` signal the classifier weighs at ×2. `INITIAL_WINDOW_DAYS = 7`.
- **naukri_roles** — fork of naukri re-scoped to the role families: same capture mechanics but the recipe pages through **16 per-family searches** (clinical research, CDM, PV, RA, medical writing/coding, MSL, HEOR, TMF, public health, epidemiology…) instead of the 41k-job healthcare listing. `classify_job(title, tagsAndSkills, description)`; out of scope → dropped; rich CSV carries the `all_families`/`family_scores`/`matched_in` trace. Measured on old captures: 19% of cards in scope, 12% of those with no title match at all (rescued by skills/description). Captures go stale fast against the 2-day window — take a fresh one before running.

---

### F. Browser-driven scraper

- **dubizzle** — `dubai.dubizzle.com/jobs/medical-healthcare/` behind **Imperva/Incapsula**. The docstring records what failed: plain requests, `curl_cffi` chrome impersonation, headless-shell, new-headless Chromium — only **headed** Chromium passes. So it drives Playwright `launch_persistent_context` (profile in `.pw_profile/` so Imperva cookies survive daily runs), polls for `__NEXT_DATA__` up to 10×6 s with re-navigations on polls 3 and 6, and reads the Algolia results from the redux state (`listings/fetchListingDataForQuery/fulfilled`). Robots honored — the disallowed `/api/` endpoints are never called. Listing not date-sorted (highlighted ads first) → all pages scanned, cutoff per ad. AED salary buckets parsed; epoch dates converted in Asia/Dubai. Cron caveat: needs a logged-in GUI session.

---

### G. Government / portal-emulation scrapers

- **dha** — Dubai Health Authority's Sheryan opportunities board (IBM **WebSphere** portal; `careers.dha.gov.ae` is NXDOMAIN — it doesn't exist). **Runtime endpoint discovery:** two bootstrap GETs regex the portlet action URLs out of the live pages, so nothing is hardcoded and a WebSphere redeploy doesn't break the scraper — if the page shape changes it exits before writing anything. Search responses are **JSON nested inside a JSON string** (parsed twice); detail fields are decoded from JS string literals with a hand-written unescaper. **The board publishes no dates at all** → `first_seen_date` is recorded and honestly used as `posted_at`; incrementality rides on sequential job ids. The portal's licence-register category is kept as a raw column (it misfiles Pharmacist under Allied Health), not as the category. City/job-type/experience derived from free text only when stated.
- ⛔ **dubaihealth** — Dubai Health via **Oracle Taleo** on the Dubai Careers portal. A cookie-less POST 500s, so a bootstrap GET grabs the Taleo session cookie; the session sends `tz: GMT+04:00` headers Taleo expects. Search POST carries an `ORGANIZATION=[4105100456]` filter (the source-side healthcare gate). Detail state is a **serialized Taleo blob** — one `<input>` split on `!$!`/`!*!`/`!|!` tokens read by fixed positional offsets with sanity guards (brittle by nature, all best-effort). AED salaries → club columns deliberately blank.
- ⛔ **phcc** — Qatar's Primary Health Care Corporation on **Oracle E-Business Suite iRecruitment** — server-rendered OAF HTML, no API. Browser-flow emulation over `requests`: follow the session-scoped Job Search link, submit the search form with **all 17 professional-area options at once** (the form rejects an empty search), re-read the per-render MAC tokens (`FORM_MAC_LIST` etc.) from every page about to be submitted, parse results off stable span ids, replay the record-set navigator for more pages. Detail pages are bookmarkable (`p_svid`). ⚠️ **`careers.phcc.gov.qa/robots.txt` is Oracle's stock `Disallow: /`** — the scraper exits at startup and only runs with `--ignore-robots` (the README argues it's an AutoConfig default, but says use only with PHCC authorization). HR-code titles (`063.Operations.Allied Health.Technologist`) unpacked and flagged; no salary, no closing dates; open vacancies live 7+ months → no initial window.
- ⛔ **moh** — Saudi Ministry of Health on **SharePoint 2013**. No job API; the real applicant system (`erp.moh.gov.sa` Oracle iRecruitment) is unreachable from the public internet. Robots is load-bearing: `?PageIndex=` (the listing's pagination) is disallowed, so the archive is enumerated from **sitemap.xml** (528 announcement pages) merged with listing page 1; slug-embedded dates (`ads-2023-10-04-001`) let out-of-window pages be skipped without a request. Two streams: announcements (a classifier decides *is this even a job* — ~40% are; tenders/hackathons denied, training tracks kept+flagged) and the "Work For Us" recruitment-plan tables. **Hijri→Gregorian conversion** implemented via the arithmetic Kuwaiti calendar (±1 day vs Umm al-Qura), with the verbatim window sentence always kept auditable. `refresh_activity()` re-evaluates each stored row's open/closed state every run, in the rich CSV only — the club `is_active` column is retired. Announcement-level rows (one row = one specialty batch Kingdom-wide). `INITIAL_WINDOW_DAYS = 365`.
- **nhm** — the National Health Mission *policy* portal, which has **no job board**: four announcement pages fetched as HTML, anchors regexed and filtered by recruitment-notice keywords. **Zero rows is the documented steady state** — real NHM hiring lives on state portals (Assam, UP, Maharashtra…), each of which would need its own scraper. No dates → first-seen; every captured row force-flagged (they're notice PDFs, not job cards). robots.txt returns a 404 page with HTTP 200, detected by HTML sniffing.

---

### H. Agency AJAX scraper

- ⛔ **profco** — Professional Connections (nursing/allied placements in Saudi, Ireland, UK, Australia). Three form-encoded AJAX endpoints returning small HTML fragments (`getJobSearchResult`, `getJobSpecialitySelect`, `getContent`). **The signature mechanism: a 50-row server cap with adaptive drill-down** — an unfiltered search first, then per-category; only a category returning exactly 50 gets drilled per speciality, and only a capped speciality per country; a still-capped slice sets `complete=False`. **No dates on the site** → first-seen dates, watermark no-op. Country resolved from salary currency first (USD deliberately *not* a signal — Saudi posts quote SAR or USD), then a city map, then a title prefix; unresolved → flagged. **The only scraper with an `is_active` lifecycle**: rows that vanish from the board export `is_active=false` — but only when the crawl was complete, so a partial crawl can't mass-deactivate. On the legacy scheme, so `is_active` is still a club column here. Confidential mandates → `company = "Profco (Professional Connections)"`.

---

## 6. Known issues (as of 2026-08-25)

The `gulftalent_roles` `_PHARMA_RE` crash and the `jobslly` scraper are resolved and deleted respectively. What remains:

1. **Two schemes coexist, by decision.** Some scrapers are migrated to the shared classifier and the 22-column schema; a set is excluded for now and keeps the legacy profession enum and the older club schema; the rest are mid-migration. **A downstream import must therefore handle both `category` vocabularies**, or filter by scraper. `instructions/taxonomy-migration-status.md` is the authoritative, single-copy list of which is which, and the live worklist for the rest.
2. **Stored rich CSVs still need their one-off reclassification** on migrated scrapers (demoted rows move to `out-of-scope.csv`, reversibly) — see the spec's "Stored-data reclassification".
3. **`apna` is broken** — apna.co moved to the Next.js App Router and the parser needs a rewrite.
4. **Club CSVs are full-store dumps, not daily deltas** (except `pharmabharat/daily_scraper.py`). An import expecting a day's new jobs would re-import history.
5. **`needs_review.csv` semantics vary**: most overwrite per run (stale files linger after clean runs); a few append+dedupe; several write to the CWD rather than the scraper folder.
6. **Structurally empty / stalled sources**: `nhm` (0 rows by design), `zulekhahospitals` (2 rows — worth a live probe). `gulftalent` was retired 2026-08-25 (1 row, and a strict crawl-subset of `gulftalent_roles`).
7. **Per-scraper READMEs lag**: the `*_roles` forks were originally copied verbatim from their parents, and several `scrappers/<site>/README.md` files still describe the profession enum. For an excluded scraper that description is correct; for a migrated one it is stale. Treat this guide, `_shared/README.md` and the status doc as authoritative where they disagree.
8. **A shared-classifier judgement call is open**: on `himalayas` the shared classifier readmits ~24 US payer-side rows (utilization review / IME physician reviewer) the old local list excluded. Suppressing them means new negative keywords in `_shared/taxonomy_keywords.py` — a fleet-wide change, so it needs a decision first.
9. **Small code warts**: `role_families._QUALIFICATION_RES` compiled twice; `profco`'s `excluded_non_healthcare` counter printed but never incremented.
10. **Dead/blocked sources documented in-repo**: `careers.dha.gov.ae` (NXDOMAIN), Glassdoor (fully robots-blocked + 403), `fortishealthcare.com` / `apollohospitals.com` / `purehealth.ae` (browser-only fronts, bypassed via their ATS APIs), `apna --enrich` (documented no-op kept for old cron lines).
