#!/usr/bin/env python3
"""Scrape healthcare job listings from shine.com (India).

Data source
-----------
shine.com is a Next.js site whose search pages are server-rendered with the
full result payload embedded in `<script id="__NEXT_DATA__">`:

    props.pageProps.initialState.jsrp.searchresult.data
        .results[20]  -- one JSON record per job card
        .count / .num_pages / .page

robots.txt disallows `/api/*`, so the JSON API is off-limits (master spec §1);
the `/job-search/` listing pages themselves are allowed and carry everything
we need — no detail-page fetches, no headless browser. A descriptive
User-Agent is accepted (verified: HTTP 200, same payload as a browser UA).

URL scheme (all verified live):

    https://www.shine.com/job-search/<query>-jobs?sort=1        page 1
    https://www.shine.com/job-search/<query>-jobs-<N>?sort=1    page N

`sort=1` = newest-first (default is relevance, which surfaces months-old
posts on page 1). Ordering is only *approximately* descending — promoted
cards interleave — so the early-stop rule is "stop when an entire page is
older than the cutoff", never "stop at the first old job".

`ind=13` = the "Medical / Healthcare" industry facet, and it DOES work as
a URL param (verified: count drops ~26k -> ~21k, the SSR payload echoes
query.ind = 13, and every returned card carries jInd "Medical /
Healthcare"; pagination and sort compose with it). Note the param is
`ind`, NOT the facet's field name `jIndID` — the latter is ignored. The
industry browse is the first crawl query (guaranteed industry-wide
coverage, master spec §2 "filter at the source"); the keyword queries
after it recover healthcare roles filed under OTHER industries.

Record fields used: id, jJT (title), jCName (company), jCID (company id),
jSal (salary display string), jJD (description HTML), jLoc (list of
locations), jPDate (posted datetime), jExpDate (expiry), jExp (experience
range display), jInd (industry name), jKwd (keywords), jSlug (detail-page
slug), jTypeC (1 full/2 part time), jEType (1 regular/2 contractual/
3 internship/4 work from home), jJobType (1 regular/2 walkin), jWM (work
mode flag; 0 everywhere in samples).

Verified quirks
---------------
* shine RE-DATES reposted/refreshed listings: with sort=1 the "healthcare"
  query showed 600+ jobs all dated "today" (page 30 was still today). The
  posted-date watermark therefore cannot bound crawl depth on its own —
  MAX_PAGES_PER_QUERY caps each query and the run prints a NOTE whenever a
  query is cut off by the cap rather than by the date window (no silent
  truncation). Dedup by job id absorbs the re-served reposts across runs.
* Shine truncates `jJD` at ~5000 chars SERVER-SIDE, everywhere: the search
  payload, the detail page's __NEXT_DATA__, the detail page's JSON-LD, AND
  the fully hydrated page in a real browser all show the same ~4999-char
  text ending mid-sentence, with no read-more control (verified 2026-08-26
  on job 19480916). Human visitors see the truncated text too — no public
  surface serves more, so it is unrecoverable by any means. Our own
  DESCRIPTION_MAX_CHARS (20k) never bites; the ~5000-char ceiling in the
  data is the source's, and the JD-rewrite prompt's truncation rule
  handles the dangling final sentence.
* Every record also carries its industry in `jInd` ("Medical /
  Healthcare" for ~70% of bare healthcare-query results). Since the
  2026-08-25 taxonomy migration this is a RAW SOURCE COLUMN only — it is
  recorded in the rich CSV and decides nothing.
* Salary strings: "Rs 4.0  - 4.5 Lakh/Yr", "< Rs 50,000  - 2.5 Lakh/Yr"
  (mixed absolute + lakh!), or "[Salary Hidden]" (the majority).
* jLoc can be ["All India"] — kept verbatim; it is a real answer, not a city.

Classification (taxonomy migration 2026-08-25)
----------------------------------------------
The ONLY keep/drop and labeling decision is the shared
`classification.classify_job(jJT, jKwd, strip_html(jJD))` — the weighted
role-family gate plus the two-level taxonomy split. Out-of-scope records
are dropped (counted excluded_out_of_scope); in-scope records get
category ("Non Clinical" | "Public Health"), sub_category, role_family
and the score trace. This scraper was already family-scored; the change
here is that the family moves out of `category` into `role_family` and
`category`/`sub_category` now carry the two-level taxonomy.

Title-only matching would drop roughly a third of genuine hits: many
Indian CRO listings carry a generic title such as "Senior Executive" and
name the domain only in the keyword tags — hence jKwd as `skills`.

Salary (master spec §3): capture, don't filter. Never excluded, never
invented. Monthly normalization: Lakh = 100,000 INR; /Yr divided by 12.

Outputs
-------
* shine_jobs.csv                        -- rich cumulative store (dedup key:
                                           job_id), watermark source of truth.
* needs_review.csv                      -- titles the classifier could not
                                           confidently place.
* ../../jobs_csv/<DD-MM-YYYY>/shine.csv -- HealthCareers.club 23-column
                                           schema, rewritten every run.

Time window (master spec §4): first run keeps INITIAL_WINDOW_DAYS (7) days;
later runs keep jobs newer than the stored max posted_date minus
WATERMARK_GRACE_DAYS (2). Run `python shine_scraper.py --help` for options.
"""

