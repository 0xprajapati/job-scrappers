#!/usr/bin/env python3
"""Scrape job openings from Sidra Medicine (Doha, Qatar).

Data source
-----------
Sidra Medicine recruits through its **Oracle Recruiting Cloud** candidate
portal (the URL published on sidra.org/careers):

    https://fa-epxn-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Sidra-Career-Site/jobs

That SPA is backed by Oracle's public, token-free CE REST API (master spec §1.1
— an underlying JSON API is the best source). The tenant is Sidra-only, so no
organization facet is needed:

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber=Sidra-Career-Site,limit=N,offset=N,
                sortBy=POSTING_DATES_DESC

The listing carries only Id / Title / PostedDate / PrimaryLocation / a short
description. Oracle's CATEGORIES facet, requisition type, job schedule, study
level, posting end date and the full HTML description + responsibilities +
qualifications come from the detail endpoint, fetched for NEW jobs only (on by
default, `--no-details` skips it):

    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="<id>",siteNumber=Sidra-Career-Site

Salary is never exposed by this portal -> `salary_raw = "Not Disclosed"`.
Sidra Medicine is a single specialist hospital, so every requisition it posts is
healthcare-sector employment; the healthcare filter reduces to mapping Oracle's
category facet (Physician / Nursing / Allied Health / Enabling Function: …)
onto the club category enum, with the title classifier as backup. Anything that
cannot be placed — and every "Join Our Talent Pool" campaign requisition, which
is an expression of interest rather than a live vacancy — is kept and flagged
`needs_review` (master spec §2 — never silently dropped).

Time window: an ATS only lists OPEN requisitions, so the first run keeps ALL of
them (`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
(newest stored posted_date minus WATERMARK_GRACE_DAYS).

robots.txt: sidra.org publishes `User-agent: * / Disallow:` (everything
allowed); the Oracle host serves no robots.txt (404 => allowed). Both checked
at startup.

Outputs
-------
* sidra_jobs.csv                      — rich cumulative store (dedup: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/sidra.csv — HealthCareers.club 22-col schema.

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

SITE = "sidra"
CAREERS_PAGE = "https://www.sidra.org/careers"

ORACLE_BASE = "https://fa-epxn-saasfaprod1.fa.ocs.oraclecloud.com"
SITE_NUMBER = "Sidra-Career-Site"
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
# Sidra postings are long (summary + responsibilities + a full selection-criteria
# table); keep them whole — the longest observed is ~12.6k chars.
DESCRIPTION_MAX_CHARS = 14_000

RICH_CSV = "sidra_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Single-employer, single-city portal — no salary data anywhere on it.
COMPANY_NAME = "Sidra Medicine"
COMPANY_ABOUT = (
    "Sidra Medicine is a Doha-based academic medical centre and member of Qatar "
    "Foundation, providing specialist paediatric and women's healthcare — "
    "tertiary and quaternary care for children and young people plus maternity, "
    "gynaecology and reproductive medicine services — alongside biomedical "
    "research and medical education in Qatar."
)
COUNTRY_NAME = "Qatar"
COUNTRY_CODE = "QA"
COUNTRY_DIAL = "+974"
DEFAULT_CITY = "Doha"

RICH_COLUMNS = [
    "source", "job_id", "title", "raw_title", "company",
    "work_location", "city", "country",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "job_schedule_original", "job_shift",
    "category", "category_original", "requisition_type", "job_function",
    "education", "experience_min_years", "experience_max_years",
    "nationals_only", "talent_pool",
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

log = logging.getLogger("sidra_scraper")

# ----------------------------------------------------------------------------
# Text parsing
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")

# Sidra marks Qatarization-restricted postings in the title:
# "Engineer - Specialty Systems (Nationals only)", "Supervisor - Financial
# Counselling (Qatarized)".
_NATIONALS_ONLY_RE = re.compile(
    r"\b(nationals?\s+only|qatarized|qatarization|qatari\s+nationals?)\b",
    re.IGNORECASE)


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(text):
    """HTML -> flat text, with <li>/<br>/<p>/table-cell boundaries kept as
    separators so bullet lists and the selection-criteria tables Sidra embeds in
    its qualifications don't run their items together."""
    text = re.sub(r"<\s*(li|br|/p|/div|/tr|/td|/th)[^>]*>", " • ", str(text or ""),
                  flags=re.IGNORECASE)
    text = clean_text(_TAG_RE.sub(" ", text))
    text = re.sub(r"(?:•\s*)+", "• ", text)
    return text.strip(" •").strip()


