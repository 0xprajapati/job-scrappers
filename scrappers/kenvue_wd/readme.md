# kenvue scraper

Scrapes Kenvue's global careers board from the **Workday CXS JSON API** at
`kenvue.wd5.myworkdayjobs.com` (tenant `kenvue`, site `kenvue` — note the
**lowercase** site slug, unlike most tenants in this fleet). **193 open
postings on 2026-08-28** at probe time, 194 seven minutes later at the first
run — a small board.

Kenvue is the consumer-health company spun off from Johnson & Johnson in
2023 (NEUTROGENA, AVEENO, TYLENOL, LISTERINE, BAND-AID). Supply is
consumer-goods shaped — brand marketing, sales, supply chain, finance — with
an in-scope minority in regulatory affairs, quality, R&D, medical safety and
manufacturing science. Most titles are dropped by the shared classifier.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/kenvue/kenvue/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields` (Kenvue's ids look like `2607048644W`). `total` is only
  reliable on the offset=0 page (deeper pages report 0).
- **Detail**: `GET /wday/cxs/kenvue/kenvue/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` is `null` (not `[]`) on
  single-site postings.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### robots.txt — tenant-specific quirk

`kenvue.wd5.myworkdayjobs.com/robots.txt` serves:

```
User-agent: *
Disallow: /kenvue/
Disallow: /refreshFacet/
```

This is **stricter than the other Workday tenants in this fleet** (IQVIA
serves `Allow: /IQVIA/`). `Disallow: /kenvue/` covers the human-facing
careers UI. The CXS API this scraper uses lives under
`/wday/cxs/kenvue/kenvue/…`, a different path prefix — `robotparser`
confirms `can_fetch()` **True** for both the listing and the detail URL, and
**False** for `/kenvue/job/…`.

The scraper never requests a disallowed path: the public `/kenvue/job/…`
URL is only *stored* as `job_url` for humans to click, never fetched.
`check_robots()` re-checks both crawled URLs at startup on every run, so if
Kenvue ever widens the rule the run aborts instead of ignoring it. A
compliance test (`test_the_crawled_urls_avoid_the_disallowed_ui_prefix`)
pins this so a future refactor cannot quietly start crawling the UI path.

## Why the window matters here

At 193 postings this board sits **under `EXHAUSTIVE_BOARD_MAX` (200)**, so it
takes the exhaustive path: all 10 listing pages are walked and the
consecutive-old-pages early stop never engages. The `postedOn` gate still
applies **before** any detail request, so out-of-window ids go to
`seen_old_ids.csv` unfetched and a run costs ~10 listing requests plus one
detail per genuinely new in-window posting.

**Ordering soundness check (2026-08-28, `searchText=""`)** — labels age
monotonically with offset:

| offset | first `postedOn` labels |
| ------ | ----------------------- |
| 0      | Posted Today, Posted Today, Posted Today, … Posted Yesterday |
| 90     | Posted 10 Days Ago (all) |
| 180    | Posted 30+ Days Ago (all) |

Newest-first **holds**, and `total` (193) is not a round display cap. The
template's early stop would therefore be sound if this board ever grew past
the 200-posting threshold. Velocity is roughly 8–10 requisitions a day.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The board is small enough that the first
run needed **no `--max-pages` cap**.

## `city` — tenant adaptation in `parse_city`

Kenvue's location strings are four-segment macro-region strings:

```
Europe/Middle East/Africa, Spain, Community of Madrid, Madrid
Asia Pacific, India, Karnataka, Bangalore
North America, United States, New Jersey, Summit
```

i.e. `<macro-region>, <country>, <state/province>, <city>` — **the city is
LAST**, where the rest of the fleet puts it first. The fleet's
first-survivor rule would publish the macro-region (`"Asia Pacific"`) as the
city, and skipping only the region would publish the state
(`"Karnataka"`). The ISO-token skip does not help — these segments are
words, not 2–3 letter codes.

`parse_city()` therefore carries a **tenant adaptation** (the sibling `msd`
scraper carries the same last-survivor rule for its `country - state - city`
shape):

1. The shape is detected by **segment 0 being one of Workday's four
   macro-regions** — `Asia Pacific`, `Europe/Middle East/Africa`,
   `Latin America`, `North America`. Only then is the **last** survivor
   taken; every other format keeps the fleet's first-survivor rule, so
   `"Dallas, TX"` → `Dallas` and `"IND-Bengaluru"` → `Bengaluru` are
   unchanged.
2. The region form is split on **commas only**, so hyphenated city names
   survive intact (`Val-de-Reuil`, not `Reuil`).

The shape test is deliberately **not** a survivor count. Dropping the
country can collapse two segments at once — `"Asia Pacific, Hong Kong, Hong
Kong, Mongkok"` and `"North America, United States, Puerto Rico, Las
Piedras"` leave only two survivors — and a count gate falls back to the
first survivor and re-publishes the macro-region. Both are real strings from
this board and both are pinned as tests.

Replayed over all **37 distinct location strings** in the store: **0**
macro-region or state leaks. One upstream oddity remains and is not
fixable here: `"Europe/Middle East/Africa, United Kingdom, Reading,
Berkshire"` has the town and county **reversed in Kenvue's own data**, so it
yields `Berkshire`. 1 of 37.

`country`, `country_code` and `country_dial_code` never depend on any of
this — they come from the detail's ISO `alpha2Code`.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English descriptions are kept but forced into review — Kenvue posts
country-office vacancies untranslated (Portuguese for Brazil, French for
France, Spanish for LatAm).

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Kenvue); the Workday legal
entity (e.g. "8501-JNTL Consumer Health (Spain), S.L. Legal Entity") is kept
raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                           # unit tests (66)
```

Outputs: `kenvue_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/kenvue.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
