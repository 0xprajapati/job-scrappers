#!/usr/bin/env python3
"""Scrape job openings from phcc.gov.qa (Primary Health Care Corporation, Qatar).

Data source
-----------
www.phcc.gov.qa carries NO vacancies of its own — `/careers` is a landing page
whose only outbound link is PHCC's **Oracle E-Business Suite iRecruitment**
visitor portal:

    https://careers.phcc.gov.qa/OA_HTML/IrcVisitor.jsp

That portal has no JSON API (EBS 12.2 renders server-side OAF HTML), no
sitemap and no JobPosting JSON-LD, so the listing is driven the way a visitor
drives it:

1. GET the visitor home page -> follow the `VisJobSchPG` (Job Search) link.
2. POST the search form. The form REFUSES an empty search ("Please fill in
   search criteria values in at least one of the following fields: Job
   Category"), but `ProfessionalAreaCode` is a *multi*-select, so selecting
   every category listed on the page is equivalent to "all jobs" in one POST.
3. Results come back as an OAF "detached table" whose cells carry stable span
   ids — `JobSearchTable:<Field>:<rowIndex>` — which is what this scraper
   parses. The table normally renders every row at once; if the server ever
   returns fewer rows than the "N of M" record-set counter, the navigation
   `goto` event is replayed for the remaining blocks.

Each row also embeds `ppVacancyId=<n>`, which yields a **session-free,
bookmarkable** detail URL used both as `job_url` and as the description source:

    /OA_HTML/OA.jsp?OAFunc=IRC_VIS_VAC_DISPLAY&p_svid=<n>&p_spid=0

(`p_spid` is required and must parse as an integer; its value is not otherwise
used by the visitor page. Omitting it yields "You are trying to access a page
that is no longer active".)

The detail page supplies Department Description / Brief Description / Detailed
Description / Job Requirements / Additional Details. Roughly a fifth of PHCC's
open vacancies leave all five blank — those keep the empty description rather
than inventing one.

Salary is never exposed by iRecruitment -> `salary_raw = "Not Disclosed"`
(master spec §3: capture, never invent, never filter on it).

Everything PHCC posts is healthcare-sector employment (it runs Qatar's public
primary-care health centres), so the healthcare filter reduces to mapping the
portal's own `ProfessionalArea` facet onto the club category enum — a
source-side field, which master spec §2 prefers over title guessing. The facet
is populated on every row; the title classifier only *overrides* it where a
title is unmistakable (a "Consultant Radiologist" filed under Radiology is a
doctor, not a technologist). Nothing is ever silently dropped.

Time window: an ATS lists only OPEN vacancies (PHCC keeps some live for well
over a year), so the first run keeps ALL of them (`INITIAL_WINDOW_DAYS = None`);
later runs use the master-spec watermark (newest stored posted_date minus
WATERMARK_GRACE_DAYS).

robots.txt
----------
`careers.phcc.gov.qa/robots.txt` is Oracle EBS's stock shipped file and
publishes `User-agent: * / Disallow: /`. Master spec §7 makes that binding, so
this scraper **aborts by default**. It runs only when the operator passes
`--ignore-robots`, which logs a loud warning on every run. Use it only with
authorization from PHCC.

Outputs
-------
* phcc_jobs.csv                      — rich cumulative store (dedup: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/phcc.csv — HealthCareers.club 22-col schema.

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
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

SITE = "phcc"
PUBLIC_SITE = "https://www.phcc.gov.qa/careers"

BASE = "https://careers.phcc.gov.qa"
VISITOR_HOME = BASE + "/OA_HTML/IrcVisitor.jsp"
ROBOTS_URL = BASE + "/robots.txt"
# Session-free vacancy page. p_spid must be present and integer-parseable.
JOB_URL_TMPL = BASE + "/OA_HTML/OA.jsp?OAFunc=IRC_VIS_VAC_DISPLAY&p_svid={}&p_spid=0"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# ATS lists only OPEN vacancies, so the first run keeps everything
# (None = no initial window); later runs use the watermark (master spec §4).
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 90
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
MAX_RESULT_PAGES = 50          # navigation-loop guard
# PHCC posts long structured descriptions (duties + requirements); the longest
# observed is ~7.5k chars, so keep them whole.
DESCRIPTION_MAX_CHARS = 12_000

RICH_CSV = "phcc_jobs.csv"
REVIEW_CSV = "needs_review.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COMPANY_NAME = "Primary Health Care Corporation (PHCC)"
COMPANY_ABOUT = (
    "Primary Health Care Corporation (PHCC) is the public provider of primary "
    "health care in the State of Qatar, operating a nationwide network of "
    "health centres, wellness facilities and urgent care centres across Doha, "
    "Al Rayyan, Al Wakra, Al Khor and Al Daayen. PHCC delivers family "
    "medicine, women's and child health, dental, mental health, pharmacy, "
    "laboratory, radiology and preventive health services to residents of "
    "Qatar."
)
COUNTRY_NAME = "Qatar"
COUNTRY_CODE = "QA"
COUNTRY_DIAL = "+974"
DEFAULT_CITY = "Doha"

RICH_COLUMNS = [
    "source", "job_id", "vacancy_id", "title", "raw_title", "company",
    "organization", "city", "country", "location_raw",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "employment_status",
    "category", "category_original",
    "experience_min_years", "experience_max_years",
    "needs_review", "posted_date", "description", "job_requirements",
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

log = logging.getLogger("phcc_scraper")

# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(text):
    """HTML -> flat text, keeping <li>/<br>/<p> boundaries as bullet separators
    so PHCC's bulleted duty lists don't run their items together."""
    text = re.sub(r"<\s*(li|br|/p|/div|/tr)[^>]*>", " • ", str(text or ""),
                  flags=re.IGNORECASE)
    text = clean_text(_TAG_RE.sub(" ", text))
    text = re.sub(r"(?:•\s*)+", "• ", text)
    return text.strip(" •").strip()


# ----------------------------------------------------------------------------
# Title / organization cleanup
# ----------------------------------------------------------------------------

# A handful of PHCC requisitions carry the raw HR position path as their title
# ("063.Operations.Allied Health.Technologist") with the matching cost-centre
# code on the organization ("080000000.Operations"). Those get unpacked into a
# readable title and flagged for review — a human should confirm the wording.
_CODED_TITLE_RE = re.compile(r"^\d+\s*\.")
_LEADING_CODE_RE = re.compile(r"^\d+\s*\.\s*")


def clean_organization(raw_org):
    """ "080000000.Operations" -> "Operations"; plain names pass through."""
    return clean_text(_LEADING_CODE_RE.sub("", clean_text(raw_org)))


def clean_title(raw_title, organization=""):
    """Return (title, needs_review).

    Normal titles ("Specialist Emergency Medicine") pass through untouched.
    A dotted HR-code title is unpacked: the numeric segment is dropped, as is
    any segment that merely repeats the organization, and the rest is joined —
    "063.Operations.Allied Health.Technologist" (org "080000000.Operations")
    becomes "Allied Health Technologist".
    """
    title = clean_text(raw_title)
    if not _CODED_TITLE_RE.match(title):
        return title, False
    org = clean_organization(organization).lower()
    parts = []
    for part in title.split("."):
        part = clean_text(part)
        if not part or part.isdigit() or part.lower() == org:
            continue
        parts.append(part)
    cleaned = clean_text(" ".join(parts))
    return (cleaned or title), True


# ----------------------------------------------------------------------------
# Dates, location, employment status
# ----------------------------------------------------------------------------

def parse_posted_date(raw):
    """iRecruitment renders "23-Jul-2026" -> "2026-07-23"; "" when unparseable."""
    raw = clean_text(raw)
    if not raw:
        return ""
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    log.debug("Unparseable posted date %r", raw)
    return ""


def parse_city(location_raw):
    """ "Doha,QA" -> "Doha". A bare country ("QA") has no city component, so it
    falls back to PHCC's head-office city rather than leaving the field empty."""
    location = clean_text(location_raw)
    city = location.split(",")[0].strip()
    if not city or city.upper() in ("QA", "QAT", "QATAR"):
        return DEFAULT_CITY
    return city


