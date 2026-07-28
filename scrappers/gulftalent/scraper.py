#!/usr/bin/env python3
"""Scrape healthcare job listings from gulftalent.com (Kuwait by default).

Data source
-----------
GulfTalent publishes one server-rendered listing per country + industry:

    https://www.gulftalent.com/<country>/jobs/industry/healthcare        (page 1)
    https://www.gulftalent.com/<country>/jobs/industry/healthcare/<N>    (page N)

Industry 15 = "Healthcare, Pharmaceuticals & Medical Services", so every job
on this listing is healthcare-sector at the source (master spec §2). 25 rows
per page, sorted newest-first, with a `pagination-last-btn` link giving the
page count. There IS a JSON endpoint behind the filter sidebar
(`/api/jobs/search`), but it only returns facet counts — `config[results]`
variants 500 — so the server-rendered listing + the detail pages' schema.org
JobPosting JSON-LD are the structured sources (spec preference #2).

Listing rows carry: numeric job id (`data-ga-label`, the dedup key), title,
detail URL, company (linked or plain text), city, posted date as "13 May"
(no year) and the company logo. Detail pages add the JSON-LD JobPosting
(datePosted, validThrough, employmentType, hiringOrganization + logo,
baseSalary, jobLocation, industry, full HTML description) plus an attribute
grid (Job Type, Job Location, Nationality, Salary, Gender, Arabic Fluency,
Job Function, Company Industry), the "Ref: ..." code and an
"About the Company" blurb.

Quirks
------
* The site 302-redirects non-browser User-Agents to `/mobile/...`, whose
  listing renders only the first 25 jobs and whose `/mobile/.../<N>` paths
  serve an unrelated FAQ page. The UA below therefore keeps a desktop
  platform token in front of the scraper's own name + contact address.
* Listing dates have no year ("13 May"); they are only used for the
  early-stop check. The authoritative `posted_date` is the detail page's
  JSON-LD `datePosted`.
* Postings are purged ~90 days after posting (`validThrough` = posted + 90),
  so the listing only ever holds live jobs.
* Salary is usually "Not Specified"; when shown it is a monthly Gulf-currency
  range (e.g. "7000 - 8000 AED"). The club schema's salary_currency enum only
  allows INR/USD, so non-USD amounts stay in the rich CSV and the club salary
  fields stay blank (same decision as the dubizzle/dubailivejobs scrapers) —
  no currency is invented.
* Kuwait's healthcare listing is tiny (1 live job on 2026-07-27); the same
  scraper serves any country slug via `--country` (uae, saudi-arabia, qatar…).

Healthcare filter: the listing is already industry-filtered, so the classifier
only maps a job onto the club category enum. Titles with no clinical or
healthcare signal (GulfTalent lists commercial/admin roles at pharma
companies) are KEPT and flagged `needs_review` (master spec §2).

Time window: the listing only holds live postings, so the first run keeps ALL
of them (`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec
watermark (newest stored posted_date minus WATERMARK_GRACE_DAYS).

robots.txt: gulftalent.com blocks a named list of AI-training crawlers and
throttles some SEO bots, but ends with `User-agent: * / Allow: /` — this
scraper's UA falls under that rule. Checked per-URL at startup.

Outputs
-------
* gulftalent_jobs.csv                          — rich cumulative store
                                                 (dedup key: numeric job id)
* ../../jobs_csv/<DD-MM-YYYY>/gulftalent.csv   — HealthCareers.club 22-col schema
* needs_review.csv                             — titles flagged for review

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

SITE = "gulftalent"
SITE_BASE = "https://www.gulftalent.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
DEFAULT_COUNTRY = "kuwait"
INDUSTRY_SLUG = "healthcare"   # industry 15: Healthcare, Pharma & Medical Services

# A desktop platform token is required or the site redirects to /mobile/,
# where pagination is broken; the scraper still names itself + a contact URL.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec §3): salaries are captured, never filtered on.
# The listing only holds live postings (purged ~90 days after posting), so the
# first run keeps all of them.
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 25
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
MAX_PAGES_SAFETY = 200
DESCRIPTION_MAX_CHARS = 3_000
ABOUT_MAX_CHARS = 1_000

RICH_CSV = "gulftalent_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# GulfTalent country slug -> (country_name, ISO-2, dial code, currency).
COUNTRY_META = {
    "kuwait": ("Kuwait", "KW", "+965"),
    "uae": ("United Arab Emirates", "AE", "+971"),
    "saudi-arabia": ("Saudi Arabia", "SA", "+966"),
    "qatar": ("Qatar", "QA", "+974"),
    "oman": ("Oman", "OM", "+968"),
    "bahrain": ("Bahrain", "BH", "+973"),
    "egypt": ("Egypt", "EG", "+20"),
    "jordan": ("Jordan", "JO", "+962"),
    "lebanon": ("Lebanon", "LB", "+961"),
    "iraq": ("Iraq", "IQ", "+964"),
    "morocco": ("Morocco", "MA", "+212"),
}

RICH_COLUMNS = [
    "source", "job_id", "reference", "title", "company", "company_url",
    "company_logo", "company_about", "city", "country", "country_code",
    "country_dial_code", "salary_raw", "salary_min_monthly",
    "salary_max_monthly", "salary_period_original", "salary_currency",
    "job_type", "job_type_original", "job_function", "site_industry",
    "nationality", "gender", "arabic_fluency", "easy_apply",
    "experience_raw", "experience_min_years", "experience_max_years",
    "category", "company_type", "needs_review", "posted_date", "expires_at",
    "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("gulftalent_scraper")


def listing_url(country, page=1):
    base = "{}/{}/jobs/industry/{}".format(SITE_BASE, country, INDUSTRY_SLUG)
    return base if page <= 1 else "{}/{}".format(base, page)


# ----------------------------------------------------------------------------
# Parsing helpers (pure functions — unit-tested in test_filters.py)
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    """HTML -> plain text, keeping list items and paragraphs separated."""
    text = re.sub(r"</(p|li|h\d|div|tr)>", " ", markup or "", flags=re.I)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    return clean_text(_TAG_RE.sub(" ", text))


# ---- listing rows -----------------------------------------------------------

_ROW_SPLIT_RE = re.compile(r'<tr class="content-visibility-auto">')
_ROW_LINK_RE = re.compile(
    r'<a class="ga-job-impression[^"]*"[^>]*?data-ga-label="(\d+)"'
    r'[^>]*?data-ga-dimension-three="([^"]*)"'
    r'[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_ROW_COMPANY_LINK_RE = re.compile(
    r'<a class="text-base text-muted text-secondary-hover"[^>]*?'
    r'href="(/companies/[^"]+)"[^>]*>(.*?)</a>', re.S)
_ROW_CITY_RE = re.compile(
    r'<td class="text-overflow col-sm-6">(.*?)</td>', re.S)
_ROW_CITY_SPAN_RE = re.compile(r'<span title="([^"]*)">(.*?)</span>', re.S)
_ROW_DATE_RE = re.compile(r'<td class="col-sm-4">(.*?)</td>', re.S)
_ROW_LOGO_CELL_RE = re.compile(
    r'<td class="text-center col-sm-5">(.*?)</td>', re.S)
_IMG_SRC_RE = re.compile(r'<img\s[^>]*?src="([^"]+)"', re.S)
_ROW_TITLE_CELL_RE = re.compile(r'<td class="col-sm-21[^"]*">(.*?)</td>', re.S)
_LAST_PAGE_RE = re.compile(
    r'href="([^"]*?/(\d+))"[^>]*\s+data-cy="pagination-last-btn"', re.S)


def parse_last_page(page_html):
    """Highest listing page number from the pager (1 when there is no pager)."""
    match = _LAST_PAGE_RE.search(page_html or "")
    if match:
        try:
            return max(1, int(match.group(2)))
        except ValueError:
            pass
    return 1


def _row_company(title_cell):
    """(company_name, company_page_url) — the name is linked or bare text."""
    linked = _ROW_COMPANY_LINK_RE.search(title_cell)
    if linked:
        return clean_text(linked.group(2)), SITE_BASE + linked.group(1)
    # Unlinked employers sit as bare text after the title paragraph.
    tail = title_cell.rsplit("</p>", 1)[-1]
    return strip_html(tail), ""


def parse_listing_rows(page_html):
    """All job rows on a listing page -> list of dicts (may be empty)."""
    rows = []
    for chunk in _ROW_SPLIT_RE.split(page_html or "")[1:]:
        link = _ROW_LINK_RE.search(chunk)
        if not link:
            continue
        job_id, country_slug, path, title = link.groups()
        title_cell = _ROW_TITLE_CELL_RE.search(chunk)
        company, company_url = _row_company(
            title_cell.group(1) if title_cell else "")
        city_cell = _ROW_CITY_RE.search(chunk)
        city, city_title = "", ""
        if city_cell:
            span = _ROW_CITY_SPAN_RE.search(city_cell.group(1))
            if span:
                city_title, city = clean_text(span.group(1)), clean_text(span.group(2))
            else:
                city = strip_html(city_cell.group(1))
        date_cell = _ROW_DATE_RE.search(chunk)
        # The logo cell is empty for employers without a profile — search
        # inside the cell only, never past it.
        logo_cell = _ROW_LOGO_CELL_RE.search(chunk)
        logo = _IMG_SRC_RE.search(logo_cell.group(1)) if logo_cell else None
        rows.append({
            "job_id": job_id,
            "country_slug": country_slug,
            "title": clean_text(title),
            "job_url": SITE_BASE + path.split("?")[0],
            "company": company,
            "company_url": company_url,
            "city": city,
            "city_full": city_title,
            "listing_date_raw": strip_html(date_cell.group(1)) if date_cell else "",
            "company_logo": logo.group(1) if logo else "",
            "easy_apply": 'class="icon-easy-apply"' in chunk,
        })
    return rows


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_LISTING_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s*(\d{4})?")


def parse_listing_date(text, today=None):
    """Listing "13 May" / "13 May 2025" -> ISO date ("" when unparseable).

    The listing omits the year; a date that would land in the future is read
    as last year's (postings live ~90 days, so the ambiguity is one year).
    """
    today = today or date.today()
    match = _LISTING_DATE_RE.search(clean_text(text))
    if not match:
        return ""
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name.lower())
    if not month:
        return ""
    try:
        if year:
            return date(int(year), month, int(day)).isoformat()
        parsed = date(today.year, month, int(day))
        if parsed > today + timedelta(days=7):
            parsed = date(today.year - 1, month, int(day))
        return parsed.isoformat()
    except ValueError:
        return ""


# ---- detail pages -----------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S)
_LAX_DECODER = json.JSONDecoder(strict=False)


def parse_job_posting(page_html):
    """The schema.org JobPosting JSON-LD block of a detail page, or {}."""
    for match in _LDJSON_RE.finditer(page_html or ""):
        try:
            data, _ = _LAX_DECODER.raw_decode(match.group(1).strip())
        except ValueError as exc:
            log.debug("Unparseable ld+json block: %s", exc)
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


_ATTR_RE = re.compile(
    r'<span style="color: #6c757d">(.*?)</span><br>\s*<span>(.*?)</span>', re.S)
_ABOUT_RE = re.compile(
    r'<h4 class="header-ribbon">About the Company</h4>(.*?)(?=<h4|<div\s|\Z)', re.S)
_REF_RE = re.compile(r'<span class="text-supermuted">\s*Ref:\s*(.*?)</span>', re.S)


def parse_detail_attributes(page_html):
    """The detail page's label/value grid -> {"Job Type": "Full Time", ...}."""
    return {clean_text(k): clean_text(v)
            for k, v in _ATTR_RE.findall(page_html or "")}


