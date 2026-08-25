#!/usr/bin/env python3
"""Scrape healthcare job listings from jobberman.com (Nigeria).

Data source
-----------
Jobberman Nigeria's healthcare vertical is a server-rendered listing at

    https://www.jobberman.com/jobs/healthcare            (16 cards/page)
    https://www.jobberman.com/jobs/healthcare?page=N     (N = 2..10)

so every job is healthcare-industry at the source (§2 of the master spec).

robots.txt is restrictive and binding (§1):
* /api/ and /ajax/ are disallowed — no JSON endpoints are called.
* /job/ is disallowed, but detail pages live under /listings/<slug>, which
  is allowed.
* `Disallow: /*page=*` with explicit Allows for page=2..10 only — so a run
  never requests beyond page 10 (MAX_PAGES is hard-coded to 10; Python's
  robotparser doesn't understand the wildcard rules, so the cap is enforced
  in code, not left to the parser).

Listing cards carry: title, detail URL (slug = dedup key), internal numeric
id, company, location/job-type/salary chips and the site's function
category, plus a coarse relative age ("4 days ago", "2 weeks ago").

Detail pages embed a schema.org @graph whose JobPosting node has the exact
datePosted, validThrough, employmentType, occupationalCategory, industry,
full HTML description, baseSalary (NGN, usually monthly; often absent /
"Confidential") and experienceRequirements.monthsOfExperience. The employer
name and address live in sibling Organization / PostalAddress nodes reached
via @id references (spec source preference #2).

Quirks
------
* The listing is date-sorted newest-first with a few FEATURED cards
  injected at the top, so pagination stops once a whole page is older than
  the cutoff. Card ages are coarse ("1 month ago"), so a card is only
  skipped as old when even its newest possible date is before the cutoff;
  exact dates come from the detail page for everything else.
* posted_date exact value exists ONLY on detail pages; out-of-window jobs
  are recorded in seen_old_ids.csv so later runs skip their detail fetch.
* Salaries are NGN. The club schema's salary_currency enum only allows
  INR/USD, so amounts are kept in the rich CSV but the club CSV's salary
  columns are left blank (never converted, never invented).
* The healthcare vertical is a SECTOR facet, not a role facet: it is full of
  back-office jobs (accountants, drivers, sales reps) and of bedside clinical
  roles, and neither is in scope. The facet therefore only scopes the crawl;
  the shared classifier makes the keep/drop call and most cards are dropped
  as excluded_out_of_scope.
* PostalAddress fields are shuffled (streetAddress holds the state, e.g.
  "Lagos"); the card's location chip is the cleaner city value and wins.

Outputs
-------
* jobberman_jobs.csv                          — rich cumulative store
  (dedup key: listing slug)
* ../../jobs_csv/<DD-MM-YYYY>/jobberman.csv   — HealthCareers.club 22-col
  schema
* seen_old_ids.csv                            — out-of-window slugs (skip list)
* needs_review.csv                            — kept but flagged
* out-of-scope.csv                            — rows the taxonomy dropped

Time window (master spec §4): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

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

# The one shared classifier (see ../../instructions/taxonomy-migration-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "jobberman"
SITE_BASE = "https://www.jobberman.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
LISTING_URL = SITE_BASE + "/jobs/healthcare"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec §3): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 16
MAX_PAGES = 10          # robots.txt only allows ?page=2..10 (+ page 1)
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 2
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "jobberman_jobs.csv"
SEEN_OLD_CSV = "seen_old_ids.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COUNTRY_NAME = "Nigeria"
COUNTRY_CODE = "NG"
COUNTRY_DIAL = "+234"

RICH_COLUMNS = [
    "source", "job_id", "internal_id", "title", "company",
    "city", "state", "country", "country_code", "country_dial_code",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "salary_currency", "job_type", "site_job_type", "site_function",
    "site_industry", "qualification_level", "min_experience_years",
    "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in",
    "company_type", "needs_review", "posted_date",
    "expires_at", "description", "job_url", "scraped_at",
]

OUT_OF_SCOPE_CSV = "out-of-scope.csv"

log = logging.getLogger("jobberman_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


# ---- listing cards ----------------------------------------------------------

_CARD_SPLIT_RE = re.compile(r'data-cy="listing-cards-components"')
_CARD_ID_RE = re.compile(r'aria-labelledby="job-(\d+)-title"')
_CARD_LINK_RE = re.compile(
    r'<a\s+href="(https://www\.jobberman\.com/listings/([^"?#]+))"'
    r'[^>]*data-cy="listing-title-link"[^>]*title="([^"]*)"', re.S)
_CARD_COMPANY_RE = re.compile(
    r'<p class="text-sm text-blue-700[^"]*">\s*(.*?)\s*</p>', re.S)
_CARD_CHIP_RE = re.compile(
    r'bg-brand-secondary-100[^>]*>(.*?)</span>', re.S)
_CARD_FUNCTION_RE = re.compile(
    r'<p class="text-sm text-gray-500 text-loading-animate[^"]*">'
    r'\s*(.*?)\s*</p>', re.S)
_CARD_AGE_RE = re.compile(
    r'>\s*(\d+\+?\s+(?:second|minute|hour|day|week|month|year)s?\s+ago'
    r'|Today|Yesterday)\s*<')

_JOB_TYPE_CHIP_RE = re.compile(
    r"full[ -]?time|part[ -]?time|contract|internship|graduate|temporary|"
    r"volunteer|remote|hybrid", re.IGNORECASE)
_SALARY_CHIP_RE = re.compile(r"\d|confidential|negotiable", re.IGNORECASE)


def split_chips(chips):
    """Classify a card's chip texts into (location, job_type, salary)."""
    location, job_type, salary = "", "", ""
    for chip in chips:
        if not chip:
            continue
        if not job_type and _JOB_TYPE_CHIP_RE.search(chip):
            job_type = chip
        elif not salary and _SALARY_CHIP_RE.search(chip):
            salary = chip
        elif not location:
            location = chip
    return location, job_type, salary


