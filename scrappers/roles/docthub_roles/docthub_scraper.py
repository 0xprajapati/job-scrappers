#!/usr/bin/env python3
"""Scrape healthcare job listings from jobs.docthub.com into a deduplicated CSV.

Data source
-----------
DoctHub's job board is a Next.js app backed by a public JSON API:

    GET https://api.docthub.com/jobcenter/jobs?pageNumber=N&pageSize=M[&categories=ID]

The response contains structured job objects (numeric salary with a
Monthly/Yearly type, exact publish timestamps, org + location, and a
`code` slug ending in the job ID, e.g. "dermatologist-J120239").
`isFacet=true` additionally returns facet counts, including the 18 job
categories which exactly partition the full job set. We therefore crawl
per category, which both tags every job with its category and lets us
skip clearly non-clinical categories entirely.

Run `python docthub_scraper.py --help` for options.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
import role_families as RF
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

API_BASE = "https://api.docthub.com/jobcenter"
JOBS_ENDPOINT = API_BASE + "/jobs"
SITE_BASE = "https://jobs.docthub.com"
ROBOTS_URLS = [SITE_BASE + "/robots.txt", "https://api.docthub.com/robots.txt"]

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (personal research; contact: eleswarapu.madhav@gmail.com)"
)

# Time window (no salary filter — salaries are captured but never filtered on).
# First run keeps jobs posted in the last INITIAL_WINDOW_DAYS; later runs keep
# only jobs newer than the newest posted_date already in the CSV, minus
# WATERMARK_GRACE_DAYS of overlap (dedup absorbs the overlap).
# First-run window, narrowed from the master spec §4 default of 7 to 2:
# these feeds post fast, so every extra day of first-run window costs a lot of
# crawl for jobs that are already stale by import time. Later runs ignore this
# entirely and use the watermark.
INITIAL_WINDOW_DAYS = 2
WATERMARK_GRACE_DAYS = 2

# Categories (as named in the API facets) that are always healthcare —
# every job in them is kept without looking at the title.
# Docthub's own facets are the source-side filter (master spec §1). The
# original scraper crawled the bedside categories (Doctor, Nursing, ...) and
# so found almost none of these roles; the clinical-research scope lives in
# these two instead. Facet counts on 2026-08-24: Pharmaceuticals 2,560,
# Clinical Research/ Data Science 80.
# Docthub's facets are the source-side filter (master spec §1), but only ONE
# of its 18 categories carries these roles.
#
# Measured 2026-08-24: the "Pharmaceuticals" facet (2,560 jobs) is pharma
# MANUFACTURING — Production, QA/QC, IPQA, HPLC, GLP, PPMC. A 40-job sample
# scored ZERO of the eleven families, so crawling it is 2,560 wasted requests.
# "Clinical Research/ Data Science" (80 jobs) yields ~11 in-scope roles, and
# that is docthub's entire realistic contribution to this scope.
INCLUDE_CATEGORIES = {
    "Clinical Research/ Data Science",
}

# Only INCLUDE_CATEGORIES are crawled. The original scraper also crawled any
# unknown category and classified by title; with a scope this narrow that is
# thousands of requests for nothing.
CRAWL_ONLY_INCLUDED = True

# Categories that are never healthcare — not even crawled (saves requests).
EXCLUDE_CATEGORIES = {
    "Marketing / Business Development",
    "Administration / Management",
    "Human Resource (HR)",
    "Engineering / Maintenance",
    "Housekeeping Department",
}

# Any category in neither list (e.g. "Pharmaceuticals", "Others",
# "Professor / Academic Staff", "Clinical Research/ Data Science",
# "Counsellor") is crawled and each job is classified by its TITLE using the
# ALLOW/DENY keyword lists below. Titles matching neither list are kept but
# flagged needs_review=True and logged to the needs-review CSV.

# Title phrases that mark a job as NON-healthcare. Checked first; also acts
# as a safety net inside INCLUDE_CATEGORIES. Matched case-insensitively on
# word boundaries.
DENY_TITLE_KEYWORDS = [
    "software developer", "software engineer", "web developer", "full stack",
    "front end", "frontend", "back end", "backend", "mobile app",
    "computer science", "data engineer", "devops",
    "mechanical engineer", "civil engineer", "electrical engineer",
    "maintenance engineer", "network engineer", "hardware",
    "accountant", "accounts executive", "chartered accountant", "cashier",
    "graphic designer", "ui designer", "ux designer", "video editor",
    "digital marketing", "marketing executive", "marketing manager",
    "sales executive", "sales manager", "business development",
    "telecaller", "tele caller", "receptionist", "front office", "front desk",
    "human resource", "hr executive", "hr manager", "recruiter",
    "housekeeping", "security guard", "security supervisor", "driver",
    "electrician", "plumber", "cook", "chef", "store keeper", "storekeeper",
    "purchase executive", "billing executive", "data entry",
]

# Title keywords that mark a job as healthcare / clinical / allied health.
ALLOW_TITLE_KEYWORDS = [
    "nurse", "nursing", "gnm", "anm",
    "doctor", "physician", "surgeon", "medical officer", "rmo", "mbbs",
    "consultant", "specialist", "registrar", "intensivist", "hospitalist",
    "anesthesiologist", "anaesthesiologist", "anesthetist", "anaesthetist",
    "cardiologist", "neurologist", "nephrologist", "urologist", "oncologist",
    "radiologist", "pathologist", "microbiologist", "biochemist",
    "gynecologist", "gynaecologist", "obstetrician", "pediatrician",
    "paediatrician", "neonatologist", "psychiatrist", "dermatologist",
    "ophthalmologist", "ent ", "orthopedic", "orthopaedic", "physiatrist",
    "pulmonologist", "gastroenterologist", "endocrinologist", "hematologist",
    "haematologist", "immunologist", "rheumatologist", "dietician",
    "dietitian", "nutritionist", "psychologist", "counsellor", "counselor",
    "pharmacist", "pharmacy", "pharmacologist", "pharmacovigilance",
    "dental", "dentist", "orthodontist", "endodontist", "periodontist",
    "prosthodontist", "hygienist",
    "physiotherapist", "physiotherapy", "occupational therapist",
    "speech therapist", "audiologist", "therapist", "rehabilitation",
    "lab technician", "laboratory", "technologist", "technician",
    "radiographer", "radiography", "sonographer", "ultrasound", "x-ray",
    "xray", "mri", "ct scan", "cath lab", "dialysis", "phlebotomist",
    "phlebotomy", "optometrist", "optician", "perfusionist", "audiometrist",
    "paramedic", "emt", "emergency medical",
    "medical superintendent", "medical director", "medical", "clinical",
    "icu", "ot ", "operation theatre", "operation theater", "ward",
    "opd", "ipd", "casualty", "emergency",
    "biomedical", "microbiology", "pathology", "radiology", "anatomy",
    "physiology", "midwife", "midwifery", "vaccinator", "health",
    "hospital", "ayurved", "homeopath", "homoeopath", "unani", "siddha",
    "yoga", "naturopath", "veterinary", "vet ",
]

PAGE_SIZE = 100                # jobs per API request (100 verified working)
REQUEST_DELAY_SECONDS = 1.0    # pause between successive API requests
REQUEST_TIMEOUT_SECONDS = 60   # per-request timeout (server can be slow)
MAX_RETRIES = 4                # retries per request, exponential backoff
BACKOFF_BASE_SECONDS = 3.0     # 3s, 6s, 12s, 24s

DEFAULT_OUTPUT_CSV = "docthub_roles_jobs.csv"
NEEDS_REVIEW_CSV = "needs_review.csv"

CSV_COLUMNS = [
    "job_id", "title", "company", "location",
    "experience_min_years", "experience_max_years",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "category",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "needs_review", "posted_date", "job_url", "scraped_at",
]

log = logging.getLogger("docthub_scraper")

# ----------------------------------------------------------------------------
# Salary parsing / normalization
# ----------------------------------------------------------------------------

_SALARY_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(K|L|LAKH|LAC)?", re.IGNORECASE)
_NON_DISCLOSED_RE = re.compile(r"not\s*disclosed|negotiable|confidential", re.IGNORECASE)


def parse_salary_string(raw):
    """Parse a display salary string like "18K - 40K P.M" or "8.00L - 10.00L P.A".

    Returns (min_monthly, max_monthly, period) with integer INR/month amounts
    and period "P.M" or "P.A", or None if missing/unparseable/not disclosed.
    K = 1,000; L = 100,000. P.A amounts are divided by 12. For ranges the two
    numbers are min and max; a single number means min == max.
    """
    if not raw or not raw.strip() or _NON_DISCLOSED_RE.search(raw):
        return None

    text = raw.strip()
    if re.search(r"P\.?\s*M\.?|per\s*month", text, re.IGNORECASE):
        period, divisor = "P.M", 1
    elif re.search(r"P\.?\s*A\.?|per\s*annum|per\s*year", text, re.IGNORECASE):
        period, divisor = "P.A", 12
    else:
        return None  # no recognizable period -> unparseable

    amounts = []
    for num, unit in _SALARY_AMOUNT_RE.findall(text):
        value = float(num)
        unit = (unit or "").upper()
        if unit == "K":
            value *= 1_000
        elif unit in ("L", "LAKH", "LAC"):
            value *= 100_000
        amounts.append(value)
    if not amounts:
        return None

    lo, hi = min(amounts[:2]), max(amounts[:2])
    return (int(round(lo / divisor)), int(round(hi / divisor)), period)


def normalize_salary_object(salary):
    """Normalize the API's structured salary object to monthly INR.

    The API returns e.g. {"currency": "INR", "type": "Monthly"|"Yearly",
    "minAmount": 30000, "maxAmount": 50000}. Undisclosed salaries come back
    with type "0" and zero amounts. Returns (min_monthly, max_monthly,
    period, raw_string) or None if the salary is not disclosed/unusable.
    """
    if not isinstance(salary, dict):
        return None
    stype = str(salary.get("type") or "").strip().lower()
    min_amt = salary.get("minAmount") or 0
    max_amt = salary.get("maxAmount") or 0
    if stype == "monthly":
        divisor, period = 1, "P.M"
    elif stype == "yearly":
        divisor, period = 12, "P.A"
    else:
        return None  # type "0" / unknown -> not disclosed
    if min_amt <= 0 and max_amt <= 0:
        return None
    if max_amt <= 0:
        max_amt = min_amt
    if min_amt > max_amt:
        min_amt, max_amt = max_amt, min_amt
    currency = salary.get("currency") or "INR"
    raw = "{} {:,.0f} - {:,.0f} {}".format(currency, min_amt, max_amt, period)
    return (int(round(min_amt / divisor)), int(round(max_amt / divisor)), period, raw)


def compute_cutoff(existing_df):
    """Return the ISO date below which jobs are skipped.

    First run: today - INITIAL_WINDOW_DAYS. Later runs: the newest
    posted_date already saved, minus WATERMARK_GRACE_DAYS of overlap.
    """
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# Healthcare classification
# ----------------------------------------------------------------------------

def _compile_keywords(keywords):
    # Word-boundary match; keywords ending in a space (e.g. "ot ") keep it so
    # "OT Manager" matches but "photo" does not.
    parts = []
    for kw in keywords:
        esc = re.escape(kw.strip())
        parts.append(r"\b" + esc + (r"\s" if kw.endswith(" ") else r"\b"))
    return re.compile("|".join(parts), re.IGNORECASE)


_DENY_RE = _compile_keywords(DENY_TITLE_KEYWORDS)
_ALLOW_RE = _compile_keywords(ALLOW_TITLE_KEYWORDS)


def classify_title(title):
    """Return "deny", "allow", or "unknown" for a job title."""
    title = title or ""
    if _DENY_RE.search(title):
        return "deny"
    if _ALLOW_RE.search(title):
        return "allow"
    return "unknown"


def classify_job(title, category, description="", skills=""):
    """Return the shared family verdict for one docthub job.

    The category facet gets us into the right neighbourhood; the family
    scorer decides which of the eleven families (if any) the job actually is.
    """
    return RF.classify(title=title, skills=skills, description=description)

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": SITE_BASE,
        "Referer": SITE_BASE + "/all-jobs",
    })
    return session


def check_robots(session):
    """Abort if robots.txt disallows us. A missing robots.txt means allowed."""
    for robots_url in ROBOTS_URLS:
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
            continue
        if resp.status_code >= 400:
            continue  # no robots.txt -> allowed
        rp.parse(resp.text.splitlines())
        probe = JOBS_ENDPOINT if "api." in robots_url else SITE_BASE + "/all-jobs"
        if not rp.can_fetch(USER_AGENT, probe):
            sys.exit("robots.txt at {} disallows fetching {} — aborting.".format(robots_url, probe))
    log.info("robots.txt check passed")


def get_json(session, url, params=None):
    """GET a JSON document with rate limiting and exponential-backoff retries."""
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
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Fetching & pagination
# ----------------------------------------------------------------------------

def fetch_categories(session):
    """Return the live category facet list: [{"id", "value", "count"}, ...]."""
    data = get_json(session, JOBS_ENDPOINT + "/facets")
    if not data or not (data.get("facets") or {}).get("categories"):
        sys.exit("Could not fetch category facets from the API — aborting.")
    return data["facets"]["categories"]


def iter_category_jobs(session, category_id, page_size, cutoff, max_pages=None):
    """Yield job dicts for one category, paginating until exhausted.

    Listings come newest-first, so pagination stops early once an entire
    page is older than the cutoff date — daily runs only fetch the head.
    """
    page = 1
    seen = 0
    total = None
    while True:
        if max_pages is not None and page > max_pages:
            return
        data = get_json(session, JOBS_ENDPOINT, {
            "pageNumber": page, "pageSize": page_size,
            "isFacet": "false", "categories": category_id,
        })
        if data is None:
            log.error("Skipping rest of category %s (page %d failed)", category_id, page)
            return
        jobs = data.get("jobs") or []
        if total is None:
            total = data.get("totalRecords")
        if not jobs:
            return
        for job in jobs:
            yield job
        newest = max((job.get("publishedDate") or "")[:10] for job in jobs)
        if newest and newest < cutoff:
            log.debug("Category %s: page %d entirely older than %s — stopping",
                      category_id, page, cutoff)
            return
        seen += len(jobs)
        if total is not None and seen >= total:
            return
        page += 1


# ----------------------------------------------------------------------------
# Parsing a job record into a CSV row
# ----------------------------------------------------------------------------

_JOB_ID_RE = re.compile(r"-(J\d+)$")


def job_to_row(job, category_name, needs_review):
    code = job.get("code") or ""
    id_match = _JOB_ID_RE.search(code)
    job_id = id_match.group(1) if id_match else "J{}".format(job.get("id", ""))

    normalized = normalize_salary_object(job.get("salary"))
    if normalized:
        sal_min, sal_max, period, raw = normalized
    else:
        sal_min = sal_max = None
        period, raw = "", "Not Disclosed"

    work_exp = job.get("workExperience") or {}
    organization = job.get("organization") or {}
    location = (job.get("location") or {}).get("location") or ""
    if not location:
        address = organization.get("address") or {}
        location = ", ".join(x for x in (address.get("city"), address.get("state")) if x)

    published = job.get("publishedDate") or job.get("createdDate") or ""

    return {
        "job_id": job_id,
        "title": (job.get("title") or "").strip(),
        "company": (organization.get("name") or "").strip(),
        "location": location,
        "experience_min_years": work_exp.get("fromYear"),
        "experience_max_years": work_exp.get("toYear"),
        "salary_raw": raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": period,
        "job_type": job.get("employementType") or "",
        "category": category_name,
        "needs_review": bool(needs_review),
        "posted_date": published[:10],
        "job_url": "{}/{}".format(SITE_BASE, code) if code else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----------------------------------------------------------------------------
# Optional detail-page enrichment
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def enrich_row(session, row):
    """Fetch the job-detail API for description and apply link. Best-effort."""
    code = row["job_url"].rsplit("/", 1)[-1]
    detail = get_json(session, "{}/{}".format(JOBS_ENDPOINT, code))
    if not detail:
        return row
    description = _TAG_RE.sub(" ", detail.get("description") or "")
    description = re.sub(r"\s+", " ", description.replace("&nbsp;", " ")).strip()
    row["description"] = description
    row["apply_url"] = detail.get("referenceLink") or row["job_url"]
    return row


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_known_ids(csv_path):
    try:
        existing = pd.read_csv(csv_path, dtype=str)
    except FileNotFoundError:
        return set(), None
    return set(existing["job_id"].dropna()), existing


def append_needs_review(rows, path):
    if not rows:
        return
    review = pd.DataFrame(rows)
    try:
        known = set(pd.read_csv(path, dtype=str)["job_id"].dropna())
        review = review[~review["job_id"].isin(known)]
        review.to_csv(path, mode="a", header=False, index=False)
    except FileNotFoundError:
        review.to_csv(path, index=False)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from jobs.docthub.com into a CSV.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_CSV,
                        help="output CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="crawl at most N pages per category (for test runs)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE,
                        help="jobs per API request (default: %(default)s)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each NEW passing job's detail page for "
                             "description and apply_url (extra requests)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    categories = fetch_categories(session)
    known_names = INCLUDE_CATEGORIES | EXCLUDE_CATEGORIES
    for cat in categories:
        if cat["value"] not in known_names:
            log.info("Category %r not in INCLUDE/EXCLUDE lists — will crawl "
                     "and classify by title", cat["value"])

    known_ids, existing_df = load_known_ids(args.output)
    log.info("Existing CSV has %d known job ids", len(known_ids))
    cutoff = compute_cutoff(existing_df)
    log.info("Keeping jobs posted on/after %s", cutoff)

    counters = {
        "scanned": 0, "excluded_category": 0, "excluded_deny_title": 0,
        "excluded_old": 0, "needs_review": 0, "new": 0, "duplicates": 0,
    }
    new_rows, review_log = [], []

    for cat in categories:
        name, cat_id, count = cat["value"], cat["id"], cat["count"]
        if name in EXCLUDE_CATEGORIES:
            counters["scanned"] += count
            counters["excluded_category"] += count
            log.info("Skipping excluded category %r (%d jobs)", name, count)
            continue
        if CRAWL_ONLY_INCLUDED and name not in INCLUDE_CATEGORIES:
            log.info("Skipping out-of-scope category %r (%d jobs)", name, count)
            continue

        log.info("Crawling category %r (%d jobs)...", name, count)
        for job in iter_category_jobs(session, cat_id, args.page_size, cutoff,
                                      args.max_pages):
            counters["scanned"] += 1
            title = job.get("title") or ""
            verdict = classify_job(
                title, name,
                description=job.get("description") or job.get("jobDescription") or "",
                skills=job.get("skills") or job.get("keySkills") or "")
            if not verdict["family"]:
                counters["excluded_deny_title"] += 1
                continue
            needs_review = verdict["needs_review"]

            row = job_to_row(job, name, needs_review)
            row["category"] = verdict["family"]
            row["all_families"] = verdict["all_families"]
            row["family_scores"] = verdict["family_scores"]
            row["family_confidence"] = verdict["confidence"]
            row["matched_in"] = verdict["matched_in"]
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            if needs_review:
                counters["needs_review"] += 1
                review_log.append({
                    "job_id": row["job_id"], "title": row["title"],
                    "category": name, "salary_raw": row["salary_raw"],
                    "posted_date": row["posted_date"],
                })

            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue

            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

    if args.enrich and new_rows:
        log.info("Enriching %d new jobs with detail pages...", len(new_rows))
        for row in new_rows:
            enrich_row(session, row)

    columns = CSV_COLUMNS + (["description", "apply_url"] if args.enrich else [])
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if existing_df is not None:
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset="job_id", keep="first")
        else:
            combined = new_df
        for col in columns:  # keep column order stable across runs
            if col not in combined.columns:
                combined[col] = ""
        ordered = [c for c in columns if c in combined.columns] + \
                  [c for c in combined.columns if c not in columns]
        combined[ordered].to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        log.info("No new jobs; %s left unchanged", args.output)

    append_needs_review(review_log, NEEDS_REVIEW_CSV)

    print("\n===== Run summary =====")
    print("Total jobs scanned:            {:>7,}".format(counters["scanned"]))
    print("Excluded (non-healthcare cat): {:>7,}".format(counters["excluded_category"]))
    print("Excluded (deny-list title):    {:>7,}".format(counters["excluded_deny_title"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:          {:>7,}".format(counters["needs_review"]))
    print("New jobs added:                {:>7,}".format(counters["new"]))
    print("Duplicates skipped:            {:>7,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
