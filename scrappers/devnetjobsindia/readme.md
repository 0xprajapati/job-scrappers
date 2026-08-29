# devnetjobsindia scraper

Scrapes development-sector job listings from
[devnetjobsindia.org](https://devnetjobsindia.org) — India's NGO/development
job board. The board's supply is programme roles at NGOs, foundations and
public-health projects (HIV/TI outreach, community health, M&E,
epidemiology, nutrition), which makes it a real **Public Health** source;
the majority admin/livelihood/education roles are dropped by the shared
classifier.

## How it works

1. **Discovery: the sitemap.** `sitemap.aspx` (named by robots.txt) lists
   every active posting as `jobdescription.aspx?job_id=<sequential int>`
   (~628 URLs, Aug 2026). The homepage and all listing pages
   (standard/highlighted/consulting/rfp) are strict subsets of it, and their
   cards carry no posted date — so the sitemap is the one crawl source.
   Every `<lastmod>` is the sitemap's own generation date and carries no
   per-job information.
2. **Detail pages** embed a schema.org JobPosting JSON-LD block with
   `datePosted`, `validThrough`, title, HTML description,
   `hiringOrganization.name` and a full address. Two site bugs are
   tolerated: the JSON-LD ends with a stray extra `}` (parsed with
   `raw_decode`), and the visible "posted date" span
   (`lblPostedDate`) actually holds the **apply-by deadline** — only the
   JSON-LD `datePosted` is trusted.
3. **Relevant Sectors** tags on the detail page (`lblSector1..3`, e.g.
   "Health, Doctors, Nurses, HIV/AIDS, Nutrition") are the site's curated
   role signal — passed to `classify_job` as `skills`, kept in the rich CSV
   as a raw source column, never allowed to decide the category. **One tag
   is removed before classifying** — see below.
4. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`): out of scope → dropped, counted, full row
   appended to `out-of-scope.csv`; `needs_review` → kept AND flagged.
5. **Skip lists.** posted_date exists only on detail pages, so the date
   gate can only run after the fetch. Out-of-window ids go to
   `seen_old_ids.csv` and dropped ids live in `out-of-scope.csv` — both
   consulted before fetching, so each id is fetched at most once, ever.
   Steady state ≈ 1 sitemap request + 1 detail request per new posting.
6. **RFPs/tenders** share the job_id space and the same JSON-LD. They are
   procurement notices, not jobs: title-detected (`RFP/EOI/tender/
   empanelment/...`), marked `is_rfp`, and force-flagged into
   `needs_review.csv` when the classifier keeps them.

## The funding-sector tag (why one tag is stripped)

The board draws its sector tags from a **closed 16-item vocabulary**. One of
them — `Fundraising, Business Development, Grants Writer` — describes how the
hiring *organisation* is funded, not what the role does, and the shared
taxonomy treats "Business Development" as a negative keyword. So a health
role sitting in a fundraising-adjacent unit gets vetoed on the strength of an
org-chart label. The tag is on 18 of 442 stored in-window postings.

`sectors_for_classifier()` drops that one tag before classification; the
other 15 pass through untouched and the raw list is still stored in the
`sectors` column, so the decision is visible and reversible. Measured
2026-08-27 holding the classifier constant: **+2 rows, 0 lost** (57 -> 59) —
the WJCF District and Block Coordinator, Presbyopia Program postings.

**This is a per-board judgement, not a candidate for a shared fix.** Verified
across all 30 boards on 2026-08-27: other sources put genuinely
role-descriptive text in the same argument — himalayas files jobs under
`Revenue-Cycle-Management` / `Healthcare-Billing`, and the veto is right to
read it (suppressing it there would admit 118 billing and sales rows). Only a
board that files by ORG SECTOR rather than by role belongs here. The same
strip is applied on the global twin, `../devnetjobs/`.

No salary is published anywhere on the board → club salary columns stay
blank ("Not Disclosed" semantics, never invented). `min_experience` and
`qualification` are grounded extractions from the description
("Minimum 4 years...", MPH/MSW/GNM credentials), never inferred.

## Outputs

| File | Purpose |
|---|---|
| `devnetjobsindia_jobs.csv` | Rich cumulative store (dedup key: `job_id`) |
| `../../jobs_csv/<DD-MM-YYYY>/devnetjobsindia.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids — detail-fetch skip list |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged (other-profession titles, RFPs) |

## Running

```bash
python scraper.py                # incremental run
python scraper.py --limit 20     # test run: at most 20 detail fetches
python scraper.py --since 2026-08-01
python test_filters.py           # unit tests (no network)
```

Time window: first run keeps the last 14 days; later runs use
`max(stored posted_date) − 2 days`. The first run fetches every sitemap id
(~10 min at the 1 s delay); later runs cost a handful of requests.

robots.txt is `Allow: /` (only /FCKeditor/ and /admin/ disallowed) and is
verified at startup. Probed and built 2026-08-26.
