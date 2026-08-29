# calyx scraper

Scrapes Calyx's careers board from the **Workday CXS JSON API** at
`calyx.wd1.myworkdayjobs.com` (tenant `calyx`, site **`Perceptive`** — the
board still carries the company's former brand, and the postings themselves
are signed "At Perceptive, ..."). **28 open postings on 2026-08-28** — one of
the smallest boards in the fleet.

Calyx is a clinical-trial technology and medical-imaging CRO: eClinical
platforms (RTSM/IRT, eTMF, regulatory information management) plus an imaging
core lab and radiopharmacy for oncology and neurology trials. Being a pure
clinical-research company the in-scope rate is high for the science and
clinical-operations roles, but a large slice of this particular board is
eClinical *software* engineering (DevOps, .NET, agentic-AI, solution
architects) that the shared classifier drops.

Locations at probe time: Hyderabad India (9), London/Hammersmith Hospital and
Nottingham UK (5), Needham MA and remote US (6), remote UK (4), remote
Germany/India (2), plus one "2 Locations" placeholder.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/calyx/Perceptive/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, a *relative* `postedOn` label
  ("Posted Today" … "Posted 30+ Days Ago") and the requisition id
  (`JR1049xx`) in `bulletFields`. `total` is only reliable on the offset=0
  page — offset 20 reported `total: 0`, same as the other tenants.
- **Detail**: `GET /wday/cxs/calyx/Perceptive/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and
  the canonical `externalUrl`.
- **robots.txt**: `Allow: /Perceptive/`, `Disallow: /refreshFacet/` only — the
  CXS API paths are allowed. Checked at startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

Only one Workday site exists on this tenant and it is the company's own brand,
so `WORKDAY_SITES = ['Perceptive']` — no separate university/referral/other-
brand site to exclude.

## Ordering soundness

The board has 28 postings, so the brief's offset 0/100/200 comparison is not
available; offsets **0, 20 and 40** were compared instead.

- offset 0: `30+ Days`, `Today`, `Yesterday`, `Yesterday`, `3`, `3`, `7`, `8`,
  `8`, `9`, `9`, `14`, `16`, `16`, `17`, `17`, `22`, `23`, `25`, `30+`
- offset 20 (8 rows): all `Posted 30+ Days Ago`
- offset 40: **wraps back to the offset-0 page verbatim**

So labels age monotonically **from position 1 onward**, but one stray
"Posted 30+ Days Ago" requisition (`JR104834`, Business Development Director)
is pinned at offset 0. Strict newest-first therefore does **not** hold.

This needs no code change: at 28 postings the board is under
`EXHAUSTIVE_BOARD_MAX` (200), so the template already walks it completely and
the consecutive-old-pages early stop is **never armed** — exactly the
small-board hazard that guard exists for. The `offset >= total` break also
stops the walk before the offset-40 wraparound could loop it. Both facts are
pinned by tests and documented in the scraper docstring.

## Why the window matters here

Less than on the big boards — the whole listing is two pages (~2 requests) —
but the `postedOn` gate still keeps detail fetches cheap: only **6 of the 28**
postings were inside a 7-day window at probe time, so a first run spends ~6
detail requests instead of 28. Out-of-window ids go to `seen_old_ids.csv`
unfetched. The detail's exact `startDate` re-checks the window after the fetch
(the relative label has only day granularity) — and it matters on this board:
the pinned offset-0 row labels as "30+ Days Ago" but its real `startDate` is
2026-06-19, ~70 days old.

Normally the first run keeps the last 7 days and later runs use the newest
stored `posted_date` minus 2 days grace. The board is small enough that no
`--max-pages` cap was ever needed.

### The seed run was a `--since` backfill (deliberate)

A plain 7-day first run on this board kept **zero** rows: only 6 of the 28
postings were inside the window and all 6 were eClinical software roles the
shared classifier drops. That is a trap for a low-velocity board — with an
empty store `compute_cutoff` has no watermark, so every subsequent run falls
back to the same rolling 7-day window and the standing in-scope inventory is
never picked up.

The store was therefore seeded with a full-inventory backfill:

```bash
../../.venv/bin/python scraper.py --since 2026-01-01
```

**Why the whole board rather than a date proxy:** a requisition that is still
listed on the CXS board is still open — Workday drops filled and closed reqs
from the listing — so for a board this small the correct seed target is "all
live inventory", not a cutoff date. An intermediate `--since 2026-06-01` seed
was tried first and lost two genuinely in-scope, genuinely live rows
(Application Architect, Medical Imaging — `startDate` 2026-05-28; Senior
Software Engineer — 2026-05-18) for no upside at 28 postings.

That subtlety is worth remembering when re-seeding: the date gate is applied
twice and the **second one is authoritative**. Those two rows pass the cheap
listing gate no matter how old they are, because the relative `postedOn` label
floors at "30+ Days Ago", and only the detail's real `startDate` reveals they
are months old. A date-based seed therefore silently drops long-standing
inventory *after* paying for the detail fetch.

The seed kept **4 rows** and gave `compute_cutoff` a real watermark: the next
run cuts at **2026-08-18** (newest stored `posted_date` 2026-08-20 minus 2
days grace) instead of a rolling `today - 7`. From here normal incremental
runs work as designed, and the seed never needs repeating.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (Calyx); the Workday legal entity
(e.g. "LE1730 Heron Health Pvt Ltd") is kept raw in `hiring_org`, and is
**empty on some requisitions** — as is `timeType`. Both degrade cleanly.

### Location formats

Calyx writes locations in an unusually wide range of formats for a board this
size. `parse_city` normalises them in three stages — trailing parenthetical,
then leading code tokens, then the shared segment walk:

| Raw | City |
|---|---|
| `Hyderabad, India` | `Hyderabad` |
| `Nottingham, UK` | `Nottingham` |
| `GBR - London (Hammersmith Hospital)` | `London` |
| `IND Hyderabad, RMS #12C` | `Hyderabad` |
| `IND Hyderabad - Decentralized` | `Hyderabad` |
| `US MA Needham` | `Needham` |
| `Remote (US)` / `Remote (Germany)` | `Remote` (via build_row's fallback) |
| `India (Decentralised)` | `` (country, not a city) |
| `2 Locations` | `` |

Order matters: the parenthetical is stripped **first** so that `Remote (US)`
becomes a bare `Remote` the segment walk can drop as a remote marker, and so
that `GBR - London (Hammersmith Hospital)` reaches `London` rather than
`London (Hammersmith Hospital)`.

The leading-code strip is **guarded**: it is kept only when a lowercase letter
survives it. A bare `^[A-Z]{2,3}\s+` rule would eat the first word of an
all-caps city name — `LOS ANGELES` → `ANGELES`, `NEW YORK` → `YORK`, since
`LOS` and `NEW` are themselves three capitals. The guard is cheaper and less
brittle than whitelisting ~150 ISO alpha-3 codes this scraper does not
otherwise carry. Pinned by tests.

One known residual: a real place name beginning with a 2–3 letter all-caps
token followed by lowercase is still shortened (`MD Anderson Cancer Center` →
`Anderson Cancer Center`). Nothing on this board hits it, but a
hospital-operator tenant could.

`country` / `country_code` never depend on any of this — they come from the
detail's ISO `alpha2Code`.

## Usage

```bash
../../.venv/bin/python scraper.py                     # daily incremental run
../../.venv/bin/python scraper.py --limit 5           # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-01-01  # the seed backfill (see above)
python test_filters.py                                # unit tests (67)
```

Outputs: `calyx_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/calyx.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
