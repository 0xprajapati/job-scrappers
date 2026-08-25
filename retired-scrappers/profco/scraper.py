#!/usr/bin/env python3
"""Scrape healthcare job listings from profco.com (Professional Connections).

Data source
-----------
www.profco.com is an international healthcare recruitment agency (nursing,
midwifery and medical-doctor posts in Saudi Arabia, Australia and the UK).
It is a hand-rolled PHP site with no JSON API and no sitemap, but its own
front-end drives two form-encoded AJAX endpoints that return small HTML
fragments (master spec §1, source preference #1/#2):

    POST /ajax.php?task=getJobSearchResult
         freetext, country_id, jobcat_id, jobspeciality_id
      -> <ul class="joblist"> of <a href="index.php?job_id=N" title="...">

    POST /ajax.php   task=getJobSpecialitySelect, jobcat_id=N
      -> <select> of the specialities that exist for a category

    POST /ajax.php   task=getContent, job_id=N
      -> the job detail fragment: <h1>#N Title</h1> plus a <th>/<td> table
         (Category, Speciality, Location, Salary min, Salary max, Hospital,
         Description, Benefits, Requirements)

`index.php?job_id=N` renders the same fragment inside the full page; the
scraper uses the AJAX form (3 KB instead of 40 KB) and stores the index.php
URL as the public `job_url`.

robots.txt: profco.com serves no robots.txt (404 -> no restrictions). It is
still fetched at startup and honoured if the site ever adds one.

Quirks
------
* **The search caps every result set at 50 rows** (RESULT_CAP). An unfiltered
  search therefore returns 50 of ~200 live jobs. Enumeration is adaptive:
  query per category, and only when a category hits the cap drill down its
  speciality list (and, if a speciality also hits the cap, its countries).
  A category-only query is also always issued so that jobs with no speciality
  assigned (they exist, e.g. #15770) are not missed.
* **No posted date anywhere** on the site. As in the nhm scraper,
  `posted_date` is the date a job was FIRST SEEN by this scraper, so the
  watermark/time-window logic (master spec §4) is a no-op and dedup alone
  provides idempotency. Running twice in a row adds 0 rows.
* **No country field.** It is resolved from the salary currency
  (SAR -> Saudi Arabia, AUD -> Australia, GBP -> UK), then the location city,
  then a country prefix in the title. Saudi jobs are quoted in either SAR or
  USD, so USD is not a country signal and falls through to the city map.
* **Salaries are mostly "On Application SAR"** -> "Not Disclosed"
  (master spec §3: never invented). Australian posts carry real figures
  ("86434 AUD" - "103979 AUD"); the site never states a period, so it is
  inferred from magnitude and kept only in the rich CSV — the club schema's
  salary_currency enum allows INR/USD only, so AUD amounts are not exported.
* **The employer is confidential** (agency mandates: "our client hospital"),
  so `company` is Profco and the Hospital blurb becomes `company_about` —
  the same convention as the michaelpage scraper.
* Every listing is healthcare recruitment, and the site's own category
  (Nursing and Midwifery / Medical Doctor / Pharmacist / Allied Health ...)
  maps onto the club category enum; titles under the generic categories
  (Other / Administration / Engineering / Career with Profco) with no
  healthcare signal are kept and flagged `needs_review` (master spec §2).
* The listing is an OPEN-vacancy board: rows that disappear are closed
  postings. They stay in the rich CSV with their old `last_seen` and are
  exported with `is_active=false` (only when a run enumerated cleanly).

Outputs
-------
* profco_jobs.csv                          — rich cumulative store
                                             (dedup key: job_id)
* ../../jobs_csv/<DD-MM-YYYY>/profco.csv   — HealthCareers.club 22-col schema
* needs_review.csv                         — kept-but-unclassified titles

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

SITE = "profco"
SITE_BASE = "https://www.profco.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
HOME_URL = SITE_BASE + "/"
AJAX_URL = SITE_BASE + "/ajax.php"
JOB_URL_TEMPLATE = SITE_BASE + "/index.php?job_id={}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec §3): salaries are captured, never filtered on.
# The site publishes no dates, so posted_date = first-seen date and the
# window below never excludes anything; it is kept for spec conformance.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

RESULT_CAP = 50          # server-side cap on getJobSearchResult rows
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000
COMPANY_ABOUT_MAX_CHARS = 500

RICH_CSV = "profco_jobs.csv"
NEEDS_REVIEW_CSV = "needs_review.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COMPANY_NAME = "Profco (Professional Connections)"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_about",
    "city", "country", "country_code", "country_dial_code",
    "site_category", "site_speciality", "contract_type",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "salary_period_original", "salary_currency", "job_type", "category",
    "company_type", "min_experience_years", "needs_review",
    "posted_date", "last_seen", "description", "benefits", "requirements",
    "hospital", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

# Club salary columns accept these currencies only; everything else stays in
# the rich CSV (never converted — master spec §3).
CLUB_CURRENCIES = ("INR", "USD")

COUNTRIES = {
    "Saudi Arabia": ("SA", "+966"),
    "Australia": ("AU", "+61"),
    "United Kingdom": ("GB", "+44"),
    "Ireland": ("IE", "+353"),
}

CURRENCY_COUNTRY = {"SAR": "Saudi Arabia", "AUD": "Australia",
                    "GBP": "United Kingdom", "EUR": "Ireland"}

# Cities Profco actually recruits for (lowercased keys).
CITY_COUNTRY = {
    "riyadh": "Saudi Arabia", "jeddah": "Saudi Arabia",
    "dammam": "Saudi Arabia", "dhahran": "Saudi Arabia",
    "al ahsa": "Saudi Arabia", "al hasa": "Saudi Arabia",
    "qassim": "Saudi Arabia", "al madinah": "Saudi Arabia",
    "madinah": "Saudi Arabia", "medina": "Saudi Arabia",
    "mecca": "Saudi Arabia", "makkah": "Saudi Arabia",
    "taif": "Saudi Arabia", "khobar": "Saudi Arabia",
    "sydney": "Australia", "melbourne": "Australia", "brisbane": "Australia",
    "perth": "Australia", "adelaide": "Australia", "canberra": "Australia",
    "london": "United Kingdom", "manchester": "United Kingdom",
    "dublin": "Ireland",
}

# Location strings as typed by the site -> the city they mean. Only obvious
# typos / suffixes are corrected; anything unlisted is kept verbatim.
CITY_ALIASES = {
    "riaydh": "Riyadh",
    "al qassim": "Qassim",
    "sydney eastern surburbs": "Sydney",
    "al hasa": "Al Ahsa",
    "medina": "Al Madinah",
}

# Country words the site prefixes onto some titles.
TITLE_COUNTRY_RE = re.compile(
    r"^\s*(SAUDI ARABIA|SAUDI|AUSTRALIA|UNITED KINGDOM|UK|IRELAND)\b",
    re.IGNORECASE)
TITLE_COUNTRY_MAP = {"saudi arabia": "Saudi Arabia", "saudi": "Saudi Arabia",
                     "australia": "Australia", "united kingdom": "United Kingdom",
                     "uk": "United Kingdom", "ireland": "Ireland"}

# Site category -> club category enum. Categories not listed here are
# resolved from the title (and flagged when nothing healthcare shows up).
SITE_CATEGORY_MAP = {
    "nursing and midwifery": "nurses",
    "medical doctor": "doctors",
    "pharmacist": "pharmacists",
    "allied health professionals": "non_clinical",
}

log = logging.getLogger("profco_scraper")

# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*(?:p|div|li|tr)\s*>", re.IGNORECASE)


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    """HTML fragment -> flat text, <br> and block ends becoming spaces."""
    return clean_text(_TAG_RE.sub(" ", _BR_RE.sub(" ", markup or "")))


# ---- listing / speciality fragments -----------------------------------------

_JOB_LINK_RE = re.compile(
    r'<a\s+href="index\.php\?job_id=(\d+)"\s+title="([^"]*)"', re.IGNORECASE)
_OPTION_RE = re.compile(r'<option value="(\d+)"[^>]*>(.*?)</option>', re.S)


def parse_job_links(fragment):
    """A getJobSearchResult fragment -> [{job_id, listing_title, job_url}]."""
    seen, jobs = set(), []
    for job_id, title in _JOB_LINK_RE.findall(fragment or ""):
        if job_id in seen:
            continue
        seen.add(job_id)
        jobs.append({
            "job_id": job_id,
            "listing_title": clean_text(title),
            "job_url": JOB_URL_TEMPLATE.format(job_id),
        })
    return jobs


def parse_options(fragment):
    """A <select> fragment -> [(value, label)], dropping the "-- Select --" 0."""
    return [(value, clean_text(label))
            for value, label in _OPTION_RE.findall(fragment or "")
            if value != "0"]


# ---- job detail fragment ----------------------------------------------------

_DETAIL_TITLE_RE = re.compile(r"<h1>\s*(?:#(\d+)\s*)?(.*?)</h1>", re.S)
_DETAIL_ROW_RE = re.compile(r"<th>(.*?)</th>\s*<td>(.*?)</td>", re.S)


def parse_detail(fragment):
    """A getContent fragment -> {"title": str, <field label>: html, ...}.

    Field labels are normalised to lowercase without surrounding spaces
    ("Requirements " -> "requirements"), values kept as raw HTML so callers
    decide how to flatten them.
    """
    fragment = fragment or ""
    detail = {"title": ""}
    title = _DETAIL_TITLE_RE.search(fragment)
    if title:
        detail["title"] = clean_text(title.group(2))
    for label, value in _DETAIL_ROW_RE.findall(fragment):
        detail[clean_text(label).lower()] = value
    return detail


# ---- salary -----------------------------------------------------------------

_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")
# Below this, a figure reads as a monthly wage rather than an annual package.
ANNUAL_THRESHOLD = 20_000


def parse_amount(text):
    """"86434 AUD" -> 86434; "On Application SAR"/"" -> None."""
    match = _AMOUNT_RE.search(clean_text(text))
    if not match:
        return None
    try:
        value = int(round(float(match.group(0).replace(",", ""))))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_currency(*texts):
    """First 3-letter currency code in the given salary cells, or ""."""
    for text in texts:
        match = _CURRENCY_RE.search(clean_text(text).upper())
        if match:
            return match.group(1)
    return ""


def parse_salary(min_text, max_text):
    """The two salary cells -> salary fields (master spec §3).

    The site shows "Salary min"/"Salary max" as a bare amount plus a currency
    ("86434 AUD") or the placeholder "On Application SAR". The placeholder
    yields "Not Disclosed" with empty numbers; nothing is ever invented.
    The period is never stated, so `salary_period_original` stays empty and
    `salary_period` is inferred from magnitude (documented in the readme).
    """
    currency = parse_currency(min_text, max_text)
    low, high = parse_amount(min_text), parse_amount(max_text)
    if low is None and high is None:
        return {"salary_raw": "Not Disclosed", "salary_min": "",
                "salary_max": "", "salary_period": "",
                "salary_period_original": "", "salary_currency": ""}
    low = low if low is not None else high
    high = high if high is not None else low
    if high < low:
        low, high = high, low
    period = "per_annum" if high >= ANNUAL_THRESHOLD else "per_month"
    raw = ("{} {:,}".format(currency, low) if low == high
           else "{} {:,} - {:,}".format(currency, low, high)).strip()
    return {"salary_raw": raw, "salary_min": low, "salary_max": high,
            "salary_period": period, "salary_period_original": "",
            "salary_currency": currency}


# ---- location ---------------------------------------------------------------

def normalize_city(location):
    """Site location cell -> city name (typos and suffixes fixed)."""
    city = clean_text(location).strip(" ,;-")
    return CITY_ALIASES.get(city.lower(), city)


def resolve_country(currency, city, title):
    """Country for a listing, or "" when nothing identifies one.

    Priority: salary currency (SAR/AUD/GBP are country-specific), then the
    city, then a country word prefixed onto the title. Saudi posts are quoted
    in SAR *or* USD, so USD deliberately identifies nothing.
    """
    country = CURRENCY_COUNTRY.get(clean_text(currency).upper(), "")
    if country:
        return country
    country = CITY_COUNTRY.get(normalize_city(city).lower(), "")
    if country:
        return country
    match = TITLE_COUNTRY_RE.match(clean_text(title))
    if match:
        return TITLE_COUNTRY_MAP.get(match.group(1).lower(), "")
    return ""


# ---- classification ---------------------------------------------------------

_NURSE_RE = re.compile(r"nurs|midwif|\brn\b|\bscn\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b",
                            re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"consultant|physician|surgeon|doctor|\bmbbs\b|dentist|registrar|"
    r"medical officer|medical director|intensivist|anaesthetist|anesthetist|"
    # "-ologist" catches specialists, but not lab/imaging TECHNologists
    r"[a-z]+(?<!techn)ologist|psychiatrist|p(a?)ediatrician|obstetrician",
    re.IGNORECASE)

_HEALTHCARE_SIGNAL_RE = re.compile(
    r"medical|pharma|health|nurs|doctor|clinic|hospital|\blab\b|laborator|"
    r"diagnost|patient|dental|surg|therap|physio|radiol|patholog|midwif|"
    r"anaesth|anesth|care\b|ward|theatre|icu\b|\bward\b|paramedic|"
    r"life ?science|biotech|vaccin", re.IGNORECASE)


def classify_category(site_category, title, speciality=""):
    """(club category enum, needs_review) — master spec §2.

    Profco's own category is authoritative for its clinical buckets; the
    catch-all ones (Other / Administration - Management / Engineering /
    Career with Profco) fall back to the title. Nothing is ever dropped: a
    title with no healthcare signal at all is kept and flagged for review.
    """
    mapped = SITE_CATEGORY_MAP.get(clean_text(site_category).lower())
    if mapped:
        return mapped, False

    title = title or ""
    if _NURSE_RE.search(title):
        return "nurses", False
    if _PHARMACIST_RE.search(title):
        return "pharmacists", False
    if _DOCTOR_RE.search(title):
        return "doctors", False

    haystack = " ".join(filter(None, [title, site_category, speciality]))
    needs_review = mapped is None and not _HEALTHCARE_SIGNAL_RE.search(haystack)
    return "non_clinical", needs_review


_PHARMA_RE = re.compile(
    r"pharmaceutical|pharma company|\bcro\b|clinical research|biotech|"
    r"life ?science|medical device", re.IGNORECASE)


def classify_company_type(title, site_category, hospital):
    """Club enum hospital|pharma — Profco places into hospitals by default."""
    haystack = " ".join(filter(None, [title, site_category, hospital]))
    return "pharma" if _PHARMA_RE.search(haystack) else "hospital"


_CONTRACT_RE = re.compile(
    r"locum|\b(\d{2,3})[ -]?day\b|short[ -]?term|temporary", re.IGNORECASE)
_PART_TIME_RE = re.compile(r"part[ -]?time", re.IGNORECASE)


def parse_contract_type(title, benefits=""):
    """Site wording for the engagement, e.g. "Locum / 90-day contract"."""
    text = " ".join(filter(None, [title, benefits]))
    match = _CONTRACT_RE.search(text)
    if not match:
        return ""
    return "locum/short-term contract" if match.group(0).lower().startswith(
        ("locum", "short", "temp")) else "{}-day contract".format(match.group(1))


def job_type_from(title):
    """Club enum full_time|part_time|remote|hybrid.

    Profco places international relocations, so everything is on-site and
    full-time unless a title says part-time; locum/90-day contracts have no
    club enum value and stay full_time (kept verbatim in contract_type).
    """
    return "part_time" if _PART_TIME_RE.search(title or "") else "full_time"


# Matches "3+ years", "3 years" and the spelled-out "two (2) years" form.
_EXPERIENCE_RE = re.compile(
    r"(?:\((\d{1,2})\)|(\d{1,2}))\s*(?:\+|plus)?\s*(?:years?|yrs?)\b",
    re.IGNORECASE)
_MAX_EXPERIENCE_YEARS = 30


def parse_experience_years(*texts):
    """Smallest plausible "N years" figure in the requirements text, or "".

    Real examples: "Minimum of two (2) years of current clinical nursing
    experience" -> 2; "A minimum of 3+ years full-time post-graduate
    experience" -> 3. Figures above 30 years are ignored as false positives.
    """
    values = []
    for text in texts:
        for match in _EXPERIENCE_RE.finditer(clean_text(text)):
            years = int(match.group(1) or match.group(2))
            if 0 < years <= _MAX_EXPERIENCE_YEARS:
                values.append(years)
    return str(min(values)) if values else ""


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff, or the initial window on a first run.

    posted_date here is the first-seen date, so newly found jobs are always
    on/after the cutoff — the window never excludes anything on this site.
    """
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max()
                    - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


