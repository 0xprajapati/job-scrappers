# elsevier scraper

Scrapes Elsevier's global careers board from the **Workday CXS JSON API** at
`relx.wd3.myworkdayjobs.com` (tenant `relx`, site `ElsevierJobs`). **135 open
postings on 2026-08-28** — one of the smallest boards in this fleet. Elsevier
is the medical and scientific publisher / health-analytics company behind
ScienceDirect, Scopus and ClinicalKey (part of RELX). Supply is
publishing-and-tech-shaped: product, software engineering, data science,
sales and academic-account management, with a thin in-scope seam of
scientific/medical editorial and clinical-solutions roles. Posting velocity
is roughly **2–3 requisitions a day** (offset 60 was already 21–22 days old
at probe time).

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/relx/ElsevierJobs/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (21 → HTTP 400, probe-verified
  2026-08-28). Each posting carries title, externalPath, locationsText, a
  *relative* `postedOn` label ("Posted Today" … "Posted 30+ Days Ago") and
  the requisition id in `bulletFields`. `total` is only reliable on the
  offset=0 page (offsets 60/100/120 all reported `total: 0`).
- **Detail**: `GET /wday/cxs/relx/ElsevierJobs/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, `jobReqId`, `location`, `additionalLocations`,
  the country descriptor + ISO `alpha2Code`, and the canonical
  `externalUrl`. All confirmed present on this tenant.
- **robots.txt**: `Allow:` for each of the nine sites, `Disallow:
  /refreshFacet/` only — the CXS API paths are allowed. Checked at startup,
  honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Tenant scope — why only one site

`relx` is a **group-wide RELX tenant** serving nine Workday sites. Each was
probed on 2026-08-28 (limit 1, offset 0); all answered HTTP 200, and all but
`ElsevierJobs` are a different RELX brand, so they are **excluded** per the
fleet's same-brand rule:

| site | total | brand | decision |
|---|---|---|---|
| `ElsevierJobs` | **135** | Elsevier | **included** |
| `relx` | 719 | RELX group-wide superset | excluded |
| `LexisNexisLegal` | 314 | LexisNexis | excluded |
| `RiskSolutions` | 177 | LexisNexis Risk Solutions | excluded |
| `ReedExhibitions` | 64 | RX (events) | excluded |
| `ciriumcareers` | 8 | Cirium (aviation analytics) | excluded |
| `reedtech` | 5 | Reed Tech | excluded |
| `Law360` | 3 | Law360 (LexisNexis) | excluded |
| `Knowable` | 0 | Knowable | excluded (also empty) |

The `relx` site is not just a different brand, it is a **superset that
re-lists Elsevier requisitions**: searching it for `R116326` returns the same
Product Manager II posting under `/job/Oxford-Nielsen-House/Product-Manager-II_R116326-1`.
Including it would both mis-attribute LexisNexis/RX/Cirium vacancies to
Elsevier and duplicate every Elsevier row under a near-identical id.

## Why the window matters here

At 135 postings the board sits **below `EXHAUSTIVE_BOARD_MAX` (200)**, so it
is walked completely on every run — 7 listing pages — and the
consecutive-old-pages early stop never engages. The window is still applied
at the listing level so detail fetches stay cheap: the relative `postedOn`
label decides before any detail request, and out-of-window ids go to
`seen_old_ids.csv` unfetched. The first run scanned all 135 postings but
spent only **27 detail requests**. The detail's exact `startDate` re-checks
the window after the fetch (the relative label has only day granularity).

**Ordering is SOUND** (probe-verified 2026-08-28, `searchText=""`): postedOn
labels age monotonically with offset —

| offset | first postedOn label |
|---|---|
| 0 | Posted Today |
| 60 | Posted 21 Days Ago |
| 100 | Posted 30+ Days Ago |
| 120 | Posted 30+ Days Ago |

`total` (135) is not a round display cap. The template's early stop would be
safe here; the board is simply small enough that it never runs.

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. **No `--max-pages` cap was used** — at 135
postings the whole board is 7 pages, well inside `DEFAULT_MAX_PAGES` (40).

### Tenant quirks

- **Out-of-range offsets wrap instead of emptying.** `offset=200` on this
  135-posting board returned the *newest 20* postings with `total: 135`
  again, not an empty page. The crawl loop breaks on `offset >= total`
  before that can happen, so it is never reached — but a future
  max-pages-only walk here must not treat "empty page" as its terminator.
- **Site-name locations.** `location` is often a building/campus rather than
  a plain city: `Oxford Nielsen House`, `NLD Amsterdam (Radarweg)`,
  `JAPAN-Tokyo-MitaGarden`, `Singapore - ELS Winsland House`,
  `USA - Bethesda, MD`, `Home based-Illinois`, `London Wall`. The
  fleet-standard ISO/state-code skip in `parse_city` handles the
  comma/hyphen-delimited ones. `NLD Amsterdam (Radarweg)` is
  *space*-separated, so it has no segment to split on and is kept whole —
  the same accepted shape as CorroHealth's "Noida Luminaire". Pinned by
  test so the behaviour is deliberate, not accidental.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

**Expect a very low keep rate on this board.** The first run kept **1 of 27**
in-window postings: Elsevier's supply is overwhelmingly software, product,
data-science and sales. The shared classifier also treats the
publishing-editorial ladder unevenly — `Scientific Editor` /
`Associate Scientific Editor` map to **Medical Writer** and are kept, while
`Associate Clinical Editor`, `Journal Manager`, `Managing Editor`,
`Publisher`, `Peer Review Manager` and `Content Project Manager` are dropped
as out of scope. That matches the sibling `springernature` scraper's
finding and is a **taxonomy decision, not a scraper fault** — no local
filters were added here.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Elsevier); the Workday legal
entity (e.g. "Elsevier Limited Company", "Elsevier Inc. Company") is kept raw
in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                     # daily incremental run
../../.venv/bin/python scraper.py --limit 5           # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-01  # explicit window
python test_filters.py                                # unit tests (64)
```

Outputs: `elsevier_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/elsevier.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