def map_job_type(employment_status):
    low = clean_text(employment_status).lower()
    if "part" in low:
        return "part_time"
    if "intern" in low or "trainee" in low:
        return "internship"
    if "contract" in low or "temporary" in low or "temp" in low:
        return "contract"
    return "full_time"  # portal default; PHCC requisitions are full-time posts


# ----------------------------------------------------------------------------
# Experience parsing
# ----------------------------------------------------------------------------

# PHCC states requirements in free text: "At least 5 years' experience",
# "Minimum 2 years of experience", "Not less than 3 years". Roles are often
# tiered, so the LOWEST stated requirement is the entry bar we record.
_EXP_CUE_RE = re.compile(
    r"(?:\bnot\s+less\s+than\b|\bnlt\b|\bminimum(?:\s+of)?\b|\bmin\.?\b|"
    r"\bat\s+least\b|\bmore\s+than\b)\s*"
    r"(\d{1,2})\s*(?:\+|\s*(?:-|to)\s*\d{1,2})?\s*(?:year|yr)",
    re.IGNORECASE)
_EXP_TRAILING_RE = re.compile(
    r"(\d{1,2})\s*(?:\+)?\s*(?:-|to)?\s*(?:\d{1,2})?\s*(?:year|yr)s?[’'`]?s?\b"
    r"[^.;]{0,40}?\bexperience\b",
    re.IGNORECASE)

