#!/usr/bin/env python3
"""Scrape job listings from maxhealthcarecareers.peoplestrong.com.

Data source
-----------
https://www.maxhealthcare.in/careers redirects to
maxhealthcarecareers.peoplestrong.com — the PeopleStrong (Alt Recruit)
candidate portal for Max Healthcare Institute Ltd and its group entities
(Alps Hospital, Starlit Medical Centre, Nanavati Max, BLK-Max, Max Lab,
Max@Home, ...). Being a hospital chain's own ATS, every posting is
healthcare-industry at the source (§2 of the master spec: filter at source).

Same platform as the carecareers scraper: the Angular SPA loads everything
from a JSON API (robots.txt is the SPA shell, i.e. no crawl restrictions;
the corporate www.maxhealthcare.in robots.txt does not cover this host):

    POST /api/cp/rest/altone/cp/jobs/v1?offset=N&limit=45   body: {}

which returns {totalRecords, response: [job, ...]} with jobTitle, jobCode,
requisitionId, jobPostedDate, jobClosureDate, organizationUnitComplete
(MHC>entity>department>sub-dept), locationHierarchyComplete
(India>STATE>city>...>site), expRange ("1-3 years"), openings and CTCRange.

Detail endpoint (fetched for NEW jobs only; `--no-details` skips):

    GET /api/cp/rest/altone/cp/job/<ID>/v2?part=basic,organisational,
        descriprion,skill,qualification,language&isReqId=false

where <ID> is the jobCode with "/" -> "_" (e.g. MHC_28734) — the same id
used by the public detail URL /job/detail/<ID>. It adds the full HTML
jobDescription, qualifications, minSalary/maxSalary and employmentType.

Quirks
------
* Salaries come in a MIXED unit: CTCRange "200000.0000-400000.0000" is
  absolute annual INR, but "13.0000-15.0000" means 13-15 lakhs per annum
  (the detail API mirrors this: minSalary "13" / maxSalary "15"). Values
  below LAKH_THRESHOLD are treated as lakhs (x 100,000). 0/absent means
  "Not Disclosed" — never invented.
* minBudgetSalary/maxBudgetSalary are 0 in practice; CTCRange (list) and
  minSalary/maxSalary (detail) carry the real numbers.
* States come ALL-CAPS ("UTTAR PRADESH") and are title-cased.
* Entity names carry legal boilerplate ("Alps Hospital Limited (Formerly
  known as ...)"); the parenthetical is stripped.
* The listing is roughly newest-first but tiny (~66 jobs), so every run
  scans all pages and applies the time-window cutoff per job.

Outputs
-------
* maxhealthcare_jobs.csv                         — rich cumulative store
  (dedup key: requisitionId)
* ../../jobs_csv/<DD-MM-YYYY>/maxhealthcare.csv  — HealthCareers.club
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

SITE = "maxhealthcare"
SITE_BASE = "https://maxhealthcarecareers.peoplestrong.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
LIST_URL = SITE_BASE + "/api/cp/rest/altone/cp/jobs/v1"
DETAIL_URL_TMPL = (
    SITE_BASE + "/api/cp/rest/altone/cp/job/{}/v2"
    "?part=basic,organisational,descriprion,skill,qualification,language"
    "&isReqId=false")
JOB_URL_TMPL = SITE_BASE + "/job/detail/{}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

# CTC figures below this are lakhs-per-annum ("13" == 1,300,000 INR);
# at/above they are absolute annual INR ("200000" == 200,000 INR).
LAKH_THRESHOLD = 1_000

PAGE_SIZE = 45
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
MAX_PAGES_SAFETY = 200          # index is ~2 pages today; hard stop regardless
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "maxhealthcare_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# country name (lowercased) -> (ISO code, dial code); Max Healthcare is
# Indian and every observed location starts with India.
COUNTRY_META = {
    "india": ("IN", "+91"),
    "united arab emirates": ("AE", "+971"),
    "saudi arabia": ("SA", "+966"),
    "qatar": ("QA", "+974"),
    "oman": ("OM", "+968"),
}

RICH_COLUMNS = [
    "source", "job_id", "job_code", "detail_id", "title", "designation",
    "company", "group_company", "department", "city", "state", "country",
    "country_code", "country_dial_code", "salary_raw", "salary_min",
    "salary_max", "salary_period", "salary_currency", "job_type",
    "category", "company_type", "skills", "qualifications",
    "min_experience", "max_experience", "openings", "needs_review",
    "posted_date", "closure_date", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("maxhealthcare_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


def _normalize_annual(value):
    """One CTC figure -> annual INR int, or None.

    The API mixes units: "13" means 13 lakhs (1,300,000), "200000" means
    200,000. Values below LAKH_THRESHOLD are lakhs. 0/absent -> None.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n < LAKH_THRESHOLD:
        n *= 100_000
    return int(round(n))


