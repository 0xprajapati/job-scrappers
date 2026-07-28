#!/usr/bin/env python3
"""Scrape healthcare job listings from internshala.com/jobs/.

Data source
-----------
internshala.com is a general jobs/internships portal. robots.txt (checked at
startup) disallows `/api/`, `/job/search/`, `/job/details/` and any URL with
a query string for generic agents, so this scraper uses only what the site
exposes to crawlers:

1. Server-rendered category listing pages, one per healthcare-related
   profile — `https://internshala.com/jobs/<category>-jobs/` paginated as
   `/jobs/<category>-jobs/page-N/` (50 cards on page 1, 40 after; an
   out-of-range page returns 200 with zero cards; a category with no live
   jobs 301-redirects back to `/jobs/`, which is detected and skipped so an
   empty category can never trigger a full-site crawl).
2. Detail pages `/job/detail/<slug>` (allowed — robots only blocks the
   plural `/job/details/`) embed a schema.org JobPosting JSON-LD with the
   authoritative fields: exact datePosted, full description, baseSalary
   (INR, YEAR/MONTH), employmentType, jobLocation, validThrough, skills.
   Details are fetched for NEW in-window jobs only (`--no-details` skips).

Healthcare filter (at the source): only the category pages listed in
CORE_CATEGORIES / AMBIGUOUS_CATEGORIES are crawled. Jobs from ambiguous
categories (psychology, biotech, …) are KEPT and flagged needs_review when
the title carries no healthcare keyword — never silently dropped.

Quirks
------
* Listing cards show posted age as coarse text ("Today", "Few hours ago",
  "3 weeks ago"). That approximation only pre-filters obviously-old jobs
  (with AGO_SLACK_DAYS of slack); the exact JSON-LD datePosted from the
  detail page makes the final in-window decision.
* Card salaries are annual CTC ("₹ 2,00,000 - 3,00,000"); occasionally a
  foreign symbol (£) appears — captured raw, amounts kept only for INR/USD.
* Listings are only roughly recency-ordered, so every page of each category
  is crawled (categories are small, ≤ ~4 pages) instead of early-stopping.

Outputs
-------
* internshala_jobs.csv                        — rich cumulative store (dedup: job_id)
* ../../jobs_csv/<DD-MM-YYYY>/internshala.csv — HealthCareers.club 22-col schema
* needs_review.csv                            — flagged titles from this run

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored date minus
WATERMARK_GRACE_DAYS of overlap.

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

SITE = "internshala"
SITE_BASE = "https://internshala.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
LISTING_URL_TMPL = SITE_BASE + "/jobs/{}/"
LISTING_PAGE_TMPL = SITE_BASE + "/jobs/{}/page-{}/"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2
# card "ago" text is coarse (weeks); only pre-skip detail fetches when the
# approximate date is older than the cutoff by more than this slack.
AGO_SLACK_DAYS = 7

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_PAGES_PER_CATEGORY = 40
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "internshala_jobs.csv"
REVIEW_CSV = "needs_review.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Category slugs under /jobs/<slug>/ that are healthcare at the source.
CORE_CATEGORIES = [
    "hospitals-healthcare-jobs",
    "medical-jobs",
    "medicine-jobs",
    "nurse-jobs",
    "pharmacist-jobs",
    "pharma-jobs",
    "pharmaceutical-jobs",
    "dietetics-nutrition-jobs",
]

# Adjacent categories: crawled, but titles without a healthcare keyword are
# flagged needs_review (kept, never dropped — master spec §2).
AMBIGUOUS_CATEGORIES = [
    "psychology-jobs",
    "biotechnology-jobs",
    "biotech-jobs",
    "bioinformatics-jobs",
    "biology-jobs",
]

ALLOW_TITLE_RE = re.compile(
    r"medical|pharma|health|nurs|doctor|clinic|hospital|\blab\b|diagnost|"
    r"patient|dental|surgi|therap|physio|radiol|patholog|ayurved|wellness|"
    r"\bmr\b|life ?science|nutrition|dieti|psycholog|counsell?or|biotech|"
    r"phlebotom|optometr|paramedic|veterinar", re.IGNORECASE)

# ISO country code (from JSON-LD addressCountry) -> (name, dial code).
COUNTRY_META = {
    "IN": ("India", "+91"),
    "US": ("United States", "+1"),
    "GB": ("United Kingdom", "+44"),
    "AE": ("United Arab Emirates", "+971"),
    "SG": ("Singapore", "+65"),
    "AU": ("Australia", "+61"),
    "CA": ("Canada", "+1"),
}

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "state", "country",
    "country_code", "country_dial_code", "salary_raw", "salary_min",
    "salary_max", "salary_period", "salary_currency", "job_type",
    "employment_type_raw", "experience_min_years", "experience_max_years",
    "site_categories", "category", "company_type", "industry", "skills",
    "needs_review", "posted_date", "posted_date_is_estimate",
    "valid_through", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("internshala_scraper")

# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def strip_html(markup):
    markup = re.sub(r"<br\s*/?>", "\n", markup or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", markup)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


# ----------------------------------------------------------------------------
# Posted-age parsing ("Today", "Few hours ago", "3 weeks ago", ...)
# ----------------------------------------------------------------------------

_AGO_RE = re.compile(r"(\d+)\s*(hour|day|week|month)", re.IGNORECASE)


def ago_to_date(ago_text, today=None):
    """Approximate posted date from a card's age string; '' if unparseable."""
    today = today or date.today()
    text = clean_text(ago_text).lower()
    if not text:
        return ""
    if "just now" in text or "today" in text or "few hours" in text:
        return today.isoformat()
    m = _AGO_RE.search(text)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2).lower()
    days = {"hour": 0, "day": n, "week": n * 7, "month": n * 30}[unit]
    if unit == "hour":
        days = 1 if n >= 24 else 0
    return (today - timedelta(days=days)).isoformat()


