# sandoz scraper

Scrapes Sandoz's global careers board from the **Workday CXS JSON API** at
`sandoz.wd103.myworkdayjobs.com` (tenant `sandoz`, site `Sandoz_Careers`).
**344 open postings on 2026-08-28.** Sandoz is the generics and biosimilars
maker spun out of Novartis in 2023 (HQ Basel), so supply is manufacturing-
and quality-shaped: MS&T, QA/QC, regulatory affairs, supply chain and
production roles across its European sites (Germany, Slovenia, Poland,
Austria, Spain), the Hyderabad/Telangana hub and commercial offices
worldwide — plus a long sales/finance/IT tail the shared classifier drops.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/sandoz/Sandoz_Careers/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago"), the requisition id in
  `bulletFields` (`REQ-10031451` form) and — unusually for this fleet — a
  `timeType` echo. This tenant reports `total` on every page (344 at
  offsets 0/100/200), but the scraper still latches the offset=0 value:
  that is the fleet-wide contract and other tenants report 0 on deep pages.
- **Detail**: `GET /wday/cxs/sandoz/Sandoz_Careers/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`. `additionalLocations` is **null** on this
  tenant — multi-site requisitions show `"2 Locations"` in the listing's
  `locationsText` instead.
- **robots.txt**: `Allow: /Sandoz_Careers/`, `Allow: /SwissRAVCareerSite/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked
  at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Site selection

The tenant's robots.txt advertises a second candidate site,
`SwissRAVCareerSite`. It was probed: it answers HTTP 200 with
`"total": 0` — an empty Swiss-RAV (regional employment office) landing
site, not part of the Sandoz brand board. **Excluded** from
`WORKDAY_SITES`; `Sandoz_Careers` carries all 344 postings.

## Why the window matters here

Ordering was checked before trusting the early stop (searchText="",
offsets 0/100/200 on 2026-08-28):

| offset | observed `postedOn` labels |
|--------|----------------------------|
| 0      | Posted Today, Posted Today, Posted Yesterday … |
| 100    | Posted 10 Days Ago (all 20 rows) |
| 200    | Posted 25 Days Ago … Posted 28/29 Days Ago |

Labels age monotonically with offset and `total` (344) is not a round
display cap, so **newest-first ordering holds** and the template's
consecutive-old-pages early stop is sound — it stays enabled.

That works out to roughly **10 requisitions a day**, so the 7-day window
closes around offset ~70 (page 4). The relative `postedOn` label decides
the window **before** any detail request is spent: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the walk stops after 2 consecutive pages
with nothing in the window. The detail's exact `startDate` re-checks the
window after the fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was capped at
`--max-pages 15` (300 newest postings) to keep the initial burst modest —
in practice the early stop fired well before the cap.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review —
Sandoz posts many manufacturing-site adverts wholly in German
("Ausbildung zum Pharmakanten (m/w/d)"), plus French and Spanish
commercial roles. Slovenian and Polish site adverts also appear; the
shared FR/ES/DE stopword sniff does not detect those, so the classifier's
own title/description signals decide them.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Sandoz); the Workday legal
entity (e.g. "DE78 (FCRS = DE078) Salutas Pharma GmbH") is kept raw in
`hiring_org`.

### Tenant adaptation — parenthetical city suffixes

Sandoz writes every location as `City (Legal Entity) (Sandoz)`, stacking
the groups: `"Barleben (Salutas Pharma GmbH) (Sandoz)"`,
`"Telangana (Sandoz)"`, `"Rotkreuz (Office-Based) (Sandoz)"`. A plain
comma/hyphen segment split leaves the suffix glued to `city` and — worse —
**truncates mid-token** whenever a parenthesis contains a hyphen:
`"Rotkreuz (Office-Based) (Sandoz)"` → `"Rotkreuz (Office"`. That is a
corrupt value, not a cosmetic one.

`parse_city` therefore strips **all** trailing parenthetical groups before
the segment loop (`re.sub(r"(\s*\([^()]*\))+$", "", location)`), the same
shape the `novartis` and `clarivate` scrapers carry:

| raw `locationsText` | stored `city` |
|---|---|
| `Barleben (Salutas Pharma GmbH) (Sandoz)` | `Barleben` |
| `Telangana (Sandoz)` | `Telangana` |
| `Rotkreuz (Office-Based) (Sandoz)` | `Rotkreuz` |
| `New South Wales (NSW) (Sandoz)` | `New South Wales` |

Other formats are untouched (`Dallas, TX` → `Dallas`), and the
fleet-standard ISO-token skip still applies (`IND-Bengaluru` →
`Bengaluru`, `USA - WI - Mequon` → `Mequon`). Four tests pin this.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (64)
```

Outputs: `sandoz_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/sandoz.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
