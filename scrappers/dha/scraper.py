#!/usr/bin/env python3
"""Scrape healthcare job openings published by the Dubai Health Authority (DHA).

Data source
-----------
``careers.dha.gov.ae`` (the URL this scraper was requested for) does not
resolve — DHA has no careers host of its own. What DHA *does* run is the
**Sheryan Opportunities** board: vacancies posted by DHA-licensed healthcare
facilities in Dubai, on the authority's own services host.

    https://services.dha.gov.ae/sheryan/wps/portal/home/opportunities

Sheryan is an IBM WebSphere portal whose Opportunities portlet renders its
cards from one public, token-free JSON resource URL:

    GET <base>/p0/IZ7_…=CZ6_…=NJsearchOpportunities=/
        ?opportunitiesSearchVO={"string":"","vacancyType":[],"category":[],
                                "facilityName":[],"nationality":[],
                                "locale":"en","pageSize":100,"pageNo":1}

    -> {"opportunitySummaryResponseDTO": "<json string>"}
       with .opportunities[], .filters and .pagination.

``<base>`` carries WebSphere's ``!ut/p/z1/…`` navigation-state token, so the
scraper never hardcodes it: it GETs the landing page for the token plus
``getViewOpportunitiesURL``, then the results page for
``searchOpportunitiesURL`` and ``getViewOpportunityDetailsURL``, and builds
every later request from those. A portal redeploy changes the token, not the
scraper.

Listing rows carry only title / job id / vacancy type / category / facility /
nationality, so each NEW job's ``viewOpportunityDetails`` page is fetched
(details on by default, ``--no-details`` to skip) for the description, the
requirements list and the facility's contact email/phone.

Classification is the shared two-level taxonomy (``_shared/classification``):
every candidate is scored by ``classify_job(title, skills, description)``,
where the portal's licence-register ``category`` ("Physician", "Nurse and
Midwife", "Allied Health", …) travels only as a *skills* signal and stays in
the rich CSV as the raw ``category_original`` column — it no longer decides
anything. Jobs the classifier rules out of scope are dropped and counted as
``excluded_out_of_scope``; in-scope jobs get ``category`` ("Non Clinical" |
"Public Health"), ``sub_category`` and the role-family trace columns.

Salary is never published on this board -> ``salary_raw = "Not Disclosed"``
and empty numeric fields (master spec §3: capture, never invent).

Time window (master spec §4): the board publishes **no posting date** for any
vacancy, so there is nothing to window on. It lists only currently-open
opportunities, so the first run keeps all of them and later runs rely on
dedup. Results come back newest-first (job ids are sequential —
``OPP-<year>-<counter>``), so pagination stops early once a whole page is
already known; ``--full`` walks every page instead.

Outputs
-------
* dha_jobs.csv                        — rich cumulative store (dedup: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/dha.csv — HealthCareers.club schema.

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
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "dha"
PORTAL_ROOT = "https://services.dha.gov.ae"
LANDING_URL = PORTAL_ROOT + "/sheryan/wps/portal/home/opportunities"
ROBOTS_URL = PORTAL_ROOT + "/robots.txt"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# The board lists only open opportunities and publishes no posting dates, so
# there is no time window to apply — dedup by job_id makes runs incremental.
PAGE_SIZE = 100          # server caps the page size at 100
MAX_PAGES_HARD_LIMIT = 50

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "dha_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COMPANY_ABOUT = (
    "Vacancy published on the Dubai Health Authority's Sheryan Opportunities "
    "board, where DHA-licensed healthcare facilities advertise openings to "
    "licensed health professionals."
)
COUNTRY_NAME = "United Arab Emirates"
COUNTRY_CODE = "AE"
COUNTRY_DIAL = "+971"
DEFAULT_CITY = "Dubai"

RICH_COLUMNS = [
    "source", "job_id", "title", "raw_title", "company", "raw_facility_name",
    "facility_id", "city", "country", "salary_raw", "salary_min_monthly",
    "salary_max_monthly", "salary_period_original", "job_type", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in",
    "category_original", "vacancy_type", "nationality_requirement",
    "experience_min_years", "experience_max_years", "interested_count",
    "contact_email", "contact_phones", "needs_review", "posted_date",
    "first_seen_date", "description", "requirements", "job_url", "scraped_at",
]

log = logging.getLogger("dha_scraper")

# ----------------------------------------------------------------------------
# Text / title helpers
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")

# Words kept fully uppercase when re-casing an ALL-CAPS title / facility name.
_ACRONYMS = {
    "DHA", "MOH", "DOH", "HAAD", "ICU", "NICU", "PICU", "OT", "ER", "OPD",
    "ENT", "OB", "GYN", "OBGY", "MRI", "CT", "GP", "UAE", "RN", "IVF", "USG",
    "T&CM", "AYUSH", "HR", "IT", "CEO", "CFO", "PRO", "FZ", "DHCC",
}

# Facility-name noise: legal-entity suffixes carried by the licence record.
_ENTITY_SUFFIX_RE = re.compile(
    r"\b(L\.?\s?L\.?\s?C\.?|FZ-?\s?LLC|FZ-?LLC|FZ\s?CO|FZCO|FZE|"
    r"D\.?M\.?C\.?C\.?|S\.?P\.?C\.?)\b\.?",
    re.IGNORECASE)


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(text):
    return clean_text(_TAG_RE.sub(" ", str(text or "")))


def recase(text):
    """Title-case a shouty string, keeping known acronyms uppercase.
    Mixed-case input is returned unchanged."""
    text = clean_text(text)
    if not text or text != text.upper():
        return text
    return " ".join(
        word if word.strip("()-/.,&") in _ACRONYMS else word.title()
        for word in text.split()
    )


def clean_facility(raw_name):
    """'BELLA ROMA SPECIALTY HOSPITAL L.L.C' -> 'Bella Roma Specialty Hospital'."""
    name = _ENTITY_SUFFIX_RE.sub(" ", clean_text(raw_name))
    name = _WS_RE.sub(" ", name).strip(" -,.")
    return recase(name)


# ----------------------------------------------------------------------------
# Classification (shared two-level taxonomy) / company type (club enum)
# ----------------------------------------------------------------------------

_PHARMA_COMPANY_RE = re.compile(
    r"pharmac|laborator|\blab\b|diagnost|biotech|life ?science|therapeut",
    re.IGNORECASE)


def classify_row(row):
    """Run the shared classifier over a built rich row (after any detail
    enrichment) and write the verdict back into it.

    The portal's licence-register category is a curated role signal, so it
    travels in the `skills` argument; it stays in the rich CSV only as the
    raw `category_original` column. Returns the verdict; in_scope False
    means the caller must DROP the row (counted excluded_out_of_scope)."""
    verdict = classify_job(
        row.get("raw_title") or row.get("title", ""),
        row.get("category_original", ""),
        " ".join(filter(None, [row.get("description", ""),
                               row.get("requirements", "")])))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = verdict["needs_review"]
    return verdict


def classify_company_type(name):
    """Club enum: hospital | pharma. Pharmacies, labs and diagnostic centres
    on the licence register map to pharma, every other facility to hospital."""
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# City, job type and experience, read off the free-text description
# ----------------------------------------------------------------------------
#
# The board carries no structured location, schedule or experience field. The
# three parsers below only report what the posting states explicitly and fall
# back to the DHA default (Dubai / full_time / blank experience) otherwise.

_EMIRATES = [
    ("Abu Dhabi", re.compile(r"\babu\s?dhabi\b", re.IGNORECASE)),
    ("Sharjah", re.compile(r"\bsharjah\b", re.IGNORECASE)),
    ("Ajman", re.compile(r"\bajman\b", re.IGNORECASE)),
    ("Ras Al Khaimah", re.compile(r"\bras\s?al\s?khaimah\b|\brak\b", re.IGNORECASE)),
    ("Fujairah", re.compile(r"\bfujairah\b", re.IGNORECASE)),
    ("Umm Al Quwain", re.compile(r"\bumm\s?al\s?quwain\b", re.IGNORECASE)),
    ("Al Ain", re.compile(r"\bal\s?ain\b", re.IGNORECASE)),
]
_DUBAI_RE = re.compile(r"\bdubai\b", re.IGNORECASE)

_PART_TIME_RE = re.compile(r"\bpart[\s-]?time\b", re.IGNORECASE)
_FULL_TIME_RE = re.compile(r"\bfull[\s-]?time\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(remote|work from home|telehealth|teleconsult\w*)\b",
                        re.IGNORECASE)


def detect_city(text):
    """Emirate named in the posting text, else Dubai (DHA licenses Dubai
    facilities, so an unqualified posting is a Dubai one)."""
    text = clean_text(text)
    if _DUBAI_RE.search(text):
        return DEFAULT_CITY
    for city, pattern in _EMIRATES:
        if pattern.search(text):
            return city
    return DEFAULT_CITY


def detect_job_type(text):
    """Club job_type enum. 'PART TIME OR FULL TIME' -> full_time (the wider
    offer); only an unambiguous part-time posting becomes part_time."""
    text = clean_text(text)
    if _REMOTE_RE.search(text) and not _PART_TIME_RE.search(text):
        return "remote"
    if _PART_TIME_RE.search(text) and not _FULL_TIME_RE.search(text):
        return "part_time"
    return "full_time"


_YEARS = (r"(?:(?P<lo>\d{1,2})\s*(?:-|–|to)\s*(?P<hi>\d{1,2})|(?P<one>\d{1,2}))"
          r"\s*(?P<plus_before>\+)?\s*(?:year|yr)s?\b"
          r"(?P<plus_after>\s*(?:\+|or more|and above|and more))?")

_EXPERIENCE_RE = re.compile(_YEARS + r"[^.\n]{0,40}?\bexp", re.IGNORECASE)
_EXPERIENCE_PREFIX_RE = re.compile(
    r"\bexp(?:erience)?\b[^.\n]{0,40}?" + _YEARS, re.IGNORECASE)


def parse_experience(text):
    """Return (min_years, max_years) as strings when the posting states an
    experience requirement, else ("", "").

    Handles "at least 4 year experience", "3-5 years experience",
    "experience of 2 years", "5+ years experience" (open-ended -> no max).
    """
    text = clean_text(text)
    match = _EXPERIENCE_RE.search(text) or _EXPERIENCE_PREFIX_RE.search(text)
    if not match:
        return ("", "")
    groups = match.groupdict()
    if groups.get("one") is not None:
        open_ended = groups.get("plus_before") or groups.get("plus_after")
        return (groups["one"], "" if open_ended else groups["one"])
    lo, hi = int(groups["lo"]), int(groups["hi"])
    if hi < lo:
        lo, hi = hi, lo
    return (str(lo), str(hi))


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, text/html;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def check_robots(session):
    """services.dha.gov.ae serves no robots.txt (the path returns the portal's
    HTML), so nothing is disallowed — but check every run and abort if that
    ever changes (master spec §7)."""
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    looks_like_robots = (resp.status_code < 400
                         and "<html" not in resp.text[:500].lower())
    rp.parse(resp.text.splitlines() if looks_like_robots else [])
    if not rp.can_fetch(USER_AGENT, LANDING_URL):
        sys.exit("robots.txt disallows the Sheryan opportunities board — aborting.")
    log.info("robots.txt check passed")


def _request(session, url, *, params=None, as_json=True):
    """GET with retries/backoff on 429/5xx and network errors. Returns None
    once the retries are exhausted — callers degrade, they never crash."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, params=params,
                               timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