# A requirement cue can also introduce *training* rather than experience —
# IRC40694 asks for "Minimum 1 year of internship/training ... Minimum 3 years
# of experience in physiotherapy", where only the 3 is the entry bar. Any cue
# match followed closely by one of these words is discarded.
_TRAINING_CONTEXT_RE = re.compile(
    r"\b(internship|traineeship|training|residency|residence|rotation|"
    r"fellowship|stud(y|ies)|course|programme|program|contract|probation)\b",
    re.IGNORECASE)
TRAINING_LOOKAHEAD_CHARS = 30

MAX_PLAUSIBLE_YEARS = 40


def _cue_years(text):
    """Years introduced by an explicit requirement cue, minus training spans."""
    for match in _EXP_CUE_RE.finditer(text):
        tail = text[match.end():match.end() + TRAINING_LOOKAHEAD_CHARS]
        if _TRAINING_CONTEXT_RE.search(tail):
            continue
        yield int(match.group(1))


def parse_min_experience_years(text):
    """Lowest explicitly-required years of experience in a job description.

    Returns a string ("5") or "" when the text states no requirement. Only
    numbers tied to an explicit requirement cue ("minimum of 5 years", "at
    least 3 years") or directly qualifying the word "experience" count, so
    stray numbers ("2 year contract", "25 years of age") are ignored — as are
    cues that turn out to introduce training rather than experience.
    """
    text = clean_text(text)
    if not text:
        return ""
    years = list(_cue_years(text))
    years += [int(m) for m in _EXP_TRAILING_RE.findall(text)]
    years = [y for y in years if 0 <= y <= MAX_PLAUSIBLE_YEARS]
    return str(min(years)) if years else ""


# ----------------------------------------------------------------------------
# Category mapping (club enum: doctors | nurses | pharmacists | non_clinical)
# ----------------------------------------------------------------------------

