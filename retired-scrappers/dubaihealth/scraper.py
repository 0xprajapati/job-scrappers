#!/usr/bin/env python3
"""Scrape job listings of Dubai Health (dubaihealth.ae).

Data source
-----------
dubaihealth.ae hosts no job listings itself: its /careers page redirects to
**Dubai Careers** (the Dubai Government recruitment portal), where Dubai
Health is the employer/organization ``4105100456``. The portal is an Oracle
Taleo career section with a JSON search API:

    GET  https://jobs.dubaicareers.ae/careersection/dubaicareers/jobsearch.ftl
         (bootstrap: establishes the session cookie)
    POST https://jobs.dubaicareers.ae/careersection/rest/jobboard/searchjobs
         ?lang=en&portal=40505100456
         body: advanced-search filter ORGANIZATION=4105100456, sorted by
         posting date descending (newest first -> watermark early stop).

The listing rows carry only title / posting date / nationality, so each NEW
job's ``jobdetail.ftl?job=<jobNumber>`` page is fetched (details on by
default) and its serialized Taleo state blob parsed for: description,
department, education, contract type, job level, required nationality,
monthly salary and schedule (full/part time) plus the unposting date.

Classification is the shared two-level taxonomy (scrappers/_shared/
classification.py): ``category`` is "Non Clinical" | "Public Health" plus a
``sub_category``. The source filter (organization = Dubai Health) is a
crawl-side saver only — it makes every row healthcare-sector, but a Dubai
Health board is mostly bedside/clinical, so most requisitions are dropped as
``excluded_out_of_scope``. The Taleo detail ``department`` is passed to the
classifier as its curated ``skills`` signal and stays in the rich CSV as a
raw source column; it never decides the category itself. Jobs are classified
AFTER the detail fetch so the description and department can be scored.

Salaries are in **AED**; the shared HealthCareers.club schema only allows
INR/USD, so AED amounts stay in the rich CSV and the club CSV salary fields
are left blank (no invented conversion). Most postings show "Unspecified"
-> ``salary_raw = "Not Disclosed"``.

Outputs
-------
* dubaihealth_jobs.csv                — rich cumulative store (dedup: job_id
                                        = Taleo job number, e.g. 26000730).
* ../../jobs_csv/<DD-MM-YYYY>/dubaihealth.csv — HealthCareers.club schema.

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

Run ``python scraper.py --help`` for options.
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
from urllib.parse import unquote

import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "dubaihealth"
JOBS_BASE = "https://jobs.dubaicareers.ae"
ROBOTS_URL = JOBS_BASE + "/robots.txt"
CAREER_SECTION = "dubaicareers"
SEARCH_PAGE_URL = "{}/careersection/{}/jobsearch.ftl".format(JOBS_BASE, CAREER_SECTION)
SEARCH_API_URL = JOBS_BASE + "/careersection/rest/jobboard/searchjobs"
DETAIL_URL_TMPL = ("{}/careersection/{}/jobdetail.ftl?job={{}}&lang=en"
                   .format(JOBS_BASE, CAREER_SECTION))
PORTAL_ID = "40505100456"          # career-section portal code (from the FTL page)
ORGANIZATION_ID = "4105100456"     # Dubai Health employer id on Dubai Careers

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "dubaihealth_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COMPANY_NAME = "Dubai Health"
COUNTRY_NAME = "United Arab Emirates"
COUNTRY_CODE = "AE"
COUNTRY_DIAL = "+971"
DEFAULT_CITY = "Dubai"

RICH_COLUMNS = [
    "source", "job_id", "taleo_req_id", "title", "company", "department",
    "city", "country", "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_currency_original", "salary_period", "job_type", "contract_type",
    "job_level", "education", "nationality_requirement", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in", "company_type", "needs_review",
    "posted_date", "expires_at", "description", "job_url", "scraped_at",
]

log = logging.getLogger("dubaihealth_scraper")

# ----------------------------------------------------------------------------
# Small text / date helpers
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_DDMMYYYY_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def ddmmyyyy_to_iso(value):
    """'21/07/2026' -> '2026-07-21'; '' for anything unparseable."""
    value = (value or "").strip()
    if not _DDMMYYYY_RE.match(value):
        return ""
    d, m, y = value.split("/")
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return ""


def decode_taleo_text(raw):
    """Decode a value from the Taleo serialized state: percent-encoded HTML
    with ':' escaped as '\\:'. Returns clean plain text."""
    text = unquote(raw or "").replace("\\:", ":")
    return clean_text(_TAG_RE.sub(" ", text))


# ----------------------------------------------------------------------------
# Classification (shared two-level taxonomy)
# ----------------------------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The Taleo ``department`` field is the curated `skills` signal; the raw
    value stays in the rich CSV as a source column only.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("department", ""),
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
# Salary (AED, from the "Monthly Salary" detail field)
# ----------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d[\d,\.]*")
_UNDISCLOSED_RE = re.compile(r"^(unspecified|not disclosed|n/?a|-)?$", re.IGNORECASE)


def parse_salary(raw):
    """Return (salary_raw, min_monthly, max_monthly, currency).

    The portal field is 'Monthly Salary'; most Dubai Health postings show
    'Unspecified'. When numbers are present they are AED per month."""
    raw = (raw or "").strip()
    if _UNDISCLOSED_RE.match(raw):
        return ("Not Disclosed", "", "", "")
    nums = []
    for m in _NUM_RE.finditer(raw):
        try:
            nums.append(int(float(m.group(0).replace(",", ""))))
        except ValueError:
            continue
    if not nums:
        return (raw, "", "", "")
    return (raw, str(min(nums)), str(max(nums)), "AED")


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        # Taleo's REST layer expects the caller's timezone headers.
        "tz": "GMT+04:00",
        "tzname": "Asia/Dubai",
    })
    return s


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        # jobs.dubaicareers.ae has no robots.txt (404) -> everything allowed.
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    if not rp.can_fetch(USER_AGENT, SEARCH_API_URL):
        sys.exit("robots.txt disallows the job search API — aborting.")
    log.info("robots.txt check passed")


def _request(session, method, url, *, params=None, json_body=None, as_json=True):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url, params=params, json=json_body,
                                   timeout=REQUEST_TIMEOUT_SECONDS)
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


def bootstrap_session(session):
    """GET the career-section search page once so Taleo issues the session
    cookie the REST API requires (a cookie-less POST returns HTTP 500)."""
    page = _request(session, "GET", SEARCH_PAGE_URL,
                    params={"lang": "en"}, as_json=False)
    if page is None:
        sys.exit("Could not load the Dubai Careers search page — aborting.")


def fetch_search_page(session, page_no):
    body = {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3",   # posting date
                             "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": "", "LOCATION": "",
                                 "ORGANIZATION": ORGANIZATION_ID},
                      "valid": True},
        "filterSelectionParam": {"searchFilterSelections": [
            {"id": "POSTING_DATE", "selectedValues": []},
            {"id": "LOCATION", "selectedValues": []},
            {"id": "JOB_FIELD", "selectedValues": []},
        ]},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
            {"id": "ORGANIZATION", "selectedValues": [ORGANIZATION_ID]},
            {"id": "LOCATION", "selectedValues": []},
            {"id": "JOB_FIELD", "selectedValues": []},
            {"id": "URGENT_JOB", "selectedValues": []},
            {"id": "EMPLOYEE_STATUS", "selectedValues": []},
            {"id": "STUDY_LEVEL", "selectedValues": []},
            {"id": "WILL_TRAVEL", "selectedValues": []},
            {"id": "JOB_SHIFT", "selectedValues": []},
        ]},
        "pageNo": page_no,
    }
    return _request(session, "POST", SEARCH_API_URL,
                    params={"lang": "en", "portal": PORTAL_ID}, json_body=body)


# ----------------------------------------------------------------------------
# Job detail parsing (serialized Taleo state in jobdetail.ftl)
# ----------------------------------------------------------------------------
#
# The detail page embeds one big <input value="..."> blob:
#   <header tokens> !*! <description html> !*! <description html (bis)>
#   !*! <qualifications html> !*! <qualifications html (bis)> !|! <field tokens>
# sections are separated by "!*!", tokens by "!|!", top-level interfaces by
# "!%24!" ("!$!"). Field tokens (last section) follow the career section's
# configured field list, each as a (value, localized value) pair:
#   [qual-html, department x2, organization x2, '' x2, education x2,
#    contract-type + '', job-type + '', job-level + '', nationality x2,
#    people-of-determination x2, monthly-salary x2, schedule x2,
#    posting-date x2, unposting-date x2, ...]

_STATE_INPUT_RE = re.compile(r'value="([^"]*!\*![^"]*)"')

_SCHEDULE_MAP = {"full time": "full_time", "part time": "part_time"}

# Offsets into the last "!*!" section, relative to its leading HTML token.
_IDX_DEPARTMENT = 1
_IDX_EDUCATION = 7
_IDX_CONTRACT = 9
_IDX_JOB_LEVEL = 13


def parse_detail_html(page_html):
    """Extract the useful fields from a jobdetail.ftl page. Every field is
    best-effort: a template change degrades single fields, never the run."""
    out = {}
    m = _STATE_INPUT_RE.search(page_html or "")
    if not m:
        return out
    # requisition data = the "!$!" interface that carries the "!*!" HTML
    # chunks (the blob starts with header/pager interfaces without them)
    data = next((p for p in m.group(1).split("!%24!") if "!*!" in p), "")
    if not data:
        return out
    sections = data.split("!*!")
    if len(sections) >= 4:
        desc = decode_taleo_text(re.sub(r"!\|!$", "", sections[1]))
        qual = decode_taleo_text(re.sub(r"!\|!$", "", sections[3]))
        joined = " ".join(p for p in (desc, qual and "Qualifications: " + qual) if p)
        out["description"] = joined[:DESCRIPTION_MAX_CHARS]

    tail = sections[-1].split("!|!") if sections else []
    fields = [decode_taleo_text(t) for t in tail]

    def field_at(idx):
        return fields[idx] if 0 < idx < len(fields) and len(fields[idx]) < 120 else ""

    out["department"] = field_at(_IDX_DEPARTMENT)
    out["education"] = field_at(_IDX_EDUCATION)
    out["contract_type"] = field_at(_IDX_CONTRACT)
    out["job_level"] = field_at(_IDX_JOB_LEVEL)

    # Schedule: first token that is a known schedule value; the monthly-salary
    # pair sits immediately before it.
    for i, val in enumerate(fields):
        job_type = _SCHEDULE_MAP.get(val.lower())
        if job_type:
            out["job_type"] = job_type
            if i >= 2 and fields[i - 1] == fields[i - 2]:
                out["salary_field"] = fields[i - 1]
            break

    # Dates: posting date appears first (x2), unposting date after it (x2).
    dates = [ddmmyyyy_to_iso(v) for v in fields if _DDMMYYYY_RE.match(v)]
    if dates:
        out["detail_posted_date"] = dates[0]
        if len(dates) > 2 and dates[-1] != dates[0]:
            out["expires_at"] = dates[-1]
    return out


def fetch_job_detail(session, job_number):
    page_html = _request(session, "GET", DETAIL_URL_TMPL.format(job_number),
                         as_json=False)
    return parse_detail_html(page_html) if page_html else {}


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def listing_to_rich_row(req):
    """Build a row from one searchjobs requisition entry.
    column = [title, posting date DD/MM/YYYY, required nationality]."""
    column = req.get("column") or []
    title = clean_text(column[0] if len(column) > 0 else "")
    job_number = str(req.get("contestNo") or "").strip()
    return {
        "source": SITE,
        "job_id": job_number,
        "taleo_req_id": str(req.get("jobId") or ""),
        "title": title,
        "company": COMPANY_NAME,
        "department": "",
        "city": DEFAULT_CITY,
        "country": COUNTRY_NAME,
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_currency_original": "",
        "salary_period": "",
        "job_type": "full_time",
        "contract_type": "",
        "job_level": "",
        "education": "",
        "nationality_requirement": clean_text(column[2] if len(column) > 2 else ""),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": "hospital",
        "needs_review": False,
        "posted_date": ddmmyyyy_to_iso(column[1] if len(column) > 1 else ""),
        "expires_at": "",
        "description": "",
        "job_url": DETAIL_URL_TMPL.format(job_number),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, detail):
    for key in ("description", "department", "education", "contract_type",
                "job_level", "job_type", "expires_at"):
        if detail.get(key):
            row[key] = detail[key]
    if detail.get("salary_field"):
        raw, lo, hi, cur = parse_salary(detail["salary_field"])
        row.update({"salary_raw": raw, "salary_min_monthly": lo,
                    "salary_max_monthly": hi, "salary_currency_original": cur,
                    "salary_period": "per_month" if cur else ""})
    return row


def rich_row_to_club_row(r):
    return {
        "country_name": r.get("country", COUNTRY_NAME),
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": r.get("city", DEFAULT_CITY),
        "company_name": r.get("company", COMPANY_NAME),
        "company_type": r.get("company_type", "hospital"),
        "company_logo": "",
        "company_about": "",
        "title": r.get("title", ""),
        "description": r.get("description", ""),
        "job_type": r.get("job_type", "full_time"),
        "category": r.get("category", ""),
        "sub_category": r.get("sub_category", ""),
        "application_url": r.get("job_url", ""),
        "posted_at": r.get("posted_date", ""),
        "min_experience": "",
        "max_experience": "",
        # structured source field (Taleo education level) first, else
        # grounded extraction from the description — never inferred
        "qualification": (r.get("education", "")
                          or extract_qualification(r.get("description", ""))),
        # Salary is AED (not in the club INR/USD enum) -> left blank on
        # purpose; the raw AED amount is preserved in the rich CSV.
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
    }


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
        description="Scrape Dubai Health jobs from the Dubai Careers portal.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N search pages (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip per-job detail pages (description, salary, "
                             "expiry, department). Details are ON by default "
                             "because the listing rows carry title/date only.")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)
    bootstrap_session(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "duplicates": 0,
                "detail_failed": 0}
    new_rows, review_log = [], []
    page_no, total_count = 1, None

    while True:
        if args.max_pages is not None and page_no > args.max_pages:
            break
        result = fetch_search_page(session, page_no)
        if not result:
            break
        requisitions = result.get("requisitionList") or []
        if not requisitions:
            break
        paging = result.get("pagingData") or {}
        total_count = paging.get("totalCount", total_count)

        page_all_old = True
        for req in requisitions:
            counters["scanned"] += 1
            try:
                row = listing_to_rich_row(req)
            except Exception as exc:
                log.warning("Skipping malformed requisition %s: %s",
                            req.get("contestNo"), exc)
                continue
            if row["posted_date"] and row["posted_date"] >= cutoff:
                page_all_old = False
            else:
                counters["excluded_old"] += 1
                continue
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if not args.no_details:
                detail = fetch_job_detail(session, row["job_id"])
                if detail:
                    row = apply_detail(row, detail)
                else:
                    counters["detail_failed"] += 1
            # classify only after the detail fetch, so department and
            # description can be scored alongside the title
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

        if page_all_old:
            log.info("Page %d entirely older than %s — stopping", page_no, cutoff)
            break
        page_size = paging.get("pageSize") or len(requisitions)
        if total_count is not None and page_no * page_size >= int(total_count):
            break
        page_no += 1

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