def within_window(posted_date, cutoff):
    return bool(posted_date) and posted_date >= cutoff


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def check_robots(session):
    """Honour robots.txt per URL; profco.com currently serves none (404)."""
    parser = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        response = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            log.info("No robots.txt (HTTP %d) — nothing disallowed",
                     response.status_code)
            parser.parse([])
        else:
            parser.parse(response.text.splitlines())
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (HOME_URL, AJAX_URL, JOB_URL_TEMPLATE.format(1)):
        if not parser.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))


def _request(session, url, data=None):
    """GET (data=None) or form-POST with retries; None when it never worked."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            if data is None:
                response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            else:
                response = session.post(url, data=data,
                                        timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(response.status_code)
                continue
            if 400 <= response.status_code < 500:
                log.warning("HTTP %d for %s", response.status_code, url)
                return None
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES,
              last_error)
    return None


def search_jobs(session, jobcat_id="0", jobspeciality_id="0", country_id="0"):
    """One getJobSearchResult call -> (jobs, ok). `ok` is False on failure."""
    fragment = _request(session, AJAX_URL + "?task=getJobSearchResult", data={
        "freetext": "", "country_id": country_id,
        "jobcat_id": jobcat_id, "jobspeciality_id": jobspeciality_id,
    })
    if fragment is None:
        return [], False
    return parse_job_links(fragment), True


def fetch_categories(session):
    """Category id/label pairs from the homepage's job-search form."""
    home = _request(session, HOME_URL)
    if not home:
        log.error("Could not load the homepage — no categories to search")
        return []
    match = re.search(r'<select name="jobcat_id".*?</select>', home or "", re.S)
    return parse_options(match.group(0)) if match else []


