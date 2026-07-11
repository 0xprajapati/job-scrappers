#!/usr/bin/env python3
"""Scrape healthcare job listings from jobslly.in into a deduplicated CSV.

Data source
-----------
jobslly.in's robots.txt explicitly disallows `/api/` for all user agents, so
this scraper never touches the JSON API. Instead it uses what the site
deliberately exposes to crawlers:

    https://jobslly.in/sitemap.xml        -> all job detail URLs (updated daily)
    https://jobslly.in/jobs/<slug>-<hex>  -> job page with schema.org
                                             JobPosting JSON-LD embedded

Each job page embeds a structured JobPosting object (title, description,
company, location, datePosted, employmentType, baseSalary). Salary values are
published in LAKHS per annum (e.g. minValue 18, unitText "YEAR" means 18 LPA);
values >= 1000 are treated as raw INR.

The slug's trailing hex id (e.g. ...-3e71038c) is the dedup key, so re-runs
only download pages for jobs not already in the CSV.

Run `python jobslly_scraper.py --help` for options.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE_BASE = "https://jobslly.in"
SITEMAP_URL = SITE_BASE + "/sitemap.xml"
ROBOTS_URL = SITE_BASE + "/robots.txt"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (personal research; contact: eleswarapu.madhav@gmail.com)"
)

# Time window (no salary filter — salaries are captured but never filtered on).
# First run keeps jobs posted in the last INITIAL_WINDOW_DAYS; later runs keep
# only jobs newer than the newest posted_date already in the CSV, minus
# WATERMARK_GRACE_DAYS of overlap (dedup absorbs the overlap).
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

# Salary numbers below this are assumed to be in lakhs (jobslly publishes
# "18" meaning 18 LPA); numbers at or above it are taken as raw INR.
LAKH_HEURISTIC_THRESHOLD = 1_000

# Jobslly is healthcare-only, but it includes corporate/non-clinical roles.
# Titles are classified with the same ALLOW/DENY lists as the DoctHub
# scraper: DENY drops the job, ALLOW keeps it, anything else is kept but
# flagged needs_review=True and logged to needs_review.csv.
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
    "admissions counselor", "sales consultant",
]

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
    # Jobslly-specific non-clinical-but-healthcare corporate roles
    "drug safety", "regulatory affairs", "regulatory writer",
    "scientific writer", "medical writer", "medical coding", "medical coder",
    "medical scribe", "medical affairs", "msl", "medical science liaison",
]

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

DEFAULT_OUTPUT_CSV = "jobslly_jobs.csv"
NEEDS_REVIEW_CSV = "needs_review.csv"

CSV_COLUMNS = [
    "job_id", "title", "company", "location",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "needs_review",
    "posted_date", "job_url", "description", "scraped_at",
]

log = logging.getLogger("jobslly_scraper")

# ----------------------------------------------------------------------------
# Salary normalization
# ----------------------------------------------------------------------------

def normalize_base_salary(base_salary):
    """Normalize a schema.org baseSalary object to monthly INR.

    Jobslly publishes e.g. {"currency": "INR", "value": {"minValue": 18,
    "maxValue": 25, "unitText": "YEAR"}} where 18 means 18 lakhs/year.
    Values >= LAKH_HEURISTIC_THRESHOLD are treated as raw INR instead.
    Returns (min_monthly, max_monthly, period, raw_string) or None when the
    salary is missing/undisclosed. period is "LPA" for yearly lakhs,
    "P.A" for yearly INR, "P.M" for monthly.
    """
    if not isinstance(base_salary, dict):
        return None
    value = base_salary.get("value") or {}
    if not isinstance(value, dict):
        return None
    min_val, max_val = value.get("minValue"), value.get("maxValue")
    unit = str(value.get("unitText") or "").strip().upper()
    if min_val is None and max_val is None:
        return None
    if min_val is None:
        return None  # "up to X" — lower bound unknown, fails a strict min filter
    if max_val is None or max_val < min_val:
        max_val = min_val

    in_lakhs = max_val < LAKH_HEURISTIC_THRESHOLD
    factor = 100_000 if in_lakhs else 1
    if unit == "YEAR":
        divisor = 12
        period = "LPA" if in_lakhs else "P.A"
    elif unit == "MONTH":
        divisor = 1
        period = "P.M"
    else:
        return None  # unknown period -> treat as unparseable

    currency = base_salary.get("currency") or "INR"
    raw = "{} {:g} - {:g} {}".format(currency, min_val, max_val, unit)
    min_monthly = int(round(min_val * factor / divisor))
    max_monthly = int(round(max_val * factor / divisor))
    if min_monthly <= 0 and max_monthly <= 0:
        return None
    return (min_monthly, max_monthly, period, raw)


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
# Healthcare classification (same semantics as the DoctHub scraper)
# ----------------------------------------------------------------------------

def _compile_keywords(keywords):
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


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def load_robots(session):
    """Return a RobotFileParser for jobslly.in; abort if we can't be polite."""
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        rp.parse([])
        return rp
    rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    if not rp.can_fetch(USER_AGENT, SITEMAP_URL):
        sys.exit("robots.txt disallows the sitemap — aborting.")
    return rp


