#!/usr/bin/env python3
"""Scrape job listings from the Fortis Healthcare careers portal.

Data source
-----------
www.fortishealthcare.com/careers-at-fortis is a landing page whose five
"Click Here to Apply" tiles (Clinicians, Nursing, Technicians, Medical
Support, Other Functions) all point at Fortis's Oracle Recruiting Cloud
(Candidate Experience) site:

    https://fa-ermg-saasfaprod1.fa.ocs.oraclecloud.com
        /hcmUI/CandidateExperience/en/sites/CX_1/requisitions

The fortishealthcare.com host sits behind a Cloudflare managed challenge,
but the Oracle host serves a plain public JSON REST API (its robots.txt is
a 404, i.e. no crawl restrictions) — §1 of the master spec, best case:

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.secondaryLocations      <- REQUIRED (see quirks)
        &finder=findReqs;siteNumber=CX_1,limit=25,offset=N,
                sortBy=POSTING_DATES_DESC

returns {items: [{TotalJobsCount, requisitionList: [job, ...]}]} with Id,
Title, PostedDate (YYYY-MM-DD), PrimaryLocation ("Mumbai, Maharashtra,
India") per job, newest first. Detail endpoint (fetched for NEW jobs only;
`--no-details` skips):

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="<Id>",siteNumber=CX_1

adds Category/JobFunction (CLINICIAN/NURSING/...), StudyLevel, JobSchedule,
ExternalPostedEndDate and the hiring facility (workLocation LocationName,
e.g. "FHL- Manesar").

Classification is the shared two-level taxonomy (scrappers/_shared/
classification.py): category "Non Clinical" | "Public Health" plus a
sub_category. Being a hospital chain's ATS, most requisitions (clinicians,
nursing, technicians) are out of scope and dropped (counted as
excluded_out_of_scope). The Fortis job-family / Category facet stays in the
rich CSV as raw source columns (job_family, category_source) and feeds the
classifier as its curated `skills` signal — it never decides the category.

Quirks
------
* The list endpoint returns an EMPTY requisitionList unless the
  `expand=requisitionList.secondaryLocations` parameter is present, and
  silently caps `limit` at 25.
* Salary is never exposed by the API -> always "Not Disclosed" (§3: no
  salary filter, nothing invented).
* All external description fields (ExternalDescriptionStr, qualifications,
  responsibilities) are empty strings in practice — Fortis does not fill
  them in; they are still captured when present.
* Their ATS category codes contain typos ("TECHINICIANS").

Outputs
-------
* fortis_jobs.csv                         — rich cumulative store
  (dedup key: Id)
* ../../jobs_csv/<DD-MM-YYYY>/fortis.csv  — HealthCareers.club 22-col schema

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap. The list is newest-first, so pagination
stops at the first page that is entirely older than the cutoff.

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

SITE = "fortis"
API_BASE = "https://fa-ermg-saasfaprod1.fa.ocs.oraclecloud.com"
SITE_NUMBER = "CX_1"
ROBOTS_URL = API_BASE + "/robots.txt"
LIST_URL_TMPL = (
    API_BASE + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList.secondaryLocations"
    "&finder=findReqs;siteNumber=" + SITE_NUMBER +
    ",limit={limit},offset={offset},sortBy=POSTING_DATES_DESC")
DETAIL_URL_TMPL = (
    API_BASE + "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    "?expand=all&onlyData=true&finder=ById;Id=%22{}%22,siteNumber=" + SITE_NUMBER)
JOB_URL_TMPL = (
    API_BASE + "/hcmUI/CandidateExperience/en/sites/" + SITE_NUMBER + "/job/{}")

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): the API exposes no salary at all.
INITIAL_WINDOW_DAYS = 7
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 25                  # server-side hard cap
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
MAX_PAGES_SAFETY = 200          # ~1,200 live jobs => ~48 pages; hard stop
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "fortis_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Job-family ids of the five tiles on careers-at-fortis (also returned as
# JobFamilyId by the detail API) -> human-readable family name. Raw source
# data for the rich CSV only — the category is decided by classify_job.
JOB_FAMILY_NAMES = {
    "300000787441924": "Clinicians",
    "300000787442029": "Nursing",
    "300000787442090": "Technicians",
    "300000787442096": "Medical Support",
    "300000787441932": "Other Functions",
}
# Detail-API Category codes / RequisitionType names, uppercased (typos are
# theirs; some requisitions carry only RequisitionType) -> same names.
CATEGORY_CODE_NAMES = {
    "CLINICIAN": "Clinicians",
    "CLINICIANS": "Clinicians",
    "NURSING": "Nursing",
    "TECHINICIANS": "Technicians",
    "TECHNICIANS": "Technicians",
    "MEDICAL SUPPORT": "Medical Support",
    "MEDICAL_SUPPORT": "Medical Support",
    "OTHER FUNCTIONS": "Other Functions",
    "OTHERS": "Other Functions",
}

COUNTRY_META = {
    "IN": ("India", "+91"),
    "AE": ("United Arab Emirates", "+971"),
    "LK": ("Sri Lanka", "+94"),
    "NP": ("Nepal", "+977"),
}

RICH_COLUMNS = [
    "source", "job_id", "requisition_id", "title", "company", "facility",
    "job_family", "category_source", "city", "state", "country",
    "country_code", "country_dial_code", "salary_raw", "salary_min",
    "salary_max", "salary_period", "salary_currency", "job_type",
    "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in", "company_type",
    "study_level", "min_experience", "max_experience", "needs_review",
    "posted_date", "closure_date", "description", "job_url", "scraped_at",
]

log = logging.getLogger("fortis_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The Fortis job-family name and raw Category code are the curated
    `skills` signal; the raw values stay in the rich CSV as source columns
    only. Returns in_scope — False means DROP the row
    (excluded_out_of_scope).
    """
    skills = " ".join(filter(None, [row.get("job_family", ""),
                                    row.get("category_source", "")]))
    verdict = classify_job(row.get("title", ""), skills,
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


def parse_location(raw):
    """"Mumbai, Maharashtra, India" -> (city, state); "Gurugram, India" ->
    ("Gurugram", "")."""
    parts = [clean_text(p) for p in str(raw or "").split(",") if clean_text(p)]
    if not parts:
        return "", ""
    city = parts[0]
    state = parts[1] if len(parts) > 2 else ""
    return city, state


def country_meta(country_code):
    code = clean_text(country_code).upper() or "IN"
    name, dial = COUNTRY_META.get(code, ("India", "+91"))
    return name, code if code in COUNTRY_META else "IN", dial


def family_name_for(job_family_id="", category_code=""):
    """Human-readable Fortis family name from either ATS identifier
    (rich-CSV source data only — never a category decision)."""
    key = clean_text(job_family_id)
    if key in JOB_FAMILY_NAMES:
        return JOB_FAMILY_NAMES[key]
    code = clean_text(category_code).upper()
    if code in CATEGORY_CODE_NAMES:
        return CATEGORY_CODE_NAMES[code]
    return ""


_PART_TIME_RE = re.compile(r"part.?time", re.IGNORECASE)


def parse_job_type(job_schedule):
    """Oracle JobSchedule ("Full time"/"Part time") -> club enum."""
    if _PART_TIME_RE.search(clean_text(job_schedule)):
        return "part_time"
    return "full_time"


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
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def check_robots(session):
    """The Oracle host has no robots.txt (plain 404) = no restrictions;
    abort only on an explicit disallow of our endpoints."""
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code >= 400:
            log.info("robots.txt -> HTTP %d (no robots file) — allowed",
                     resp.status_code)
            return
        rp.parse(resp.text.splitlines())
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LIST_URL_TMPL.format(limit=PAGE_SIZE, offset=0),
                JOB_URL_TMPL.format("0")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, url):
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


