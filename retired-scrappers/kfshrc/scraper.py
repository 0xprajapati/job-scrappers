#!/usr/bin/env python3
"""Scrape job openings from kfshrc.edu.sa (King Faisal Specialist Hospital &
Research Centre — KFSH&RC, Saudi Arabia).

Data source
-----------
www.kfshrc.edu.sa/en/careers/jobs-listing renders its cards client-side from a
first-party JSON endpoint — a server-side proxy in front of Sitecore Search
(the page carries the widget config in `<div id="card" data-rfkid=...>`):

    POST /kfhapis/CustomSearch/GetJobSearchResults
    {"rfkId":"rfkid_16","entity":"job","sources":["1201178"],
     "fields":[...], "country":"us","language":"en",
     "sortField":"recently_posted","facetLocations":"All",
     "limit":N,"offset":N}

    -> {"widgets":[{"total_item":24,"limit":..,"offset":..,"content":[ ... ]}]}

The LISTING carries the whole posting — title, department/section, branch,
audience, posting date, apply-by date and the full HTML of summary / duties /
education / experience / other requirements. Querying the same endpoint with
`jobID=<id>` (what the detail page does) returns the identical record, so there
is no detail request to make: one paged listing call per 50 jobs is the whole
crawl.

Endpoint quirks (both handled below):
* An unknown name in `fields`, or an empty `sortField`, makes the proxy answer
  **HTTP 200 with an empty body** — so only the 12 indexed field names are
  requested, and an empty/non-JSON body is retried like a 5xx.
* Past the last page `content` comes back as `null` rather than `[]`.
* All dates are Microsoft-JSON epochs (`/Date(1785013200000)/`) stored at
  midnight **Riyadh** time (UTC+3), so they are converted in UTC+3 — using UTC
  would report every posting one day early.

Salary is never exposed by the portal -> `salary_raw = "Not Disclosed"`.
Everything KFSH&RC posts is healthcare-sector employment (it is a tertiary
referral hospital and biomedical research centre), so the healthcare filter
reduces to mapping titles onto the club category enum; anything the classifier
cannot place is kept as `non_clinical` + `needs_review` (master spec §2 — never
silently dropped).

`location` on this feed is NOT a place — it holds the audience, "External Job"
or "Internal Job" (internal postings are open to current staff only). Both are
kept by default, tagged in the `audience` column; `--external-only` drops the
internal ones. The real place is `branch` (Riyadh / Jeddah / Madinah).

Time window: the portal lists only OPEN postings (24 at the time of writing,
some posted months ago), so the first run keeps ALL of them
(`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
(newest stored posted_date minus WATERMARK_GRACE_DAYS).

robots.txt: kfshrc.edu.sa disallows only `/intranet/*` (and its localised
variants); `/kfhapis/` and `/en/careers/` are allowed. Checked at startup.

Outputs
-------
* kfshrc_jobs.csv                      — rich cumulative store (dedup: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/kfshrc.csv — HealthCareers.club 22-col schema.

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

SITE = "kfshrc"
BASE = "https://www.kfshrc.edu.sa"
CAREERS_PAGE = BASE + "/en/careers/jobs-listing"
API_URL = BASE + "/kfhapis/CustomSearch/GetJobSearchResults"
JOB_URL_TMPL = BASE + "/en/careers/jobs-listing/job?id={}"

# Widget config copied from `<div id="card">` on the jobs-listing page.
RFK_ID = "rfkid_16"
ENTITY = "job"
SOURCES = ["1201178"]
# EXACTLY the indexed field names — one unknown name empties the response.
API_FIELDS = [
    "id", "jobtitle", "departmentsection", "location", "branch", "applyby",
    "duties", "experience", "education", "otherrequirements", "startdate",
    "summary",
]
SORT_FIELD = "recently_posted"  # newest posting date first

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# The portal lists only OPEN postings, so the first run keeps everything
# (None = no initial window); later runs use the watermark (master spec §4).
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

PER_PAGE = 50
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
# KFSH&RC duties lists are long (the longest observed is ~6k chars); keep whole.
DESCRIPTION_MAX_CHARS = 8_000

RICH_CSV = "kfshrc_jobs.csv"
REVIEW_CSV = "needs_review.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Saudi tertiary referral hospital — country fixed, no salary data on the portal.
COMPANY_NAME = "King Faisal Specialist Hospital & Research Centre"
COMPANY_ABOUT = (
    "King Faisal Specialist Hospital & Research Centre (KFSH&RC) is Saudi "
    "Arabia's tertiary and quaternary referral hospital and biomedical research "
    "centre, with facilities in Riyadh, Jeddah and Madinah. It specialises in "
    "oncology, organ transplantation, cardiovascular medicine, neurosciences, "
    "genetics and paediatrics, and runs its own research centre and academic "
    "training programmes."
)
COUNTRY_NAME = "Saudi Arabia"
COUNTRY_CODE = "SA"
COUNTRY_DIAL = "+966"
DEFAULT_CITY = "Riyadh"
# The feed's `branch` values, normalised to the spelling used on the site.
BRANCH_CITIES = {"riyadh": "Riyadh", "jeddah": "Jeddah", "madinah": "Madinah",
                 "medina": "Madinah"}
# Dates are stored at midnight Riyadh time (UTC+3), not UTC.
RIYADH_TZ = timezone(timedelta(hours=3))

RICH_COLUMNS = [
    "source", "job_id", "title", "raw_title", "company", "department",
    "raw_department", "audience", "city", "country",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "category",
    "education", "experience_raw", "experience_min_years",
    "experience_max_years", "other_requirements",
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

log = logging.getLogger("kfshrc_scraper")

# ----------------------------------------------------------------------------
# Text cleaning
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
# KFSH&RC's CMS mangles some curly apostrophes into "?" ("Bachelor?s Degree").
# Only the possessive-'s case is repaired — narrow enough to never touch a
# genuine question mark.
_MOJIBAKE_APOSTROPHE_RE = re.compile(r"(?<=[A-Za-z])\?(?=s\b)")


def clean_text(text):
    text = _MOJIBAKE_APOSTROPHE_RE.sub("’", str(text or ""))
    return _WS_RE.sub(" ", html_lib.unescape(text)).strip()


def strip_html(text):
    """HTML -> flat text, with <li>/<br>/<p> boundaries kept as separators so
    bullet lists don't run their items together."""
    text = re.sub(r"<\s*(li|br|/p|/div|/tr)[^>]*>", " • ", str(text or ""),
                  flags=re.IGNORECASE)
    text = clean_text(_TAG_RE.sub(" ", text))
    text = re.sub(r"(?:•\s*)+", "• ", text)
    return text.strip(" •").strip()


