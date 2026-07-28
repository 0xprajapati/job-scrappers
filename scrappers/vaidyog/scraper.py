#!/usr/bin/env python3
"""Scrape job listings from healthcarejobs.vaidyog.com (Vaidyog, India).

Data source
-----------
healthcarejobs.vaidyog.com is a Vite/React SPA (a healthcare-only job board,
so no extra healthcare filter is needed at the source level). The landing
page fetches its job cards from a public, unauthenticated JSON API:

    GET https://jobs.vaidyog.com/api/jobs-for-u
        ?page=N&limit=M&jobTitle=&location=&keySkills=

which returns jobs newest-first: _id, jobTitle, jobDescription, keySkills,
experienceLevel{min,max}, location{city,state,office}, salaryRange{min,max},
institutionName. Neither host serves a robots.txt (both return 404/SPA
HTML), so nothing is disallowed; the check is still performed at startup.

Per-job detail endpoints (`api/job/<id>`, `api/jobs-for-u/<id>`) require a
Bearer token (401) and are NOT used — the public listing already carries the
full description. There is no public per-job URL either (Apply Now leads to
login), so `job_url` points at the public board.

Quirks
------
* No posted-date field. The Mongo ObjectId `_id` embeds the creation
  timestamp in its first 8 hex chars; that is used as `posted_date`.
* `salaryRange` is always {min:int, max:int} but employers type shorthand:
  {0,0} means undisclosed; values under 1,000 are thousands ("30-40" =
  30-40k/month); a few entries are junk ("0-2"). Normalization: x1000 when
  max < 1000, then drop numeric fields when max still < 5,000 (raw string
  kept). No period field: max >= 100,000 reads per-year, else per-month
  (Indian norms). Currency is always INR (Indian board, no symbols).
* State names are dirty ("Maharastra", "west bengal ", "Tamilnadu"); a
  small alias map cleans the common ones.
* A handful of junk postings exist ("test", "testing"); they are kept and
  flagged needs_review, never dropped.

Outputs
-------
* vaidyog_jobs.csv                         — rich cumulative store (dedup: _id)
* ../../jobs_csv/<DD-MM-YYYY>/vaidyog.csv  — HealthCareers.club 22-col schema

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored date minus
WATERMARK_GRACE_DAYS of overlap.

Run `python scraper.py --help` for options.
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

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "vaidyog"
BOARD_URL = "https://healthcarejobs.vaidyog.com/"
API_BASE = "https://jobs.vaidyog.com"
ROBOTS_URLS = (BOARD_URL + "robots.txt", API_BASE + "/robots.txt")
LISTING_URL = API_BASE + "/api/jobs-for-u"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 3_000
MIN_PLAUSIBLE_MONTHLY = 5_000  # below this after normalization -> raw only

RICH_CSV = "vaidyog_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "state", "country",
    "country_code", "country_dial_code", "salary_raw", "salary_min",
    "salary_max", "salary_period", "salary_currency", "job_type",
    "min_experience", "max_experience", "category", "company_type",
    "skills", "needs_review", "posted_date", "description", "job_url",
    "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("vaidyog_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text) if text is not None else "")).strip()


_OBJECTID_RE = re.compile(r"^[0-9a-f]{24}$")


def posted_date_from_id(object_id):
    """YYYY-MM-DD from the creation timestamp a Mongo ObjectId embeds in its
    first 8 hex chars; "" when the id does not look like an ObjectId."""
    oid = clean_text(object_id).lower()
    if not _OBJECTID_RE.match(oid):
        return ""
    ts = int(oid[:8], 16)
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def parse_salary(salary_range):
    """Normalize the API's salaryRange {min:int, max:int}.

    Real examples: {15000, 30000} monthly, {1800000, 2400000} = 18-24 LPA,
    {30, 40} = employer shorthand for 30-40k/month, {0, 0} = undisclosed,
    {0, 2} = junk. Returns {} for undisclosed (never invents values), else
    salary_raw plus, when plausible, salary_min/max (int, x1000 applied to
    sub-1000 shorthand), salary_period (club enum: max >= 100,000 per_annum
    else per_month) and salary_currency INR. Implausible values (max under
    5,000 after normalization) keep the raw string only.
    """
    sr = salary_range or {}
    try:
        lo = int(sr.get("min") or 0)
        hi = int(sr.get("max") or 0)
    except (TypeError, ValueError):
        return {}
    if hi <= 0 and lo <= 0:
        return {}
    if hi < lo:
        lo, hi = hi, lo
    raw = "{}-{}".format(sr.get("min"), sr.get("max"))
    if hi < 1000:  # thousands shorthand ("30-40" = 30-40k)
        lo, hi = lo * 1000, hi * 1000
    if hi < MIN_PLAUSIBLE_MONTHLY:
        return {"salary_raw": raw}
    period = "per_annum" if hi >= 100_000 else "per_month"
    return {"salary_raw": raw, "salary_min": lo, "salary_max": hi,
            "salary_period": period, "salary_currency": "INR"}


def parse_experience(experience_level):
    """(min, max) ints as strings; blanks when absent/invalid."""
    exp = experience_level or {}

    def _num(v):
        try:
            n = int(float(v))
            return str(n) if 0 <= n <= 60 else ""
        except (TypeError, ValueError):
            return ""

    return _num(exp.get("min")), _num(exp.get("max"))


_JUNK_TITLE_RE = re.compile(
    r"^\s*test(ing)?\b|\btest jobs?\b|\bdummy\b|asdf|qwer", re.IGNORECASE)

_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|\bbams\b|\bbhms\b|dentist|"
    r"medical officer|\brmo\b|[a-z]+ologist|intensivist|hospitalist|"
    r"anaesthetist|anesthetist|obstetrician|p(a?)ediatrician|psychiatrist|"
    r"\bconsultant\b.*(medicine|surgery|physician)|medical superintendent|"
    r"medical director|sonologist|ultrasonologist", re.IGNORECASE)


def classify_category(title):
    """Map a title to the club category enum (doctors|nurses|pharmacists|
    non_clinical). Returns (category, needs_review); needs_review flags only
    junk/test titles — the board itself is healthcare-only."""
    title = title or ""
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title):
        category = "pharmacists"
    elif _DOCTOR_RE.search(title):
        category = "doctors"
    else:
        category = "non_clinical"
    return category, bool(_JUNK_TITLE_RE.search(title))


_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|diagnostic|"
    r"life ?science", re.IGNORECASE)


def classify_company_type(company):
    """club enum hospital|pharma."""
    return "pharma" if _PHARMA_RE.search(company or "") else "hospital"


_STATE_ALIASES = {
    "maharastra": "Maharashtra",
    "tamilnadu": "Tamil Nadu",
    "westbengal": "West Bengal",
    "karnatka": "Karnataka",
    "kerela": "Kerala",
    "telengana": "Telangana",
}


def normalize_state(state):
    """Fix the common misspellings employers type into the state field."""
    text = clean_text(state)
    return _STATE_ALIASES.get(text.replace(" ", "").lower(), text)


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def check_robots(session):
    for robots_url in ROBOTS_URLS:
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
            body = resp.text if resp.status_code < 400 else ""
            # both hosts currently 404 / serve the SPA shell instead of a
            # robots.txt; only parse what actually looks like one
            if "<html" in body[:200].lower():
                body = ""
            rp.parse(body.splitlines())
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
            continue
        if not rp.can_fetch(USER_AGENT, LISTING_URL):
            sys.exit("robots.txt disallows {} — aborting.".format(LISTING_URL))
    log.info("robots.txt check passed")


def fetch_page(session, page):
    """One page of the public listing, newest-first; None on failure."""
    params = {"page": page, "limit": PAGE_SIZE,
              "jobTitle": "", "location": "", "keySkills": ""}
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for page %d in %.0fs (%s)",
                        attempt, MAX_RETRIES, page, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(LISTING_URL, params=params,
                               timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for page %d", resp.status_code, page)
                return None
            resp.raise_for_status()
            return resp.json().get("data") or []
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    log.error("Giving up on page %d after %d retries (%s)", page, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    title = clean_text(job.get("jobTitle"))
    category, needs_review = classify_category(title)
    company = clean_text(job.get("institutionName"))
    location = job.get("location") or {}
    exp_min, exp_max = parse_experience(job.get("experienceLevel"))
    skills = job.get("keySkills") or []
    if not isinstance(skills, list):
        skills = [skills]

    row = {
        "source": SITE,
        "job_id": clean_text(job.get("_id")),
        "title": title,
        "company": company,
        "city": clean_text(location.get("city")),
        "state": normalize_state(location.get("state")),
        "country": "India",           # Indian board; API carries no country
        "country_code": "IN",
        "country_dial_code": "+91",
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": "full_time",      # site lists openings; no type field
        "min_experience": exp_min,
        "max_experience": exp_max,
        "category": category,
        "company_type": classify_company_type(company),
        "skills": "; ".join(clean_text(s) for s in skills[:15] if clean_text(s)),
        "needs_review": needs_review,
        "posted_date": posted_date_from_id(job.get("_id")),
        "description": clean_text(job.get("jobDescription"))[:DESCRIPTION_MAX_CHARS],
        "job_url": BOARD_URL,         # no public per-job URL (apply is login-gated)
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    row.update(parse_salary(job.get("salaryRange")))
    return row


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _int_str(value):
    value = _blank(value)
    if value == "":
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def rich_row_to_club_row(r):
    max_sal = _int_str(r.get("salary_max"))
    has_salary = bool(max_sal) and _blank(r.get("salary_currency")) == "INR"
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
        "company_name": _blank(r.get("company")) or _blank(r.get("title")),
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")) or "non_clinical",
        "application_url": _blank(r.get("job_url")) or BOARD_URL,
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        "min_salary": _int_str(r.get("salary_min")) if has_salary else "",
        "max_salary": max_sal if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": "INR" if has_salary else "",
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
    rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape jobs from healthcarejobs.vaidyog.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N pages of %d (test runs)" % PAGE_SIZE)
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
    page, empty_pages = 1, 0

    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        jobs = fetch_page(session, page)
        page += 1
        if not jobs:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        empty_pages = 0

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:
                log.warning("Skipping malformed job %s: %s", job.get("_id"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
            if not row["job_id"] or row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "company": row["company"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        # newest-first: once a whole page is older than the cutoff, stop.
        if page_all_old or len(jobs) < PAGE_SIZE:
            break

    # ---- rich cumulative CSV ----
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

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
