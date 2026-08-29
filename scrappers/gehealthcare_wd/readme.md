# gehealthcare scraper

Scrapes GE HealthCare's global careers board from the **Workday CXS JSON API**
at `gehc.wd5.myworkdayjobs.com` (tenant `gehc`, site `GEHC_ExternalSite`).
**958 open postings on 2026-08-28.** Note the tenant slug (`gehc`) differs from
the fleet's directory/CSV name (`gehealthcare`).

GE HealthCare is a medical imaging and diagnostics technology company — MRI/CT/
ultrasound systems, patient monitoring, contrast media, and the digital software
around them. Supply is device-manufacturer shaped: engineering, field service,
manufacturing and commercial roles dominate, with an in-scope minority in
regulatory affairs, quality/QMS, clinical applications and biomedical/imaging
service. Expect a much lower keep rate than the CRO boards in this fleet.

## Data source

- **Listing** (paged, newest-first):
  `POST /wday/cxs/gehc/GEHC_ExternalSite/jobs` with
  `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`.
  `limit` is **hard-capped at 20** (more → HTTP 400). Each posting carries
  title, externalPath, locationsText, `timeType`, a *relative* `postedOn`
  label ("Posted Today" … "Posted 30+ Days Ago") and the requisition id in
  `bulletFields`. Unlike the IQVIA tenant, this one repeats the true `total`
  on **every** page (958 at offsets 0/100/200) — harmless, the crawler latches
  the first non-zero value either way.
- **Detail**: `GET /wday/cxs/gehc/GEHC_ExternalSite/job/<externalPath>` →
  `jobPostingInfo` with the full HTML description, the exact posted date
  (`startDate`), `timeType`, the country descriptor + ISO `alpha2Code`, and the
  canonical `externalUrl`. `additionalLocations` is `null` on this tenant
  rather than an empty list.
- **robots.txt**: `Allow: /GEHC_ExternalSite/`,
  `Allow: /Only_Confidential_Executive_Recruiting/`,
  `Disallow: /refreshFacet/` only — the CXS API paths are allowed. Checked at
  startup, honored per-URL.
- Requests send `Accept-Language` and a Mozilla-compatible descriptive
  User-Agent (some Workday hosts answer 406 to bare non-browser clients);
  ≥1.5 s between requests, exponential backoff on 429/5xx.

### Sites: why only one

robots.txt advertises a second site, `Only_Confidential_Executive_Recruiting`.
Probed 2026-08-28: it answers HTTP 200 with `total: 0` and an empty
`jobPostings` array — a confidential executive-search site with nothing
public. It is therefore **excluded** from `WORKDAY_SITES`; the external brand
board is the only source.

## Why the window matters here

Ordering was checked explicitly at probe time with `searchText=""`, comparing
`postedOn` labels across offsets:

| offset | labels observed 2026-08-28 |
|--------|----------------------------|
| 0      | "Posted 2 Days Ago", then "Posted Today" ×18, "Posted Yesterday" |
| 100    | "Posted 3 Days Ago" ×20 |
| 200    | "Posted 8 Days Ago" ×15, "Posted 9 Days Ago" ×5 |

Labels age **monotonically** with offset, so newest-first ordering holds and
the template's consecutive-old-pages early stop is sound on this board. The
`total` of 958 is not a round display cap, which corroborates it. No
tenant-specific ordering workaround is needed.

That spacing puts the board's velocity at roughly **30–35 requisitions a day**,
so a 7-day window reaches to about offset 170. The relative `postedOn` label
decides the window **before** any detail request: out-of-window ids go to
`seen_old_ids.csv` unfetched, and the walk stops after 2 consecutive pages with
nothing in the window. The detail's exact `startDate` re-checks the window
after the fetch (the relative label has only day granularity).

First run keeps the last 7 days; later runs use the newest stored
`posted_date` minus 2 days grace. The first run was capped (`--max-pages 15`,
i.e. the 300 newest postings) to keep the initial burst modest — the watermark
takes over from run 2.

**First run, 2026-08-28** (`--max-pages 15`, cutoff 2026-08-21): 240 postings
scanned, 190 details fetched, the early stop fired at offset 240 after 2
consecutive out-of-window pages, 50 excluded as older than the cutoff, 188
dropped as out of scope, 0 needs_review, 0 detail failures, **2 jobs kept**.
A ~1% keep rate is expected here and is not a bug — see Classification.

