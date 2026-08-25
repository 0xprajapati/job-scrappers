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

**⚠️ The migration is partial.** A set of scrapers is excluded from it for now
and still runs its **own** classifier on the legacy profession enum
(`doctors | nurses | pharmacists | non_clinical`) with the older club schema —
`is_active`/`expires_at` present, no `sub_category`, and nothing dropped for
scope (unplaceable titles fall back to `non_clinical` and are flagged). Steps 2
and 4 above describe the migrated scrapers and are what any **new** scraper
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

### workindia
- **Site:** workindia.in (India, blue/grey-collar).
- **Fetch:** the daily `latest-jd-pages.xml` **sitemap** (~14k job URLs);
  each detail page has JobPosting JSON-LD. Job id is in the URL, so seen jobs
  are skipped before any request.
- **Quirk:** query-string URLs (`/*?*`) robots-disallowed — only clean paths.
- **Scope:** URL title-slug allow/ambiguous regex — a **crawl-side** filter
  that decides which of the ~14k sitemap URLs are worth a detail fetch.

---

## Group B — Pharma / niche vertical boards

### pharmabharat
- **Site:** pharmabharat.com (India pharma). **Fetch:** open **WordPress REST
  API** (`/wp-json/wp/v2/posts`), full article HTML, newest-first (~11.5k
  posts). **Scope:** whole site is pharma, so every post is scraped and the
  source category slug is kept as a rich-CSV column.

### pharmarecruiter / pharmarecruiter_roles
- **Site:** pharmarecruiter.in (India pharma). **Fetch:** open WordPress REST
  API, `jobs` category only (skips `pharma-news` at source). **Scope:** the
  `_roles` fork adds WP full-text `?search=` walks per role-family term.

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

### gulftalent / gulftalent_roles
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

### dubizzle
- **Site:** dubai.dubizzle.com (UAE classifieds). **Fetch:** healthcare
  category listing with the full **Algolia** result set in `__NEXT_DATA__`.
  **Scope:** source category; AED salary buckets kept rich-only (club schema
  is INR/USD).

### dubailivejobs
- **Site:** dubailivejobs.com (UAE; WordPress content-farm). **Fetch:** posts
  REST endpoint filtered to the **Healthcare category (id 86)**. **Quirk:**
  each post is a *company careers page* (many roles), so company from title and
  category best-effort/flagged.

### hziegler
- **Site:** hziegler.com (Helen Ziegler — agency placing into Saudi/UAE/Canada).
  **Fetch:** small hand-built static site (~54 jobs) — server-rendered index +
  detail pages, no API/sitemap. **Scope:** four index pages, merged on URL.

### profco
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

**Oracle Recruiting Cloud (public token-free CE REST API**,
`recruitingCEJobRequisitions`, newest-first, detail endpoint for new jobs):
- **apollohospitals** — Apollo Hospitals, India (site `CX_2`).
- **fortis** — Fortis Healthcare, India (`CX_1`; corporate host is
  Cloudflare-walled, Oracle host is open).
- **sidra** — Sidra Medicine, Qatar (`Sidra-Career-Site`).
- **seha** — SEHA / Abu Dhabi Health Services, UAE (`CX_1`).
- **medcare** — Medcare / Aster DM, UAE (`CX`, isolated by the Medcare
  organization facet within the group tenant).
- **purehealth** — PureHealth Group, UAE (`CX_6007`, via talentone.ae).

**PeopleStrong (Alt Recruit) Angular SPA** (`/api/cp/rest/altone/cp/jobs`
JSON, `{}` body):
- **carecareers** — Quality Care India (CARE Hospitals / KIMS).
- **maxhealthcare** — Max Healthcare group, India.

**Other platforms:**
- **manipalhospitals** — Manipal Hospitals, India; **Zwayam** platform,
  `public.zwayam.com/manageESQueries/searchJob` returns the whole index in one
  Elasticsearch hit list.
- **narayanahealth** — Narayana Health, India; SAP **SuccessFactors** — JSON
  backends are robots-disallowed, so server-rendered `/search/` HTML + detail
  pages are used.
- **kfshrc** — King Faisal Specialist Hospital, Saudi Arabia; first-party JSON
  proxy in front of **Sitecore Search** (`GetJobSearchResults`); the listing
  carries the entire posting. (Watch the Riyadh-midnight date boundary.)
- **hmg** — Dr. Sulaiman Al Habib Medical Group, Saudi Arabia; **Elevatus
  (EVA-REC)** public JSON API (`*.elevatus.io/api/v1/jobs`), full description
  in the listing.
- **dubaihealth** — Dubai Health, UAE; routed through the **Dubai Careers**
  government portal (Oracle **Taleo**), `searchjobs` JSON filtered to the
  Dubai Health organization id.
- **dha** — Dubai Health Authority, UAE; the **Sheryan Opportunities** board
  (IBM WebSphere portal) — vacancies from DHA-licensed facilities via one JSON
  resource URL.
- **moh** — Saudi Ministry of Health; no job API — recruitment **announcement
  pages** enumerated from the site sitemap (the real iRecruitment portal is
  behind SSO / not publicly reachable).
- **nhm** — National Health Mission, India; central portal has no job board —
  scrapes recruitment-advertisement **links/PDFs** from its announcement
  surfaces by title regex.

---

## Known-broken / blocked (do not expect output)

- **apna** — apna.co moved to the Next.js App Router; cards are server-rendered
  HTML only (no `__NEXT_DATA__`, no job JSON in the RSC stream, no detail
  JSON-LD). **Needs a parser rewrite.**
- **phcc** — careers.phcc.gov.qa (Oracle EBS iRecruitment) has
  `robots.txt: Disallow: /`; the scraper aborts by design and only runs with
  explicit `--ignore-robots` authorization.
- **jadarat.sa / MNGHA** (Saudi) — not in this fleet as working scrapers;
  jadarat returns 403 everywhere and MNGHA serves its block page as HTTP 200.
- Small incremental Gulf sources (**moh**, **gulftalent** base,
  **zulekhahospitals**, **dubaihealth**) legitimately return only 0–3 rows per
  run.

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
