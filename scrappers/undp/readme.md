# undp scraper

Scrapes UNDP / UNCDF / UNV vacancies from the **Oracle HCM Candidate
Experience REST API** that sits behind
[jobs.undp.org](https://jobs.undp.org). Small, global board — 108 open
requisitions across 57 countries on 27 Aug 2026 — whose supply is
development programme roles (governance, climate, inclusive growth, M&E).
Health is a thin seam here: the shared classifier keeps a couple of rows per
run and drops the rest.

## Why not jobs.undp.org

**`jobs.undp.org/robots.txt` is `User-Agent: * / Disallow: /`** — the whole
ColdFusion host is off limits, and this scraper never requests it. Every
card on `cj_view_jobs.cfm` links out to an Oracle CE requisition on
`estm.fa.em2.oraclecloud.com`, which serves **no robots.txt at all
(HTTP 404 → no restrictions)** and is where the data actually lives. The
compliant path and the cheap path are the same path.
`check_robots()` verifies the Oracle host at startup and
`assert_no_disallowed_host()` fails the run if any configured URL ever
points back at `jobs.undp.org`.

**`application_url` intentionally points at the Oracle host** (fixes ledger
UNDP-02, 2026-08-27). The `estm.fa.em2.oraclecloud.com/...` links look
opaque, but they are the requisitions' canonical public URLs — the ones
UNDP's own board redirects applicants to. There is no UNDP-branded URL to
map to: every `jobs.undp.org` path is robots-disallowed (above), so linking
there would send applicants to a page this project is not permitted to
verify. The Oracle links resolve to UNDP-branded application pages once
opened; do not "fix" them back to `jobs.undp.org`.

## How it works

1. **Discovery: one request for the whole board.** Oracle CE's public
   candidate API is unauthenticated — no key, no cookie, no signed
   parameter:

   ```
   GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
       ?onlyData=true
       &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
       &finder=findReqs;siteNumber=CX_1,limit=500,sortBy=POSTING_DATES_DESC
   ```

   `limit=200` and `limit=500` both return all 108, and `TotalJobsCount`
   is asserted against the row count so the board outgrowing one page is a
   loud warning rather than silent under-collection. `finder` needs literal
   `;` and `,`, so URLs are built as strings — a `params` dict would
   percent-encode the separators and Oracle answers 400.
2. **Detail, one request per requisition:**
   `recruitingCEJobRequisitionDetails?expand=all&finder=ById;Id="<id>",siteNumber=CX_1`.
3. **The window is decided before any detail fetch.** The listing's
   `PostedDate` is exactly the date half of the detail's
   `ExternalPostedStartDate`, so — unlike devnetjobsindia — no detail
   request is ever spent on an out-of-window posting. `seen_old_ids.csv` is
   therefore a *record* of what was skipped, not a fetch-saving skip list.
   The listing's own `PostingEndDate` is null on every row; the real closing
   date exists only on the detail (`ExternalPostedEndDate` → `valid_through`).
4. **Oracle's taxonomy columns are dead on this tenant.** `JobFamily`,
   `JobFunction`, `Category`, `ContractType` and `WorkerType` are null on
   all 108 rows and `skills` is `[]` on every detail. The curated topic
   signal lives in `requisitionFlexFields` as **`Practice Area`** ("Health",
   "Governance", "Nature, Climate and Energy", …) — this board's analogue of
   devnetjobsindia's Relevant Sectors. It is kept raw in the `sectors`
   column and passed to `classify_job` as `skills`; it never decides the
   category.
5. **`requisitionFlexFields` also carries the employer.** `Agency` is UNDP,
   UNCDF or UNV — not always UNDP — and becomes `company`. The flex
   `Education & Work Experience` string ("Master's Degree - 2 year(s)
   experience OR Bachelor's Degree - 4 year(s) experience") is a cleaner
   experience source than the prose and is parsed first, taking the *lowest*
   alternative as the entry bar; the description regexes are the fallback.
   Grade, Bureau, Vacancy Type and Contract Duration are available in the
   API but are **not** stored — the rich CSV keeps the fleet's exact column
   contract.
6. **Boilerplate stripping.** Every vacancy is wrapped in UNDP's standard
   legal furniture: a tier-eligibility preamble and an
   equal-opportunity / harassment / scam-alert epilogue, together 1.5k–3.3k
   characters of text identical across postings and pure noise for both the
   classifier and the JD-rewrite pipeline. `strip_boilerplate()` cuts to the
   first real content heading and drops the epilogue **in English, French
   and Spanish** — UNDP country offices post in all three, and an
   English-only matcher silently leaves the French and Spanish furniture in.
   The cuts are conservative: the preamble is only cut when the text opens
   with it, a content heading found in the first 200 characters is treated
   as prose rather than a section start, an epilogue cut landing
   mid-sentence falls back to the last sentence end, and any cut that would
   leave under 400 characters is refused so an unrecognised template keeps
   its full text. Verified: 0 residual preambles or epilogues across all 96
   in-window rows.
7. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`) and nothing else — the scraper never
   assigns a category. Out of scope → dropped, counted, full row appended to
   `out-of-scope.csv`; `needs_review` → kept AND flagged.
8. **Location.** `PrimaryLocationCountry` is ISO alpha-2 except for two UNDP
   pseudo-codes: `U2` = "Home Based" (exported `job_type` "remote") and
   `U1` = "Multiple". Neither is a country, so name/code/dial stay blank
   rather than being guessed. The ISO table is inverted from the one already
   vetted in `../impactpool/`, extended with Barbados, Cabo Verde and
   Solomon Islands.
9. **Procurement notices.** UNDP's tenders are not in this API — they live
   on `procurement-notices.undp.org` (see below) — so `is_rfp` is a guard
   against a stray leak rather than a routine case. A tender-shaped title is
   marked `is_rfp` and force-flagged into `needs_review.csv` when the
   classifier keeps it.

UNDP publishes no salary anywhere → club salary columns stay blank, never
invented. `min_experience` and `qualification` are grounded extractions,
never inferred.

## Not in scope: the Consultancies tab

`cj_view_consultancies.cfm` is **not** this source. Its 191 rows link to
`procurement-notices.undp.org/view_negotiation.cfm?nego_id=<id>` —
Individual-Contractor tenders bid through the UNDP Quantum supplier portal,
a separate system with a separate schema and no overlap with the Oracle CE
requisition space. It is a viable candidate for its own scraper.

## Outputs

| File | Purpose |
|---|---|
| `undp_jobs.csv` | Rich cumulative store (dedup key: `job_id`) |
| `../../jobs_csv/<DD-MM-YYYY>/undp.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `seen_old_ids.csv` | Out-of-window ids and their dates |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged (other-profession titles, tenders) |

## Running

```bash
python scraper.py                # incremental run
python scraper.py --limit 20     # test run: at most 20 detail fetches
python scraper.py --since 2026-08-01
python test_filters.py           # 61 unit tests, no network
```

Time window: first run keeps the last 14 days; later runs use
`max(stored posted_date) − 2 days`.

**Cost.** First run = 1 listing request + 1 detail request per in-window
requisition — 97 requests, **3m 05s** wall clock at the 1 s delay (the delay
*is* the runtime; a 7-request burst with no delay returned 7× HTTP 200 in
~0.9 s each, so the host imposes no throttle of its own). Later runs cost
the listing request plus one detail per genuinely new posting — a handful.

Probed and built 2026-08-27.