def normalise_title(raw_title):
    """Tidy an Oracle requisition title.

    Sidra titles are already employer-clean (one hospital, so no facility to peel
    off), but they mix hyphens and en/em dashes as the role/speciality separator
    ("Manager – Ethics and Compliance", "Specialist- Anesthesiology"). The
    parenthetical is always a role qualifier — a division ("(Operating Room)"),
    a modality ("(EP)", "(Non-Invasive)"), a language ("(Arabic Speaker)") or a
    Qatarization marker ("(Nationals only)") — so it stays in the title.
    """
    # only a dash used as a SEPARATOR (whitespace on at least one side) is
    # normalised — an intra-word hyphen ("(Non-Invasive)", "Post-Doctorate")
    # must survive untouched
    title = re.sub(r"(?:\s+[-–—]\s*|\s*[-–—]\s+)", " - ", clean_text(raw_title))
    title = title.replace("–", "-").replace("—", "-")
    return _WS_RE.sub(" ", title).strip(" -")


def is_nationals_only(raw_title):
    return bool(_NATIONALS_ONLY_RE.search(clean_text(raw_title)))


# ----------------------------------------------------------------------------
# Experience parsing
# ----------------------------------------------------------------------------

# Sidra states requirements inside the qualifications table, under an
# "Experience" heading, in house style "5+ years" / "2+ Years specialized
# experience", plus the usual "A minimum of 1 year" / "at least 3 years".
_EXPERIENCE_SECTION_RE = re.compile(
    r"•\s*Experience\s*•(.*?)"
    r"(?:•\s*(?:Education|Certification|Professional\s+Membership|"
    r"Job\s+Specific|Skills|Language|ESSENTIAL|PREFERRED|Qualifications)\b|$)",
    re.IGNORECASE | re.DOTALL)

# "5+ years", "3 - 5 years", "minimum of 8 years", "at least 2 yrs"
_EXP_RANGE_RE = re.compile(
    r"(?:\b(?:minimum(?:\s+of)?|min\.?|at\s+least|not\s+less\s+than|nlt|"
    r"more\s+than|over)\s+)?"
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*(\d{1,2}))?\s*"
    r"(?:year|yr)s?\b",
    re.IGNORECASE)
# Outside the Experience section a bare "2 years" is usually not a requirement
# ("2 year contract", "25 years of age"), so require an explicit cue or the word
# "experience" nearby.
_EXP_CUE_RE = re.compile(
    r"(?:\b(?:minimum(?:\s+of)?|min\.?|at\s+least|not\s+less\s+than|nlt|"
    r"more\s+than)\s+|\b)"
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*(\d{1,2}))?\s*(?:year|yr)s?"
    r"[^.;•]{0,40}?\bexperience\b",
    re.IGNORECASE)
_EXP_CUE_LEADING_RE = re.compile(
    r"\b(?:minimum(?:\s+of)?|min\.?|at\s+least|not\s+less\s+than|nlt)\s+"
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*(\d{1,2}))?\s*(?:year|yr)s?",
    re.IGNORECASE)

MAX_PLAUSIBLE_YEARS = 40