def get(session, url, as_json=False):
    """GET with rate limiting and exponential-backoff retries."""
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
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Sitemap & page parsing
# ----------------------------------------------------------------------------

_JOB_URL_RE = re.compile(r"^https://jobslly\.in/jobs/.+-([0-9a-f]{8})$")
_LDJSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_WS_RE = re.compile(r"\s+")


def fetch_job_urls(session):
    """Return [(job_id, url), ...] for every job page in the sitemap."""
    xml_text = get(session, SITEMAP_URL)
    if xml_text is None:
        sys.exit("Could not fetch the sitemap — aborting.")
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        sys.exit("Could not parse the sitemap ({}) — aborting.".format(exc))
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    jobs = []
    for loc in root.findall(".//sm:url/sm:loc", ns):
        match = _JOB_URL_RE.match((loc.text or "").strip())
        if match:
            jobs.append((match.group(1), match.group(0)))
    return jobs


def extract_job_posting(html):
    """Return the schema.org JobPosting dict embedded in a job page, or None."""
    for match in _LDJSON_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def posting_to_row(posting, job_id, url, needs_review):
    normalized = normalize_base_salary(posting.get("baseSalary"))
    if normalized:
        sal_min, sal_max, period, raw = normalized
    else:
        sal_min = sal_max = None
        period, raw = "", "Not Disclosed"

    address = ((posting.get("jobLocation") or {}).get("address") or {})
    location = ", ".join(
        part.strip() for part in
        (address.get("addressLocality"), address.get("addressRegion"))
        if part and part.strip())

    employment = posting.get("employmentType") or []
    if isinstance(employment, str):
        employment = [employment]
    job_type = ", ".join(e.replace("_", " ").title() for e in employment)

    description = _WS_RE.sub(" ", posting.get("description") or "").strip()

    return {
        "job_id": job_id,
        "title": (posting.get("title") or "").strip(),
        "company": ((posting.get("hiringOrganization") or {}).get("name") or "").strip(),
        "location": location,
        "salary_raw": raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": period,
        "job_type": job_type,
        "needs_review": bool(needs_review),
        "posted_date": (posting.get("datePosted") or "")[:10],
        "job_url": url,
        "description": description[:DESCRIPTION_MAX_CHARS],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


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
        description="Scrape healthcare jobs from jobslly.in into a CSV.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_CSV,
                        help="output CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="fetch at most N new job pages (for test runs)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    robots = load_robots(session)

    job_urls = fetch_job_urls(session)
    log.info("Sitemap lists %d job pages", len(job_urls))

    known_ids, existing_df = load_known_ids(args.output)
    log.info("Existing CSV has %d known job ids", len(known_ids))
    cutoff = compute_cutoff(existing_df)
    log.info("Keeping jobs posted on/after %s", cutoff)

    counters = {
        "scanned": 0, "duplicates": 0, "robots_blocked": 0, "page_errors": 0,
        "excluded_deny_title": 0, "excluded_old": 0, "needs_review": 0,
        "new": 0,
    }
    new_rows, review_log = [], []
    fetched = 0

    for job_id, url in job_urls:
        counters["scanned"] += 1
        if job_id in known_ids:
            counters["duplicates"] += 1
            continue
        if args.limit is not None and fetched >= args.limit:
            continue
        if not robots.can_fetch(USER_AGENT, url):
            counters["robots_blocked"] += 1
            log.warning("robots.txt disallows %s — skipping", url)
            continue

        html = get(session, url)
        fetched += 1
        if html is None:
            counters["page_errors"] += 1
            continue
        posting = extract_job_posting(html)
        if posting is None:
            counters["page_errors"] += 1
            log.warning("No JobPosting JSON-LD on %s — skipping", url)
            continue

        title = (posting.get("title") or "").strip()
        verdict = classify_title(title)
        if verdict == "deny":
            counters["excluded_deny_title"] += 1
            continue
        needs_review = verdict == "unknown"

        row = posting_to_row(posting, job_id, url, needs_review)
        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            continue
        if needs_review:
            counters["needs_review"] += 1
            review_log.append({
                "job_id": job_id, "title": row["title"],
                "salary_raw": row["salary_raw"],
                "posted_date": row["posted_date"],
            })

        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if existing_df is not None:
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset="job_id", keep="first")
        else:
            combined = new_df
        for col in CSV_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        ordered = [c for c in CSV_COLUMNS if c in combined.columns] + \
                  [c for c in combined.columns if c not in CSV_COLUMNS]
        combined[ordered].to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        log.info("No new jobs; %s left unchanged", args.output)

    append_needs_review(review_log, NEEDS_REVIEW_CSV)

    print("\n===== Run summary =====")
    print("Job pages in sitemap:          {:>5,}".format(counters["scanned"]))
    print("Duplicates skipped (no fetch): {:>5,}".format(counters["duplicates"]))
    print("Blocked by robots.txt:         {:>5,}".format(counters["robots_blocked"]))
    print("Page/parse errors:             {:>5,}".format(counters["page_errors"]))
    print("Excluded (deny-list title):    {:>5,}".format(counters["excluded_deny_title"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:          {:>5,}".format(counters["needs_review"]))
    print("New jobs added:                {:>5,}".format(counters["new"]))


if __name__ == "__main__":
    main()
