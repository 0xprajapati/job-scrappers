#!/usr/bin/env python3
"""Scrape job openings from seha.ae (SEHA — Abu Dhabi Health Services Co., UAE).

Data source
-----------
www.seha.ae/careers is a static page whose "Apply now" / vacancies links point
at SEHA's **Oracle Recruiting Cloud** candidate portal:

    https://fa-eutv-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1

That portal exposes the standard public, token-free Oracle CE REST API. The
tenant is SEHA-only (~131 open requisitions), so no organization facet is
needed:

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber=CX_1,limit=N,offset=N,
                sortBy=POSTING_DATES_DESC

The listing carries only Id / Title / PostedDate / PrimaryLocation. Category,
job schedule, study level, work location and the full HTML description come
from the detail endpoint, fetched for NEW jobs only (on by default,
`--no-details` skips it):

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="<id>",siteNumber=CX_1

Salary is never exposed by this portal -> `salary_raw = "Not Disclosed"`.
Everything SEHA posts is healthcare-sector employment (it operates Abu Dhabi's
public hospitals), so the healthcare filter reduces to mapping Oracle's
CATEGORIES facet (Medical / Nursing / Allied Health / Administration) onto the
club category enum. That facet is populated on only ~40% of requisitions, so
the title classifier carries most of the load; anything it cannot place is kept
as `non_clinical` + `needs_review` (master spec §2 — never silently dropped).

Time window: an ATS only lists OPEN requisitions (SEHA keeps some live for well
over a year), so the first run keeps ALL of them (`INITIAL_WINDOW_DAYS = None`);
later runs use the master-spec watermark (newest stored posted_date minus
WATERMARK_GRACE_DAYS).

robots.txt: seha.ae publishes `User-agent: * / Disallow:` (everything allowed);
the Oracle host serves no robots.txt (404 => allowed). Both checked at startup.

Outputs
-------
* seha_jobs.csv                      — rich cumulative store (dedup: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/seha.csv — HealthCareers.club 22-col schema.

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

SITE = "seha"
CAREERS_PAGE = "https://www.seha.ae/careers"

ORACLE_BASE = "https://fa-eutv-saasfaprod1.fa.ocs.oraclecloud.com"
SITE_NUMBER = "CX_1"
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
# SEHA descriptions are long and structured (duties + qualifications); keep them
# whole — the longest observed is ~7.5k chars.
DESCRIPTION_MAX_CHARS = 8_000

RICH_CSV = "seha_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# UAE public-hospital operator — country fixed. No salary data on the portal.
COMPANY_NAME = "SEHA — Abu Dhabi Health Services Company"
COMPANY_ABOUT = (
    "SEHA (Abu Dhabi Health Services Company) is the largest healthcare network "
    "in the UAE, operating the Emirate of Abu Dhabi's public hospitals, "
    "specialty centres and ambulatory healthcare clinics — including Sheikh "
    "Khalifa Medical City, Tawam Hospital, Corniche Hospital, Al Ain Hospital "
    "and Al Rahba Hospital — across Abu Dhabi, Al Ain and Al Dhafra."
)
COUNTRY_NAME = "United Arab Emirates"
COUNTRY_CODE = "AE"
COUNTRY_DIAL = "+971"
DEFAULT_CITY = "Abu Dhabi"

RICH_COLUMNS = [
    "source", "job_id", "title", "raw_title", "company", "facility",
    "work_location", "city", "country",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "job_shift", "category",
    "category_original", "job_function", "education",
    "experience_min_years", "experience_max_years",
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

log = logging.getLogger("seha_scraper")

# ----------------------------------------------------------------------------
# Text / title parsing
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]*)\)\s*$")

# A trailing parenthetical in a SEHA title is EITHER the facility
# ("... (Tawam Hospital)", "... (SKMC)") or a role qualifier
# ("... (IVF)", "... (Arabic Speaker)", "... (UAE National)"). Only the former
# is peeled off into `facility`; qualifiers stay part of the title.
_FACILITY_HINT_RE = re.compile(
    r"\b(hospital|clinic|clinics|centre|center|medical\s+city|region|"
    r"skmc|stmc|sakina|seha|tawam|corniche|rahba|wagan|madinat\s+zayed|"
    r"mafraq|ambulatory)\b",
    re.IGNORECASE)

# SEHA writes several facilities as acronyms; expand them so the club CSV's
# company_name is readable.
_FACILITY_EXPANSIONS = {
    "skmc": "Sheikh Khalifa Medical City",
    "stmc": "Sheikh Tahnoon Bin Mohammad Medical City",
    "ach": "Al Corniche Hospital",
    "twm": "Tawam Hospital",
    "seha clinics": "SEHA Clinics",
    "sakina": "Sakina (SEHA Behavioral Health Centre)",
}
_TRAILING_ACRONYM_RE = re.compile(r"\s*[-–]\s*([A-Za-z]{2,6})\s*$")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(text):
    """HTML -> flat text, with <li>/<br>/<p> boundaries kept as separators so
    bullet lists don't run their items together."""
    text = re.sub(r"<\s*(li|br|/p|/div|/tr)[^>]*>", " • ", str(text or ""),
                  flags=re.IGNORECASE)
    text = clean_text(_TAG_RE.sub(" ", text))
    text = re.sub(r"(?:•\s*)+", "• ", text)
    return text.strip(" •").strip()