def parse_about_company(page_html):
    match = _ABOUT_RE.search(page_html or "")
    return strip_html(match.group(1))[:ABOUT_MAX_CHARS] if match else ""


def parse_reference(page_html):
    match = _REF_RE.search(page_html or "")
    return clean_text(match.group(1)) if match else ""


# ---- salary -----------------------------------------------------------------

_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}
_CURRENCY_CODES = ("AED", "SAR", "QAR", "KWD", "OMR", "BHD", "USD", "EGP",
                   "JOD", "LBP", "IQD", "MAD", "GBP", "EUR", "INR")
_NOT_DISCLOSED = "Not Disclosed"
_AMOUNT_RE = re.compile(r"\d[\d,\.]*")


def _to_int(value):
    try:
        number = int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_salary_text(text):
    """Grid salary string -> {} when undisclosed, else parsed fields.

    Real GulfTalent forms: "Not Specified", "7000 - 8000 AED",
    "30000 - 40000 AED", "$4,000 - $5,000", "AED 12,000". Amounts are
    monthly (the detail page spells out "per month inclusive of fixed
    allowances"); nothing is invented when the field is absent.
    """
    raw = clean_text(text)
    if not raw or raw.lower() in ("not specified", "not disclosed",
                                  "unspecified", "negotiable", "n/a"):
        return {}
    currency = ""
    upper = raw.upper()
    for code in _CURRENCY_CODES:
        if re.search(r"\b{}\b".format(code), upper):
            currency = code
            break
    if not currency:
        for symbol, code in _CURRENCY_SYMBOLS.items():
            if symbol in raw:
                currency = code
                break
    amounts = [_to_int(a) for a in _AMOUNT_RE.findall(raw.replace(currency, ""))]
    amounts = [a for a in amounts if a is not None]
    if not amounts:
        return {}
    low, high = min(amounts), max(amounts)
    return {
        "salary_raw": raw,
        "salary_min_monthly": low,
        "salary_max_monthly": high,
        "salary_period_original": "per_month",
        "salary_currency": currency,
    }