def parse_listing_cards(page_html):
    """All job cards on a listing page -> list of dicts (may be empty)."""
    cards = []
    for chunk in _CARD_SPLIT_RE.split(page_html or "")[1:]:
        link = _CARD_LINK_RE.search(chunk)
        if not link:
            continue
        url, slug, title = link.groups()
        internal = _CARD_ID_RE.search(chunk)
        company = _CARD_COMPANY_RE.search(chunk)
        chips = [strip_html(c) for c in _CARD_CHIP_RE.findall(chunk)]
        location, chip_job_type, chip_salary = split_chips(chips)
        function = _CARD_FUNCTION_RE.search(chunk)
        age = _CARD_AGE_RE.search(chunk)
        cards.append({
            "job_id": slug.strip("/"),
            "internal_id": internal.group(1) if internal else "",
            "title": clean_text(title),
            "job_url": url,
            "company": strip_html(company.group(1)) if company else "",
            "location": location,
            "chip_job_type": chip_job_type,
            "chip_salary": chip_salary,
            "function": strip_html(function.group(1)) if function else "",
            "age_text": age.group(1) if age else "",
        })
    return cards


# ---- relative card ages -----------------------------------------------------

_AGE_RE = re.compile(r"(\d+)\+?\s+(second|minute|hour|day|week|month|year)s?",
                     re.IGNORECASE)

# Newest possible age in days for each displayed unit ("1 month ago" can
# mean anything from ~28 days up, so 28 is the optimistic bound).
_AGE_UNIT_MIN_DAYS = {"second": 0, "minute": 0, "hour": 0,
                      "day": 1, "week": 7, "month": 28, "year": 365}


