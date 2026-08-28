# How each scraper works

A reference for all scrapers under `scrappers/`. Each subdirectory is one
scraper (entrypoint `scraper.py` or `<name>_scraper.py`); `_shared/` holds the
one shared classifier and the club schema, not a scraper.

## The shared model

Almost every scraper follows the same shape:

1. **Fetch** from the source (JSON API, server-rendered HTML/JSON-LD, sitemap,
   or a browser capture).
2. **Filter to scope.** Source-side facets (a site's own healthcare
   category/industry filter) may narrow the crawl to save requests. On a
   **migrated** scraper the final keep/drop and labeling decision is then made
   by the one shared classifier, `_shared/classification.py` (`classify_job`):
   every job gets `category` = **"Non Clinical" | "Public Health"** and one of
   the **20 sub-categories** (see `Jobs_keywords/keywords_for_jobs.md`).
   Out-of-scope jobs are dropped and counted as `excluded_out_of_scope`;
   ambiguous-title jobs are kept and flagged `needs_review`. Scrapers named
   `*_roles` are re-scoped forks of older siblings.
3. **Incremental window.** First run keeps the last N days; later runs keep
   only jobs newer than the newest stored `posted_date` minus a grace margin,
   and stop paginating once a whole page predates the cutoff (feeds are
   newest-first).
4. **Output** (called "standard" below): a rich cumulative CSV (dedup key =
   job id) as the source of truth — carrying `category`, `sub_category`, and
   the classifier's trace columns (`role_family`, `all_families`,
   `family_scores`, `family_confidence`, `matched_in`, `needs_review`) — plus
   `jobs_csv/<DD-MM-YYYY>/<site>.csv` in the club's 22-column schema
   (`CLUB_COLUMNS` imported from `_shared/classification.py`; the old
   `is_active`/`expires_at` columns are retired), plus a `needs_review.csv`
   sidecar for jobs kept but flagged (nothing in scope is ever silently
   dropped; out-of-scope jobs are dropped with a printed counter).

**⚠️ The migration is nearly complete.** Three scrapers are still excluded
from it and each still runs its **own** classifier on the legacy profession
enum (`doctors | nurses | pharmacists | non_clinical`) with the older club
schema — `is_active`/`expires_at` present, no `sub_category`, and nothing
dropped for scope (unplaceable titles fall back to `non_clinical` and are
flagged). Steps 2 and 4 above describe the migrated scrapers and are what any **new** scraper
must do. `instructions/taxonomy-migration-status.md` holds the authoritative,
single-copy list of which scrapers are on which scheme; it is deliberately not
duplicated here.

The sections below give, per scraper: **site**, **how it fetches**, **access
quirk**, and **how it scopes**. "Scope" here means **which URLs, facets, or
queries the scraper walks** — the crawl-side recall decision, which is
unchanged by the migration and is what makes each scraper interesting. Which
classifier then judges the fetched jobs is not repeated per entry: look the
scraper up in `instructions/taxonomy-migration-status.md`. Output is standard
unless noted.

---

## Group A — General job boards (India)

These are large multi-sector boards; healthcare/role jobs are a slice, so they
either filter at the source facet or lean on the classifier.

### shine / shine_roles
- **Site:** shine.com (India), Next.js.
- **Fetch:** server-rendered `/job-search/` pages with the full result payload
  in `<script id="__NEXT_DATA__">`. No detail fetch, no browser.
- **Quirk:** `/api/*` is robots-disallowed (listing pages are fine); Shine
  re-dates reposts, so a date watermark can't bound crawl depth — pages are
  capped per query.
- **Scope:** `shine_roles` is the re-scoped fork — it runs **industry-facet
  browses** (`?ind=13` Healthcare, `63` Pharma, `31` NGO, `61` KPO) to catch
  garbage-titled jobs, plus per-role keyword searches, then the classifier gate.

