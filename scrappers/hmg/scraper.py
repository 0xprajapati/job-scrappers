#!/usr/bin/env python3
"""Scrape job openings from hmg.com (Dr. Sulaiman Al Habib Medical Group, KSA).

Data source
-----------
`hmg.com/en/careers` is a dead SharePoint URL (404); the live "Careers" link on
hmg.com points at the group's careers hub **https://hmg.elevatus.io/**, which is
only a landing page linking to one **Elevatus (EVA-REC) career portal per group
company** — the hospitals themselves live on https://talents.hmg.com/.

Every one of those portals is the same Next.js app fed by Elevatus' public,
token-free JSON API (master spec §1.1 — an underlying JSON API is the best
source):

    GET https://dammam-core-api.elevatus.io/api/v1/jobs
        ?language_profile_uuid=<English language uuid>&limit=…&page=…
        headers: Accept-Company: <portal company uuid>
    -> results.{jobs[], total, page, pages}

The listing already carries the FULL `description` + `requirements` HTML, the
category, degree, major, career level, structured `years_of_experience` and the
public apply URL, so there is no detail call to make (`--enrich` from the master
spec does not apply here — there is no per-job endpoint; `/api/v1/jobs/<uri>`
404s).

`Accept-Company` is discovered at runtime from each portal's `__NEXT_DATA__`
(`props.portalLayout.company_uuid.id`) and falls back to the uuid pinned in
`PORTALS` if the portal page ever changes shape.

Quirks
------
* The corporate portal (talents.hmg.com) already aggregates the hospital
  companies (63 jobs across 10 internal company uuids); the other portals hold
  their own small sets, so all of them are scraped and `company` is the portal's
  brand, not the internal uuid (the API exposes no company-name lookup).
* `category` is the ATS' own taxonomy (Physicians / Doctor / Nursing / Pharmacy
  / Paramedical / Administration / **Default**) and "Default" is used for real
  clinical roles, so it may NOT decide anything. It is stored verbatim as
  `category_original` (a raw source column) and, together with major/industry/
  career level/skills, is passed to the shared classifier as its curated
  `skills` signal.
* Classification is the shared two-level taxonomy (scrappers/_shared/
  classification.py): category "Non Clinical" | "Public Health" plus a
  sub_category. Being a hospital group's ATS, most postings (physicians,
  nursing, pharmacy dispensing, paramedical) are out of scope and dropped
  (counted as excluded_out_of_scope).
* `salary` is `{"min": 0, "max": 0}` on every posting -> `salary_raw =
  "Not Disclosed"`, numeric fields empty (master spec §3). The parser still
  handles a real range if HMG ever publishes one.
* `location` is polymorphic: a facility label (`name.en` = "Jeddah - Al
  Mohammdiya", "Sewedi - Riyadh"), or `{city, country}`, or nothing at all —
  hence `normalize_city()` and `DEFAULT_CITY` (Riyadh, HMG's headquarters).
* A handful of postings have `posted_at: null`. They are kept (never dropped
  for a missing date) and, being deduped on uuid, are added exactly once.
* "Tamheer Program - …" postings are Saudi government-funded graduate training
  placements; the club schema has no internship enum, so they map to
  `full_time` and keep the program name in the title.

Time window: an ATS lists only currently OPEN postings (the oldest here has
been open since 2025-09), so the first run keeps ALL of them
(`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
(newest stored posted_date minus `WATERMARK_GRACE_DAYS`).

robots.txt: neither the portals nor the API host serve one (404 => allowed);
checked at startup for the API host and every portal.

Outputs
-------
* hmg_jobs.csv                        — rich cumulative store.
* ../../jobs_csv/<DD-MM-YYYY>/hmg.csv — HealthCareers.club 22-column schema.

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

SITE = "hmg"
CAREERS_HUB = "https://hmg.elevatus.io/"          # what hmg.com/en/careers means

API_HOST = "https://dammam-core-api.elevatus.io"
JOBS_URL = API_HOST + "/api/v1/jobs"
# portalLayout.languages -> the English language profile shared by all portals
LANGUAGE_PROFILE_UUID = "14613182-02fa-449b-b78f-296f9d6be59a"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

GROUP_ABOUT = (
    "Dr. Sulaiman Al Habib Medical Group (HMG) is one of the largest private "
    "healthcare providers in the Middle East, operating hospitals, medical "
    "centres, pharmacies and diagnostic laboratories across Saudi Arabia and "
    "the UAE."
)

# One Elevatus career portal per group company, all linked from the hmg.com
# careers hub. `company_uuid` is only the fallback for runtime discovery.
PORTALS = [
    {
        "host": "talents.hmg.com",
        "company": "Dr. Sulaiman Al Habib Medical Group",
        "company_uuid": "ff0a5014-23dc-439f-befe-796d15cdcb56",
        "company_type": "hospital",
        "about": GROUP_ABOUT,
    },
    {
        "host": "hmccareers.elevatus.io",
        "company": "Habib Medical Centers",
        "company_uuid": "02ca56e2-8e10-4182-a573-5187e95c12b7",
        "company_type": "hospital",
        "about": "Habib Medical Centers hires through the Dr. Sulaiman Al "
                 "Habib Medical Group (HMG) careers hub. " + GROUP_ABOUT,
    },
    {
        "host": "ajajitalents.elevatus.io",
        "company": "Dr. Abdulaziz Al Ajaji Dental Clinics",
        "company_uuid": "16691839-4531-4f1e-8245-51122a902487",
        "company_type": "hospital",
        "about": "Dr. Abdulaziz Al Ajaji Dental Clinics is a dental clinic "
                 "network in Saudi Arabia that recruits through the Dr. "
                 "Sulaiman Al Habib Medical Group (HMG) careers hub. "
                 + GROUP_ABOUT,
    },
    {
        "host": "mepcareer.elevatus.io",
        "company": "Middle East Pharmacy",
        "company_uuid": "b5841d45-f6e9-4f28-a889-cd2f40e8c917",
        "company_type": "pharma",
        "about": "Middle East Pharmacy is the retail pharmacy arm recruiting "
                 "through the Dr. Sulaiman Al Habib Medical Group (HMG) "
                 "careers hub. " + GROUP_ABOUT,
    },
    {
        "host": "mdlabcareers.elevatus.io",
        "company": "Mokhtabarat Diagnostic Medical Laboratories (MDLAB)",
        "company_uuid": "a3c9c0ac-bb2b-44cc-8c80-4c9a6c1f6f18",
        "company_type": "hospital",
        "about": "MDLAB (Mokhtabarat Diagnostic Medical Laboratories) recruits "
                 "through the Dr. Sulaiman Al Habib Medical Group (HMG) "
                 "careers hub. " + GROUP_ABOUT,
    },
    {
        "host": "taswyatcareers.elevatus.io",
        "company": "Taswayat Company",
        "company_uuid": "547780dd-d016-4674-be6f-611497966972",
        "company_type": "hospital",
        "about": "Taswayat recruits through the Dr. Sulaiman Al Habib Medical "
                 "Group (HMG) careers hub. " + GROUP_ABOUT,
    },
    {
        "host": "wrass.elevatus.io",
        "company": "WRASS",
        "company_uuid": "1f12723e-5b2f-436b-8e94-a7733a8aa6b0",
        "company_type": "hospital",
        "about": "WRASS is an HMG group support-services company recruiting "
                 "through the Dr. Sulaiman Al Habib Medical Group (HMG) "
                 "careers hub. " + GROUP_ABOUT,
    },
    {
        "host": "talents.cloudsolutions.com.sa",
        "company": "Cloud Solutions",
        "company_uuid": "629064c3-fca0-4f32-b33e-88eafded5b22",
        "company_type": "hospital",
        "about": "Cloud Solutions is HMG's health-technology company, "
                 "recruiting through the Dr. Sulaiman Al Habib Medical Group "
                 "(HMG) careers hub. " + GROUP_ABOUT,
    },
]

# The ATS lists only OPEN postings, so the first run keeps everything
# (None = no initial window); later runs use the watermark (master spec §4).
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 50
MAX_EMPTY_PAGES = 3
MAX_PAGES_HARD_STOP = 100
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 5_000

RICH_CSV = "hmg_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

DEFAULT_COUNTRY = "Saudi Arabia"
DEFAULT_CITY = "Riyadh"                       # HMG headquarters
COUNTRY_META = {
    "saudi arabia": ("Saudi Arabia", "SA", "+966"),
    "united arab emirates": ("United Arab Emirates", "AE", "+971"),
    "bahrain": ("Bahrain", "BH", "+973"),
    "egypt": ("Egypt", "EG", "+20"),
}

# Facility labels are "<district> - <city>" or "<city> - <district>"; the token
# that matches a known city wins ("Jeddah - Al Mohammdiya" -> Jeddah).
KNOWN_CITIES = [
    "Riyadh", "Jeddah", "Khobar", "Dammam", "Qassim", "Buraidah", "Kharj",
    "Makkah", "Madinah", "Taif", "Hail", "Jubail", "Tabuk", "Abha", "Khamis",
    "Dubai", "Abu Dhabi", "Sharjah", "Manama", "Cairo",
]

RICH_COLUMNS = [
    "source", "portal", "job_id", "title", "company", "company_type",
    "location_label", "city", "country", "salary_raw", "salary_min_monthly",
    "salary_max_monthly", "salary_period_original", "salary_currency",
    "job_type", "career_level", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "category_original", "industry",
    "major", "education", "skills", "experience_min_years",
    "experience_max_years", "needs_review", "posted_date", "expires_at",
    "description", "job_url", "scraped_at",
]

log = logging.getLogger("hmg_scraper")

# ----------------------------------------------------------------------------
# Text cleaning
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text):
    """Collapse whitespace (incl. &nbsp;) and unescape HTML entities."""
    return _WS_RE.sub(
        " ", html_lib.unescape(str(text or "")).replace("\xa0", " ")).strip()


def strip_html(text):
    """Flatten the ATS' rich-text HTML into one plain-text paragraph.

    Tags become spaces so list items never run together
    ("…rota.<li>Admit the…" -> "…rota. Admit the…").
    """
    return clean_text(_TAG_RE.sub(" ", str(text or "")))


def build_description(description_html, requirements_html):
    """Job body + the separately stored requirements, in one club-CSV field."""
    body = strip_html(description_html)
    requirements = strip_html(requirements_html)
    if requirements:
        requirements = "Requirements: " + requirements
    return clean_text(" ".join(part for part in (body, requirements) if part))


# ----------------------------------------------------------------------------
# Location
# ----------------------------------------------------------------------------

def location_label(location):
    """The portal's own location text: facility label, else city, else ''."""
    if not isinstance(location, dict):
        return ""
    name = location.get("name")
    if isinstance(name, dict):
        label = clean_text(name.get("en"))
        if label:
            return label
    elif name:
        return clean_text(name)
    return clean_text(location.get("city"))