_BASE_HREF_RE = re.compile(r'<base href="([^"]+)"')
_VIEW_URL_RE = re.compile(r"getViewOpportunitiesURL\s*=\s*'([^']+)'")
_SEARCH_URL_RE = re.compile(r"searchOpportunitiesURL\s*=\s*'([^']+)'")
_DETAIL_URL_RE = re.compile(r"getViewOpportunityDetailsURL\s*=\s*'([^']+)'")


def bootstrap(session):
    """Discover the portal's current URLs.

    Returns (landing_base, search_url, detail_path_template). The landing base
    is the stable public entry point used for `job_url`; the portlet paths are
    read from the pages themselves so a WebSphere redeploy (which rewrites the
    `!ut/p/z1/…` navigation token) cannot silently break the run.
    """
    landing = _request(session, LANDING_URL, as_json=False)
    if not landing:
        sys.exit("Could not load the Sheryan opportunities page — aborting.")
    base_match = _BASE_HREF_RE.search(landing)
    view_match = _VIEW_URL_RE.search(landing)
    if not (base_match and view_match):
        sys.exit("Opportunities landing page changed shape (no base/view URL) "
                 "— aborting before writing anything.")
    landing_base = base_match.group(1)

    results = _request(session, landing_base + view_match.group(1),
                       params={"nationality": ""}, as_json=False)
    if not results:
        sys.exit("Could not load the opportunities result page — aborting.")
    search_match = _SEARCH_URL_RE.search(results)
    detail_match = _DETAIL_URL_RE.search(results)
    if not search_match:
        sys.exit("Opportunities page no longer exposes searchOpportunities "
                 "— aborting before writing anything.")
    detail_template = detail_match.group(1) if detail_match else ""
    log.info("Portal endpoints discovered")
    return landing_base, landing_base + search_match.group(1), detail_template


