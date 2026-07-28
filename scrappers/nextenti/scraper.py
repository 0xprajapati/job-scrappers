#!/usr/bin/env python3
"""Scrape healthcare job listings from nextenti.ai.

Data source
-----------
nextenti.ai is a client-rendered React SPA backed by a JSON microservice API.
The public job search needs a short-lived ANONYMOUS bearer token that the site
hands out with no credentials:

    GET  https://authenticator.nextenti.ai/token
         -> {"data": {"userId": "...", "accessToken": "gAAAA...(Fernet)"}}

    POST https://job-maintenance.nextenti.ai/api/search?page=N
         headers: Authorization: Bearer <accessToken>, userId: <userId>
         body:    {}                      (an empty filter object = all jobs)
         -> {"data": [ {job}, ... ], "totalCount": 792}

Pagination is a fixed 30 jobs/page (the `size` param is ignored); results are
sorted newest-first by `postDate`, which lets the incremental mode stop as soon
as it reaches jobs older than the watermark. Each job exposes: jobId, jobTitle,
organizationName, city, country, salaryRange ("66000 - 83000"), salaryType
(Monthly/Annual/None), experience ("2 - 5 years"), jobType, profession,
postDate (YYYY-MM-DD), jobDescription, organizationLogo, verifiedOrganization.

Outputs (per the repo README + master-scraper-spec.md)
-----------------------------------------------------
* nextenti_jobs.csv          — rich cumulative store (dedup key: job_id),
                               the source of truth used for the incremental
                               watermark. Never filtered on salary.
* ../../jobs_csv/<DD-MM-YYYY>/nextenti.csv
                             — the same jobs mapped to the shared
                               HealthCareers.club import schema (job_samples.csv).

Time window (master spec): first run keeps jobs posted in the last
INITIAL_WINDOW_DAYS; later runs keep only jobs newer than the newest postDate
already stored, minus WATERMARK_GRACE_DAYS of overlap (dedup absorbs it).

Run `python scraper.py --help` for options.
"""

import argparse
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

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "nextenti"
SITE_BASE = "https://nextenti.ai"
ROBOTS_URL = SITE_BASE + "/robots.txt"
TOKEN_URL = "https://authenticator.nextenti.ai/token"
SEARCH_URL = "https://job-maintenance.nextenti.ai/api/search"
# The listing endpoint truncates jobDescription to 250 chars; the detail
# endpoint returns the full text (and cleaner experienceMin/Max integers).
DETAIL_URL = "https://job-maintenance.nextenti.ai/job-details"
SEARCH_PAGE_URL = "https://nextenti.ai/search-jobs"  # the crawl target robots-wise

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries are captured, never filtered on.
# First run keeps the last INITIAL_WINDOW_DAYS; later runs keep only jobs newer
# than the newest stored postDate minus WATERMARK_GRACE_DAYS of overlap.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 30                 # server-fixed page size (informational)
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "nextenti_jobs.csv"
# jobs_csv/ lives at the repo root, two levels up from scrappers/nextenti/.
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Rich (source-of-truth) columns — superset, keeps everything the API gives.
RICH_COLUMNS = [
    "source", "job_id", "title", "company", "profession", "city", "country",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "experience_raw", "experience_min_years", "experience_max_years",
    "job_type", "category", "company_type", "verified_organization",
    "company_logo", "posted_date", "description", "job_url", "scraped_at",
]

# Shared HealthCareers.club import schema (must match job_samples.csv exactly).
CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

# India-only board; map country name -> (ISO code, dial code).
COUNTRY_META = {"india": ("IN", "+91")}

log = logging.getLogger("nextenti_scraper")

# ----------------------------------------------------------------------------
# Field parsing / classification
# ----------------------------------------------------------------------------

_RANGE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def parse_range(text):
    """Parse "66000 - 83000" or "2 - 5 years" -> (min, max) floats, or (None, None)."""
    if not text:
        return (None, None)
    nums = [float(n.replace(",", "")) for n in _RANGE_RE.findall(str(text))]
    if not nums:
        return (None, None)
    lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)