# The portal's own "Job Category" (ProfessionalArea) facet -> club category.
# Allied-health and technical areas have no club bucket of their own, so they
# land in non_clinical (same treatment seha/medcare give "Allied Health").
CATEGORY_MAP = {
    "physicians": "doctors",
    "dentist": "doctors",
    "dental health": "non_clinical",
    "nursing": "nurses",
    "pharmacy": "pharmacists",
    "lab": "non_clinical",
    "radiology": "non_clinical",
    "other allied health services": "non_clinical",
    "health information management(him)": "non_clinical",
    "administration & support services": "non_clinical",
    "corporate communications": "non_clinical",
    "engineering": "non_clinical",
    "executive leadership": "non_clinical",
    "governance": "non_clinical",
    "information & communication technology": "non_clinical",
    "supervisory": "non_clinical",
    "technical administration": "non_clinical",
}

_NURSE_RE = re.compile(r"\bnurs(e|es|ing)\b|\bmidwi(fe|ves|fery)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\bpharmac(y|ist|ists|ies)\b", re.IGNORECASE)
# Physician titles that can appear under a non-physician facet — e.g. a
# "Consultant Radiologist" filed under Radiology, or a "Dentist" under Dental
# Health. Deliberately narrower than the facet: bare "Consultant"/"Specialist"
# are NOT here, because PHCC also titles corporate roles that way.
_DOCTOR_RE = re.compile(
    r"\b(physician|surgeon|dentist|general practitioner|medical officer|"
    r"radiologist|pathologist|psychiatrist|p(a?)ediatrician|obstetrician|"
    r"gynaecologist|gynecologist|anaesthetist|anesthetist|anaesthesiologist|"
    r"anesthesiologist|cardiologist|dermatologist|endocrinologist|"
    r"gastroenterologist|neurologist|nephrologist|oncologist|ophthalmologist|"
    r"orthodontist|otolaryngologist|periodontist|prosthodontist|"
    r"pulmonologist|rheumatologist|urologist|neonatologist|intensivist|"
    r"h(a?)ematologist)\b",
    re.IGNORECASE)


def classify_category(title, professional_area=""):
    """Return (club_category, needs_review).

    The portal's Job Category facet is the primary signal (master spec §2
    prefers source-side classification) and is populated on every row. The
    title only overrides it where it is unmistakable. A row whose facet is
    unknown AND whose title matches nothing is kept as non_clinical and flagged
    needs_review — never dropped.
    """
    area = clean_text(professional_area).lower()
    mapped = CATEGORY_MAP.get(area)

    if mapped:
        # the facet is department-level, so an unmistakable title still wins
        if mapped != "pharmacists" and _PHARM_RE.search(title):
            return "pharmacists", False
        if mapped == "non_clinical":
            if _NURSE_RE.search(title):
                return "nurses", False
            if _DOCTOR_RE.search(title):
                return "doctors", False
        return mapped, False

    # unrecognised facet (PHCC added a new Job Category) — fall back to title
    if _PHARM_RE.search(title):
        return "pharmacists", True
    if _NURSE_RE.search(title):
        return "nurses", True
    if _DOCTOR_RE.search(title):
        return "doctors", True
    return "non_clinical", True


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def check_robots(session, ignore):
    """careers.phcc.gov.qa serves Oracle EBS's stock `Disallow: /`.

    Master spec §7 makes robots.txt binding, so a disallowed target aborts the
    run. `--ignore-robots` overrides that at the operator's explicit
    instruction and warns loudly on every run.
    """
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch %s (%s); assuming allowed", ROBOTS_URL, exc)
        return
    # a 404 page is HTML, not a robots policy — treat it as "no rules"
    looks_like_robots = (resp.status_code < 400
                         and "<html" not in resp.text[:500].lower())
    rp.parse(resp.text.splitlines() if looks_like_robots else [])

    if rp.can_fetch(USER_AGENT, VISITOR_HOME):
        log.info("robots.txt check passed")
        return
    if not ignore:
        sys.exit(
            "robots.txt at {} disallows {} — aborting.\n"
            "PHCC's iRecruitment portal publishes Oracle EBS's stock "
            "'User-agent: * / Disallow: /'. Re-run with --ignore-robots only "
            "if you have authorization from PHCC to crawl it.".format(
                ROBOTS_URL, VISITOR_HOME))
    log.warning("=" * 72)
    log.warning("robots.txt disallows %s; continuing at the operator's "
                "explicit instruction (--ignore-robots).", VISITOR_HOME)
    log.warning("=" * 72)