def parse_base_salary(posting):
    """JobPosting.baseSalary -> parsed fields; {} when the block is absent.

    Real example (job 613092): currency AED, minValue 7000, maxValue 8000,
    unitText MONTH. Annual amounts are normalised to a monthly figure.
    """
    base = posting.get("baseSalary") or {}
    value = base.get("value") or {}
    low, high = _to_int(value.get("minValue")), _to_int(value.get("maxValue"))
    if low is None and high is None:
        return {}
    low = low if low is not None else high
    high = high if high is not None else low
    if high < low:
        low, high = high, low
    unit = clean_text(value.get("unitText")).upper()
    period = "per_annum" if unit == "YEAR" else "per_month"
    if period == "per_annum":
        low, high = int(round(low / 12.0)), int(round(high / 12.0))
    currency = clean_text(base.get("currency")).upper()
    return {
        "salary_raw": "{} {:,} - {:,} per month".format(
            currency or "", low, high).strip(),
        "salary_min_monthly": low,
        "salary_max_monthly": high,
        "salary_period_original": "per_month",
        "salary_currency": currency,
    }


# ---- experience -------------------------------------------------------------

_EXPERIENCE_RES = (
    re.compile(r"(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\+?\s*years?[^.]{0,40}?experience",
               re.IGNORECASE),
    re.compile(r"(?:minimum|min\.?|at least|over)\s*(?:of\s*)?(\d{1,2})\+?\s*"
               r"years?[^.]{0,40}?experience", re.IGNORECASE),
    re.compile(r"(\d{1,2})\+\s*years?[^.]{0,40}?experience", re.IGNORECASE),
    re.compile(r"experience[^.]{0,40}?(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*years?",
               re.IGNORECASE),
    re.compile(r"experience[^.]{0,30}?(?:of\s*)?(\d{1,2})\+?\s*years?",
               re.IGNORECASE),
)