def normalize_city(label, fallback_city=""):
    """Best city for a facility label ("Sewedi - Riyadh" -> "Riyadh").

    Falls back to the API's own `city`, then to DEFAULT_CITY, because the club
    schema always wants a city and HMG's head office is in Riyadh.
    """
    label = clean_text(label)
    for chunk in [label] + [c.strip() for c in re.split(r"[-/,]", label)]:
        for city in KNOWN_CITIES:
            if re.search(r"\b{}\b".format(re.escape(city)), chunk, re.IGNORECASE):
                return city
    fallback_city = clean_text(fallback_city)
    if fallback_city:
        return fallback_city
    # a label such as "Saudi Arabia" is a country, not a city
    if label and label.lower() not in COUNTRY_META:
        return label
    return DEFAULT_CITY


def country_meta(location):
    """(country_name, code, dial) from the posting, defaulting to Saudi Arabia."""
    country = ""
    if isinstance(location, dict):
        country = clean_text(location.get("country"))
    meta = COUNTRY_META.get(country.lower())
    if meta:
        return meta
    if country:                       # unknown country: keep the name, no codes
        return (country, "", "")
    return COUNTRY_META[DEFAULT_COUNTRY.lower()]


# ----------------------------------------------------------------------------
# Salary (capture, never filter — master spec §3)
# ----------------------------------------------------------------------------

