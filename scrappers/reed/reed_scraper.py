#!/usr/bin/env python3
"""Scrape health & medicine job listings from reed.co.uk (UK).

Data source
-----------
reed.co.uk is a Next.js app; every listing page embeds the results as JSON
in <script id="__NEXT_DATA__">:

    GET https://www.reed.co.uk/jobs/health-jobs
        ?datecreatedoffset=LastTwoWeeks&sortby=DisplayDate&pageno=N
    -> props.pageProps.searchResults: {count, jobs: [...25...], promotedJobs}
       props.pageProps.criteria: echoes the applied filters

/jobs/health-jobs pins the Health & Medicine sector (parentSectorIds=[36]).
datecreatedoffset accepts Today/LastThreeDays/LastWeek/LastTwoWeeks/
LastMonth — LastTwoWeeks is the tightest superset of the agreed 10-day
window; the exact cutoff is applied client-side. Results come newest-first
by displayDate, so the watermark early-stop works; promoted cards carry
arbitrary dates and don't vote.

Listing jobDetail fields: jobId, jobTitle, jobDescriptionSnippet,
displayDate / dateCreated / expiryDate (ISO strings), displayLocationName,
countyLocation, ouName (posting recruiter/employer), isFullTime, isPartTime,
remoteWorkingOption (On-Site/Remote/Hybrid), salaryFrom/salaryTo,
salaryType, salaryDescription (type id), salaryCurrencyId (1 = GBP),
taxonomyLevel1/2, isPromoted, url (/jobs/<slug>/<id>).

CAUTION — listing salary numbers lie for undisclosed salaries: rows whose
salaryDescription type is 64 ("Competitive salary") still carry search-band
numbers like 20000-70000. Those are dropped as Not Disclosed. The detail
page's jobSalary.displaySalary string is authoritative, which is why
--enrich is the recommended mode (a 10-day window is only ~500 jobs).

Detail pages (--enrich):

    GET https://www.reed.co.uk/jobs/<slug>/<id>
    -> pageProps.consolidatedJobDetails.jobDetails: description (HTML),
       jobSalary.displaySalary, jobContractType, jobLocation (locationName,
       regionName), jobSector, isAgency/isEmployer/isReed

robots.txt: /jobs/ is allowed for User-agent: *; /api/ is disallowed and
therefore never called (master spec: robots.txt is binding). Reed accepts a
plain descriptive User-Agent — no browser impersonation needed.

Classification (taxonomy migration 2026-08-25)
----------------------------------------------
Reed previously had NO scope gate at all: every card in the Health &
Medicine / Scientific sector listings was kept and stamped with the old
profession enum (in practice non_clinical). It now runs the same gate as
the rest of the fleet — the shared
`classification.classify_job(jobTitle, taxonomy/sector, description)`:

* out-of-scope rows are DROPPED and counted excluded_out_of_scope
  (expect this to remove most of a UK health-sector listing: care
  assistants, support workers, RGNs and locum doctors are all out of
  scope now);
* in-scope rows get category ("Non Clinical" | "Public Health"),
  sub_category, role_family and the score trace;
* reed's own taxonomyLevel1/taxonomyLevel2 and jobSector are the curated
  `skills` signal and stay in the rich CSV as raw source columns — they
  never decide the category themselves.

The gate runs AFTER --enrich fetches the detail page, so the classifier
sees the full description rather than the 200-char snippet. That costs
detail requests for jobs that are then dropped; it is the honest order
(a pre-gate on the snippet would be a second, weaker classifier making
the real keep/drop decision).

Salary (master spec §3: capture, don't filter)
----------------------------------------------
UK salaries are GBP; the club schema's salary_currency enum only allows
INR/USD, so the club CSV's salary columns stay empty and the verbatim
displaySalary lives in the rich CSV. Never invented, never filtered.

Outputs (per the repo README + master-scraper-spec.md)
------------------------------------------------------
* reed_jobs.csv              — rich cumulative store (dedup key: job_id),
                               source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/reed.csv
                             — the same jobs mapped to the shared
                               HealthCareers.club schema (job_samples.csv).
                               expiryDate populates expires_at.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(10, as agreed); later runs keep only jobs newer than the newest stored
posted_date minus WATERMARK_GRACE_DAYS of overlap.

Run `python reed_scraper.py --help` for options.
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

import os

import pandas as pd
from curl_cffi import requests
from curl_cffi.requests import exceptions as requests_exceptions

# The one shared classifier (see ../../instructions/taxonomy-migration-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "reed"
SITE_BASE = "https://www.reed.co.uk"
ROBOTS_URL = SITE_BASE + "/robots.txt"
# Widened 2026-08-25 (fetch-wide/filter-tight pass): clinical research,
# pharmacovigilance and regulatory jobs on reed sit in the Scientific
# sector, not Health & Medicine, so both sector listings are walked and the
# title gate decides. NOTE: /jobs/scientific-jobs is a best-guess slug not
# yet probed live — a wrong slug 404s, search_page returns None, and that
# sector is skipped with an error log; the health walk is unaffected.
SEARCH_URLS = [
    SITE_BASE + "/jobs/health-jobs",        # sector 36, Health & Medicine
    SITE_BASE + "/jobs/scientific-jobs",    # Scientific (pharma/clinical R&D)
]
SEARCH_URL = SEARCH_URLS[0]   # kept for tests/back-compat

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

# Health & Medicine sector listing, newest-first, last two weeks at source
# (tightest superset of the agreed 10-day window, cut exactly client-side).
SEARCH_PARAMS = {"datecreatedoffset": "LastTwoWeeks", "sortby": "DisplayDate"}

INITIAL_WINDOW_DAYS = 10       # agreed window
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 25
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 3_000

# salaryDescription type ids whose salaryFrom/To are search bands, not real
# salaries (verified: 64 = "Competitive salary" listing with 20000-70000).
PLACEHOLDER_SALARY_TYPE_IDS = {64}
SALARY_CURRENCIES = {1: "GBP"}
SALARY_TYPE_PERIODS = {5: "per_annum", 1: "per_hour"}

RICH_CSV = str(Path(__file__).resolve().parent / "reed_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_kind", "city", "county",
    "region", "salary_raw", "salary_min", "salary_max", "salary_currency",
    "salary_period", "job_type", "remote_working", "contract_type",
    "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in", "needs_review",
    "company_type", "sponsored", "sector", "taxonomy_l1",
    "taxonomy_l2", "company_logo", "posted_date", "expires_date",
    "description", "job_url", "scraped_at",
]

log = logging.getLogger("reed_scraper")

# ----------------------------------------------------------------------------
# Field parsing / classification
# ----------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def extract_page_props(html):
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def iso_to_date(value):
    """"2026-07-12T11:12:05" -> "2026-07-12"; "" for anything unparseable."""
    value = (value or "")[:10]
    return value if re.match(r"^\d{4}-\d{2}-\d{2}$", value) else ""


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d)[^>]*>", " ", text or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_display_salary(display):
    """Parse a detail-page displaySalary string (the authoritative form).

    "£41,000 - £42,000 per annum"  -> (raw, 41000, 42000, GBP, per_annum)
    "£18.85 per hour"              -> (raw, 19, 19, GBP, per_hour)
    "Competitive salary"           -> ("Competitive salary", "", "", "", "")
    None                           -> ("Not Disclosed", "", "", "", "")
    """
    raw = (display or "").strip()
    if not raw:
        return ("Not Disclosed", "", "", "", "")
    nums = [float(n.replace(",", "")) for n in _NUM_RE.findall(raw)]
    if not nums:
        return (raw, "", "", "", "")
    lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
    if hi < lo:
        lo, hi = hi, lo
    text = raw.lower()
    if "annum" in text or "year" in text:
        period = "per_annum"
    elif "month" in text:
        period = "per_month"
    elif "hour" in text:
        period = "per_hour"
    elif "day" in text:
        period = "per_day"
    elif "week" in text:
        period = "per_week"
    else:
        return (raw, "", "", "", "")
    currency = "GBP" if "£" in raw else ""
    return (raw, str(int(round(lo))), str(int(round(hi))), currency, period)


def listing_salary(job_detail):
    """Salary from listing numerics — only when they are a real disclosed
    salary (placeholder "Competitive" rows carry meaningless bands)."""
    desc_id = job_detail.get("salaryDescription")
    lo, hi = job_detail.get("salaryFrom"), job_detail.get("salaryTo")
    if desc_id in PLACEHOLDER_SALARY_TYPE_IDS or not lo:
        return ("Not Disclosed", "", "", "", "")
    currency = SALARY_CURRENCIES.get(job_detail.get("salaryCurrencyId"), "")
    period = SALARY_TYPE_PERIODS.get(job_detail.get("salaryType"), "")
    lo_i = str(int(round(float(lo))))
    hi_i = str(int(round(float(hi or lo))))
    raw = "{} {} - {}{}".format(currency or "?", lo_i, hi_i,
                                " " + period.replace("_", " ") if period else "")
    return (raw, lo_i, hi_i, currency, period)


def map_job_type(remote_working, is_full_time, is_part_time):
    rw = (remote_working or "").strip().lower()
    if "hybrid" in rw:
        return "hybrid"
    if "remote" in rw:
        return "remote"
    if is_part_time and not is_full_time:
        return "part_time"
    return "full_time"


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    Reed's own taxonomyLevel1 / taxonomyLevel2 / jobSector are joined into
    the curated `skills` signal (they name the discipline — "Clinical
    Research", "Pharmacovigilance" — where the title often does not); the
    raw values stay in the rich CSV as source columns only.

    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    site_taxonomy = ", ".join(t for t in (row.get("taxonomy_l1", ""),
                                          row.get("taxonomy_l2", ""),
                                          row.get("sector", "")) if t)
    verdict = classify_job(row.get("title", ""), site_taxonomy,
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = verdict["needs_review"]
    return verdict["in_scope"]


# company_type is NOT a category — it fills the club schema's company_type
# column ("pharma" | "hospital") and never influences classify_job.
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|life ?science|"
    r"diagnostic|clinical research|medical devices?", re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None):
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    # Reed serves plain clients happily — descriptive UA, no impersonation.
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests_exceptions.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in SEARCH_URLS + [SITE_BASE + "/jobs/some-job/1"]:
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _get_html(session, url, params=None):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS,
                               allow_redirects=True)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.text
        except requests_exceptions.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def search_page(session, page_no, search_url=SEARCH_URL):
    """Fetch one listing page. Returns (jobs, promoted, count) or
    (None, None, None) on failure; job dicts are the flattened jobDetail
    merged with the card's url."""
    params = dict(SEARCH_PARAMS, pageno=page_no)
    html = _get_html(session, search_url, params=params)
    props = extract_page_props(html) if html else None
    if props is None:
        return (None, None, None)
    results = props.get("searchResults") or {}

    def flatten(cards):
        flat = []
        for card in cards or []:
            detail = dict(card.get("jobDetail") or {})
            detail["url"] = card.get("url") or ""
            detail["logoImage"] = card.get("logoImage") or ""
            flat.append(detail)
        return flat

    return (flatten(results.get("jobs")), flatten(results.get("promotedJobs")),
            results.get("count"))