def parse_experience(description):
    """(raw_phrase, min_years, max_years) mined from the description text.

    GulfTalent has no structured experience field, so the requirement is read
    from the description ("Minimum 7 years of experience within..." ->
    (phrase, 7, "")). Nothing found -> ("", "", "").
    """
    text = clean_text(description)
    for pattern in _EXPERIENCE_RES:
        match = pattern.search(text)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        low = _to_int(groups[0])
        high = _to_int(groups[1]) if len(groups) > 1 else None
        if low is None or low > 50:
            continue
        if high is not None and high < low:
            low, high = high, low
        return match.group(0).strip(), low, (high if high is not None else "")
    return "", "", ""


# ---- classification ---------------------------------------------------------

_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"general practitioner|\bgp\b|[a-z]+ologist|intensivist|hospitalist|"
    r"an(a)?esthetist|obstetrician|p(a?)ediatrician|psychiatrist|veterinar|"
    r"medical director|medical superintendent|medical affairs|"
    r"medical science liaison|\bmsl\b|consultant\s+(?:physician|surgeon)",
    re.IGNORECASE)

_HEALTHCARE_SIGNAL_RE = re.compile(
    r"medical|pharma|health|nurs|doctor|clinic|hospital|\blab\b|laborator|"
    r"diagnost|patient|dental|surgi|therap|physio|radiol|patholog|"
    r"life ?science|biotech|\bcro\b|clinical|vaccin|wellness|med.?tech|"
    r"device|dermat|optic|care\b", re.IGNORECASE)


