#!/usr/bin/env python3
"""Scrape academic and research vacancies from jobs.ac.uk.

Data source
-----------
jobs.ac.uk is the UK's academic job board (Jisc). Its supply is university
posts — lecturers, senior lecturers, professors, postdoctoral research
associates, research fellows, PhD studentships and university professional
services — across every discipline. The health-relevant slice is UK public
health academia ("Associate Lecturer in Public Health", "Postdoctoral
Research Associate in Public Health Nutrition", "Lecturer / Senior Lecturer
in Public Health Management"), which lands in Public Health Research and
Public Health Program Management; the large majority (physics studentships,
finance faculty, IT officers) is dropped by the shared classifier.

Discovery is the site's own search, run with NO keyword and NO facet:

    /search/?keywords=&sortOrder=1&pageSize=25&startIndex=N

`sortOrder=1` is date-placed descending (verified 2026-08-27: page 1 all
"26 Aug", startIndex=2001 "24 Jul"), so the crawl walks the whole board
newest-first and stops at the first page entirely older than the cutoff.
This is the widest cheap enumeration available and is a strict superset of
any union of health keyword searches — an empty search reports "2,294 Jobs
Found", and `sitemap0.xml` (the only sitemap robots.txt names) carries 2,216
`/job/<ref>/<slug>` URLs, i.e. the same board. The sitemap is NOT used as
the crawl source because it carries no per-job date (every `<changefreq>` is
"daily", there is no `<lastmod>`), which would force a detail fetch for all
2,216 ids on every run; the search cards do carry "Date Placed", so the date
gate can run before any detail request.

Every detail page (`/job/<ref>/<slug>`) embeds a complete schema.org
JobPosting JSON-LD block: `datePosted`, `validThrough`, clean `title`, the
full HTML `description` (verified complete, not truncated),
`hiringOrganization` (name, logo, department) and a `jobLocation` address
(locality/region/country). The visible detail table adds Location, Salary,
Hours, Contract Type and Job Ref, none of which are in the JSON-LD.

The board's curated role signal is the detail page's **Advert information**
block — a "Type / Role" tag (`Academic or Research`, `PhDs`, `Professional
or Managerial`, ...) plus one or more "Subject Area(s)" tags drawn from the
site's academic-discipline taxonomy (`Health & Medical` > `Medicine &
Dentistry`, `Nutrition`, ...). These are passed to classify_job as `skills`
and kept verbatim in the rich CSV's `sectors` column; they never decide the
category on their own.

robots.txt: `User-agent: *` with only /job/feedback/ and /enhanced/fp/
disallowed, plus a Sitemap line. Compliance is verified at startup with
urllib.robotparser.

Verified quirks
---------------
* `pageSize` is capped server-side at 25 — `pageSize=50/100/200` all return
  25 cards — so the page count is fixed at ceil(window_jobs / 25). The board
  places ~200 adverts a day, so a 14-day first run is ~55 listing pages.
* Search cards date-stamp as "Date Placed: 25 Aug" — **day and month, no
  year**. The year is inferred (current year, rolled back one if that lands
  more than 2 days in the future) and used only as a cheap pre-filter with a
  ±PREFILTER_MARGIN_DAYS margin; the authoritative gate is the detail page's
  JSON-LD `datePosted`.
* The detail table's second date row is labelled "Closes:" on most adverts
  but "Expires:" on others (e.g. overseas posts) — both are read.
* "Type / Role" renders as a *disabled* `<input type="button">` on job
  adverts but as a live `<input type="submit">` on studentships, so the tag
  is read from the enclosing form's `action="/search/<slug>"` instead of
  from the widget, which is stable across both.
* PhD/Masters studentships share the board, the URL space and the same
  JobPosting JSON-LD, but they are study places, not jobs. A row whose
  Type / Role is `phds`/`masters` — or whose title says studentship /
  scholarship / doctoral training — is marked `is_studentship` and, when the
  classifier keeps it, force-flagged into needs_review.csv rather than
  silently exported as a job.
* Salaries are free text ("£39,906 to £46,049 per annum", "£69.68 per
  hour", "Competitive"). They are parsed into rich-CSV columns verbatim plus
  min/max/currency/period, but GBP cannot be expressed in the club schema's
  salary_currency enum (INR/USD only), so the club salary columns stay blank
  — the reed precedent. Nothing is converted or invented.
* The board is UK-centric but not UK-only (Chengdu, Germany, ...); the club
  country columns come from the JSON-LD `addressCountry`, not a constant.

Classification (shared taxonomy)
--------------------------------
The whole board is crawled — fetch wide, filter tight. The keep/drop and
labeling decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the Advert information Type / Role + Subject Area(s) tags
* description = the JSON-LD description, HTML-stripped

in_scope False (aluminium alloys, marketing faculty, estates officers) ->
DROPPED, counted excluded_out_of_scope, full row appended to
out-of-scope.csv. in_scope True fills category/sub_category/role_family and
the score-trace columns; needs_review True keeps the row AND appends it to
needs_review.csv.

One crawl-side exception saves requests without touching that authority:
~90% of the board is out of scope, so a card whose title is a BARE generic
research title ("Research Associate") with no health-scope term anywhere in
the card's text (title/department/employer/location) skips its detail fetch
— counted "Skipped (non-health listing)", never added to a skip list, and
re-decided from fresh listing text every run (see should_skip_listing).

Outputs
-------
* jobsacuk_jobs.csv — rich cumulative store (dedup key: job_id, the
  `/job/<ref>` reference), source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/jobsacuk.csv — the same jobs mapped to the
  HealthCareers.club CLUB_COLUMNS schema.
* seen_old_ids.csv — out-of-window ids (detail-fetch skip list).
* out-of-scope.csv — dropped rows + their skip list (reversible).
* needs_review.csv — kept but flagged.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(14); later runs keep only jobs newer than the newest stored posted_date
minus WATERMARK_GRACE_DAYS of overlap.

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

SITE = "jobsacuk"
SITE_BASE = "https://www.jobs.ac.uk"
ROBOTS_URL = SITE_BASE + "/robots.txt"
# Empty keywords + no facet = the whole board; sortOrder=1 = date placed desc.
SEARCH_URL_TEMPLATE = (SITE_BASE + "/search/?keywords=&sortOrder=1"
                                   "&pageSize={size}&startIndex={start}")
JOB_URL_TEMPLATE = SITE_BASE + "/job/{}"

# Server-side cap: pageSize=50/100/200 all return 25 cards (probed 2026-08-27).
PAGE_SIZE = 25
# Hard stop so a sort-order regression can never walk the whole archive.
MAX_PAGES = 200

# A stock browser UA leading the honest name: jobs.ac.uk is served through a
# CDN that 302s some bare bot UAs, and the search page renders identically
# for browsers. The scraper still identifies itself and its contact URL.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "HealthCareersJobScraper/1.0 "
    "(+https://github.com/0xprajapati/job-scrappers)"
)

# Academic adverts run 4-6 weeks; 14 days seeds a usable first corpus without
# a 2,200-page crawl. No salary filter (master spec): salaries are captured,
# never filtered on.
INITIAL_WINDOW_DAYS = 14
WATERMARK_GRACE_DAYS = 2
# Listing cards carry no year, so the card-date gate is deliberately loose;
# the JSON-LD datePosted makes the real decision.
PREFILTER_MARGIN_DAYS = 3

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "jobsacuk_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "department", "company_logo",
    "city", "state", "country", "sectors", "type_role", "is_studentship",
    "salary_raw", "salary_min", "salary_max", "salary_currency",
    "salary_period", "job_type", "contract_type", "category", "sub_category",
    "role_family", "all_families", "family_scores", "family_confidence",
    "matched_in", "needs_review", "experience_min_years", "posted_date",
    "valid_through", "description", "job_url", "scraped_at",
]

log = logging.getLogger("jobsacuk_scraper")

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


# ---- crawl-side scoping gate ------------------------------------------------

# jobs.ac.uk is an all-disciplines board and ~90% of it is out of scope, so a
# conservative LISTING-stage gate saves the detail request for cards that are
# clearly not health-related. The search card carries the title, department,
# employer, location, salary and date — the gate reads the WHOLE card text so
# a health signal anywhere on the card ("Faculty of Medicine", "School of
# Nursing", a snippet) keeps the detail fetch.
#
# The gate is a request saver ONLY: the shared classifier remains the final
# keep/drop authority for everything that is fetched, and the gate skips a
# card only when BOTH hold:
#   1. no health-scope term appears anywhere in the card text, AND
#   2. the title is a bare generic research title ("Research Associate",
#      "Senior Research Fellow") that names no discipline at all — a title
#      that names ANY discipline (even "Research Associate in Chemistry") is
#      still fetched and left to the classifier.
# Skipped cards are counted ("Skipped (non-health listing)") and are not
# added to any skip list, so the decision is re-made from fresh listing text
# on every run.
HEALTH_SCOPE_RE = re.compile(
    r"health|medical|medicine|clinical|nursing|pharma\w*|epidemiolog\w*|"
    r"public health|biomed\w*|psychiat\w*|oncolog\w*|immunolog\w*|virol\w*|"
    r"microbiol\w*|neurosci\w*|dental|veterinary|physiolog\w*|toxicol\w*|"
    r"nutrition|life science\w*|biostatist\w*|global health|population health",
    re.IGNORECASE)

_GENERIC_RESEARCH_TITLE_RE = re.compile(
    r"^(?:senior |postdoctoral )?research (?:associate|assistant|fellow)s?\b",
    re.IGNORECASE)
# Words allowed AFTER the generic title without making it informative:
# contract furniture ("(Fixed Term)", "x2", "0.5 FTE"), never a discipline.
_GENERIC_TITLE_FILLER = {
    "fixed", "term", "full", "part", "time", "fte", "x", "post", "posts",
    "position", "positions", "vacancy", "vacancies", "ref", "grade",
    "maternity", "cover", "months", "month", "years", "year",
}


def is_bare_generic_research_title(title):
    """True only for a generic research title that names no discipline."""
    title = clean_text(title)
    m = _GENERIC_RESEARCH_TITLE_RE.match(title)
    if not m:
        return False
    rest = re.findall(r"[a-zA-Z]+", title[m.end():])
    return all(w.lower() in _GENERIC_TITLE_FILLER for w in rest)


def should_skip_listing(title, card_text):
    """True when a search card can safely skip its detail fetch.

    Conservative by construction: any health-scope term anywhere on the card
    keeps the fetch, and only bare generic research titles are gateable at
    all — everything else goes to the detail page and the shared classifier.
    """
    if HEALTH_SCOPE_RE.search(card_text or "") or HEALTH_SCOPE_RE.search(title or ""):
        return False
    return is_bare_generic_research_title(title)


# ---- search listing ---------------------------------------------------------

_CARD_SPLIT_RE = re.compile(r'<div class="j-search-result__result\b')
_CARD_ID_RE = re.compile(r'data-advert-id="(\d+)"')
_CARD_LINK_RE = re.compile(r'href="/job/([^/"]+)/([^"]*)"[^>]*>\s*(.*?)\s*</a>', re.S)
_CARD_EMPLOYER_RE = re.compile(
    r'j-search-result__employer[^>]*>\s*<b>\s*(.*?)\s*</b>', re.S)
_CARD_DATE_RE = re.compile(r'Date Placed:\s*</strong>\s*([^<]+)<', re.I)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_card_date(text, today=None):
    """"25 Aug" -> "2026-08-25". The board omits the year.

    The current year is assumed and rolled back one when that would place
    the advert in the future (adverts are never post-dated). Returns "" for
    anything unparseable — an undated card is never date-excluded.
    """
    today = today or date.today()
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})", clean_text(text))
    if not m:
        return ""
    day, month = int(m.group(1)), _MONTHS.get(m.group(2).lower())
    if not month:
        return ""
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:                      # 29 Feb on a non-leap year
            continue
        if candidate <= today + timedelta(days=2):
            return candidate.isoformat()
    return ""


def parse_search_cards(page_html, today=None):
    """Every result card on one search page, in page (date-desc) order.

    Each card is {job_id, title, company, card_date, card_text} — enough for
    the cheap date pre-filter, the non-health scoping gate and logging; every
    exported field comes from the detail page. card_text is the whole card
    stripped of markup (title + department + employer + location + salary),
    so the scoping gate sees every scrap of listing-stage text there is.
    """
    cards = []
    for block in _CARD_SPLIT_RE.split(page_html or "")[1:]:
        link = _CARD_LINK_RE.search(block)
        if not link:
            continue
        advert_id = _CARD_ID_RE.search(block)
        employer = _CARD_EMPLOYER_RE.search(block)
        card_date = _CARD_DATE_RE.search(block)
        cards.append({
            "job_id": clean_text(link.group(1)),
            "slug": clean_text(link.group(2)),
            "advert_id": advert_id.group(1) if advert_id else "",
            "title": strip_html(link.group(3)),
            "company": strip_html(employer.group(1)) if employer else "",
            "card_date": parse_card_date(card_date.group(1) if card_date else "",
                                         today=today),
            "card_text": strip_html(block),
        })
    return cards


# ---- detail page ------------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S)
# strict=False tolerates raw control characters inside the advert HTML.
_LAX_DECODER = json.JSONDecoder(strict=False)


def parse_job_posting(page_html):
    """The schema.org JobPosting JSON-LD block of a detail page, or {}."""
    for m in _LDJSON_RE.finditer(page_html or ""):
        try:
            data, _ = _LAX_DECODER.raw_decode(m.group(1).strip())
        except ValueError as exc:
            log.debug("Unparseable ld+json block: %s", exc)
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


_ADVERT_INFO_RE = re.compile(
    r"<h5>\s*Advert information\s*</h5>(.*?)<h5>", re.S | re.I)
_ADVERT_INFO_TAIL_RE = re.compile(
    r"<h5>\s*Advert information\s*</h5>(.*)", re.S | re.I)
# The Type / Role widget is a disabled <input type="button"> on job adverts
# and a live <input type="submit"> on studentships; the enclosing form's
# action slug is the one stable carrier of the tag.
_TYPE_ROLE_RE = re.compile(r'action="/search/([a-z0-9\-]+)"', re.I)
# Subject Area(s): forms that post the site's academic-discipline facets.
# The visible label is the button/submit value inside the same form.
_SUBJECT_FORM_RE = re.compile(
    r'<form[^>]*action="/search/"[^>]*>(.*?)</form>', re.S | re.I)
_SUBJECT_FACET_RE = re.compile(
    r'name="(?:academicDisciplineFacet|subDisciplineFacet)\[\d+\]"', re.I)
_SUBJECT_LABEL_RE = re.compile(
    r'<input[^>]*type="(?:submit|button)"[^>]*value="([^"]*)"', re.I)

TYPE_ROLE_LABELS = {
    "academic-or-research": "Academic or Research",
    "professional-or-managerial": "Professional or Managerial",
    "senior-management": "Senior Management",
    "technical": "Technical",
    "clerical": "Clerical",
    "craft-or-manual": "Craft or Manual",
    "hospitality-retail-and-conference": "Hospitality, Retail and Conference",
    "phds": "PhDs",
    "masters": "Masters",
}
# Type / Role slugs that mark a study place rather than a job.
STUDY_TYPE_ROLES = {"phds", "masters"}


def _advert_info_block(page_html):
    m = _ADVERT_INFO_RE.search(page_html or "")
    if not m:
        m = _ADVERT_INFO_TAIL_RE.search(page_html or "")
    return m.group(1) if m else ""


def parse_type_role(page_html):
    """The Advert information "Type / Role" slug, e.g. "academic-or-research"."""
    block = _advert_info_block(page_html)
    for slug in _TYPE_ROLE_RE.findall(block):
        if slug in TYPE_ROLE_LABELS:
            return slug
    return ""


def parse_subject_areas(page_html):
    """The Advert information "Subject Area(s)" labels, in page order."""
    labels, seen = [], set()
    for form in _SUBJECT_FORM_RE.findall(_advert_info_block(page_html)):
        if not _SUBJECT_FACET_RE.search(form):
            continue
        for raw in _SUBJECT_LABEL_RE.findall(form):
            label = clean_text(raw)
            if label and label.lower() not in seen:
                seen.add(label.lower())
                labels.append(label)
    return labels


def parse_sectors(page_html):
    """The board's curated role signal, joined ("; ").

    "Academic or Research; Health & Medical; Medicine & Dentistry" — the
    Type / Role tag followed by every Subject Area tag.
    """
    slug = parse_type_role(page_html)
    parts = [TYPE_ROLE_LABELS[slug]] if slug else []
    parts.extend(parse_subject_areas(page_html))
    return "; ".join(parts)


_DETAIL_ROW_RE = re.compile(
    r'<th class="j-advert-details__table-header">\s*([^<]+?)\s*:?\s*</th>'
    r'\s*<td[^>]*>(.*?)</td>', re.S | re.I)


def parse_detail_table(page_html):
    """The Location/Salary/Hours/Contract Type/Placed On/Closes table."""
    return {clean_text(k).rstrip(":").lower(): strip_html(v)
            for k, v in _DETAIL_ROW_RE.findall(page_html or "")}


def parse_iso_date(text):
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


# ---- salary -----------------------------------------------------------------

_CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "¥": "CNY"}
_SALARY_PERIODS = [
    (re.compile(r"per\s+annum|p\.?a\.?\b|annual|/\s*year|per\s+year", re.I),
     "per_annum"),
    (re.compile(r"per\s+month|/\s*month|monthly", re.I), "per_month"),
    (re.compile(r"per\s+hour|/\s*hour|hourly|an\s+hour", re.I), "per_hour"),
    (re.compile(r"per\s+week|/\s*week|weekly", re.I), "per_week"),
    (re.compile(r"per\s+day|/\s*day|daily|per\s+diem", re.I), "per_day"),
]
_AMOUNT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{4,7}|\d{1,3}\.\d{2})")


def parse_salary(text):
    """"£39,906 to £46,049 per annum" -> (raw, min, max, currency, period).

    Free-text only — the board has no structured salary anywhere. Rows with
    no number ("Competitive", "Grade 7") keep the raw text and blank
    numbers; nothing is converted or invented.
    """
    raw = clean_text(text)
    if not raw:
        return ("", "", "", "", "")
    currency = ""
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in raw:
            currency = code
            break
    else:
        code_match = re.search(r"\b(GBP|USD|EUR|AUD|CAD|CHF|SGD|HKD|CNY|AED)\b",
                               raw, re.I)
        if code_match:
            currency = code_match.group(1).upper()
    period = ""
    for regex, name in _SALARY_PERIODS:
        if regex.search(raw):
            period = name
            break
    amounts = []
    for match in _AMOUNT_RE.findall(raw):
        try:
            value = float(match.replace(",", ""))
        except ValueError:
            continue
        # Grade/scale numbers ("Grade 7", "Level 6") are not pay. A decimal
        # amount always is, however small ("£69.68 per hour").
        if "." in match or value >= 100:
            amounts.append(value)
    lo = hi = ""
    if amounts:
        lo = _format_amount(min(amounts))
        hi = _format_amount(max(amounts))
    return (raw, lo, hi, currency, period)


def _format_amount(value):
    """Whole pounds stay integers; hourly rates keep their pence."""
    return ("{:.0f}" if float(value).is_integer() else "{:.2f}").format(value)


# ---- experience -------------------------------------------------------------

# "Minimum 4 years of experience", "at least 3 years", "2+ years of
# experience", "3-5 years' experience" — grounded extraction only, never
# inferred.
_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?",
               re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
               r"(?:relevant\s+|work(?:ing)?\s+|professional\s+|"
               r"post[- ]?doctoral\s+|research\s+)?experience",
               re.IGNORECASE),
]


def parse_experience_years(description):
    """The stated minimum years of experience, or "" when not stated."""
    for regex in _EXPERIENCE_RES:
        match = regex.search(description or "")
        if match:
            years = int(match.group(1))
            if 0 < years <= 30:
                return str(years)
    return ""


# ---- studentships -----------------------------------------------------------

# PhD/Masters studentships share the board and the JobPosting JSON-LD but are
# study places, not jobs — detected on the Type / Role tag or the title.
_STUDENTSHIP_TITLE_RE = re.compile(
    r"\b(phd|mphil|dphil|studentship|scholarship|doctoral training|"
    r"masters? by research|msc by research|graduate teaching assistantship)\b",
    re.IGNORECASE)


def is_studentship(title, type_role=""):
    if (type_role or "").lower() in STUDY_TYPE_ROLES:
        return True
    return bool(_STUDENTSHIP_TITLE_RE.search(title or ""))


# ---- job type ---------------------------------------------------------------

def map_job_type(hours, contract_type, location):
    """The club job_type enum from Hours / Contract Type / Location text."""
    blob = " ".join(str(x or "") for x in (hours, contract_type, location)).lower()
    if "hybrid" in blob:
        return "hybrid"
    if "remote" in blob:
        return "remote"
    hours_l = (hours or "").lower()
    if "part time" in hours_l and "full time" not in hours_l:
        return "part_time"
    return "full_time"


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The Advert information Type / Role + Subject Area(s) tags are the site's
    curated `skills` signal; they stay in the rich CSV as a raw source
    column (`sectors`) and never decide the category. Returns in_scope —
    False means DROP the row (excluded_out_of_scope).
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
    # An in-scope studentship is a funded study place on an in-scope topic,
    # not a job opening — always reviewed by a human before export.
    row["needs_review"] = verdict["needs_review"] or bool(row.get("is_studentship"))
    return verdict["in_scope"]


# company_type (hospital|pharma) is a separate club field, NOT a category;
# universities default to "hospital" (the fleet-wide convention for the enum).
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ---- country ----------------------------------------------------------------

# jobs.ac.uk is UK-centric but not UK-only; the club country columns come
# from the advert's own addressCountry. Unknown countries keep their name
# and leave code/dial blank rather than inventing one.
COUNTRIES = {
    "united kingdom": ("United Kingdom", "GB", "+44"),
    "uk": ("United Kingdom", "GB", "+44"),
    "england": ("United Kingdom", "GB", "+44"),
    "scotland": ("United Kingdom", "GB", "+44"),
    "wales": ("United Kingdom", "GB", "+44"),
    "northern ireland": ("United Kingdom", "GB", "+44"),
    "ireland": ("Ireland", "IE", "+353"),
    "china": ("China", "CN", "+86"),
    "hong kong": ("Hong Kong", "HK", "+852"),
    "singapore": ("Singapore", "SG", "+65"),
    "germany": ("Germany", "DE", "+49"),
    "france": ("France", "FR", "+33"),
    "netherlands": ("Netherlands", "NL", "+31"),
    "belgium": ("Belgium", "BE", "+32"),
    "switzerland": ("Switzerland", "CH", "+41"),
    "austria": ("Austria", "AT", "+43"),
    "italy": ("Italy", "IT", "+39"),
    "spain": ("Spain", "ES", "+34"),
    "portugal": ("Portugal", "PT", "+351"),
    "sweden": ("Sweden", "SE", "+46"),
    "norway": ("Norway", "NO", "+47"),
    "denmark": ("Denmark", "DK", "+45"),
    "finland": ("Finland", "FI", "+358"),
    "luxembourg": ("Luxembourg", "LU", "+352"),
    "poland": ("Poland", "PL", "+48"),
    "czech republic": ("Czech Republic", "CZ", "+420"),
    "united states": ("United States", "US", "+1"),
    "united states of america": ("United States", "US", "+1"),
    "usa": ("United States", "US", "+1"),
    "canada": ("Canada", "CA", "+1"),
    "australia": ("Australia", "AU", "+61"),
    "new zealand": ("New Zealand", "NZ", "+64"),
    "japan": ("Japan", "JP", "+81"),
    "south korea": ("South Korea", "KR", "+82"),
    "malaysia": ("Malaysia", "MY", "+60"),
    "india": ("India", "IN", "+91"),
    "united arab emirates": ("United Arab Emirates", "AE", "+971"),
    "saudi arabia": ("Saudi Arabia", "SA", "+966"),
    "qatar": ("Qatar", "QA", "+974"),
    "south africa": ("South Africa", "ZA", "+27"),
}


def resolve_country(name):
    return COUNTRIES.get(clean_text(name).lower(), (clean_text(name), "", ""))


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


def prefilter_cutoff(cutoff):
    """The loose card-date gate: cutoff minus the no-year margin."""
    try:
        parsed = datetime.strptime(cutoff, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return cutoff
    return (parsed - timedelta(days=PREFILTER_MARGIN_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer (master spec §7)
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (SEARCH_URL_TEMPLATE.format(size=PAGE_SIZE, start=1),
                JOB_URL_TEMPLATE.format("DSS400/associate-lecturer-in-public-health")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch(session, url):
    """GET one page with retries/backoff. Returns text or None."""
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
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(job_id, slug, posting, page_html):
    """Rich row from a detail page's JobPosting JSON-LD + visible table."""
    title = clean_text(posting.get("title"))
    table = parse_detail_table(page_html)
    sectors = parse_sectors(page_html)
    type_role = parse_type_role(page_html)

    locations = posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    address = (locations[0].get("address") or {}) if locations else {}
    city = clean_text(address.get("addressLocality"))
    state = clean_text(address.get("addressRegion"))
    country = clean_text(address.get("addressCountry"))

    org = posting.get("hiringOrganization")
    if isinstance(org, dict):
        company = clean_text(org.get("name"))
        logo = clean_text(org.get("logo"))
        dept = org.get("department") or {}
        department = clean_text(dept.get("name")) if isinstance(dept, dict) else ""
    else:
        company, logo, department = clean_text(org), "", ""

    description = strip_html(posting.get("description") or "")[:DESCRIPTION_MAX_CHARS]
    salary_raw, sal_min, sal_max, sal_cur, sal_period = parse_salary(
        table.get("salary", ""))

    return {
        "source": SITE,
        "job_id": str(job_id),
        "title": title,
        "company": company,
        "department": department,
        "company_logo": logo,
        "city": city,
        "state": state,
        "country": country,
        "sectors": sectors,
        "type_role": type_role,
        "is_studentship": is_studentship(title, type_role),
        "salary_raw": salary_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": sal_cur,
        "salary_period": sal_period,
        "job_type": map_job_type(table.get("hours", ""),
                                 table.get("contract type", ""),
                                 table.get("location", "")),
        "contract_type": table.get("contract type", ""),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "experience_min_years": parse_experience_years(description),
        "posted_date": parse_iso_date(posting.get("datePosted")),
        "valid_through": parse_iso_date(posting.get("validThrough")),
        "description": description,
        "job_url": "{}/job/{}/{}".format(SITE_BASE, job_id, slug).rstrip("/"),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _int_str(value):
    text = _blank(value)
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return ""


def rich_row_to_club_row(r):
    """Rich row -> HealthCareers.club 23-column row.

    jobs.ac.uk pays in GBP (and occasionally EUR/CNY), none of which the
    club schema's salary_currency enum (INR/USD) can express, so the club
    salary columns stay blank and the verbatim values live on in the rich
    CSV — the reed precedent. No currency is converted or invented.
    """
    country_name, country_code, dial = resolve_country(r.get("country"))
    currency = _blank(r.get("salary_currency")).upper()
    period = _blank(r.get("salary_period"))
    lo, hi = _int_str(r.get("salary_min")), _int_str(r.get("salary_max"))
    exportable = (bool(lo) and currency in ("INR", "USD")
                  and period in ("per_month", "per_annum"))
    return {
        "country_name": country_name,
        "country_code": country_code,
        "country_dial_code": dial,
        # Nationwide/remote adverts carry no locality; the region, then the
        # country, stand in so the club feed always has a place name.
        "city_name": (_blank(r.get("city")) or _blank(r.get("state"))
                      or country_name),
        "company_name": _blank(r.get("company")),
        "company_type": classify_company_type(_blank(r.get("company"))),
        "company_logo": _blank(r.get("company_logo")),
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
        "max_experience": "",
        # no structured qualification field on the board — grounded
        # extraction from the description only, never inferred
        "qualification": extract_qualification(_blank(r.get("description"))),
        "min_salary": lo if exportable else "",
        "max_salary": (hi or lo) if exportable else "",
        "salary_period": period if exportable else "",
        "salary_currency": currency if exportable else "",
    }


# ----------------------------------------------------------------------------
# Main
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


def crawl_listing(session, soft_cutoff, max_pages=MAX_PAGES):
    """Every search card from the whole board, newest-first, down to the cutoff.

    Walks `/search/?keywords=&sortOrder=1` (date placed descending) and stops
    at the first page with no card on/after `soft_cutoff` — the board is the
    only enumeration, no facet or keyword narrows it.
    """
    cards, pages = [], 0
    for page in range(max_pages):
        start = page * PAGE_SIZE + 1
        url = SEARCH_URL_TEMPLATE.format(size=PAGE_SIZE, start=start)
        page_html = fetch(session, url)
        if page_html is None:
            log.warning("Listing page at startIndex=%d failed — stopping", start)
            break
        pages += 1
        page_cards = parse_search_cards(page_html)
        if not page_cards:
            log.info("No cards at startIndex=%d — end of board", start)
            break
        cards.extend(page_cards)
        in_window = [c for c in page_cards if not c["card_date"]
                     or c["card_date"] >= soft_cutoff]
        log.info("startIndex=%d: %d cards (%d in window, dates %s..%s)",
                 start, len(page_cards), len(in_window),
                 page_cards[0]["card_date"] or "?",
                 page_cards[-1]["card_date"] or "?")
        if not in_window:
            break
    else:
        log.warning("Hit MAX_PAGES=%d — listing crawl truncated", max_pages)
    return cards, pages


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape academic and research vacancies from jobs.ac.uk.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail pages (test runs)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, metavar="N",
                        help="cap the listing crawl at N search pages "
                             "(default: %(default)s)")
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
    soft_cutoff = prefilter_cutoff(cutoff)
    log.info("Existing CSV has %d known jobs (%d known-old, %d known "
             "out-of-scope skipped); keeping jobs posted on/after %s%s "
             "(card pre-filter %s)",
             len(known_ids), len(seen_old), len(out_of_scope_ids), cutoff,
             " (--since override)" if args.since else "", soft_cutoff)

    cards, pages = crawl_listing(session, soft_cutoff, max_pages=args.max_pages)
    log.info("Listing crawl: %d pages, %d cards", pages, len(cards))
    if not cards:
        sys.exit("Search yielded zero cards — page shape changed? Aborting.")

    counters = {"scanned": 0, "duplicates": 0, "skipped_old": 0,
                "skipped_out_of_scope": 0, "skipped_nonhealth": 0,
                "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "detail_failed": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0
    seen_in_run = set()

    for card in cards:                          # already newest-first
        job_id = card["job_id"]
        counters["scanned"] += 1
        if job_id in seen_in_run:
            counters["duplicates"] += 1
            continue
        seen_in_run.add(job_id)
        if job_id in known_ids:
            counters["duplicates"] += 1
            continue
        if job_id in seen_old:
            counters["skipped_old"] += 1
            continue
        if job_id in out_of_scope_ids:
            counters["skipped_out_of_scope"] += 1
            continue
        # Cheap card-date gate — the card has no year, so it runs with a
        # margin and never decides alone.
        if card["card_date"] and card["card_date"] < soft_cutoff:
            counters["excluded_old"] += 1
            seen_old.add(job_id)
            new_seen_old.append({"job_id": job_id,
                                 "posted_date": card["card_date"]})
            continue
        # Crawl-side scoping gate: a bare generic research title with no
        # health-scope term anywhere on the card skips its detail fetch.
        # Request saver only — the shared classifier stays the authority
        # for everything fetched, and nothing is added to any skip list.
        if should_skip_listing(card["title"], card.get("card_text", "")):
            counters["skipped_nonhealth"] += 1
            log.debug("Non-health listing gate skipped %s (%s)",
                      job_id, card["title"])
            continue
        if args.limit is not None and fetched >= args.limit:
            break

        detail_html = fetch(session, "{}/job/{}/{}".format(
            SITE_BASE, job_id, card["slug"]).rstrip("/"))
        fetched += 1
        posting = parse_job_posting(detail_html or "")
        if not posting:
            counters["detail_failed"] += 1
            log.warning("No JobPosting JSON-LD for job_id=%s", job_id)
            continue
        try:
            row = build_row(job_id, card["slug"], posting, detail_html)
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", job_id, exc)
            continue

        # The authoritative date gate: the JSON-LD datePosted.
        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            seen_old.add(job_id)
            new_seen_old.append({"job_id": job_id,
                                 "posted_date": row["posted_date"]})
            continue

        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            out_of_scope_ids.add(job_id)
            dropped_rows.append(row)
            continue

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "company": row["company"],
                               "sectors": row["sectors"],
                               "category": row["category"],
                               "sub_category": row["sub_category"],
                               "role_family": row["role_family"],
                               "is_studentship": row["is_studentship"]})
        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1

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
        total = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)
    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    considered = counters["new"] + counters["excluded_out_of_scope"]
    rate = (100.0 * counters["new"] / considered) if considered else 0.0
    print("\n===== Run summary =====")
    print("Listing pages crawled:        {:>6,}".format(pages))
    print("Search cards scanned:         {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Skipped (non-health listing): {:>6,}".format(counters["skipped_nonhealth"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Skipped (known old):          {:>6,}".format(counters["skipped_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail parse failures:        {:>6,}".format(counters["detail_failed"]))
    print("In-scope rate (classified):   {:>6.1f}%  ({:,}/{:,})".format(
        rate, counters["new"], considered))


if __name__ == "__main__":
    main()