def newest_possible_date(age_text, today=None):
    """Card age -> the NEWEST date it could stand for (ISO), or "".

    Used only to skip/stop early: a card is treated as old solely when even
    this optimistic date is before the cutoff. Unknown formats return ""
    (never skipped on age alone).
    """
    today = today or date.today()
    text = clean_text(age_text)
    if not text:
        return ""
    if text.lower() == "today":
        return today.isoformat()
    if text.lower() == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    m = _AGE_RE.search(text)
    if not m:
        return ""
    days = int(m.group(1)) * _AGE_UNIT_MIN_DAYS[m.group(2).lower()]
    return (today - timedelta(days=days)).isoformat()


# ---- detail-page JSON-LD @graph ---------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S)
_LAX_DECODER = json.JSONDecoder(strict=False)  # payload has raw control chars


def parse_graph(page_html):
    """Detail page -> (JobPosting node, {@id: node}) or ({}, {})."""
    for m in _LDJSON_RE.finditer(page_html or ""):
        try:
            data, _ = _LAX_DECODER.raw_decode(m.group(1).strip())
        except ValueError as exc:
            log.debug("Unparseable ld+json block: %s", exc)
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        by_id, posting = {}, {}
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            if node.get("@id"):
                by_id[node["@id"]] = node
            if node.get("@type") == "JobPosting":
                posting = node
        if posting:
            return posting, by_id
    return {}, {}


def resolve(node, by_id):
    """Follow an @id reference, merging the referenced node over the stub."""
    if not isinstance(node, dict):
        return {}
    target = by_id.get(node.get("@id"), {})
    return {**node, **target} if target else node


def company_from_graph(posting, by_id):
    org = resolve(posting.get("hiringOrganization") or {}, by_id)
    return clean_text(org.get("name") or org.get("legalName"))


def address_from_graph(posting, by_id):
    place = resolve(posting.get("jobLocation") or {}, by_id)
    return resolve(place.get("address") or {}, by_id)


def parse_base_salary(posting):
    """JobPosting.baseSalary -> salary fields; {} when undisclosed.

    Real example (listing-1244751): currency NGN, minValue 250000,
    maxValue 400000, unitText "MONTH". Confidential listings carry no
    baseSalary at all (never invented).
    """
    base = posting.get("baseSalary") or {}
    if not isinstance(base, dict):
        return {}
    value = base.get("value") or {}

    def to_int(v):
        try:
            n = int(round(float(v)))
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    lo = to_int(value.get("minValue"))
    hi = to_int(value.get("maxValue"))
    single = to_int(value.get("value"))
    if lo is None and hi is None:
        lo = hi = single
    if lo is None and hi is None:
        return {}
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if hi < lo:
        lo, hi = hi, lo
    currency = clean_text(base.get("currency")) or "NGN"
    period = ("per_annum"
              if clean_text(value.get("unitText")).upper() == "YEAR"
              else "per_month")
    unit = "year" if period == "per_annum" else "month"
    return {
        "salary_raw": "{} {:,} - {:,} per {}".format(currency, lo, hi, unit),
        "salary_min": lo, "salary_max": hi,
        "salary_period": period, "salary_currency": currency,
    }