def parse_salary(salary_range):
    """Return (min_int, max_int) INR from a salaryRange string, or (None, None).

    Salaries are captured, never filtered on. A 0/blank range is left empty.
    """
    lo, hi = parse_range(salary_range)
    if lo is None or (lo == 0 and hi == 0):
        return (None, None)
    return (int(round(lo)), int(round(hi)))


def salary_period(salary_type):
    """Map nextenti salaryType -> club salary_period enum, or ""."""
    st = (salary_type or "").strip().lower()
    if st in ("monthly", "month", "per month"):
        return "per_month"
    if st in ("annual", "yearly", "annually", "per annum", "year"):
        return "per_annum"
    return ""


def map_job_type(job_type):
    """Map nextenti jobType -> club job_type enum."""
    jt = (job_type or "").strip().lower()
    if "part" in jt:
        return "part_time"
    if "intern" in jt:
        return "part_time"
    if jt in ("remote", "work from home"):
        return "remote"
    if "hybrid" in jt:
        return "hybrid"
    return "full_time"  # default (the board is overwhelmingly Full Time)


# profession is a source-provided field — the primary category signal.
_DOCTOR_PROF = {
    "doctor", "medical doctor", "general physician", "mbbs", "dentist",
    "surgeon", "consultant", "physician", "medical officer", "radiologist",
}
_NURSE_PROF = {"nurse", "nursing", "gnm", "anm", "midwife"}
_PHARM_PROF = {"pharmacist", "pharmacy"}

# Title fallbacks when profession is generic ("Others", "trainee", blank).
_DOCTOR_TITLE_RE = re.compile(
    r"\b(doctor|physician|surgeon|mbbs|bds|dentist|medical officer|rmo|"
    r"consultant|[a-z]+ologist|intensivist|anaesthet|anesthet|physician)\b",
    re.IGNORECASE)
_NURSE_TITLE_RE = re.compile(r"\b(nurse|nursing|gnm|anm|midwife)\b", re.IGNORECASE)
_PHARM_TITLE_RE = re.compile(r"\b(pharmacist|pharmacy|pharm\.?d)\b", re.IGNORECASE)


# Professions that map cleanly to non_clinical — these are authoritative and
# must NOT be overridden by the title fallback (e.g. "Speech Language
# Pathologist" is allied health despite the "-ologist" in the title).
_NONCLINICAL_PROF_KEYWORDS = (
    "physiotherapy", "therapist", "technician", "paramedical", "dietit",
    "dietician", "audiolog", "human resources", "hr", "marketing", "sales",
    "management", "administrator", "finance", "account", "engineering",
    "maintanence", "maintenance", "facility", "research", "analytics",
    "call center", "pr &", "speech")

# Generic profession values that carry no signal — fall back to the title.
_GENERIC_PROF = {"", "others", "other", "trainee", "general"}


def classify_category(profession, title):
    """Map to the club category enum: doctors | nurses | pharmacists | non_clinical.

    Precedence: the source `profession` field is authoritative. Only when it is
    generic/blank do we consult the title. Returns (category, needs_review);
    needs_review flags jobs that resolved to non_clinical purely by default so
    the mapping can be refined — nothing is dropped (the board is healthcare).
    """
    prof = (profession or "").strip().lower()
    if prof in _NURSE_PROF or "nurse" in prof:
        return ("nurses", False)
    if prof in _PHARM_PROF or "pharmac" in prof:
        return ("pharmacists", False)
    if prof in _DOCTOR_PROF or any(w in prof for w in ("doctor", "physician", "surgeon", "dentist")):
        return ("doctors", False)
    # Authoritative non-clinical profession — do not let the title override it.
    if any(w in prof for w in _NONCLINICAL_PROF_KEYWORDS):
        return ("non_clinical", False)

    # profession is generic/unknown — consult the title.
    if _NURSE_TITLE_RE.search(title or ""):
        return ("nurses", False)
    if _PHARM_TITLE_RE.search(title or ""):
        return ("pharmacists", False)
    if _DOCTOR_TITLE_RE.search(title or ""):
        return ("doctors", False)
    return ("non_clinical", True)  # pure default -> flag for review