def _request(session, method, url, **kwargs):
    """GET/POST with retries and exponential backoff; returns text or None."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url,
                                   timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# OAF form handling
# ----------------------------------------------------------------------------

_FORM_ACTION_RE = re.compile(r'<form[^>]*action="([^"]+)"', re.IGNORECASE)
_INPUT_RE = re.compile(r"<input[^>]*>", re.IGNORECASE)
_NAME_RE = re.compile(r'\bname="([^"]*)"')
_VALUE_RE = re.compile(r'\bvalue="([^"]*)"')
_TYPE_RE = re.compile(r'\btype="([^"]*)"')


def parse_form(page):
    """Collect the OAF form's action plus every hidden/text input value.

    OAF POSTs the whole form back on every interaction, and several of those
    hidden fields are per-request MAC tokens (_FORM, _AM_TX_ID_FIELD,
    FORM_MAC_LIST) — replaying a stale set gets the request rejected, so they
    are always re-read from the page that is about to be submitted.
    """
    action = _FORM_ACTION_RE.search(page)
    if not action:
        return None, {}
    fields = {}
    for tag in _INPUT_RE.findall(page):
        name = _NAME_RE.search(tag)
        if not name:
            continue
        input_type = _TYPE_RE.search(tag)
        if input_type and input_type.group(1).lower() in (
                "submit", "button", "image", "checkbox", "radio"):
            continue
        value = _VALUE_RE.search(tag)
        fields[name.group(1)] = html_lib.unescape(value.group(1)) if value else ""
    return html_lib.unescape(action.group(1)), fields


def parse_categories(page):
    """Every option value of the `ProfessionalAreaCode` multi-select.

    Selecting all of them is how this scraper says "all jobs": the form rejects
    an empty search but accepts any non-empty category set.
    """
    select = re.search(
        r'(?is)<select[^>]*name="ProfessionalAreaCode".*?</select>', page)
    if not select:
        return []
    return [html_lib.unescape(v) for v in
            re.findall(r'<option value="([^"]*)"', select.group(0)) if v]


def button_ircaction(page, button_id):
    """OAF stamps a per-render token onto each button's IrcAction value
    (`'IrcAction':'GompHDhtJ3'`); the POST is ignored without the exact one."""
    match = re.search(
        r'id="%s"[^>]*onclick="[^"]*?\'IrcAction\':\'([^\']*)\'' % re.escape(button_id),
        page)
    return match.group(1) if match else ""


# ----------------------------------------------------------------------------
# Result-table parsing
# ----------------------------------------------------------------------------

# Result cells carry stable ids: <span id="JobSearchTable:<Field>:<rowIdx>">.
_ROW_INDEX_RE = re.compile(r'<span id="JobSearchTable:JobTitle:(\d+)"')
_VACANCY_ID_RE = r'hiddenUrlVACVWPP:%d\s+value=[^>]*?ppVacancyId=(\d+)'
_TOTAL_RE = re.compile(r'of (\d+)</option>')
_NAV_OPTION_RE = re.compile(r'<option[^>]*value="(\d+),(\d+)"')
_IRC_NAME_RE = re.compile(r'\b(IRC\d+)\b')


def cell(page, field, row_index):
    match = re.search(
        r'<span id="JobSearchTable:%s:%d"[^>]*>(.*?)</span>'
        % (re.escape(field), row_index), page, re.S)
    return strip_html(match.group(1)) if match else ""


def parse_result_rows(page):
    """Every rendered result row as a dict of raw listing fields."""
    rows = []
    for row_index in sorted(int(i) for i in set(_ROW_INDEX_RE.findall(page))):
        vacancy = re.search(_VACANCY_ID_RE % row_index, page)
        # the Name cell wraps the requisition code plus a "Job Details" link
        name = _IRC_NAME_RE.search(cell(page, "region1", row_index))
        rows.append({
            "job_id": name.group(1) if name else "",
            "vacancy_id": vacancy.group(1) if vacancy else "",
            "raw_title": cell(page, "JobTitle", row_index),
            "organization": cell(page, "OrganizationName", row_index),
            "professional_area": cell(page, "ProfessionalArea", row_index),
            "location_raw": cell(page, "LocationResult", row_index),
            "posted_raw": cell(page, "DatePostedResult", row_index),
            "employment_status": cell(page, "FullTime", row_index),
        })
    return rows


def parse_total(page):
    """Total matches from the record-set navigator ("1-10 of 15"); 0 if absent
    (a single-block result set has no navigator)."""
    match = _TOTAL_RE.search(page)
    return int(match.group(1)) if match else 0


def parse_nav_blocks(page):
    """Record-set navigator options as (start_row, block_size) pairs."""
    match = re.search(r'(?is)<select[^>]*_navChoiceSubmit.*?</select>', page)
    if not match:
        return []
    return [(int(a), int(b)) for a, b in _NAV_OPTION_RE.findall(match.group(0))]


# ----------------------------------------------------------------------------
# Listing flow
# ----------------------------------------------------------------------------

def open_job_search(session):
    """Visitor home -> Job Search page (its OAF tokens are session-scoped, so
    the link has to be followed rather than reconstructed)."""
    home = _request(session, "GET", VISITOR_HOME)
    if not home:
        return None
    link = re.search(
        r'href="(/OA_HTML/OA\.jsp\?page=[^"]*VisJobSchPG[^"]*)"', home)
    if not link:
        log.error("Job Search link not found on the visitor home page — "
                  "the portal layout may have changed")
        return None
    return _request(session, "GET", BASE + html_lib.unescape(link.group(1)))


def submit_search(session, search_page):
    """POST the Job Search form with every Job Category selected.

    The form rejects an empty search, and `ProfessionalAreaCode` is a
    multi-select, so passing every option is the portal's own "match anything".
    """
    action, fields = parse_form(search_page)
    if not action:
        log.error("No form found on the Job Search page")
        return None
    categories = parse_categories(search_page)
    if not categories:
        log.error("No Job Category options found — cannot search "
                  "(the form requires at least one)")
        return None
    log.info("Searching all %d job categories", len(categories))

    fields.pop("ProfessionalAreaCode", None)
    fields.update({
        "event": "update",
        "source": "Go",
        "IrcAction": button_ircaction(search_page, "Go"),
        "evtSrcRowIdx": "",
        "evtSrcRowId": "",
    })
    payload = list(fields.items()) + [("ProfessionalAreaCode", c) for c in categories]
    return _request(session, "POST", BASE + action, data=payload)


def submit_nav_block(session, results_page, start_row, block_size):
    """Replay the record-set navigator's `goto` event for a later block."""
    action, fields = parse_form(results_page)
    if not action:
        return None
    fields.update({
        "event": "goto",
        "source": "JobSearchTable",
        "value": str(start_row),
        "size": str(block_size),
        "direction:JobSearchTable": "N",
    })
    return _request(session, "POST", BASE + action, data=list(fields.items()))


