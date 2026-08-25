# foundit.in role-family scraper

Scrapes [foundit.in](https://www.foundit.in/) (formerly Monster India),
gated to the two in-scope categories — **Non Clinical** and **Public
Health**, ten sub-categories each — and emits a rich per-source CSV plus the
shared HealthCareers.club import file.

> **Scope (2026-08-24 re-scope, finalised 2026-08-25).** foundit is no longer
> a broad "all healthcare" scraper. Every card goes through the one shared
> classifier, [`_shared/classification.classify_job`](../_shared/classification.py)
> (which composes `role_families.classify()` — title 5 / skills 2 /
> description 1, keep-threshold 3, Public Health needs a title-or-skills hit —
> with `taxonomy_keywords`' negative-keyword veto and finer split). The old
> profession enum is retired fleet-wide; `category` now holds
> **`Non Clinical` | `Public Health`** and `sub_category` holds one of the 20
> sub-categories. The winning role family is kept as the rich-CSV trace
> column `role_family`. Cards with `in_scope == False` are dropped and
> counted as `excluded_out_of_scope` in the run summary. This is the same
> pipeline `shine_roles`, `himalayas`, and `pharmarecruiter_roles` use, so
> all four agree on scope by construction.

## Data source

foundit's SEO search pages are server-rendered Next.js (App Router) pages
whose full result payload rides in the React Server Components flight stream
(`self.__next_f.push` script chunks):

```
https://www.foundit.in/search/<keyword>-jobs        page 1
https://www.foundit.in/search/<keyword>-jobs-<N>    page N

    "jobSearchAPIData": {
        "data": [20 cards], "meta": {"paging": {"total": N, "limit": 20}}
    }
```

Each card carries everything we need — jobId, title, company, locations,
experience range, salary (`absoluteValue` INR/yr + `absoluteMonthlyValue`
INR/mo), `hideSalary`, postedAt/createdAt/updatedAt (epoch ms), industries,
functions, jobTypes, employmentTypes, skills, `jdUrl`, `redirectUrl`,
totalApplicants — plus a full description as a flight text-chunk reference
(`"description":"$2b"` → `2b:T<hexlen>,<html…>`), so no detail-page fetches
are needed.

### Why a browser capture (not live HTTP)

The whole domain sits behind Akamai bot protection: `/search/` and `/job/`
return **HTTP 403 to every plain HTTP client** (requests/curl, script UA or
full Chrome header set alike — TLS fingerprinting; only `/xmlsitemap/` is
served, and the sitemaps carry no posting dates). So, exactly like this
repo's [`indeed/`](../indeed) and [`naukri/`](../naukri) scrapers, the pages
are loaded **in a real browser** and the extracted cards are dumped to
`captures/<DD-MM-YYYY>.json`; `foundit_scraper.py` transforms the capture
offline.

### robots.txt (verified 2026-08-08, master spec §7)

* `User-agent: *` disallows `/middleware/` — foundit's private JSON search
  API — so the API is **off-limits** and is never called; the capture reads
  only the server-rendered `/search/` HTML, which that group allows.
* The AI-training-bot group (GPTBot, ClaudeBot, CCBot…) is disallowed from
  `/jobs/` and `/search/`; as with naukri, the capture is a human-driven
  browser session of the human-facing listing, not an AI-training crawl,
  and the offline transform never touches the site.

### Capture quirks (all verified live)

* The SEO listing is **relevance-ordered** (freshness-weighted, not sorted
  by date), and adding **any** query parameter makes foundit ignore the
  `-N` page suffix (every page returns page 1) — so there is no date-sorted
  crawl and no early-stop: the capture walks each keyword's pages up to a
  cap and the scraper's date window does the filtering.
* Akamai **rate-limits bursts**: ~25 rapid same-session page loads earn the
  whole domain a temporary 403 ("Access Denied") that lifts after a few
  quiet minutes. The capture snippet therefore paces itself (4–7 s jitter,
  a 45 s breather every 12 pages) and saves partial results to the local
  receiver as it goes.
* `hideSalary: true` cards sometimes still carry real salary values in the
  payload; they are stored with `salary_hidden=true` (foundit's UI hides
  them — the data is not invented).
* Pagination depth is server-limited: pages past ~20 of any keyword return
  **HTTP 410** (observed 2026-08-25; deeper pages held only stale postings
  anyway, so page caps cost nothing the date window would have kept).
* Chrome's Private Network Access can block the page's POST to
  `127.0.0.1` outright ("Failed to fetch"; `sendBeacon` returns true but
  delivers nothing — never trust it). `receiver.py` sends
  `Access-Control-Allow-Private-Network: true`, which normal Chrome
  accepts; if the POST still fails, the jobs are safe in `window.__cap` —
  serialize to `window.__out` and pull it out in ~1.8 MB string slices
  (an oversized DevTools/automation result can be saved to a file and the
  slices reassembled offline).
* T-chunk description refs are length-prefixed in **UTF-8 bytes**; the
  extractor binary-searches the JS-string slice that encodes to that byte
  length and resolves refs sequentially (chunks are not newline-separated).

## Keyword set

The capture (`capture.js`) walks two tiers of SEO pages: the broad
healthcare terms (`healthcare, medical, doctor, nurse, …`) for recall, plus
role-family terms added in the 2026-08-24 re-scope (`clinical-research,
clinical-trials, clinical-data-management, pharmacovigilance, drug-safety,
regulatory-affairs, medical-writing, medical-affairs, market-access,
public-health, epidemiology`). The keyword pages are just recall — the
`role_families` + `taxonomy_keywords` gate is the precision — so pulling
extra terms only helps. "medical" alone lists ~20k relevance-sorted jobs and
runs under a page cap; the run summary and the scraper log every cap hit.
Dedup by jobId makes keyword overlap free.

## Capturing a fresh page set

1. Start the local receiver (writes the POSTed JSON to `captures/`):

   ```bash
   python3 receiver.py captures/$(date +%d-%m-%Y).json 8765
   ```

   (any tiny CORS-enabled POST-to-file server works; see `receiver.py`
   in this folder)

2. Open `https://www.foundit.in/search/healthcare-jobs` in a real browser
   and run `capture.js` (this folder) in the DevTools console. It walks the
   keyword pages with polite pacing, extracts + resolves the cards, POSTs
   partials to the receiver every 12 pages and the full set at the end.
   Progress lives in `window.__cap` (`status`, `progress`, `keywords`);
   set `window.__cap.abort = true` to stop early — the partial is saved.

3. Run the transform:

   ```bash
   python3 foundit_scraper.py            # ingests newest captures/*.json
   ```

## Outputs

* `foundit_jobs.csv` — rich cumulative store (dedup key: `job_id`),
  watermark source of truth. Carries the taxonomy plus the full score trace
  per row: `category` (`Non Clinical` | `Public Health`), `sub_category`,
  `role_family`, `sub_category_basis`, `all_families`, `family_scores`,
  `family_confidence`, `matched_in`, `needs_review`.
* `../../jobs_csv/<DD-MM-YYYY>/foundit.csv` — exactly the 22 `CLUB_COLUMNS`
  imported from `_shared/classification.py` (with `category`,
  `sub_category` and `qualification`; the old `is_active`/`expires_at`
  columns are retired), regenerated from the full rich store every run.
  `qualification` is `extract_qualification(description)` — foundit exposes
  no structured qualification field, and it is never inferred.
* `needs_review.csv` — kept rows whose title reads like a different
  profession but were rescued on skills/description (`needs_review=true` in
  the main CSV — never dropped, master spec §2).

`sub_category` is `taxonomy_keywords`' finer split. When the scorer admits a
Non Clinical family on description evidence but the taxonomy's stricter
title/skills tiers can't place it, `sub_category` falls back to the family
name (the ten Non Clinical families ARE their sub-categories, one to one;
`sub_category_basis=family`). Public Health has no such fallback — its single
family is coarser than its ten sub-categories — so those stay blank when
unresolved rather than being guessed.

Time window (master spec §4): first run keeps INITIAL_WINDOW_DAYS (7) days;
later runs keep jobs newer than the stored max `posted_date` minus
WATERMARK_GRACE_DAYS (2). Dates are IST calendar days (foundit is an India
board). Salary is captured, never a filter (master spec §3).

## Usage

```bash
python3 foundit_scraper.py [--capture FILE] [--output CSV] [--limit N]
                           [--since YYYY-MM-DD] [--run-date DD-MM-YYYY]
                           [--verbose]
python3 test_filters.py      # unit tests (salary, scope gate, dates, club)
```

The 2026-08-24 re-scope changed the CSV schema (`category` → the two-level
taxonomy, new role_family/sub_category columns), so the old broad-schema
`foundit_jobs.csv` is incompatible and was preserved as
`foundit_jobs.broadschema.bak.csv`. Regenerate the store fresh from the
existing capture (which already spans 2026-08-08 … 08-24):

```bash
rm -f foundit_jobs.csv needs_review.csv
python3 foundit_scraper.py --capture captures/25-08-2026.json \
                           --since 2026-08-08 --run-date 25-08-2026
```