def parse_salary_pair(min_value, max_value):
    """minSalary/maxSalary style values (mixed lakh/absolute units).

    Returns {} when both are missing/zero (Not Disclosed — never invents
    values), else salary_raw plus club-schema numeric fields. Amounts are
    annual, so the period is always per_annum.
    """
    lo, hi = _normalize_annual(min_value), _normalize_annual(max_value)
    if lo is None and hi is None:
        return {}
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if hi < lo:
        lo, hi = hi, lo
    return {
        "salary_raw": "INR {:,} - {:,} per year".format(lo, hi),
        "salary_min": lo, "salary_max": hi,
        "salary_period": "per_annum", "salary_currency": "INR",
    }


def parse_ctc_range(raw):
    """CTCRange "200000.0000-400000.0000" / "13.0000-15.0000" -> salary dict.

    Same mixed-unit rule as parse_salary_pair; {} when absent/zero.
    """
    parts = clean_text(raw).split("-")
    if len(parts) != 2:
        return {}
    return parse_salary_pair(parts[0], parts[1])


_EXP_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_exp_range(raw):
    """"1-3 years" / "12-18 years" -> (min, max) as strings; ("", "") if absent."""
    numbers = _EXP_RE.findall(clean_text(raw))
    if not numbers:
        return "", ""
    lo = str(int(float(numbers[0])))
    hi = str(int(float(numbers[1]))) if len(numbers) > 1 else lo
    return lo, hi


_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"consultant|coun?sultant|intensivist|hospitalist|[a-z]+ologist|"
    r"anaesthetist|anesthetist|obstetrician|p(a?)ediatrician|psychiatrist|"
    r"registrar|\bdnb\b|\bdmo\b|attending|senior resident|junior resident",
    re.IGNORECASE)

# Consultant/registrar-style titles are doctors only when the department is
# clinical; a "Consultant - HR" must not become a doctor.
_CLINICAL_DEPT_RE = re.compile(
    r"clinical|medical|nursing|surg|cardi|ortho|neuro|onco|uro|gastro|"
    r"nephro|derma|paedia|pedia|gyn|radiol|patholog|anaesth|anesth|icu|"
    r"emergency|internal medicine|critical care|pulmo|respiratory|"
    r"nuclear medicine|hepato|endocrin|rheumat|haemat|hemat",
    re.IGNORECASE)
_DOCTOR_TITLE_NEEDS_DEPT_RE = re.compile(
    r"consultant|coun?sultant|registrar", re.IGNORECASE)

_NON_CLINICAL_TITLE_RE = re.compile(
    r"business development|\bsales\b|marketing|tele ?call|receptionist|"
    r"accountant|\bhr\b|\badmin|executive|manager|officer|billing|"
    r"housekeep|security|logistic|store|purchase|\bit\b|engineer",
    re.IGNORECASE)

# Healthcare signal for needs_review flagging (the site itself is a hospital
# chain, so nothing is dropped — generic corporate titles with no healthcare
# word in title or department are only flagged).
_HEALTHCARE_SIGNAL_RE = re.compile(
    r"medical|pharma|health|nurs|doctor|clinic|hospital|\blab\b|laborator|"
    r"diagnost|patient|dental|surgi|therap|physio|radiol|patholog|dialysis|"
    r"icu|ward|emergency|paramedic|technician|cath|blood ?bank|cssd|"
    r"anaesth|anesth|biomedical|onco|cardi|neuro|nephro|paedia|pedia|"
    r"pulmo|respiratory|critical care|nuclear medicine",
    re.IGNORECASE)