def _pairs(matches):
    out = []
    for lo, hi in matches:
        try:
            low = int(lo)
        except (TypeError, ValueError):
            continue
        if not 0 <= low <= MAX_PLAUSIBLE_YEARS:
            continue
        high = None
        if hi:
            try:
                high = int(hi)
            except (TypeError, ValueError):
                high = None
            if high is not None and not low <= high <= MAX_PLAUSIBLE_YEARS:
                high = None
        out.append((low, high))
    return out


def parse_experience_years(text):
    """Return (min_years, max_years) as strings from a job's qualifications.

    Sidra tiers its requirements ("5+ years as a senior Cardiac Technologist",
    "2+ Years specialized experience in cardiac catheterization"), so the LOWEST
    stated bar is the entry requirement recorded as min. `max` is filled only
    when that same phrase gives an explicit range ("3 - 5 years"). Returns
    ("", "") when the posting states no requirement — never guesses.
    """
    text = clean_text(text)
    if not text:
        return ("", "")
    section = _EXPERIENCE_SECTION_RE.search(text)
    if section and section.group(1).strip(" •"):
        # inside the Experience block every "N years" IS a requirement
        pairs = _pairs(_EXP_RANGE_RE.findall(section.group(1)))
    else:
        pairs = _pairs(_EXP_CUE_RE.findall(text)) + \
            _pairs(_EXP_CUE_LEADING_RE.findall(text))
    if not pairs:
        return ("", "")
    low, high = min(pairs, key=lambda p: p[0])
    return (str(low), str(high) if high is not None else "")


# ----------------------------------------------------------------------------
# Category mapping (club enum: doctors | nurses | pharmacists | non_clinical)
# ----------------------------------------------------------------------------

# Oracle CATEGORIES facet values seen on the Sidra tenant -> club category.
# "Allied Health" has no club bucket of its own, so it lands in non_clinical
# (same treatment SEHA and medcare give it). "Enabling Function: <X>" is Sidra's
# name for corporate/support functions (Admin, Finance, IT, …) and is matched by
# prefix so a new sub-function still maps.
CATEGORY_MAP = {
    "physician": "doctors",
    "physicians": "doctors",
    "medical": "doctors",
    "dental": "doctors",
    "nursing": "nurses",
    "pharmacy": "pharmacists",
    "allied health": "non_clinical",
    "research": "non_clinical",
    "education": "non_clinical",
    "administration": "non_clinical",
}
_ENABLING_FUNCTION_PREFIX = "enabling function"

# Campaign requisitions ("Join Our Talent Pool - Future opportunities") are
# open-ended expressions of interest, not a specific vacancy.
_TALENT_POOL_RE = re.compile(r"talent\s*pool|future\s+opportunit", re.IGNORECASE)

_NURSE_RE = re.compile(r"\bnurs(e|es|ing)\b|\bmidwi(fe|ves|fery)\b|\blpn\b",
                       re.IGNORECASE)
_PHARM_RE = re.compile(r"\bpharmac(y|ist|ists|ies|eutical)\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|surgery|dentist|general practitioner|"
    r"consultant|specialist|registrar|medical officer|attending|fellow|"
    r"intensivist|anaesthetist|anesthetist|anesthesiologist|anaesthesiologist|"
    r"obstetrician|gynecologist|gynaecologist|p(a?)ediatrician|psychiatrist|"
    r"radiologist|pathologist|neonatologist|oncologist|cardiologist|"
    r"dermatologist|endocrinologist|gastroenterologist|neurologist|"
    r"ophthalmologist|otolaryngologist|rheumatologist|urologist|nephrologist|"
    r"pulmonologist|hematologist|haematologist|geneticist|"
    # Sidra heads its clinical divisions with physician leadership titles
    # (JobFunction "1260 - DIV CHIEF/PHY")
    r"division\s+chief|division\s+head|chair\s+of\s+department)\b",
    re.IGNORECASE)