def fetch_specialities(session, jobcat_id):
    fragment = _request(session, AJAX_URL, data={
        "task": "getJobSpecialitySelect", "jobcat_id": jobcat_id})
    return parse_options(fragment)


def fetch_detail(session, job_id):
    fragment = _request(session, AJAX_URL,
                        data={"task": "getContent", "job_id": job_id})
    return parse_detail(fragment) if fragment else {}


# ----------------------------------------------------------------------------
# Enumeration
# ----------------------------------------------------------------------------

def enumerate_jobs(session, categories, country_ids=("176", "314", "345")):
    """Every live job id -> listing dict, working around the 50-row cap.

    A category query returns at most RESULT_CAP rows, so a capped category is
    re-queried per speciality (and a capped speciality per country). The
    category-only query is always kept as well: some jobs have no speciality
    assigned and appear nowhere else.

    Returns (jobs_by_id, complete) — `complete` is False when any request
    failed or a slice stayed capped, i.e. the run may have missed listings.
    """
    jobs, complete = {}, True

    def absorb(found):
        for job in found:
            jobs.setdefault(job["job_id"], job)

    everything, ok = search_jobs(session)   # catches category-less jobs too
    complete &= ok
    absorb(everything)

    for jobcat_id, label in categories:
        found, ok = search_jobs(session, jobcat_id=jobcat_id)
        complete &= ok
        absorb(found)
        if len(found) < RESULT_CAP:
            log.info("Category %s (%s): %d jobs", label, jobcat_id, len(found))
            continue

        log.info("Category %s (%s) hit the %d-row cap — drilling specialities",
                 label, jobcat_id, RESULT_CAP)
        specialities = fetch_specialities(session, jobcat_id)
        if not specialities:
            complete = False
            log.error("No speciality list for category %s — results truncated",
                      jobcat_id)
            continue
        for speciality_id, speciality in specialities:
            found, ok = search_jobs(session, jobcat_id=jobcat_id,
                                    jobspeciality_id=speciality_id)
            complete &= ok
            absorb(found)
            if len(found) < RESULT_CAP:
                continue
            log.warning("Speciality %s/%s also capped — drilling countries",
                        label, speciality)
            for country_id in country_ids:
                found, ok = search_jobs(session, jobcat_id=jobcat_id,
                                        jobspeciality_id=speciality_id,
                                        country_id=country_id)
                complete &= ok
                absorb(found)
                if len(found) >= RESULT_CAP:
                    complete = False
                    log.error("Country slice %s/%s/%s still capped — some "
                              "jobs are unreachable", label, speciality,
                              country_id)
        log.info("Category %s (%s): %d specialities drilled, %d jobs known "
                 "so far", label, jobcat_id, len(specialities), len(jobs))
    return jobs, complete


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(listing, detail, today):
    """Listing entry + detail fragment -> a rich CSV row."""
    title = detail.get("title") or listing.get("listing_title", "")
    site_category = clean_text(strip_html(detail.get("category", "")))
    speciality = clean_text(strip_html(detail.get("speciality", "")))
    hospital = strip_html(detail.get("hospital", ""))
    description = strip_html(detail.get("description", ""))
    benefits = strip_html(detail.get("benefits", ""))
    requirements = strip_html(detail.get("requirements", ""))

    salary = parse_salary(detail.get("salary min", ""),
                          detail.get("salary max", ""))
    city = normalize_city(strip_html(detail.get("location", "")))
    country = resolve_country(salary["salary_currency"], city,
                              listing.get("listing_title") or title)
    country_code, dial_code = COUNTRIES.get(country, ("", ""))
    category, needs_review = classify_category(site_category, title, speciality)

    row = {
        "source": SITE,
        "job_id": listing["job_id"],
        "title": title,
        "company": COMPANY_NAME,
        "company_about": hospital[:COMPANY_ABOUT_MAX_CHARS],
        "city": city,
        "country": country,
        "country_code": country_code,
        "country_dial_code": dial_code,
        "site_category": site_category,
        "site_speciality": speciality,
        "contract_type": parse_contract_type(title, benefits),
        "job_type": job_type_from(title),
        "category": category,
        "company_type": classify_company_type(title, site_category, hospital),
        "min_experience_years": parse_experience_years(requirements,
                                                       description),
        # No date is published anywhere on profco.com: posted_date is the
        # date this scraper first saw the listing (see module docstring).
        "needs_review": needs_review or not country,
        "posted_date": today,
        "last_seen": today,
        "description": description[:DESCRIPTION_MAX_CHARS],
        "benefits": benefits[:DESCRIPTION_MAX_CHARS],
        "requirements": requirements[:DESCRIPTION_MAX_CHARS],
        "hospital": hospital[:DESCRIPTION_MAX_CHARS],
        "job_url": listing["job_url"],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    row.update(salary)
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


def club_description(row):
    """Description + requirements + benefits, as one import-ready block."""
    parts = [_blank(row.get("description"))]
    for label, key in (("Requirements", "requirements"),
                       ("Benefits", "benefits")):
        value = _blank(row.get(key))
        if value:
            parts.append("{}: {}".format(label, value))
    return " ".join(part for part in parts if part)[:DESCRIPTION_MAX_CHARS]


def rich_row_to_club_row(row, active_ids=None):
    """Rich row -> HealthCareers.club 22-column row.

    Club salary columns stay empty unless the currency is one the schema
    allows (INR/USD) — SAR/AUD amounts are never converted.
    """
    currency = _blank(row.get("salary_currency"))
    min_salary = _int_str(row.get("salary_min"))
    has_salary = bool(min_salary) and currency in CLUB_CURRENCIES
    job_id = _blank(row.get("job_id"))
    is_active = "true" if active_ids is None or job_id in active_ids else "false"
    return {
        "country_name": _blank(row.get("country")),
        "country_code": _blank(row.get("country_code")),
        "country_dial_code": _blank(row.get("country_dial_code")),
        "city_name": _blank(row.get("city")),
        "company_name": _blank(row.get("company")) or COMPANY_NAME,
        "company_type": _blank(row.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": _blank(row.get("company_about")),
        "title": _blank(row.get("title")),
        "description": club_description(row),
        "job_type": _blank(row.get("job_type")) or "full_time",
        "category": _blank(row.get("category")) or "non_clinical",
        "application_url": _blank(row.get("job_url")),
        "posted_at": _blank(row.get("posted_date")),
        "min_experience": _int_str(row.get("min_experience_years")),
        "max_experience": "",
        "min_salary": min_salary if has_salary else "",
        "max_salary": _int_str(row.get("salary_max")) if has_salary else "",
        "salary_period": _blank(row.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
        "is_active": is_active,
        "expires_at": "",
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def write_club_csv(rich_df, run_date, active_ids):
    rows = [rich_row_to_club_row(row, active_ids)
            for _, row in rich_df.iterrows()]
    club_df = pd.DataFrame(rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from profco.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="fetch detail pages for at most N new jobs "
                             "(test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    today = date.today().isoformat()
    session = make_session()
    check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; keeping jobs dated on/after %s "
             "(posted_date = first-seen date)", len(known_ids), cutoff)

    categories = fetch_categories(session)
    log.info("Job categories: %s", ", ".join(label for _, label in categories))
    listings, complete = enumerate_jobs(session, categories)
    log.info("Enumerated %d live listings (%s)", len(listings),
             "complete" if complete else "INCOMPLETE — see warnings above")

    counters = {"scanned": len(listings), "excluded_non_healthcare": 0,
                "excluded_old": 0, "needs_review": 0, "new": 0,
                "duplicates": 0, "detail_failed": 0, "skipped_by_limit": 0}
    new_rows, review_log = [], []

    fresh = [job for job in listings.values() if job["job_id"] not in known_ids]
    counters["duplicates"] = len(listings) - len(fresh)
    if args.limit is not None:
        counters["skipped_by_limit"] = max(0, len(fresh) - args.limit)
        fresh = fresh[:args.limit]
        log.info("--limit %d: fetching detail pages for %d of the new jobs",
                 args.limit, len(fresh))

    for job in fresh:
        detail = fetch_detail(session, job["job_id"])
        if not detail or not detail.get("title"):
            counters["detail_failed"] += 1
            log.warning("No detail fragment for job %s — using listing data",
                        job["job_id"])
        try:
            row = build_row(job, detail, today)
        except Exception as exc:      # one bad job never kills the run (§7)
            counters["detail_failed"] += 1
            log.warning("Skipping malformed job %s: %s", job["job_id"], exc)
            continue
        if not within_window(row["posted_date"], cutoff):
            counters["excluded_old"] += 1
            continue
        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "site_category": row["site_category"],
                               "site_speciality": row["site_speciality"],
                               "country": row["country"],
                               "job_url": row["job_url"]})
        new_rows.append(row)
        counters["new"] += 1

    # ---- rich cumulative CSV ----
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
    else:
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))

    # Refresh last_seen for every listing still on the board.
    if len(combined) and "job_id" in combined.columns:
        still_live = combined["job_id"].isin(listings.keys())
        combined.loc[still_live, "last_seen"] = today
    for column in RICH_COLUMNS:
        if column not in combined.columns:
            combined[column] = ""
    combined = combined[RICH_COLUMNS]
    combined.to_csv(args.output, index=False)
    log.info("Wrote %s (%d total rows, %d new)", args.output, len(combined),
             len(new_rows))

    # ---- club-schema CSV ----
    if len(combined):
        # Closed postings are only demoted when this run saw the whole board.
        active_ids = set(listings) if complete else None
        target, count = write_club_csv(combined, args.run_date, active_ids)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, count)

    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d rows)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Jobs scanned:            {:>5,}".format(counters["scanned"]))
    print("Excluded non-healthcare: {:>5,}".format(
        counters["excluded_non_healthcare"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff,
                                                    counters["excluded_old"]))
    print("Flagged needs_review:    {:>5,}".format(counters["needs_review"]))
    print("New jobs added:          {:>5,}".format(counters["new"]))
    print("Duplicates skipped:      {:>5,}".format(counters["duplicates"]))
    print("Detail parse failures:   {:>5,}".format(counters["detail_failed"]))
    if counters["skipped_by_limit"]:
        print("Left for the next run (--limit): {:>5,}".format(
            counters["skipped_by_limit"]))


if __name__ == "__main__":
    main()