def expand_facility(facility):
    """ "SKMC" -> "Sheikh Khalifa Medical City"; unknown names pass through.

    HR work-location strings often append the acronym to the full name
    ("Sheikh Khalifa Medical City - SKMC", "AL Corniche - ACH"); those collapse
    to the single canonical name so one facility can't become two different
    `company_name` values in the club CSV.
    """
    facility = clean_text(facility).strip(" -")
    expanded = _FACILITY_EXPANSIONS.get(facility.lower())
    if expanded:
        return expanded
    match = _TRAILING_ACRONYM_RE.search(facility)
    if match:
        head = facility[:match.start()].strip()
        expanded = _FACILITY_EXPANSIONS.get(match.group(1).lower(), "")
        if head and expanded.lower().startswith(head.lower()):
            return expanded
    return facility


def split_title(raw_title):
    """Return (title, facility) from an Oracle requisition title.

    Observed shapes:
      "Sonographer (Tawam Fertility Center)"        -> facility in parentheses
      "Specialist Physician- Cardiology (SEHA Clinics)"
      "Pharmacist - Inpatient Pharmacy (SKMC ) - UAE National"
      "Embryologist (IVF)"                          -> qualifier, NOT a facility
      "Consultant Neurology (Arabic Speaker)"       -> qualifier
      "Staff Nurse - ICU"                           -> no parenthetical
    """
    title = clean_text(raw_title)
    facility = ""
    # A facility parenthetical can be followed by a trailing qualifier
    # ("(SKMC ) - UAE National"); check the last parenthetical either way.
    head, sep, tail = title.rpartition(")")
    if sep:
        match = _TRAILING_PAREN_RE.search(head + sep)
        if match and _FACILITY_HINT_RE.search(match.group(1)):
            facility = expand_facility(match.group(1))
            title = clean_text(
                _TRAILING_PAREN_RE.sub("", head + sep) + " " + tail).strip(" -")
    # normalise the "Role- Speciality" typo shape into "Role - Speciality"
    title = re.sub(r"(?<=\w)-\s+(?=\w)", " - ", title)
    return _WS_RE.sub(" ", title).strip(" -"), facility


# ----------------------------------------------------------------------------
# Experience parsing
# ----------------------------------------------------------------------------

# SEHA states requirements in free text, in house style "NLT 2 years"
# (not less than), plus the usual "minimum of 5 years" / "at least 3 years".
# Roles are tiered ("Tier 1: NLT 2 years ... Tier 2: NLT 8 years"), so the
# LOWEST stated requirement is the entry bar we record.
_EXP_CUE_RE = re.compile(
    r"(?:\bnlt\b|\bnot\s+less\s+than\b|\bminimum(?:\s+of)?\b|\bmin\.?\b|"
    r"\bat\s+least\b|\bmore\s+than\b)\s*"
    r"(\d{1,2})\s*(?:\+|\s*(?:-|to)\s*\d{1,2})?\s*(?:year|yr)",
    re.IGNORECASE)
