#!/usr/bin/env python3
"""Scrape healthcare job listings from simplyhired.co.in (India).

Data source
-----------
simplyhired.co.in is a Next.js app (Indeed family). Every SERP HTML page
embeds the full result set as JSON in <script id="__NEXT_DATA__">:

    GET https://www.simplyhired.co.in/search?q=healthcare&l=india&s=d&t=15
        [&cursor=<token>]
    -> props.pageProps: {jobs: [...20 jobs...], resultCount,
                         pageCursors: {"2": "...", ...}, currentPageNumber}

Query params: q = keyword, l = location, s=d = sort newest-first,
t=15 = posted within the last 15 days (server-side date filter — arbitrary
day counts are accepted). Pagination is cursor-based: each response's
pageCursors maps upcoming page numbers to opaque tokens; page N+1 is fetched
with &cursor=<token>. Listing job fields: jobKey, title, company, location
("[Area, ]City, State" or just "State"), salaryInfo ("₹18,000 - ₹25,000 a
month", "From ₹18,00,000 a year", or null), dateOnIndeed (epoch millis),
jobTypes (["Full-time", ...]), remoteAttributes, snippet, sponsored, botUrl.

IMPORTANT — sponsored interleaving: with s=d the ORGANIC results are sorted
newest-first, but sponsored cards (often weeks old) are mixed in anywhere.
The old-page early stop therefore only considers non-sponsored jobs.

Detail pages (used by --enrich, one request per new job):

    GET https://www.simplyhired.co.in/job/<jobKey>
    -> __NEXT_DATA__ props.pageProps: jobDescriptionHtml, formattedLocation,
       jobTypes, workSettings, compensation, datePublished, qualifications,
       benefits, employerName, employerSquareLogoUrl

robots.txt (checked at startup): /search and /job/<key> are allowed for
User-agent: *; /serp, /job-id/, /a/job-details/, /c/jobs-api/ and the
/out?r= apply redirects are disallowed and never touched — application_url
is the public /job/<jobKey> page.

Like naukrigulf, fetching needs a real-browser TLS fingerprint, so requests
go through curl_cffi with Chrome impersonation.

Salary (master spec §3: capture, don't filter)
----------------------------------------------
Salaries are INR with an explicit period ("a month" / "a year"), so unlike
the Gulf boards they CAN be exported to the club CSV. "a day"/"an hour"
periods can't be represented by the club enum and stay rich-CSV-only.
Missing salary -> "Not Disclosed", numeric fields empty. Never invented.

Outputs (per the repo README + master-scraper-spec.md)
------------------------------------------------------
* simplyhired_jobs.csv       — rich cumulative store (dedup key: job_id),
                               source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/simplyhired.csv
                             — the same jobs mapped to the shared
                               HealthCareers.club schema (job_samples.csv).

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(15, matching the t=15 source filter); later runs keep only jobs newer than
the newest stored posted_date minus WATERMARK_GRACE_DAYS of overlap.

Run `python simplyhired_scraper.py --help` for options.
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

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "simplyhired"
SITE_BASE = "https://www.simplyhired.co.in"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SEARCH_URL = SITE_BASE + "/search"
JOB_URL = SITE_BASE + "/job/{job_key}"

SCRAPER_IDENTITY = "HealthCareersJobScraper/1.0"
CONTACT = "https://github.com/0xprajapati/job-scrappers"
IMPERSONATE = "chrome"  # Indeed-family bot protection wants browser TLS

# Search filters (agreed): healthcare keyword, all-India, newest-first,
# posted within the last 15 days (server-side date filter).
SEARCH_PARAMS = {"q": "healthcare", "l": "india", "s": "d", "t": "15"}

INITIAL_WINDOW_DAYS = 15       # matches the t=15 source filter
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 20                 # server-fixed (informational)
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = str(Path(__file__).resolve().parent / "simplyhired_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "location", "city", "state",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "job_type", "job_types_raw", "remote_attributes", "category",
    "company_type", "sponsored", "qualifications", "benefits",
    "company_logo", "posted_date", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("simplyhired_scraper")

# ----------------------------------------------------------------------------
# Field parsing / classification
# ----------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def extract_page_props(html):
    """Pull props.pageProps out of a Next.js page's __NEXT_DATA__ script."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def parse_city(location):
    """City from "[Area, ]City, State" (city = second-to-last part) or the
    bare state/region string ("Goa") when that's all the listing gives."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if not parts:
        return ""
    return parts[-2] if len(parts) >= 2 else parts[0]


def epoch_ms_to_date(epoch_ms):
    """Epoch milliseconds (int/str) -> "YYYY-MM-DD" (UTC), or ""."""
    try:
        return datetime.fromtimestamp(
            int(epoch_ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d)[^>]*>", " ", text or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_salary(salary_info):
    """Parse simplyhired salaryInfo strings.

    "₹18,000 - ₹25,000 a month"     -> (raw, 18000, 25000, per_month)
    "From ₹18,00,000 a year"        -> (raw, 1800000, 1800000, per_annum)
    "Up to ₹350.75 an hour"         -> (raw, "", "", "")   period unsupported
    None / ""                       -> ("Not Disclosed", "", "", "")

    Salaries are captured, never filtered on; nothing is invented.
    """
    raw = (salary_info or "").strip()
    if not raw:
        return ("Not Disclosed", "", "", "")
    nums = [float(n.replace(",", "")) for n in _NUM_RE.findall(raw)]
    if not nums:
        return (raw, "", "", "")
    lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
    if hi < lo:
        lo, hi = hi, lo
    text = raw.lower()
    if "month" in text:
        period = "per_month"
    elif "year" in text or "annum" in text:
        period = "per_annum"
    else:
        return (raw, "", "", "")  # day/hour/week — club enum can't hold it
    return (raw, str(int(round(lo))), str(int(round(hi))), period)


def map_job_type(job_types, remote_attributes=(), work_settings=()):
    """Listing jobTypes + remote/work-setting hints -> club job_type enum."""
    remote_text = " ".join(list(remote_attributes or []) +
                           list(work_settings or [])).lower()
    if "hybrid" in remote_text:
        return "hybrid"
    if "remote" in remote_text or "work from home" in remote_text:
        return "remote"
    types = [t.strip().lower() for t in (job_types or [])]
    if any("full" in t for t in types):
        return "full_time"
    if any("part" in t for t in types):
        return "part_time"
    if any(t in ("internship", "freelance", "contractual / temporary",
                 "temporary", "student job", "volunteer") for t in types):
        return "part_time"
    return "full_time"


# Title -> club category. Same conventions as the naukrigulf scraper:
# unmatched titles are KEPT as non_clinical and flagged needs_review.
_NURSE_RE = re.compile(r"\b(nurse|nursing|midwif\w*|gnm|anm|sister)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\b(pharmacist|pharmacy|pharm\.?\s?d|b\.? ?pharma?|d\.? ?pharma?)\b", re.IGNORECASE)
_ALLIED_RE = re.compile(
    r"\b(audiolog\w*|physiotherap\w*|radiograph\w*|optometr\w*|paramedic\w*|"
    r"speech|lab ?technician|phlebotom\w*|dental hygien\w*|dialysis technician)\b",
    re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|mbbs|bhms|bams|bums|md|dentist|bds|mds|"
    r"medical officer|rmo|duty doctor|gp|[a-z]+ologist|[a-z]{4,}ology|"
    r"orthop[ae]?edic\w*|intensivist|hospitalist|anaesthetist|anesthetist|"
    r"an[ae]sthesiolog\w*|obstetric\w*|p[ae]?diatric\w*|p[ae]?ediatric\w*|"
    r"psychiatrist|neonat\w*|general practitioner|veterinar\w*|ayurved\w*|"
    r"hom[oe]{1,2}opath\w*|(family|internal|general|emergency) medicine|"
    r"medical director|medical superintendent|registrar|consultant physician)\b",
    re.IGNORECASE)
_NONCLINICAL_RE = re.compile(
    r"\b(accountant|accounts?|finance|sales|marketing|receptionist|driver|"
    r"secretary|hr\b|human resources|admin\w*|technician|technologist|"
    r"therapist|dietician|dietitian|nutritionist|coordinator|executive|"
    r"manager|officer|engineer|analyst|assistant|biller|billing|coder|coding|"
    r"insurance|housekeeping|security|store ?keeper|procurement|liaison|"
    r"counsel(l)?or|telecaller|caretaker|warden|attendant|trainer|tutor|"
    r"faculty|professor|lecturer|data entry|back office|front office|"
    r"operations|supervisor|developer|designer|writer|analytics|scientist)\b",
    re.IGNORECASE)


def classify_category(title):
    """Return (category, needs_review) for a job title."""
    title = title or ""
    if _NURSE_RE.search(title):
        return ("nurses", False)
    if _PHARM_RE.search(title):
        return ("pharmacists", False)
    if _ALLIED_RE.search(title):
        return ("non_clinical", False)
    if _DOCTOR_RE.search(title):
        return ("doctors", False)
    if _NONCLINICAL_RE.search(title):
        return ("non_clinical", False)
    return ("non_clinical", True)


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
    session = requests.Session(impersonate=IMPERSONATE)
    session.headers.update({"From": CONTACT})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests_exceptions.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (SEARCH_URL + "?q=healthcare", JOB_URL.format(job_key="x")):
        if not rp.can_fetch(SCRAPER_IDENTITY, url):
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
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
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


def search_page(session, cursor=None):
    """Fetch one SERP page. Returns (jobs, page_cursors, result_count) or
    (None, None, None) on failure."""
    params = dict(SEARCH_PARAMS)
    if cursor:
        params["cursor"] = cursor
    html = _get_html(session, SEARCH_URL, params=params)
    props = extract_page_props(html) if html else None
    if props is None:
        return (None, None, None)
    return (props.get("jobs") or [], props.get("pageCursors") or {},
            props.get("resultCount"))


def fetch_detail(session, job_key):
    """Fetch a /job/<key> page's pageProps for --enrich, or None."""
    html = _get_html(session, JOB_URL.format(job_key=job_key))
    return extract_page_props(html) if html else None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job, detail=None):
    detail = detail or {}
    title = (job.get("title") or "").strip()
    location = (detail.get("formattedLocation") or job.get("location") or "").strip()
    salary_raw, sal_min, sal_max, sal_per = parse_salary(
        detail.get("compensation") or job.get("salaryInfo"))
    category, needs_review = classify_category(title)
    job_types = detail.get("jobTypes") or job.get("jobTypes") or []

    description = strip_html(detail.get("jobDescriptionHtml") or "") or \
        (job.get("snippet") or "").strip()

    return {
        "source": SITE,
        "job_id": str(job.get("jobKey") or ""),
        "title": title,
        "company": (detail.get("employerName") or job.get("company") or "").strip(),
        "location": location,
        "city": parse_city(location),
        "state": (detail.get("state") or "").strip(),
        "salary_raw": salary_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_period": sal_per,
        "job_type": map_job_type(job_types, job.get("remoteAttributes"),
                                 detail.get("workSettings")),
        "job_types_raw": "|".join(job_types),
        "remote_attributes": "|".join(job.get("remoteAttributes") or []),
        "category": category,
        "company_type": classify_company_type(
            detail.get("employerName") or job.get("company")),
        "sponsored": bool(job.get("sponsored")),
        "qualifications": "|".join(detail.get("qualifications") or
                                   job.get("requirements") or []),
        "benefits": "|".join(detail.get("benefits") or job.get("benefits") or []),
        "company_logo": detail.get("employerSquareLogoUrl") or "",
        "posted_date": epoch_ms_to_date(detail.get("datePublished") or
                                        job.get("dateOnIndeed")),
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": SITE_BASE + "/job/" + str(job.get("jobKey") or ""),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "needs_review": needs_review,
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    lo, hi = _clean(r.get("salary_min")), _clean(r.get("salary_max"))
    period = _clean(r.get("salary_period"))
    exportable = bool(lo) and period in ("per_month", "per_annum")
    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        "city_name": _clean(r.get("city")),
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": _clean(r.get("company_logo")),
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": _clean(r.get("job_type")) or "full_time",
        "category": _clean(r.get("category")) or "non_clinical",
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        "min_salary": lo if exportable else "",
        "max_salary": (hi or lo) if exportable else "",
        "salary_period": period if exportable else "",
        "salary_currency": "INR" if exportable else "",
        "is_active": "true",
        "expires_at": "",
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
        description="Scrape healthcare jobs from simplyhired.co.in.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (for test runs)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each new job's detail page for the full"
                             " description (1 extra request/job)")
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

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0}
    new_rows, review_log = [], []
    page_no, cursor, cursors = 1, None, {}
    total, empty_pages, stop = None, 0, False

    while not stop:
        if args.max_pages is not None and page_no > args.max_pages:
            break
        jobs, page_cursors, result_count = search_page(session, cursor)
        if jobs is None:
            log.error("Page %d failed after retries — stopping", page_no)
            break
        if total is None and result_count is not None:
            total = result_count
            log.info("Site reports %d matching jobs (~%d pages)",
                     total, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        cursors.update(page_cursors or {})
        if not jobs:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            page_no += 1
            cursor = cursors.get(str(page_no))
            if cursor is None:
                break
            continue
        empty_pages = 0

        organic_all_old, organic_seen = True, False
        for job in jobs:
            counters["scanned"] += 1
            try:
                job_id = str(job.get("jobKey") or "")
                posted = epoch_ms_to_date(job.get("dateOnIndeed"))
                is_recent = bool(posted) and posted >= cutoff
                if not job.get("sponsored"):
                    organic_seen = True
                    if is_recent:
                        organic_all_old = False
                if not is_recent:
                    counters["excluded_old"] += 1
                    continue
                if job_id in known_ids:
                    counters["duplicates"] += 1
                    continue

                detail = fetch_detail(session, job_id) if args.enrich else None
                row = job_to_rich_row(job, detail)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job on page %d: %s", page_no, exc)
                continue

            if row.pop("needs_review", False):
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "company": row["company"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1
            if args.limit is not None and counters["new"] >= args.limit:
                stop = True
                break

        # Organic results are newest-first; sponsored cards are interleaved
        # with arbitrary dates, so only organic jobs vote on the early stop.
        if organic_seen and organic_all_old:
            log.info("Page %d organic results all older than %s — stopping",
                     page_no, cutoff)
            break

        page_no += 1
        cursor = cursors.get(str(page_no))
        if cursor is None:
            log.info("No cursor for page %d — end of results", page_no)
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
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:    {:>5,}".format(counters["needs_review"]))
    print("New jobs added:          {:>5,}".format(counters["new"]))
    print("Duplicates skipped:      {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
