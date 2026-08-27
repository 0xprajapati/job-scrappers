#!/usr/bin/env python3
"""Scrape Omega Healthcare (medical coding / revenue-cycle BPO) vacancies
from the Oracle HCM Candidate Experience REST API on
fa-equm-saasfaprod1.fa.ocs.oraclecloud.com.

Data source
-----------
Omega Healthcare's careers site is an Oracle HCM Candidate Experience
tenant with TWO published sites (probed 2026-08-27):

    CX_1001 — the US board (~69 requisitions: coders, auditors, RCM ops)
    CX_2001 — the offshore delivery board (~834 requisitions across India
              AND the Philippines — the site is not India-only, so the
              location is read from each requisition, never assumed)

Oracle CE exposes its public candidate API unauthenticated — no key, no
cookie, no signed parameter — the same platform as the undp scraper this
one is modeled on:

  discovery (paginated on CX_2001, one page on CX_1001):
    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
        &finder=findReqs;siteNumber=<site>,limit=200,offset=<n>,
                sortBy=POSTING_DATES_DESC

  detail (one request per in-window requisition):
    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="<id>",siteNumber=<site>

`finder` needs literal `;` and `,`, so URLs are built as strings — a
params dict would percent-encode the separators and Oracle answers 400.
The host serves no robots.txt (HTTP 404 => no restrictions, the precedent
the undp readme documents); check_robots() still runs so a later-added
robots.txt stops the scraper instead of being ignored.

Verified quirks (probe 2026-08-27)
----------------------------------
* THE DETAIL CARRIES NO DATES on this tenant: `ExternalPostedStartDate`
  and `ExternalPostedEndDate` are null on every sampled detail. The
  listing's plain-date `PostedDate` is the ONLY posted date and decides
  the time window BEFORE any detail fetch. Oracle dates can be
  tenant-timezone (the kfshrc Riyadh-midnight lesson); the 2-day
  watermark grace absorbs any off-by-one.
* The listing caps at 200 rows per page regardless of `limit`, so
  CX_2001 is offset-paginated; POSTING_DATES_DESC ordering lets the crawl
  stop as soon as a whole page predates the cutoff.
* CX_2001 requisitions are mostly DESCRIPTION-LESS: `ExternalDescriptionStr`,
  Responsibilities, Qualifications and even ShortDescriptionStr are all
  empty on sampled rows, and titles are opaque BPO grades ("Clinical
  Executive", "Executive - AR"). The real signal lives in
  `requisitionFlexFields` — Speciality ("HCC", "Hospital Billing",
  "Oncology"), Service Line ("Coding", "Patient Interaction", "Coverage &
  Authorization") and Sub Job Categorization ("Voice/AR", "Support") —
  which are joined (minus "Default" placeholders) with the detail's
  `skills` list and `Category` into the classifier's `skills` signal.
  Expect the classifier to drop most of this board (AR calling, billing,
  patient-interaction voice work are out of taxonomy scope) — that is
  correct behavior, counted as excluded_out_of_scope.
* CX_1001 details are rich: real `skills` ("CPT Coding", "ICD10
  Diagnostic Coding"), a populated `Category` ("Coding"), and flex fields
  "Required Years of Experience" (structured experience, used before the
  prose) plus "Minimum Pay"/"Maximum Pay" (e.g. 21 / 28.25 for an hourly
  US coder). The pay numbers are captured VERBATIM in the rich CSV
  (pay_min_raw/pay_max_raw); the club salary columns stay blank because
  the API states neither period nor currency and nothing is ever
  invented.
* The flex "Full-Time/Part-Time" field drives the club job_type enum;
  everything else exports full_time.

First-run detail cap
--------------------
CX_2001 holds ~834 requisitions. Details are fetched only for ids newer
than the watermark, so steady-state runs cost a handful of requests — but
a first run (or a wide --since) could try to fetch hundreds. When no rich
CSV exists yet, detail fetches are capped at FIRST_RUN_DETAIL_CAP (150),
newest-first; anything in-window beyond the cap is left unfetched and
loudly logged (re-run with --since to backfill). --limit overrides the
cap for test runs.

Classification (shared taxonomy)
--------------------------------
Fetch wide, filter tight: every open requisition on both sites is
enumerated (down to the time cutoff) and the keep/drop and labeling
decision belongs entirely to
_shared/classification.classify_job(title, skills, description).
in_scope False -> DROPPED, counted excluded_out_of_scope, full row (with
its vetoed_by trace) appended to out-of-scope.csv. needs_review True ->
kept AND appended to needs_review.csv.

Outputs
-------
* omega_healthcare_jobs.csv — rich cumulative store (dedup key: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/omega_healthcare.csv — CLUB_COLUMNS export.
* seen_old_ids.csv          — out-of-window ids and their dates.
* out-of-scope.csv          — dropped rows + their skip list (reversible).
* needs_review.csv          — kept but flagged.

Time window: first run keeps requisitions posted in the last
INITIAL_WINDOW_DAYS (14); later runs keep only those newer than the newest
stored posted_date minus WATERMARK_GRACE_DAYS of overlap.

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
import logging
import os
import re
import sys
import time
import urllib.robotparser
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# The one shared classifier (see ../../instructions/master-scraper-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "omega_healthcare"
COMPANY_NAME = "Omega Healthcare"

CE_HOST = "https://fa-equm-saasfaprod1.fa.ocs.oraclecloud.com"
ROBOTS_URL = CE_HOST + "/robots.txt"
REST_BASE = CE_HOST + "/hcmRestApi/resources/latest"

# Two published CE sites: the US board and the offshore delivery board
# (India + Philippines — location is read per requisition, never assumed).
SITE_NUMBERS = ("CX_1001", "CX_2001")

LIST_URL = (
    REST_BASE + "/recruitingCEJobRequisitions"
    "?onlyData=true"
    "&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
    "&finder=findReqs;siteNumber={site},limit={limit},offset={offset},"
    "sortBy=POSTING_DATES_DESC"
)
DETAIL_URL = (
    REST_BASE + "/recruitingCEJobRequisitionDetails"
    "?expand=all&onlyData=true"
    "&finder=ById;Id=%22{job_id}%22,siteNumber={site}"
)
JOB_URL_TEMPLATE = (
    CE_HOST + "/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"
)

# Oracle serves at most 200 listing rows per page on this tenant
# (limit=200 returned exactly 200 of CX_2001's 834), so pagination steps
# by LIST_PAGE_LIMIT and MAX_LIST_PAGES bounds a runaway walk.
LIST_PAGE_LIMIT = 200
MAX_LIST_PAGES = 10

# First-run safety cap on detail fetches (see module docstring).
FIRST_RUN_DETAIL_CAP = 150

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

INITIAL_WINDOW_DAYS = 14
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "omega_healthcare_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "site_number", "title", "company", "city", "state",
    "country", "sectors", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "vetoed_by", "needs_review", "experience_min_years", "work_schedule",
    "pay_min_raw", "pay_max_raw", "posted_date", "valid_through",
    "description", "job_url", "scraped_at",
]

log = logging.getLogger("omega_healthcare_scraper")

# ISO alpha-2 -> (country name, dial code): the fleet-vetted table from
# ../undp/ (itself inverted from ../impactpool/), extended with HK/TW/MO.
# An unknown code exports blank name/dial — never guessed.
COUNTRY_BY_CODE = {
    "AE": ("United Arab Emirates", "+971"), "AF": ("Afghanistan", "+93"),
    "AL": ("Albania", "+355"), "AM": ("Armenia", "+374"),
    "AO": ("Angola", "+244"), "AR": ("Argentina", "+54"),
    "AT": ("Austria", "+43"), "AU": ("Australia", "+61"),
    "AZ": ("Azerbaijan", "+994"), "BA": ("Bosnia and Herzegovina", "+387"),
    "BB": ("Barbados", "+1"), "BD": ("Bangladesh", "+880"),
    "BE": ("Belgium", "+32"), "BF": ("Burkina Faso", "+226"),
    "BG": ("Bulgaria", "+359"), "BI": ("Burundi", "+257"),
    "BJ": ("Benin", "+229"), "BO": ("Bolivia", "+591"),
    "BR": ("Brazil", "+55"), "BW": ("Botswana", "+267"),
    "CA": ("Canada", "+1"),
    "CD": ("Democratic Republic of the Congo", "+243"),
    "CF": ("Central African Republic", "+236"), "CG": ("Congo", "+242"),
    "CH": ("Switzerland", "+41"), "CI": ("Côte d'Ivoire", "+225"),
    "CL": ("Chile", "+56"), "CM": ("Cameroon", "+237"),
    "CN": ("China", "+86"), "CO": ("Colombia", "+57"),
    "CR": ("Costa Rica", "+506"), "CU": ("Cuba", "+53"),
    "CV": ("Cabo Verde", "+238"), "CY": ("Cyprus", "+357"),
    "CZ": ("Czechia", "+420"), "DE": ("Germany", "+49"),
    "DJ": ("Djibouti", "+253"), "DK": ("Denmark", "+45"),
    "DO": ("Dominican Republic", "+1"), "DZ": ("Algeria", "+213"),
    "EC": ("Ecuador", "+593"), "EE": ("Estonia", "+372"),
    "EG": ("Egypt", "+20"), "ER": ("Eritrea", "+291"), "ES": ("Spain", "+34"),
    "ET": ("Ethiopia", "+251"), "FI": ("Finland", "+358"),
    "FJ": ("Fiji", "+679"), "FR": ("France", "+33"), "GA": ("Gabon", "+241"),
    "GB": ("United Kingdom", "+44"), "GE": ("Georgia", "+995"),
    "GH": ("Ghana", "+233"), "GM": ("Gambia", "+220"),
    "GN": ("Guinea", "+224"), "GR": ("Greece", "+30"),
    "GT": ("Guatemala", "+502"), "GW": ("Guinea-Bissau", "+245"),
    "HK": ("Hong Kong", "+852"), "HN": ("Honduras", "+504"),
    "HR": ("Croatia", "+385"), "HT": ("Haiti", "+509"),
    "HU": ("Hungary", "+36"), "ID": ("Indonesia", "+62"),
    "IE": ("Ireland", "+353"), "IL": ("Israel", "+972"),
    "IN": ("India", "+91"), "IQ": ("Iraq", "+964"), "IR": ("Iran", "+98"),
    "IT": ("Italy", "+39"), "JM": ("Jamaica", "+1"), "JO": ("Jordan", "+962"),
    "JP": ("Japan", "+81"), "KE": ("Kenya", "+254"),
    "KG": ("Kyrgyzstan", "+996"), "KH": ("Cambodia", "+855"),
    "KI": ("Kiribati", "+686"), "KR": ("South Korea", "+82"),
    "KW": ("Kuwait", "+965"), "KZ": ("Kazakhstan", "+7"),
    "LA": ("Laos", "+856"), "LB": ("Lebanon", "+961"),
    "LK": ("Sri Lanka", "+94"), "LR": ("Liberia", "+231"),
    "LS": ("Lesotho", "+266"), "LT": ("Lithuania", "+370"),
    "LU": ("Luxembourg", "+352"), "LV": ("Latvia", "+371"),
    "LY": ("Libya", "+218"), "MA": ("Morocco", "+212"),
    "MD": ("Moldova", "+373"), "ME": ("Montenegro", "+382"),
    "MG": ("Madagascar", "+261"), "MK": ("North Macedonia", "+389"),
    "ML": ("Mali", "+223"), "MM": ("Myanmar", "+95"),
    "MN": ("Mongolia", "+976"), "MO": ("Macao", "+853"),
    "MR": ("Mauritania", "+222"), "MT": ("Malta", "+356"),
    "MW": ("Malawi", "+265"), "MX": ("Mexico", "+52"),
    "MY": ("Malaysia", "+60"), "MZ": ("Mozambique", "+258"),
    "NA": ("Namibia", "+264"), "NE": ("Niger", "+227"),
    "NG": ("Nigeria", "+234"), "NI": ("Nicaragua", "+505"),
    "NL": ("Netherlands", "+31"), "NO": ("Norway", "+47"),
    "NP": ("Nepal", "+977"), "NZ": ("New Zealand", "+64"),
    "OM": ("Oman", "+968"), "PA": ("Panama", "+507"), "PE": ("Peru", "+51"),
    "PG": ("Papua New Guinea", "+675"), "PH": ("Philippines", "+63"),
    "PK": ("Pakistan", "+92"), "PL": ("Poland", "+48"),
    "PS": ("Palestine", "+970"), "PT": ("Portugal", "+351"),
    "PY": ("Paraguay", "+595"), "QA": ("Qatar", "+974"),
    "RO": ("Romania", "+40"), "RS": ("Serbia", "+381"),
    "RU": ("Russia", "+7"), "RW": ("Rwanda", "+250"),
    "SA": ("Saudi Arabia", "+966"), "SB": ("Solomon Islands", "+677"),
    "SD": ("Sudan", "+249"), "SE": ("Sweden", "+46"),
    "SG": ("Singapore", "+65"), "SI": ("Slovenia", "+386"),
    "SK": ("Slovakia", "+421"), "SL": ("Sierra Leone", "+232"),
    "SN": ("Senegal", "+221"), "SO": ("Somalia", "+252"),
    "SS": ("South Sudan", "+211"), "SV": ("El Salvador", "+503"),
    "SY": ("Syria", "+963"), "SZ": ("Eswatini", "+268"),
    "TD": ("Chad", "+235"), "TG": ("Togo", "+228"), "TH": ("Thailand", "+66"),
    "TJ": ("Tajikistan", "+992"), "TL": ("Timor-Leste", "+670"),
    "TN": ("Tunisia", "+216"), "TR": ("Turkey", "+90"),
    "TW": ("Taiwan", "+886"), "TZ": ("Tanzania", "+255"),
    "UA": ("Ukraine", "+380"), "UG": ("Uganda", "+256"),
    "US": ("United States", "+1"), "UY": ("Uruguay", "+598"),
    "UZ": ("Uzbekistan", "+998"), "VE": ("Venezuela", "+58"),
    "VN": ("Vietnam", "+84"), "VU": ("Vanuatu", "+678"),
    "YE": ("Yemen", "+967"), "ZA": ("South Africa", "+27"),
    "ZM": ("Zambia", "+260"), "ZW": ("Zimbabwe", "+263"),
}

# country name -> (ISO alpha-2, dial code), for the club export, which only
# has the stored country name to work from.
CODE_BY_COUNTRY = {name: (code, dial)
                   for code, (name, dial) in COUNTRY_BY_CODE.items()}

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table)[^>]*>", " ",
                    markup or "", flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


def parse_requisition_list(payload):
    """(requisitions, total_count) from a recruitingCEJobRequisitions body.

    Oracle wraps the search result — including the requisition array — in a
    single-element `items` list.
    """
    items = (payload or {}).get("items") or []
    if not items:
        return [], 0
    result = items[0] or {}
    return (result.get("requisitionList") or []), result.get("TotalJobsCount")


def parse_requisition_detail(payload):
    """The single requisition object of a ...RequisitionDetails body, or {}."""
    items = (payload or {}).get("items") or []
    return items[0] if items and isinstance(items[0], dict) else {}


def parse_flex_fields(detail):
    """`requisitionFlexFields` as a {Prompt: Value} dict."""
    out = {}
    for field in (detail.get("requisitionFlexFields") or []):
        if not isinstance(field, dict):
            continue
        prompt, value = clean_text(field.get("Prompt")), clean_text(field.get("Value"))
        if prompt and value:
            out[prompt] = value
    return out


def parse_iso_date(text):
    """The date half of an Oracle timestamp, or a plain date."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


