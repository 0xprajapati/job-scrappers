#!/usr/bin/env python3
"""Scrape healthcare job listings from naukrigulf.com.

Data source
-----------
naukrigulf.com is a client-rendered SPA backed by a public JSON API under
/spapi/. Both endpoints require two static headers (400 without them):

    appid: 205
    systemid: 2323

1. Listing (search) API — one call per page of 30 jobs:

       GET https://www.naukrigulf.com/spapi/jobapi/search
           ?ClusterInd=30,37          # industries: 30=Medical, 37=Pharma
           &Freshness=1,3,7,15        # posted within the last 15 days
           &Keywords=healthcare
           &SortPreference=date       # newest-first (enables early stop)
           &Limit=30&Offset=N&pageNo=P
       -> {"TotalJobsCount": "593", "Jobs": [{"Job": {...}}, ...]}

   Each Job: Designation, Location ("City - Country (UAE)"), jobInfo (short
   summary, may be null), Experience {Min, Max}, Company {Name, Id}, JobId,
   JdURL (absolute detail URL), LatestPostedDate (unix epoch), Vacancies,
   LogoUrl. The listing has NO salary and NO employment-type fields.

2. Detail API (used by --enrich, one call per new job):

       GET https://www.naukrigulf.com/spapi/jobs/<JobId>
       -> {"Job": {Description (HTML), IndustryType, FunctionalArea,
                   Compensation {jobMinCurrency "AED 3,000", jobMaxCurrency,
                                 IsCtcHidden, salaryTimeBrand},
                   Other {currLabel, PostedDate}, Company {Profile},
                   DesiredCandidate {Education, Nationality},
                   employmentType "Full Time", locationType "On Site"}}

robots.txt (checked at startup) allows /healthcare-jobs and /spapi/.

The site sits behind Akamai bot protection which resets connections from
non-browser TLS fingerprints AND non-browser User-Agents, so requests go
through curl_cffi with Chrome impersonation; contact info is carried in the
RFC-standard From header.

Salary (master spec §3: capture, don't filter)
----------------------------------------------
Salaries are almost always in AED/SAR/QAR and only visible via --enrich.
The rich CSV stores the verbatim string + parsed amounts + currency. The
club CSV can only represent INR/USD, so its salary columns stay empty for
other currencies (or when the period is unknown) — nothing is invented.

Outputs (per the repo README + master-scraper-spec.md)
------------------------------------------------------
* naukrigulf_jobs.csv        — rich cumulative store (dedup key: job_id),
                               source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/naukrigulf.csv
                             — the same jobs mapped to the shared
                               HealthCareers.club schema (job_samples.csv).

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(15 = the source's own Freshness cap); later runs keep only jobs newer than
the newest stored posted_date minus WATERMARK_GRACE_DAYS of overlap.

Run `python naukrigulf_scraper.py --help` for options.
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
from curl_cffi import requests
from curl_cffi.requests import exceptions as requests_exceptions

# The one shared two-level classifier (taxonomy 2026-08-25). No local
# category regexes/enums — classify_job alone decides keep/drop + labels.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "naukrigulf"
SITE_BASE = "https://www.naukrigulf.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SEARCH_URL = SITE_BASE + "/spapi/jobapi/search"
DETAIL_URL = SITE_BASE + "/spapi/jobs/{job_id}"
SEARCH_PAGE_URL = SITE_BASE + "/healthcare-jobs"  # the crawl target robots-wise

# Identity used for robots.txt matching and the RFC-standard From contact
# header. The wire User-Agent must stay Chrome's (see make_session).
SCRAPER_IDENTITY = "HealthCareersJobScraper/1.0"
CONTACT = "https://github.com/0xprajapati/job-scrappers"

# Static headers the /spapi/ endpoints require (public values used by the
# site's own frontend; requests without them get HTTP 400).
API_HEADERS = {"appid": "205", "systemid": "2323", "Accept": "application/json"}

# naukrigulf sits behind Akamai, which resets/blackholes both non-browser TLS
# fingerprints and non-browser User-Agents (curl exit 92 / infinite read
# timeouts). curl_cffi's Chrome impersonation is required to fetch at all;
# contact info therefore travels in the From header instead of the UA.
IMPERSONATE = "chrome"

# Filters mirroring the agreed listing URL:
# naukrigulf.com/healthcare-jobs?freshness=1,3,7,15&industryType=30,37
CLUSTER_IND = "30,37"          # 30 = Medical/Healthcare, 37 = Pharmaceutical
FRESHNESS = "1,3,7,15"         # posted within the last 15 days
# Widened 2026-08-25 (fetch-wide/filter-tight pass): the single "healthcare"
# keyword narrowed the search WITHIN the two industries, hiding role-family
# jobs whose cards never say "healthcare" (a "Regulatory Affairs Specialist"
# at a pharma). One paginated walk runs per keyword; JobId dedup makes the
# overlap free, and the newest-first early stop bounds each walk.
KEYWORD_QUERIES = [
    "healthcare",
    "clinical research", "clinical data management", "pharmacovigilance",
    "drug safety", "regulatory affairs", "medical writer", "medical coding",
    "medical affairs", "market access", "public health", "epidemiology",
    "infection control",
    # 2026-08-25 Public Health widening: cover all ten PH sub-categories.
    # Gulf market, so the India-only program terms (ASHA, anganwadi, NHM)
    # are deliberately absent; the watermark early-stop keeps the extra
    # queries cheap after the first run.
    "epidemiologist", "disease surveillance",
    "public health program",
    "monitoring and evaluation",
    "community health",
    "health educator", "health promotion",
    "tuberculosis", "immunization", "vaccination",
    "public health nutrition", "nutritionist",
    "health informatics",
    "public health research",
]
SORT_PREFERENCE = "date"       # newest-first -> watermark early stop works

# No salary filter (master spec): salaries are captured, never filtered on.
# The source's Freshness filter already caps listings at 15 days, so the
# first-run window matches it. Later runs use the watermark minus grace.
INITIAL_WINDOW_DAYS = 15
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 30
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3            # tolerate transient empty pages
# Sanity cap only — the detail API serves full descriptions, and rows must
# not be clipped mid-sentence. (Was 3_000 until 2026-08-26, which silently
# truncated ~1 in 5 stored descriptions; those old rows are unrecoverable
# because the jobs expired off the 15-day board.)
DESCRIPTION_MAX_CHARS = 20_000

RICH_CSV = str(Path(__file__).resolve().parent / "naukrigulf_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
# jobs_csv/ lives at the repo root, two levels up from scrappers/naukrigulf/.
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Rich (source-of-truth) columns — superset, keeps everything the API gives.
# category holds "Non Clinical" | "Public Health"; the shared score trace
# (role_family / all_families / family_scores / family_confidence /
# matched_in) and needs_review keep every admission auditable.
# industry_type / functional_area stay as RAW source columns only — they
# feed classify_job as the skills signal but never decide the category.
RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_id", "city", "country",
    "country_code", "country_dial_code", "salary_raw", "salary_min",
    "salary_max", "salary_currency", "salary_period", "experience_min_years",
    "experience_max_years", "job_type", "location_type", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in", "needs_review",
    "company_type", "industry_type", "functional_area", "education",
    "vacancies", "company_logo", "company_about", "posted_date",
    "description", "job_url", "scraped_at",
]

# The club CSV contract (23 columns) is imported from _shared/classification
# as CLUB_COLUMNS — never hand-copied here.

NEEDS_REVIEW_COLUMNS = ["job_id", "title", "company", "category",
                        "sub_category", "role_family", "matched_in"]

# Gulf board: map normalized country name -> (ISO code, dial code).
COUNTRY_META = {
    "united arab emirates": ("AE", "+971"),
    "saudi arabia": ("SA", "+966"),
    "qatar": ("QA", "+974"),
    "kuwait": ("KW", "+965"),
    "bahrain": ("BH", "+973"),
    "oman": ("OM", "+968"),
    "jordan": ("JO", "+962"),
    "lebanon": ("LB", "+961"),
    "egypt": ("EG", "+20"),
    "iraq": ("IQ", "+964"),
    "yemen": ("YE", "+967"),
    "india": ("IN", "+91"),
    "pakistan": ("PK", "+92"),
    "morocco": ("MA", "+212"),
    "algeria": ("DZ", "+213"),
    "tunisia": ("TN", "+216"),
    "libya": ("LY", "+218"),
    "sudan": ("SD", "+249"),
    "turkey": ("TR", "+90"),
    "philippines": ("PH", "+63"),
    "sri lanka": ("LK", "+94"),
    "maldives": ("MV", "+960"),
    "japan": ("JP", "+81"),
    "bangladesh": ("BD", "+880"),
    "nepal": ("NP", "+977"),
    "kenya": ("KE", "+254"),
    "nigeria": ("NG", "+234"),
    "south africa": ("ZA", "+27"),
    "malaysia": ("MY", "+60"),
    "singapore": ("SG", "+65"),
    "united kingdom": ("GB", "+44"),
    "united states": ("US", "+1"),
}

log = logging.getLogger("naukrigulf_scraper")

# ----------------------------------------------------------------------------
# Field parsing / classification
# ----------------------------------------------------------------------------

_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def parse_location(location):
    """Split "Dubai - United Arab Emirates (UAE)" -> ("Dubai", "United Arab Emirates").

    "Qatar - Qatar" -> ("Qatar", "Qatar"). Unknown formats keep the whole
    string as both city and country so nothing required ends up empty.
    """
    location = (location or "").strip()
    if not location:
        return ("", "")
    if " - " in location:
        city, country = location.split(" - ", 1)
    else:
        city = country = location
    country = _PAREN_RE.sub("", country).strip()
    return (city.strip(), country)


def country_meta(country):
    """Return (iso_code, dial_code) for a country name, or ("", "")."""
    return COUNTRY_META.get((country or "").strip().lower(), ("", ""))


def epoch_to_date(epoch):
    """Unix epoch (str/int) -> "YYYY-MM-DD" (UTC), or "" when unparseable."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    """HTML -> readable single-spaced text."""
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d)[^>]*>", " ", text or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_salary(compensation, curr_label=""):
    """Parse the detail API's Compensation block.

    Returns (salary_raw, min_int, max_int, currency, period). Salaries are
    captured, never filtered on; undisclosed -> ("Not Disclosed", "", "", "", "").
    """
    comp = compensation or {}
    if str(comp.get("IsCtcHidden", "")).lower() == "true":
        return ("Not Disclosed", "", "", "", "")
    lo_raw = (comp.get("jobMinCurrency") or "").strip()
    hi_raw = (comp.get("jobMaxCurrency") or "").strip()
    if not lo_raw:
        return ("Not Disclosed", "", "", "", "")

    raw = lo_raw + (" - " + hi_raw if hi_raw else "")
    nums = [float(n.replace(",", "")) for n in _NUM_RE.findall(raw)]
    if not nums:
        return (raw, "", "", "", "")
    lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
    if hi < lo:
        lo, hi = hi, lo

    currency = (curr_label or "").strip().upper()
    if not currency:
        match = re.match(r"([A-Z]{3})\b", lo_raw.upper())
        currency = match.group(1) if match else ""
    # the API sometimes sends a currency symbol instead of an ISO code
    currency = {"₹": "INR", "$": "USD", "US$": "USD"}.get(currency, currency)

    period = ""
    brand = (comp.get("salaryTimeBrand") or "").strip().lower()
    if "month" in brand:
        period = "per_month"
    elif "ann" in brand or "year" in brand:
        period = "per_annum"
    return (raw, str(int(round(lo))), str(int(round(hi))), currency, period)


def map_job_type(employment_type, location_type="", title=""):
    """Map detail-API employmentType/locationType -> club job_type enum.

    Without --enrich only the title is available; the board is overwhelmingly
    full-time, so that is the default.
    """
    lt = (location_type or "").strip().lower()
    if "remote" in lt:
        return "remote"
    if "hybrid" in lt:
        return "hybrid"
    text = "{} {}".format(employment_type or "", title or "").lower()
    if "part" in text and "time" in text:
        return "part_time"
    if "remote" in text or "work from home" in text:
        return "remote"
    if "hybrid" in text:
        return "hybrid"
    return "full_time"


# Category / scope decisions belong exclusively to the shared
# classification.classify_job (title x5 / skills x2 / description x1
# scoring + negative-keyword veto). The old per-scraper title regexes and
# the doctors/nurses/pharmacists/non_clinical enum are retired.


def classification_skills_signal(industry_type, functional_area):
    """Join the detail API's curated IndustryType/FunctionalArea fields into
    the `skills` signal for classify_job (empty without --enrich). They stay
    raw source columns in the rich CSV and never decide the category."""
    return ", ".join(s for s in ((industry_type or "").strip(),
                                 (functional_area or "").strip()) if s)


_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|life ?science|"
    r"diagnostic|clinical research|medical devices?", re.IGNORECASE)


def classify_company_type(name, industry_type=""):
    """club company_type enum: hospital | pharma (default hospital)."""
    if _PHARMA_COMPANY_RE.search(name or ""):
        return "pharma"
    if "pharma" in (industry_type or "").lower():
        return "pharma"
    return "hospital"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None):
    """First run: today - INITIAL_WINDOW_DAYS. Later: newest stored
    posted_date minus WATERMARK_GRACE_DAYS of overlap (dedup absorbs it)."""
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
    session = requests.Session(impersonate=IMPERSONATE)
    session.headers.update({"From": CONTACT, **API_HEADERS})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests_exceptions.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (SEARCH_PAGE_URL, SEARCH_URL, DETAIL_URL.format(job_id="0")):
        if not rp.can_fetch(SCRAPER_IDENTITY, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request_json(session, url, params=None):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests_exceptions.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def search_page(session, page, keyword):
    """Return (jobs, total_count) for one page (0-based), or (None, None)."""
    params = {
        "ClusterInd": CLUSTER_IND, "Experience": "", "Freshness": FRESHNESS,
        "Keywords": keyword, "KeywordsAr": "", "Limit": PAGE_SIZE,
        "Location": "", "LocationAr": "", "Offset": page * PAGE_SIZE,
        "SortPreference": SORT_PREFERENCE, "breadcrumb": 1,
        "clusterSelected": 1, "locationId": "", "nationality": "",
        "pageNo": page + 1, "seo": 1, "srchId": "",
    }
    data = _request_json(session, SEARCH_URL, params=params)
    if data is None:
        return (None, None)
    jobs = [item.get("Job", item) for item in (data.get("Jobs") or [])]
    try:
        total = int(data.get("TotalJobsCount"))
    except (TypeError, ValueError):
        total = None
    return (jobs, total)


def fetch_detail(session, job_id):
    """Return the detail-API Job dict for one job, or None on failure."""
    data = _request_json(session, DETAIL_URL.format(job_id=job_id))
    if not data:
        return None
    return data.get("Job") or None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def _exp_int(value):
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def job_to_rich_row(job, detail=None):
    """Build one rich-CSV row from a listing job (+ optional detail payload).

    Returns None when the shared classifier rules the job out of scope
    (the caller counts it as excluded_out_of_scope and never exports it).
    """
    detail = detail or {}
    title = (job.get("Designation") or "").strip()
    company = job.get("Company") or {}
    city, country = parse_location(job.get("Location"))
    code, dial = country_meta(country)
    experience = job.get("Experience") or {}

    other = detail.get("Other") or {}
    candidate = detail.get("DesiredCandidate") or {}
    detail_company = detail.get("Company") or {}
    salary_raw, sal_min, sal_max, sal_cur, sal_per = parse_salary(
        detail.get("Compensation"), other.get("currLabel"))

    description = strip_html(detail.get("Description") or "") or \
        (job.get("jobInfo") or "").strip()

    verdict = classify_job(
        title,
        classification_skills_signal(detail.get("IndustryType"),
                                     detail.get("FunctionalArea")),
        description)
    if not verdict["in_scope"]:
        return None

    return {
        "source": SITE,
        "job_id": str(job.get("JobId") or ""),
        "title": title,
        "company": (company.get("Name") or "").strip(),
        "company_id": str(company.get("Id") or ""),
        "city": city,
        "country": country,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": salary_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": sal_cur,
        "salary_period": sal_per,
        "experience_min_years": _exp_int(experience.get("Min")),
        "experience_max_years": _exp_int(experience.get("Max")),
        "job_type": map_job_type(detail.get("employmentType"),
                                 detail.get("locationType"), title),
        "location_type": (detail.get("locationType") or "").strip(),
        "category": verdict["category"],
        "sub_category": verdict["sub_category"],
        "role_family": verdict["role_family"],
        "all_families": verdict["all_families"],
        "family_scores": verdict["family_scores"],
        "family_confidence": verdict["family_confidence"],
        "matched_in": verdict["matched_in"],
        "needs_review": verdict["needs_review"],
        "company_type": classify_company_type(
            (company.get("Name") or ""), detail.get("IndustryType")),
        "industry_type": (detail.get("IndustryType") or "").strip(),
        "functional_area": (detail.get("FunctionalArea") or "").strip(),
        "education": (candidate.get("Education") or "").strip(),
        "vacancies": str(job.get("Vacancies") or ""),
        "company_logo": job.get("LogoUrl") or "",
        "company_about": strip_html(detail_company.get("Profile") or "")[:500],
        "posted_date": epoch_to_date(job.get("LatestPostedDate")),
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": (job.get("JdURL") or "").strip(),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the shared CLUB_COLUMNS schema (23 columns).

    The club salary_currency enum only allows INR/USD, so salary is exported
    only when the source currency matches AND the pay period is known;
    otherwise the columns stay empty (the rich CSV keeps the raw values).
    qualification is the source's structured Education field when present,
    else grounded extraction from the description — never inferred.
    """
    currency = _clean(r.get("salary_currency")).upper()
    period = _clean(r.get("salary_period"))
    lo, hi = _clean(r.get("salary_min")), _clean(r.get("salary_max"))
    exportable = bool(lo) and currency in ("INR", "USD") and period != ""
    return {
        "country_name": _clean(r.get("country")),
        "country_code": _clean(r.get("country_code")),
        "country_dial_code": _clean(r.get("country_dial_code")),
        "city_name": _clean(r.get("city")),
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": _clean(r.get("company_logo")),
        "company_about": _clean(r.get("company_about")),
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": _clean(r.get("job_type")) or "full_time",
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": _clean(r.get("experience_min_years")),
        "max_experience": _clean(r.get("experience_max_years")),
        "qualification": _clean(r.get("education")) or
                         extract_qualification(_clean(r.get("description"))),
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


def append_needs_review(entries):
    """Append flagged rows to needs_review.csv, deduped on job_id."""
    new_df = pd.DataFrame(entries, columns=NEEDS_REVIEW_COLUMNS, dtype=str)
    try:
        old = pd.read_csv(NEEDS_REVIEW_CSV, dtype=str, keep_default_na=False)
        new_df = pd.concat([old, new_df], ignore_index=True)
    except FileNotFoundError:
        pass
    new_df = new_df.reindex(columns=NEEDS_REVIEW_COLUMNS).fillna("")
    new_df = new_df.drop_duplicates(subset="job_id", keep="last")
    new_df.to_csv(NEEDS_REVIEW_CSV, index=False)
    return len(new_df)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from naukrigulf.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (for test runs)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each new job's detail page for description,"
                             " salary, employment type (1 extra request/job)")
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

    # One newest-first walk per keyword (--max-pages caps each walk;
    # --limit caps the TOTAL). JobId dedup absorbs cross-keyword overlap.
    for keyword in KEYWORD_QUERIES:
        if stop:
            break
        log.info("--- keyword: %s ---", keyword)
        page, total, empty_pages = 0, None, 0
        while not stop:
            if args.max_pages is not None and page >= args.max_pages:
                break
            if total is not None and page * PAGE_SIZE >= total:
                break
            jobs, total_count = search_page(session, page, keyword)
            if jobs is None:
                log.error("[%s] page %d failed after retries — stopping keyword",
                          keyword, page)
                break
            if total is None and total_count is not None:
                total = total_count
                log.info("[%s] API reports %d total jobs (~%d pages)",
                         keyword, total, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            if not jobs:
                empty_pages += 1
                if empty_pages >= MAX_EMPTY_PAGES:
                    break
                page += 1
                continue
            empty_pages = 0

            page_all_old = True
            for job in jobs:
                counters["scanned"] += 1
                try:
                    job_id = str(job.get("JobId") or "")
                    posted = epoch_to_date(job.get("LatestPostedDate"))
                    if posted and posted >= cutoff:
                        page_all_old = False
                    else:
                        counters["excluded_old"] += 1
                        continue
                    if job_id in known_ids:
                        counters["duplicates"] += 1
                        continue

                    detail = fetch_detail(session, job_id) if args.enrich else None
                    row = job_to_rich_row(job, detail)
                except Exception as exc:  # never let one job crash the run
                    log.warning("[%s] skipping malformed job on page %d: %s",
                                keyword, page, exc)
                    continue

                if row is None:  # classify_job ruled it out of scope
                    counters["excluded_out_of_scope"] += 1
                    continue
                if row["needs_review"]:
                    counters["needs_review"] += 1
                    review_log.append({
                        "job_id": row["job_id"], "title": row["title"],
                        "company": row["company"], "category": row["category"],
                        "sub_category": row["sub_category"],
                        "role_family": row["role_family"],
                        "matched_in": row["matched_in"]})
                known_ids.add(row["job_id"])
                new_rows.append(row)
                counters["new"] += 1
                if args.limit is not None and counters["new"] >= args.limit:
                    stop = True
                    break

            # Newest-first ordering (SortPreference=date): once an entire page
            # is older than the cutoff, everything after it is older too.
            if page_all_old and jobs:
                log.info("[%s] page %d entirely older than %s — next keyword",
                         keyword, page, cutoff)
                break
            page += 1

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
        n_review = append_needs_review(review_log)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, n_review)

    print("\n===== Run summary =====")
    print("Jobs scanned:            {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Excluded (out of scope): {:>5,}".format(counters["excluded_out_of_scope"]))
    print("Flagged needs_review:    {:>5,}".format(counters["needs_review"]))
    print("New jobs added:          {:>5,}".format(counters["new"]))
    print("Duplicates skipped:      {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
