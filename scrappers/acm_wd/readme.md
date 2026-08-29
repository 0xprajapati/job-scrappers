# acm scraper

Scrapes **ACM Global Laboratories** from the **Workday CXS JSON API** at
`rrhs.wd5.myworkdayjobs.com` (tenant `rrhs`, site `acm`). **12 open postings
on 2026-08-28** — the smallest board in the fleet.

## What this board actually is

The tenant slug is opaque, so the brand was established from the payloads
themselves (probe 2026-08-28) rather than assumed:

- the tenant is `rrhs` and robots.txt advertises a sibling `/RRH/` site →
  **Rochester Regional Health** is the parent health system;
- `hiringOrganization` on this site is `951 ACM Medical Labs, Inc`,
  `955 ACM UK` and `958 DrugScan Inc.`;
- every description closes with "Rochester Regional Health is an Equal
  [Opportunity Employer]", and the Scientific Affairs posting says the role
  advances "ACM's long-term vision";
- `bulletFields` carry the real localities: Rochester NY (14624), Horsham PA
  (19044, DrugScan's toxicology lab) and York, UK (YO10 4DZ).

So this is ACM Global Laboratories — Rochester Regional Health's commercial
central-laboratory / reference-lab business (clinical trials specimen
management, esoteric testing, toxicology) — operating across three sites in
two countries. `COMPANY_NAME` is therefore **"ACM Global Laboratories"**, not
the bare slug "ACM" (which on its own is ambiguous). `COMPANY_TYPE` stays
`pharma`: the crawled board is the laboratory business, not the parent's
hospital board.

Supply is lab-shaped and unusually in-scope-dense for its size: laboratory
scientists, clinical-trials specimen technicians, R&D and scientific affairs,
alongside a few sales and software titles the shared classifier drops.

## Data source

- **Listing**: `POST /wday/cxs/rrhs/acm/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`. `limit` is
  hard-capped at 20 (more → HTTP 400). The whole board fits on the first page.
- **Detail**: `GET /wday/cxs/rrhs/acm/job/<externalPath>` → `jobPostingInfo`
  with the full HTML description, the exact posted date (`startDate`),
  `timeType`, the country descriptor + ISO `alpha2Code`, and the canonical
  `externalUrl` (`https://rrhs.wd5.myworkdayjobs.com/acm/job/...`).
- **robots.txt**: `Allow: /RRH/`, `/Appcast/`, `/ESS/`, `/maynewgradrns/`,
  `/decnewgradrns/`, `/acm/`; `Disallow: /refreshFacet/` only — the CXS API
  paths are allowed. Checked at startup, honored per-URL.
- **Sites**: only `acm` is crawled. `RRH` is the Rochester Regional Health
  hospital board (a different brand — and a hospital operator, so it would be
  `COMPANY_TYPE = "hospital"` if the fleet ever adds it as its own tenant);
  `Appcast` is a job-distribution feed; `ESS` is employee self-service;
  `maynewgradrns` / `decnewgradrns` are new-grad RN cohort boards. All are
  excluded per the fleet convention.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

## Tenant deviation: `listing_job_id` is forked

**This is the one forked function in `scraper.py`** and it is a correctness
fix, not a preference. `bulletFields` on this tenant is **not**
`[requisition id, ...]`. It is `[location, postcode, requisition id, city]`:

```
["ACM - Drugscan", "19044", "REQ_241093", "Horsham"]
```

The stock template returns `bulletFields[0]`, which would make the **site
label** the dedup key. The 12 postings share only 6 distinct leading bullets,
so the stock key would silently collapse the board to 6 rows. `listing_job_id`
therefore takes the first *requisition-shaped* bullet — space-free, carrying
both a letter and a digit. That rejects every decoy (the digits-only US zip
`19044`, the space-carrying UK postcode `YO10 4DZ`, the letters-only city
`Horsham`) and is a **no-op** on boards whose `bulletFields[0]` already is an
id (`R1564910` on iqvia). The externalPath-tail fallback is unchanged.
`test_filters.py` pins all of this.

## Why the window matters here (barely)

Ordering is **sound**, and moot. The whole board is 12 postings on one listing
page, and its `postedOn` labels age monotonically down that page (probed
2026-08-28, `searchText=""`):

```
2, 4, 4, 7, 9, 11, 27, 28, 30+, 30+, 30+, 30+ days
```

At 12 ≤ `EXHAUSTIVE_BOARD_MAX` (200) the board is walked exhaustively anyway,
so the consecutive-old-pages early stop never fires. Velocity is very low —
roughly one posting every 2–3 days. A full run is one listing page plus at
most 12 detail fetches, so the first run needed **no `--max-pages` cap**.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (ACM Global Laboratories); the
Workday legal entity ("958 DrugScan Inc.") is kept raw in `hiring_org`.

### Location quirks

Every location on this board is an internal site label prefixed `ACM - `:
`ACM - Drugscan`, `ACM - York Building 23`, `ACM - Headquarters`,
`ACM - Clinical Trials Specimen Management`, `ACM - Remote`,
`ACM - DrugScan - Remote`. The fleet-standard ISO/state-code skip is what
strips that prefix — `"ACM"` is a bare 3-letter uppercase token — so `city`
becomes `Drugscan` / `York Building 23` / `Headquarters` rather than `"ACM"`
on **every** row. Without the amendment this board would export one city name
for the entire tenant. `ACM - Remote` yields no locality, so those rows export
`city_name = "Remote"` per the himalayas convention. The real city does exist
in `bulletFields[3]`, but the fleet's `build_row` does not read it and is not
forked here.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window
python test_filters.py                           # unit tests (70)
```

Outputs: `acm_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/acm.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