# ----------------------------------------------------------------------------
# Title / department presentation
# ----------------------------------------------------------------------------

# KFSH&RC stores titles and departments in ALL CAPS ("ASSISTANT CONSULTANT,
# PEDIATRIC HEMATOLOGY / ONCOLOGY"). `raw_title` keeps that verbatim; the
# `title` column is title-cased for the job board, preserving the grade roman
# numerals ("STAFF NURSE I") and clinical acronyms.
_ROMAN_NUMERALS = {"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
                   "XI", "XII"}
_ACRONYMS = {a: a for a in (
    "KFSHRC", "KFSH&RC", "KFSH", "SCFHS", "SCHS", "IPA", "CME", "CPR", "ACLS",
    "ICU", "PICU", "NICU", "MICU", "SICU", "CCU", "CVICU", "PACU", "ER", "ED",
    "OPD", "IPD", "CSSD", "ENT", "GI", "OB", "GYN", "MRI", "CT", "PET", "ECG",
    "EKG", "EEG", "EMG", "IVF", "IV", "BMT", "HIV", "TB", "DNA", "RNA", "PCR",
    "IT", "HR", "QA", "QI", "MD", "MBBS", "RN", "LPN", "EMS", "HVAC", "VIP",
    "UK", "USA", "US", "EU",
)}
_ACRONYMS["PHD"] = "PhD"
_SMALL_WORDS = {"a", "an", "and", "the", "of", "for", "to", "in", "on", "at",
                "or", "with", "by", "per", "from", "as", "&"}