import argparse
import html as html_lib
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# The one shared classifier (see ../../instructions/taxonomy-migration-spec.md).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "shine_roles"
SITE_BASE = "https://www.shine.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SEARCH_PATH = "/job-search/"

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

# Query slugs crawled newest-first each run. A slug may carry extra query
# params after "?". The ind=13 industry browse comes first: it returns
# every job filed under "Medical / Healthcare" regardless of title. The
# keyword slugs then recover healthcare roles filed under other industries
# (e.g. a "Staff Nurse" at a company registered as Education). Dedup by
# job id makes the overlap between queries free.
# One search query per role family, plus the synonyms each family is
# actually advertised under. Master spec §1 prefers filtering at the source,
# and shine exposes free-text search at /job-search/<slug>-jobs — so we ask
# the site for these roles instead of crawling all of healthcare and
# discarding ~85% of it.
SEARCH_QUERIES = [
    # Industry facet browses (verified live 2026-08-24: /job-search/jobs
    # composes with ind= and sort=1, paginates as jobs-N, echoes query.ind).
    # These catch garbage-titled jobs no keyword query can find; the
    # classifier's skills/description rescue does the reading. Facet IDs from
    # the jIndID facet block on the browse-all page:
    #   ind=13 Medical / Healthcare (~25k)  ind=63 Pharma / Biotech (139)
    #   ind=31 NGO / Social Work (6)        ind=61 KPO / Analytics (46)
    # Deliberately NOT browsed: ind=20 BPO / Call Center (19k rows of
    # telecalling; the medical-coding keyword queries below already search
    # across all industries, so a full BPO browse buys ~0 unique keeps).
    "jobs?ind=13", "jobs?ind=63", "jobs?ind=31", "jobs?ind=61",
    # Clinical Research
    "clinical-research", "clinical-research-associate", "clinical-trials",
    "clinical-operations", "clinical-data-coordinator",
    # Clinical Data Management
    "clinical-data-management", "clinical-data-manager", "cdisc", "edc",
    # Pharmacovigilance
    "pharmacovigilance", "drug-safety", "signal-detection",
    # Regulatory Affairs
    "regulatory-affairs", "drug-regulatory-affairs", "regulatory-submissions",
    # Medical Writer
    "medical-writer", "medical-writing", "scientific-writer",
    # Medical Coding
    "medical-coding", "medical-coder", "clinical-coding",
    # MSL
    "medical-science-liaison", "medical-affairs",
    # Medical Reviewer
    "medical-reviewer", "medical-monitor",
    # HEOR
    "heor", "health-economics", "market-access",
    # TMF
    "trial-master-file",
    # Public Health — expanded 2026-08-24 to the ten-sub-category taxonomy
    # (Epidemiology, Program Management, M&E, Community Health, Health
    # Promotion & Education, Disease Programs, Nutrition, IPC, Health
    # Informatics & Data, PH Research). Every slug below was probed live on
    # 2026-08-24; only slugs with real PH density on page 1 are kept.
    # Shine's multi-word matching is erratic: some slugs behave as exact
    # phrases ("public-health" -> 2 results), others as OR-noise
    # ("health-program" -> 69,829; "monitoring-and-evaluation" -> 46,585
    # of IT monitoring; "community-health" -> 5,684 with ZERO in-scope on
    # page 1) — the noisy ones were dropped: 50 capped pages of noise per
    # run bought ~0 PH jobs. M&E / Community Health / WASH roles barely
    # exist on shine (it is a private-sector board); the classifier still
    # catches any that surface via the other queries.
    "public-health", "epidemiology", "epidemiologist",
    "disease-surveillance",                          # 18/20 in-scope
    "tuberculosis", "hiv", "malaria",                # disease programs
    "immunization", "vaccination",                   # 8 PH/page
    "nutritionist", "public-health-nutrition",       # 7 PH/page
    "infection-control",                             # 10 PH/page
    "health-informatics", "hmis",
    "health-promotion",
    "public-health-research",
    "maternal-child-health", "asha", "anganwadi",    # tiny but exact
    # Broad catch-all nets inherited from the `shine` sibling (added
    # 2026-08-26). They search across all industries and close the one gap the
    # role slugs leave: an in-scope job whose card says only "hospital" or
    # "medical" and matches no family slug. Probed live 2026-08-26, pages 1-2:
    # medical 19/40 in scope, healthcare 11/40, hospital 2/40 (the weakest —
    # mostly bedside roles the classifier vetoes). Heavily Medical Coding,
    # which the family slugs also reach, so dedup by job id absorbs most of
    # it — fetch wide, let classify_job reject.
    "healthcare", "hospital", "medical",
]