_EXP_TRAILING_RE = re.compile(
    r"(\d{1,2})\s*(?:\+)?\s*(?:-|to)?\s*(?:\d{1,2})?\s*(?:year|yr)s?[’'`]?s?\b"
    r"[^.;]{0,40}?\bexperience\b",
    re.IGNORECASE)

MAX_PLAUSIBLE_YEARS = 40


def parse_min_experience_years(text):
    """Lowest explicitly-required years of experience in a job description.

    Returns a string ("2") or "" when the text states no requirement. Only
    numbers tied to an explicit requirement cue ("NLT 2 years", "minimum of 5
    years") or directly qualifying the word "experience" are considered, so
    stray numbers ("25 years of age", "2 year contract") are ignored.
    """
    text = clean_text(text)
    if not text:
        return ""
    years = [int(m) for m in _EXP_CUE_RE.findall(text)]
    years += [int(m) for m in _EXP_TRAILING_RE.findall(text)]
    years = [y for y in years if 0 <= y <= MAX_PLAUSIBLE_YEARS]
    return str(min(years)) if years else ""


# ----------------------------------------------------------------------------
# Category mapping (club enum: doctors | nurses | pharmacists | non_clinical)
# ----------------------------------------------------------------------------

# Oracle CATEGORIES facet values on the SEHA tenant -> club category.
# "Allied Health" has no club bucket of its own, so it lands in non_clinical
# (same treatment medcare gives "Paramedical").
CATEGORY_MAP = {
    "medical": "doctors",
    "nursing": "nurses",
    "allied health": "non_clinical",
    "administration": "non_clinical",
    "pharmacy": "pharmacists",
}

_NURSE_RE = re.compile(r"\bnurs(e|es|ing)\b|\bmidwi(fe|ves|fery)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\b(pharmac(y|ist|ists|ies))\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|surgery|dentist|general practitioner|"
    r"consultant|specialist|registrar|medical officer|gp|intensivist|"
    r"anaesthetist|anesthetist|anesthesiologist|anaesthesiologist|"
    r"obstetrician|gynecologist|gynaecologist|p(a?)ediatrician|psychiatrist|"
    r"radiologist|pathologist|neonatologist|oncologist|cardiologist|"
    r"dermatologist|endocrinologist|gastroenterologist|neurologist|"
    r"ophthalmologist|otolaryngologist|rheumatologist|urologist|nephrologist|"
    r"pulmonologist|hematologist|haematologist)\b",
    re.IGNORECASE)
# Allied-health / technical / corporate roles: clinical-adjacent at most, and
# checked BEFORE _DOCTOR_RE so "Consultant" in an admin title can't win.
_ALLIED_RE = re.compile(
    r"\b(sonographer|radiographer|technologist|technician|therapist|"
    r"physiotherapist|dietit(ian|ician)|nutritionist|embryologist|"
    r"phlebotomist|optometrist|audiologist|paramedic|coder|"
    r"engineer|officer|manager|representative|assistant|analyst|"
    r"coordinator|administrator|secretary|clerk|accountant|receptionist)\b",
    re.IGNORECASE)
# Corporate-function words. SEHA titles head-office roles "Specialist - ..." /
# "Consultant - ...", which would otherwise trip _DOCTOR_RE, so these are
# checked alongside _ALLIED_RE, before the clinical keywords.
_CORPORATE_RE = re.compile(
    r"\b(talent|performance|business|commercial|finance|financial|procurement|"
    r"marketing|communications|human\s+resources|recruitment|payroll|legal|"
    r"quality|excellence|strategy|supply\s+chain|information\s+technology|"
    r"facilities|housekeeping|catering|security)\b",
    re.IGNORECASE)
# Titles SEHA posts that no club bucket fits (behavioural-health clinicians).
# Kept as non_clinical AND flagged so a human decides (master spec §2).
_AMBIGUOUS_RE = re.compile(
    r"\b(psychologist|physiologist|counsell?or|analyst|health\s*care\s*assistant)\b",
    re.IGNORECASE)


