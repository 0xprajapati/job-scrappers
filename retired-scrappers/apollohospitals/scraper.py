#!/usr/bin/env python3
"""Scrape job listings from the Apollo Hospitals careers portal.

Data source
-----------
www.apollohospitals.com/careers/ is a static marketing shell (and its host
blocks non-browser clients with an Azure gateway 403). The actual "Job Search"
runs on Oracle Recruiting Cloud (Candidate Experience site ``CX_2``) at
cgs.fa.ap2.oraclecloud.com, whose public REST API serves plain JSON with no
bot-blocking (robots.txt is a 404 — no crawl rules):

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber=CX_2,limit=N,offset=M,
                sortBy=POSTING_DATES_DESC

returns Id, Title, PostedDate (YYYY-MM-DD), PrimaryLocation
("City, State, India"), newest-first. The whole index is small (~90 open
requisitions), so a full crawl is a couple of pages.

Detail endpoint (fetched for NEW jobs only, default on; `--no-details`
skips):

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;siteNumber=CX_2,Id="<Id>"

adds RequisitionType (site category: Nursing / Administration / Paramedical /
…), JobSchedule, StudyLevel, the HTML description, and workLocation with the
concrete hospital unit name ("Apollo Hospitals, Bannerghatta Road,
Bangalore") used as company_name.

The legacy per-city apollo.taleo.net career sections linked from the marketing
page are the old platform; the Oracle site is the maintained central index.

Quirks
------
* No listing ever shows a salary -> salary_raw = "Not Disclosed" everywhere.
* Descriptions are populated for some requisitions (mostly Nursing) and
  genuinely empty for many others.
* Classification is the shared two-level taxonomy (scrappers/_shared/
  classification.py): category "Non Clinical" | "Public Health" plus a
  sub_category. Being a hospital board, most requisitions (nursing, doctors,
  paramedical) are out of scope and dropped (counted as
  excluded_out_of_scope). The Oracle RequisitionType stays in the rich CSV
  as a raw source column (site_category) and feeds the classifier as its
  curated `skills` signal — it never decides the category itself.

Outputs
-------
* apollohospitals_jobs.csv                         — rich cumulative store (dedup: Id)
* ../../jobs_csv/<DD-MM-YYYY>/apollohospitals.csv  — HealthCareers.club 22-col schema

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored date minus
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

SITE = "apollohospitals"
API_BASE = "https://cgs.fa.ap2.oraclecloud.com/hcmRestApi/resources/latest"
ROBOTS_URL = "https://cgs.fa.ap2.oraclecloud.com/robots.txt"
SITE_NUMBER = "CX_2"
LIST_URL_TMPL = (
    API_BASE + "/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList.secondaryLocations"
    "&finder=findReqs;siteNumber={site},limit={limit},offset={offset},"
    "sortBy=POSTING_DATES_DESC")
DETAIL_URL_TMPL = (
    API_BASE + "/recruitingCEJobRequisitionDetails"
    "?expand=all&onlyData=true"
    "&finder=ById;siteNumber={site},Id=%22{job_id}%22")
JOB_URL_TMPL = ("https://cgs.fa.ap2.oraclecloud.com/hcmUI/CandidateExperience"
                "/en/sites/{site}/job/{job_id}".format(site=SITE_NUMBER,
                                                       job_id="{}"))

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): the site never discloses salaries at all.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "apollohospitals_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COMPANY_FALLBACK = "Apollo Hospitals"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "hospital_unit", "city", "state",
    "country", "country_code", "country_dial_code", "salary_raw",
    "salary_min", "salary_max", "salary_period", "salary_currency",
    "job_type", "site_category", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "company_type", "study_level", "needs_review", "posted_date",
    "posting_end_date", "description", "job_url", "scraped_at",
]

log = logging.getLogger("apollohospitals_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text) if text is not None else "")).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The Oracle RequisitionType (site_category) is the curated `skills`
    signal; the raw value stays in the rich CSV as a source column only.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("site_category", ""),
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


def classify_job_type(job_schedule):
    """Oracle JobSchedule -> club enum."""
    schedule = (job_schedule or "").strip().lower()
    if "part" in schedule:
        return "part_time"
    if "contract" in schedule or "fixed term" in schedule:
        return "contract"
    return "full_time"


def parse_location(primary_location):
    """"Bangalore, Karnataka, India" -> (city, state). Tolerates shorter
    forms ("Karnataka, India", "India")."""
    parts = [clean_text(p) for p in (primary_location or "").split(",")]
    parts = [p for p in parts if p]
    if parts and parts[-1].lower() == "india":
        parts = parts[:-1]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


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
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        # Oracle Cloud serves no robots.txt (404) -> no crawl restrictions.
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    probe = LIST_URL_TMPL.format(site=SITE_NUMBER, limit=1, offset=0)
    if not rp.can_fetch(USER_AGENT, probe):
        sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt check passed")


def _request_json(session, url):
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
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_search_page(session, offset):
    """One page of the requisition index, newest-first.
    Returns (jobs, total_count) or (None, None) on failure."""
    url = LIST_URL_TMPL.format(site=SITE_NUMBER, limit=PAGE_SIZE, offset=offset)
    data = _request_json(session, url)
    if not data or not data.get("items"):
        return None, None
    item = data["items"][0]
    return item.get("requisitionList") or [], item.get("TotalJobsCount")


def fetch_detail(session, job_id):
    """Full requisition detail; {} when unavailable."""
    url = DETAIL_URL_TMPL.format(site=SITE_NUMBER, job_id=job_id)
    data = _request_json(session, url)
    if not data or not data.get("items"):
        return {}
    return data["items"][0]


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_description(detail):
    """Description + qualifications + responsibilities, HTML stripped."""
    parts = []
    for key in ("ExternalDescriptionStr", "ExternalQualificationsStr",
                "ExternalResponsibilitiesStr"):
        text = strip_html(detail.get(key) or "")
        if text:
            parts.append(text)
    return " ".join(parts)[:DESCRIPTION_MAX_CHARS]


def job_to_rich_row(job, detail=None):
    detail = detail or {}
    job_id = clean_text(job.get("Id"))
    title = clean_text(job.get("Title"))
    site_category = clean_text(detail.get("RequisitionType"))
    city, state = parse_location(job.get("PrimaryLocation"))

    work_locations = detail.get("workLocation") or []
    hospital_unit = clean_text(
        work_locations[0].get("LocationName")) if work_locations else ""

    return {
        "source": SITE,
        "job_id": job_id,
        "title": title,
        "company": hospital_unit or COMPANY_FALLBACK,
        "hospital_unit": hospital_unit,
        "city": city,
        "state": state,
        "country": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        # the portal never displays compensation on any listing
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": classify_job_type(detail.get("JobSchedule")),
        "site_category": site_category,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": "hospital",
        "study_level": clean_text(detail.get("StudyLevel")),
        "needs_review": False,
        "posted_date": clean_text(job.get("PostedDate"))[:10],
        "posting_end_date": clean_text(detail.get("ExternalPostedEndDate"))[:10],
        "description": build_description(detail),
        "job_url": JOB_URL_TMPL.format(job_id),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def rich_row_to_club_row(r):
    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
        "company_name": _blank(r.get("company")) or COMPANY_FALLBACK,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        # structured source field (StudyLevel) first, else grounded
        # extraction from the description — never inferred
        "qualification": _blank(r.get("study_level"))
            or extract_qualification(_blank(r.get("description"))),
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
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
        description="Scrape jobs from the Apollo Hospitals careers portal.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N search pages of %d (test runs)" % PAGE_SIZE)
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (no category/description; faster)")
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
    offset, page_count, empty_pages = 0, 0, 0

    while True:
        if args.max_pages is not None and page_count >= args.max_pages:
            break
        jobs, total = fetch_search_page(session, offset)
        page_count += 1
        offset += PAGE_SIZE
        if jobs is None or not jobs:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        empty_pages = 0

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            posted = clean_text(job.get("PostedDate"))[:10]
            if posted and posted < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
            job_id = clean_text(job.get("Id"))
            if not job_id or job_id in known_ids:
                counters["duplicates"] += 1
                continue
            detail = {}
            if not args.no_details:
                detail = fetch_detail(session, job_id)
                if not detail:
                    counters["detail_failed"] += 1
            try:
                row = job_to_rich_row(job, detail)
            except Exception as exc:
                log.warning("Skipping malformed job %s: %s", job.get("Id"), exc)
                continue
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "site_category": row["site_category"]})
            known_ids.add(job_id)
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
    print("Excluded (out of scope): {:>3,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