# First-run window, narrowed from the master spec §4 default of 7 to 2:
# these feeds post fast, so every extra day of first-run window costs a lot of
# crawl for jobs that are already stale by import time. Later runs ignore this
# entirely and use the watermark.
INITIAL_WINDOW_DAYS = 2
WATERMARK_GRACE_DAYS = 2

# shine re-dates reposts (see module docstring), so the date window cannot
# bound depth on its own. 50 pages x 20 jobs bounds each query at 1,000
# newest slots per run; the run summary warns when a query hits the cap.
MAX_PAGES_PER_QUERY = 50

# Per-query page-cap overrides (SHINE-04, 2026-08-27 audit). Three queries
# hit the 50-page cap before their date window closed on the 27-08 run —
# the industry browse and the two highest-volume keyword slugs — so they
# get deeper caps while the other ~52 queries keep the cheap default.
# An explicit --max-pages on the command line overrides this dict too.
QUERY_MAX_PAGES = {
    "jobs?ind=13": 120,       # Medical / Healthcare industry browse (~25k jobs)
    "clinical-coding": 120,
    "healthcare": 120,
}

# Bulk re-posters excluded at source (SHINE-01, 2026-08-27 audit). Five
# accounts mass-repost overseas (US/Canada) listings onto shine.com — city
# always "All India", titles like "Senior Clinical Data Manager Remote,
# Canada based", several literally starting "reputed company". Together
# they accounted for 439 of the 1,111 stored rows (39%): FlexBoard 134,
# remote zest jobs 98, vacancy global pro 88, vmysmartpros 75, remote
# click jobs 44. Matching is case-insensitive on the exact company name;
# skips are counted as "Excluded (bulk poster)", not as out-of-scope.
# Applies to future crawls only — existing store rows are left alone.
BULK_POSTER_BLOCKLIST = frozenset({
    "flexboard",
    "remote zest jobs",
    "vacancy global pro",
    "vmysmartpros",
    "remote click jobs",
})