# ----------------------------------------------------------------------------
# Salary parsing
# ----------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FOREIGN_CUR_RE = re.compile(r"[£€]|\b(AED|SAR|QAR|GBP|EUR)\b")


def parse_card_salary(raw):
    """Parse a listing-card salary like "₹ 2,00,000 - 3,00,000".

    Card amounts are annual CTC. "Competitive salary" / empty -> no amounts
    (never invents values). Foreign currencies (£/€/...) keep the raw string
    only; the club schema accepts INR/USD alone.
    """
    text = clean_text(raw)
    if not text:
        return {}
    numbers = [float(n.replace(",", "")) for n in _NUM_RE.findall(text)]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return {"salary_raw": text[:120]}
    if _FOREIGN_CUR_RE.search(text):
        return {"salary_raw": text[:120]}
    currency = "USD" if ("$" in text or "USD" in text.upper()) else "INR"
    lo, hi = numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]
    if hi < lo:
        lo, hi = hi, lo
    return {"salary_raw": text[:120], "salary_min": int(round(lo)),
            "salary_max": int(round(hi)), "salary_period": "per_annum",
            "salary_currency": currency}


def parse_ld_salary(base_salary):
    """Parse a JSON-LD baseSalary MonetaryAmount into rich-row fields."""
    if not isinstance(base_salary, dict):
        return {}
    value = base_salary.get("value")
    if not isinstance(value, dict):
        return {}
    lo, hi = value.get("minValue"), value.get("maxValue")
    if lo is None and hi is None:
        lo = hi = value.get("value")
    if lo is None and hi is None:
        return {}
    lo = float(lo if lo is not None else hi)
    hi = float(hi if hi is not None else lo)
    if hi < lo:
        lo, hi = hi, lo
    if lo <= 0:
        return {}
    currency = clean_text(str(base_salary.get("currency") or "INR")).upper()
    unit = clean_text(str(value.get("unitText") or "YEAR")).upper()
    period = "per_month" if unit.startswith("MONTH") else "per_annum"
    raw = "{} {:,.0f} - {:,.0f} {}".format(currency, lo, hi, unit.title())
    out = {"salary_raw": raw[:120]}
    if currency in ("INR", "USD"):
        out.update({"salary_min": int(round(lo)), "salary_max": int(round(hi)),
                    "salary_period": period, "salary_currency": currency})
    return out


# ----------------------------------------------------------------------------
# Classification (club enums)
# ----------------------------------------------------------------------------