_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|life ?science|"
    r"diagnostic|clinical research", re.IGNORECASE)


def classify_company_type(name):
    """club company_type enum: hospital | pharma (default hospital)."""
    return "pharma" if _PHARMA_RE.search(name or "") else "hospital"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text):
    text = (text or "").lower().replace("&", " and ")
    return _SLUG_RE.sub("-", text).strip("-")


def build_job_url(job):
    """Reconstruct the public detail URL (slug--jobId), matching the site's format."""
    parts = "-".join(p for p in (
        slugify(job.get("organizationName")),
        slugify(job.get("jobTitle")),
        slugify(job.get("city")),
    ) if p)
    return "{}/jobs/{}--{}".format(SITE_BASE, parts, job.get("jobId"))


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Origin": SITE_BASE,
                            "Referer": SITE_BASE + "/"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    if not rp.can_fetch(USER_AGENT, SEARCH_PAGE_URL):
        sys.exit("robots.txt disallows {} — aborting.".format(SEARCH_PAGE_URL))
    log.info("robots.txt check passed")


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def get_anonymous_token(session):
    """Mint a fresh anonymous bearer token; returns (accessToken, userId)."""
    data = _request(session, "GET", TOKEN_URL)
    if not data or "data" not in data:
        sys.exit("Could not obtain an anonymous token from nextenti — aborting.")
    tok = data["data"]
    return tok["accessToken"], tok["userId"]


def search_page(session, token, user_id, page):
    """Return (jobs, total_count) for one page, or (None, None) on failure."""
    headers = {"Authorization": "Bearer " + token, "userId": user_id,
               "Content-Type": "application/json"}
    data = _request(session, "POST", "{}?page={}".format(SEARCH_URL, page),
                    headers=headers, data="{}")
    if data is None:
        return (None, None)
    return (data.get("data") or [], data.get("totalCount"))