# Allied-health / technical roles: clinical-adjacent at most, and checked BEFORE
# _DOCTOR_RE so "Specialist" in "Specialist - Poison Control Center" or
# "Technologist" titles can't win a doctors bucket.
_ALLIED_RE = re.compile(
    r"\b(sonographer|radiographer|technologist|technician|therapist|therapy|"
    r"physiotherapist|dietit(ian|ician)|nutritionist|embryologist|"
    r"phlebotomist|optometrist|audiologist|paramedic|perfusionist|"
    r"pathologist\s+assistant|speech\s+and\s+language|child\s+life|"
    r"scientist|coder)\b",
    re.IGNORECASE)
# Corporate-function words — Sidra titles head-office roles "Specialist - …" /
# "Manager - …" too, so these are tested alongside _ALLIED_RE.
_CORPORATE_RE = re.compile(
    r"\b(auditor|accountant|finance|financial|procurement|supply\s+chain|"
    r"human\s+resources|talent|recruitment|payroll|legal|compliance|ethics|"
    r"marketing|communications|engineer|architect|developer|"
    r"coordinator|administrator|secretary|clerk|receptionist|officer|"
    r"housekeeping|catering|security|facilities|information\s+technology|"
    r"biomedical\s+engineering|counselling|counseling)\b",
    re.IGNORECASE)
# Titles no club bucket fits (psychology / physiology clinicians). Kept as
# non_clinical AND flagged so a human decides (master spec §2).
_AMBIGUOUS_RE = re.compile(
    r"\b(psychologist|physiologist|counsell?or|analyst|"
    r"health\s*care\s*assistant|patient\s+care\s+assistant)\b",
    re.IGNORECASE)


def classify_category(title, oracle_category="", requisition_type=""):
    """Return (club_category, needs_review).

    Oracle's category facet is authoritative when present (populated on every
    live Sidra requisition); the title decides otherwise. A pharmacy title
    overrides the coarse "Allied Health" facet, and a nursing title overrides a
    corporate/administrative one. Talent-pool campaign requisitions and titles
    that match nothing are kept as their best-guess category and flagged
    needs_review — never dropped.
    """
    facet = clean_text(oracle_category).lower()
    mapped = CATEGORY_MAP.get(facet)
    if mapped is None and facet.startswith(_ENABLING_FUNCTION_PREFIX):
        mapped = "non_clinical"
    talent_pool = bool(_TALENT_POOL_RE.search(facet)
                       or _TALENT_POOL_RE.search(clean_text(requisition_type))
                       or clean_text(requisition_type).lower() == "campaigns")
    if mapped and not talent_pool:
        # the facet is department-level, so an unmistakable title still wins
        if mapped != "pharmacists" and _PHARM_RE.search(title):
            return ("pharmacists", False)
        if mapped == "non_clinical" and _NURSE_RE.search(title):
            return ("nurses", False)
        return (mapped, False)
    category, needs_review = _classify_title(title)
    return (category, True if talent_pool else needs_review)


def _classify_title(title):
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


def is_talent_pool(oracle_category="", requisition_type=""):
    return bool(_TALENT_POOL_RE.search(clean_text(oracle_category))
                or _TALENT_POOL_RE.search(clean_text(requisition_type))
                or clean_text(requisition_type).lower() == "campaigns")


# Sidra's portal is bilingual: a few requisitions carry the Arabic schedule
# label instead of the English one.
_ARABIC_SCHEDULE = {
    "كل الوقت": "full_time",       # full time
    "دوام كامل": "full_time",
    "بعض الوقت": "part_time",      # part time
    "دوام جزئي": "part_time",
}


def map_job_type(job_schedule):
    schedule = clean_text(job_schedule)
    arabic = _ARABIC_SCHEDULE.get(schedule)
    if arabic:
        return arabic
    low = schedule.lower()
    if "part" in low:
        return "part_time"
    if "intern" in low:
        return "internship"
    if "contract" in low or "temporary" in low:
        return "contract"
    return "full_time"  # portal default; Sidra requisitions are full-time posts


