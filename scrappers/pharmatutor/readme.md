# pharmatutor scraper

Scrapes job postings from
[pharmatutor.org](https://www.pharmatutor.org/pharma-jobs) — an Indian
pharma career portal (Drupal, fully server-rendered, no anti-bot). The
supply is government pharmacist/CMHO notices, company
R&D/regulatory/medical-writing roles, walk-in manufacturing drives and
research fellowships — similar profile to pharmarecruiter: most of the
board is manufacturing/QC/dispensing the taxonomy does not cover and is
dropped by the shared classifier; the keepers are medical writers,
regulatory, pharmacovigilance and clinical-research roles.

## How it works

1. **Discovery: RSS ∪ term-page walk** (the reliefweb idiom — one
   source alone cannot be trusted here). `rss.xml` serves the 20 newest
   posts with exact pubDates and is immune to the page cache — it goes
   first. The archive walk is `/taxonomy/term/1754?page=N` — the
   **"vacancies" tag** term page every job post carries: a real Drupal
   pager, 10 teasers per page, months deep (page 40 is July at Aug
   volumes, ~13 posts/day). Teasers are read only inside the
   infinite-scroll wrapper; the sidebar repeats the newest posts
   outside it.

   **The `/pharma-jobs` pager is a trap.** That view has no pager at
   all: the `page` param is ignored, and every `?page=N` URL is just a
   separate page-cache key holding a snapshot of the same 6 newest
   cards from whenever that URL was first hit (Age ~1h,
   `X-Drupal-Cache: HIT`). Probing it looks like working pagination
   until the cache turns over and every page serves the identical set.
   Measured live 2026-08-26.

   Cards carry **no date**, but every job URL embeds its posting month
   (`/content/<month>-<year>/<slug>`), so a URL whose month is strictly
   before the cutoff month is old without fetching it. The walk stops
   on a page whose every card is provably old by URL month, or after 3
   consecutive pages needing zero detail fetches (~30
   already-processed posts — deeper than any cache staleness). Re-seen
   cards are free via the skip lists.
2. **Detail pages** embed an **Article** (not JobPosting) JSON-LD in an
   `@graph` with the exact `datePublished` (IST) and clean `headline`.
   The content is the article's `post-content` div: editorial HTML with
   `<strong>Label :</strong> value` runs (Post/Designation, Location,
   Experience, Qualification, Salary, End Date) that vary by post type;
   multi-role walk-in drives repeat them — first match wins, and the post
   title stays the job title (matching how the site presents it).
3. **Scoping trap:** the page is littered with sidebar/page-builder
   blocks reusing the `field--name-body` class and the inlined CSS
   contains the literal strings `field--name-body` and `.post-tags` —
   every parser anchors inside the `<article>` element first. Embedded ad
   units (`<ins>`/`<script>`) and the pasted alerts footer ("See All …
   B.Pharm Alerts … Subscribe") are stripped from the description.
4. **Tags** (`post-tags` div: role, qualifications, company, state,
   Government/Company Jobs) are the site's curated signal — passed to
   `classify_job` as `skills`, kept in the rich CSV as a raw source
   column, never allowed to decide the category. Govt notices name no
   Location run; their state arrives as a tag and is recovered from the
   Indian-states list.
5. **Company** has no structured field: extracted from the title
   ("<role> at <employer>", "<College> invites applications", the
   "| Freshers may apply" suffix stripped), else blank + `needs_review`.
6. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`): out of scope → dropped, counted, full
   row appended to `out-of-scope.csv`; `needs_review` → kept AND flagged.
7. **Skip lists.** The exact posted date exists only in detail JSON-LD,
   so in-month URLs must be fetched before the date gate. Out-of-window
   ids go to `seen_old_ids.csv`, dropped ids live in `out-of-scope.csv` —
   both consulted before fetching, so each URL is fetched at most once,
   ever. Steady state ≈ 1-2 listing pages + 1 detail request per new
   posting.

Salary appears only on govt notices ("Rs 16,500/- pm") and rarely on
company posts — parsed when present (LPA/k/per-month conventions),
otherwise "Not Disclosed", never invented. `min_experience` ("12+
years") and `qualification` come from the labeled runs, with grounded
description extraction as the qualification fallback.

## Outputs

| File | Purpose |
|---|---|
| `pharmatutor_jobs.csv` | Rich cumulative store (dedup key: `job_id`, the `<month>-<year>/<slug>` URL path) |
| `../../jobs_csv/<DD-MM-YYYY>/pharmatutor.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids — detail-fetch skip list |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged (missing company, classifier review) |

## Running

```bash
python scraper.py                # incremental run
python scraper.py --limit 20     # test run: at most 20 detail fetches
python scraper.py --since 2026-08-01
python test_filters.py           # unit tests (no network)
```

Time window: first run keeps the last 14 days; later runs use
`max(stored posted_date) − 2 days`. The board posts a handful of jobs per
day, so the first run costs ~90 requests (~2 min at the 1 s delay) and
later runs a handful.

robots.txt is stock Drupal (only /core/, /admin/, /search/ etc.
disallowed; /pharma-jobs and /content/ allowed) and is verified at
startup. Probed and built 2026-08-26.