def classify_category(title, oracle_category=""):
    """Return (club_category, needs_review).

    Oracle's category facet is authoritative when present (it is populated on
    only ~40% of SEHA requisitions); otherwise the title decides. Nursing and
    pharmacy titles override a coarse facet value. A title that matches nothing
    — or matches a role with no club bucket — is kept as non_clinical and
    flagged needs_review, never dropped.
    """
    mapped = CATEGORY_MAP.get(clean_text(oracle_category).lower())
    if mapped:
        # the facet is department-level, so an unmistakable title still wins
        if mapped != "pharmacists" and _PHARM_RE.search(title):
            return ("pharmacists", False)
        if mapped == "non_clinical" and _NURSE_RE.search(title):
            return ("nurses", False)
        return (mapped, False)
    if _PHARM_RE.search(title):
        return ("pharmacists", False)
    if _NURSE_RE.search(title):
        return ("nurses", False)
    if _AMBIGUOUS_RE.search(title):
        return ("non_clinical", True)
    if _ALLIED_RE.search(title) or _CORPORATE_RE.search(title):
        return ("non_clinical", False)
    if _DOCTOR_RE.search(title):
        return ("doctors", False)
    return ("non_clinical", True)


def map_job_type(job_schedule):
    low = clean_text(job_schedule).lower()
    if "part" in low:
        return "part_time"
    if "intern" in low:
        return "internship"
    if "contract" in low or "temporary" in low:
        return "contract"
    return "full_time"  # portal default; SEHA requisitions are full-time posts