def fetch_detail(session, job_url):
    """Fetch a job detail page's jobDetails dict for --enrich, or None."""
    html = _get_html(session, SITE_BASE + job_url)
    props = extract_page_props(html) if html else None
    if not props:
        return None
    return (props.get("consolidatedJobDetails") or {}).get("jobDetails")


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def _company_kind(detail):
    if not detail:
        return ""
    if str(detail.get("isReed")).lower() == "true":
        return "reed"
    if str(detail.get("isEmployer")).lower() == "true":
        return "employer"
    if str(detail.get("isAgency")).lower() == "true":
        return "agency"
    return ""


def job_to_rich_row(job, detail=None):
    """Build one rich row from a flattened listing job (+ detail payload)."""
    detail = detail or {}
    title = (job.get("jobTitle") or detail.get("title") or "").strip()

    if detail.get("jobSalary"):
        salary = parse_display_salary(
            (detail["jobSalary"] or {}).get("displaySalary"))
    else:
        salary = listing_salary(job)
    salary_raw, sal_min, sal_max, sal_cur, sal_per = salary

    job_location = detail.get("jobLocation") or {}
    contract = (detail.get("jobContractType") or {}).get("name") or ""
    sector = (detail.get("jobSector") or {}).get("name") or ""

    description = strip_html(detail.get("description") or "") or \
        (job.get("jobDescriptionSnippet") or "").strip()

    hours = detail.get("jobEmploymentHours") or {}
    is_full = job.get("isFullTime", hours.get("isFullTime", True))
    is_part = job.get("isPartTime", hours.get("isPartTime", False))

    return {
        "source": SITE,
        "job_id": str(job.get("jobId") or detail.get("id") or ""),
        "title": title,
        "company": (job.get("ouName") or "").strip(),
        "company_kind": _company_kind(detail),
        "city": (job_location.get("locationName") or
                 job.get("displayLocationName") or "").strip(),
        "county": (job.get("countyLocation") or "").strip(),
        "region": (job_location.get("regionName") or "").strip(),
        "salary_raw": salary_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": sal_cur,
        "salary_period": sal_per,
        "job_type": map_job_type(job.get("remoteWorkingOption"), is_full, is_part),
        "remote_working": (job.get("remoteWorkingOption") or "").strip(),
        "contract_type": contract,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "company_type": classify_company_type(job.get("ouName")),
        "sponsored": bool(job.get("isPromoted")),
        "sector": sector,
        "taxonomy_l1": (job.get("taxonomyLevel1") or "").strip(),
        "taxonomy_l2": (job.get("taxonomyLevel2") or "").strip(),
        "company_logo": job.get("logoImage") or "",
        "posted_date": iso_to_date(job.get("displayDate")),
        "expires_date": iso_to_date(job.get("expiryDate") or
                                    detail.get("expiryDate")),
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": SITE_BASE + (job.get("url") or ""),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map to the 23-column club schema (CLUB_COLUMNS, shared). GBP cannot
    be represented by the club salary_currency enum, so salary columns stay
    empty (the rich CSV keeps the verbatim values). is_active/expires_at
    are retired; reed's expiryDate lives on in the rich CSV's
    expires_date."""
    currency = _clean(r.get("salary_currency"))
    period = _clean(r.get("salary_period"))
    lo, hi = _clean(r.get("salary_min")), _clean(r.get("salary_max"))
    exportable = (bool(lo) and currency in ("INR", "USD")
                  and period in ("per_month", "per_annum"))
    return {
        "country_name": "United Kingdom",
        "country_code": "GB",
        "country_dial_code": "+44",
        "city_name": _clean(r.get("city")),
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": _clean(r.get("company_logo")),
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": _clean(r.get("job_type")) or "full_time",
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        # reed has no structured qualification field — grounded extraction
        # from the description only, never inferred
        "qualification": extract_qualification(_clean(r.get("description"))),
        "min_salary": lo if exportable else "",
        "max_salary": (hi or lo) if exportable else "",
        "salary_period": period if exportable else "",
        "salary_currency": currency if exportable else "",
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape health & medicine jobs from reed.co.uk.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (for test runs)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each new job's detail page (recommended:"
                             " full description + authoritative salary)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    new_rows, review_log = [], []
    stop = False

    # One newest-first walk per sector listing (--max-pages caps each walk;
    # --limit caps the TOTAL). jobId dedup absorbs cross-sector overlap.
    for search_url in SEARCH_URLS:
      if stop:
          break
      log.info("--- listing: %s ---", search_url)
      page_no, total, empty_pages = 1, None, 0
      while not stop:
        if args.max_pages is not None and page_no > args.max_pages:
            break
        if total is not None and (page_no - 1) * PAGE_SIZE >= total:
            break
        jobs, promoted, count = search_page(session, page_no, search_url)
        if jobs is None:
            log.error("Page %d of %s failed after retries — stopping this "
                      "listing", page_no, search_url)
            break
        if total is None and count is not None:
            total = count
            log.info("Site reports %d jobs in the window (~%d pages)",
                     total, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if not jobs and not promoted:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            page_no += 1
            continue
        empty_pages = 0

        organic_all_old, organic_seen = True, False
        for job in promoted + jobs:
            counters["scanned"] += 1
            try:
                job_id = str(job.get("jobId") or "")
                posted = iso_to_date(job.get("displayDate"))
                is_recent = bool(posted) and posted >= cutoff
                if not job.get("isPromoted"):
                    organic_seen = True
                    if is_recent:
                        organic_all_old = False
                if not is_recent:
                    counters["excluded_old"] += 1
                    continue
                if job_id in known_ids:
                    counters["duplicates"] += 1
                    continue

                detail = (fetch_detail(session, job.get("url"))
                          if args.enrich and job.get("url") else None)
                row = job_to_rich_row(job, detail)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job on page %d: %s", page_no, exc)
                continue

            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                continue

            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "company": row["company"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1
            if args.limit is not None and counters["new"] >= args.limit:
                stop = True
                break

        # Newest-first by displayDate; promoted cards don't vote.
        if organic_seen and organic_all_old:
            log.info("Page %d organic results all older than %s — stopping",
                     page_no, cutoff)
            break
        page_no += 1

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
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Jobs scanned:            {:>5,}".format(counters["scanned"]))
    print("Excluded (out of scope): {:>5,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:    {:>5,}".format(counters["needs_review"]))
    print("New jobs added:          {:>5,}".format(counters["new"]))
    print("Duplicates skipped:      {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