def parse_salary(salary, currency="SAR"):
    """Return (raw, min_monthly, max_monthly, period, currency).

    Elevatus stores a plain monthly {min, max} pair and HMG leaves it at 0/0 on
    every posting, which is "Not Disclosed" — never an invented number.
    """
    def as_int(value):
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    low = as_int((salary or {}).get("min"))
    high = as_int((salary or {}).get("max"))
    if not low and not high:
        return ("Not Disclosed", "", "", "", "")
    if low and high and high < low:
        low, high = high, low
    if low and high:
        raw = "{} {:,} - {:,} per month".format(currency, low, high)
    else:
        raw = "{} {:,} per month".format(currency, low or high)
    return (raw, str(low or ""), str(high or ""), "per_month", currency)


# ----------------------------------------------------------------------------
# Experience
# ----------------------------------------------------------------------------

def parse_experience(years_of_experience):
    """Structured `years_of_experience` list -> (min, max) as strings.

    One value means a minimum ("At least 3 years"); two or more become a range.
    """
    values = []
    for value in years_of_experience or []:
        try:
            values.append(int(float(value)))
        except (TypeError, ValueError):
            continue
    values = [v for v in values if v >= 0]
    if not values:
        return ("", "")
    low, high = min(values), max(values)
    return (str(low), str(high) if high > low else "")