def parse_city(primary_location):
    """ "Al Ain, United Arab Emirates" -> "Al Ain"; a bare country -> default.

    Some requisitions carry only "United Arab Emirates" (no city component).
    """
    location = clean_text(primary_location)
    city = location.split(",")[0].strip()
    if not city or city.lower() in ("united arab emirates", "uae"):
        return DEFAULT_CITY
    return city


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def check_robots(session):
    """seha.ae publishes "User-agent: * / Disallow:" (all allowed); the Oracle
    host serves no robots.txt (404 => allowed). Abort if either changes."""
    for robots_url, probe in [
            ("https://www.seha.ae/robots.txt", CAREERS_PAGE),
            (ORACLE_BASE + "/robots.txt", LIST_URL)]:
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
            continue
        # a 404 page is HTML, not a robots policy — treat it as "no rules"
        looks_like_robots = (resp.status_code < 400
                             and "<html" not in resp.text[:500].lower())
        rp.parse(resp.text.splitlines() if looks_like_robots else [])
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
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_requisitions_page(session, offset, limit=PER_PAGE):
    # Oracle's finder syntax packs paging into the `finder` param itself.
    finder = ("findReqs;siteNumber={site},limit={limit},offset={offset},"
              "sortBy=POSTING_DATES_DESC").format(
                  site=SITE_NUMBER, limit=limit, offset=offset)
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
    title, facility = split_title(raw_title)
    category, needs_review = classify_category(title)

    return {
        "source": SITE,
        "job_id": str(req.get("Id") or ""),
        "title": title,
        "raw_title": raw_title,
        "company": COMPANY_NAME,
        "facility": facility,
        "work_location": "",
        "city": parse_city(req.get("PrimaryLocation")),
        "country": COUNTRY_NAME,
        # portal never shows salary — capture, don't invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": map_job_type(req.get("JobSchedule")),
        "job_shift": clean_text(req.get("JobShift") or ""),
        "category": category,
        "category_original": "",
        "job_function": "",
        "education": clean_text(req.get("StudyLevel") or ""),
        "experience_min_years": "",
        "experience_max_years": "",
        "needs_review": needs_review,
        "posted_date": clean_text(req.get("PostedDate") or "")[:10],
        "expires_at": clean_text(req.get("PostingEndDate") or "")[:10],
        "description": strip_html(req.get("ShortDescriptionStr") or "")[:DESCRIPTION_MAX_CHARS],
        "job_url": JOB_URL_TMPL.format(req.get("Id")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_description(detail):
    """Full posting text: summary + responsibilities + qualifications, in the
    order the candidate portal renders them."""
    parts = []
    for label, key in (("", "ExternalDescriptionStr"),
                       ("Responsibilities: ", "ExternalResponsibilitiesStr"),
                       ("Qualifications: ", "ExternalQualificationsStr")):
        chunk = strip_html(detail.get(key) or "")
        if chunk:
            parts.append(label + chunk)
    return " ".join(parts)[:DESCRIPTION_MAX_CHARS]


def apply_detail(row, detail):
    """Overlay detail-endpoint fields (Oracle category facet, schedule, study
    level, work location, full description) onto a listing row, and re-run the
    classifier with the authoritative category."""
    if not detail:
        return row
    oracle_category = clean_text(detail.get("Category") or "")
    if oracle_category:
        row["category_original"] = oracle_category
    row["category"], row["needs_review"] = classify_category(
        row["title"], oracle_category)
    if detail.get("JobSchedule"):
        row["job_type"] = map_job_type(detail["JobSchedule"])
    if detail.get("JobShift"):
        row["job_shift"] = clean_text(detail["JobShift"])
    if detail.get("StudyLevel"):
        row["education"] = clean_text(detail["StudyLevel"])
    if detail.get("JobFunction"):
        row["job_function"] = clean_text(detail["JobFunction"])
    if detail.get("PrimaryLocation"):
        row["city"] = parse_city(detail["PrimaryLocation"])

    work_location = clean_text(
        (detail.get("workLocation") or [{}])[0].get("LocationName") or "")
    if work_location:
        row["work_location"] = work_location
        # the title's parenthetical is the cleaner facility name; fall back to
        # the HR work location only when it actually names a unit (values like
        # a bare "Abu Dhabi" are the emirate, not a facility)
        if not row["facility"] and _FACILITY_HINT_RE.search(work_location):
            row["facility"] = expand_facility(work_location)

    description = build_description(detail)
    if description:
        row["description"] = description
        row["experience_min_years"] = parse_min_experience_years(description)
    if detail.get("ExternalPostedStartDate"):
        row["posted_date"] = str(detail["ExternalPostedStartDate"])[:10]
    if detail.get("ExternalPostedEndDate"):
        row["expires_at"] = str(detail["ExternalPostedEndDate"])[:10]
    return row


def rich_row_to_club_row(r):
    def _s(key, default=""):
        val = r.get(key)
        if val is None or val == "":
            return default
        try:
            if pd.isna(val):
                return default
        except (TypeError, ValueError):
            pass
        return str(val)

    facility = _s("facility")
    return {
        "country_name": _s("country", COUNTRY_NAME),
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": _s("city", DEFAULT_CITY),
        "company_name": facility or COMPANY_NAME,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": COMPANY_ABOUT,
        "title": _s("title"),
        "description": _s("description"),
        "job_type": _s("job_type", "full_time"),
        "category": _s("category", "non_clinical"),
        "application_url": _s("job_url"),
        "posted_at": _s("posted_date"),
        "min_experience": _s("experience_min_years"),
        "max_experience": _s("experience_max_years"),
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
        description="Scrape SEHA (seha.ae) job openings from the Oracle "
                    "Recruiting candidate portal.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N list pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N requisitions (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip the per-NEW-job detail request (category, "
                             "schedule, education and the full description "
                             "come from it)")
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
    offset, page = 0, 1

    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        reqs, total = fetch_requisitions_page(session, offset)
        if reqs is None:
            log.error("List request failed on page %d — stopping pagination", page)
            break
        if not reqs:
            break
        log.info("Page %d: %d requisitions (total on portal: %d)",
                 page, len(reqs), total)

        page_all_old = True
        for req in reqs:
            if args.limit is not None and counters["scanned"] >= args.limit:
                page_all_old = False
                break
            counters["scanned"] += 1
            try:
                row = requisition_to_rich_row(req)
            except Exception as exc:
                log.warning("Skipping malformed requisition %s: %s", req.get("Id"), exc)
                continue
            if not row["job_id"]:
                log.warning("Requisition without Id skipped: %s", row["raw_title"])
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
                    try:
                        row = apply_detail(row, detail)
                    except Exception as exc:
                        log.warning("Detail parse failed for %s: %s",
                                    row["job_id"], exc)
                        counters["detail_failed"] += 1
                else:
                    counters["detail_failed"] += 1
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["raw_title"],
                                   "category_original": row["category_original"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        if args.limit is not None and counters["scanned"] >= args.limit:
            log.info("Reached --limit %d — stopping", args.limit)
            break
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
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Requisitions scanned:  {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(
        cutoff or "no cutoff", counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