### naukri / naukri_roles
- **Site:** naukri.com (India's biggest board).
- **Fetch:** **browser-capture** — the `/jobapi/v3/search` JSON needs a
  per-request signed `nkparam` header, so a human/browser pages through the
  listing while an interceptor accumulates `jobDetails` into
  `captures/<date>.json`; the scraper ingests that JSON offline (`--from-json`).
- **Quirk:** signed header + Akamai TLS reset make plain HTTP impossible; AI
  crawlers are robots-blocked from `/`, so only human-driven capture is used.
- **Scope:** `naukri_roles` captures **per-role searches** (clinical research,
  pharmacovigilance, regulatory affairs, …) plus the Healthcare & Life Sciences
  functional-area browse, deduped, then the classifier gate.

### foundit
- **Site:** foundit.in (ex-Monster India).
- **Fetch:** **browser-capture** — ships its own `capture.js` (walks ~25
  healthcare + role keyword search pages, extracts cards from the RSC
  `__next_f` flight stream) and `receiver.py` (localhost sink).
- **Quirk:** Akamai-walled (403 to all plain HTTP); paces itself 4–7s/page with
  breathers; a full run is ~30–45 min.
- **Scope:** ~25 healthcare + role-family keyword SERPs walked by `capture.js`.

### indeed
- **Site:** in.indeed.com (remote-in-India healthcare).
- **Fetch:** **browser-assisted** — reads the React model embedded in each SERP
  (`window.mosaic...results`). Pagination is robots-forbidden, so coverage
  comes from **breadth**: one page each of ~27 queries (broad healthcare + the
  eleven role families), deduped by jobkey.
- **Quirk:** Cloudflare 403 to scripted clients; `&start=` pagination and
  `/viewjob` forbidden; full descriptions retrieved via the allowed
  `?vjk=` SERP-pane technique.
- **Scope:** the ~27-query set is the whole scoping mechanism (remote +
  healthcare + the eleven role families); one page each.

### simplyhired
- **Site:** simplyhired.co.in (India; Indeed-family).
- **Fetch:** `/search` pages with results in `__NEXT_DATA__`; cursor-paginated.
- **Quirk:** needs browser-TLS impersonation (`curl_cffi` chrome).
- **Scope:** **one cursor walk per keyword** (healthcare + eleven role terms),
  newest-first, deduped by jobKey.

### internshala
- **Site:** internshala.com (India, entry-level/jobs).
- **Fetch:** server-rendered category listing pages `/jobs/<slug>-jobs/`; detail
  pages carry JobPosting JSON-LD (fetched for new in-window jobs).
- **Quirk:** empty categories 301-redirect to `/jobs/` (detected and skipped);
  `?`/`,` URLs robots-disallowed.
- **Scope:** healthcare + role category slugs at the source, plus five
  adjacent ones (psychology, biotech, …) crawled but flagged unless the title
  shows a healthcare keyword.

### workindia *(retired)*
- **Site:** workindia.in (India, blue/grey-collar).
- **Fetch:** the daily `latest-jd-pages.xml` **sitemap** (~14k job URLs);
  each detail page has JobPosting JSON-LD. Job id is in the URL, so seen jobs
  are skipped before any request.
- **Quirk:** query-string URLs (`/*?*`) robots-disallowed — only clean paths.
- **Scope:** URL title-slug allow/ambiguous regex — a **crawl-side** filter
  that decides which of the ~14k sitemap URLs are worth a detail fetch.

---

## Group B — Pharma / niche vertical boards

### pharmabharat *(retired)*
- **Site:** pharmabharat.com (India pharma). **Fetch:** open **WordPress REST
  API** (`/wp-json/wp/v2/posts`), full article HTML, newest-first (~11.5k
  posts). **Scope:** whole site is pharma, so every post is scraped and the
  source category slug is kept as a rich-CSV column.

### pharmarecruiter / pharmarecruiter_roles
- **Site:** pharmarecruiter.in (India pharma). **Fetch:** open WordPress REST
  API, `jobs` category only (skips `pharma-news` at source). **Scope:** the
  `_roles` fork adds WP full-text `?search=` walks per role-family term — 54
  terms since 2026-08-25 (all eleven families + all ten PH sub-categories),
  one page per term in steady state because the date watermark stops each one.
  The parent walks the whole `jobs` category with no keyword filter, so it
  backstops anything the term list misses.

### docthub / docthub_roles
- **Site:** jobs.docthub.com (India healthcare). **Fetch:** public JSON API
  (`api.docthub.com/jobcenter/jobs`), fully structured, crawled per category
  facet. **Scope:** `docthub_roles` crawls **all** categories (widened from
  one) — early-stop makes each extra category ≈1 request/run in steady state,
  and it recovers garbage-titled roles filed under "Others".

### swaasa
- **Site:** swaasa.com (India healthcare; Phenom People platform). **Fetch:**
  Phenom `/widgets` JSON endpoint (`refineSearch`), one master index newest-
  first (~22.9k). **Scope:** healthcare board; category/country kept as columns.

### vaidyog
- **Site:** healthcarejobs.vaidyog.com (India; React SPA). **Fetch:** public
  unauthenticated `/api/jobs-for-u` JSON, full description in the listing.
  **Quirk:** no posted-date field — derived from the Mongo `_id` timestamp;
  detail endpoints need a Bearer token (unused). **Scope:** healthcare-only
  board (no extra filter).

### nextenti
- **Site:** nextenti.ai (India healthcare; React SPA). **Fetch:** JSON
  microservice — grabs an **anonymous bearer token** from `authenticator.
  nextenti.ai/token`, then POSTs `job-maintenance.../api/search`. 30/page,
  newest-first. **Scope:** healthcare board.

### publichealthcareer
- **Site:** publichealthcareer.org (India public-health). **Fetch:** WordPress
  REST API custom `job` post type; company/salary scraped from each detail
  page's sidebar (its JSON-LD is malformed). **Scope:** public-health board.

### himalayas
- **Site:** himalayas.app (remote jobs, global). **Fetch:** public
  `/jobs/api`, offset-paginated 20/page, strictly newest-first, full
  descriptions in the listing. **Quirk:** deep offset pagination re-serves
  ~35% of rows (tied sort key) — union across feed snapshots to converge.
  **Scope:** the whole feed is walked; the board's own category slugs are
  passed to the classifier as `skills` (they are explicitly *not* an admission
  path on their own — measured ~50% wrong).

---

## Group C — Gulf / international boards

### naukrigulf
- **Site:** naukrigulf.com (UAE/Saudi/Qatar/…). **Fetch:** public `/spapi/`
  JSON (needs static `appid: 205` / `systemid: 2323` headers). **Quirk:** Akamai
  needs browser-TLS impersonation. **Scope:** industries Medical(30)+Pharma(37)
  at source, **one walk per keyword** (healthcare + role terms).

### gulftalent *(retired)* / gulftalent_roles
- **Site:** gulftalent.com (Gulf). **Fetch:** server-rendered listing per
  country+industry (`/jobs/industry/healthcare`) + each detail page's
  JobPosting JSON-LD (the JSON API only returns facet counts). **Quirk:**
  non-browser UA gets 302'd to a broken `/mobile/` site — needs a desktop UA
  token. **Scope:** `gulftalent_roles` walks **all major countries** (UAE,
  Saudi, Qatar, Oman, Kuwait) by default, against the base scraper's single
  country+industry listing.

### reed
- **Site:** reed.co.uk (UK Health & Medicine + Scientific). **Fetch:** listing
  results in `__NEXT_DATA__`, plain numeric pagination; detail pages with
  `--enrich`. **Quirk:** listing salary numbers are placeholders for
  "Competitive"/undisclosed rows (dropped or overridden via detail).
  **Scope:** walks both sector listings (Health & Medicine, Scientific).

### dubizzle *(retired)*
- **Site:** dubai.dubizzle.com (UAE classifieds). **Fetch:** healthcare
  category listing with the full **Algolia** result set in `__NEXT_DATA__`.
  **Scope:** source category; AED salary buckets kept rich-only (club schema
  is INR/USD).

### dubailivejobs *(retired)*
- **Site:** dubailivejobs.com (UAE; WordPress content-farm). **Fetch:** posts
  REST endpoint filtered to the **Healthcare category (id 86)**. **Quirk:**
  each post is a *company careers page* (many roles), so company from title and
  category best-effort/flagged.

### hziegler *(retired)*
- **Site:** hziegler.com (Helen Ziegler — agency placing into Saudi/UAE/Canada).
  **Fetch:** small hand-built static site (~54 jobs) — server-rendered index +
  detail pages, no API/sitemap. **Scope:** four index pages, merged on URL.

### profco *(retired)*
- **Site:** profco.com (Professional Connections — nursing/medical agency for
  Saudi/Australia/UK). **Fetch:** form-encoded AJAX endpoints
  (`/ajax.php?task=...`) returning small HTML fragments. **Scope:** iterates
  its own category/speciality selectors (adaptive drill-down around a 50-row
  server cap).

### jobberman
- **Site:** jobberman.com (Nigeria). **Fetch:** server-rendered healthcare
  vertical listing (16/page). **Quirk:** robots allows only `page=2..10` and
  `/listings/<slug>` detail pages (`/api/`, `/job/` disallowed) — hard-capped
  at 10 pages. **Scope:** source healthcare vertical.

---

## Group D — Hospital & government career portals (ATS-backed)

Each is a single employer/authority, so every posting is healthcare at the
source and there is nothing to filter on the crawl side — these walk the whole
board via the site's own ATS JSON/HTML. Being a single-employer board buys no
exemption from classification, and it changes the arithmetic sharply: under the
two-level taxonomy most hospital requisitions are bedside/clinical and are
dropped as out of scope, where the legacy scheme kept them all under a
profession label. Grouped by the platform they run on.

> **RETIRED.** Entries marked *(retired)* — here and throughout this file —
> were moved out of the active fleet to `retired-scrappers/` because they
> yielded almost nothing under the taxonomy. Two waves: **2026-08-25**, the
> bedside-only ATS boards, at 0-3 rows each; and **2026-08-26**, seven
> low-yield sources re-run **live** and scored at 0-0.5%
> (narayanahealth, kfshrc, zulekhahospitals, dubailivejobs, dubizzle,
> hziegler, profco), plus `pharmabharat`, retired by user decision despite
> scoring 78%. Their code, tests and stored CSVs are unchanged and any of
> them can be revived with one `git mv` — see `retired-scrappers/README.md`.
> The notes are kept here because they are what makes reviving one cheap.

**Oracle Recruiting Cloud (public token-free CE REST API**,
`recruitingCEJobRequisitions`, newest-first, detail endpoint for new jobs):
- **apollohospitals** *(retired)* — Apollo Hospitals, India (site `CX_2`).
- **fortis** *(retired)* — Fortis Healthcare, India (`CX_1`; corporate host is
  Cloudflare-walled, Oracle host is open).
- **sidra** *(retired)* — Sidra Medicine, Qatar (`Sidra-Career-Site`).
- **seha** *(retired)* — SEHA / Abu Dhabi Health Services, UAE (`CX_1`).
- **medcare** *(retired)* — Medcare / Aster DM, UAE (`CX`, isolated by the Medcare
  organization facet within the group tenant).
- **purehealth** *(retired)* — PureHealth Group, UAE (`CX_6007`, via talentone.ae).

**PeopleStrong (Alt Recruit) Angular SPA** (`/api/cp/rest/altone/cp/jobs`
JSON, `{}` body):
- **carecareers** *(retired)* — Quality Care India (CARE Hospitals / KIMS).
- **maxhealthcare** *(retired)* — Max Healthcare group, India.

**Other platforms:**
- **manipalhospitals** — Manipal Hospitals, India; **Zwayam** platform,
  `public.zwayam.com/manageESQueries/searchJob` returns the whole index in one
  Elasticsearch hit list.
- **narayanahealth** *(retired)* — Narayana Health, India; SAP **SuccessFactors** — JSON
  backends are robots-disallowed, so server-rendered `/search/` HTML + detail
  pages are used.
- **kfshrc** *(retired)* — King Faisal Specialist Hospital, Saudi Arabia; first-party JSON
  proxy in front of **Sitecore Search** (`GetJobSearchResults`); the listing
  carries the entire posting. (Watch the Riyadh-midnight date boundary.)
- **hmg** *(retired)* — Dr. Sulaiman Al Habib Medical Group, Saudi Arabia; **Elevatus
  (EVA-REC)** public JSON API (`*.elevatus.io/api/v1/jobs`), full description
  in the listing.
- **dubaihealth** *(retired)* — Dubai Health, UAE; routed through the **Dubai Careers**
  government portal (Oracle **Taleo**), `searchjobs` JSON filtered to the
  Dubai Health organization id.
- **dha** — Dubai Health Authority, UAE; the **Sheryan Opportunities** board
  (IBM WebSphere portal) — vacancies from DHA-licensed facilities via one JSON
  resource URL.
- **moh** *(retired)* — Saudi Ministry of Health; no job API — recruitment **announcement
  pages** enumerated from the site sitemap (the real iRecruitment portal is
  behind SSO / not publicly reachable).
- **nhm** — National Health Mission, India; central portal has no job board —
  scrapes recruitment-advertisement **links/PDFs** from its announcement
  surfaces by title regex.

---

## Group E — Employer boards on Workday CXS

One employer per scraper, all speaking the same protocol, so these differ from
each other only in a config block. The sourcing rationale: PharmaBharat's
"apply" links were harvested from its cached archive and resolved to the
career sites behind them (`HR_info_extraction/source_companies.csv`), which
turned out to be ~60 Workday tenants — the upstream of a large share of the
India clinical/PV/regulatory supply that pharmabharat reposts. Going to the
tenants directly gets that supply fresher and whole. The per-tenant registry
(host, site slug, live board size, robots status, quirks) is
`instructions/workday-tenant-probe.csv`.

**The shared shape.** Workday's Candidate Experience Site API is public and
unauthenticated: `POST /wday/cxs/{tenant}/{site}/jobs` with
`{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}` for the listing
(`limit` is hard-capped at 20 — more answers HTTP 400), then one
`GET /wday/cxs/{tenant}/{site}/job/{externalPath}` per requisition for the
description, exact `startDate`, `timeType`, ISO country code and canonical
URL. The listing's *relative* `postedOn` label ("Posted Today" … "Posted 30+
Days Ago") decides the time window **before** a detail request is spent;
out-of-window ids go to `seen_old_ids.csv` unfetched. Requests need a
Mozilla-compatible UA and `Accept-Language` (bare clients get HTTP 406).
Workday boards never show pay → `salary_raw = "Not Disclosed"`. `scrappers/iqvia/`
is the reference implementation; every entry below is that file with a changed
config block, docstring, and the `parse_city` amendments noted next.

**Ordering is not guaranteed — check it per tenant.** The template early-stops
after 2 consecutive out-of-window pages, which is only sound on a newest-first
board. Two tenants here are **not** newest-first (**accenture**, **cencora**);
both disable the early stop and walk the listing to its end instead. Probe
before trusting it by comparing `postedOn` at offsets 0/100/200.

**A display cap and unsound ordering are two different findings — don't
conflate them.** An exactly-round `total` (accenture and freseniusmedicalcare
both report 2000) means the board is larger than it admits and its oldest tail
is unreachable. That alone says nothing about ordering. accenture has both
problems and needs the full walk; **freseniusmedicalcare has the cap but is
strictly newest-first** (verified monotonic from offset 0 through 1980), so the
cap can only hide rows past ~24 days — outside any window this scraper uses —
and it correctly keeps the early stop. Test the two properties separately.
Caps also fail differently: past the cap accenture's offsets end, while
freseniusmedicalcare's **wrap to page 1**, so always probe one offset beyond a
round `total`. Distinguish genuine non-monotonicity from the harmless **featured-row
pinning** several boards do (stryker, elanco, premier_research, pfizer pin a
few old rows at the top of page 1) — that only matters if page 1 holds no
in-window rows at all.

**The `myworkdaysite.com` variant needs a different public URL.** A few tenants
sit on `wdN.myworkdaysite.com` rather than `{tenant}.wdN.myworkdayjobs.com`
(**hcahealthcare**, **havas**, and parexel's site). The CXS API path is
unchanged, but the public job URL is `/recruiting/{tenant}/{site}/job/…`, not
`/{site}/job/…` — probe-verified on hcahealthcare, where the fleet-default shape
answers **HTTP 500**. The template's default would still *write* those URLs, so
the failure is silent and lands dead links in the club export. Set
`PUBLIC_JOB_URL = HOST + "/recruiting/" + TENANT + "/{site}{external_path}"` on
any tenant on this host and pin it with a test.

> **Open bug — a transient 403 silently loses a job, fleet-wide.**
> `_request_json` treats every 4xx as permanent (`retry` covers only
> 429/5xx), but resmed observed 1 detail fetch in 40 returning **403 that
> succeeded on a manual retry minutes later** — bot-defense, not a real
> refusal. The posting is dropped, and because failed details are recorded in
> *no* sidecar, the next run's tighter watermark never revisits it: the job is
> gone permanently and nothing reports it beyond a `detail_failed` counter.
> Two fixes, neither applied yet (the line is identical in every scraper, so
> this wants one coordinated pass): add `403` to the retry set, and record
> failed ids somewhere the next run re-tries. Until then, treat a non-zero
> `detail_failed` as lost data rather than noise — except on philips, where
> exactly 1 is the known ghost-stub row.

**Two paging traps.** `total` is trustworthy only on the offset=0 page — most
tenants report 0 on deeper offsets (a few, like astrazeneca and clarivate,
report the real count), so latch the first non-zero value. And an offset *past*
the end of a board **wraps to page 0 with `total` restored** (probe-verified on
endo, 98 postings, offset 100): a walk that does not break on
`offset >= total` first would silently re-ingest page 1 forever.

**Facet-scoped crawls have predictable blind spots.** Narrowing the crawl to a
tenant's own job-family facets is cheap, but employers file in-scope roles under
groups nobody would guess: BMS puts safety MDs under its **RayzeBio subsidiary**
group, HEOR under **Market Access**, regulatory and neuroscience program roles
under **Project Management**, clinical procurement under **Supply Chain**, and —
verified live on req R1605041 — a "Sr. Medical Information Communication
Specialist" under **Sales**. Subsidiary groups and Sales are the magnets. A
facet-scoped scraper should either include those groups explicitly or run a
periodic unfaceted audit walk to measure what its facets miss; **accenture**
takes the belt-and-braces form (a capped unfaceted walk *plus* the country-facet
walk), which is why the facet there is a coverage guarantee rather than a filter.

**Location parsing is the recurring per-tenant deviation.** Boards prefix
locations with ISO-2/ISO-3 country codes, US state codes, internal office
codes or region tiers, all of which otherwise land in `city`. The fleet-standard
amendment is a skip for bare all-caps tokens (`[A-Z]{2,3}`) in the scraper's own
`parse_city`; **cencora** widens it to `{2,5}` for region tiers (WEMEA, LATAM,
NCEE), **clarivate** adds `[A-Z]\d{2,4}` for office codes (R155) plus a trailing
`(121- …)` strip, **novartis** strips a trailing parenthetical ("Cambridge
(USA)"), **sandoz** strips *stacked* trailing parentheticals ("Barleben (Salutas
Pharma GmbH) (Sandoz)" — and without it a hyphen inside the parens truncates the
city outright, "Rotkreuz (Office-Based) (Sandoz)" → "Rotkreuz (Office"), and
**msd** and **kenvue** take the *last* surviving segment when three or more
survive (msd's format is country - state - city, kenvue's is macro-region -
country - state - city; the first survivor would otherwise be the state, or
literally "Europe/Middle East/Africa").

The trailing-parenthetical strip has recurred on four tenants and is a safe
promotion candidate for the template. **The last-segment rule is not** — do not
generalize it. **haleon** is the counter-example: its hierarchies put the city
second on some rows and last on others ("China - Shanghai - HuangPu District -
The Headquarters Building" wants Shanghai; "USA - New Jersey - Warren" wants
Warren), so no positional rule serves both and haleon is deliberately left
leaking the state/province. Telling those apart needs a state/province name
table, which nothing here has; the tenants that got the last-segment fix got it
because *their* formats are positionally consistent, verified per board.
**The bare-hyphen splitter truncates hyphenated city names on every scraper in
the fleet, silently.** `parse_city` splits on `[,\-–]`, so `Villeneuve-Loubet`
→ `Villeneuve` (gehealthcare) and `Val-de-Reuil` → `Reuil` (kenvue), and by the
same mechanism `Stratford-upon-Avon` or `Baden-Baden` would truncate anywhere
they appear. Found independently on two tenants, which makes it a template
issue rather than a tenant quirk. The tenants that split on `" - "` instead
(**msd**, **kenvue**) are immune; everyone else is exposed wherever a real
hyphenated place name occurs. Blast radius is small but the failure is
invisible — nothing errors, the city is just wrong.

**calyx** strips leading space-separated code tokens, but note *how*: a naive
`^[A-Z]{2,3}\s+` is wrong, because `LOS`, `NEW` and `SAN` are themselves three
capitals — it turns "LOS ANGELES" into "ANGELES". The working form strips only
when a lowercase letter survives (`US MA Needham` → `Needham`, "NEW YORK"
untouched). One residual to know before copying it anywhere: a genuine place
name opening with a short all-caps token still shortens — **"MD Anderson Cancer
Center" → "Anderson Cancer Center"** — so a hospital-operator tenant needs this
checked rather than assumed.

**illumina** is the same shape as haleon and is left alone for the same reason
("US - California - San Diego" wants the last segment, but "India - Bengaluru -
Manyata" and "Singapore - Woodlands - NorthTech" want the middle one, since the
last is a campus). Two tenants now want the same thing, so **a spelled-out
US-state drop set is the next amendment worth building** — it fixes both
without a positional guess, and both boards pin their current wrong output in a
test so the fix announces itself. Known unfixed leaks: "Remote Based" (philips) and "Teleworker" (elanco)
are not matched by `_REMOTE_RE`, and "Client" (syneoshealth) is a placeholder
locality — all cosmetic, with country and `job_type` still correct. **regeneron
is deliberately left alone**: its locations mix campus codes with genuine
all-caps place names ("RENSS - TECH VALLEY" beside "TARRYTOWN", "SLEEPY
HOLLOW"), so the all-caps skip that fixes other tenants would delete real
cities here. Any fix needs a campus-code mapping, not a pattern.

**Yield varies by employer type, and low is usually correct.** CRO and pharma
boards keep roughly 10-30% of what they fetch; medtech and device boards
(medtronic, stryker, baxter, philips) 1-4%; diagnostics (labcorp ~1.3%) and
distribution (cencora ~3%) lower still, because those boards are dominated by
bench, manufacturing, field-service and warehouse roles the taxonomy has no
family for. Do not compensate with scraper-side filters — the drop is the
shared classifier's verdict and is archived reversibly in `out-of-scope.csv`.
Two title patterns are worth knowing: "Clinical Specialist" on a device board
is a field sales/support role (correctly dropped), and journal editorial roles
(springernature) are dropped wholesale — a taxonomy decision, not a bug.

The tenants, with board size at build time (2026-08-28) and anything peculiar:

- **syneoshealth** — Syneos Health, CRO. 658. Locations are `AAA-City`
  (alpha-3 prefix); field roles use "Client" as the locality.
- **accenture** — life-sciences BPO inside a consulting board (its PV and
  regulatory-services requisitions are the reason it is here). **Ordering
  unsound**, `total` capped at 2000, so it runs two listing walks — the capped
  global view plus an India country-facet walk
  (`locationCountry: bc33aa3152ec42d4995f4791a106ed09`) — with the early stop
  off. Expect ~98% dropped.
- **labcorp** — 1,652. Diagnostics; facility-string locations.
- **novartis** — 959. `City (Country)` locations.
- **thermofisher** — 3,204, the largest here. robots.txt disallows the
  *human-facing* `/ThermoFisherCareers/` path but not the `/wday/cxs/` API the
  scraper uses; the per-URL check passes legitimately. Unique in the fleet.
- **sanofi** — 790. Alt site `OpellaCareers` 404s and is a separate brand;
  excluded.
- **astrazeneca** — 1,234, ~50-65 reqs/day. Sibling `Alexion` board excluded
  (separate Rare Disease brand — its own scraper if ever wanted).
- **clarivate** — 160. Sporadic RWE/HEOR supply; zero-row runs are normal.
- **clarioclinical** — Clario, 77. `hiringOrganization` is empty on this tenant.
- **jj** — Johnson & Johnson, 1,727, ~100 reqs/day.
- **medtronic** — 1,141. Sibling `RedeploymentMedtronicCareers` is
  robots-disallowed and excluded.
- **alcon** — 393. Posts untranslated JA/ZH text.
- **elanco** — 371, animal health. "US - Teleworker" leaks as a city.
- **baxter** — 565. Requisition ids carry spaces (`JR - 197008`), so they do
  not string-match the `externalPath` spelling — matters only for cross-source
  id joins.
- **philips** — 830. A **ghost stub** posting (bulletFields only, no title or
  path) sits at offset 0 and costs exactly one `detail_failed` per run — a
  count of 1 here is expected, not a defect.
- **cencora** — 970. **Ordering unsound** (full walk, cap 54). Region-tier
  `>`-separated locations. Sibling site `Distribution` (3 postings) excluded.
- **msd** — 885. `jobs.merck.com` is geo-blocked from India but the CXS tenant
  answers cleanly, which is the whole reason this route is used.
- **springernature** — 66. Built for scientific-editor supply, but the taxonomy
  drops journal editorial roles; see above.
- **stryker** — 1,186, device. Agency-submission sibling site excluded.
- **hcahealthcare** — 115, on the `myworkdaysite.com` variant (see above).
  Note the board is HCA **UK** — London private hospitals plus Sarah Cannon
  Research Institute UK, uniformly `GB`; the US HCA requisitions, and with them
  the US SCRI oncology-trial supply, are on a different tenant that is not in
  the PharmaBharat-derived source list and has not been located. Locations are
  hospital site names ("The Princess Grace Hospital"), never towns.
- **alvotech** — 16, biosimilars. Posts Icelandic/English bilingual adverts,
  invisible to the language sniffer.
- **vantive** — 277, dialysis. Leanest yield in the fleet (~1.6%).
- **sandoz** — 344. **calyx** — 28, seeded with `--since` (below).
- **haleon** — 373, consumer health on the legacy `gsknch` tenant.
- **regeneron** — 578. **ferring** — 79. **kenvue** — 194, region-prefixed
  locations. **lonza** — 683, CDMO. **endo** — 98, seeded with `--since`.
- **elsevier** — 135, on the group-wide `relx` tenant. Eight sibling sites all
  answer 200 and are all excluded as different brands (LexisNexisLegal,
  RiskSolutions, ReedExhibitions, ciriumcareers, reedtech, Law360, Knowable) —
  and critically, the `relx` site itself (719) is a **superset that re-lists
  Elsevier requisitions under near-identical ids**, so including it would both
  duplicate every Elsevier row and mis-attribute other brands' jobs. A useful
  general warning for any group tenant: check whether the parent site re-lists
  its subsidiaries before adding it.

**Zero-keep boards need a `--since` seed, or they stay empty forever.**
`compute_cutoff` falls back to the rolling 7-day `INITIAL_WINDOW_DAYS` whenever
the store holds no rows, so a board whose in-scope postings are all older than a
week can never reach them — the window never widens and the watermark never
starts. **clarivate**, **calyx**, **endo** and **lonza** were seeded with an
explicit `--since` for exactly this reason. Two lessons from doing it: prefer
"all live inventory" over a date proxy where the board is small (a listed
Workday req is an open one — calyx's June-1 seed silently dropped two in-scope
roles from May that are still open today), and note that the cheap listing gate
cannot protect you here, because `postedOn` floors at "Posted 30+ Days Ago" —
only the detail's real `startDate` reveals a months-old posting, after the fetch
is already paid for. After any taxonomy change, re-run the affected boards with
`--since` rather than waiting for the daily incremental to surface a backlog it
structurally cannot see.

Also on this protocol, built separately: **iqvia**, **parexel**, **propharma**,
**corrohealth**, **fhi360**, **path**, **bms** (job-family-facet scoped),
**premier_research**. `instructions/workday-tenant-probe.csv` also carries the
tenants probed but not built, including **piramalpharma** (endpoint healthy but
a full pull scored 0 of 235 titles in scope — CDMO ops/QA/engineering) and
three needing a site-slug hunt (**abbott**, **takeda**, **novozymes**).

> **Two sessions build in this tree concurrently.** Before writing into
> `scrappers/<name>/`, check whether the directory already holds files you did
> not create and stop if it does — three tenants (gsk, amgen, pfizer) had files
> overwritten when two builds raced on the same path.

---

## Known-broken / blocked (do not expect output)

- **apna** — apna.co moved to the Next.js App Router; cards are server-rendered
  HTML only (no `__NEXT_DATA__`, no job JSON in the RSC stream, no detail
  JSON-LD). **Needs a parser rewrite**, which is also why it is one of the
  three scrapers still on the legacy scheme (with **nhm** and **swaasa**) —
  there is nothing running to migrate.
- **phcc** *(retired)* — careers.phcc.gov.qa (Oracle EBS iRecruitment) has
  `robots.txt: Disallow: /`; the scraper aborts by design and only runs with
  explicit `--ignore-robots` authorization.
- **jadarat.sa / MNGHA** (Saudi) — not in this fleet as working scrapers;
  jadarat returns 403 everywhere and MNGHA serves its block page as HTTP 200.
- **zulekhahospitals** *(retired)* — legitimately returned only 0–3 rows per
  run, and 0 of them were in scope when re-run live; retired 2026-08-26.
  The other chronically thin Gulf sources (**moh**, **gulftalent** base,
  **dubaihealth**) were retired on 2026-08-25 — see
  `retired-scrappers/README.md`.

---

## Club export

There is no central converter any more: the shared
`instructions/export_club_csv.py` was deleted with the 2026-08-25 migration.
Each scraper regenerates `jobs_csv/<DD-MM-YYYY>/<site>.csv` itself from its
full rich store each run — migrated ones using `CLUB_COLUMNS` imported from
`_shared/classification.py` (`docthub` gained its first club export this way),
the rest still writing their older club schema. `apna` writes no club export at
all and needs its parser fixed before it can.

## There is no fleet runner

Each scraper is invoked by hand (`../../.venv/bin/python <site>_scraper.py`),
or from one cron line per scraper. A `run_daily.sh` is referenced in older
notes but does not exist.
