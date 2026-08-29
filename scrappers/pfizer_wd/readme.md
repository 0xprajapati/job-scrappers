# pfizer scraper (Workday CXS)

REBUILT 2026-08-28 after a cross-session collision deleted the original
folder (see testing/pipeline.md, "Territory split"). Code is the bms/lilly
mirrored Workday implementation with pfizer config; the data files are the
preserved pre-collision originals (10-row store from the 2026-08-28 first
run, out-of-scope and needs_review sidecars filtered back to this session's
rows).

* Board: pfizer.wd1.myworkdayjobs.com, tenant `pfizer`, site `PfizerCareers`
  (~561 postings). robots.txt explicitly Allows /PfizerCareers/ and
  disallows /InternalJobs/ + /refreshFacet/ (checked at startup).
* Facet scope (names resolved to live ids each run): Medical (76),
  Research and Development (26), Regulatory Affairs (4), and Market Access
  (11) — the fourth added on the BMS under-coverage lesson: HEOR/pricing
  roles hide under Market Access-style groups.
* Locations are country-first ("United States - Maryland - Baltimore");
  city = last surviving segment.
* Listing order: newest-first held on probe, with one stray pinned older
  row at offset 0 (same pattern as premier_research) — the windowing
  tolerates it.
* Cadence intel from the collision-era runs: ~150 details/week ≈ 15
  in-scope + 2 needs_review (both French-Canadian bilingual CRA postings).
* Requisition ids are bare numbers; a posting can carry endDate one day
  after startDate — run daily.

Usage: `../../.venv/bin/python scraper.py` (flags: --since, --max-pages,
--limit, --run-date, --verbose). Tests: `python -m unittest discover`.