def collect_listing_rows(session, max_pages):
    """All result rows, following the record-set navigator when needed.

    The OAF "detached table" normally renders the entire result set in one
    response (it paginates client-side), so the navigator loop is a safety net
    for the day PHCC's vacancy count outgrows a single block.
    """
    search_page = open_job_search(session)
    if not search_page:
        return []
    results = submit_search(session, search_page)
    if not results:
        return []
    if "Please fill in search criteria" in results:
        log.error("Portal rejected the search criteria — the Job Category "
                  "field may have been renamed")
        return []

    rows = parse_result_rows(results)
    total = parse_total(results)
    log.info("Result page 1: %d rows rendered (portal reports %s matches)",
             len(rows), total or "no")

    if total and len(rows) < total:
        # server sent one block at a time — walk the remaining blocks
        seen_starts = {1}
        page = 1
        for start, size in parse_nav_blocks(results):
            if start in seen_starts or len(rows) >= total:
                continue
            page += 1
            if page > min(max_pages or MAX_RESULT_PAGES, MAX_RESULT_PAGES):
                log.warning("Stopping at page %d (page limit)", page - 1)
                break
            seen_starts.add(start)
            block = submit_nav_block(session, results, start, size)
            if not block:
                break
            block_rows = parse_result_rows(block)
            log.info("Result page %d (rows %d-%d): %d rows",
                     page, start, start + size - 1, len(block_rows))
            if not block_rows:
                break
            rows.extend(block_rows)
            results = block
        if len(rows) < total:
            log.warning("Collected %d of %d reported matches", len(rows), total)

    # a row can appear twice if the navigator overlaps blocks
    unique, seen = [], set()
    for row in rows:
        key = row["job_id"] or row["vacancy_id"]
        if key and key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