_US_STATE_RE = re.compile(r"^[A-Z]{2}$")


def parse_location(primary_location, country_code):
    """(city, state, country_name) from Oracle's PrimaryLocation string.

    "Boca Raton, FL, United States" + US -> ("Boca Raton", "FL",
    "United States"); "Manila Rizal, Philippines" + PH -> ("Manila Rizal",
    "", "Philippines"); "FL, United States" -> ("", "FL", "United States").
    The trailing country segment is dropped when it repeats the country the
    ISO code already names; a remaining trailing 2-letter-uppercase token is
    a state code, and other multi-segment tails become the state.
    """
    location = clean_text(primary_location)
    code = clean_text(country_code).upper()
    country = COUNTRY_BY_CODE.get(code, ("", ""))[0]

    parts = [p.strip() for p in location.split(",") if p.strip()]
    if parts and country and parts[-1].lower() == country.lower():
        parts = parts[:-1]
    if not parts:
        return "", "", country
    if len(parts) == 1:
        if _US_STATE_RE.match(parts[0]):
            return "", parts[0], country
        return parts[0], "", country
    if _US_STATE_RE.match(parts[-1]):
        return ", ".join(parts[:-1]), parts[-1], country
    return ", ".join(parts), "", country


def build_description(detail):
    """The posting text, concatenated from Oracle's body fields.

    CX_1001 rows carry ExternalDescriptionStr and often
    ExternalQualificationsStr; CX_2001 rows are usually empty across all
    four fields — the flex-field signal (build_skills) carries those.
    """
    chunks = [strip_html(detail.get(key) or "")
              for key in ("ExternalDescriptionStr",
                          "ExternalResponsibilitiesStr",
                          "ExternalQualificationsStr")]
    text = " ".join(c for c in chunks if c).strip()
    if not text:
        text = strip_html(detail.get("ShortDescriptionStr") or "")
    return text[:DESCRIPTION_MAX_CHARS]