def fetch_list_page(session, offset):
    """One page of the job list. Returns (jobs, total_count); (None, None)
    on a failed request."""
    data = _request(session, LIST_URL_TMPL.format(limit=PAGE_SIZE, offset=offset))
    if not isinstance(data, dict) or not data.get("items"):
        return None, None
    item = data["items"][0]
    return item.get("requisitionList") or [], item.get("TotalJobsCount")


def fetch_detail(session, job_id):
    """Full requisition detail; returns the item dict or {}."""
    data = _request(session, DETAIL_URL_TMPL.format(job_id))
    if not isinstance(data, dict) or not data.get("items"):
        return {}
    return data["items"][0] or {}


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    job_id = clean_text(job.get("Id"))
    title = clean_text(job.get("Title"))
    city, state = parse_location(job.get("PrimaryLocation"))
    country_name, code, dial = country_meta(job.get("PrimaryLocationCountry"))
    family_name = family_name_for(category_code=job.get("JobFamily"))

    return {
        "source": SITE,
        "job_id": job_id,
        "requisition_id": "",
        "title": title,
        "company": "Fortis Healthcare",
        "facility": "",
        "job_family": family_name,
        "category_source": clean_text(job.get("JobFamily")),
        "city": city,
        "state": state,
        "country": country_name,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",  # the API never exposes salary
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": "full_time",        # detail enrich refines via JobSchedule
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": "hospital",     # hospital chain's own ATS
        "study_level": "",
        "min_experience": "",
        "max_experience": "",
        "needs_review": False,
        "posted_date": clean_text(job.get("PostedDate"))[:10],
        "closure_date": clean_text(job.get("PostingEndDate"))[:10],
        "description": strip_html(job.get("ShortDescriptionStr"))[:DESCRIPTION_MAX_CHARS],
        "job_url": JOB_URL_TMPL.format(job_id) if job_id else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, detail):
    """Merge detail-API extras into a rich row (in place)."""
    if not detail:
        return row
    row["requisition_id"] = clean_text(detail.get("RequisitionId"))
    family_name = family_name_for(
        detail.get("JobFamilyId"),
        detail.get("Category") or detail.get("RequisitionType"))
    if family_name:
        row["job_family"] = family_name
        row["category_source"] = clean_text(detail.get("Category")
                                            or detail.get("RequisitionType"))
    work_locations = detail.get("workLocation") or []
    if work_locations:
        row["facility"] = clean_text(work_locations[0].get("LocationName"))
        # The facility address is more precise than PrimaryLocation, which
        # is often just "India" or a bare state name.
        town = clean_text(work_locations[0].get("TownOrCity"))
        region = clean_text(work_locations[0].get("Region2"))
        if town:
            row["city"] = town
        if region:
            row["state"] = region
    if detail.get("JobSchedule"):
        row["job_type"] = parse_job_type(detail.get("JobSchedule"))
    row["study_level"] = clean_text(detail.get("StudyLevel"))
    if detail.get("WorkYears"):
        row["min_experience"] = clean_text(detail.get("WorkYears"))
    closure = clean_text(detail.get("ExternalPostedEndDate"))[:10]
    if closure:
        row["closure_date"] = closure
    description = " ".join(filter(None, [
        strip_html(detail.get("ExternalDescriptionStr")),
        strip_html(detail.get("ExternalResponsibilitiesStr")),
        strip_html(detail.get("ExternalQualificationsStr")),
    ]))
    if description:
        row["description"] = description[:DESCRIPTION_MAX_CHARS]
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
    company = _blank(r.get("company")) or "Fortis Healthcare"
    facility = _blank(r.get("facility"))
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
        "company_name": company,
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")) or
            (facility and "Hiring facility: {}".format(facility)) or "",
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        # structured source field (StudyLevel) first, else grounded
        # extraction from the description — never inferred
        "qualification": _blank(r.get("study_level"))
            or extract_qualification(_blank(r.get("description"))),
        "min_salary": "",               # never exposed by the source
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
        description="Scrape jobs from the Fortis Healthcare careers portal "
                    "(Oracle Recruiting Cloud).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N list pages of %d (test runs)" % PAGE_SIZE)
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (no facility/category "
                             "refinement; faster)")
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
    total_count = None

    # Newest-first list: stop at the first page entirely older than cutoff.
    while page_count < MAX_PAGES_SAFETY:
        if args.max_pages is not None and page_count >= args.max_pages:
            break
        jobs, total = fetch_list_page(session, offset)
        page_count += 1
        offset += PAGE_SIZE
        if total is not None:
            total_count = total
        if jobs is None:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        if not jobs:
            break
        empty_pages = 0

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:
                log.warning("Skipping malformed job %s: %s", job.get("Id"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if not args.no_details:
                detail = fetch_detail(session, row["job_id"])
                if detail:
                    apply_detail(row, detail)
                else:
                    counters["detail_failed"] += 1
            # Classify only after the detail fetch so the description and
            # refined job_family feed the shared classifier.
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "job_family": row["job_family"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        if page_all_old:
            log.info("Page %d entirely older than %s — stopping", page_count, cutoff)
            break
        if len(jobs) < PAGE_SIZE:
            break
        if total_count is not None and offset >= total_count:
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
