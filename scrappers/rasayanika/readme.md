# rasayanika scraper

Scrapes vacancies from [www.rasayanika.com](https://www.rasayanika.com) — a chemistry and pharma job portal for India.
Rasayanika is a WordPress site with a fully open REST API, so the scraper
never parses a listing page and never fetches a detail page: the posts
endpoint returns the full post body inline.

## How it works

1. **Discovery: the WP REST posts endpoint.**

   ```
   GET https://www.rasayanika.com/wp-json/wp/v2/posts
       ?categories_exclude=<editorial ids>&per_page=100&page=N
       &orderby=date&order=desc
       &_fields=id,date,link,title,content,categories
   ```

   One request covers 100 postings **with their full bodies**, newest
   first, so a daily run costs one or two requests. Corpus ~25,815 posts;
   ~266 new posts per 30 days.

2. **Separating jobs from news — the site's own taxonomy.** This site mixes
   vacancies with editorial (news digests, career advice, admissions, exam
   alerts) in the **same `post` type**, so the post type alone is not a
   filter. The `category` taxonomy is the separator — but the obvious
   choice, crawling the `jobs` category, **silently drops real
   vacancies**: over 30 days `jobs` held 187 of 266 posts while only
   23 were genuinely editorial. The missing postings are
   fellowship/JRF/project-associate vacancies the site files under
   `government-vacancy`/`fellowships` and never tags `jobs`.

   So the crawl is the **inverse** — fetch wide, filter tight: every post
   *except* the editorial categories

   `news`, `rasayanika-times`, `admissions`, `career-advice`, `exam-alerts`,
   `scholarships`, `events`

   whose ids are resolved from those slugs at startup (never hardcoded; a
   slug that has disappeared aborts the run rather than silently widening
   the crawl back into the news feed). Jobs and editorial are effectively
   disjoint on the live site, so the exclusion removes editorial without
   eating vacancies.

3. **Residual news risk is flagged, never hidden.** Because the crawl is
   wider than the site's own `jobs` category, a kept post that is *not* in
   `jobs` **and** publishes no labelled vacancy field is flagged
   `needs_review`. Article-shaped titles (listicles, "how to", result/admit
   card alerts) and posts whose employer could not be determined are
   flagged the same way. The `in_jobs_category` column keeps the site's own
   verdict auditable on every row.

4. **Field extraction.** ~9 in 10 posts publish their facts as labelled
   `<li><strong>Label:</strong> value</li>` bullets under a "Job Details"
   heading; ~1 in 7 uses a two-column `Particulars | Details` table
   instead. Both shapes feed one label map (Company/Organisation/Institute,
   Position, Location, Experience, Qualification, Salary/Stipend/Fellowship
   Amount, Application Deadline, Job Type). There is **no schema.org
   JSON-LD** anywhere on the site.

5. **Classification** is the shared two-level taxonomy
   (`_shared/classification.py`). The post's own WP **category slugs** are
   the site's curated role signal: passed to `classify_job` as `skills`,
   kept in the rich CSV as the raw `site_categories` column, and never
   allowed to decide the category. Out of scope → dropped, counted, full
   row appended to `out-of-scope.csv`; `needs_review` → kept **and**
   flagged.

## Measured in-scope rate

Scored through `classify_job` on a live 493-post sample (60 days,
2026-08-27): **23 in scope — 23 of 493 kept posts (4.7%)**.

**All 15** of the `Public Health Research` admissions are bench
"Research Associate" titles at chemistry labs and CROs (Lupin, Jubilant,
Dishman Carbogen, IISER), not public health research — a shared-classifier
over-admission reported upstream, not patched here (`taxonomy_keywords.py` /
`role_families.py` are never edited by a scraper). Excluding those, the
strict rate is **8 of 493 (1.6%)** — four Clinical Research Associate posts,
three Regulatory Affairs and one Pharmacovigilance, all at pharma
manufacturers advertising to chemistry graduates. This board is
structurally a **chemistry** board (M.Sc Chemistry, paints, coatings,
petrochemicals), which is why the rate is an order of magnitude below
biotecnika's.

## Grounded fields, never invented

* **Company** is a labelled field on a minority of posts, so it falls back
  to a grounded parse of the title ("Research Associate Jobs at Lupin",
  "Sun Pharma Hiring Chemistry Graduates"). The leading
  "<Company> Hiring" shape is tried first, then the **rightmost** `at` —
  otherwise "CDM Jobs in Bengaluru at ICON" yields the city. An
  unparseable title leaves company blank and flags `needs_review`.
* **posted_date** is the WP `date` field. `modified` is deliberately
  ignored — the site re-touches old posts for SEO, which would re-date them.
* **valid_through** is the "Application Deadline"/"Last Date to Apply"
  field — an apply-by date, never a posted date.
* **Salary** is stated on a minority of posts, usually a monthly fellowship
  stipend ("Rs. 67,000/- p.m.") or an LPA range. Parsed when present,
  "Not Disclosed" otherwise. A foreign currency that cannot be converted
  keeps its raw string and leaves the numeric club columns blank.
* **company_type** (`hospital`|`pharma`) is a club field, not a taxonomy
  category, and is read from the **employer name only** — nearly every
  in-scope title here contains the word "Clinical", which would otherwise
  type every CRO as a hospital.

## Outputs

| File | Purpose |
|---|---|
| `rasayanika_jobs.csv` | Rich cumulative store (dedup key: WP post `id`) |
| `../../jobs_csv/<DD-MM-YYYY>/rasayanika.csv` | HealthCareers.club `CLUB_COLUMNS` export (full-store dump) |
| `out-of-scope.csv` | Dropped rows + their skip list (reversible) |
| `needs_review.csv` | Kept but flagged (news-shaped, unknown employer, outside `jobs`) |

Unlike `devnetjobsindia` there is **no `seen_old_ids.csv`**: date and body
arrive in the listing itself, so an out-of-window post costs nothing to
re-see and the watermark alone bounds the crawl.

The rich schema mirrors `devnetjobsindia`'s with two source-specific slots
adapted — `sectors` → `site_categories` (this site's curated raw signal)
and `is_rfp` dropped (no procurement notices in this post type) — plus the
`pharmarecruiter` salary block, because unlike devnetjobsindia this site
does publish stipends and `CLUB_COLUMNS` has somewhere to put them.

## Running

```bash
python scraper.py                  # incremental run
python scraper.py --max-pages 2    # test run: at most 200 posts
python scraper.py --since 2026-06-28
python test_filters.py             # 51 unit tests, no network
```

Time window: first run keeps the last 30 days; later runs use
`max(stored posted_date) − 2 days`. Steady state is one or two requests.

robots.txt allows the REST API (only `/wp-admin/` is disallowed) and is
verified at startup. Probed and built 2026-08-27.