# The flex prompts whose values describe WHAT the role works on. "Default"
# is this tenant's placeholder for "not set" and is dropped; the other
# operational prompts (RFH For, Resource Type, Required By Date, Region,
# pay and schedule fields) say nothing about the work.
SKILL_FLEX_PROMPTS = ("Speciality", "Service Line", "Sub Job Categorization")


def build_skills(detail, flex):
    """The classifier's `skills` signal: Oracle skills + Category + flex.

    Joined with "; " — the detail's curated skill names ("CPT Coding",
    "ICD10 Diagnostic Coding"), the requisition Category ("Coding"), and
    the Speciality / Service Line / Sub Job Categorization flex values
    that are this board's only structured role signal on CX_2001.
    """
    seen, out = set(), []

    def add(value):
        value = clean_text(value)
        if value and value.lower() != "default" and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)

    for skill in (detail.get("skills") or []):
        if isinstance(skill, dict):
            add(skill.get("Skill"))
    add(detail.get("Category"))
    for prompt in SKILL_FLEX_PROMPTS:
        add(flex.get(prompt))
    return "; ".join(out)


# ---- experience -------------------------------------------------------------

# Grounded prose fallback, same patterns as the rest of the fleet: never
# inferred, only lifted when the posting states it.
_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?",
               re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
               r"(?:relevant\s+|recent\s+|work(?:ing)?\s+|professional\s+|"
               r"coding\s+|billing\s+)?experience",
               re.IGNORECASE),
]


