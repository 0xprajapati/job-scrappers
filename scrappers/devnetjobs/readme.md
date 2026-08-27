# devnetjobs scraper

Scrapes international-development job listings from
[devnetjobs.org](https://devnetjobs.org) — the **global twin** of
devnetjobsindia.org. Same ASP.NET platform, same `job_id` URL scheme, same
sitemap, same JSON-LD contract, same two site bugs. The supply is worldwide
UN/INGO/NGO postings (WHO, UNICEF, MSF, CARE, IFRC and national NGOs) with
duty stations in ~120 countries.

## Why a separate scraper, not a flag on `devnetjobsindia`

The crawl and detail layers are twins, but three differences reach into
every row:

* **Separate id spaces.** Global is at `job_id` ~313xxx, India at ~302xxx —
  separate databases, not one board with a filter (302833 is a live RFP on
  the India board and does not exist on the global one). Each board needs
  its own rich CSV, skip lists and watermark, i.e. its own directory, which
  is also how the fleet runner finds scrapers (`scraper.py` per dir, no
  args).
* **Location is a different shape.** India publishes `addressRegion` (a
  state) and hard-codes India/IN/+91 in the club export. Global publishes
  **no** region and gives `addressCountry` as an **ISO alpha-2 code** —
  sometimes several (`"KE, GB"`), sometimes absent (worldwide-remote). The
  club mapping is different code, not a parameter.
* **The markup differs.** Global emits ASP.NET's full control ids
  (`ctl00_ContentPlaceHolder1_JD1_lblSector1`); India strips the `ctl00_`
  prefix. The India scraper's sector regex does not match this board.

What is genuinely shared (the sitemap crawl, the `raw_decode` workaround,
the RFP flag, the skip-list idiom, the experience/qualification extraction)
is copied deliberately, so devnetjobsindia stays untouched and working.

## How it works

1. **Discovery: the sitemap.** `sitemap.aspx` (named by robots.txt) lists
   every active posting as `jobdescription.aspx?job_id=<sequential int>`
   (856 job urls + ~27 static pages, Aug 2026). The homepage and all
   listing pages (standard/highlighted/consulting/rfp) are strict subsets
   of it, and their cards carry no posted date — so the sitemap is the one
   crawl source, one request per run. Every `<lastmod>` is the sitemap's
   own generation date and carries no per-job information.
   *(`www.` and `http://` both 301 to `https://devnetjobs.org`; the apex
   host is used directly.)*
2. **Detail pages** embed a schema.org JobPosting JSON-LD block with
   `datePosted`, `validThrough`, title, description,
   `hiringOrganization.name` and an address. Two site bugs are tolerated:
   the JSON-LD ends with a stray extra `}` (parsed with `raw_decode`), and
   the date span `ctl00_…_lblPostedDate` is mislabeled — the visible text
   beside it reads **"Apply by:"**, so the control holds the deadline. Only
   the JSON-LD `datePosted` is trusted.
3. **Relevant Sectors** tags (`lblSector1..3`, e.g. "Health, Doctors,
   Nurses, HIV/AIDS") are the site's curated role signal — passed to
   `classify_job` as `skills`, kept in the rich CSV as a raw source column,
   never allowed to decide the category. Unlike the India board they are
   **optional**: 26 of 60 sampled postings carry none, so the classifier
   leans on title+description more often here. **One tag is removed before
   classifying** — see below.
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
   empanelment/...`, with a word boundary so reference numbers like
   `(RFP500586)` don't trip it), marked `is_rfp`, and force-flagged into
   `needs_review.csv` when the classifier keeps them. Two real titles
   dodged that vocabulary entirely ("Calling for Data Collection **Teams**
   for … Endline Survey", "… Maintenance **Support Services for**
   Infectious Disease Surveillance"), so the regex also matches
   `calling for <0-3 words> teams|services|consultants|firms|vendors` and
   `support services for`. Both are deliberately narrow — bare
   `calling for` would flag ordinary ads ("Calling for applications").
   Over-flagging is the safe direction: the flag only routes a row to
   `needs_review.csv`, it never drops it.

## The funding-sector tag (why one tag is stripped)

The board draws its sector tags from a **closed 16-item vocabulary**. One
of them — `Fundraising, Business Development, Grants Writer` — describes how
the hiring *organisation* is funded, not what the role does, and the shared
taxonomy treats "Business Development" as a negative keyword. So a health
role sitting in a fundraising-adjacent unit gets vetoed on the strength of
an org-chart label.

Measured 2026-08-27 over the full board: the tag appears on **78 of 547**
in-window postings and was the **sole** cause of all 78 "Business
Development" vetoes — never once triggered by a title or description.
`sectors_for_classifier()` drops that one tag before classification;
the other 15 pass through untouched and the raw list is still stored in the
`sectors` column, so the decision is visible and reversible.

Effect: **26 → 34 kept, 0 lost** (4.8% → 6.2% in-scope), recovering among
others UNICEF's "Health Specialist (Vaccine Preventable Disease Control)",
GAVI's "Manager, Strategy Design and Delivery" and two M&E officers. Note
this is strictly better than passing no tags at all, which was measured at
26 → 19 (rescues 5, loses 12) — the health tags are worth keeping, it is
only the funding tag that misleads.

The durable fix — teaching the shared classifier that a rejection word in a
*sector label* shouldn't count the way it does in a job title — is a
fleet-wide change and is **not** made here; scrapers never edit
`taxonomy_keywords.py` / `role_families.py`.

## Location handling

`addressCountry` is an ISO alpha-2 code, so the club export maps it through
`COUNTRY_BY_CODE` (code → country name + dial code). Multi-country postings
(`"SO, KE"`) treat the first code as the duty station and keep the full list
in the rich `country_codes` column. An **unknown code exports its code with
a blank country name and dial code — never guessed.** Remote postings set
`jobLocationType: "TELECOMMUTE"` and may drop `jobLocation` entirely in
favour of `applicantLocationRequirements: {"name": "Worldwide"}`; those
export as `job_type` "remote" with `city_name` "Remote".

**Remote-only rows (DEVNETJOBS-02).** A remote posting with **no**
`addressCountry` at all (e.g. 312899, "Director, Global Health and
Development Team" at Rethink Priorities) used to ship with
`country_name`/`country_code`/`country_dial_code` all empty. The convention
now mirrors the himalayas scraper — the fleet's remote-only board — which
puts the source's hiring-region name in `country_name` and "Remote" in
`city_name`: such rows export **`country_name` "Worldwide"** with the code
and dial code left blank (there is no ISO code for "worldwide", and the
board does not expose the org's HQ country, so nothing is invented), and
`city_name` "Remote" (region placeholders like "Remote"/"Worldwide"/
"Anywhere" in the source's locality are not cities). A remote posting that
*is* country-restricted keeps its real country. The rich CSV is unchanged —
`country`/`country_codes` store ISO codes only, and the club CSV is a
full-store dump regenerated every run, so the stored 312899 row exports
correctly under the new mapping.

No salary is published anywhere on the board → club salary columns stay
blank ("Not Disclosed" semantics, never invented). `min_experience` and
`qualification` are grounded extractions from the description
("Minimum 4 years...", MPH/MSc/MD credentials), never inferred.

## Known blind spot

A sizeable minority of postings are in French, Spanish or German
("Recrutement D'un Consultant Individuel...", "Consultoría — Asistencia
Técnica..."). The shared classifier is English-keyword-based, so these fall
out of scope unless the English part of the description carries the signal.
This is deliberate: the fix would be widening the taxonomy keywords, which
scrapers must never do. Non-English rows are kept in `out-of-scope.csv`, so
the decision is reversible if the taxonomy ever gains translations.

## Outputs

| File | Purpose |
|---|---|
| `devnetjobs_jobs.csv` | Rich cumulative store (dedup key: `job_id`) |
| `../../jobs_csv/<DD-MM-YYYY>/devnetjobs.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
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
(~20 min at the 1 s delay); later runs cost a handful of requests.

robots.txt is `Allow: /` (only /FCKeditor/ and /admin/ disallowed) and is
verified at startup. Probed and built 2026-08-27.