def classify_category(title, job_function="", company="", description=""):
    """Club enum doctors|nurses|pharmacists|non_clinical + needs_review flag.

    The listing is industry-filtered, so the site industry ("Healthcare,
    Pharmaceuticals & Medical Services") carries no signal and is deliberately
    left out of the review haystack: a commercial role at a pharma company
    would otherwise never be flagged. Nothing is dropped either way.
    """
    title = title or ""
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title):
        category = "pharmacists"
    elif _DOCTOR_RE.search(title):
        category = "doctors"
    else:
        # Everything else — including the site's broad "Healthcare" job
        # function (technicians, receptionists, allied staff) — is non_clinical.
        category = "non_clinical"
    haystack = " ".join(filter(None, [title, job_function, company,
                                      (description or "")[:400]]))
    needs_review = (category == "non_clinical"
                    and clean_text(job_function).lower() != "healthcare"
                    and not _HEALTHCARE_SIGNAL_RE.search(haystack))
    return category, needs_review


_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\bcro\b|clinical research|biotech|"
    r"life ?science|med.?tech|medical device|vaccin|diagnost|\bapi\b",
    re.IGNORECASE)


def classify_company_type(company, title, job_function):
    """Club enum hospital|pharma.

    Only the employer name, title and job function are consulted: most of
    GulfTalent's healthcare advertisers are agencies whose "About the Company"
    blurb name-drops every sector they staff, which would make everything
    look like pharma.
    """
    haystack = " ".join(filter(None, [company, title, job_function]))
    return "pharma" if _PHARMA_RE.search(haystack) else "hospital"


def country_meta(country_slug, address_country=""):
    """Listing slug (fallback: JSON-LD addressCountry) -> (name, ISO-2, dial)."""
    key = clean_text(country_slug).lower()
    if key in COUNTRY_META:
        return COUNTRY_META[key]
    key = clean_text(address_country).lower().replace(" ", "-")
    if key in COUNTRY_META:
        return COUNTRY_META[key]
    for name, code, dial in COUNTRY_META.values():
        if name.lower() == key.replace("-", " "):
            return name, code, dial
    return clean_text(address_country) or clean_text(country_slug).title(), "", ""


def city_from(row_city, address_locality, country_name, country_code="",
              country_slug=""):
    """Best city for a job, or "" for a country-wide posting.

    The listing's city column is the site's own city facet and wins over the
    JSON-LD locality, which is sometimes street-level ("Zabeel 2 - Zabeel -
    Dubai"). A value that merely repeats the country ("UAE", "Kuwait") is
    dropped — those postings are country-wide.
    """
    aliases = {clean_text(alias).lower()
               for alias in (country_name, country_code, country_slug) if alias}
    for candidate in (row_city, address_locality):
        city = clean_text(candidate)
        if city and city.lower() not in aliases:
            return city
    return ""