_JUNK_TITLE_RE = re.compile(
    r"^\s*test\b|\btest jobs?\b|\bdummy\b|asdf|qwer", re.IGNORECASE)

_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"[a-z]+ologist|intensivist|hospitalist|anaesthetist|anesthetist|"
    r"obstetrician|p(a?)ediatrician|psychiatrist|veterinar|"
    r"medical superintendent|medical director|"
    r"medical affairs|medical science liaison|\bmsl\b", re.IGNORECASE)

_NON_CLINICAL_TITLE_RE = re.compile(
    r"business development|\bsales\b|marketing|tele ?call|receptionist|"
    r"accountant|\bhr\b|\badmin|underwriter|content writ|graphic design|"
    r"software|developer|data entry", re.IGNORECASE)


def classify_category(title, site_categories):
    """Map to the club category enum; returns (category, needs_review).

    Title regexes win; a job whose only source categories are ambiguous
    (psychology/biotech/...) and whose title shows no healthcare keyword is
    kept but flagged for review (master spec §2).
    """
    title = title or ""
    slugs = set(site_categories or [])
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title):
        category = "pharmacists"
    elif _DOCTOR_RE.search(title):
        category = "doctors"
    else:
        category = "non_clinical"
    all_ambiguous = bool(slugs) and slugs.issubset(set(AMBIGUOUS_CATEGORIES))
    needs_review = bool(_JUNK_TITLE_RE.search(title)) or (
        all_ambiguous and not ALLOW_TITLE_RE.search(title))
    return category, needs_review


_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|clinical|biotech|"
    r"diagnostic|life ?science", re.IGNORECASE)


def classify_company_type(company, industry=""):
    """club enum hospital|pharma."""
    text = "{} {}".format(company or "", industry or "")
    return "pharma" if _PHARMA_RE.search(text) else "hospital"


# ----------------------------------------------------------------------------
# Experience parsing ("No experience required", "1 year(s)")
# ----------------------------------------------------------------------------

_EXP_RE = re.compile(r"(\d+)(?:\s*-\s*(\d+))?\s*year", re.IGNORECASE)


def parse_experience(text):
    """Return (min_years, max_years) as strings; '' when absent."""
    text = clean_text(text)
    if not text:
        return "", ""
    if "no experience" in text.lower() or "fresher" in text.lower():
        return "0", ""
    m = _EXP_RE.search(text)
    if not m:
        return "", ""
    return m.group(1), m.group(2) or ""


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None):
    today = today or date.today()
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def url_is_clean(url):
    """robots.txt disallows /*?* and /*,* — never request such URLs."""
    return "?" not in url and "," not in url


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    probes = [LISTING_URL_TMPL.format(CORE_CATEGORIES[0]),
              SITE_BASE + "/job/detail/sample-slug"]
    for url in probes:
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch_page(session, url):
    """GET a page with retries/backoff; returns (final_url, text) or None."""
    if not url_is_clean(url):
        log.warning("Skipping robots-disallowed URL pattern: %s", url)
        return None
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
            return resp.url, resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Listing-card parsing
# ----------------------------------------------------------------------------

_CARD_SPLIT_RE = re.compile(
    r'<div class="container-fluid individual_internship[^"]*"\s+'
    r'id="individual_internship_(\d+)"')
_HREF_RE = re.compile(r"data-href='([^']+)'|href=\"(/job/detail/[^\"]+)\"")
_TITLE_RE = re.compile(r'class="job-title-href"[^>]*>\s*(.*?)\s*</a>', re.S)
_COMPANY_RE = re.compile(r'<p class="company-name">\s*(.*?)\s*</p>', re.S)
_LOCATIONS_RE = re.compile(
    r'class="row-1-item\s+locations">.*?<span>(.*?)</span>', re.S)
_LOC_LINK_RE = re.compile(r"<a[^>]*>([^<]*)</a>")
_SALARY_RE = re.compile(
    r'ic-16-money"></i>\s*<span class="desktop">\s*(.*?)\s*</span>', re.S)
_EXP_BLOCK_RE = re.compile(
    r'ic-16-briefcase"></i>\s*<span>([^<]*)</span>')
_AGO_BLOCK_RE = re.compile(
    r'ic-16-reschedule"></i><span>([^<]*)</span>')


