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

Scope — fetch wide, filter tight (2026-08-25)
---------------------------------------------
Every live job category is crawled (ALL_CATEGORIES, 173 slugs derived from
internshala's own category sitemaps and probed live), and the shared
two-level taxonomy classifier decides what survives. Internshala's category
facet is far too loose to scope a crawl with — "biostatistics-jobs" returns
maths teachers and "pharmacovigilance-jobs" returns sales analysts — and the
old 13-slug healthcare-only list both trusted that facet and capped reach
(a Medical Coder filed under "bpo-jobs" was unreachable).

Classification is `scrappers/_shared/classification.py` (`classify_job` +
the 23-column `CLUB_COLUMNS`); the retired profession enum
(doctors/nurses/pharmacists/non_clinical) is gone. Out-of-scope rows are
DROPPED and counted as excluded_out_of_scope. On the stored backfill this
is 50 kept / 478 dropped out of 528.

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
# ---------------------------------------------------------------------------
# Crawl scope — fetch wide, filter tight (2026-08-25)
# ---------------------------------------------------------------------------
# Internshala's category facet is LOOSE: "biostatistics-jobs" returns maths
# teachers, "pharmacovigilance-jobs" returns sales analysts, "nurse-jobs"
# returns biology teachers. Hand-picking healthcare-looking slugs therefore
# buys precision the site cannot actually deliver, while silently capping
# reach — a Medical Coder filed under "bpo-jobs" was unreachable.
#
# So the crawl walks EVERY live job category and lets classify_job do all the
# rejecting. Unrelated categories cost one listing request each and contribute
# nothing to the output.
#
# The 173 slugs below were derived from internshala's own
# sitemap-categories.xml + sitemap-virtual-categories.xml (207 category names,
# suffix swapped -internship -> -jobs) and each probed live on 2026-08-25;
# the 34 that 301 to /jobs/ are omitted. crawl_category() independently
# detects a redirect and skips, so a slug going stale is safe, not silent
# corruption. To refresh: re-derive from the two sitemaps and re-probe.
ALL_CATEGORIES = [
    "3d-printing-jobs", "accounting-jobs", "accounts-jobs", "acting-jobs",
    "aerospace-jobs", "agriculture-and-food-engineering-jobs",
    "ai-agent-development-jobs", "analytics-jobs", "anchoring-jobs",
    "android-app-development-jobs", "angular-js-development-jobs",
    "animation-jobs", "architecture-jobs", "artificial-intelligence-ai-jobs",
    "asp-net-jobs", "audio-making-editing-jobs", "auditing-jobs",
    "automobile-engineering-jobs", "aws-jobs", "backend-development-jobs",
    "bank-jobs", "big-data-jobs", "bioinformatics-jobs", "biology-jobs",
    "biotech-jobs", "blockchain-development-jobs", "blogging-jobs",
    "brand-management-jobs", "business-development-jobs",
    "ca-articleship-jobs", "cad-design-jobs", "campus-ambassador-jobs",
    "chartered-accountancy-ca-jobs", "chemical-jobs", "chemistry-jobs",
    "cinematography-jobs", "civil-jobs", "client-servicing-jobs",
    "cloud-computing-jobs", "cma-articleship-jobs", "commerce-jobs",
    "company-secretary-cs-jobs", "computer-science-jobs",
    "computer-vision-jobs", "consultant-jobs", "consulting-jobs",
    "content-writing-jobs", "copywriting-jobs", "creative-writing-jobs",
    "culinary-arts-jobs", "customer-service-jobs", "cyber-security-jobs",
    "data-entry-jobs", "data-science-jobs", "database-building-jobs",
    "design-jobs", "dietetics-nutrition-jobs", "digital-marketing-jobs",
    "e-commerce-jobs", "editorial-jobs", "electric-vehicle-jobs",
    "electrical-jobs", "electronics-jobs", "email-marketing-jobs",
    "embedded-systems-jobs", "energy-science-and-engineering-jobs",
    "engineering-design-jobs", "engineering-jobs", "engineering-physics-jobs",
    "environmental-sciences-jobs", "event-management-jobs",
    "facebook-marketing-jobs", "facility-management-jobs",
    "fashion-design-jobs", "film-making-jobs", "finance-jobs",
    "flutter-development-jobs", "front-end-development-jobs",
    "full-stack-development-jobs", "fundraising-jobs", "game-design-jobs",
    "game-development-jobs", "general-management-jobs", "government-jobs",
    "graphic-design-jobs", "hospitality-jobs", "hotel-management-jobs",
    "hr-jobs", "humanities-jobs", "image-processing-jobs",
    "industrial-and-production-engineering-jobs", "industrial-design-jobs",
    "information-technology-jobs",
    "instrumentation-and-control-engineering-jobs", "interior-design-jobs",
    "international-jobs", "internet-of-things-iot-jobs",
    "ios-app-development-jobs", "java-jobs", "javascript-development-jobs",
    "journalism-jobs", "law-jobs", "legal-research-jobs", "logistics-jobs",
    "machine-learning-jobs", "manufacturing-engineering-jobs",
    "market-business-research-jobs", "marketing-jobs",
    "material-science-jobs", "mathematics-jobs", "mba-jobs",
    "mechanical-jobs", "mechatronics-jobs", "media-jobs", "medicine-jobs",
    "merchandise-design-jobs", "merchandising-jobs", "mlops-engineering-jobs",
    "mobile-app-development-jobs", "motion-graphics-jobs", "music-jobs",
    "natural-language-processing-nlp-jobs", "net-development-jobs",
    "network-engineering-jobs", "networking-jobs", "ngo-jobs",
    "node-js-development-jobs", "operations-jobs", "other-jobs",
    "pharmaceutical-jobs", "photography-jobs", "php-development-jobs",
    "physics-jobs", "political-economics-policy-research-jobs", "pr-jobs",
    "product-jobs", "programming-jobs", "project-management-jobs",
    "prompt-engineering-jobs", "proofreading-jobs", "psychology-jobs",
    "python-django-jobs", "quality-analyst-jobs", "recruitment-jobs",
    "robotics-jobs", "sales-jobs", "sap-jobs", "science-jobs",
    "search-engine-optimization-seo-jobs", "site-engineering-jobs",
    "social-media-marketing-jobs", "social-work-jobs",
    "software-development-jobs", "software-testing-jobs", "sports-jobs",
    "statistics-jobs", "stock-market-trading-jobs", "strategy-jobs",
    "subject-matter-expert-sme-jobs", "supply-chain-management-scm-jobs",
    "talent-acquisition-jobs", "tally-jobs", "teaching-jobs",
    "telecalling-jobs", "transcription-jobs", "translation-jobs",
    "travel-and-tourism-jobs", "ui-ux-jobs", "video-making-editing-jobs",
    "videography-jobs", "volunteering-jobs", "web-development-jobs",
    "wordpress-development-jobs",
]

# Kept for the robots probe and for tests that assert healthcare reach.
CORE_CATEGORIES = [
    "medicine-jobs", "nurse-jobs", "pharmacist-jobs", "pharmaceutical-jobs",
    "dietetics-nutrition-jobs", "clinical-research-jobs",
    "clinical-data-management-jobs", "pharmacovigilance-jobs",
    "regulatory-affairs-jobs", "medical-writing-jobs", "medical-coding-jobs",
]

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "state", "country",
    "country_code", "country_dial_code", "salary_raw", "salary_min",
    "salary_max", "salary_period", "salary_currency", "job_type",
    "employment_type_raw", "experience_min_years", "experience_max_years",
    "site_categories", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "qualification", "company_type", "industry", "skills",
    "needs_review", "posted_date", "posted_date_is_estimate",
    "valid_through", "description", "job_url", "scraped_at",
]


