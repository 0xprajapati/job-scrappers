#!/usr/bin/env python3
"""Scrape job openings from zulekhahospitals.com (Zulekha Healthcare Group, UAE).

Data source
-----------
www.zulekhahospitals.com/careers is a static page; recruiting happens on the
group's career portal zulekhacareers.com, whose "Vacancies" button opens the
**Adrenalin MAX HRIS** candidate portal:

    https://myhrismax1.myadrenalin.com/CandidateMAX/#/?CompanyID=ZULEKHA

That Angular SPA loads all open positions from one public, token-free JSON
endpoint (no pagination — the tenant lists every open vacancy at once):

    POST /CandidateMAX/CPVacancyDetails/GetVacancyInformationWithoutToken
    {"CompanyID": "ZULEKHA", "Flag": "OP"}          # OP = open positions

Each vacancy carries FUNCTION_ID (stable id -> dedup key), FUNCTION_NAME
(title, often ALL-CAPS with a requisition suffix like "CARE ADVISOR_608"),
CITY, LOCATION_NAME ("Zulekha Hospital LLC -Dubai"), EXPERIENCE
("3 - 7  Year(s)"), POSTED_ON (ISO timestamp), FUNCTIONAL_AREA
("Non Clinical" / clinical buckets) and optional FUNCTION_DESC /
FUNCTION_RESPONSIBILITY HTML.

The portal has no per-job deep link (job selection is in-page SPA state), so
`job_url` is the portal URL for every row; dedup relies on `(source, job_id)`.
Salary is never exposed -> `salary_raw = "Not Disclosed"`. Everything a
hospital group posts is healthcare-sector employment, so the healthcare filter
reduces to mapping title keywords / FUNCTIONAL_AREA onto the club category
enum; unmappable titles are kept + `needs_review` (master spec §2).

Time window: an ATS only lists OPEN vacancies, so the first run keeps ALL of
them (`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
(newest stored posted_date minus WATERMARK_GRACE_DAYS).

robots.txt: zulekhahospitals.com allows /careers; myhrismax1.myadrenalin.com
serves no robots.txt (redirects to an error page => allowed). Both checked at
startup.

Outputs
-------
* zulekhahospitals_jobs.csv                        — rich cumulative store.
* ../../jobs_csv/<DD-MM-YYYY>/zulekhahospitals.csv — HealthCareers.club schema.

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

SITE = "zulekhahospitals"
CAREERS_PAGE = "https://www.zulekhahospitals.com/careers"

PORTAL_BASE = "https://myhrismax1.myadrenalin.com"
COMPANY_ID = "ZULEKHA"
LIST_URL = PORTAL_BASE + "/CandidateMAX/CPVacancyDetails/GetVacancyInformationWithoutToken"
PORTAL_URL = PORTAL_BASE + "/CandidateMAX/#/?CompanyID=" + COMPANY_ID

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# ATS lists only OPEN vacancies, so the first run keeps everything
# (None = no initial window); later runs use the watermark (master spec §4).
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "zulekhahospitals_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# UAE hospital group — country fixed. No salary data on the portal.
COMPANY_NAME = "Zulekha Healthcare Group"
COMPANY_ABOUT = (
    "The Zulekha Healthcare Group runs two multidisciplinary hospitals in "
    "Dubai and Sharjah plus UAE medical centres and pharmacies, providing "
    "specialised treatments in over 30 disciplines since 1992."
)
COUNTRY_NAME = "United Arab Emirates"
COUNTRY_CODE = "AE"
COUNTRY_DIAL = "+971"
DEFAULT_CITY = "Dubai"

RICH_COLUMNS = [
    "source", "job_id", "title", "raw_title", "company", "facility", "city",
    "country", "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "category", "category_original",
    "experience_raw", "experience_min_years", "experience_max_years",
    "number_of_posts", "needs_review", "posted_date", "description",
    "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("zulekhahospitals_scraper")

# ----------------------------------------------------------------------------
# Text / title parsing
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_REQ_SUFFIX_RE = re.compile(r"[_\s]*_\d+\s*$")  # "CARE ADVISOR_608" -> "CARE ADVISOR"

# Words kept fully uppercase when re-casing an ALL-CAPS title.
_ACRONYMS = {
    "DHA", "MOH", "DOH", "HAAD", "ICU", "NICU", "PICU", "OT", "ER", "OPD",
    "IP", "OP", "HR", "IT", "CSSD", "ENT", "OB", "GYN", "MRI", "CT", "GP",
    "UAE", "RN", "CEO", "CFO", "PRO",
}


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(text):
    return clean_text(_TAG_RE.sub(" ", str(text or "")))


def clean_title(raw_title):
    """Drop the numeric requisition suffix and re-case shouty titles:
    "CARE ADVISOR_608" -> "Care Advisor"; mixed-case titles pass through."""
    title = _REQ_SUFFIX_RE.sub("", clean_text(raw_title))
    if title and title == title.upper():
        title = " ".join(
            word if word.strip("()-/.,") in _ACRONYMS else word.title()
            for word in title.split()
        )
    return title


_LLC_RE = re.compile(r"\b(LLC|L\.L\.C\.?|FZ-?LLC)\b\.?", re.IGNORECASE)


def clean_facility(location_name):
    """ "Zulekha Hospital LLC -Dubai" -> "Zulekha Hospital Dubai" """
    facility = _LLC_RE.sub("", clean_text(location_name))
    facility = re.sub(r"\s*-\s*", " ", facility)
    return _WS_RE.sub(" ", facility).strip(" -")


# ----------------------------------------------------------------------------
# Experience parsing
# ----------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_experience(raw):
    """Return (min_years, max_years) as strings from the portal's EXPERIENCE.

    Observed shapes: "3 - 7  Year(s)", "10", "0 - 1  Year(s)"; months are
    converted to whole years (floor). Unparseable -> ("", "").
    """
    text = clean_text(raw)
    if not text:
        return ("", "")
    if re.search(r"\bfresher\b", text, re.IGNORECASE):
        return ("0", "")
    numbers = [float(n) for n in _NUM_RE.findall(text)]
    if not numbers:
        return ("", "")
    if re.search(r"month", text, re.IGNORECASE):
        numbers = [n / 12 for n in numbers]
    lo = int(numbers[0])
    hi = int(numbers[1]) if len(numbers) > 1 else None
    if hi is not None and hi < lo:
        lo, hi = hi, lo
    return (str(lo), "" if hi is None else str(hi))


# ----------------------------------------------------------------------------
# Category mapping (club enum: doctors | nurses | pharmacists | non_clinical)
# ----------------------------------------------------------------------------

_NURSE_RE = re.compile(r"\bnurs(e|es|ing)\b|\bmidwi(fe|ves|fery)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\b(pharmac(y|ist|ists|ies))\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|dentist|general practitioner|consultant|"
    r"specialist|registrar|medical officer|gp|intensivist|anaesthetist|"
    r"anesthetist|obstetrician|p(a?)ediatrician|psychiatrist)\b|"
    r"(?<!audi)(?<!techn)ologist\b",
    re.IGNORECASE)

# FUNCTIONAL_AREA values that confidently mean "not a clinical role".
_NON_CLINICAL_AREAS = {"non clinical", "non-clinical", "nonclinical"}


def classify_category(title, functional_area="", categ_code=""):
    """Return (club_category, needs_review).

    The title decides when it matches a clinical pattern; otherwise the
    portal's FUNCTIONAL_AREA / VAC_EMP_CATG_CODE confirm non-clinical roles.
    A title matching nothing is kept as non_clinical and flagged needs_review
    (master spec §2 — never silently dropped).
    """
    if _PHARM_RE.search(title):
        return ("pharmacists", False)
    if _NURSE_RE.search(title):
        return ("nurses", False)
    if _DOCTOR_RE.search(title):
        return ("doctors", False)
    area = clean_text(functional_area).lower()
    if area in _NON_CLINICAL_AREAS or clean_text(categ_code).upper() == "NC":
        return ("non_clinical", False)
    return ("non_clinical", True)


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def check_robots(session):
    """zulekhahospitals.com robots.txt allows /careers; the Adrenalin host
    serves no robots.txt (redirects to errorpage.aspx => allowed). Abort if
    either changes to a disallow."""
    for robots_url, probe in [
            ("https://www.zulekhahospitals.com/robots.txt", CAREERS_PAGE),
            (PORTAL_BASE + "/robots.txt", LIST_URL)]:
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
            continue
        looks_like_robots = resp.status_code < 400 and "<html" not in resp.text[:500].lower()
        rp.parse(resp.text.splitlines() if looks_like_robots else [])
        if not rp.can_fetch(USER_AGENT, probe):
            sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt checks passed")


def fetch_vacancies(session):
    """POST the open-positions query; returns the vacancy list (one shot,
    no pagination on this portal)."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d in %.0fs (%s)",
                        attempt, MAX_RETRIES, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.post(
                LIST_URL,
                json={"CompanyID": COMPANY_ID, "Flag": "OP"},
                timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("IsValid"):
                last_error = "IsValid=false: {}".format(data.get("ErrorMessage"))
                continue
            blocks = data.get("Data") or []
            return (blocks[0].get("VacancyInformation") or []) if blocks else []
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", LIST_URL, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def vacancy_to_rich_row(vac):
    raw_title = clean_text(vac.get("FUNCTION_NAME") or "")
    category, needs_review = classify_category(
        raw_title, vac.get("FUNCTIONAL_AREA") or "", vac.get("VAC_EMP_CATG_CODE") or "")
    experience_raw = clean_text(vac.get("EXPERIENCE") or "")
    exp_min, exp_max = parse_experience(experience_raw)
    description = strip_html(
        (vac.get("FUNCTION_DESC") or "") + " " + (vac.get("FUNCTION_RESPONSIBILITY") or ""))

    return {
        "source": SITE,
        "job_id": str(vac.get("FUNCTION_ID") or ""),
        "title": clean_title(raw_title),
        "raw_title": raw_title,
        "company": COMPANY_NAME,
        "facility": clean_facility(vac.get("LOCATION_NAME") or ""),
        "city": clean_text(vac.get("CITY") or "") or DEFAULT_CITY,
        "country": COUNTRY_NAME,
        # portal never shows salary — capture, don't invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": "full_time",  # portal marks no schedule; hospital posts are full-time
        "category": category,
        "category_original": clean_text(vac.get("FUNCTIONAL_AREA") or ""),
        "experience_raw": experience_raw,
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "number_of_posts": str(vac.get("NUMBER_OF_POST") or ""),
        "needs_review": needs_review,
        "posted_date": clean_text(vac.get("POSTED_ON") or "")[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": PORTAL_URL,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def rich_row_to_club_row(r):
    return {
        "country_name": r.get("country") or COUNTRY_NAME,
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": r.get("city") or DEFAULT_CITY,
        "company_name": r.get("facility") or COMPANY_NAME,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": COMPANY_ABOUT,
        "title": r.get("title", ""),
        "description": r.get("description", ""),
        "job_type": r.get("job_type") or "full_time",
        "category": r.get("category") or "non_clinical",
        "application_url": r.get("job_url", PORTAL_URL),
        "posted_at": r.get("posted_date", ""),
        "min_experience": r.get("experience_min_years", ""),
        "max_experience": r.get("experience_max_years", ""),
        # no salary data on the Adrenalin portal — left blank, never invented
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        "expires_at": "",
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date) or None = keep everything (first run:
    the ATS only lists open vacancies, so all of them are current)."""
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
        description="Scrape Zulekha Hospitals job openings from the Adrenalin "
                    "MAX candidate portal.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N vacancies (for test runs)")
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
             len(known_ids), cutoff or "none (keeping all open vacancies)")

    vacancies = fetch_vacancies(session)
    if vacancies is None:
        sys.exit("Could not fetch the vacancy list — aborting (CSV untouched).")
    log.info("Portal lists %d open vacancies", len(vacancies))
    if args.limit is not None:
        vacancies = vacancies[:args.limit]

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0}
    new_rows, review_log = [], []

    for vac in vacancies:
        counters["scanned"] += 1
        try:
            row = vacancy_to_rich_row(vac)
        except Exception as exc:
            log.warning("Skipping malformed vacancy %s: %s",
                        vac.get("FUNCTION_ID"), exc)
            continue
        if not row["job_id"]:
            log.warning("Vacancy without FUNCTION_ID skipped: %s", row["raw_title"])
            continue
        if not within_window(row["posted_date"], cutoff):
            counters["excluded_old"] += 1
            continue
        if row["job_id"] in known_ids:
            counters["duplicates"] += 1
            continue
        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["raw_title"],
                               "category_original": row["category_original"]})
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
    print("Vacancies scanned:     {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff or "no cutoff",
                                                    counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