REQUEST_DELAY_SECONDS = 1.2
REQUEST_TIMEOUT_SECONDS = 45
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 20_000

RICH_CSV = str(Path(__file__).resolve().parent / "shine_roles_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_id", "location",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "employment_type", "is_walkin",
    "work_mode", "experience_raw", "experience_min_years",
    "experience_max_years", "industry", "keywords", "category",
    "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "company_type", "match_signal", "needs_review", "posted_date",
    "expires_date", "description", "job_url", "scraped_at",
]

# jTypeC / jEType / jJobType enum decodings (from the search facets block).
JOB_TYPE_C = {1: "Full time", 2: "Part time"}
EMPLOYMENT_TYPE = {1: "Regular", 2: "Contractual", 3: "Internship",
                   4: "Work from home"}

log = logging.getLogger("shine_scraper")

# ----------------------------------------------------------------------------
# Classification — the shared two-level taxonomy is the ONLY decision maker
# ----------------------------------------------------------------------------

def apply_classification(row):
    """Stamp the shared taxonomy onto a rich row.

    Uses all three fields shine exposes — jJT (title), jKwd (keywords, the
    site's curated tag list, passed as `skills`) and the HTML-stripped jJD
    (description). Title-only matching would drop roughly a third of
    genuine hits: many Indian CRO listings carry a generic title such as
    "Senior Executive" and name the domain only in the keyword tags.

    jInd (the industry facet) is NOT a signal — it is a raw source column.

    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("keywords", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = "true" if verdict["needs_review"] else "false"
    return verdict["in_scope"]


# company_type is NOT a category — it fills the club schema's company_type
# column ("pharma" | "hospital") and never influences classify_job.
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical|drug|"
    r"iqvia|parexel|syneos|pfizer|thermo ?fisher",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


def is_bulk_poster(company):
    """True when the company name is on BULK_POSTER_BLOCKLIST (SHINE-01).

    Case-insensitive exact match on the trimmed name — substring matching
    would be too eager ("Global Pro Services" is not "vacancy global pro").
    """
    return clean_value(company).lower() in BULK_POSTER_BLOCKLIST


def max_pages_for_query(query, default=MAX_PAGES_PER_QUERY):
    """Per-query page cap: QUERY_MAX_PAGES override, else the default."""
    return QUERY_MAX_PAGES.get(query, default)


# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_value(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan", "null") else text


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|td)[^>]*>", " ", text or "",
                  flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_description(text, limit=DESCRIPTION_MAX_CHARS):
    """Cap at `limit` chars on a word boundary, marked with a trailing "…"."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    if len(spaced) >= limit * 0.5:
        cut = spaced
    return cut.rstrip() + "…"


def _salary_amounts(raw):
    """Return (lo, hi, period) as full-figure floats in the ORIGINAL period.

    Numbers written with a comma ("50,000") are absolute rupees; bare
    numbers under 1,000 are lakhs when the string says Lakh. period is
    "Yr" / "Mo" / ""; (None, None, period) when nothing parseable.
    """
    text = raw.lower()
    is_lakh = "lakh" in text or "lpa" in text
    if "/yr" in text or "p.a" in text or "annum" in text or "year" in text:
        period = "Yr"
    elif "/mo" in text or "p.m" in text or "month" in text:
        period = "Mo"
    else:
        period = ""

    amounts = []
    for token in _NUM_RE.findall(raw):
        value = float(token.replace(",", ""))
        if is_lakh and "," not in token and value < 1000:
            value *= 100_000
        amounts.append(value)
    if not amounts or not period:
        return (None, None, period)
    return (min(amounts), max(amounts), period)


