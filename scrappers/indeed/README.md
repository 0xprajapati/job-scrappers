# Indeed (India) — remote healthcare jobs

Scrapes **remote, India** healthcare listings from `https://in.indeed.com`,
mapped to the shared HealthCareers.club 22-column CSV.

## Why this scraper cannot fetch the site itself

- **Cloudflare wall**: every scripted HTTP client (requests/curl, any UA)
  gets **HTTP 403** with a challenge page. There is no open JSON API
  (`/graphql` is robots-disallowed anyway). Only a real browser session
  renders the site.
- **robots.txt** (checked 2026-07-29; the `User-agent: *` and the explicit
  Claude/AI-bot groups carry the *same* rule set):
  - `/jobs?q=...` search pages — **allowed** ✅
  - `Disallow: /*&start=` — **pagination is forbidden** → page 1 only,
    ~15 organic cards per query
  - `Disallow: /viewjob?`, `/rc/`, `/rss`, `/graphql`, `/jobs/IN/` — never
    fetched. `viewjob?jk=<key>` URLs are still written to
    `application_url` for the human applicant.

So the workflow is: **a human (or the Claude browser pane) opens the search
pages; the script processes the structured JSON embedded in them.**

## Data source inside the page

Each SERP embeds its React model:

```
window.mosaic.providerData["mosaic-provider-jobcards"]
      .metaData.mosaicProviderJobCardsModel.results   // ≤15 cards
```