## Tenant adaptation: site-code location prefixes

About **17% of postings** (33 of the 190 fetched on the first run) prefix
`location` with GE's internal site code **and** a building number instead of
naming a plain city:

```
HUN02-01-Budapest-Vaci Greens C
CHN42-01-Xi'an-No. 11 Jinye Road
WA07-01-Bellevue-1100-112th Avenue NE
IL03-01-Chicago-500 W Monroe St
```

`parse_city` splits on hyphens and skips bare uppercase ISO/state tokens
(`[A-Z]{2,3}`), but these codes mix letters and digits, so unpatched they
leak the site code into `city` ("HUN02", "CHN42", "WA07"). This scraper
therefore carries **two tenant-specific skips** on top of the fleet
amendment, in the same `parse_city` loop:

| skip | catches | why it is safe |
|------|---------|----------------|
| `[A-Z]{2,4}\d+` | AUS08, CHN42, IL03, PRT03, WA07 … | a run of 2–4 capitals followed by digits is never a city name |
| `\d+` | the `-01-` building number | a bare number is never a city name |

Both are needed: the site code is always followed by `-01-`, so skipping only
the code hands `city` the number instead. Verified against **all 16**
site-code strings the first run produced — Sydney, Melbourne, Brisbane,
Xi'an, Bogota, Bilbao, Helsinki, Budapest, Chicago, Bengaluru ×2, Mumbai,
Niskayuna, Porto, Bellevue all resolve correctly.

One residual, inherited from the template rather than this format: the shared
splitter treats `-` as a separator, so hyphenated city names lose their tail
(`FRA32-01-Villeneuve-Loubet-...` → `Villeneuve`, not `Villeneuve-Loubet`).
Fixing that would mean changing how `parse_city` splits, which is fleet
territory, so it is left alone. `country` / `country_code` are unaffected
throughout — they come from the detail payload's ISO `alpha2Code`, never from
this string.

## Classification

Every in-window posting goes through
`_shared/classification.classify_job(title, "", description)` — Workday CXS
exposes **no curated role/category field** on this tenant, so `skills` is
empty. `in_scope: False` rows are archived to `out-of-scope.csv` (never the
main CSV). `needs_review` rows are kept and logged to `needs_review.csv`;
non-English (DE/FR/ES) descriptions are kept but forced into review. Because
this is a device manufacturer rather than a CRO, the great majority of
postings (engineering, sales, field service, supply chain) are correctly
dropped as out of scope — hence the ~1% keep rate on the first run.

One pattern for the fleet to decide on (**not** changed here — the shared
classifier owns keep/drop): this board carries a device-vendor role family the
CRO boards do not, and the classifier currently drops all of it. First-run
examples archived in `out-of-scope.csv`: "Clinical Applications Specialist -
CT", "Lead Clinical Applications Engineer", "MRI Applications Specialist -
NSW", "Nuclear Medicine Clinical Application Specialist", "Clinical Education
Specialist - MRI", "Biomedical Technician I/II/III", "Biomedical Equipment
Technician II", "Radiation Safety Manager" — 25 of the 188 dropped titles
match that shape. These are clinical-adjacent vendor roles (applications
training, imaging modality support, biomed service). If the taxonomy should
cover them, that belongs in `_shared/classification.py`, not here.

Salary is never shown on this board → `salary_raw = "Not Disclosed"`, club
salary columns blank. `company` is the brand (GE HealthCare); the Workday
legal entity — sometimes a joint venture, e.g. "I00M05 Wipro GE Healthcare
Private Limited" — is kept raw in `hiring_org`.

## Usage

```bash
../../.venv/bin/python scraper.py                 # daily incremental run
../../.venv/bin/python scraper.py --max-pages 15  # capped run (first run used this)
../../.venv/bin/python scraper.py --limit 5       # smoke test: 5 detail fetches
../../.venv/bin/python scraper.py --since 2026-08-20   # explicit window
python test_filters.py                            # unit tests (63)
```

Outputs: `gehealthcare_jobs.csv` (rich store, dedup key `job_id`),
`../../jobs_csv/<DD-MM-YYYY>/gehealthcare.csv` (CLUB_COLUMNS),
`seen_old_ids.csv`, `out-of-scope.csv`, `needs_review.csv`.