# ----------------------------------------------------------------------------
# Detail page
# ----------------------------------------------------------------------------

# Rendered in this order; each is anchored by <span id="<name>" class="xhd">
# and its content sits in the following <td align="left"> cells.
DETAIL_SECTIONS = [
    ("DepartmentDescription", "Department"),
    ("IrcBriefDescription", ""),
    ("DetailedDescription", ""),
    ("JobRequirements", "Requirements"),
    ("AdditionalDetails", "Additional details"),
]
# The Skills table follows the last description section; its column headers use
# generated OASH__ ids, which makes a reliable end marker.
_DETAIL_END_RE = re.compile(r'<span id="OASH__')
_TD_LEFT_RE = re.compile(r'<td align="left">(.*?)</td>', re.S)


def parse_detail_sections(page):
    """Map section id -> flattened text. Missing/empty sections are omitted.

    Around a fifth of PHCC's open vacancies leave every section blank; those
    legitimately produce an empty description rather than a fabricated one.
    """
    anchors = []
    for section_id, _ in DETAIL_SECTIONS:
        index = page.find('<span id="%s"' % section_id)
        if index >= 0:
            anchors.append((index, section_id))
    anchors.sort()
    if not anchors:
        return {}

    end_match = _DETAIL_END_RE.search(page, anchors[-1][0])
    end = end_match.start() if end_match else len(page)

    sections = {}
    for position, (index, section_id) in enumerate(anchors):
        stop = anchors[position + 1][0] if position + 1 < len(anchors) else end
        body = " ".join(_TD_LEFT_RE.findall(page[index:max(stop, index)]))
        text = strip_html(body)
        if text:
            sections[section_id] = text
    return sections


def build_description(sections):
    """Full posting text in the order the portal renders it, each optional
    section labelled so the concatenation stays readable."""
    parts = []
    for section_id, label in DETAIL_SECTIONS:
        if section_id == "JobRequirements":
            continue  # stored separately in the rich CSV, appended below
        chunk = sections.get(section_id, "")
        if chunk:
            parts.append("{}: {}".format(label, chunk) if label else chunk)
    requirements = sections.get("JobRequirements", "")
    if requirements:
        parts.append("Requirements: " + requirements)
    return " ".join(parts)[:DESCRIPTION_MAX_CHARS]