Per card: `jobkey`, `title`, `company`, `companyRating`,
`formattedLocation`, `remoteWorkModel.type` (`REMOTE_ALWAYS`…),
`taxonomyAttributes` (job types / benefits / Remote flag),
`salarySnippet.text` + `extractedSalary {min,max,type}` (INR; `-1` = open
range; type `MONTHLY|YEARLY|HOURLY|DAILY|WEEKLY`), `pubDate` (epoch **ms**,
pinned ~05:00 UTC — take the UTC date, don't localize to IST),
`formattedRelativeTime`, `snippet` (a ~160-char teaser — see "Full
descriptions" below for how to get the real description compliantly).

## Queries (breadth instead of pagination)

One page each of `QUERIES` in `indeed_scraper.py` on
`https://in.indeed.com/jobs?q=<query>&l=Remote` (l=Remote on in.indeed.com ==
remote-within-India), then dedup by `jobkey`:

> healthcare, nurse, doctor, physician, pharmacist, medical coder, medical
> billing, clinical research, pharmacovigilance, medical writer,
> telemedicine, dietitian, psychologist, physiotherapist, counsellor,
> medical transcriptionist

2026-07-29 run: 16 queries → 205 cards → 142 unique.

## Capture workflow

Option A — browser DevTools (any query page, F12 → Console):

```js
copy(JSON.stringify(window.mosaic.providerData["mosaic-provider-jobcards"]
  .metaData.mosaicProviderJobCardsModel.results.map(j => {
    const taxo = {}; (j.taxonomyAttributes||[]).forEach(t =>
      taxo[t.label] = (t.attributes||[]).map(a=>a.label));
    const s = h => (h||"").replace(/<[^>]+>/g," ").replace(/\s+/g," ").trim();
    return {k:j.jobkey, t:s(j.displayTitle||j.title), c:j.company,
      cr:j.companyRating||"", loc:j.formattedLocation,
      rw:j.remoteWorkModel?j.remoteWorkModel.type:"",
      jt:(taxo["job-types-cc"]||[]).join("|"),
      sal:j.salarySnippet?s(j.salarySnippet.text):"",
      mn:j.extractedSalary?j.extractedSalary.min:"",
      mx:j.extractedSalary?j.extractedSalary.max:"",
      st:j.extractedSalary?j.extractedSalary.type:"",
      pd:j.pubDate, rel:j.formattedRelativeTime, sn:s(j.snippet).slice(0,280),
      ben:(taxo["benefits"]||[]).join("|"),
      q:new URL(location.href).searchParams.get("q")};
  })))
```

Paste each page's output into one JSON array file, then:

```bash
python indeed_scraper.py --from-json capture.json
```

Option B — save pages (`Cmd+S` → HTML) into a folder, then:

```bash
python indeed_scraper.py --from-html ./saved_pages/
```

## Full descriptions (the `vjk` technique)

The SERP card `snippet` is a ~160-char teaser. Full descriptions live on
`/viewjob?jk=` pages, which robots.txt forbids — but the **search page URL
`/jobs?q=...&l=Remote&vjk=<jobkey>` is an allowed path** and renders the
selected job's complete description in its right-hand pane
(`#jobDescriptionText`). So: one allowed SERP load per job, extract in the
browser console:

```js
sessionStorage.setItem("vj_" + new URL(location.href).searchParams.get("vjk"),
  document.querySelector("#jobDescriptionText").innerText
    .replace(/\n{3,}/g, "\n\n").trim().slice(0, 3000));
```

Collect all `vj_*` sessionStorage keys into a `{jobkey: text}` JSON file
(sessionStorage survives same-origin navigations), then merge:

```bash
python indeed_scraper.py --descriptions captures/<date>-descriptions.json
```

This rewrites the matching rows' `description` in the rich CSV and
regenerates the club CSV.

**Rate limit (learned 2026-07-29):** ~50 rapid back-to-back SERP loads
triggered a Cloudflare Turnstile ("Verify you are human") interstitial.
Pace the vjk loads a few seconds apart, and if the checkbox appears a human
must click it — do not automate that.

## Filtering & mapping

- **Remote gate**: `remoteWorkModel` `REMOTE_*` or location `Remote`
  ("Remote in ‹city›" cards count; the city goes to `city_name`).
  The SERP occasionally pads in a non-remote card → `excluded_not_remote`.
- **Scope gate & category — the shared classifier only.** Every card runs
  through `_shared/classification.classify_job` (title = card title, skills =
  the card's taxonomy attributes via `card_skills()`, description = the card
  snippet). The scraper keeps no ALLOW/DENY or category regexes of its own.
  - `in_scope == False` → dropped, counted `excluded_out_of_scope` and
    printed in the run summary. The broad remote queries surface a lot of
    non-healthcare noise (admission counsellors, audio-annotation gigs) and
    plenty of bedside clinical work — all of it out of scope now.
  - In-scope cards get `category` = `Non Clinical` | `Public Health` and
    `sub_category` (one of the 20 sub-categories), plus the rich-CSV trace
    columns `role_family`, `all_families`, `family_scores`,
    `family_confidence`, `matched_in`.
  - `needs_review == True` → kept **and** appended to `needs_review.csv`
    (never silently dropped).
- **Salary**: captured verbatim, never filtered/invented. Club columns only
  for `per_month`/`per_annum` INR; hourly/daily/weekly stay in the rich CSV.
- **Window**: first run keeps 30 days (`--window-days`; page-1 cards are
  live posts, unlike a feed, so the spec's 7-day default would drop live
  jobs). Later runs use the watermark (newest stored `posted_date` − 2 days)
  with `jobkey` dedup — rerunning the same capture adds 0 rows.

## Outputs

- `indeed_jobs.csv` — rich cumulative store (dedup key `jobkey`)
- `../../jobs_csv/<DD-MM-YYYY>/indeed.csv` — exactly the 22 `CLUB_COLUMNS`
  imported from `_shared/classification.py`, including `sub_category` and
  `qualification`; the old `is_active`/`expires_at` columns are retired.
  The SERP card model has no structured qualification field, so
  `qualification` is `extract_qualification(description)` — never inferred.
- `needs_review.csv` — in-scope rows the classifier flagged for review
- `out-of-scope.csv` — rows the classifier dropped from the rich store during
  the one-off taxonomy migration (reversible archive)

## Quirks learned the hard way

- `extractedSalary.max == -1` means "From ₹X" (open range) — not a real max.
- `pubDate` is epoch **milliseconds** at ~05:00 UTC; converting to IST can
  roll a "Just posted" job into tomorrow's date. Use the UTC date.
- Salary decimals like "₹13,238.09 a month" are Indeed's currency
  conversions of employer-entered values — rounded to whole INR.
- A query's page title may claim more jobs (e.g. "75 Medical Transcriptionist
  Job Vacancies") than the ~15 organic cards the model exposes; the rest sit
  behind forbidden pagination.
- Narrow queries return fewer organic cards than the banner count even on
  page 1 (nurse → 4 cards despite "11 jobs").

## Tests

```bash
python test_filters.py    # 25 tests, worked examples from real cards
```