def classify_category(title, designation="", department=""):
    """Map to the club category enum (doctors|nurses|pharmacists|
    non_clinical). Returns (category, needs_review)."""
    text = " ".join(filter(None, [title, designation]))
    if _NURSE_RE.search(text) or _NURSE_RE.search(department or ""):
        category = "nurses"
    elif _PHARMACIST_RE.search(text):
        category = "pharmacists"
    elif _DOCTOR_RE.search(text):
        if (_DOCTOR_TITLE_NEEDS_DEPT_RE.search(text)
                and not _CLINICAL_DEPT_RE.search(department or "")
                and _NON_CLINICAL_TITLE_RE.search(text)):
            category = "non_clinical"
        else:
            category = "doctors"
    else:
        category = "non_clinical"
    haystack = " ".join(filter(None, [title, designation, department]))
    needs_review = not _HEALTHCARE_SIGNAL_RE.search(haystack) and \
        category == "non_clinical"
    return category, needs_review


def split_hierarchy(raw):
    return [clean_text(p) for p in str(raw or "").split(">") if clean_text(p)]


def _title_case_state(state):
    return " ".join(w.capitalize() for w in state.split()) if state.isupper() \
        else state


def parse_location(raw):
    """"India>HARYANA>Gurugram>...>site" -> (country, state, city)."""
    parts = split_hierarchy(raw)
    country = parts[0] if len(parts) > 0 else ""
    state = _title_case_state(parts[1]) if len(parts) > 1 else ""
    city = parts[2] if len(parts) > 2 else state
    return country, state, city


_FORMERLY_RE = re.compile(r"\s*\((formerly|erstwhile)[^)]*\)", re.IGNORECASE)


def parse_org(raw):
    """"MHC>Entity (Formerly ...)>Department>Sub-dept" ->
    (group, entity, department). Legal parentheticals are stripped."""
    parts = split_hierarchy(raw)
    group = parts[0] if len(parts) > 0 else ""
    entity = _FORMERLY_RE.sub("", parts[1]).strip() if len(parts) > 1 else group
    department = " - ".join(parts[2:4]) if len(parts) > 2 else ""
    return group, entity, department


def country_meta(country):
    key = clean_text(country).lower()
    if key in COUNTRY_META:
        code, dial = COUNTRY_META[key]
        return clean_text(country), code, dial
    return "India", "IN", "+91"


def detail_id_from_job(job):
    """The detail-API id: last URL segment, else jobCode with "/" -> "_"."""
    url = clean_text(job.get("jobDetailUrl"))
    if url:
        return url.rstrip("/").rsplit("/", 1)[-1]
    code = clean_text(job.get("jobCode"))
    return code.replace("/", "_") if code else ""


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
    """The server has no real robots.txt (it answers with the SPA's HTML
    shell); parse whatever comes back and abort only on an explicit
    disallow of our endpoints."""
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        body = resp.text if resp.status_code < 400 else ""
        if "<html" in body[:500].lower():
            log.info("robots.txt is the SPA shell (no robots file) — allowed")
            return
        rp.parse(body.splitlines())
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LIST_URL, JOB_URL_TMPL.format("MHC_0")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, method, url, *, payload=None, params=None):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url, json=payload, params=params,
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


def fetch_list_page(session, offset):
    """One page of the job list. Returns (jobs, total_records)."""
    data = _request(session, "POST", LIST_URL, payload={},
                    params={"offset": offset, "limit": PAGE_SIZE})
    if not isinstance(data, dict):
        return None, None
    return data.get("response") or [], data.get("totalRecords")


