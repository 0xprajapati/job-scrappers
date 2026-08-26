# ngobox scraper

Scrapes development-sector job listings from [ngobox.org](https://ngobox.org)
— India's NGO/CSR job board (a CSRBOX property). The board's supply is
programme roles at NGOs, foundations and CSR arms; public-health NGO
programmes (Piramal Swasthya district programme managers, community health,
M&E, nutrition) make it a real **Public Health** source, while the majority
CSR/education/livelihood/admin roles are dropped by the shared classifier.

## How it works

1. **Discovery: the listing page, two blocks.** `job_listing.php` holds a
   "featured" block (20/page, paged server-side with `?page=N`) and a
   "standard" block (10/page, paged with `?page1=M`). The standard block's
   first page repeats on every `?page=N`, so both params are walked, each
   until a page adds no unseen id (~15 requests for ~270 postings,
   Aug 2026). Cards link `job-detail_<slug>_<id>` with a sequential
   numeric id — the dedup key. RFPs/EOIs live on a separate
   `rfp_eoi_listing.php` and never enter this crawl.
2. **Detail pages have no JSON-LD** — fields sit on stable HTML anchors:
   `<h1 class="card-header">` (title), `Organization:` (org), `Apply By:`
   (deadline, or the literal "No Deadline"), `Location:` as `City(State)` —
   city optional on state-wide postings. The description is everything from
   the first `row_section font_chance12` div to the commented-out ad
   script, including the "How to apply" section.
3. **The posted date exists only in the `<title>` tag** —
   `Title-Org-25 Aug . 2026-NGO jobs in India, ...` — verified monotonic
   with job_id, so it is trusted as `datePosted` and drives the watermark.
   The site sprinkles stray dots/spaces in dates ("25 Aug . 2026"),
   tolerated by the parser.
4. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`), with an empty `skills` signal (the board
   publishes no sector tags): out of scope → dropped, counted, full row
   appended to `out-of-scope.csv`; `needs_review` → kept AND flagged.
5. **Skip lists.** posted_date exists only on detail pages, so the date
   gate can only run after the fetch. Out-of-window ids go to
   `seen_old_ids.csv` and dropped ids live in `out-of-scope.csv` — both
   consulted before fetching, so each id is fetched at most once, ever.
   Steady state ≈ ~15 listing requests + 1 detail request per new posting.

No salary is published anywhere on the board → club salary columns stay
blank ("Not Disclosed" semantics, never invented). `min_experience` and
`qualification` are grounded extractions from the description
("Minimum 4 years...", MPH/MSW credentials), never inferred.

## Outputs

| File | Purpose |
|---|---|
| `ngobox_jobs.csv` | Rich cumulative store (dedup key: `job_id`) |
| `../../jobs_csv/<DD-MM-YYYY>/ngobox.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids — detail-fetch skip list |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged |

## Running

```bash
python scraper.py                # incremental run
python scraper.py --limit 20     # test run: at most 20 detail fetches
python scraper.py --since 2026-08-01
python test_filters.py           # unit tests (no network)
```

Time window: first run keeps the last 14 days; later runs use
`max(stored posted_date) − 2 days`. The first run fetches every listed id
(~270 details, ~6 min at the 1 s delay); later runs cost ~15 listing
requests plus a handful of details.

robots.txt is a 404 — the site publishes no robots policy; the startup
robotparser check treats that as allow-all. Probed and built 2026-08-26.