def parse_experience_years(flex, description):
    """Stated minimum years of experience, or "" when none is stated.

    The structured flex field "Required Years of Experience" (CX_1001)
    wins over the prose; nothing is ever inferred.
    """
    stated = clean_text(flex.get("Required Years of Experience"))
    match = re.match(r"(\d{1,2})", stated)
    if match and 0 < int(match.group(1)) <= 30:
        return str(int(match.group(1)))

    for regex in _EXPERIENCE_RES:
        match = regex.search(description or "")
        if match:
            years = int(match.group(1))
            if 0 < years <= 30:
                return str(years)
    return ""


# ---- pay --------------------------------------------------------------------

_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_pay(flex):
    """(pay_min_raw, pay_max_raw): the flex pay numbers VERBATIM, or "".

    CX_1001 states "Minimum Pay" / "Maximum Pay" as bare numbers (21 /
    28.25 for an hourly US coder) with neither period nor currency, so the
    values are captured raw in the rich CSV and the club salary columns
    stay blank — nothing is normalized on a guess.
    """
    lo = clean_text(flex.get("Minimum Pay"))
    hi = clean_text(flex.get("Maximum Pay"))
    return (lo if _NUMBER_RE.match(lo) else "",
            hi if _NUMBER_RE.match(hi) else "")


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    The scraper never assigns a category itself.
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("sectors", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["vetoed_by"] = verdict["vetoed_by"]
    row["needs_review"] = verdict["needs_review"]
    return verdict["in_scope"]


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, since=None):
    """First run: last INITIAL_WINDOW_DAYS. Later: watermark minus grace."""
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer (master spec §7)
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/json"})
    return session