def fetch_page(session, search_url, page_no):
    """One page of the opportunities search. Returns (opportunities, total)."""
    search_vo = {
        "string": "",
        "vacancyType": [],
        "category": [],
        "facilityName": [],
        "nationality": [],
        "locale": "en",
        "pageSize": PAGE_SIZE,
        "pageNo": page_no,
    }
    payload = _request(session, search_url,
                       params={"opportunitiesSearchVO": json.dumps(search_vo)})
    if not payload:
        return None, None
    raw_dto = payload.get("opportunitySummaryResponseDTO")
    if not raw_dto:
        log.warning("Page %d returned no opportunitySummaryResponseDTO", page_no)
        return [], None
    try:
        dto = json.loads(raw_dto)
    except ValueError as exc:
        log.warning("Page %d payload is not valid JSON (%s)", page_no, exc)
        return [], None
    total = (dto.get("pagination") or {}).get("totalNumberOfRecords")
    return (dto.get("opportunities") or []), total


# ----------------------------------------------------------------------------
# Detail page parsing (viewOpportunityDetails)
# ----------------------------------------------------------------------------
#
# The portlet renders the description and the requirements list client-side
# from two JS string literals, and the facility's contact details into the
# card markup:
#     var description = 'Looking for a Registered Nurse…\nImmediate joiners';
#     var jobRequirement = '[first requirement, second requirement]';