# ----------------------------------------------------------------------------
# Classification — delegated to _shared/classification.py
# ----------------------------------------------------------------------------

def classification_skills(row):
    """The curated `skills` signal: the Elevatus category, career level,
    industry and major joined together.

    The ATS category ("Physicians", "Nursing", ... and the literal
    "Default" it also files real clinical roles under) is a raw source
    column — it feeds the classifier but never decides anything.

    The posting's `skills` array is deliberately NOT part of this signal:
    HMG fills it from a generic corporate competency framework ("data
    management & record keeping", "process management") that describes no
    role, and feeding it in admits radiologists and secretaries as Clinical
    Data Management. It is still stored verbatim in the rich CSV.
    """
    return " ".join(filter(None, [row.get("category_original", ""),
                                  row.get("career_level", ""),
                                  row.get("industry", ""),
                                  row.get("major", "")])).strip()


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           classification_skills(row),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = verdict["needs_review"]
    return verdict["in_scope"]


def map_job_type(job_types):
    """Elevatus job_type list -> club enum (full_time|part_time|remote|hybrid).

    Blank means the recruiter left the field empty; hospital postings default
    to full_time. Tamheer training placements have no club enum of their own.
    """
    text = " ".join(clean_text(t) for t in (job_types or [])).lower()
    if "remote" in text:
        return "remote"
    if "hybrid" in text:
        return "hybrid"
    if "part" in text:
        return "part_time"
    return "full_time"


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def robots_allows(session, base_url, target_url):
    """True when base_url's robots.txt permits target_url.

    Every Elevatus host answers /robots.txt with the SPA's 404 HTML page, which
    means "no robots.txt" => everything allowed; a real file is honoured.
    """
    robots_url = base_url.rstrip("/") + "/robots.txt"
    rp = urllib.robotparser.RobotFileParser(robots_url)
    try:
        resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
        return True
    looks_like_robots = (resp.status_code < 400
                         and "<html" not in resp.text[:500].lower())
    rp.parse(resp.text.splitlines() if looks_like_robots else [])
    return rp.can_fetch(USER_AGENT, target_url)


def check_robots(session, portals):
    """Abort on the API host; skip (don't kill the run) a disallowed portal."""
    if not robots_allows(session, API_HOST, JOBS_URL):
        sys.exit("robots.txt disallows {} — aborting.".format(JOBS_URL))
    allowed = []
    for portal in portals:
        base = "https://" + portal["host"]
        if robots_allows(session, base, base + "/jobs"):
            allowed.append(portal)
        else:
            log.warning("robots.txt disallows %s — skipping that portal", base)
    log.info("robots.txt check passed (%d/%d portals allowed)",
             len(allowed), len(portals))
    return allowed