# Section/department/unit markers plus the trailing branch letter
# ("CASE MANAGEMENT (Sec)-R" -> "CASE MANAGEMENT").
_DEPT_MARKER_RE = re.compile(
    r"\s*\((?:Sec|Sect|Dpt|Dept|Unit|Div|Grp)\)(?:\s*-\s*[RJM]\b)?",
    re.IGNORECASE)


def _case_token(token, is_first):
    """Title-case one whitespace-delimited token, honouring acronyms, roman
    numerals and hyphen/slash compounds ("NON-MALIGNANT" -> "Non-Malignant")."""
    bare = token.strip("(),.;:/[]").upper()
    if bare in _ACRONYMS:
        return token.upper().replace(bare, _ACRONYMS[bare])
    if bare in _ROMAN_NUMERALS:
        return token.upper()
    if re.search(r"[-/]", token):
        # every part of a compound is capitalised ("IN-PATIENT" -> "In-Patient")
        parts = re.split(r"([-/])", token)
        return "".join(p if p in "-/" else _case_token(p, True) for p in parts)
    lowered = token.lower()
    if not is_first and lowered.strip(",.") in _SMALL_WORDS:
        return lowered
    if not re.search(r"[A-Za-z]", token):
        return token
    return lowered[:1].upper() + lowered[1:]


def title_case(text):
    """ "STAFF NURSE I" -> "Staff Nurse I"; already-mixed-case text is left
    alone (the feed is ALL CAPS today, but a CMS change shouldn't mangle it)."""
    text = clean_text(text)
    if not text or text != text.upper():
        return text
    tokens = re.split(r"(\s+)", text)
    out, first = [], True
    for token in tokens:
        if not token.strip():
            out.append(token)
            continue
        out.append(_case_token(token, first))
        first = False
    return "".join(out)


def clean_department(raw_department):
    """ "ADMIN (Sec)-R/BILLING & ACCOUNTS RECEIVABLE (Dpt)-R"
        -> "Admin / Billing & Accounts Receivable" """
    text = _DEPT_MARKER_RE.sub("", clean_text(raw_department))
    text = re.sub(r"\s*/\s*", " / ", text).strip(" -/")
    return title_case(text)


# ----------------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------------