_JS_DESCRIPTION_RE = re.compile(r"var\s+description\s*=\s*'((?:[^'\\]|\\.)*)'")
_JS_REQUIREMENT_RE = re.compile(r"var\s+jobRequirement\s*=\s*'((?:[^'\\]|\\.)*)'")
_MAILTO_RE = re.compile(r'href="mailto:([^"]+)"')
_PHONE_RE = re.compile(r'number-ar-ltr"\s*>\s*([+\d][^<]*)<')

_JS_ESCAPES = {"n": "\n", "t": " ", "r": " ", "'": "'", '"': '"', "\\": "\\"}


def unescape_js_string(raw):
    """Decode the escapes of a single-quoted JS literal (\\n, \\', \\\\)."""
    out, i = [], 0
    raw = raw or ""
    while i < len(raw):
        char = raw[i]
        if char == "\\" and i + 1 < len(raw):
            out.append(_JS_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(char)
            i += 1
    return html_lib.unescape("".join(out)).strip()


def parse_detail_html(page_html):
    """Extract description / requirements / contacts from a details page.
    Every field is best-effort: a template change degrades single fields."""
    out = {}
    page_html = page_html or ""

    desc_match = _JS_DESCRIPTION_RE.search(page_html)
    if desc_match:
        description = unescape_js_string(desc_match.group(1))
        out["description"] = _WS_RE.sub(" ", description.replace("\n", " ")).strip()

    req_match = _JS_REQUIREMENT_RE.search(page_html)
    if req_match:
        requirements = unescape_js_string(req_match.group(1)).strip()
        if requirements.startswith("[") and requirements.endswith("]"):
            requirements = requirements[1:-1]
        items = [clean_text(item) for item in requirements.split(",")]
        out["requirements"] = "; ".join(item for item in items if item)

    emails = _MAILTO_RE.findall(page_html)
    if emails:
        out["contact_email"] = clean_text(emails[0])
    phones = [clean_text(p) for p in _PHONE_RE.findall(page_html)]
    if phones:
        out["contact_phones"] = "; ".join(dict.fromkeys(phones))
    return out


def build_detail_url(landing_base, detail_template, job_id):
    """Public detail URL for a job, or the board URL when the portal stopped
    exposing the details template."""
    if not detail_template:
        return LANDING_URL
    path = detail_template.replace("_jobId_", job_id).replace("_locale_", "en")
    return "{}{}?jobId={}&locale=en".format(landing_base, path, job_id)


def fetch_job_detail(session, url):
    page_html = _request(session, url, as_json=False)
    return parse_detail_html(page_html) if page_html else {}


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def opportunity_to_rich_row(opp, job_url, today):
    raw_title = clean_text(opp.get("jobTitle") or "")
    raw_facility = clean_text(opp.get("facilityName") or "")
    portal_category = clean_text(opp.get("category") or "")
    vacancy_type = clean_text(opp.get("vacancyType") or "")
    return {
        "source": SITE,
        "job_id": clean_text(opp.get("jobID") or ""),
        "title": recase(raw_title),
        "raw_title": raw_title,
        "company": clean_facility(raw_facility),
        "raw_facility_name": raw_facility,
        "facility_id": clean_text(opp.get("facilityID") or ""),
        "city": DEFAULT_CITY,
        "country": COUNTRY_NAME,
        # The board never publishes pay — capture, never invent (spec §3).
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": "full_time",
        # Filled by classify_row() once the detail text is in.
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "category_original": portal_category,
        "vacancy_type": vacancy_type,
        "nationality_requirement": clean_text(opp.get("national") or ""),
        "experience_min_years": "",
        "experience_max_years": "",
        "interested_count": clean_text(opp.get("interested") or ""),
        "contact_email": "",
        "contact_phones": "",
        "needs_review": False,
        # DHA publishes no posting date; first_seen_date records when this
        # scraper first saw the vacancy (see readme.md).
        "posted_date": "",
        "first_seen_date": today,
        "description": "",
        "requirements": "",
        "job_url": job_url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, detail):
    """Fold the detail-page fields into a listing row."""
    for key in ("description", "requirements", "contact_email", "contact_phones"):
        if detail.get(key):
            row[key] = detail[key]
    row["description"] = row["description"][:DESCRIPTION_MAX_CHARS]

    text = " ".join((row["title"], row["description"], row["requirements"]))
    row["city"] = detect_city(text)
    row["job_type"] = detect_job_type(text)
    exp_min, exp_max = parse_experience(text)
    row["experience_min_years"] = exp_min
    row["experience_max_years"] = exp_max
    return row


def _blank(value):
    """NaN-safe string coercion for values read back from the rich CSV."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def rich_row_to_club_row(r):
    company = _blank(r.get("company")) or "DHA-licensed healthcare facility"
    return {
        "country_name": r.get("country") or COUNTRY_NAME,
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": r.get("city") or DEFAULT_CITY,
        "company_name": company,
        "company_type": classify_company_type(company),
        "company_logo": "",
        "company_about": COMPANY_ABOUT,
        "title": r.get("title", ""),
        "description": r.get("description", ""),
        "job_type": r.get("job_type") or "full_time",
        "category": r.get("category", ""),
        "sub_category": r.get("sub_category", ""),
        "role_family": r.get("role_family", ""),
        "application_url": r.get("job_url", LANDING_URL),
        # No posting date is published; the date the board first listed the
        # job for us is the closest honest value.
        "posted_at": r.get("first_seen_date", ""),
        "min_experience": r.get("experience_min_years", ""),
        "max_experience": r.get("experience_max_years", ""),
        # No structured qualification field on the board — grounded
        # extraction from the posting text only, never inferred.
        "qualification": extract_qualification(" ".join(filter(None, [
            _blank(r.get("description")), _blank(r.get("requirements"))]))),
        # No salary anywhere on the board — left blank, never invented.
        "min_salary": "",
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
        description="Scrape the Dubai Health Authority (Sheryan) opportunities "
                    "board at services.dha.gov.ae.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N search pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="add at most N new jobs (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip per-job detail pages (description, "
                             "requirements, contacts, city, experience)")
    parser.add_argument("--full", action="store_true",
                        help="walk every page instead of stopping at the first "
                             "page that holds only already-known jobs")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)
    landing_base, search_url, detail_template = bootstrap(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    today = date.today().isoformat()
    log.info("Existing CSV has %d known jobs", len(known_ids))

    counters = {"scanned": 0, "excluded_out_of_scope": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    page_no, total = 1, None
    stop = False

    while not stop:
        if args.max_pages is not None and page_no > args.max_pages:
            break
        if page_no > MAX_PAGES_HARD_LIMIT:
            log.warning("Hit the %d-page hard limit", MAX_PAGES_HARD_LIMIT)
            break
        opportunities, page_total = fetch_page(session, search_url, page_no)
        if opportunities is None:
            log.error("Page %d could not be fetched — stopping pagination", page_no)
            break
        if not opportunities:
            break
        total = page_total or total
        if page_no == 1:
            log.info("Board lists %s open opportunities", total or "?")

        page_had_new = False
        for opp in opportunities:
            counters["scanned"] += 1
            try:
                job_id = clean_text(opp.get("jobID") or "")
                if not job_id:
                    log.warning("Opportunity without a job id skipped: %s",
                                opp.get("jobTitle"))
                    continue
                if job_id in known_ids:
                    counters["duplicates"] += 1
                    continue
                job_url = build_detail_url(landing_base, detail_template, job_id)
                row = opportunity_to_rich_row(opp, job_url, today)
            except Exception as exc:            # one bad card never kills a run
                log.warning("Skipping malformed opportunity %s: %s",
                            opp.get("jobID"), exc)
                continue

            if not args.no_details:
                detail = fetch_job_detail(session, row["job_url"])
                if detail:
                    row = apply_detail(row, detail)
                else:
                    counters["detail_failed"] += 1

            # Shared taxonomy gate: out-of-scope jobs are dropped, never
            # exported (the board is unseen next run too — that is fine, the
            # classifier drops them again). The page still counts as "new"
            # activity so early-stop pagination is not fooled.
            verdict = classify_row(row)
            page_had_new = True
            if not verdict["in_scope"]:
                counters["excluded_out_of_scope"] += 1
                continue

            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["raw_title"],
                                   "category": row["category"],
                                   "sub_category": row["sub_category"],
                                   "category_original": row["category_original"],
                                   "vacancy_type": row["vacancy_type"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1
            page_had_new = True
            if args.limit is not None and counters["new"] >= args.limit:
                log.info("Reached --limit %d", args.limit)
                stop = True
                break

        if stop:
            break
        # Job ids are sequential and the board returns them newest-first, so a
        # page without a single new job means everything after it is known too.
        if not page_had_new and not args.full:
            log.info("Page %d held only known jobs — stopping (use --full to "
                     "walk every page)", page_no)
            break
        if total is not None and page_no * PAGE_SIZE >= int(total):
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
    print("Opportunities scanned: {:>5,}".format(counters["scanned"]))
    print("Excluded out of scope: {:>5,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (old):        {:>5,}  (board publishes no dates)".format(0))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