def get(session, url, params=None, headers=None):
    """GET with retries/backoff on 429/5xx/network errors. None = gave up."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d in %.0fs (%s)",
                        attempt, MAX_RETRIES, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, params=params, headers=headers,
                               timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES,
              last_error)
    return None


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def discover_company_uuid(session, portal):
    """Read `Accept-Company` off the portal page, else the pinned fallback."""
    url = "https://{}/".format(portal["host"])
    resp = get(session, url, headers={"Accept": "text/html"})
    if resp is not None:
        match = _NEXT_DATA_RE.search(resp.text)
        if match:
            try:
                data = json.loads(match.group(1))
                uuid = (data["props"]["portalLayout"]["company_uuid"]["id"])
                if uuid:
                    if uuid != portal["company_uuid"]:
                        log.info("%s: company uuid changed to %s",
                                 portal["host"], uuid)
                    return uuid
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("%s: could not read __NEXT_DATA__ (%s)",
                            portal["host"], exc)
    log.info("%s: using pinned company uuid", portal["host"])
    return portal["company_uuid"]


def iter_jobs(session, company_uuid, max_pages=None):
    """Yield postings newest-first, paging with the API's 0-based `page`."""
    page, empty_streak, total = 0, 0, None
    headers = {"Accept-Company": company_uuid}
    while page < MAX_PAGES_HARD_STOP:
        if max_pages is not None and page >= max_pages:
            log.info("Reached --max-pages=%d", max_pages)
            return
        params = {
            "language_profile_uuid": LANGUAGE_PROFILE_UUID,
            "limit": PAGE_SIZE,
            "page": page,
        }
        resp = get(session, JOBS_URL, params=params, headers=headers)
        if resp is None:
            log.error("Listing page %d failed; stopping pagination", page)
            return
        try:
            results = (resp.json() or {}).get("results") or {}
        except ValueError as exc:
            log.error("Listing page %d was not JSON (%s); stopping", page, exc)
            return
        batch = results.get("jobs") or []
        if total is None:
            total = results.get("total")
            log.info("Portal lists %s posting(s)",
                     total if total is not None else "?")
        if not batch:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES:
                return
        else:
            empty_streak = 0
            for job in batch:
                yield job
        page += 1
        pages = results.get("pages")
        if pages is not None and page >= pages:
            return


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def _joined(values):
    return "; ".join(clean_text(v) for v in (values or []) if clean_text(v))