def parse_experience_years(posting):
    """experienceRequirements.monthsOfExperience -> whole years, or ""."""
    req = posting.get("experienceRequirements") or {}
    if not isinstance(req, dict):
        return ""
    try:
        months = float(req.get("monthsOfExperience"))
    except (TypeError, ValueError):
        return ""
    return str(int(months // 12)) if months >= 0 else ""


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The site's own occupationalCategory (site_function) and industry
    (site_industry) are the curated `skills` signal; both stay in the rich
    CSV as raw source columns only. Returns in_scope — False means DROP the
    row (excluded_out_of_scope).
    """
    skills = " , ".join(v for v in (row.get("site_function", ""),
                                    row.get("site_industry", "")) if v)
    verdict = classify_job(row.get("title", ""), skills,
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


_PHARMA_RE = re.compile(
    r"pharma|life ?science|biotech|\bcro\b|clinical research|laborator|"
    r"diagnost|med.?tech|medical device|vaccin|chemist", re.IGNORECASE)


def classify_company_type(title, company, function):
    """Club enum hospital|pharma, matched on text."""
    haystack = " ".join(filter(None, [title, company, function]))
    return "pharma" if _PHARMA_RE.search(haystack) else "hospital"


_PART_TIME_RE = re.compile(r"part[ -_]?time", re.IGNORECASE)
_REMOTE_RE = re.compile(r"remote", re.IGNORECASE)


def job_type_from(chip_job_type, employment_type):
    """Site job type -> club enum full_time|part_time|remote|hybrid.

    Jobberman also lists Contract / Internship & Graduate roles; the club
    enum has no such values, so they fall back to full_time (the site's
    original label is kept in the rich CSV's site_job_type column).
    """
    text = " ".join([chip_job_type or "", employment_type or ""])
    if _PART_TIME_RE.search(text):
        return "part_time"
    if _REMOTE_RE.search(text):
        return "remote"
    return "full_time"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def check_robots(session):
    """Abort if robots.txt disallows the listing or detail paths.

    Python's robotparser doesn't understand Jobberman's wildcard rules
    (Disallow: /*page=*), so the page cap is enforced by MAX_PAGES=10 —
    only ?page=2..10 are explicitly allowed.
    """
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LISTING_URL, SITE_BASE + "/listings/example-slug"):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed (pagination capped at page %d)", MAX_PAGES)


def _request(session, url, params=None):
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
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(card, posting, by_id):
    """Rich row from a listing card + its detail JobPosting @graph."""
    title = card["title"] or clean_text(posting.get("title"))
    company = company_from_graph(posting, by_id) or card["company"]
    function = (clean_text(posting.get("occupationalCategory"))
                or card["function"])

    address = address_from_graph(posting, by_id)
    # PostalAddress fields are shuffled (streetAddress holds the state);
    # the card's location chip is the cleaner value.
    city = card["location"] or clean_text(address.get("streetAddress"))
    if city.lower() in ("nigeria", "remote", ""):
        city = ""

    row = {
        "source": SITE,
        "job_id": card["job_id"],
        "internal_id": card["internal_id"],
        "title": title,
        "company": company,
        "city": city,
        "state": clean_text(address.get("streetAddress")),
        "country": COUNTRY_NAME,
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": job_type_from(card["chip_job_type"],
                                  posting.get("employmentType")),
        "site_job_type": card["chip_job_type"]
                         or clean_text(posting.get("employmentType")),
        "site_function": function,
        "site_industry": clean_text(posting.get("industry")),
        "qualification_level": clean_text(posting.get("qualifications")),
        "min_experience_years": parse_experience_years(posting),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": classify_company_type(title, company, function),
        "needs_review": False,
        "posted_date": clean_text(posting.get("datePosted"))[:10],
        "expires_at": clean_text(posting.get("validThrough"))[:10],
        "description": strip_html(posting.get("description")
                                  or "")[:DESCRIPTION_MAX_CHARS],
        "job_url": card["job_url"],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    row.update(parse_base_salary(posting))
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


def rich_row_to_club_row(r):
    # Club enum only allows INR/USD; Jobberman pays NGN, so club salary
    # columns stay blank (the rich CSV keeps the NGN amounts).
    min_sal = _int_str(r.get("salary_min"))
    currency = _blank(r.get("salary_currency"))
    has_salary = bool(min_sal) and currency in ("INR", "USD")
    return {
        "country_name": _blank(r.get("country")) or COUNTRY_NAME,
        "country_code": _blank(r.get("country_code")) or COUNTRY_CODE,
        "country_dial_code": _blank(r.get("country_dial_code")) or COUNTRY_DIAL,
        "city_name": (_blank(r.get("city")) or _blank(r.get("state"))
                      or COUNTRY_NAME),
        "company_name": _blank(r.get("company")),
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience_years")),
        "max_experience": "",
        # the JobPosting's own `qualifications` field first, else grounded
        # extraction from the description — never inferred
        "qualification": (_blank(r.get("qualification_level"))
                          or extract_qualification(_blank(r.get("description")))),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def load_seen_old(path):
    df = load_existing(path)
    if df is None or "job_id" not in df.columns:
        return set()
    return set(df["job_id"].dropna())


def load_out_of_scope_ids(path):
    """job_ids already judged out of scope, so their detail page is not
    re-fetched every run.

    The gate can only run after the detail fetch (the description lives
    there), so without this skip list a vertical that is mostly out of scope
    would re-fetch every dropped card on every run. Rows are kept in full,
    so widening the taxonomy can recover them.
    """
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
    df.to_csv(path, index=False)
    return len(df)


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
        description="Scrape healthcare jobs from jobberman.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, metavar="N",
                        help="stop after N listing pages, capped at %d by "
                             "robots.txt (default: %d)" % (MAX_PAGES, MAX_PAGES))
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
    seen_old = load_seen_old(SEEN_OLD_CSV)
    out_of_scope_ids = load_out_of_scope_ids(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs (%d known-old skipped); "
             "keeping jobs posted on/after %s",
             len(known_ids), len(seen_old), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "skipped_out_of_scope": 0, "needs_review": 0, "new": 0,
                "duplicates": 0, "detail_failed": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    empty_pages = 0
    max_pages = min(args.max_pages, MAX_PAGES)

    for page in range(1, max_pages + 1):
        page_html = _request(session, LISTING_URL,
                             params={"page": page} if page > 1 else None)
        cards = parse_listing_cards(page_html) if page_html else None
        if not cards:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        empty_pages = 0
        page_all_old = True

        for card in cards:
            counters["scanned"] += 1
            optimistic = newest_possible_date(card["age_text"])
            card_surely_old = bool(optimistic) and optimistic < cutoff
            if not card_surely_old:
                page_all_old = False
            if card["job_id"] in out_of_scope_ids:
                counters["skipped_out_of_scope"] += 1
                continue
            if card["job_id"] in known_ids or card["job_id"] in seen_old:
                counters["duplicates"] += 1
                continue
            if card_surely_old:
                counters["excluded_old"] += 1
                seen_old.add(card["job_id"])
                new_seen_old.append({"job_id": card["job_id"],
                                     "posted_date": optimistic + " (approx)"})
                continue
            detail_html = _request(session, card["job_url"])
            posting, by_id = parse_graph(detail_html or "")
            if not posting:
                counters["detail_failed"] += 1
                log.warning("No JobPosting JSON-LD for %s", card["job_url"])
            try:
                row = build_row(card, posting, by_id)
            except Exception as exc:
                log.warning("Skipping malformed job %s: %s", card["job_id"], exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                seen_old.add(row["job_id"])
                new_seen_old.append({"job_id": row["job_id"],
                                     "posted_date": row["posted_date"]})
                continue
            # The healthcare vertical is a sector facet; the classifier is
            # what decides whether the role itself is in scope.
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                out_of_scope_ids.add(row["job_id"])
                dropped_rows.append(row)
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "company": row["company"],
                                   "site_function": row["site_function"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        # Listing is newest-first (after a few featured cards): once a whole
        # page is certainly older than the cutoff, later pages are too.
        if page_all_old:
            log.info("Page %d entirely older than %s — stopping", page, cutoff)
            break
        if len(cards) < PAGE_SIZE:
            break

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

    # ---- sidecars ----
    if new_seen_old:
        old_df = pd.DataFrame(sorted(new_seen_old, key=lambda d: d["job_id"]))
        try:
            prev = pd.read_csv(SEEN_OLD_CSV, dtype=str)
            old_df = pd.concat([prev, old_df], ignore_index=True)
        except FileNotFoundError:
            pass
        old_df.drop_duplicates(subset="job_id").to_csv(SEEN_OLD_CSV, index=False)
    if dropped_rows:
        total = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (out of scope): {:>3,}".format(counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>3,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    print("Detail parse failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
