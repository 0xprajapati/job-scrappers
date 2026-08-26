#!/usr/bin/env python3
"""Scrape healthcare job listings from freshersworld.com.

Data source
-----------
Freshersworld is a general Indian fresher-jobs board, but it exposes
healthcare-specific, server-rendered category listings — the master spec's
preferred source-side filter:

    https://www.freshersworld.com/jobs/category/health-care-job-vacancies
    https://www.freshersworld.com/jobs/category/pharma-job-vacancies

Each listing page carries 20 job cards with `job_id="..."` and
`job_display_url="..."` attributes plus title, company, location, salary
("100000 - 150000 Monthly"), experience, qualification and a relative
"Posted: N days ago" stamp. Pagination is `?limit=20&offset=N`; a page with
zero cards ends the category. The two categories overlap heavily (medical rep
jobs are tagged with both) — dedup by job_id absorbs that.

Every job DETAIL page embeds a schema.org JobPosting JSON-LD block with the
exact `datePosted`, `validThrough`, a clean `title` (the card only has the
long SEO title), full HTML `description`, `employmentType`,
`qualifications`, `experienceRequirements.monthsOfExperience`, structured
`baseSalary` (INR, min/max, unit Month/Year) and `jobLocation` addresses.
Detail pages are fetched ONLY for jobs not already in the CSV whose card
age-estimate is inside the time window — a handful per day after the first
run — so there is no separate --enrich flag: the detail fetch IS the primary
source of posted_date/description, and it is already minimal.

robots.txt: `Allow:/` with specific disallows. The paths this scraper uses —
/jobs/category/* and /jobs/<slug>-<id> — are allowed. The disallowed
/jobsearch, /jobs/jobsearch/, /jobs/getjobs and /*ajax_* endpoints are never
touched, and pagination uses limit=20 (only `/*limit=25` is disallowed).
Compliance is verified at startup with urllib.robotparser.

Verified quirks
---------------
* The active inventory is tiny (~22 health-care + ~25 pharma jobs), so every
  run crawls both categories fully — no early-stop needed.
* Cards are roughly newest-first but premium/"HOT JOB" cards are pinned to
  the top out of order, so ordering is never relied upon.
* The card's relative age ("3 days ago", "1 months ago") is only an
  ESTIMATE; it is used to skip detail-fetching jobs that are far outside the
  window (with AGO_SKIP_MARGIN_DAYS of safety). The kept/excluded decision
  always uses the exact JSON-LD datePosted when available.
* The card salary line shares its CSS class with the qualification line;
  parsing is by content pattern ("<num> - <num> Monthly"), not by class.

Classification (shared taxonomy, 2026-08-25)
--------------------------------------------
Crawl-side scoping stays source-side: only the four healthcare/pharma
category listings are crawled (that merely saves requests). The keep/drop
and labeling decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the site's own category tags (fw_categories), humanized
                ("regulatory-affairs-job-vacancies" -> "regulatory affairs")
                — a curated role signal for RA jobs with generic titles.
* description = the JSON-LD description, HTML-stripped.

in_scope False (e.g. Staff Nurse — clinical, or non-healthcare noise) ->
the job is DROPPED and counted excluded_out_of_scope in the run summary.
in_scope True fills category ("Non Clinical" | "Public Health"),
sub_category, role_family and the score-trace columns; needs_review True
(in-scope but the title looks like a different profession) keeps the row
AND appends it to needs_review.csv. No local classification keyword lists
exist any more.

Salary (master spec §3: capture, don't filter)
----------------------------------------------
Nothing is excluded for its salary. salary_raw keeps the displayed string;
salary_min_monthly / salary_max_monthly are INR per month (Year ÷ 12);
salary_period_original keeps the source unit. Missing salary =>
"Not Disclosed" with empty numeric fields — never invented.

Outputs
-------
* freshersworld_jobs.csv — rich cumulative store (dedup key: job_id),
  source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/freshersworld.csv — the same jobs mapped to
  the HealthCareers.club 23-column schema.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(7); later runs keep only jobs newer than the newest stored posted_date
minus WATERMARK_GRACE_DAYS of overlap.

Run `python freshersworld_scraper.py --help` for options.
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

SITE = "freshersworld"
SITE_BASE = "https://www.freshersworld.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"

CATEGORY_PATHS = [
    "/jobs/category/health-care-job-vacancies",
    "/jobs/category/pharma-job-vacancies",
    # Added 2026-08-24 (facet-widening pass): the site's category index
    # carries a dedicated Regulatory Affairs category (verified: real RA
    # openings under generic titles like "Manager" / "Executive" at pharma
    # companies — invisible to any title keyword) and a Research category
    # (pharma research associates / scientists). Probed and skipped:
    # analyst-analytics (AR-caller/content noise), govt-sector (empty).
    "/jobs/category/regulatory-affairs-job-vacancies",
    "/jobs/category/research-job-vacancies",
]

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

INITIAL_WINDOW_DAYS = 7        # master spec §4
WATERMARK_GRACE_DAYS = 2
# A card's "N days/months ago" is an estimate; only skip the detail fetch
# when the estimate is outside the window by more than this safety margin.
AGO_SKIP_MARGIN_DAYS = 5

PAGE_SIZE = 20                 # server default; robots only disallows limit=25
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3            # tolerate transient empty pages (spec §7)
MAX_PAGES_PER_CATEGORY = 50    # safety cap; real categories are 1-3 pages
DESCRIPTION_MAX_CHARS = 20_000

RICH_CSV = str(Path(__file__).resolve().parent / "freshersworld_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "seo_title", "company", "location", "city",
    "state", "country", "salary_raw", "salary_min_monthly",
    "salary_max_monthly", "salary_period_original", "job_type",
    "experience_raw", "experience_min_years", "qualification", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in", "needs_review", "fw_categories",
    "posted_date", "valid_through", "description", "job_url", "scraped_at",
]

# ISO alpha-2 (as used in JSON-LD addressCountry) -> (name, dial code).
# Freshersworld is an Indian board; IN is the norm.
COUNTRY_CODES = {
    "IN": ("India", "+91"),
    "AE": ("United Arab Emirates", "+971"),
    "SA": ("Saudi Arabia", "+966"),
    "QA": ("Qatar", "+974"),
    "OM": ("Oman", "+968"),
    "KW": ("Kuwait", "+965"),
    "BH": ("Bahrain", "+973"),
    "SG": ("Singapore", "+65"),
    "DE": ("Germany", "+49"),
    "GB": ("United Kingdom", "+44"),
    "US": ("United States", "+1"),
}

log = logging.getLogger("freshersworld_scraper")

# ----------------------------------------------------------------------------
# Card / field parsing
# ----------------------------------------------------------------------------

# One listing card: the container div carries job_id + job_display_url; the
# card body runs until the next container div (or end of the list markup).
_CARD_RE = re.compile(
    r'<div class="[^"]*\bjob-container\b[^"]*"\s+job_id="(?P<job_id>\d+)"'
    r'\s+job_display_url="(?P<url>[^"]+)"'
    r'(?P<body>.*?)'
    r'(?=<div class="[^"]*\bjob-container\b[^"]*"\s+job_id="|$)',
    re.S)

_SEO_TITLE_RE = re.compile(
    r'class="wrap-title seo_title">(.*?)<span', re.S)
_COMPANY_RE = re.compile(
    r'class="[^"]*company-name[^"]*">\s*([^<]*?)\s*</h3>', re.S)
_LOCATION_LINK_RE = re.compile(
    r'class="job-location[^"]*">(.*?)</span>', re.S)
_EXPERIENCE_RE = re.compile(
    r'class="experience job-details-span"[^>]*>\s*([^<]*?)\s*</span>', re.S)
_QUAL_SPAN_RE = re.compile(
    r'class="qualifications[^"]*"[^>]*>\s*(.*?)\s*</span>', re.S)
_AGO_RE = re.compile(r'class="ago-text">\s*([^<]*?)\s*</span>', re.S)

_TAG_RE = re.compile(r"<[^>]+>")

# "100000 - 150000 Monthly" | "15000 Monthly" | "2.5 - 4 Lakh Yearly"
_CARD_SALARY_RE = re.compile(
    r"^\s*([\d,.]+)\s*(?:-\s*([\d,.]+)\s*)?(Monthly|Yearly|Annual|Lakh)",
    re.IGNORECASE)


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table)[^>]*>", " ",
                  text or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_value(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan", "na", "") else text


def truncate_description(text, limit=DESCRIPTION_MAX_CHARS):
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    if len(spaced) >= limit * 0.5:
        cut = spaced
    return cut.rstrip() + "…"


def parse_ago_days(text):
    """ "3 days ago" -> 3; "1 months ago" -> 30; "5 hours ago" -> 0.

    Returns None when the text is missing or unrecognised — callers must then
    treat the job as in-window (never exclude on a guess).
    """
    text = (text or "").strip().lower()
    match = re.match(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", text)
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2)
    return count * {"minute": 0, "hour": 0, "day": 1,
                    "week": 7, "month": 30, "year": 365}[unit]


def title_from_seo_title(seo_title):
    """ "Physiotherapist Jobs Opening in X at Y, Bangalore" -> "Physiotherapist".

    Fallback for when the detail page's JSON-LD is unavailable.
    """
    seo_title = clean_value(seo_title)
    return re.split(r"\s+Jobs?\s+Opening\s+in\s+", seo_title,
                    flags=re.IGNORECASE)[0].strip() or seo_title


def parse_card_salary(text):
    """Return (salary_raw, min_monthly, max_monthly, period_original).

    "100000 - 150000 Monthly" -> ("100000 - 150000 Monthly", "100000",
                                  "150000", "Monthly")
    Yearly amounts are divided by 12 for the normalized monthly fields
    (spec §3); "Lakh" units multiply by 100,000 first. Anything that doesn't
    look like a salary => ("Not Disclosed", "", "", "").
    """
    text = clean_value(text)
    match = _CARD_SALARY_RE.match(text)
    if not match:
        return ("Not Disclosed", "", "", "")
    lo = float(match.group(1).replace(",", ""))
    hi = float(match.group(2).replace(",", "")) if match.group(2) else lo
    unit = match.group(3).capitalize()
    if hi < lo:
        lo, hi = hi, lo
    if unit == "Lakh":
        # "2.5 - 4 Lakh Yearly" — the Lakh multiplier, then treat as yearly
        lo, hi = lo * 100_000, hi * 100_000
        unit = "Yearly"
    if unit in ("Yearly", "Annual"):
        lo, hi = lo / 12, hi / 12
        period = "Yearly"
    else:
        period = "Monthly"
    return (text, str(int(round(lo))), str(int(round(hi))), period)


def parse_jsonld_salary(base_salary):
    """Return (salary_raw, min_monthly, max_monthly, period_original) from a
    JobPosting baseSalary MonetaryAmount.

    {"currency": "INR", "value": {"minValue": 100000, "maxValue": 150000,
     "unitText": "Month"}} -> ("INR 100000 - 150000 Month", "100000",
                               "150000", "Month")
    Missing/zero amounts => ("Not Disclosed", "", "", "").
    """
    if not isinstance(base_salary, dict):
        return ("Not Disclosed", "", "", "")
    value = base_salary.get("value")
    if not isinstance(value, dict):
        value = {}
    currency = clean_value(base_salary.get("currency")) or "INR"
    unit = clean_value(value.get("unitText")) or "Month"

    def as_float(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    lo = as_float(value.get("minValue"))
    hi = as_float(value.get("maxValue")) or as_float(value.get("value"))
    if lo is None and hi is None:
        return ("Not Disclosed", "", "", "")
    if lo is None:
        lo = hi
    if hi is None:
        hi = lo
    if not lo and not hi:
        return ("Not Disclosed", "", "", "")
    if hi < lo:
        lo, hi = hi, lo
    raw = "{} {} - {} {}".format(currency, int(round(lo)), int(round(hi)), unit)
    factor = 1 / 12 if unit.lower() in ("year", "yearly", "annual") else 1
    return (raw, str(int(round(lo * factor))), str(int(round(hi * factor))),
            unit)


def parse_iso_date(text):
    """ "2026-07-10T00:00:00+05:30" -> "2026-07-10"; "" when unparseable."""
    text = clean_value(text)
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else ""


def parse_experience_years(exp_req, card_text=""):
    """monthsOfExperience -> whole years; falls back to the card's "0 Years"."""
    if isinstance(exp_req, dict):
        try:
            months = float(exp_req.get("monthsOfExperience"))
            return str(int(months // 12))
        except (TypeError, ValueError):
            pass
    match = re.match(r"([\d.]+)\s*Year", clean_value(card_text), re.IGNORECASE)
    if match:
        return str(int(float(match.group(1))))
    return ""


def parse_cards(page_html):
    """Yield one dict per listing card on a category page."""
    for match in _CARD_RE.finditer(page_html):
        body = match.group("body")

        def first(regex, default=""):
            found = regex.search(body)
            return strip_html(found.group(1)) if found else default

        # The salary line shares the "qualifications" class with the actual
        # qualification line — tell them apart by content.
        salary_text, qualification = "", ""
        for span in _QUAL_SPAN_RE.findall(body):
            text = strip_html(span)
            if _CARD_SALARY_RE.match(text):
                salary_text = text
            elif text:
                qualification = text

        yield {
            "job_id": match.group("job_id"),
            "job_url": match.group("url"),
            "seo_title": first(_SEO_TITLE_RE),
            "company": first(_COMPANY_RE),
            "location": first(_LOCATION_LINK_RE),
            "experience_raw": first(_EXPERIENCE_RE),
            "salary_text": salary_text,
            "qualification": qualification,
            "ago_text": first(_AGO_RE),
        }


def extract_jobposting_jsonld(page_html):
    """Return the JobPosting JSON-LD dict from a detail page, or None."""
    for match in re.finditer(
            r'<script type="application/ld\+json">(.*?)</script>',
            page_html, re.S):
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


# ----------------------------------------------------------------------------
# Classification — the ONE shared two-level taxonomy
# ----------------------------------------------------------------------------

def humanize_fw_categories(fw_categories):
    """"regulatory-affairs-job-vacancies; pharma-job-vacancies" ->
    "regulatory affairs, pharma".

    The site's own category slugs are the only curated role signal
    Freshersworld offers, so they are passed to classify_job as `skills`
    (they never decide the category — the raw value stays in the rich CSV's
    fw_categories source column).
    """
    parts = []
    for slug in str(fw_categories or "").split(";"):
        slug = slug.strip()
        if not slug:
            continue
        slug = re.sub(r"-job-vacancies$", "", slug)
        parts.append(slug.replace("-", " "))
    return ", ".join(parts)


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           humanize_fw_categories(row.get("fw_categories", "")),
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


# company_type (hospital|pharma) is a separate club field, NOT a category.
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None, since=None):
    """First run: last INITIAL_WINDOW_DAYS. Later: watermark minus grace."""
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer (master spec §7)
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return rp
    probes = [SITE_BASE + path for path in CATEGORY_PATHS]
    probes.append(SITE_BASE + CATEGORY_PATHS[0]
                  + "?limit={}&offset=20".format(PAGE_SIZE))
    probes.append(SITE_BASE + "/jobs/sample-job-opening-1234567")
    for url in probes:
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")
    return rp


def fetch_html(session, url, params=None):
    """GET one page with retries/backoff. Returns HTML text or None."""
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
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)",
              url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(card, posting, fw_categories, today=None):
    """Merge a listing card and its detail-page JSON-LD into a rich row.

    `posting` may be None (detail fetch failed) — the card alone still makes
    a valid row, with posted_date estimated from the card's ago-text.
    """
    posting = posting or {}
    today = today or date.today()

    title = clean_value(posting.get("title")) or \
        title_from_seo_title(card.get("seo_title"))

    posted_date = parse_iso_date(posting.get("datePosted"))
    if not posted_date:
        ago_days = parse_ago_days(card.get("ago_text"))
        if ago_days is not None:
            posted_date = (today - timedelta(days=ago_days)).isoformat()

    if posting.get("baseSalary"):
        salary = parse_jsonld_salary(posting.get("baseSalary"))
    else:
        salary = parse_card_salary(card.get("salary_text"))
    salary_raw, sal_min, sal_max, sal_period = salary

    city = state = country_code = ""
    locations = posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    localities = []
    for place in locations:
        address = place.get("address", {}) if isinstance(place, dict) else {}
        locality = clean_value(address.get("addressLocality"))
        if locality:
            localities.append(locality)
        if not state:
            state = clean_value(address.get("addressRegion"))
        if not country_code:
            country_code = clean_value(address.get("addressCountry"))
    city = localities[0] if localities else \
        clean_value(card.get("location")).split(",")[0].strip()
    location = "; ".join(localities) or clean_value(card.get("location"))

    description = truncate_description(
        strip_html(posting.get("description") or ""))

    employment = clean_value(posting.get("employmentType"))

    org = posting.get("hiringOrganization")
    company = clean_value(org.get("name")) if isinstance(org, dict) else ""
    company = company or clean_value(card.get("company"))

    return {
        "source": SITE,
        "job_id": card["job_id"],
        "title": title,
        "seo_title": clean_value(card.get("seo_title")),
        "company": company,
        "location": location,
        "city": city,
        "state": state,
        "country": country_code or "IN",
        "salary_raw": salary_raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": sal_period,
        "job_type": employment or "FULL_TIME",
        "experience_raw": clean_value(card.get("experience_raw")),
        "experience_min_years": parse_experience_years(
            posting.get("experienceRequirements"), card.get("experience_raw")),
        "qualification": clean_value(posting.get("qualifications")) or
                         clean_value(card.get("qualification")),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "fw_categories": "; ".join(sorted(fw_categories)),
        "posted_date": posted_date,
        "valid_through": parse_iso_date(posting.get("validThrough")),
        "description": description,
        "job_url": card["job_url"],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the 23-column club schema.

    Salary re-derives period from salary_period_original so exported amounts
    are the ORIGINAL figures. Yearly amounts are re-read from salary_raw
    (not un-normalized from the ÷12 monthly fields, which would round:
    100000/12*12 -> 99996).
    """
    period_orig = _clean(r.get("salary_period_original")).lower()
    lo = _clean(r.get("salary_min_monthly"))
    hi = _clean(r.get("salary_max_monthly")) or lo
    if lo and period_orig in ("year", "yearly", "annual"):
        numbers = re.findall(r"[\d,]+(?:\.\d+)?",
                             _clean(r.get("salary_raw")))
        numbers = [n.replace(",", "") for n in numbers]
        if numbers:
            lo = str(int(round(float(numbers[0]))))
            hi = str(int(round(float(numbers[1] if len(numbers) > 1
                                     else numbers[0]))))
        else:
            lo = str(int(round(float(lo) * 12)))
            hi = str(int(round(float(hi) * 12)))
        period = "per_annum"
    elif lo:
        period = "per_month"
    else:
        period = ""

    employment = _clean(r.get("job_type")).upper()
    job_type = "part_time" if "PART_TIME" in employment else "full_time"

    code = _clean(r.get("country")) or "IN"
    name, dial = COUNTRY_CODES.get(code, ("", ""))

    return {
        "country_name": name or code,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": _clean(r.get("city")) or "India",
        "company_name": _clean(r.get("company")),
        "company_type": classify_company_type(_clean(r.get("company"))),
        "company_logo": "",
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": job_type,
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": _clean(r.get("experience_min_years")),
        "max_experience": "",
        # the site's structured qualification field first (JSON-LD
        # `qualifications` / the card's qualification line), else grounded
        # extraction from the description — never inferred
        "qualification": (_clean(r.get("qualification"))
                          or extract_qualification(_clean(r.get("description")))),
        "min_salary": lo,
        "max_salary": hi,
        "salary_period": period,
        "salary_currency": "INR" if lo else "",
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
    club_rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(club_rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def crawl_categories(session, max_pages):
    """Walk every category listing; return {job_id: (card, {category slugs})}.

    The two categories overlap heavily, so cards are merged by job_id with
    the set of category slugs each job appeared under.
    """
    candidates = {}
    slots = 0
    for path in CATEGORY_PATHS:
        slug = path.rsplit("/", 1)[-1]
        empty_pages = 0
        page_cap = max_pages if max_pages is not None else MAX_PAGES_PER_CATEGORY
        for page_no in range(page_cap):
            offset = page_no * PAGE_SIZE
            url = SITE_BASE + path
            params = {"limit": PAGE_SIZE, "offset": offset} if offset else None
            page_html = fetch_html(session, url, params=params)
            if page_html is None:
                # The site intermittently 500s/times out; treat a failed page
                # like an empty one and keep walking — breaking here once hid
                # 475 in-window jobs sitting on the deeper pages (spec §7:
                # stop only after MAX_EMPTY_PAGES consecutive misses).
                log.error("Category %s offset %d failed — skipping page", slug, offset)
                empty_pages += 1
                if empty_pages >= MAX_EMPTY_PAGES:
                    break
                continue
            cards = list(parse_cards(page_html))
            log.info("%s offset %d: %d cards", slug, offset, len(cards))
            if not cards:
                empty_pages += 1
                if empty_pages >= MAX_EMPTY_PAGES:
                    break
                continue
            empty_pages = 0
            for card in cards:
                slots += 1
                entry = candidates.setdefault(card["job_id"], (card, set()))
                entry[1].add(slug)
            if len(cards) < PAGE_SIZE:   # short page == last page
                break
    return candidates, slots


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from freshersworld.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages per category (test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (test runs)")
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
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s%s",
             len(known_ids), cutoff, " (--since override)" if args.since else "")

    candidates, slots = crawl_categories(session, args.max_pages)
    log.info("Categories yielded %d unique jobs across %d card slots",
             len(candidates), slots)

    counters = {"scanned": 0, "excluded_out_of_scope": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    counters["duplicates"] += slots - len(candidates)  # cross-category repeats

    today = date.today()
    cutoff_date = date.fromisoformat(cutoff)
    new_rows, review_log = [], []

    for job_id, (card, slugs) in candidates.items():
        counters["scanned"] += 1
        try:
            if job_id in known_ids:
                counters["duplicates"] += 1
                continue

            # Ago-estimate pre-filter: skip the detail fetch only when the
            # card is outside the window by a safe margin; borderline ages
            # are resolved with the exact JSON-LD datePosted below.
            ago_days = parse_ago_days(card.get("ago_text"))
            if ago_days is not None:
                estimated = today - timedelta(days=ago_days)
                if estimated < cutoff_date - timedelta(days=AGO_SKIP_MARGIN_DAYS):
                    counters["excluded_old"] += 1
                    continue

            detail_html = fetch_html(session, card["job_url"])
            posting = extract_jobposting_jsonld(detail_html) if detail_html else None
            if posting is None:
                log.warning("No JobPosting JSON-LD for %s — using card data only",
                            card["job_url"])

            row = build_row(card, posting, slugs, today=today)

            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", job_id, exc)
            continue

        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            continue

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "company": row["company"],
                               "fw_categories": row["fw_categories"]})
        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1
        if args.limit is not None and counters["new"] >= args.limit:
            break

    # ---- write rich cumulative CSV (source of truth) ----
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

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    print("\n===== Run summary =====")
    print("Unique jobs scanned:       {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):   {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
