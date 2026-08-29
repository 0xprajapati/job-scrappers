# unilever scraper

Scrapes Unilever's experienced-hire careers board from the **Workday CXS JSON
API** at `unilever.wd3.myworkdayjobs.com` (tenant `unilever`, site
`Unilever_Experienced_Professionals`). **254 open postings on 2026-08-28.**

Unilever is the global consumer-goods group behind Dove, Vaseline, Liquid I.V.
and a large personal-care / health & wellbeing portfolio. Supply is
FMCG-shaped — supply chain, manufacturing, sales/customer development, R&D and
marketing — so only a thin slice is in scope for this fleet (R&D scientists,
regulatory / safety & environmental assurance, occupational health). A high
`excluded_out_of_scope` ratio is expected and correct on this tenant.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/unilever/Unilever_Experienced_Professionals/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. `total` is only reliable on the offset=0 page (deeper
  pages report 0 — confirmed here: offset 100 and 200 both report `total: 0`).
- **Detail**:
  `GET /wday/cxs/unilever/Unilever_Experienced_Professionals/job/<externalPath>`
  → `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow:` for the six public sites,
  `Disallow: /EB/`, `/Unilever_VIP_Careers/`, `/refreshFacet/` only — the CXS
  API paths for the crawled site are allowed. Checked at startup, honored
  per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Sibling sites (not crawled)

robots.txt advertises five more Workday sites on this tenant. All were probed
on 2026-08-28 and none is crawled:

| site | total | decision |
|------|-------|----------|
| `Unilever_Experienced_Professionals` | 254 | **crawled** — the main brand board |
| `UK_TemporaryWorkersite` | 15 | excluded — contingent-labour portal, not the brand's public vacancy board |
| `Unilever_Early_Careers` | 4 | excluded — graduate programme board (UFLP) |
| `Unilever_UFLP_ULIP_Fast_Track_Career_Site` | 1 | excluded — internship / fast-track programme board |
| `Unilever_Early_Bird_Early_Access_to_Job_Posting` | 0 | excluded — empty at probe time |
| `TMICC` | 0 | excluded — empty at probe time |

That follows the fleet convention of crawling the main brand board only (no
University / Referral / agency / early-careers sites).

## Why the window matters here

Ordering is **sound**. With `searchText=""` the `postedOn` labels age
monotonically with offset (probed 2026-08-28):

| offset | first label |
|--------|-------------|
| 0   | Posted Today       |
| 60  | Posted 2 Days Ago  |
| 100 | Posted 3 Days Ago  |
| 200 | Posted 9 Days Ago  |

So the newest-first assumption behind the template's early stop holds. Board
velocity is roughly **25–30 requisitions a day**, which puts the 7-day window
at about offset 180. At 254 postings the board is just over
`EXHAUSTIVE_BOARD_MAX` (200), so the consecutive-old-pages early stop is live
— and sound. Either way the whole board is only 13 listing pages, so the first
run needed **no `--max-pages` cap**.

The relative `postedOn` label decides the window **before** any detail request:
out-of-window ids go to `seen_old_ids.csv` unfetched. The detail's exact
`startDate` re-checks the window after the fetch (the relative label has only
day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace.

**Seeding note (2026-08-28).** The 7-day first run scanned 220 postings,
fetched 173 details and kept **0** — the last week of this board was entirely
FMCG factory / merchandising / sales work. A zero-keep run leaves no watermark
(`compute_cutoff` would fall back to a rolling 7 days forever), so the store
was immediately re-seeded with `--since 2026-06-01`: that walked all 254
postings, fetched the 81 not already archived, and kept 1 (a regulatory
affairs manager). The watermark is now 2026-08-10 and daily runs are
incremental from there.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review — this
board posts a lot of untranslated German/Portuguese/Spanish vacancies
("Instandhaltungsmechaniker (d/w/m)", "Promotor De Merchandising").

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Unilever); the Workday legal
entity (e.g. "2236 Unilever Brasil Ltda.") is kept raw in `hiring_org`.

### Location quirks

The board mixes four location formats, all handled by `parse_city`:
`"Rio de Janeiro, Brazil"` (city + country), `"Heilbronn"` (bare city),
`"Englewood Cliffs, NJ"` (city + US state code) and named sites such as
`"Hoboken US HQ"`, `"Sunlight House"`, `"Reg Vendas Sul"` which are kept
whole. `"Remote - USA"` yields no locality, so the row exports
`city_name = "Remote"`. The fleet-standard ISO/state-code skip is applied so a
bare `"NJ"`/`"IND"` segment can never leak into `city`. This tenant also sends
`additionalLocations: null` (or omits the key) rather than an empty list.

## Usage

```bash
../../.venv/bin/python scraper.py                # daily incremental run
../../.venv/bin/python scraper.py --limit 5      # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-06-01   # explicit window
python test_filters.py                           # unit tests (65)
```

Outputs: `unilever_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/unilever.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