def get_job_detail(session, token, user_id, job_id):
    """Fetch the full job-detail record (untruncated description), or None."""
    headers = {"Authorization": "Bearer " + token, "userId": user_id}
    data = _request(session, "GET", "{}?jobId={}".format(DETAIL_URL, job_id),
                    headers=headers)
    if not data:
        return None
    return data.get("data") or None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    title = (job.get("jobTitle") or "").strip()
    company = (job.get("organizationName") or "").strip()
    profession = (job.get("profession") or "").strip()
    sal_min, sal_max = parse_salary(job.get("salaryRange"))
    exp_min, exp_max = parse_range(job.get("experience"))
    category, needs_review = classify_category(profession, title)
    description = re.sub(r"\s+", " ", job.get("jobDescription") or "").strip()

    return {
        "source": SITE,
        "job_id": str(job.get("jobId") or ""),
        "title": title,
        "company": company,
        "profession": profession,
        "city": (job.get("city") or "").strip(),
        "country": (job.get("country") or "").strip(),
        "salary_raw": job.get("salaryRange") or "",
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_period": salary_period(job.get("salaryType")),
        "experience_raw": job.get("experience") or "",
        "experience_min_years": int(exp_min) if exp_min is not None else "",
        "experience_max_years": int(exp_max) if exp_max is not None else "",
        "job_type": map_job_type(job.get("jobType")),
        "category": category,
        "company_type": classify_company_type(company),
        "verified_organization": bool(job.get("verifiedOrganization")),
        "company_logo": job.get("organizationLogo") or "",
        "posted_date": (job.get("postDate") or "")[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": build_job_url(job),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "needs_review": needs_review,
    }


def apply_detail(row, detail):
    """Overlay full-description detail-endpoint fields onto a listing row.

    The listing truncates jobDescription to 250 chars; the detail record has
    the full text plus cleaner integer experience bounds. Only non-empty
    detail values override (the listing stays the fallback).
    """
    full_desc = re.sub(r"\s+", " ", detail.get("jobDescription") or "").strip()
    if len(full_desc) > len(row.get("description") or ""):
        row["description"] = full_desc[:DESCRIPTION_MAX_CHARS]
    exp_min, exp_max = detail.get("experienceMin"), detail.get("experienceMax")
    if exp_min is not None:
        row["experience_min_years"] = int(exp_min)
    if exp_max is not None:
        row["experience_max_years"] = int(exp_max)
    return row


def _int_str(value):
    """Clean integer string for the club CSV, or "" — avoids "66000.0"."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, float) and pd.isna(value):
            return ""
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def rich_row_to_club_row(r):
    country = (r.get("country") or "India").strip() or "India"
    code, dial = COUNTRY_META.get(country.lower(), ("", ""))
    min_salary = _int_str(r.get("salary_min"))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": r.get("city", ""),
        "company_name": r.get("company", ""),
        "company_type": r.get("company_type", "hospital"),
        "company_logo": r.get("company_logo", ""),
        "company_about": "",
        "title": r.get("title", ""),
        "description": r.get("description", ""),
        "job_type": r.get("job_type", "full_time"),
        "category": r.get("category", "non_clinical"),
        "application_url": r.get("job_url", ""),
        "posted_at": r.get("posted_date", ""),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        "min_salary": min_salary,
        "max_salary": _int_str(r.get("salary_max")),
        "salary_period": (r.get("salary_period", "") or "") if min_salary else "",
        "salary_currency": "INR" if min_salary else "",
        "is_active": "true",
        "expires_at": "",
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """First run: today - INITIAL_WINDOW_DAYS. Later: newest stored postDate
    minus WATERMARK_GRACE_DAYS of overlap."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


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
        description="Scrape healthcare jobs from nextenti.ai.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N pages (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip per-job detail fetches; keep the listing's "
                             "250-char truncated description (faster)")
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

    token, user_id = get_anonymous_token(session)
    log.info("Obtained anonymous token (userId %s...)", user_id[:8])

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    page, total = 0, None

    while True:
        if args.max_pages is not None and page >= args.max_pages:
            break
        jobs, total_count = search_page(session, token, user_id, page)
        if jobs is None:
            log.error("Page %d failed after retries — stopping", page)
            break
        if total is None and total_count is not None:
            total = total_count
            log.info("API reports %d total jobs (~%d pages)",
                     total, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if not jobs:
            break

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job on page %d: %s", page, exc)
                continue
            if row["posted_date"] and row["posted_date"] >= cutoff:
                page_all_old = False
            else:
                counters["excluded_old"] += 1
                continue
            if row.pop("needs_review", False):
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "profession": row["profession"]})
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            # Full description (+ cleaner experience) for NEW jobs only, so
            # daily incremental runs stay cheap.
            if not args.no_details:
                detail = get_job_detail(session, token, user_id, row["job_id"])
                if detail:
                    apply_detail(row, detail)
                else:
                    counters["detail_failed"] += 1
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        # Newest-first ordering: once an entire page is older than the cutoff,
        # everything after it is older too.
        if page_all_old and jobs:
            log.info("Page %d entirely older than %s — stopping", page, cutoff)
            break
        page += 1

    # ---- write rich cumulative CSV (source of truth) ----
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[[c for c in RICH_COLUMNS if c in combined.columns]]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- always (re)write the club-schema CSV for this run date ----
    rich_csv_path = Path(args.output).resolve()
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)
        club_csv_path = target.resolve()
    else:
        club_csv_path = None

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))
    print("Rich CSV stored at:    {}".format(rich_csv_path.as_uri()))
    if club_csv_path is not None:
        print("Club CSV stored at:    {}".format(club_csv_path.as_uri()))


if __name__ == "__main__":
    main()