def job_type_from(grid_job_type, employment_type=""):
    """Site job type -> club enum (full_time|part_time|remote|hybrid).

    Temporary/Contract/Internship have no club enum value, so they map to
    full_time; the site's own wording is kept in `job_type_original`.
    """
    raw = clean_text(grid_job_type).lower()
    if not raw:
        employment = employment_type
        if isinstance(employment, list):
            employment = employment[0] if employment else ""
        raw = clean_text(employment).lower().replace("_", " ")
    if "part" in raw:
        return "part_time"
    if "remote" in raw:
        return "remote"
    if "hybrid" in raw:
        return "hybrid"
    return "full_time"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date), or None = keep everything.

    First run: the listing only holds live postings (purged ~90 days after
    posting), so all of them are current (INITIAL_WINDOW_DAYS = None).
    """
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
    return bool(posted_date) and posted_date >= cutoff


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def check_robots(session, country):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (listing_url(country), listing_url(country, 2),
                "{}/{}/jobs/sample-job-1".format(SITE_BASE, country)):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, url):
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
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            if "/mobile/" in resp.url:
                log.warning("Redirected to the mobile site (%s) — the desktop "
                            "User-Agent token may have been rejected", resp.url)
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(row, posting=None, attributes=None, about="", country_slug=""):
    """Rich row from a listing row + its detail page (JSON-LD + grid)."""
    posting = posting or {}
    attributes = attributes or {}

    title = row.get("title") or clean_text(posting.get("title"))
    organization = posting.get("hiringOrganization") or {}
    company = row.get("company") or clean_text(organization.get("name"))
    job_function = attributes.get("Job Function", "")
    industry = clean_text(posting.get("industry")) or attributes.get(
        "Company Industry", "")

    address = (posting.get("jobLocation") or {}).get("address") or {}
    country, code, dial = country_meta(
        row.get("country_slug") or country_slug, address.get("addressCountry"))
    city = city_from(row.get("city", ""), address.get("addressLocality"),
                     country, code, row.get("country_slug") or country_slug)

    description = strip_html(posting.get("description") or "")
    experience_raw, experience_min, experience_max = parse_experience(description)
    category, needs_review = classify_category(title, job_function, company,
                                               description)

    posted_date = clean_text(posting.get("datePosted"))[:10]
    if not posted_date:
        posted_date = parse_listing_date(row.get("listing_date_raw", ""))

    result = {
        "source": SITE,
        "job_id": row.get("job_id", ""),
        "reference": row.get("reference", ""),
        "title": title,
        "company": company,
        "company_url": row.get("company_url", ""),
        "company_logo": (row.get("company_logo")
                         or clean_text(organization.get("logo"))),
        "company_about": about,
        "city": city,
        "country": country,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": _NOT_DISCLOSED,
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "salary_currency": "",
        "job_type": job_type_from(attributes.get("Job Type"),
                                  posting.get("employmentType")),
        "job_type_original": attributes.get("Job Type", ""),
        "job_function": job_function,
        "site_industry": industry,
        "nationality": attributes.get("Nationality", ""),
        "gender": attributes.get("Gender", ""),
        "arabic_fluency": attributes.get("Arabic Fluency", ""),
        "easy_apply": bool(row.get("easy_apply")),
        "experience_raw": experience_raw,
        "experience_min_years": experience_min,
        "experience_max_years": experience_max,
        "category": category,
        "company_type": classify_company_type(company, title, job_function),
        "needs_review": needs_review,
        "posted_date": posted_date,
        "expires_at": clean_text(posting.get("validThrough"))[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": row.get("job_url", ""),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # JSON-LD baseSalary is authoritative; the grid string is the fallback.
    result.update(parse_base_salary(posting)
                  or parse_salary_text(attributes.get("Salary")))
    return result


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


def rich_row_to_club_row(r):
    """Rich row -> HealthCareers.club 22-column row.

    Gulf currencies (AED/KWD/…) cannot be expressed in the club's
    salary_currency enum (INR/USD only), so their amounts stay in the rich
    CSV and the club salary fields are left blank — no currency is invented.
    """
    currency = _blank(r.get("salary_currency")).upper()
    min_salary = _int_str(r.get("salary_min_monthly"))
    has_salary = bool(min_salary) and currency in ("INR", "USD")
    return {
        "country_name": _blank(r.get("country")),
        "country_code": _blank(r.get("country_code")),
        "country_dial_code": _blank(r.get("country_dial_code")),
        # Country-wide postings ("Job Location: Kuwait") carry no city in the
        # rich CSV; the club feed expects a place name, so the country stands in.
        "city_name": _blank(r.get("city")) or _blank(r.get("country")),
        "company_name": _blank(r.get("company")),
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": _blank(r.get("company_logo")),
        "company_about": _blank(r.get("company_about")),
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")) or "non_clinical",
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        "min_salary": min_salary if has_salary else "",
        "max_salary": _int_str(r.get("salary_max_monthly")) if has_salary else "",
        "salary_period": "per_month" if has_salary else "",
        "salary_currency": currency if has_salary else "",
        "is_active": "true",
        "expires_at": _blank(r.get("expires_at")),
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


def scrape(session, country, cutoff, known_ids, max_pages=None, limit=None):
    """Walk the listing, fetch details for unseen jobs, return (rows, counters)."""
    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows = []
    page, empty_pages, last_page = 1, 0, None

    while page <= MAX_PAGES_SAFETY:
        if max_pages is not None and page > max_pages:
            break
        page_html = _request(session, listing_url(country, page))
        if page_html and last_page is None:
            last_page = parse_last_page(page_html)
            log.info("Listing reports %d page(s) for %s", last_page, country)
        rows = parse_listing_rows(page_html) if page_html else None
        if not rows:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES or (last_page and page >= last_page):
                break
            page += 1
            continue
        empty_pages = 0

        page_dates = []
        for row in rows:
            counters["scanned"] += 1
            page_dates.append(parse_listing_date(row["listing_date_raw"]))
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            listing_date = page_dates[-1]
            # Cheap pre-filter: the listing date (day precision, no year) is
            # only trusted to skip clearly-old jobs before paying for a detail
            # fetch; the JSON-LD datePosted decides below.
            if cutoff and listing_date and listing_date < cutoff:
                counters["excluded_old"] += 1
                continue

            detail_html = _request(session, row["job_url"])
            posting = parse_job_posting(detail_html or "")
            attributes = parse_detail_attributes(detail_html or "")
            if not posting and not attributes:
                counters["detail_failed"] += 1
                log.warning("No structured data on %s", row["job_url"])
            row["reference"] = parse_reference(detail_html or "")
            try:
                built = build_row(row, posting, attributes,
                                  parse_about_company(detail_html or ""), country)
            except Exception as exc:   # one bad page must never end the run
                log.warning("Skipping malformed job %s: %s", row["job_id"], exc)
                continue
            if not within_window(built["posted_date"], cutoff):
                counters["excluded_old"] += 1
                continue
            if built["needs_review"]:
                counters["needs_review"] += 1
            known_ids.add(built["job_id"])
            new_rows.append(built)
            counters["new"] += 1
            if limit is not None and counters["new"] >= limit:
                log.info("Reached --limit of %d new jobs", limit)
                return new_rows, counters

        # The listing is sorted newest-first: once a whole page predates the
        # cutoff, every later page does too.
        dated = [d for d in page_dates if d]
        if cutoff and dated and all(d < cutoff for d in dated):
            log.info("Page %d is entirely older than %s — stopping", page, cutoff)
            break
        if last_page and page >= last_page:
            break
        if len(rows) < PAGE_SIZE:
            break
        page += 1

    return new_rows, counters


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from gulftalent.com.")
    parser.add_argument("--country", default=DEFAULT_COUNTRY,
                        help="GulfTalent country slug, e.g. kuwait, uae, "
                             "saudi-arabia (default: %(default)s)")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages of %d (test runs)" % PAGE_SIZE)
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--no-club-csv", action="store_true",
                        help="skip writing the HealthCareers.club CSV")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session, args.country)

    existing_df = load_existing(args.output)
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None and "job_id" in existing_df.columns
                 else set())
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff or "(no cutoff — first run keeps all live jobs)")

    new_rows, counters = scrape(session, args.country, cutoff, known_ids,
                                max_pages=args.max_pages, limit=args.limit)

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
    if len(combined) and not args.no_club_csv:
        target, count = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, count)

    # ---- needs_review sidecar ----
    flagged = [{"job_id": r["job_id"], "title": r["title"],
                "job_function": r["job_function"], "company": r["company"],
                "job_url": r["job_url"]}
               for r in new_rows if r["needs_review"]]
    if flagged:
        review_df = pd.DataFrame(flagged)
        try:
            previous = pd.read_csv("needs_review.csv", dtype=str)
            review_df = pd.concat([previous, review_df], ignore_index=True)
        except FileNotFoundError:
            pass
        review_df.drop_duplicates(subset="job_id").to_csv("needs_review.csv",
                                                          index=False)

    print("\n===== Run summary ({}) =====".format(args.country))
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(
        cutoff or "n/a", counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    print("Detail parse failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