def parse_city(primary_location, work_location=""):
    """Sidra's requisitions carry only "Qatar" as the primary location (one
    campus, in Doha's Education City), so the country collapses to the default
    city; anything more specific in the HR work location wins."""
    for value in (work_location, primary_location):
        location = clean_text(value)
        city = location.split(",")[0].strip()
        if city and city.lower() not in ("qatar", "qa", "state of qatar"):
            return city
    return DEFAULT_CITY


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def check_robots(session):
    """sidra.org publishes "User-agent: * / Disallow:" (all allowed); the Oracle
    host serves no robots.txt (404 => allowed). Abort if either changes."""
    for robots_url, probe in [
            ("https://www.sidra.org/robots.txt", CAREERS_PAGE),
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
    title = normalise_title(raw_title)
    category, needs_review = classify_category(title)

    return {
        "source": SITE,
        "job_id": str(req.get("Id") or ""),
        "title": title,
        "raw_title": raw_title,
        "company": COMPANY_NAME,
        "work_location": "",
        "city": parse_city(req.get("PrimaryLocation")),
        "country": COUNTRY_NAME,
        # portal never shows salary — capture, don't invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": map_job_type(req.get("JobSchedule")),
        "job_schedule_original": clean_text(req.get("JobSchedule") or ""),
        "job_shift": clean_text(req.get("JobShift") or ""),
        "category": category,
        "category_original": "",
        "requisition_type": "",
        "job_function": clean_text(req.get("JobFunction") or ""),
        "education": clean_text(req.get("StudyLevel") or ""),
        "experience_min_years": "",
        "experience_max_years": "",
        "nationals_only": is_nationals_only(raw_title),
        "talent_pool": False,
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
    """Overlay detail-endpoint fields (Oracle category facet, requisition type,
    schedule, study level, posting end date, full description) onto a listing
    row, and re-run the classifier with the authoritative category."""
    if not detail:
        return row
    oracle_category = clean_text(detail.get("Category") or "")
    requisition_type = clean_text(detail.get("RequisitionType") or "")
    if oracle_category:
        row["category_original"] = oracle_category
    if requisition_type:
        row["requisition_type"] = requisition_type
    row["talent_pool"] = is_talent_pool(oracle_category, requisition_type)
    row["category"], row["needs_review"] = classify_category(
        row["title"], oracle_category, requisition_type)
    if detail.get("JobSchedule"):
        row["job_type"] = map_job_type(detail["JobSchedule"])
        row["job_schedule_original"] = clean_text(detail["JobSchedule"])
    if detail.get("JobShift"):
        row["job_shift"] = clean_text(detail["JobShift"])
    if detail.get("StudyLevel"):
        row["education"] = clean_text(detail["StudyLevel"])
    if detail.get("JobFunction"):
        row["job_function"] = clean_text(detail["JobFunction"])

    work_location = clean_text(
        (detail.get("workLocation") or [{}])[0].get("LocationName") or "")
    row["work_location"] = work_location
    row["city"] = parse_city(detail.get("PrimaryLocation") or row["city"],
                             work_location)

    description = build_description(detail)
    if description:
        row["description"] = description
        row["experience_min_years"], row["experience_max_years"] = \
            parse_experience_years(description)
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

    return {
        "country_name": _s("country", COUNTRY_NAME),
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": _s("city", DEFAULT_CITY),
        "company_name": COMPANY_NAME,
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
        description="Scrape Sidra Medicine (Doha, Qatar) job openings from its "
                    "Oracle Recruiting candidate portal.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N list pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N requisitions (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip the per-NEW-job detail request (category, "
                             "schedule, education, experience and the full "
                             "description come from it)")
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
                                   "category_original": row["category_original"],
                                   "requisition_type": row["requisition_type"],
                                   "category_assigned": row["category"]})
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