def build_rich_row(job, portal):
    location = job.get("location") or {}
    label = location_label(location)
    country, _code, _dial = country_meta(location)
    ats_category = _joined(job.get("category"))
    major = _joined(job.get("major"))
    industry = _joined(job.get("industry"))
    raw, salary_min, salary_max, period, currency = parse_salary(
        job.get("salary"))
    exp_min, exp_max = parse_experience(job.get("years_of_experience"))

    return {
        "source": SITE,
        "portal": portal["host"],
        "job_id": clean_text(job.get("uuid")),
        "title": clean_text(job.get("title")),
        "company": portal["company"],
        "company_type": portal["company_type"],
        "location_label": label,
        "city": normalize_city(label, location.get("city")),
        "country": country,
        "salary_raw": raw,
        "salary_min_monthly": salary_min,
        "salary_max_monthly": salary_max,
        "salary_period_original": period,
        "salary_currency": currency,
        "job_type": map_job_type(job.get("job_type")),
        "career_level": _joined(job.get("career_level")),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "category_original": ats_category,
        "industry": industry,
        "major": major,
        "education": _joined(job.get("degree")),
        "skills": _joined(job.get("skills")),
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "needs_review": False,
        "posted_date": clean_text(job.get("posted_at"))[:10],
        "expires_at": clean_text(job.get("end_schedule_date"))[:10],
        "description": build_description(
            job.get("description"), job.get("requirements")
        )[:DESCRIPTION_MAX_CHARS],
        "job_url": clean_text(job.get("url")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


_ABOUT_BY_COMPANY = {p["company"]: p["about"] for p in PORTALS}


def rich_row_to_club_row(r):
    def val(key):
        v = r.get(key, "")
        return "" if pd.isna(v) else str(v)

    country = val("country") or DEFAULT_COUNTRY
    name, code, dial = COUNTRY_META.get(country.lower(), (country, "", ""))
    company = val("company") or PORTALS[0]["company"]
    return {
        "country_name": name,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": val("city") or DEFAULT_CITY,
        "company_name": company,
        "company_type": val("company_type") or "hospital",
        "company_logo": "",
        "company_about": _ABOUT_BY_COMPANY.get(company, GROUP_ABOUT),
        "title": val("title"),
        "description": val("description"),
        "job_type": val("job_type") or "full_time",
        "category": val("category"),
        "sub_category": val("sub_category"),
        "application_url": val("job_url") or CAREERS_HUB,
        "posted_at": val("posted_date"),
        "min_experience": val("experience_min_years"),
        "max_experience": val("experience_max_years"),
        # structured source field (Elevatus `degree`) first, else grounded
        # extraction from the description — never inferred
        "qualification": val("education") or extract_qualification(
            val("description")),
        "min_salary": val("salary_min_monthly"),
        "max_salary": val("salary_max_monthly"),
        "salary_period": val("salary_period_original"),
        "salary_currency": val("salary_currency"),
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date) or None = keep everything (first run: the
    ATS only lists open postings, so all of them are current)."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max()
                    - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    if INITIAL_WINDOW_DAYS is not None:
        return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    return None


def within_window(posted_date, cutoff):
    """Postings without a posted_at are kept (dedup adds them exactly once)."""
    if cutoff is None or not posted_date:
        return True
    return posted_date >= cutoff


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
        description="Scrape Dr. Sulaiman Al Habib Medical Group (hmg.com) job "
                    "openings from the Elevatus career-portal API.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages of {} PER PORTAL "
                             "(test runs)".format(PAGE_SIZE))
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N postings in total (test runs)")
    parser.add_argument("--portal", action="append", metavar="HOST",
                        help="scrape only this portal host (repeatable); "
                             "default: all {} portals".format(len(PORTALS)))
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    portals = PORTALS
    if args.portal:
        wanted = {h.lower() for h in args.portal}
        portals = [p for p in PORTALS if p["host"].lower() in wanted]
        unknown = wanted - {p["host"].lower() for p in PORTALS}
        if unknown:
            sys.exit("Unknown portal(s): {}".format(", ".join(sorted(unknown))))

    session = make_session()
    portals = check_robots(session, portals)

    existing_df = load_existing(args.output)
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; cutoff: %s", len(known_ids),
             cutoff or "none (keeping all open postings)")

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "duplicates": 0, "errors": 0}
    new_rows, review_log = [], []

    for portal in portals:
        if args.limit is not None and counters["scanned"] >= args.limit:
            break
        log.info("--- %s (%s) ---", portal["host"], portal["company"])
        try:
            company_uuid = discover_company_uuid(session, portal)
        except Exception as exc:                        # never kill the run
            log.warning("%s: uuid discovery failed (%s); using pinned uuid",
                        portal["host"], exc)
            company_uuid = portal["company_uuid"]

        for job in iter_jobs(session, company_uuid, max_pages=args.max_pages):
            if args.limit is not None and counters["scanned"] >= args.limit:
                log.info("Reached --limit=%d", args.limit)
                break
            counters["scanned"] += 1
            job_id = clean_text(job.get("uuid"))
            if not job_id:
                log.warning("Posting without uuid skipped: %s", job.get("title"))
                counters["errors"] += 1
                continue
            if not within_window(clean_text(job.get("posted_at"))[:10], cutoff):
                counters["excluded_old"] += 1
                continue
            if job_id in known_ids:
                counters["duplicates"] += 1
                continue
            try:
                row = build_rich_row(job, portal)
            except Exception as exc:
                log.warning("Skipping malformed posting %s: %s", job_id, exc)
                counters["errors"] += 1
                continue
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["title"],
                                   "portal": row["portal"],
                                   "category_original": row["category_original"]})
            known_ids.add(job_id)
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
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Portals scraped:       {:>5,}".format(len(portals)))
    print("Postings scanned:      {:>5,}".format(counters["scanned"]))
    print("Excluded (out of scope): {:>3,}".format(
        counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff or "no cutoff",
                                                    counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    print("Errors (skipped rows): {:>5,}".format(counters["errors"]))


if __name__ == "__main__":
    main()
