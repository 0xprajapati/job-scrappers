# nhm.gov.in scraper (National Health Mission — central portal)

## Important: this site has no job board

`nhm.gov.in` is the MoHFW's central NHM **policy portal** (NIC PHP/CMS). It
publishes guidelines, reports and announcement PDFs — **not job listings**:

- The **"Join NHM"** page (`index4.php?lang=1&level=0&linkid=363&lid=492`)
  exists in the menu but has an empty body.
- No page on the domain (homepage, sitemap, highlights, Archives, OM & Orders,
  Media) contains vacancy/recruitment links as of July 2026.
- `robots.txt` returns the site's custom 404 HTML page (no crawl rules).

Actual NHM hiring is decentralised to the **state NHM portals**, e.g.:

- https://nhm.assam.gov.in/portlets/recruitment (structured recruitment portlet)
- https://upnrhm.gov.in (UP — "Opportunities" PDFs)
- https://nhm.maharashtra.gov.in/en/past-notices/recruitments/
- Full state list: nhm.gov.in → "States/UTs Official"
  (`index1.php?lang=1&level=1&sublinkid=23&lid=51`)

If you want real daily NHM job volume, point a scraper at one or more state
portals instead (each needs its own scraper — different CMSes).

## What this scraper does

It monitors the central portal's announcement surfaces — homepage "What's
New" ticker, `highlights.php`, Join NHM, Archives — and captures any link
whose title/filename looks like a hiring notice (`vacancy`, `recruitment`,
`walk-in`, `applications invited`, `engagement of`, `advertisement for the
post`, …), while rejecting guideline/report noise ("Training Modules for
Medical Officers" is a document, not a job ad).

Every captured row is a **notice PDF**, not a parsed job card, so it is
always flagged `needs_review=True` and logged to `needs_review.csv`.

**A run that finds 0 notices is the normal steady state** — the scraper
exists so a future central recruitment ad is not missed.

## Spec deviations (vs. instructions/master-scraper-spec.md)

- **No posted dates**: announcement links carry no machine-readable date, so
  `posted_date` = date the notice was first seen, and the watermark /
  time-window logic is a no-op. Idempotency comes from URL-based dedup
  (`job_id` = URL path; running twice adds 0 rows).
- **No salary data**: everything is `salary_raw = "Not Disclosed"` (never
  invented, per spec §3).

## Usage

```bash
python scraper.py            # normal daily run
python scraper.py --verbose  # debug logging
```

Outputs:

- `nhm_jobs.csv` — rich cumulative store
- `needs_review.csv` — new notices from the latest run
- `../../jobs_csv/<DD-MM-YYYY>/nhm.csv` — HealthCareers.club 22-column schema
  (only written when at least one notice exists)

Tests: `python test_filters.py`
