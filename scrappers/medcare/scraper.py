#!/usr/bin/env python3
"""Scrape job openings from medcare.ae (Medcare Hospitals & Medical Centres, UAE).

Data source
-----------
www.medcare.ae/en/careers.html is a static TYPO3 page whose "Current Openings"
button points at the **Oracle Recruiting Cloud** candidate portal of the parent
group (Aster DM Healthcare):

    https://hcdt.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX

That portal exposes the standard public Oracle CE REST API. The group tenant
lists ~2,900 requisitions across Aster brands in India/GCC; Medcare's own jobs
are isolated with the ORGANIZATIONS facet
(`selectedOrganizationsFacet=300003648617854` = "Medcare Medical Hospitals and
Medical Centres"):

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber=CX,sortBy=POSTING_DATES_DESC,
                limit=N,offset=N,selectedOrganizationsFacet=<org-id>

Per-requisition category / schedule / full HTML description come from the
detail endpoint (fetched for NEW jobs only; on by default):

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="<id>",siteNumber=CX

Salary is never exposed by this portal -> `salary_raw = "Not Disclosed"`.
Everything the org posts is healthcare-sector employment (it's a hospital
operator), so the healthcare filter reduces to mapping Oracle's category
facet (Nursing / Clinicians / Paramedical / ...) onto the club category enum,
with a title-keyword fallback; unmappable titles are kept + `needs_review`.

Time window: an ATS only lists OPEN requisitions (Medcare keeps ~21 live, some
posted years ago), so unlike news-feed sources the first run keeps ALL open
jobs (`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
(newest stored posted_date minus WATERMARK_GRACE_DAYS).

Outputs
-------
* medcare_jobs.csv                    — rich cumulative store (dedup: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/medcare.csv — HealthCareers.club 22-col schema.

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

SITE = "medcare"
CAREERS_PAGE = "https://www.medcare.ae/en/careers.html"

ORACLE_BASE = "https://hcdt.fa.us2.oraclecloud.com"
SITE_NUMBER = "CX"
MEDCARE_ORG_ID = "300003648617854"  # ORGANIZATIONS facet: "Medcare Medical Hospitals and Medical Centres"
LIST_URL = ORACLE_BASE + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL_URL = ORACLE_BASE + "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
JOB_URL_TMPL = ORACLE_BASE + "/hcmUI/CandidateExperience/en/sites/" + SITE_NUMBER + "/job/{}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# ATS lists only OPEN requisitions, so the first run keeps everything
# (None = no initial window); later runs use the watermark (master spec §4).
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

PER_PAGE = 100
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000
COMPANY_ABOUT_MAX_CHARS = 1_000

RICH_CSV = "medcare_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# UAE hospital group — country fixed. No salary data on the portal.
COMPANY_NAME = "Medcare Hospitals and Medical Centres"
# used when a run fetches no details (the live org blurb overrides it)
COMPANY_ABOUT_FALLBACK = (
    "Medcare Hospitals and Medical Centres (Medcare) is the premium healthcare "
    "division of Aster DM Healthcare, operating multi-speciality hospitals and "
    "medical centres across Dubai and Sharjah, United Arab Emirates.")
COUNTRY_NAME = "United Arab Emirates"
COUNTRY_CODE = "AE"
COUNTRY_DIAL = "+971"
DEFAULT_CITY = "Dubai"

RICH_COLUMNS = [
    "source", "job_id", "title", "raw_title", "company", "facility",
    "department", "city", "country",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "category", "category_original",
    "needs_review", "posted_date", "expires_at", "description", "job_url",
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

log = logging.getLogger("medcare_scraper")

# ----------------------------------------------------------------------------
# Text / title parsing
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_BR_PAREN_RE = re.compile(r"\s*\((br|branch)\b.*$", re.IGNORECASE)
_FACILITY_RE = re.compile(r"medcare|wellth|hospital|centre|center|clinic",
                          re.IGNORECASE)


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def strip_html(text):
    return clean_text(_TAG_RE.sub(" ", text or ""))


def _clean_facility(facility):
    """Drop legal-entity suffixes like "(Br)" / "(Br of Aster DM ...)" and
    re-space run-together names ("MedcareHospitalSharjah")."""
    facility = _BR_PAREN_RE.sub("", facility.strip()).strip()
    if facility and " " not in facility:
        facility = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", facility)
    return facility.strip()


def split_title(raw_title):
    """Return (role, department, facility) from Oracle requisition titles.

    Observed shapes:
      "Registered Nurse.Endoscopy.Medcare Hospital Sharjah (Br)"  (Role.Dept.Facility)
      "Associate.Insurance.Medcare Hospital (Br)"
      "Cast Technician for Medcare Orthopedics and Spine Hospital (Br)"
      "Head - Endoscopy for Medcare Hospital"
      "Medical Coder"                                             (plain)
    """
    title = clean_text(raw_title)
    parts = [p.strip() for p in title.split(".") if p.strip()]
    # single-letter parts mean the dots are an abbreviation ("(L.L.C)"), not
    # the Role.Dept.Facility separator
    if any(len(p) < 2 for p in parts):
        parts = [title]
    if len(parts) >= 3:
        return parts[0], parts[1], _clean_facility(".".join(parts[2:]))
    if len(parts) == 2:
        # "Role.Facility" vs "Role.Dept" — a Medcare/hospital name is a facility
        if _FACILITY_RE.search(parts[1]):
            return parts[0], "", _clean_facility(parts[1])
        return parts[0], parts[1], ""
    m = re.split(r"\s+for\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(m) == 2:
        return m[0].strip(" -"), "", _clean_facility(m[1])
    return title, "", ""


def display_title(role, department):
    if department and department.lower() not in role.lower():
        return "{} - {}".format(role, department)
    return role


# ----------------------------------------------------------------------------
# Category mapping (club enum: doctors | nurses | pharmacists | non_clinical)
# ----------------------------------------------------------------------------

# Oracle CATEGORIES facet values seen on the tenant -> (club category, confident)
# None = fall back to the title classifier.
CATEGORY_MAP = {
    "nursing": "nurses",
    "clinicians": "doctors",
    "academics- medical": "doctors",
    "clinical pharmacist": "pharmacists",
    "senior pharmacist": "pharmacists",
    "enabling & support": "non_clinical",
    "ancilliary": "non_clinical",
    "retail": "non_clinical",
    # allied health has no club bucket of its own
    "paramedical": "non_clinical",
}

_NURSE_RE = re.compile(r"\bnurs(e|es|ing)\b|\bmidwi(fe|ves|fery)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\b(pharmac(y|ist|ies))\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|dentist|general practitioner|consultant|"
    r"specialist|registrar|medical officer|gp)\b|(?<!audi)(?<!techn)ologist\b",
    re.IGNORECASE)


def classify_category(title, oracle_category=""):
    """Return (club_category, needs_review).

    The Oracle category facet is authoritative when we know it; otherwise the
    title decides; a title matching nothing is kept as non_clinical and
    flagged needs_review (master spec §2 — never silently dropped).
    """
    mapped = CATEGORY_MAP.get((oracle_category or "").strip().lower())
    if mapped:
        # Titles override the two mixed/apprentice-style buckets when obvious
        if mapped == "non_clinical" and _NURSE_RE.search(title):
            return ("nurses", False)
        return (mapped, False)
    if _PHARM_RE.search(title):
        return ("pharmacists", False)
    if _NURSE_RE.search(title):
        return ("nurses", False)
    if _DOCTOR_RE.search(title):
        return ("doctors", False)
    return ("non_clinical", True)


def map_job_type(job_schedule):
    low = (job_schedule or "").lower()
    if "part" in low:
        return "part_time"
    if "full" in low:
        return "full_time"
    return "full_time"  # portal default; Medcare posts are full-time roles


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def check_robots(session):
    """medcare.ae robots.txt allows /en/careers.html; the Oracle host serves
    no robots.txt (404 => everything allowed). Abort if either changes."""
    for robots_url, probe in [
            ("https://www.medcare.ae/robots.txt", CAREERS_PAGE),
            (ORACLE_BASE + "/robots.txt", LIST_URL)]:
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
            continue
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
        if not rp.can_fetch(USER_AGENT, probe):
            sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt checks passed")


def _request(session, url, params=None):
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


def fetch_requisitions_page(session, offset, limit=PER_PAGE):
    # Oracle's finder syntax packs paging into the `finder` param itself.
    finder = ("findReqs;siteNumber={site},limit={limit},offset={offset},"
              "sortBy=POSTING_DATES_DESC,selectedOrganizationsFacet={org}").format(
                  site=SITE_NUMBER, limit=limit, offset=offset, org=MEDCARE_ORG_ID)
    data = _request(session, LIST_URL, {
        "onlyData": "true",
        "expand": "requisitionList.secondaryLocations",
        "finder": finder,
    })
    if not data or not data.get("items"):
        return None, 0
    item = data["items"][0]
    return item.get("requisitionList") or [], item.get("TotalJobsCount") or 0


def fetch_detail(session, job_id):
    finder = 'ById;Id="{}",siteNumber={}'.format(job_id, SITE_NUMBER)
    data = _request(session, DETAIL_URL, {
        "expand": "all", "onlyData": "true", "finder": finder})
    if data and data.get("items"):
        return data["items"][0]
    return {}


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def requisition_to_rich_row(req):
    raw_title = clean_text(req.get("Title") or "")
    role, department, facility = split_title(raw_title)
    category, needs_review = classify_category(raw_title)
    primary_location = clean_text(req.get("PrimaryLocation") or "")
    city = primary_location.split(",")[0].strip() or DEFAULT_CITY

    return {
        "source": SITE,
        "job_id": str(req.get("Id") or ""),
        "title": display_title(role, department),
        "raw_title": raw_title,
        "company": COMPANY_NAME,
        "facility": facility,
        "department": department,
        "city": city,
        "country": COUNTRY_NAME,
        # portal never shows salary — capture, don't invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": map_job_type(req.get("JobSchedule")),
        "category": category,
        "category_original": "",
        "needs_review": needs_review,
        "posted_date": (req.get("PostedDate") or "")[:10],
        "expires_at": (req.get("PostingEndDate") or "")[:10],
        "description": strip_html(req.get("ShortDescriptionStr") or "")[:DESCRIPTION_MAX_CHARS],
        "job_url": JOB_URL_TMPL.format(req.get("Id")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, detail):
    """Overlay detail-endpoint fields (category facet, schedule, full HTML
    description) onto a listing row; re-runs the classifier with the
    authoritative Oracle category."""
    if not detail:
        return row
    oracle_category = clean_text(detail.get("Category") or "")
    if oracle_category:
        row["category_original"] = oracle_category
        row["category"], row["needs_review"] = classify_category(
            row["raw_title"], oracle_category)
    if detail.get("JobSchedule"):
        row["job_type"] = map_job_type(detail["JobSchedule"])
    description = strip_html(detail.get("ExternalDescriptionStr") or "")
    if description:
        row["description"] = description[:DESCRIPTION_MAX_CHARS]
    if detail.get("ExternalPostedStartDate"):
        row["posted_date"] = str(detail["ExternalPostedStartDate"])[:10]
    if detail.get("PostingEndDate"):
        row["expires_at"] = str(detail["PostingEndDate"])[:10]
    return row


def rich_row_to_club_row(r, company_about=""):
    def _s(key, default=""):
        val = r.get(key)
        return default if val is None or pd.isna(val) or val == "" else str(val)

    # a parsed facility becomes the club company only when it actually names
    # a Medcare unit (not a neighbourhood like "Tilal Al Ghaf")
    facility = _s("facility")
    company = (facility if _FACILITY_RE.search(facility)
               else _s("company", COMPANY_NAME))
    return {
        "country_name": _s("country", COUNTRY_NAME),
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": _s("city", DEFAULT_CITY),
        "company_name": company,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": company_about,
        "title": _s("title"),
        "description": _s("description"),
        "job_type": _s("job_type", "full_time"),
        "category": _s("category", "non_clinical"),
        "application_url": _s("job_url"),
        "posted_at": _s("posted_date"),
        "min_experience": "",
        "max_experience": "",
        # no salary data on the Oracle portal — left blank, never invented
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        "expires_at": _s("expires_at"),
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date) or None = keep everything (first run:
    the ATS only lists open requisitions, so all of them are current)."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    if INITIAL_WINDOW_DAYS is not None:
        return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    return None


def within_window(posted_date, cutoff):
    if cutoff is None:
        return True
    return bool(posted_date) and posted_date >= cutoff


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def write_club_csv(rich_df, run_date, company_about):
    rows = [rich_row_to_club_row(r, company_about) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape Medcare (medcare.ae) job openings from the Oracle "
                    "Recruiting portal.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N list pages (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip the per-NEW-job detail request (category, "
                             "schedule and full description come from it)")
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
    log.info("Existing CSV has %d known jobs; cutoff: %s",
             len(known_ids), cutoff or "none (keeping all open requisitions)")

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    company_about = ""
    offset, page = 0, 1

    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        reqs, total = fetch_requisitions_page(session, offset)
        if not reqs:
            break
        log.info("Page %d: %d requisitions (total on portal: %d)",
                 page, len(reqs), total)

        page_all_old = True
        for req in reqs:
            counters["scanned"] += 1
            try:
                row = requisition_to_rich_row(req)
            except Exception as exc:
                log.warning("Skipping malformed requisition %s: %s", req.get("Id"), exc)
                continue
            if within_window(row["posted_date"], cutoff):
                page_all_old = False
            else:
                counters["excluded_old"] += 1
                continue
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if not args.no_details:
                detail = fetch_detail(session, row["job_id"])
                if detail:
                    row = apply_detail(row, detail)
                    if not company_about:
                        company_about = strip_html(
                            detail.get("OrganizationDescriptionStr") or ""
                        )[:COMPANY_ABOUT_MAX_CHARS]
                else:
                    counters["detail_failed"] += 1
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["raw_title"],
                                   "category_original": row["category_original"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        if page_all_old:
            log.info("Page %d entirely older than %s — stopping", page, cutoff)
            break
        offset += len(reqs)
        if offset >= total or len(reqs) < PER_PAGE:
            break
        page += 1

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
        target, n = write_club_csv(combined, args.run_date,
                                   company_about or COMPANY_ABOUT_FALLBACK)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Requisitions scanned:  {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