def check_robots(session):
    """Verify the Oracle CE host allows the two REST paths.

    The host served no robots.txt when probed (HTTP 404), which means no
    restrictions — the precedent the undp readme documents; the check
    still runs so a later-added robots.txt stops the scraper instead of
    being ignored.
    """
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 404:
            log.info("No robots.txt on %s (HTTP 404) — crawling allowed", CE_HOST)
            rp.parse([])
        elif resp.status_code < 400:
            rp.parse(resp.text.splitlines())
        else:
            rp.parse([])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LIST_URL.format(site=SITE_NUMBERS[0], limit=LIST_PAGE_LIMIT,
                                offset=0),
                DETAIL_URL.format(site=SITE_NUMBERS[0], job_id="0")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch_json(session, url):
    """GET one JSON document with retries/backoff. Returns dict or None."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except ValueError as exc:            # not JSON — a block page etc.
            log.warning("Non-JSON response from %s (%s)", url, exc)
            return None
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def iter_site_requisitions(session, site_number, cutoff, counters):
    """Yield listing requisitions for one site, newest first.

    Offset-paginates and stops as soon as a whole page predates the cutoff
    (POSTING_DATES_DESC ordering makes every later page older still) — the
    master-spec early stop that keeps CX_2001's 834-row board to one or two
    listing requests per steady-state run.
    """
    offset = 0
    for page in range(MAX_LIST_PAGES):
        payload = fetch_json(session, LIST_URL.format(
            site=site_number, limit=LIST_PAGE_LIMIT, offset=offset))
        if payload is None:
            log.error("Could not fetch %s listing page at offset %d",
                      site_number, offset)
            counters["list_failed"] += 1
            return
        requisitions, total = parse_requisition_list(payload)
        if offset == 0:
            log.info("Oracle CE site %s lists %s open requisitions "
                     "(%d returned on page 1)",
                     site_number, total, len(requisitions))
        if not requisitions:
            return
        for req in requisitions:
            yield req
        last_date = parse_iso_date(requisitions[-1].get("PostedDate"))
        if last_date and last_date < cutoff:
            log.info("%s page at offset %d ends %s (< cutoff %s) — "
                     "stopping pagination", site_number, offset, last_date,
                     cutoff)
            return
        offset += len(requisitions)
        if total is not None and offset >= total:
            return
    log.warning("%s pagination stopped at MAX_LIST_PAGES=%d — raise it if "
                "the board has genuinely outgrown %d rows",
                site_number, MAX_LIST_PAGES, MAX_LIST_PAGES * LIST_PAGE_LIMIT)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(job_id, site_number, detail, posted_date, listing_end_date=""):
    """Rich row from one requisition detail + the listing's dates.

    posted_date MUST come from the listing: on this tenant the detail's
    ExternalPostedStartDate/EndDate are null (verified), so the listing's
    PostedDate is the only posted date that exists.
    """
    flex = parse_flex_fields(detail)
    title = clean_text(detail.get("Title"))
    country_code = clean_text(detail.get("PrimaryLocationCountry")).upper()
    city, state, country = parse_location(detail.get("PrimaryLocation"),
                                          country_code)
    description = build_description(detail)
    pay_min, pay_max = parse_pay(flex)

    return {
        "source": SITE,
        "job_id": str(job_id),
        "site_number": site_number,
        "title": title,
        "company": COMPANY_NAME,
        "city": city,
        "state": state,
        "country": country,
        # The classifier's `skills` signal: Oracle skills + Category +
        # Speciality / Service Line / Sub Job Categorization flex values.
        "sectors": build_skills(detail, flex),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "vetoed_by": "",
        "needs_review": False,
        "experience_min_years": parse_experience_years(flex, description),
        "work_schedule": clean_text(flex.get("Full-Time/Part-Time")),
        "pay_min_raw": pay_min,
        "pay_max_raw": pay_max,
        "posted_date": (parse_iso_date(posted_date) or
                        parse_iso_date(detail.get("ExternalPostedStartDate"))),
        "valid_through": (parse_iso_date(listing_end_date) or
                          parse_iso_date(detail.get("ExternalPostedEndDate"))),
        "description": description,
        "job_url": JOB_URL_TEMPLATE.format(site=site_number, job_id=job_id),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the CLUB_COLUMNS contract.

    Everything is derived from stored rich columns so a full-store
    re-export reproduces the same rows. Omega Healthcare is an RCM/BPO
    services company, neither hospital nor pharma; it takes the club
    enum's default "hospital" (the fleet-wide convention for non-pharma
    employers). Salary columns stay blank: the API's bare pay numbers
    state neither period nor currency (rich CSV keeps them raw), and
    nothing is ever invented.
    """
    country = _blank(r.get("country"))
    code, dial = CODE_BY_COUNTRY.get(country, ("", ""))
    description = _blank(r.get("description"))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": _blank(r.get("city")),
        "company_name": _blank(r.get("company")) or COMPANY_NAME,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": description,
        "job_type": ("part_time"
                     if _blank(r.get("work_schedule")).lower() == "part-time"
                     else "full_time"),
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _blank(r.get("experience_min_years")),
        "max_experience": "",
        # no structured qualification field on the board — grounded
        # extraction from the description only, never inferred
        "qualification": extract_qualification(description),
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
    }