def fetch_detail(session, vacancy_id):
    page = _request(session, "GET", JOB_URL_TMPL.format(vacancy_id))
    if not page:
        return None
    if "no longer active" in page:
        log.warning("Vacancy %s detail page expired", vacancy_id)
        return None
    return parse_detail_sections(page)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def listing_to_rich_row(listing):
    organization = clean_organization(listing["organization"])
    title, coded = clean_title(listing["raw_title"], listing["organization"])
    category, review = classify_category(title, listing["professional_area"])

    return {
        "source": SITE,
        "job_id": listing["job_id"],
        "vacancy_id": listing["vacancy_id"],
        "title": title,
        "raw_title": listing["raw_title"],
        "company": COMPANY_NAME,
        "organization": organization,
        "city": parse_city(listing["location_raw"]),
        "country": COUNTRY_NAME,
        "location_raw": listing["location_raw"],
        # iRecruitment never shows pay — capture, don't invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": map_job_type(listing["employment_status"]),
        "employment_status": listing["employment_status"],
        "category": category,
        "category_original": listing["professional_area"],
        "experience_min_years": "",
        "experience_max_years": "",
        "needs_review": bool(review or coded),
        "posted_date": parse_posted_date(listing["posted_raw"]),
        "description": "",
        "job_requirements": "",
        "job_url": (JOB_URL_TMPL.format(listing["vacancy_id"])
                    if listing["vacancy_id"] else ""),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, sections):
    """Overlay the detail page's description sections onto a listing row and
    derive the experience floor from them."""
    if not sections:
        return row
    row["description"] = build_description(sections)
    row["job_requirements"] = sections.get("JobRequirements", "")[:DESCRIPTION_MAX_CHARS]
    # requirements state the experience bar; fall back to the whole posting
    row["experience_min_years"] = (
        parse_min_experience_years(row["job_requirements"])
        or parse_min_experience_years(row["description"]))
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
        # no salary data on the iRecruitment portal — blank, never invented
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        # the portal exposes no closing date
        "expires_at": "",
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date), or None = keep everything (first run: the
    ATS lists only open vacancies, so all of them are current)."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max()
                    - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    if INITIAL_WINDOW_DAYS is not None:
        return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    return None


def within_window(posted_date, cutoff):
    if cutoff is None:
        return True
    # a vacancy with no parseable date is kept rather than silently dropped
    return not posted_date or posted_date >= cutoff


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
        description="Scrape Primary Health Care Corporation (phcc.gov.qa) "
                    "vacancies from its Oracle iRecruitment portal.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N result blocks (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N vacancies (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip the per-NEW-job detail request (the full "
                             "description and experience floor come from it)")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="continue even though careers.phcc.gov.qa "
                             "disallows crawlers; use only with PHCC's "
                             "authorization")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session, args.ignore_robots)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; cutoff: %s",
             len(known_ids), cutoff or "none (keeping all open vacancies)")

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []

    for listing in collect_listing_rows(session, args.max_pages):
        if args.limit is not None and counters["scanned"] >= args.limit:
            log.info("Reached --limit %d — stopping", args.limit)
            break
        counters["scanned"] += 1
        try:
            row = listing_to_rich_row(listing)
        except Exception as exc:                      # one bad row never kills a run
            log.warning("Skipping malformed row %s: %s", listing.get("job_id"), exc)
            continue
        if not row["job_id"]:
            log.warning("Vacancy without a requisition code skipped: %s",
                        row["raw_title"])
            continue
        if not within_window(row["posted_date"], cutoff):
            counters["excluded_old"] += 1
            continue
        if row["job_id"] in known_ids:
            counters["duplicates"] += 1
            continue

        if not args.no_details and row["vacancy_id"]:
            try:
                sections = fetch_detail(session, row["vacancy_id"])
            except Exception as exc:
                log.warning("Detail fetch failed for %s: %s", row["job_id"], exc)
                sections = None
            if sections is None:
                counters["detail_failed"] += 1
            else:
                row = apply_detail(row, sections)

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"],
                               "raw_title": row["raw_title"],
                               "category_original": row["category_original"],
                               "category": row["category"]})
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
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, count = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, count)

    if review_log:
        pd.DataFrame(review_log).to_csv(REVIEW_CSV, index=False)
        log.info("Wrote %s (%d rows)", REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Vacancies scanned:     {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(
        cutoff or "no cutoff", counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