def parse_listing_cards(page_html):
    """Split a listing page into job-card dicts (best effort per card)."""
    cards = []
    matches = list(_CARD_SPLIT_RE.finditer(page_html))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else m.start() + 20_000
        block = page_html[m.start():end]
        try:
            href = ""
            hm = _HREF_RE.search(block)
            if hm:
                href = hm.group(1) or hm.group(2) or ""
            locations = []
            lm = _LOCATIONS_RE.search(block)
            if lm:
                locations = [clean_text(x) for x in _LOC_LINK_RE.findall(lm.group(1))]
                locations = [x for x in locations if x]
            card = {
                "job_id": m.group(1),
                "job_url": SITE_BASE + href if href.startswith("/") else href,
                "title": clean_text(_search(_TITLE_RE, block)),
                "company": clean_text(_search(_COMPANY_RE, block)),
                "locations": locations,
                "salary_card": clean_text(_search(_SALARY_RE, block)),
                "experience_raw": clean_text(_search(_EXP_BLOCK_RE, block)),
                "ago": clean_text(_search(_AGO_BLOCK_RE, block)),
                "work_from_home": bool(re.search(r"work from home", block, re.I)),
            }
            cards.append(card)
        except Exception as exc:  # one bad card must never crash the run
            log.warning("Skipping malformed card %s: %s", m.group(1), exc)
    return cards


def _search(pattern, block):
    m = pattern.search(block)
    return m.group(1) if m else ""


def crawl_category(session, slug, max_pages):
    """Yield cards from every page of one category listing."""
    seen_ids = set()
    for page in range(1, (max_pages or MAX_PAGES_PER_CATEGORY) + 1):
        url = (LISTING_URL_TMPL.format(slug) if page == 1
               else LISTING_PAGE_TMPL.format(slug, page))
        result = fetch_page(session, url)
        if result is None:
            break
        final_url, page_html = result
        # an empty/unknown category 301s to /jobs/ — do NOT scrape that.
        if "/jobs/{}".format(slug) not in final_url:
            log.info("Category %s has no live listings (redirected to %s)",
                     slug, final_url)
            break
        cards = parse_listing_cards(page_html)
        new_cards = [c for c in cards if c["job_id"] not in seen_ids]
        if not new_cards:
            break
        seen_ids.update(c["job_id"] for c in new_cards)
        log.debug("%s page %d: %d cards", slug, page, len(new_cards))
        yield from new_cards
        if len(cards) < 10:  # short page = last page
            break


# ----------------------------------------------------------------------------
# Detail-page JSON-LD
# ----------------------------------------------------------------------------

_LD_RE = re.compile(
    r'<script type="application/ld\+json">\s*(.*?)\s*</script>', re.S)