def parse_salary(raw):
    """Master spec §3 — capture, never filter, never invent.

    "Rs 4.0  - 4.5 Lakh/Yr"      -> (raw, "33333", "37500", "Yr")
    "< Rs 50,000  - 2.5 Lakh/Yr" -> (raw, "4167", "20833", "Yr")
    "[Salary Hidden]" / ""       -> ("Not Disclosed", "", "", "")

    The middle values are normalized INR/month (spec §3): yearly amounts
    divide by 12.
    """
    raw = clean_value(raw)
    if not raw or "hidden" in raw.lower() or "not disclosed" in raw.lower():
        return ("Not Disclosed", "", "", "")
    lo, hi, period = _salary_amounts(raw)
    if lo is None:
        return (raw, "", "", period)
    if period == "Yr":
        lo, hi = lo / 12, hi / 12
    return (raw, str(int(round(lo))), str(int(round(hi))), period)


_EXP_RE = re.compile(r"(\d+)(?:\s*to\s*(\d+))?\s*Yrs?", re.IGNORECASE)


def parse_experience(raw):
    """"1 to 5 Yrs" -> ("1", "5"); "0 Yrs" -> ("0", "0"); "" -> ("", "")."""
    match = _EXP_RE.search(clean_value(raw))
    if not match:
        return ("", "")
    lo = match.group(1)
    return (lo, match.group(2) or lo)


def parse_date(value):
    """"2026-07-27T14:02:15" -> "2026-07-27"; "" for anything else."""
    text = clean_value(value)
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else ""


def decode_job_type(record):
    """Return (rich job_type, rich employment_type, club job_type enum)."""
    type_c = JOB_TYPE_C.get(record.get("jTypeC"), "")
    e_type = EMPLOYMENT_TYPE.get(record.get("jEType"), "")
    if e_type == "Work from home":
        club = "remote"
    elif type_c == "Part time":
        club = "part_time"
    else:
        club = "full_time"
    return (type_c or "Full time", e_type, club)


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None, since=None):
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def page_url(query, page):
    """"healthcare" -> .../healthcare-jobs?sort=1; page N appends -N to the
    slug; a "?k=v" suffix on the query becomes extra URL params. The bare
    slug "jobs" is the browse-all listing (/job-search/jobs, paginating as
    jobs-2), used with an ind= facet for industry browses — it must not
    grow a second "-jobs"."""
    slug, _, extra = query.partition("?")
    if slug != "jobs" and not slug.endswith("-jobs"):
        slug = slug + "-jobs"
    path = SEARCH_PATH + slug + ("-{}".format(page) if page > 1 else "")
    return SITE_BASE + path + "?sort=1" + ("&" + extra if extra else "")


def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return rp
    probe = page_url(SEARCH_QUERIES[0], 1)
    if not rp.can_fetch(USER_AGENT, probe):
        sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt check passed for %s", probe)
    return rp


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S)


