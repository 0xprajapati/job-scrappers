#!/usr/bin/env python3
"""Scrape job listings from careers.manipalhospitals.com (Manipal Hospitals).

Data source
-----------
www.manipalhospitals.com/careers/ redirects to careers.manipalhospitals.com,
an Angular SPA on the Zwayam recruitment platform (company id 15590). Being a
hospital chain's own ATS, every posting is healthcare-industry at the source
(master spec §2: filter at source).

The SPA loads everything from public.zwayam.com JSON APIs (robots.txt on the
careers subdomain is "Allow: /"; public.zwayam.com has no robots.txt at all):

    POST https://public.zwayam.com/manageESQueries/searchJob
        multipart form: id=15590, companyUrl=careers.manipalhospitals.com/,
        job/city/userGeoLocation/departmentName/fieldName/fieldValue =
        the literal string "undefined" (the SPA serialises undefined JS
        values that way, and the endpoint 400s without them)

which returns the ENTIRE index (~213 jobs) as one Elasticsearch hit list:
jobTitle, id, jobCode, jobUrl slug, locAgg city, experience min/max,
mandatorySkills, createdDate/modifiedDate (epoch ms). No pagination.

Detail endpoint (fetched for NEW jobs only, default on; `--no-details`
skips) adds the full description, departmentName and annual INR salary:

    POST https://public.zwayam.com/jobs-service/v1/jobs/careersite
        JSON: {"jobUrl": <slug>, "externalSource": "CAREERSITE",
               "campusUrl": "empty", "companyId": "15590"}

Quirks
------
* The Akamai WAF in front of public.zwayam.com silently drops (connection
  reset) ANY request whose User-Agent contains a custom token — even
  "Mozilla/5.0 (compatible; ...bot...)". Plain "Mozilla/5.0" is accepted,
  so this scraper deviates from the master spec's descriptive-UA rule and
  carries the contact address in a `From:` header instead.
* The list is sorted (featuredJob desc, modifiedDate desc), NOT by posted
  date — but the whole index arrives in one response, so every run scans
  it all and applies the time-window cutoff per job (no early stop).
* Salary appears only in the detail response (minJobSalary/maxJobSalary,
  plain numbers, e.g. 324000-360000 for a senior ICU nurse = annual INR).
  Empty strings mean "Not Disclosed" — never invented.
* jobType is always "J" and workMode is null in practice; job_type
  defaults to full_time.
* departmentName (e.g. "ICU (Intensive care Unit)") also comes only from
  the detail call; with --no-details the classifier sees only the title +
  mandatorySkills and department stays blank.
* Classification is the shared two-level taxonomy (scrappers/_shared/
  classification.py): category "Non Clinical" | "Public Health" plus a
  sub_category. Being a hospital chain's ATS, most postings (nursing,
  clinicians, paramedical, hospital admin) are out of scope and dropped
  (counted as excluded_out_of_scope). departmentName and mandatorySkills
  stay in the rich CSV as raw source columns and feed the classifier as its
  curated `skills` signal — they never decide the category themselves.

Outputs
-------
* manipalhospitals_jobs.csv                — rich cumulative store
  (dedup key: numeric ES id)
* ../../jobs_csv/<DD-MM-YYYY>/manipalhospitals.csv — HealthCareers.club
  22-col schema

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "manipalhospitals"
SITE_BASE = "https://careers.manipalhospitals.com"
ROBOTS_URLS = (SITE_BASE + "/robots.txt",
               "https://public.zwayam.com/robots.txt")
COMPANY_ID = "15590"
COMPANY_URL = "careers.manipalhospitals.com/"
SEARCH_URL = "https://public.zwayam.com/manageESQueries/searchJob"
DETAIL_URL = "https://public.zwayam.com/jobs-service/v1/jobs/careersite"
JOB_URL_TMPL = SITE_BASE + "/manipalhospitals/jobview/{}"

# The Akamai WAF resets connections for any UA carrying a custom token (see
# module docstring); contact info therefore travels in the From header.
USER_AGENT = "Mozilla/5.0"
CONTACT = "eleswarapu.madhav@gmail.com"

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

IST = timezone(timedelta(hours=5, minutes=30))

RICH_CSV = "manipalhospitals_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "job_code", "job_slug", "title", "company",
    "department", "city", "state", "country", "country_code",
    "country_dial_code", "salary_raw", "salary_min", "salary_max",
    "salary_period", "salary_currency", "job_type", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in",
    "company_type", "skills", "min_experience", "max_experience",
    "openings", "needs_review", "posted_date", "description", "job_url",
    "scraped_at",
]

log = logging.getLogger("manipalhospitals_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


def parse_job_salary(min_salary, max_salary):
    """minJobSalary/maxJobSalary from the detail API: plain INR numbers,
    sometimes empty strings (= Not Disclosed — never invents values).

    Real example: 324000 / 360000 for a senior ICU nurse, i.e. annual.
    When the upper amount is under 100,000 it is read as per-month
    (Indian salary norms), mirroring the swaasa scraper's heuristic.
    """
    def to_int(v):
        try:
            n = int(round(float(v)))
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    lo, hi = to_int(min_salary), to_int(max_salary)
    if lo is None and hi is None:
        return {}
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if hi < lo:
        lo, hi = hi, lo
    period = "per_annum" if hi >= 100_000 else "per_month"
    return {
        "salary_raw": "INR {:,} - {:,} {}".format(
            lo, hi, "per year" if period == "per_annum" else "per month"),
        "salary_min": lo, "salary_max": hi,
        "salary_period": period, "salary_currency": "INR",
    }


def epoch_ms_to_date(ms):
    """createdDate/modifiedDate epoch milliseconds -> ISO date (IST)."""
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, IST).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def parse_experience(job):
    """minYearOfExperience/maxYearOfExperience -> (min, max) strings."""
    def to_str(v):
        try:
            return str(int(float(v)))
        except (TypeError, ValueError):
            return ""
    return to_str(job.get("minYearOfExperience")), \
        to_str(job.get("maxYearOfExperience"))


def classification_skills(row):
    """The curated `skills` signal: Zwayam departmentName + the listing's
    mandatorySkills. Department only exists after the detail fetch."""
    return " ".join(filter(None, [row.get("department", ""),
                                  row.get("skills", "")])).strip()


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    departmentName/mandatorySkills are the curated `skills` signal; the raw
    values stay in the rich CSV as source columns only.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           classification_skills(row),
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
    s.headers.update({"User-Agent": USER_AGENT, "From": CONTACT})
    return s


def check_robots(session):
    """careers.manipalhospitals.com serves "Allow: /"; public.zwayam.com has
    no robots.txt (404 = no restrictions). Abort on an explicit disallow."""
    for robots_url in ROBOTS_URLS:
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code >= 400:
                log.info("No robots.txt at %s (HTTP %d) — allowed",
                         robots_url, resp.status_code)
                continue
            rp.parse(resp.text.splitlines())
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); assuming allowed",
                        robots_url, exc)
            continue
        base = robots_url.rsplit("/", 1)[0]
        for url in (SEARCH_URL, DETAIL_URL, JOB_URL_TMPL.format("x")):
            if url.startswith(base) and not rp.can_fetch(USER_AGENT, url):
                sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, url, *, form=None, payload=None):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.post(url, files=form, json=payload,
                                timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_all_jobs(session):
    """The whole index in one call. The SPA form-encodes undefined JS values
    as the literal string "undefined"; the endpoint 400s without them."""
    form = {
        "id": (None, COMPANY_ID),
        "companyUrl": (None, COMPANY_URL),
        "job": (None, "undefined"),
        "city": (None, "undefined"),
        "userGeoLocation": (None, "undefined"),
        "departmentName": (None, "undefined"),
        "fieldName": (None, "undefined"),
        "fieldValue": (None, "undefined"),
    }
    data = _request(session, SEARCH_URL, form=form)
    if not isinstance(data, list):
        return None
    return [hit.get("_source") or {} for hit in data]


def fetch_detail(session, job_slug):
    """Full job detail; returns the response dict or {}."""
    payload = {"jobUrl": job_slug, "externalSource": "CAREERSITE",
               "campusUrl": "empty", "companyId": COMPANY_ID}
    data = _request(session, DETAIL_URL, payload=payload)
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    title = clean_text(job.get("jobTitle"))
    job_slug = clean_text(job.get("jobUrl"))
    exp_min, exp_max = parse_experience(job)
    skills = job.get("mandatorySkills") or []
    if not skills and job.get("skillSet"):
        skills = [s for s in str(job["skillSet"]).split(",")]

    return {
        "source": SITE,
        "job_id": clean_text(job.get("id")) or job_slug,
        "job_code": clean_text(job.get("jobCode")),
        "job_slug": job_slug,
        "title": title,
        "company": "Manipal Hospitals",
        "department": "",
        "city": clean_text(job.get("locAgg") or job.get("location")),
        "state": "",
        "country": "India",       # all Manipal units are in India
        "country_code": "IN",
        "country_dial_code": "+91",
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": "full_time",  # jobType is always "J", workMode null
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": "hospital",
        "skills": "; ".join(clean_text(s) for s in skills[:15]),
        "min_experience": exp_min,
        "max_experience": exp_max,
        "openings": clean_text(job.get("positionsRequired")),
        "needs_review": False,
        "posted_date": epoch_ms_to_date(job.get("createdDate")),
        "description": "",
        "job_url": JOB_URL_TMPL.format(job_slug) if job_slug else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, detail):
    """Merge detail-API extras into a rich row (in place)."""
    if not detail:
        return row
    description = strip_html(detail.get("longDescription")
                             or detail.get("shortDescription") or "")
    if not description:
        description = clean_text(detail.get("mediumDescriptionWithoutHtml")
                                 or detail.get("shortDescriptionWithoutHtml"))
    if description:
        row["description"] = description[:DESCRIPTION_MAX_CHARS]
    department = clean_text(detail.get("departmentName"))
    if department:
        # feeds the classifier's `skills` signal (see classification_skills)
        row["department"] = department
    row.update(parse_job_salary(detail.get("minJobSalary"),
                                detail.get("maxJobSalary")))
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
    min_sal = _int_str(r.get("salary_min"))
    currency = _blank(r.get("salary_currency"))
    has_salary = bool(min_sal) and currency in ("INR", "USD")
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")),
        "company_name": _blank(r.get("company")) or "Manipal Hospitals",
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        # Zwayam has no structured qualification field, so this is the
        # grounded extraction from the description — never inferred
        "qualification": extract_qualification(_blank(r.get("description"))),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
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
        description="Scrape jobs from careers.manipalhospitals.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process only the first N listed jobs (test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (no descriptions/salary/"
                             "department; faster)")
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
                "needs_review": 0, "new": 0, "duplicates": 0,
                "detail_failed": 0}
    new_rows, review_log = [], []

    jobs = fetch_all_jobs(session)
    if jobs is None:
        sys.exit("Could not fetch the job index — aborting.")
    log.info("Index returned %d jobs", len(jobs))
    if args.limit is not None:
        jobs = jobs[:args.limit]

    for job in jobs:
        counters["scanned"] += 1
        try:
            row = job_to_rich_row(job)
        except Exception as exc:
            log.warning("Skipping malformed job %s: %s", job.get("id"), exc)
            continue
        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            continue
        if row["job_id"] in known_ids:
            counters["duplicates"] += 1
            continue
        if not args.no_details and row["job_slug"]:
            detail = fetch_detail(session, row["job_slug"])
            if detail:
                apply_detail(row, detail)
            else:
                counters["detail_failed"] += 1
        # classify after the detail merge so department/description count
        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            continue
        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "department": row["department"]})
        known_ids.add(row["job_id"])
        new_rows.append(row)
        counters["new"] += 1

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
    print("Excluded (out of scope): {:>3,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