def parse_job_posting_ld(page_html):
    """Return the JobPosting JSON-LD dict from a detail page, or {}."""
    for m in _LD_RE.finditer(page_html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return {}


def fetch_job_detail(session, job_url):
    result = fetch_page(session, job_url)
    if result is None:
        return {}
    return parse_job_posting_ld(result[1])


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(card, site_categories, detail=None, today=None):
    """Merge listing-card data with detail JSON-LD into a rich-CSV row."""
    detail = detail or {}
    title = card.get("title") or clean_text(detail.get("title"))
    category, needs_review = classify_category(title, site_categories)

    city = state = ""
    country_code = "IN"
    locs = detail.get("jobLocation")
    if isinstance(locs, dict):
        locs = [locs]
    if isinstance(locs, list) and locs:
        addr = (locs[0] or {}).get("address") or {}
        city = clean_text(addr.get("addressLocality"))
        state = clean_text(addr.get("addressRegion"))
        country_code = clean_text(addr.get("addressCountry")) or "IN"
    if not city and card.get("locations"):
        first = card["locations"][0]
        if "work from home" not in first.lower():
            city = first
    country_name, dial = COUNTRY_META.get(country_code, ("India", "+91"))

    employment_raw = clean_text(detail.get("employmentType"))
    if card.get("work_from_home"):
        job_type = "remote"
    elif "PART_TIME" in employment_raw.upper():
        job_type = "part_time"
    else:
        job_type = "full_time"

    posted = clean_text(detail.get("datePosted"))[:10]
    estimate = False
    if not posted:
        posted = ago_to_date(card.get("ago"), today)
        estimate = True

    exp_min, exp_max = parse_experience(card.get("experience_raw"))

    company = card.get("company") or clean_text(
        (detail.get("hiringOrganization") or {}).get("name"))
    industry = clean_text(detail.get("industry"))
    description = strip_html(detail.get("description") or "")

    row = {
        "source": SITE,
        "job_id": card["job_id"],
        "title": title,
        "company": company,
        "city": city,
        "state": state,
        "country": country_name,
        "country_code": country_code if country_code in COUNTRY_META else "IN",
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": job_type,
        "employment_type_raw": employment_raw,
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "site_categories": "; ".join(sorted(set(site_categories))),
        "category": category,
        "company_type": classify_company_type(company, industry),
        "industry": industry,
        "skills": clean_text(detail.get("skills"))[:300],
        "needs_review": needs_review,
        "posted_date": posted,
        "posted_date_is_estimate": estimate,
        "valid_through": clean_text(detail.get("validThrough"))[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": card.get("job_url", ""),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    salary = parse_ld_salary(detail.get("baseSalary")) or \
        parse_card_salary(card.get("salary_card"))
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


def rich_row_to_club_row(r):
    min_sal = _int_str(r.get("salary_min"))
    currency = _blank(r.get("salary_currency"))
    has_salary = bool(min_sal) and currency in ("INR", "USD")
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")) or "India",
        "company_name": _blank(r.get("company")) or _blank(r.get("title")),
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")) or "non_clinical",
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
        "is_active": "true",
        "expires_at": _blank(r.get("valid_through")),
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
        description="Scrape healthcare jobs from internshala.com.")
    parser.add_argument("--output", default=str(Path(__file__).parent / RICH_CSV),
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="max listing pages per category (test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (card data only; faster)")
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
    prefilter_floor = (
        date.fromisoformat(cutoff) - timedelta(days=AGO_SLACK_DAYS)).isoformat()
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    # ---- crawl every healthcare category, merging duplicate cards ----
    cards_by_id, categories_by_id = {}, {}
    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    for slug in CORE_CATEGORIES + AMBIGUOUS_CATEGORIES:
        for card in crawl_category(session, slug, args.max_pages):
            counters["scanned"] += 1
            jid = card["job_id"]
            categories_by_id.setdefault(jid, set()).add(slug)
            cards_by_id.setdefault(jid, card)

    log.info("Crawled %d categories: %d unique jobs seen",
             len(CORE_CATEGORIES + AMBIGUOUS_CATEGORIES), len(cards_by_id))

    # ---- filter, enrich, build rows ----
    new_rows, review_log = [], []
    today = date.today()
    for jid, card in cards_by_id.items():
        if jid in known_ids:
            counters["duplicates"] += 1
            continue
        approx = ago_to_date(card.get("ago"), today)
        if approx and approx < prefilter_floor:
            counters["excluded_old"] += 1
            continue
        detail = {}
        if not args.no_details and card.get("job_url"):
            detail = fetch_job_detail(session, card["job_url"])
            if not detail:
                counters["detail_failed"] += 1
        try:
            row = build_row(card, categories_by_id[jid], detail, today)
        except Exception as exc:
            log.warning("Skipping malformed job %s: %s", jid, exc)
            continue
        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            continue
        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": jid, "title": row["title"],
                               "site_categories": row["site_categories"]})
        known_ids.add(jid)
        new_rows.append(row)
        counters["new"] += 1
        if args.limit is not None and counters["new"] >= args.limit:
            log.info("--limit %d reached", args.limit)
            break

    # ---- rich cumulative CSV ----
    out_path = Path(args.output)
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[[c for c in RICH_COLUMNS if c in combined.columns]]
        combined.to_csv(out_path, index=False)
        log.info("Wrote %s (%d total rows)", out_path, len(combined))
    else:
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", out_path)

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv(Path(__file__).parent / REVIEW_CSV,
                                        index=False)

    print("\n===== Run summary =====")
    print("Cards scanned:         {:>5,}".format(counters["scanned"]))
    print("Unique jobs seen:      {:>5,}".format(len(cards_by_id)))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