def extract_results(html):
    """Return the searchresult.data dict from a listing page, or None."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
        return (payload["props"]["pageProps"]["initialState"]
                ["jsrp"]["searchresult"]["data"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def fetch_page(session, robots, query, page):
    """Fetch one listing page. Returns searchresult data dict or None."""
    url = page_url(query, page)
    if robots is not None and not robots.can_fetch(USER_AGENT, url):
        log.warning("robots.txt disallows %s — skipping", url)
        return None
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:   # permanent — don't retry
                log.warning("HTTP %d at %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            data = extract_results(resp.text)
            if data is None:
                last_error = "no __NEXT_DATA__ payload"
                continue
            return data
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES,
              last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(record, query):
    title = clean_value(record.get("jJT"))

    salary_raw, sal_min, sal_max, sal_period = parse_salary(record.get("jSal"))
    exp_min, exp_max = parse_experience(record.get("jExp"))
    job_type, employment_type, _ = decode_job_type(record)

    locations = record.get("jLoc") or []
    if not isinstance(locations, list):
        locations = [str(locations)]
    locations = [clean_value(l) for l in locations if clean_value(l)]

    slug = clean_value(record.get("jSlug"))
    return {
        "source": SITE,
        "job_id": clean_value(record.get("id")),
        "title": title,
        "company": clean_value(record.get("jCName")),
        "company_id": clean_value(record.get("jCID")),
        "location": ", ".join(locations),
        "salary_raw": salary_raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": sal_period,
        "job_type": job_type,
        "employment_type": employment_type,
        "is_walkin": "true" if record.get("jJobType") == 2 else "false",
        "work_mode": clean_value(record.get("jWM")),
        "experience_raw": clean_value(record.get("jExp")),
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "industry": clean_value(record.get("jInd")),
        "keywords": clean_value(record.get("jKwd")),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": classify_company_type(record.get("jCName")),
        # which crawl query surfaced this card (provenance, not a decision)
        "match_signal": query,
        "needs_review": "false",
        "posted_date": parse_date(record.get("jPDate")),
        "expires_date": parse_date(record.get("jExpDate")),
        "description": truncate_description(strip_html(record.get("jJD") or "")),
        "job_url": SITE_BASE + "/jobs/" + slug if slug else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def club_salary(salary_raw):
    """Full-figure INR amounts + period for the club schema.

    The rich CSV stores normalized monthly values (spec §3); the club export
    carries the ORIGINAL full amounts with their original period, matching
    the club export: "Rs 4.0 - 4.5 Lakh/Yr" -> (400000, 450000, per_annum).
    """
    raw = _clean(salary_raw)
    if not raw or raw.lower() in ("not disclosed", "[salary hidden]"):
        return ("", "", "", "")
    lo, hi, period = _salary_amounts(raw)
    if lo is None:
        return ("", "", "", "")
    club_period = "per_annum" if period == "Yr" else "per_month"
    return (str(int(round(lo))), str(int(round(hi))), club_period, "INR")


def rich_row_to_club_row(r):
    job_type = _clean(r.get("job_type"))
    employment_type = _clean(r.get("employment_type"))
    if employment_type == "Work from home":
        club_type = "remote"
    elif job_type == "Part time":
        club_type = "part_time"
    else:
        club_type = "full_time"

    # SHINE-02 (2026-08-27 audit): "All India" is a country-level answer,
    # not a city — 610/1,111 stored rows carried it as city_name. Map it
    # (and bare "India", and an empty location) to an empty city_name; the
    # country columns below already say India, and the raw location string
    # stays intact in the rich CSV's `location` column.
    city = _clean(r.get("location")).split(",")[0].strip()
    if city.lower() in ("all india", "india"):
        city = ""
    lo, hi, period, currency = club_salary(r.get("salary_raw"))

    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        "city_name": city,
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": club_type,
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": _clean(r.get("experience_min_years")),
        "max_experience": _clean(r.get("experience_max_years")),
        # shine has no structured qualification field — grounded extraction
        # from the description only, never inferred
        "qualification": extract_qualification(_clean(r.get("description"))),
        "min_salary": lo,
        "max_salary": hi,
        "salary_period": period,
        "salary_currency": currency,
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def write_club_csv(rich_df, run_date):
    club_rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(club_rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def crawl_query(session, robots, query, cutoff, known_ids, counters,
                new_rows, review_log, max_pages, limit):
    """Crawl one query slug newest-first. Returns "cap" | "dated" | "end"."""
    empty_pages, num_pages = 0, None
    for page in range(1, max_pages + 1):
        data = fetch_page(session, robots, query, page)
        if data is None:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                return "end"
            continue
        empty_pages = 0
        if num_pages is None:
            num_pages = data.get("num_pages")
            log.info("[%s] %s results, %s pages", query,
                     data.get("count"), num_pages)

        results = data.get("results") or []
        if not results:
            return "end"

        page_all_old = True
        for record in results:
            counters["scanned"] += 1
            try:
                posted = parse_date(record.get("jPDate"))
                # Undated jobs can't be placed in the window; sort=1 is
                # newest-first, so treat them as in-window and let dedup work.
                is_recent = (not posted) or posted >= cutoff
                if is_recent:
                    page_all_old = False
                else:
                    counters["excluded_old"] += 1
                    continue

                job_id = clean_value(record.get("id"))
                if not job_id:
                    log.warning("[%s] page %d: record without id — skipped",
                                query, page)
                    continue
                if job_id in known_ids:
                    counters["duplicates"] += 1
                    continue

                # SHINE-01: bulk re-posters of overseas listings are excluded
                # at source — their own counter, NOT excluded_out_of_scope.
                # The id joins known_ids so re-serves under other queries in
                # this run don't inflate the count.
                if is_bulk_poster(record.get("jCName")):
                    counters["excluded_bulk_poster"] += 1
                    known_ids.add(job_id)
                    continue

                row = job_to_rich_row(record, query)
            except Exception as exc:   # never let one card crash the run
                log.warning("[%s] page %d: skipping malformed record (%s)",
                            query, page, exc)
                continue

            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                continue

            if row["needs_review"] == "true":
                counters["needs_review"] += 1
                review_log.append({
                    "job_id": row["job_id"], "title": row["title"],
                    "company": row["company"], "industry": row["industry"],
                    "match_signal": row["match_signal"]})
            known_ids.add(job_id)
            new_rows.append(row)
            counters["new"] += 1
            if limit is not None and counters["new"] >= limit:
                return "end"

        if page_all_old:
            log.info("[%s] page %d entirely older than %s — stopping",
                     query, page, cutoff)
            return "dated"
        if num_pages is not None and page >= num_pages:
            return "end"
    return "cap"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from shine.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_QUERY,
                        metavar="N",
                        help="page cap PER QUERY (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs total (for test runs)")
    parser.add_argument("--queries", default=None, metavar="A,B,C",
                        help="comma-separated query slugs (default: built-in "
                             "list of {})".format(len(SEARCH_QUERIES)))
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark; keep jobs posted "
                             "on/after this date")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    queries = ([q.strip() for q in args.queries.split(",") if q.strip()]
               if args.queries else SEARCH_QUERIES)

    session = make_session()
    robots = check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s%s",
             len(known_ids), cutoff, " (--since override)" if args.since else "")

    counters = {"scanned": 0, "excluded_out_of_scope": 0,
                "excluded_bulk_poster": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    new_rows, review_log, capped_queries = [], [], []

    for query in queries:
        # An explicit --max-pages wins everywhere; otherwise the per-query
        # QUERY_MAX_PAGES override applies (SHINE-04).
        max_pages = (args.max_pages if args.max_pages != MAX_PAGES_PER_QUERY
                     else max_pages_for_query(query))
        outcome = crawl_query(session, robots, query, cutoff, known_ids,
                              counters, new_rows, review_log,
                              max_pages, args.limit)
        log.info("[%s] done (%s); %d new so far", query, outcome, counters["new"])
        if outcome == "cap":
            capped_queries.append((query, max_pages))
        if args.limit is not None and counters["new"] >= args.limit:
            break

    # ---- write rich cumulative CSV (source of truth) ----
    if new_rows:
        new_df = pd.DataFrame(new_rows, dtype=str)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[RICH_COLUMNS]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        review_df = pd.DataFrame(review_log)
        existing_review = load_existing(NEEDS_REVIEW_CSV)
        if existing_review is not None:
            review_df = pd.concat([existing_review, review_df],
                                  ignore_index=True)
            review_df = review_df.drop_duplicates(subset="job_id", keep="first")
        review_df.to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_df))

    print("\n===== Run summary =====")
    print("Jobs scanned:              {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope)  : {:>6,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (bulk poster)   : {:>6,}".format(counters["excluded_bulk_poster"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))
    if capped_queries:
        listed = ", ".join("{} ({} pages)".format(q, cap)
                           for q, cap in capped_queries)
        print("\n  NOTE: page cap hit before the date window closed"
              "\n  for: {}. Coverage of those queries is truncated — shine"
              "\n  re-dates reposts, so deeper pages may still hold in-window"
              "\n  jobs. Raise QUERY_MAX_PAGES or re-run with a higher"
              "\n  --max-pages to go deeper.".format(listed))


if __name__ == "__main__":
    main()