_MS_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def parse_ms_date(value):
    """ "/Date(1785013200000)/" -> "2026-07-26" (ISO date, Riyadh time).

    The feed stores each date as midnight in Riyadh (UTC+3), i.e. 21:00 UTC the
    previous day, so the conversion must use UTC+3 or every posting reads one
    day early. Values that are already plain dates pass through; anything
    unparseable becomes "".
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _MS_DATE_RE.search(text)
    if not match:
        # tolerate an ISO string if the CMS ever switches format
        iso = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        return iso.group(1) if iso else ""
    try:
        moment = datetime.fromtimestamp(int(match.group(1)) / 1000, RIYADH_TZ)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.date().isoformat()


# ----------------------------------------------------------------------------
# Experience parsing
# ----------------------------------------------------------------------------

# KFSH&RC states requirements in a rigid house style that spells the number and
# repeats it in digits: "Two (2) years of related experience with Master's, or
# four (4) years with Pharm.D./Bachelor's Degree is required." The alternatives
# are qualification pathways, so the LOWEST figure is the entry bar and the
# highest is the upper bound we publish.
_EXP_PAREN_RE = re.compile(r"\(\s*(\d{1,2})\s*\)\s*\+?\s*(?:year|yr)",
                           re.IGNORECASE)
_EXP_PLAIN_RE = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:year|yr)s?\b",
                           re.IGNORECASE)
# Only sentences actually about experience/training count, which keeps
# appraisal windows and CME hours out of the numbers.
_EXP_TOPIC_RE = re.compile(r"experience|training", re.IGNORECASE)
# A figure introduced by one of these is a ceiling or a look-back window
# ("for the last four (4) years", "less than one (1) year of experience"),
# not a requirement.
_EXP_NEGATOR_RE = re.compile(
    r"\b(last|past|previous|less\s+than|fewer\s+than|under|within|up\s+to|"
    r"no\s+more\s+than)\b[^.;]{0,30}$",
    re.IGNORECASE)

MAX_PLAUSIBLE_YEARS = 40


def _years_in_sentence(sentence, pattern):
    years = []
    for match in pattern.finditer(sentence):
        if _EXP_NEGATOR_RE.search(sentence[:match.start()]):
            continue
        value = int(match.group(1))
        if 0 <= value <= MAX_PLAUSIBLE_YEARS:
            years.append(value)
    return years


def parse_experience_years(text):
    """Return (min_years, max_years) as strings from a requirements blurb.

    Only sentences about experience or training are read, and figures that a
    look-back/ceiling phrase introduces are ignored. `max` is blank when the
    posting states a single figure. Both are "" when nothing is stated.

    "Two (2) years of Nursing experience with Bachelor's or four (4) years with
    Associate Degree/Diploma is required." -> ("2", "4")
    """
    text = clean_text(text)
    if not text:
        return ("", "")
    years = []
    for sentence in re.split(r"(?<=[.;])\s+", text):
        if not _EXP_TOPIC_RE.search(sentence):
            continue
        found = _years_in_sentence(sentence, _EXP_PAREN_RE)
        if not found:
            # fall back to bare digits only when the house "(N)" form is absent
            found = _years_in_sentence(sentence, _EXP_PLAIN_RE)
        years.extend(found)
    if not years:
        return ("", "")
    low, high = min(years), max(years)
    return (str(low), str(high) if high != low else "")


# ----------------------------------------------------------------------------
# Category mapping (club enum: doctors | nurses | pharmacists | non_clinical)
# ----------------------------------------------------------------------------

_PHARM_RE = re.compile(r"\bpharmac(y|ist|ists|ies|eutical)\b", re.IGNORECASE)
_NURSE_RE = re.compile(r"\bnurs(e|es|ing)\b|\bmidwi(fe|ves|fery)\b",
                       re.IGNORECASE)
# Allied-health / technical roles. Checked BEFORE the medical-staff ranks so
# "SPECIALIST, RESPIRATORY THERAPY" can't land in `doctors`.
_ALLIED_RE = re.compile(
    r"\b(technologist|technician|technical|therapist|therapy|dietit(ian|ician)|"
    r"nutritionist|sonographer|radiographer|radiograph(y|er)|phlebotomist|"
    r"physiotherap(y|ist)|optometrist|optician|audiologist|paramedic|"
    r"embryologist|perfusionist|prosthetist|orthotist|psychologist|"
    r"polysomnograph(y|er)|respiratory\s+care|social\s+worker|coder|coding)\b",
    re.IGNORECASE)
# Corporate / administrative / academic-research functions. Also checked before
# the ranks, because KFSH&RC titles head-office posts "HEAD, ..." and
# "SPECIALIST, ..." with the same words its clinicians use.
_CORPORATE_RE = re.compile(
    r"\b(admin|administrator|administrative|administration|billing|accounts|"
    r"accountant|accounting|finance|financial|payroll|procurement|purchasing|"
    r"supply\s+chain|warehouse|logistics|marketing|communications|"
    r"public\s+relations|human\s+resources|recruitment|talent|legal|audit|"
    r"quality|compliance|strategy|planning|information\s+technology|"
    r"systems|software|network|engineer|engineering|maintenance|facilities|"
    r"housekeeping|catering|security|driver|receptionist|secretary|clerk|"
    r"case\s+management|medical\s+records|health\s+information|"
    r"professor|lecturer|instructor|researcher|research\s+scientist|"
    r"scientist|biomedical\s+sciences|statistician|bioinformatic)\b",
    re.IGNORECASE)
# KFSH&RC medical-staff ranks and physician/dental specialties. "Consultant",
# "Associate/Assistant Consultant", "Specialist" and "Registrar" are medical
# grades here, and "Locum Tenens" is a stand-in physician post (its own
# posting requires graduation from an accredited medical school).
_DOCTOR_RE = re.compile(
    r"\b(consultant|physician|surgeon|surgery|dentist|dental\s+surgeon|"
    r"registrar|resident|fellow|fellowship|intern|specialist|"
    r"medical\s+officer|locum\s+tenens|general\s+practitioner|"
    r"anaesthe(tist|siologist)|anesthe(tist|siologist)|intensivist|"
    r"obstetrician|gyn(a)?ecologist|p(a)?ediatrician|psychiatrist|radiologist|"
    r"pathologist|neonatologist|oncologist|cardiologist|dermatologist|"
    r"endocrinologist|gastroenterologist|neurologist|nephrologist|"
    r"ophthalmologist|otolaryngologist|rheumatologist|urologist|pulmonologist|"
    r"h(a)?ematologist|geneticist)\b",
    re.IGNORECASE)
# Support roles with no club bucket of their own — non_clinical, not flagged.
_SUPPORT_RE = re.compile(
    r"\b(assistant|aide|attendant|orderly|helper|worker|operator|coordinator|"
    r"supervisor|manager|head|director|chief|officer|analyst)\b",
    re.IGNORECASE)


def classify_category(title, department=""):
    """Return (club_category, needs_review) for a KFSH&RC posting.

    Everything KFSH&RC posts is healthcare-sector employment, so nothing is
    dropped — this only picks the club bucket. Decision order: pharmacy ->
    nursing -> allied/technical -> corporate/academic -> medical-staff rank ->
    generic support -> flag. The department/section is searched alongside the
    title because grades like "STAFF NURSE I" carry no specialty and titles
    like "LOCUM TENENS" carry no discipline.
    """
    text = "{} {}".format(clean_text(title), clean_text(department))
    if _PHARM_RE.search(text):
        return ("pharmacists", False)
    if _NURSE_RE.search(text):
        return ("nurses", False)
    if _ALLIED_RE.search(text):
        return ("non_clinical", False)
    if _CORPORATE_RE.search(text):
        return ("non_clinical", False)
    if _DOCTOR_RE.search(text):
        return ("doctors", False)
    if _SUPPORT_RE.search(text):
        return ("non_clinical", False)
    return ("non_clinical", True)


# KFSH&RC posts standard establishment positions; "Locum Tenens" is the one
# explicitly temporary grade on the feed.
_CONTRACT_RE = re.compile(r"\b(locum\s+tenens|locum|temporary|contract)\b",
                          re.IGNORECASE)
_PART_TIME_RE = re.compile(r"\bpart[\s-]?time\b", re.IGNORECASE)
_INTERNSHIP_RE = re.compile(r"\b(internship|trainee\s+program)\b", re.IGNORECASE)


def map_job_type(title):
    """The feed carries no schedule field, so the grade in the title decides;
    everything else is a full-time establishment post."""
    title = clean_text(title)
    if _PART_TIME_RE.search(title):
        return "part_time"
    if _INTERNSHIP_RE.search(title):
        return "internship"
    if _CONTRACT_RE.search(title):
        return "contract"
    return "full_time"


def parse_city(branch):
    """ "Jeddah" -> "Jeddah"; blank or unknown -> the Riyadh main campus."""
    branch = clean_text(branch)
    if not branch:
        return DEFAULT_CITY
    return BRANCH_CITIES.get(branch.lower(), title_case(branch))


def parse_audience(location):
    """The feed's `location` is the audience, not a place: "External Job" /
    "Internal Job" (internal postings are open to current staff only)."""
    value = clean_text(location).lower()
    if "internal" in value:
        return "internal"
    if "external" in value:
        return "external"
    return ""


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": CAREERS_PAGE,
    })
    return s


def check_robots(session):
    """kfshrc.edu.sa disallows only `/intranet/*`; abort if the API path or the
    careers page ever becomes disallowed."""
    robots_url = BASE + "/robots.txt"
    rp = urllib.robotparser.RobotFileParser(robots_url)
    try:
        resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
        return
    # a 404 page is HTML, not a robots policy — treat it as "no rules"
    looks_like_robots = (resp.status_code < 400
                         and "<html" not in resp.text[:500].lower())
    rp.parse(resp.text.splitlines() if looks_like_robots else [])
    for probe in (API_URL, CAREERS_PAGE):
        if not rp.can_fetch(USER_AGENT, probe):
            sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt check passed")


def post_json(session, url, payload):
    """POST returning parsed JSON, or None once the retries are spent.

    The proxy answers HTTP 200 with an EMPTY body when it rejects a payload
    (unknown `fields` entry, blank `sortField`) and, in practice, occasionally
    on a transient upstream hiccup — so an empty/unparseable body is retried
    exactly like a 5xx instead of being read as "no jobs".
    """
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.post(url, data=json.dumps(payload),
                                timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            resp.raise_for_status()
            if not resp.content.strip():
                last_error = "empty body (payload rejected or upstream hiccup)"
                continue
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES,
              last_error)
    return None


def fetch_jobs_page(session, offset, limit=PER_PAGE):
    """Return (jobs, total). `jobs` is None on a hard failure, [] past the end."""
    payload = {
        "rfkId": RFK_ID,
        "entity": ENTITY,
        "sources": SOURCES,
        "fields": API_FIELDS,
        "country": "us",
        "language": "en",
        "sortField": SORT_FIELD,
        "facetLocations": "All",
        "limit": limit,
        "offset": offset,
    }
    data = post_json(session, API_URL, payload)
    if not data:
        return None, 0
    widgets = data.get("widgets") or []
    if not widgets:
        return [], 0
    widget = widgets[0]
    # past the last page the proxy sends content: null, not []
    return (widget.get("content") or []), (widget.get("total_item") or 0)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_description(job):
    """Full posting text in the order the detail page renders it: summary,
    duties, education, experience, other requirements."""
    parts = []
    for label, key in (("", "summary"),
                       ("Duties: ", "duties"),
                       ("Education: ", "education"),
                       ("Experience: ", "experience"),
                       ("Other requirements: ", "otherrequirements")):
        chunk = strip_html(job.get(key) or "")
        # "N/A" boilerplate in `otherrequirements` adds nothing to a listing
        if not chunk or re.fullmatch(r"(n/?a\.?|none\.?)", chunk, re.IGNORECASE):
            continue
        parts.append(label + chunk)
    return " ".join(parts)[:DESCRIPTION_MAX_CHARS]


def job_to_rich_row(job):
    raw_title = clean_text(job.get("jobtitle") or "")
    raw_department = clean_text(job.get("departmentsection") or "")
    title = title_case(raw_title)
    department = clean_department(raw_department)
    category, needs_review = classify_category(title, department)
    experience_raw = strip_html(job.get("experience") or "")
    exp_min, exp_max = parse_experience_years(experience_raw)

    return {
        "source": SITE,
        "job_id": str(job.get("id") or ""),
        "title": title,
        "raw_title": raw_title,
        "company": COMPANY_NAME,
        "department": department,
        "raw_department": raw_department,
        "audience": parse_audience(job.get("location")),
        "city": parse_city(job.get("branch")),
        "country": COUNTRY_NAME,
        # portal never shows salary — capture, don't invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": map_job_type(raw_title),
        "category": category,
        "education": strip_html(job.get("education") or ""),
        "experience_raw": experience_raw,
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "other_requirements": strip_html(job.get("otherrequirements") or ""),
        "needs_review": needs_review,
        "posted_date": parse_ms_date(job.get("startdate")),
        "expires_at": parse_ms_date(job.get("applyby")),
        "description": build_description(job),
        "job_url": JOB_URL_TMPL.format(job.get("id")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def rich_row_to_club_row(r, today=None):
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

    # An apply-by date in the past means the posting has closed; the portal
    # normally drops those, but a stored row can age out between runs.
    expires_at = _s("expires_at")
    today = (today or date.today()).isoformat()
    is_active = "true" if (not expires_at or expires_at >= today) else "false"

    return {
        "country_name": _s("country", COUNTRY_NAME),
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": _s("city", DEFAULT_CITY),
        "company_name": _s("company", COMPANY_NAME),
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
        # no salary data on the portal — left blank, never invented
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": is_active,
        "expires_at": expires_at,
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date) or None = keep everything (first run: the
    portal only lists open postings, so all of them are current)."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], format="ISO8601",
                               errors="coerce").dropna()
        if not dates.empty:
            return (dates.max()
                    - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
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
        description="Scrape KFSH&RC (kfshrc.edu.sa) job openings from the "
                    "site's Sitecore Search job API.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N list pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N postings (for test runs)")
    parser.add_argument("--external-only", action="store_true",
                        help="skip 'Internal Job' postings (open to current "
                             "KFSH&RC staff only); kept by default")
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
             len(known_ids), cutoff or "none (keeping all open postings)")

    counters = {"scanned": 0, "excluded_old": 0, "excluded_internal": 0,
                "needs_review": 0, "new": 0, "duplicates": 0, "malformed": 0}
    new_rows, review_log = [], []
    offset, page, empty_pages = 0, 1, 0

    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        jobs, total = fetch_jobs_page(session, offset)
        if jobs is None:
            log.error("List request failed on page %d — stopping pagination", page)
            break
        if not jobs:
            empty_pages += 1
            # tolerate a transient empty page; give up after 3 in a row
            if empty_pages >= 3 or offset == 0:
                break
            offset += PER_PAGE
            page += 1
            continue
        empty_pages = 0
        log.info("Page %d: %d postings (total on portal: %d)",
                 page, len(jobs), total)

        page_all_old = True
        for job in jobs:
            if args.limit is not None and counters["scanned"] >= args.limit:
                page_all_old = False
                break
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:
                log.warning("Skipping malformed posting %s: %s",
                            job.get("id"), exc)
                counters["malformed"] += 1
                continue
            if not row["job_id"]:
                log.warning("Posting without id skipped: %s", row["raw_title"])
                counters["malformed"] += 1
                continue
            if within_window(row["posted_date"], cutoff):
                page_all_old = False
            else:
                counters["excluded_old"] += 1
                continue
            if args.external_only and row["audience"] == "internal":
                counters["excluded_internal"] += 1
                continue
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["raw_title"],
                                   "department": row["raw_department"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        if args.limit is not None and counters["scanned"] >= args.limit:
            log.info("Reached --limit %d — stopping", args.limit)
            break
        if page_all_old:
            log.info("Page %d entirely older than %s — stopping", page, cutoff)
            break
        offset += len(jobs)
        if offset >= total or len(jobs) < PER_PAGE:
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
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv(REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to check)", REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Postings scanned:      {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(
        cutoff or "no cutoff", counters["excluded_old"]))
    if args.external_only:
        print("Excluded (internal):   {:>5,}".format(counters["excluded_internal"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    print("Malformed skipped:     {:>5,}".format(counters["malformed"]))


if __name__ == "__main__":
    main()
