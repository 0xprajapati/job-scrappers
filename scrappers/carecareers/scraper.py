#!/usr/bin/env python3
"""Scrape job listings from carecareers.peoplestrong.com (Quality Care India).

Data source
-----------
carecareers.peoplestrong.com is the PeopleStrong (Alt Recruit) candidate
portal for Quality Care India Limited — the CARE Hospitals / KIMS Health /
CIIGMA hospital group. Being a hospital chain's own ATS, every posting is
healthcare-industry at the source (§2 of the master spec: filter at source).

The Angular SPA loads everything from a JSON API (robots.txt does not exist —
the server returns the SPA shell for it, i.e. no crawl restrictions):

    POST /api/cp/rest/altone/cp/jobs/v1?offset=N&limit=45   body: {}

which returns {totalRecords, response: [job, ...]} with jobTitle, jobCode,
requisitionId, jobPostedDate, jobClosureDate, organizationUnitComplete
(group>entity>department>sub-dept), locationHierarchyComplete
(country>state>city>site>...), expRange ("1-2 years"), openings and annual
INR budget salary (minBudgetSalary/maxBudgetSalary).

Detail endpoint (fetched for NEW jobs only; the index is tiny so this is
cheap; `--no-details` skips):

    GET /api/cp/rest/altone/cp/job/<ID>/v2?part=basic,organisational,
        descriprion,skill,qualification,language&isReqId=false

where <ID> is the jobCode with "/" -> "_" (e.g. QCI_P_1806731) — the same id
used by the public detail URL /job/detail/<ID>. It adds the full HTML
jobDescription, qualifications, languages and employmentType.

Quirks
------
* The listing is NOT sorted by date, but the whole index is only a few dozen
  jobs, so every run scans all pages and applies the time-window cutoff per
  job (no newest-first early stop).
* Salaries come as annual INR numbers (e.g. 216000.0); CTCRange is null in
  practice. 0/null budget values mean "Not Disclosed" — never invented.
* Titles contain typos ("Counsultant"); the shared classifier also receives
  the designation and the department segment of the org hierarchy (as the
  `skills` signal). Being a hospital ATS, most requisitions are clinical and
  now fall out of scope of the Non Clinical / Public Health taxonomy — they
  are dropped and counted as excluded_out_of_scope.

Outputs
-------
* carecareers_jobs.csv                         — rich cumulative store
  (dedup key: requisitionId)
* ../../jobs_csv/<DD-MM-YYYY>/carecareers.csv  — HealthCareers.club 22-col
  schema

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

SITE = "carecareers"
SITE_BASE = "https://carecareers.peoplestrong.com"
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

PAGE_SIZE = 45
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
MAX_PAGES_SAFETY = 200          # index is ~1 page today; hard stop regardless
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "carecareers_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# country name (lowercased) -> (ISO code, dial code); the group is Indian and
# every observed location starts with India, but keep the map extensible.
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
    "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in",
    "company_type", "skills", "qualifications",
    "min_experience", "max_experience", "openings", "needs_review",
    "posted_date", "closure_date", "description", "job_url", "scraped_at",
]

log = logging.getLogger("carecareers_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


def parse_budget_salary(min_budget, max_budget):
    """Annual INR budget figures from the API (e.g. 216000.0 / "240000").

    Returns {} when both are missing/zero (Not Disclosed — never invents
    values), else salary_raw plus club-schema numeric fields. Amounts are
    annual, so the period is always per_annum.
    """
    def to_int(v):
        try:
            n = int(round(float(v)))
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    lo, hi = to_int(min_budget), to_int(max_budget)
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


_EXP_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_exp_range(raw):
    """"1-2 years" / "5-7 years" -> (min, max) as strings; ("", "") if absent."""
    numbers = _EXP_RE.findall(clean_text(raw))
    if not numbers:
        return "", ""
    lo = str(int(float(numbers[0])))
    hi = str(int(float(numbers[1]))) if len(numbers) > 1 else lo
    return lo, hi


def classify_row(row):
    """Run the shared classifier over a built rich row (after any detail
    enrichment) and write the verdict back into it.

    The designation, department and source skill list are curated role
    signals, so they travel in the `skills` argument. Returns the verdict;
    in_scope False means the caller must DROP the row
    (counted excluded_out_of_scope)."""
    skills_signal = " | ".join(filter(None, [
        row.get("designation", ""), row.get("department", ""),
        row.get("skills", "")]))
    verdict = classify_job(row.get("title", ""), skills_signal,
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = verdict["needs_review"]
    return verdict


def split_hierarchy(raw):
    return [clean_text(p) for p in str(raw or "").split(">") if clean_text(p)]


def parse_location(raw):
    """"India>Maharashtra>Aurangabad>site>..." -> (country, state, city)."""
    parts = split_hierarchy(raw)
    country = parts[0] if len(parts) > 0 else ""
    state = parts[1] if len(parts) > 1 else ""
    city = parts[2] if len(parts) > 2 else state
    return country, state, city


def parse_org(raw):
    """"Group>Entity>Department>Sub-dept" -> (group, entity, department)."""
    parts = split_hierarchy(raw)
    group = parts[0] if len(parts) > 0 else ""
    entity = parts[1] if len(parts) > 1 else group
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
    for url in (LIST_URL, JOB_URL_TMPL.format("QCI_X_0")):
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
    country_name, code, dial = country_meta(
        parse_location(job.get("locationHierarchyComplete")
                       or job.get("locationHierarchy"))[0])
    _, state, city = parse_location(job.get("locationHierarchyComplete")
                                    or job.get("locationHierarchy"))
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
        "company": entity,
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
        # classification fields filled by classify_row() after detail enrich
        "category": "", "sub_category": "", "role_family": "",
        "all_families": "", "family_scores": "", "family_confidence": "",
        "matched_in": "",
        "company_type": "hospital",  # hospital group's own ATS
        "skills": "; ".join(clean_text(s) for s in skill_list[:15]),
        "qualifications": "",
        "min_experience": exp_min,
        "max_experience": exp_max,
        "openings": clean_text(job.get("openings")),
        "needs_review": False,
        "posted_date": clean_text(job.get("jobPostedDate"))[:10],
        "closure_date": clean_text(job.get("jobClosureDate"))[:10],
        "description": "",
        "job_url": JOB_URL_TMPL.format(detail_id) if detail_id else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    row.update(parse_budget_salary(job.get("minBudgetSalary"),
                                   job.get("maxBudgetSalary")))
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
        row.update(parse_budget_salary(detail.get("minBudgetSalary"),
                                       detail.get("maxBudgetSalary")))
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
    # Structured qualifications field when the detail API provided one, else
    # grounded extraction from the description — never inferred.
    qualification = _blank(r.get("qualifications")) or \
        extract_qualification(_blank(r.get("description")))
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
        "company_name": _blank(r.get("company")) or _blank(r.get("group_company")),
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        "qualification": qualification,
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
        description="Scrape jobs from carecareers.peoplestrong.com.")
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

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "duplicates": 0,
                "detail_failed": 0}
    new_rows, review_log = [], []
    offset, page_count, empty_pages = 0, 0, 0
    total_records = None

    # The list is unsorted, so scan the whole (tiny) index every run.
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
            verdict = classify_row(row)
            if not verdict["in_scope"]:
                counters["excluded_out_of_scope"] += 1
                continue
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
    print("Excluded out of scope: {:>5,}".format(counters["excluded_out_of_scope"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
