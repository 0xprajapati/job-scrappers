# foundit.in healthcare scraper

Scrapes healthcare job listings from [foundit.in](https://www.foundit.in/)
(formerly Monster India) and emits both a rich per-source CSV and the shared
HealthCareers.club 22-column import file.

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
* T-chunk description refs are length-prefixed in **UTF-8 bytes**; the
  extractor binary-searches the JS-string slice that encodes to that byte
  length and resolves refs sequentially (chunks are not newline-separated).

## Keyword set

`healthcare, medical, doctor, nurse, medical-representative,
physiotherapist, pharmacist, lab-technician, radiographer, hospital,
paramedical, dentist, medical-coding, nursing` — the union of foundit's
healthcare-shaped SEO pages ("medical" alone lists ~20k relevance-sorted
jobs, so it runs under a page cap; the run summary notes every cap hit).
Dedup by jobId makes keyword overlap free. Keyword SERPs drag in
non-healthcare noise, which the title/taxonomy gate filters (see
`foundit_scraper.py` docstring).

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
  watermark source of truth for incremental runs.
* `../../jobs_csv/<DD-MM-YYYY>/foundit.csv` — HealthCareers.club 22-column
  schema, rewritten every run.
* `needs_review.csv` — titles the classifier could not confidently place
  (kept in the main CSV, flagged `needs_review=true` — never dropped).

Time window (master spec §4): first run keeps INITIAL_WINDOW_DAYS (7) days;
later runs keep jobs newer than the stored max `posted_date` minus
WATERMARK_GRACE_DAYS (2). Dates are IST calendar days (foundit is an India
board). Salary is captured, never a filter (master spec §3).

## Usage

```bash
python3 foundit_scraper.py [--capture FILE] [--output CSV] [--limit N]
                           [--since YYYY-MM-DD] [--run-date DD-MM-YYYY]
                           [--verbose]
python3 test_filters.py      # unit tests (salary, gate, dates, club rows)
```