COUNTRY_META = {
    "IN": ("India", "+91"),
    "US": ("United States", "+1"),
    "GB": ("United Kingdom", "+44"),
    "AE": ("United Arab Emirates", "+971"),
    "SG": ("Singapore", "+65"),
    "AU": ("Australia", "+61"),
    "CA": ("Canada", "+1"),
}

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

# The retired profession regexes (_NURSE_RE / _PHARMACIST_RE / _DOCTOR_RE)
# were deleted with the legacy enum on 2026-08-25 — classify_job owns
# categorisation now. _JUNK_TITLE_RE stays: it is a data-quality check on
# obvious test postings, not a category decision.

_NON_CLINICAL_TITLE_RE = re.compile(
    r"business development|\bsales\b|marketing|tele ?call|receptionist|"
    r"accountant|\bhr\b|\badmin|underwriter|content writ|graphic design|"
    r"software|developer|data entry", re.IGNORECASE)


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The internshala category slugs the job was found under are the curated
    `skills` signal (they are loose, so they are weighted, never decisive).
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    slugs = row.get("site_categories") or ""
    if isinstance(slugs, (list, tuple, set)):
        slugs = ", ".join(slugs)
    slugs = str(slugs).replace("-jobs", "").replace("-", " ")
    verdict = classify_job(row.get("title", ""), slugs,
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = verdict["needs_review"]
    row["qualification"] = extract_qualification(
        "%s %s" % (row.get("title", ""), row.get("description", "")))
    return verdict["in_scope"]


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
        # category / sub_category / role_family / qualification and the score
        # trace are stamped by apply_classification() after the row is built.
        "category": "",
        "company_type": classify_company_type(company, industry),
        "industry": industry,
        "skills": clean_text(detail.get("skills"))[:300],
        "needs_review": False,
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
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        "qualification": _blank(r.get("qualification")),
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
    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    for slug in ALL_CATEGORIES:
        for card in crawl_category(session, slug, args.max_pages):
            counters["scanned"] += 1
            jid = card["job_id"]
            categories_by_id.setdefault(jid, set()).add(slug)
            cards_by_id.setdefault(jid, card)

    log.info("Crawled %d categories: %d unique jobs seen",
             len(ALL_CATEGORIES), len(cards_by_id))

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
        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
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
    print("Excluded (out of scope): {:>4,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