def fetch_detail(session, detail_id):
    """Full job detail; returns the response dict or {}."""
    data = _request(session, "GET", DETAIL_URL_TMPL.format(detail_id))
    if not isinstance(data, dict):
        return {}
    return data.get("response") or {}


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    title = clean_text(job.get("jobTitle"))
    designation = clean_text(job.get("designation"))
    group, entity, department = parse_org(job.get("organizationUnitComplete")
                                          or job.get("organizationUnit"))
    location_raw = job.get("locationHierarchyComplete") or job.get("locationHierarchy")
    country, state, city = parse_location(location_raw)
    country_name, code, dial = country_meta(country)
    category, needs_review = classify_category(title, designation, department)
    detail_id = detail_id_from_job(job)
    exp_min, exp_max = parse_exp_range(job.get("expRange"))
    skills = job.get("skills") or {}
    skill_list = list(skills.get("mustTohave") or []) + \
        list(skills.get("goodtohave") or [])

    row = {
        "source": SITE,
        "job_id": clean_text(job.get("requisitionId")) or detail_id,
        "job_code": clean_text(job.get("jobCode")),
        "detail_id": detail_id,
        "title": title or designation,
        "designation": designation,
        "company": entity or "Max Healthcare",
        "group_company": group,
        "department": department,
        "city": city,
        "state": state,
        "country": country_name,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": "full_time",  # list API has no employment-type field;
                                  # detail enrich refines it
        "category": category,
        "company_type": "hospital",  # hospital group's own ATS
        "skills": "; ".join(clean_text(s) for s in skill_list[:15]),
        "qualifications": "",
        "min_experience": exp_min,
        "max_experience": exp_max,
        "openings": clean_text(job.get("openings")),
        "needs_review": needs_review,
        "posted_date": clean_text(job.get("jobPostedDate"))[:10],
        "closure_date": clean_text(job.get("jobClosureDate"))[:10],
        "description": "",
        "job_url": JOB_URL_TMPL.format(detail_id) if detail_id else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    salary = parse_ctc_range(job.get("CTCRange"))
    if not salary:
        salary = parse_salary_pair(job.get("minBudgetSalary"),
                                   job.get("maxBudgetSalary"))
    row.update(salary)
    return row


_PART_TIME_RE = re.compile(r"part.?time", re.IGNORECASE)
_CONTRACT_RE = re.compile(r"contract|consultant.?retainer|fixed.?term|retainer",
                          re.IGNORECASE)


def apply_detail(row, detail):
    """Merge detail-API extras into a rich row (in place)."""
    if not detail:
        return row
    description = strip_html(detail.get("jobDescription") or "")
    if description:
        row["description"] = description[:DESCRIPTION_MAX_CHARS]
    quals = detail.get("qualifications") or []
    if quals:
        row["qualifications"] = "; ".join(clean_text(q) for q in quals[:10])
    employment = clean_text(detail.get("employmentType"))
    if employment:
        if _PART_TIME_RE.search(employment):
            row["job_type"] = "part_time"
        elif _CONTRACT_RE.search(employment):
            row["job_type"] = "contract"
    if not row.get("salary_min"):
        salary = parse_salary_pair(detail.get("minSalary"),
                                   detail.get("maxSalary"))
        if not salary:
            salary = parse_salary_pair(detail.get("minBudgetSalary"),
                                       detail.get("maxBudgetSalary"))
        row.update(salary)
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
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
        "company_name": _blank(r.get("company")) or "Max Healthcare",
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")) or "non_clinical",
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
        "is_active": "true",
        "expires_at": _blank(r.get("closure_date")),
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
        description="Scrape jobs from maxhealthcarecareers.peoplestrong.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N list pages of %d (test runs)" % PAGE_SIZE)
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (no descriptions; faster)")
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
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    offset, page_count, empty_pages = 0, 0, 0
    total_records = None

    # The index is tiny, so scan all pages every run and cut per job.
    while page_count < MAX_PAGES_SAFETY:
        if args.max_pages is not None and page_count >= args.max_pages:
            break
        jobs, total = fetch_list_page(session, offset)
        page_count += 1
        offset += PAGE_SIZE
        if total is not None:
            total_records = total
        if jobs is None:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        if not jobs:
            break
        empty_pages = 0

        for job in jobs:
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:
                log.warning("Skipping malformed job %s: %s",
                            job.get("requisitionId"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if not args.no_details and row["detail_id"]:
                detail = fetch_detail(session, row["detail_id"])
                if detail:
                    apply_detail(row, detail)
                else:
                    counters["detail_failed"] += 1
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "department": row["department"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        if len(jobs) < PAGE_SIZE:
            break
        if total_records is not None and offset >= total_records:
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
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