# ----------------------------------------------------------------------------
# Stores
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def load_id_column(path):
    try:
        return set(pd.read_csv(path, dtype=str)["job_id"].dropna())
    except (FileNotFoundError, KeyError):
        return set()


def append_out_of_scope(rows, path=OUT_OF_SCOPE_CSV):
    """Append dropped rich rows, deduped on job_id. Moved, never discarded."""
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    try:
        df = pd.concat([pd.read_csv(path, dtype=str), df], ignore_index=True)
    except FileNotFoundError:
        pass
    df = df.fillna("").drop_duplicates(subset="job_id", keep="last")
    for col in RICH_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df[RICH_COLUMNS].to_csv(path, index=False)
    return len(df)


def write_club_csv(rich_df, run_date):
    rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape Omega Healthcare vacancies from the Oracle HCM "
                    "Candidate Experience API (sites CX_1001 and CX_2001).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail records "
                             "(test runs; overrides the first-run cap)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job "
                             "posted on/after this date")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    seen_old = load_id_column(SEEN_OLD_CSV)
    out_of_scope_ids = load_id_column(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df, since=args.since)

    # First-run safety cap: without a store yet, an in-window backlog on
    # CX_2001 could mean hundreds of detail fetches. --limit overrides.
    detail_cap = args.limit
    if detail_cap is None and existing_df is None:
        detail_cap = FIRST_RUN_DETAIL_CAP
        log.info("First run: detail fetches capped at %d (newest first)",
                 detail_cap)

    log.info("Existing CSV has %d known jobs (%d known-old, %d known "
             "out-of-scope skipped); keeping jobs posted on/after %s%s",
             len(known_ids), len(seen_old), len(out_of_scope_ids), cutoff,
             " (--since override)" if args.since else "")

    counters = Counter()
    veto_counts = Counter()
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0

    for site_number in SITE_NUMBERS:
        for req in iter_site_requisitions(session, site_number, cutoff,
                                          counters):
            counters["scanned"] += 1
            job_id = clean_text(req.get("Id"))
            if not job_id:
                continue
            if job_id in known_ids:
                counters["duplicates"] += 1
                continue
            if job_id in out_of_scope_ids:
                counters["skipped_out_of_scope"] += 1
                continue

            # The listing's PostedDate is the ONLY posted date on this
            # tenant (details carry none), so the window is decided before
            # spending a detail request.
            posted_date = parse_iso_date(req.get("PostedDate"))
            if posted_date and posted_date < cutoff:
                if job_id in seen_old:
                    counters["skipped_old"] += 1
                else:
                    counters["excluded_old"] += 1
                    seen_old.add(job_id)
                    new_seen_old.append({"job_id": job_id,
                                         "posted_date": posted_date})
                continue

            if detail_cap is not None and fetched >= detail_cap:
                counters["deferred_by_cap"] += 1
                continue

            payload = fetch_json(session, DETAIL_URL.format(site=site_number,
                                                            job_id=job_id))
            fetched += 1
            detail = parse_requisition_detail(payload)
            if not detail:
                counters["detail_failed"] += 1
                log.warning("No requisition detail for job_id=%s", job_id)
                continue
            try:
                row = build_row(job_id, site_number, detail, posted_date,
                                req.get("PostingEndDate"))
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job %s: %s", job_id, exc)
                continue

            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                if row.get("vetoed_by"):
                    veto_counts[row["vetoed_by"]] += 1
                out_of_scope_ids.add(job_id)
                dropped_rows.append(row)
                continue

            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["title"],
                                   "site_number": row["site_number"],
                                   "country": row["country"],
                                   "sectors": row["sectors"],
                                   "sub_category": row["sub_category"]})
            known_ids.add(job_id)
            new_rows.append(row)
            counters["new"] += 1

    if counters["deferred_by_cap"]:
        log.warning("Detail cap left %d in-window requisitions unfetched — "
                    "they are NOT recorded anywhere; re-run (the watermark "
                    "grace re-offers the newest) or use --since to backfill",
                    counters["deferred_by_cap"])

    # ---- rich cumulative CSV (source of truth) ----
    if new_rows:
        new_df = pd.DataFrame(new_rows, dtype=str)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[RICH_COLUMNS]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        combined = existing_df if existing_df is not None else \
            pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV (full-store dump, regenerated every run) ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    # ---- sidecars ----
    if new_seen_old:
        old_df = pd.DataFrame(new_seen_old)
        try:
            old_df = pd.concat([pd.read_csv(SEEN_OLD_CSV, dtype=str), old_df],
                               ignore_index=True)
        except FileNotFoundError:
            pass
        old_df.drop_duplicates(subset="job_id").to_csv(SEEN_OLD_CSV, index=False)
    if dropped_rows:
        total_dropped = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total_dropped)
    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    print("\n===== Run summary =====")
    print("Requisitions scanned:         {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Skipped (known old):          {:>6,}".format(counters["skipped_old"]))
    print("Deferred by detail cap:       {:>6,}".format(counters["deferred_by_cap"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail parse failures:        {:>6,}".format(counters["detail_failed"]))
    if veto_counts:
        print("Vetoes among dropped rows:")
        for veto, n in veto_counts.most_common():
            print("    {:<28} {:>4,}".format(veto, n))


if __name__ == "__main__":
    main()
