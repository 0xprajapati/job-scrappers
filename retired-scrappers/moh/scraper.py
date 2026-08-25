#!/usr/bin/env python3
"""Scrape healthcare job announcements from moh.gov.sa (Saudi Ministry of Health).

Data source
-----------
www.moh.gov.sa is a SharePoint 2013 portal. It has **no job API and no
individual vacancy pages**: the Ministry's real applicant system is the Oracle
iRecruitment portal at `erp.moh.gov.sa/OA_HTML/IrcVisitor.jsp`, which is not
reachable from the public internet (connections time out) and whose portal
pages under `/eservices/employment/job/` all redirect to SSO login. The site's
own `_api/` (SharePoint REST/search) is rewritten to the homepage or 302'd to a
404 page for external clients, so options #1 and #2 of master spec §1 do not
exist here. That leaves option #3 — **sitemap + detail pages** — over the two
surfaces where the Ministry actually publishes its vacancies:

1. **MOH Announcements archive** — `/en/Ministry/MediaCenter/Ads/Pages/*.aspx`,
   528 pages enumerated from `https://www.moh.gov.sa/en/SiteMap/sitemap.xml`.
   Roughly 40% are recruitment announcements ("MOH Announces Resident Dentist
   Jobs for Bachelor Degree Holders", "MOH Announces Cardiac Perfusion
   Technician Jobs for Diploma Holders"); the rest are tenders, e-consultations
   and prequalification results and are filtered out. Each page carries a
   Gregorian publish date (`<span id="pageDate">`), a body
   (`<div class="newscontent">`) with the application window, the required
   qualification, the specialties and the document requirements, and the apply
   link to the iRecruitment portal.
   Page 1 of the listing (`Ads/Pages/default.aspx`, no query string) is also
   read as a freshness check — it is the only listing page that may be fetched,
   see robots.txt below.

2. **Work For Us** — `/en/Ministry/About/Pages/Work-for-US.aspx` holds the
   Ministry's recruitment plan as two tables: currently running announcement
   tracks ("Physicians & Nursing — Since Early This Year — Still Running") and
   previous, expired ones, dated in **Hijri** ("14-8-1443H").

robots.txt
----------
`https://www.moh.gov.sa/robots.txt` disallows `/_layouts/`, `/_vti_bin/`,
`/mobile/`, `/*.aspx/` and — importantly — **`/*?PageIndex=`**, which is how the
announcements listing paginates. This scraper therefore never requests a
`PageIndex` URL: the archive is enumerated from the sitemap instead, and only
the unparameterised first listing page is fetched. Startup asserts both facts
against the live robots.txt and aborts if the detail pages ever become
disallowed.

Quirks
------
* **Announcement-level vacancies, not per-post listings.** One announcement
  opens a batch of posts for a whole specialty ("bachelor's degree in
  dentistry", "Prosthetics - Physiotherapy - Occupational Therapy - Speech
  Therapy"), Kingdom-wide. One CSV row = one announcement; the parsed
  specialties are kept in `specialties`.
* **Hijri dates.** Application windows are quoted in either Gregorian
  ("from Thursday, 05/10/2023 until Saturday, 14/10/2023") or Hijri
  ("from Sunday 10/10/1444 AH to Thursday 21/10/1444 AH", "3-6-1443H").
  Hijri dates are converted with the tabular/arithmetic Islamic calendar, which
  tracks the Umm al-Qura calendar the Ministry uses to **±1 day** — good enough
  for a posted/closing date, and the verbatim text is always kept in
  `application_window_raw`.
* **No salary anywhere.** MoH posts are paid on the public-sector pay scale but
  no announcement states a figure -> `salary_raw = "Not Disclosed"`, numeric
  fields empty (master spec §3). Nothing is invented.
* **No city.** Announcements are Kingdom-wide, so the club CSV falls back to
  the Ministry's seat, Riyadh (`DEFAULT_CITY`), and any region named in the body
  is kept in `region`.
* **`application_url` is the announcement page**, not the "click here" apply
  link: that link points at `erp.moh.gov.sa`, which is unreachable outside the
  Ministry's network. It is still captured verbatim in `apply_url`.
* **Two independent filters, in this order.**
  1. `classify_announcement()` answers "is this even a job ad?" — a crawl-side
     filter over the media-centre archive, which also carries tenders,
     e-consultations and results pages. Non-recruitment announcements are
     excluded and counted as `excluded_non_job`; recruitment-adjacent ones
     (training tracks, scholarships) are KEPT and flagged `needs_review`.
  2. `classify_job()` from `scrappers/_shared/classification.py` — the one
     shared two-level taxonomy — then decides scope and labels. `category` is
     "Non Clinical" | "Public Health" plus a `sub_category`; anything out of
     scope is dropped and counted as `excluded_out_of_scope`. The parsed
     `specialties` list is passed as the classifier's `skills` signal and stays
     in the rich CSV as a raw source column. Because MoH recruits mostly
     clinical staff Kingdom-wide, most announcements now fall out of scope.
  Both `needs_review` flags are OR'd together and logged to `needs_review.csv`.
* **Time window (master spec §4).** The archive goes back to 2011, so the first
  run keeps `INITIAL_WINDOW_DAYS = 365` days; later runs use the watermark
  (newest stored `posted_date` minus `WATERMARK_GRACE_DAYS = 2`). Because every
  archive URL carries its date in the slug (`ads-2023-10-04-001`), out-of-window
  pages are skipped **without being fetched** — a daily run costs a sitemap, one
  listing page, the Work For Us page and only the genuinely new announcements.
  Work For Us rows marked "Still Running" are always kept regardless of age.
* **`is_active`** is `true` while the parsed application window is still open (or
  the plan row says "Still Running"), `false` once it has closed; when no window
  is stated, an announcement counts as open for `ASSUMED_OPEN_DAYS = 30` days
  after its publish date. It is a **rich-CSV-only** lifecycle column now: the
  club schema retired `is_active`/`expires_at` fleet-wide, so neither is
  exported. `refresh_activity()` still re-evaluates it on every run, and the
  archive crawl still uses it to keep "Still Running" plan rows.

Outputs
-------
* moh_jobs.csv                          — rich cumulative store of the
                                          IN-SCOPE announcements (key: job_id)
* out-of-scope.csv                      — announcements classify_job rejected,
                                          archived verbatim so the decision
                                          stays reversible
* ../../jobs_csv/<DD-MM-YYYY>/moh.csv   — HealthCareers.club 22-column schema
* needs_review.csv                      — kept-but-flagged announcements

Migration note (25-08-2026): the 2 stored announcements were re-run through
the shared classifier — 0 kept, 2 moved to out-of-scope.csv ("Physicians &
Nursing" and a health-diploma graduate enrolment call). A 0-row steady state
is expected: the Ministry recruits almost entirely clinical staff.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "moh"
SITE_BASE = "https://www.moh.gov.sa"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SITEMAP_URL = SITE_BASE + "/en/SiteMap/sitemap.xml"
ADS_PATH = "/en/ministry/mediacenter/ads/pages/"
ADS_LISTING_URL = SITE_BASE + "/en/Ministry/MediaCenter/Ads/Pages/default.aspx"
WORK_FOR_US_URL = SITE_BASE + "/en/Ministry/About/Pages/Work-for-US.aspx"
# Never requested — robots.txt disallows "/*?PageIndex=". Kept as a constant so
# the startup check can prove the rule is still in force.
PAGINATED_LISTING_URL = ADS_LISTING_URL + "?PageIndex=2"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# The announcements archive spans 2011-2026, so the first run keeps one year;
# later runs use the watermark (master spec §4).
INITIAL_WINDOW_DAYS = 365
WATERMARK_GRACE_DAYS = 2
# An announcement with no stated closing date counts as open this long.
ASSUMED_OPEN_DAYS = 30

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_CONSECUTIVE_FAILURES = 3
DESCRIPTION_MAX_CHARS = 5_000

RICH_CSV = "moh_jobs.csv"
NEEDS_REVIEW_CSV = "needs_review.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COMPANY_NAME = "Ministry of Health, Saudi Arabia"
COMPANY_ABOUT = (
    "The Ministry of Health (MoH) is the Kingdom of Saudi Arabia's largest "
    "healthcare provider and regulator, running the public hospital and "
    "primary-healthcare network across all 20 health clusters and employing "
    "physicians, nurses, pharmacists and allied-health professionals "
    "Kingdom-wide."
)
COUNTRY_NAME = "Saudi Arabia"
COUNTRY_CODE = "SA"
COUNTRY_DIAL = "+966"
# Announcements are Kingdom-wide; the Ministry's seat is used as the fallback.
DEFAULT_CITY = "Riyadh"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "announcement_type",
    "specialties", "qualification", "region", "city", "country",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "category", "sub_category",
    "role_family", "all_families", "family_scores", "family_confidence",
    "matched_in", "needs_review",
    "posted_date", "application_opens", "application_closes",
    "application_window_raw", "experience_raw", "experience_min_years",
    "experience_max_years", "status", "is_active", "description",
    "apply_url", "job_url", "scraped_at",
]

log = logging.getLogger("moh_scraper")

# ----------------------------------------------------------------------------
# Text cleaning
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
# SharePoint renders its field labels ("Page Content") and the news date inside
# the content area as display:none blocks — invisible on the page, so they must
# never reach the body text.
_HIDDEN_RE = re.compile(
    r"""(?is)<(div|span|p)\b[^>]*style\s*=\s*['"][^'"]*"""
    r"""display\s*:\s*none[^'"]*['"][^>]*>.*?</\1\s*>""")
# A fragment cut at a length cap can end mid-tag; that stub is not text.
_TRUNCATED_TAG_RE = re.compile(r"<(?:/?[A-Za-z][^>]*)?$")
# SharePoint's rich-text fields are littered with zero-width joiners.
_ZERO_WIDTH_RE = re.compile(r"[​-‏﻿]")


def clean_text(text):
    """Collapse whitespace, unescape entities, drop zero-width characters."""
    plain = html_lib.unescape(str(text or "")).replace("\xa0", " ")
    return _WS_RE.sub(" ", _ZERO_WIDTH_RE.sub("", plain)).strip()


def html_to_text(fragment):
    """Flatten an HTML fragment to one plain-text paragraph.

    Invisible markup goes first — scripts/styles (the announcement body ships
    an inline jQuery slider) and display:none blocks (SharePoint's field
    labels) — then a dangling half-tag from a truncated fragment, and finally
    tags become spaces so sentences never run together.
    """
    markup = _SCRIPT_RE.sub(" ", str(fragment or ""))
    markup = _HIDDEN_RE.sub(" ", markup)
    markup = _TRUNCATED_TAG_RE.sub(" ", markup)
    return clean_text(_TAG_RE.sub(" ", markup))


# ----------------------------------------------------------------------------
# Hijri -> Gregorian (tabular Islamic calendar, ±1 day vs Umm al-Qura)
# ----------------------------------------------------------------------------

def hijri_to_gregorian(year, month, day):
    """Convert a Hijri date to a `datetime.date`, or None when out of range.

    Uses the arithmetic ("Kuwaiti") Islamic calendar via Julian Day Number:
    exact for many of the Ministry's dates and never more than a day off the
    Umm al-Qura calendar it prints, which is why `application_window_raw`
    always keeps the original text.
    """
    if not (1 <= month <= 12 and 1 <= day <= 30 and 1300 <= year <= 1600):
        return None
    jdn = ((11 * year + 3) // 30 + 354 * year + 30 * month
           - (month - 1) // 2 + day + 1948440 - 385)
    # Julian Day Number -> Gregorian (Fliegel & Van Flandern)
    l = jdn + 68569
    n = (4 * l) // 146097
    l -= (146097 * n + 3) // 4
    i = (4000 * (l + 1)) // 1461001
    l = l - (1461 * i) // 4 + 31
    j = (80 * l) // 2447
    g_day = l - (2447 * j) // 80
    l = j // 11
    g_month = j + 2 - 12 * l
    g_year = 100 * (n - 49) + i + l
    try:
        return date(g_year, g_month, g_day)
    except ValueError:                                   # pragma: no cover
        return None


# ----------------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

# "04 October 2023" as printed in <span id="pageDate">.
_PAGE_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
# Slug dates: ads-2023-10-04-001, Ads-2025-12-10-001, news-2025-06-26-001.
_SLUG_DATE_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")


def parse_page_date(text):
    """"04 October 2023" -> "2023-10-04"; "" when absent/unparseable."""
    match = _PAGE_DATE_RE.search(clean_text(text))
    if not match:
        return ""
    month = _MONTHS.get(match.group(2).lower())
    if not month:
        return ""
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return ""


def parse_slug_date(url_or_slug):
    """Date embedded in an announcement URL -> ISO string, "" when absent.

    Lets the run skip out-of-window archive pages without fetching them. A few
    slugs carry typo'd years ("ads-201-07-13-001"); those simply return "".
    """
    match = _SLUG_DATE_RE.search(str(url_or_slug or ""))
    if not match:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)),
                    int(match.group(3))).isoformat()
    except ValueError:
        return ""


# Application windows, e.g.
#   "starting from Thursday, 05/10/2023 until Saturday, 14/10/2023"
#   "from Sunday 10/10/1444 AH to Thursday 21/10/1444 AH"
#   "starting from Tuesday, 03/06/1444H, to Saturday, 07/06/1444H"
#   "applications will be opened on Thursday 01/06/1445 AH"
#   Work For Us table cells: "14-8-1443H", "24-5-1443H"
_DATE_TOKEN_RE = re.compile(
    r"\b(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(1[34]\d{2}|20\d{2})\s*"
    r"(AH|H\b|هـ)?", re.IGNORECASE)
# Hijri years used by the Ministry (1443H … 1460H) — a 4-digit token starting
# with 14 is Hijri even when the "H" suffix is missing.
_HIJRI_YEAR_RE = re.compile(r"^1[34]\d{2}$")


def parse_date_token(token, suffix=""):
    """One "d/m/y" (+ optional AH/H marker) -> (iso_date, is_hijri).

    Hijri is detected from the marker or from a 14xx year; day/month order is
    the Ministry's own d/m/y. Returns ("", False) when nothing parses.
    """
    day, month, year = token
    hijri = bool(suffix) or bool(_HIJRI_YEAR_RE.match(str(year)))
    if hijri:
        converted = hijri_to_gregorian(int(year), int(month), int(day))
        return (converted.isoformat() if converted else "", True)
    try:
        return (date(int(year), int(month), int(day)).isoformat(), False)
    except ValueError:
        return ("", False)


def parse_application_window(text):
    """Return (opens_iso, closes_iso, raw_snippet) from an announcement body.

    The first parsed date is the opening date, the second the closing date; a
    single date means "applications open then" and leaves the closing date
    empty. `raw_snippet` keeps the sentence verbatim so the ±1-day Hijri
    conversion is always auditable.
    """
    body = clean_text(text)
    if not body:
        return ("", "", "")
    matches = list(_DATE_TOKEN_RE.finditer(body))
    if not matches:
        return ("", "", "")
    parsed = []
    for match in matches:
        iso, _ = parse_date_token(match.group(1, 2, 3), match.group(4) or "")
        if iso:
            parsed.append((iso, match))
    if not parsed:
        return ("", "", "")
    opens, first = parsed[0]
    closes = parsed[1][0] if len(parsed) > 1 else ""
    if closes and closes < opens:                # printed end-first (it happens)
        opens, closes = closes, opens
    last = parsed[-1][1] if len(parsed) > 1 else first
    snippet = body[max(0, first.start() - 90):last.end() + 20].strip()
    return (opens, closes, snippet[:240])


# ----------------------------------------------------------------------------
# Announcement classification (master spec §2)
# ----------------------------------------------------------------------------

# A recruitment announcement — any of these in the title (or, for the weaker
# signals, the body) means "this is a job".
ALLOW_TITLE_KEYWORDS = (
    "vacanc", "vacant", "job", "jobs", "employment application",
    "recruit", "hiring", "appointment", "career", "posts for",
    "receiving applications", "seasonal staff", "resident dentist",
)
# Announcements that are definitely not recruitment.
DENY_TITLE_KEYWORDS = (
    "tender", "bid", "procurement", "prequalification", "pre-qualification",
    "qualified companies", "qualified firms", "qualification phase",
    "e-consultation", "consultation results", "consultation on", "survey",
    "checks", "cheques", "forum", "conference", "hackathon", "healththon",
    "privatization", "expression of interest", "eoi", "business plan",
    "purchase plan", "budget", "surplus", "results of consultation",
    "showcase your brand",
)
# Body-level evidence that an announcement really is about applying for a post.
_JOB_BODY_RE = re.compile(
    r"employment application|job application|receiv(e|ing) (employment |job )?"
    r"application|apply for this job|employment (portal|website)|"
    r"recruitment portal|job opening|vacanc|appointment of|"
    r"announces the availability of receiving", re.IGNORECASE)
# Recruitment-adjacent but not a plain vacancy (training tracks, scholarship
# programmes) — kept and flagged for review rather than dropped.
_REVIEW_BODY_RE = re.compile(
    r"training program|training track|registration for|scholarship|"
    r"wa'?ed track|diploma program", re.IGNORECASE)


def classify_announcement(title, body=""):
    """Return (is_job, needs_review) for one announcement.

    * a DENY keyword in the title wins — tenders and e-consultations are not
      jobs, whatever the body says;
    * an ALLOW keyword in the title, or clear application language in the body,
      makes it a job;
    * training/scholarship registrations, and jobs recognised only from the
      body, are kept but flagged `needs_review` (master spec §2 — never
      silently dropped).
    """
    title_l = clean_text(title).lower()
    body_l = clean_text(body).lower()
    if any(keyword in title_l for keyword in DENY_TITLE_KEYWORDS):
        return (False, False)
    title_hit = any(keyword in title_l for keyword in ALLOW_TITLE_KEYWORDS)
    body_hit = bool(_JOB_BODY_RE.search(body_l))
    review_hit = bool(_REVIEW_BODY_RE.search(title_l)
                      or _REVIEW_BODY_RE.search(body_l))
    if title_hit:
        return (True, review_hit and not body_hit)
    if body_hit or review_hit:
        return (True, True)
    return (False, False)


# ----------------------------------------------------------------------------
# Scope & labelling (shared two-level taxonomy)
# ----------------------------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    Runs AFTER `classify_announcement()` has established that the page is a
    job ad at all. The parsed `specialties` list is the curated `skills`
    signal; it stays in the rich CSV as a raw source column and never decides
    the category by itself.

    `needs_review` already carries the announcement classifier's own flag
    (recruitment-adjacent training/scholarship tracks), so the classifier's
    flag is OR'd onto it rather than replacing it.

    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("specialties", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = bool(verdict["needs_review"]) or bool(row.get("needs_review"))
    return verdict["in_scope"]


# ----------------------------------------------------------------------------
# Body field extraction
# ----------------------------------------------------------------------------

# "in the following specializations: (Prosthetics - Physiotherapy - …)",
# "in the specialty of (cardiac perfusion technician)", "degree in (dentistry)"
_SPECIALTY_RE = re.compile(
    r"(?:specializations?|specialt(?:y|ies)|specialisations?|field of|"
    r"degree in|diploma in|qualification in)\s*(?:of|in)?\s*[:\-]?\s*"
    r"\(([^)]{2,200})\)", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^)]{3,120})\)")

_QUALIFICATIONS = (
    ("phd", "PhD"), ("doctorate", "PhD"), ("master", "Master's degree"),
    ("fellowship", "Fellowship"), ("bachelor", "Bachelor's degree"),
    ("diploma", "Diploma"), ("consultant", "Consultant"),
    ("higher education", "Higher education"),
)

_REGION_RE = re.compile(
    r"\b(Riyadh|Makkah|Mecca|Madinah|Medina|Jeddah|Eastern Province|Dammam|"
    r"Asir|Aseer|Jazan|Jizan|Tabuk|Hail|Ha'il|Najran|Al Baha|Al-Baha|Qassim|"
    r"Al Qassim|Northern Borders|Al Jouf|Al-Jouf)\b", re.IGNORECASE)


def parse_specialties(body):
    """The specialties an announcement opens, as a "; "-joined string.

    Prefers the parenthesised list that follows a "specializations:" style
    lead-in, falling back to the first parenthesised phrase that is not a
    date/number. "" when the announcement names none.
    """
    text = clean_text(body)
    match = _SPECIALTY_RE.search(text)
    if not match:
        for candidate in _PAREN_RE.finditer(text):
            inner = candidate.group(1).strip()
            if not re.search(r"\d", inner) and len(inner.split()) <= 12:
                match = candidate
                break
    if not match:
        return ""
    parts = [p.strip(" .،") for p in re.split(r"\s*[-–—/,]\s*|\s+and\s+",
                                              match.group(1))]
    return "; ".join(p for p in parts if p)


def parse_qualification(title, body):
    """Required qualification level ("Bachelor's degree", "Diploma", …)."""
    haystack = "{} {}".format(clean_text(title), clean_text(body)).lower()
    for needle, label in _QUALIFICATIONS:
        if needle in haystack:
            return label
    return ""


def parse_region(body):
    """A Saudi region/city named in the body, "" when the post is nationwide."""
    match = _REGION_RE.search(clean_text(body))
    return match.group(1).title() if match else ""


# "10-12 years", "2 to 5 years", "5+ years", "two (2) years"
_EXP_RANGE_RE = re.compile(
    r"(\d{1,2})\s*\)?\s*(?:[-–—]|\bto\b)\s*(\d{1,2})\s*\)?\s*\+?\s*year",
    re.IGNORECASE)
_EXP_SINGLE_RE = re.compile(r"(\d{1,2})\s*\)?\s*\+?\s*year", re.IGNORECASE)
_EXP_CONTEXT_RE = re.compile(r"experien|minimum|at least", re.IGNORECASE)


def parse_experience(body):
    """Return (raw_snippet, min_years, max_years); ("", "", "") when unstated.

    Only fragments that mention experience/minimum/at least count, so the
    "period of ten days" application windows and Hijri years are ignored.
    """
    text = clean_text(body)
    if not text:
        return ("", "", "")
    for fragment in re.split(r"(?<=[.;•])\s+|\s{2,}", text):
        if not _EXP_CONTEXT_RE.search(fragment):
            continue
        match = _EXP_RANGE_RE.search(fragment)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if high < low:
                low, high = high, low
            return (fragment.strip()[:200], str(low), str(high))
        match = _EXP_SINGLE_RE.search(fragment)
        if match:
            return (fragment.strip()[:200], str(int(match.group(1))), "")
    return ("", "", "")


def parse_job_type(title, body):
    """club job_type. MoH appointments are full-time government posts; only an
    explicit part-time/temporary wording changes that."""
    haystack = "{} {}".format(clean_text(title), clean_text(body)).lower()
    if "part-time" in haystack or "part time" in haystack:
        return "part_time"
    return "full_time"


def compute_is_active(closes, posted_date, status="", today=None):
    """Is the announcement still open?

    "Still Running" plan rows are always active; otherwise the closing date
    decides, and when none was stated the announcement counts as open for
    ASSUMED_OPEN_DAYS after publication.
    """
    today = today or date.today()
    status_l = clean_text(status).lower()
    if "still running" in status_l or "running" == status_l:
        return True
    if "expired" in status_l or "closed" in status_l:
        return False
    if closes:
        try:
            return date.fromisoformat(closes) >= today
        except ValueError:
            pass
    if posted_date:
        try:
            return (date.fromisoformat(posted_date)
                    + timedelta(days=ASSUMED_OPEN_DAYS)) >= today
        except ValueError:
            pass
    return False


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


def check_robots(session):
    """Fetch robots.txt and hold this run to it (master spec §7).

    Aborts when the announcement detail pages, the sitemap or the Work For Us
    page become disallowed. The `PageIndex` listing pagination is *expected* to
    be disallowed; a warning is logged if that ever changes, but the scraper
    still never uses it (the sitemap is the enumeration source).
    """
    parser = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        response = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        sys.exit("Could not fetch {} ({}) — aborting rather than crawling "
                 "blind.".format(ROBOTS_URL, exc))
    parser.parse(response.text.splitlines())

    required = [SITEMAP_URL, ADS_LISTING_URL, WORK_FOR_US_URL,
                SITE_BASE + ADS_PATH + "ads-2023-10-04-001.aspx"]
    for url in required:
        if not parser.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    if parser.can_fetch(USER_AGENT, PAGINATED_LISTING_URL):
        log.warning("robots.txt no longer disallows ?PageIndex= listing pages; "
                    "this scraper still only uses the sitemap + page 1.")
    else:
        log.info("robots.txt check passed (?PageIndex= pagination disallowed, "
                 "not used)")
    return parser


def get_text(session, url, allow_404=False):
    """GET a page with retries/backoff; None when the page is unusable.

    The portal answers unknown/retired pages with a 200 redirect to a tiny
    static 404 page, so short bodies are treated as misses too.
    """
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d in %.0fs (%s) %s",
                        attempt, MAX_RETRIES, wait, last_error, url)
            time.sleep(wait)
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(response.status_code)
                continue
            if response.status_code == 404:
                if not allow_404:
                    log.warning("404 %s", url)
                return None
            response.raise_for_status()
            body = response.text
            if "/404/error.html" in response.url or len(body) < 2_000:
                log.debug("Retired or empty page: %s", url)
                return None
            return body
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)",
              url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

_SITEMAP_ENTRY_RE = re.compile(
    r"(?is)<url>\s*<loc>(.*?)</loc>(?:\s*<lastmod>(.*?)</lastmod>)?")


def discover_from_sitemap(session):
    """All announcement detail URLs from the English sitemap.

    Returns a list of {url, slug, slug_date, lastmod} newest-first (by slug
    date, which is what the time window uses).
    """
    body = get_text(session, SITEMAP_URL)
    if not body:
        log.error("Sitemap unavailable — falling back to listing page 1 only")
        return []
    entries = []
    for loc, lastmod in _SITEMAP_ENTRY_RE.findall(body):
        url = clean_text(loc)
        if ADS_PATH not in url.lower() or url.lower().endswith("default.aspx"):
            continue
        slug = urlparse(url).path.rsplit("/", 1)[-1]
        entries.append({
            "url": url,
            "slug": slug,
            "slug_date": parse_slug_date(slug),
            "lastmod": clean_text(lastmod)[:10],
            "listing_title": "",
            "listing_date": "",
        })
    entries.sort(key=lambda e: (e["slug_date"] or e["lastmod"] or ""),
                 reverse=True)
    log.info("Sitemap lists %d announcement pages", len(entries))
    return entries


_LISTING_ITEM_RE = re.compile(
    r'(?is)<li[^>]*>\s*<a[^>]+href="([^"]+Ads/Pages/[^"]+\.aspx)"[^>]*>.*?'
    r'<span class="date">(.*?)</span>\s*<span class="ntitle">(.*?)</span>')


def discover_from_listing(session):
    """The newest announcements from listing page 1 (title + date + URL).

    Page 1 has no query string, so robots.txt allows it; the `?PageIndex=`
    pages that would hold the rest of the archive are disallowed and never
    requested — that is what the sitemap is for.
    """
    body = get_text(session, ADS_LISTING_URL)
    if not body:
        log.warning("Listing page 1 unavailable")
        return []
    items = []
    for url, date_text, title in _LISTING_ITEM_RE.findall(body):
        absolute = urljoin(SITE_BASE, clean_text(url))
        slug = urlparse(absolute).path.rsplit("/", 1)[-1]
        items.append({
            "url": absolute,
            "slug": slug,
            "slug_date": parse_slug_date(slug),
            "lastmod": "",
            "listing_title": clean_text(title),
            "listing_date": parse_page_date(date_text),
        })
    log.info("Listing page 1 shows %d recent announcements", len(items))
    return items


def merge_candidates(sitemap_entries, listing_items):
    """One candidate per slug, listing metadata winning (it is authoritative
    for title and date), newest-first."""
    merged = {}
    for entry in sitemap_entries + listing_items:
        key = entry["slug"].lower()
        if key in merged:
            for field in ("listing_title", "listing_date", "lastmod",
                          "slug_date"):
                if entry.get(field) and not merged[key].get(field):
                    merged[key][field] = entry[field]
        else:
            merged[key] = dict(entry)
    candidates = list(merged.values())
    candidates.sort(
        key=lambda e: (e["listing_date"] or e["slug_date"] or e["lastmod"]
                       or ""), reverse=True)
    return candidates


# ----------------------------------------------------------------------------
# Announcement page parsing
# ----------------------------------------------------------------------------

_TITLE_TAG_RE = re.compile(r"(?is)<title>(.*?)</title>")
_PAGE_DATE_TAG_RE = re.compile(
    r'(?is)id="(?:pageDate|[^"]*lblDate)"[^>]*>(.*?)</span>')
# Both boundaries span whole tags: the opening one has to swallow the rest of
# `<div class="newscontent">` (otherwise its stray ">" opens the body text) and
# the closing one has to start back at "<" (otherwise a bare "<div" trails it).
_CONTENT_START_RE = re.compile(r'(?i)class="[^"]*\bnewscontent\b[^"]*"[^>]*>')
_CONTENT_END_RE = re.compile(
    r'(?is)<[A-Za-z][^>]*\bclass="(?:ms-hide|left_conts)"')
_APPLY_LINK_RE = re.compile(
    r'(?i)href="([^"]*(?:erp\.moh\.gov\.sa|IrcVisitor|employment[^"]*)[^"]*)"')
# Leading breadcrumb/label chrome: "> Page Content ", "Page Content: ", ...
_PAGE_LABEL_RE = re.compile(r"(?i)^[\s>|:.\u00b7\u2022\-\u2013]*"
                            r"(?:page content\b[\s>|:.\u00b7\u2022\-\u2013]*)+")


def parse_announcement(page_html, url, fallback_title="", fallback_date=""):
    """Pull the announcement's title, date, body text and apply link out of the
    SharePoint page. Missing pieces fall back to the listing/slug values."""
    title = fallback_title
    match = _TITLE_TAG_RE.search(page_html)
    if match:
        page_title = clean_text(match.group(1))
        # "MOH Announcements - <headline>"
        page_title = re.sub(r"^MOH Announcements\s*[-–]\s*", "", page_title)
        if page_title and not page_title.lower().startswith("moh announcements"):
            title = page_title
    if not title:
        title = fallback_title

    posted_date = ""
    match = _PAGE_DATE_TAG_RE.search(page_html)
    if match:
        posted_date = parse_page_date(html_to_text(match.group(1)))
    if not posted_date:
        posted_date = fallback_date or parse_slug_date(url)

    body = ""
    start = _CONTENT_START_RE.search(page_html)
    if start:
        end = _CONTENT_END_RE.search(page_html, start.end())
        fragment = page_html[start.end():end.start() if end else start.end() + 12_000]
        body = html_to_text(fragment)
        # Belt and braces: some pages render the field label visibly.
        body = _PAGE_LABEL_RE.sub("", body)

    apply_url = ""
    if start:
        link = _APPLY_LINK_RE.search(
            page_html[start.end():(start.end() + 12_000)])
        if link:
            apply_url = urljoin(SITE_BASE, html_lib.unescape(link.group(1)))

    return {"title": title, "posted_date": posted_date, "body": body,
             "apply_url": apply_url}


def build_rich_row(candidate, parsed, needs_review):
    body = parsed["body"]
    title = parsed["title"]
    specialties = parse_specialties(body)
    opens, closes, window_raw = parse_application_window(body)
    experience_raw, exp_min, exp_max = parse_experience(body)
    posted_date = parsed["posted_date"]
    active = compute_is_active(closes, posted_date)

    return {
        "source": SITE,
        "job_id": candidate["slug"].replace(".aspx", ""),
        "title": title,
        "company": COMPANY_NAME,
        "announcement_type": "announcement",
        "specialties": specialties,
        "qualification": parse_qualification(title, body),
        "region": parse_region(body),
        "city": "",
        "country": COUNTRY_NAME,
        # MoH posts follow the public-sector pay scale but no announcement
        # states a figure — capture, never invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": parse_job_type(title, body),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        # seeded with classify_announcement()'s flag; apply_classification()
        # ORs the shared classifier's own needs_review onto it
        "needs_review": bool(needs_review),
        "posted_date": posted_date,
        "application_opens": opens,
        "application_closes": closes,
        "application_window_raw": window_raw,
        "experience_raw": experience_raw,
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "status": "open" if active else "closed",
        "is_active": "true" if active else "false",
        "description": body[:DESCRIPTION_MAX_CHARS],
        "apply_url": parsed["apply_url"],
        "job_url": candidate["url"],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----------------------------------------------------------------------------
# Work For Us recruitment plan
# ----------------------------------------------------------------------------

_TABLE_RE = re.compile(r"(?is)<table.*?</table>")
_ROW_RE = re.compile(r"(?is)<tr.*?</tr>")
_CELL_RE = re.compile(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>")
_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")


def slugify(text):
    return _SLUGIFY_RE.sub("-", clean_text(text).lower()).strip("-")[:60]


def parse_work_for_us(page_html, today=None):
    """Recruitment-plan rows from Work-for-US.aspx.

    Each table row is "announcement | start date | end date | status" with
    Hijri dates; the running-plan table's dates are prose ("Since Early This
    Year"), which simply leaves the dates empty and lets `status` drive
    `is_active`. Header rows and empty rows are skipped.
    """
    today = today or date.today()
    rows = []
    for table in _TABLE_RE.findall(page_html):
        for raw_row in _ROW_RE.findall(table):
            cells = [clean_text(html_to_text(c))
                     for c in _CELL_RE.findall(raw_row)]
            cells = [c for c in cells if c != ""]
            if len(cells) < 2:
                continue
            announcement = cells[0]
            if announcement.lower().startswith("announcement"):
                continue                                  # header row
            start_raw = cells[1] if len(cells) > 1 else ""
            end_raw = cells[2] if len(cells) > 2 else ""
            status = cells[3] if len(cells) > 3 else ""
            rows.append({
                "announcement": announcement,
                "start_raw": start_raw,
                "end_raw": end_raw,
                "status": status,
                "opens": _plan_date(start_raw),
                "closes": _plan_date(end_raw),
            })
    return rows


def _plan_date(text):
    """"14-8-1443H" -> ISO date; prose like "Since Early This Year" -> ""."""
    match = _DATE_TOKEN_RE.search(clean_text(text))
    if not match:
        return ""
    iso, _ = parse_date_token(match.group(1, 2, 3), match.group(4) or "")
    return iso


def build_plan_row(plan, first_seen):
    """A Work For Us plan row as a rich CSV row.

    Rows without a parseable start date (the running plan says "Since Early
    This Year") take the first-seen date as `posted_date`, the same convention
    the nhm/profco scrapers use for date-less sources.
    """
    title = plan["announcement"]
    active = compute_is_active(plan["closes"], plan["opens"] or first_seen,
                               plan["status"])
    window_raw = " ".join(part for part in
                          (plan["start_raw"], plan["end_raw"], plan["status"])
                          if part)
    return {
        "source": SITE,
        "job_id": "workforus-{}-{}".format(
            plan["opens"] or slugify(plan["start_raw"]) or "current",
            slugify(title)),
        "title": title,
        "company": COMPANY_NAME,
        "announcement_type": "recruitment_plan",
        "specialties": "; ".join(
            p.strip() for p in re.split(r"\s*&\s*|\s*/\s*", title) if p.strip()),
        "qualification": parse_qualification(title, ""),
        "region": "",
        "city": "",
        "country": COUNTRY_NAME,
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": "full_time",
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "posted_date": plan["opens"] or first_seen,
        "application_opens": plan["opens"],
        "application_closes": plan["closes"],
        "application_window_raw": window_raw,
        "experience_raw": "",
        "experience_min_years": "",
        "experience_max_years": "",
        "status": clean_text(plan["status"]).lower() or
                  ("open" if active else "closed"),
        "is_active": "true" if active else "false",
        "description": (
            "Ministry of Health recruitment-plan announcement: {}. "
            "Announcement period: {}. Applications are submitted through the "
            "MOH Recruitment Portal.".format(title, window_raw or "not stated")),
        "apply_url": "",
        "job_url": WORK_FOR_US_URL,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----------------------------------------------------------------------------
# Club export
# ----------------------------------------------------------------------------

def rich_row_to_club_row(row):
    def val(key):
        value = row.get(key, "")
        return "" if value is None or pd.isna(value) else str(value)

    return {
        "country_name": val("country") or COUNTRY_NAME,
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        # announcements are Kingdom-wide; the region is used when one is named,
        # otherwise the Ministry's seat (documented fallback, never invented pay)
        "city_name": val("region") or val("city") or DEFAULT_CITY,
        "company_name": COMPANY_NAME,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": COMPANY_ABOUT,
        "title": val("title"),
        "description": val("description"),
        "job_type": val("job_type") or "full_time",
        "category": val("category"),
        "sub_category": val("sub_category"),
        # the "click here" apply link points at erp.moh.gov.sa, which is not
        # reachable from outside the Ministry — the announcement page is
        "application_url": val("job_url"),
        "posted_at": val("posted_date"),
        "min_experience": val("experience_min_years"),
        "max_experience": val("experience_max_years"),
        # the announcement's own stated qualification level, else a grounded
        # extraction from the body — never inferred
        "qualification": val("qualification") or extract_qualification(
            val("description")),
        # no salary is ever stated (master spec §3)
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        # is_active / application_closes stay in the rich CSV only: the club
        # schema retired is_active and expires_at fleet-wide
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, initial_window_days=INITIAL_WINDOW_DAYS,
                   today=None):
    """Watermark cutoff (ISO date), or None to keep everything.

    First run: today - `initial_window_days` (None => the whole archive).
    Later runs: newest stored posted_date - WATERMARK_GRACE_DAYS.
    """
    today = today or date.today()
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"],
                               errors="coerce").dropna()
        if not dates.empty:
            return (dates.max()
                    - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    if initial_window_days is not None:
        return (today - timedelta(days=initial_window_days)).isoformat()
    return None


def within_window(candidate_date, cutoff):
    """Keep a candidate whose date is >= cutoff, or whose date is unknown (it
    then gets fetched and re-checked against the page's own date)."""
    if cutoff is None or not candidate_date:
        return True
    return candidate_date >= cutoff


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def write_club_csv(rich_df, run_date):
    rows = [rich_row_to_club_row(row) for _, row in rich_df.iterrows()]
    club_df = pd.DataFrame(rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def refresh_activity(combined_df):
    """Re-evaluate `is_active`/`status` on stored rows so closed application
    windows stop being exported as open (the archive is historical)."""
    if combined_df is None or not len(combined_df):
        return combined_df
    for column in ("application_closes", "posted_date", "status"):
        if column not in combined_df.columns:
            combined_df[column] = ""
    active = [
        compute_is_active(
            "" if pd.isna(closes) else str(closes),
            "" if pd.isna(posted) else str(posted),
            "" if pd.isna(status) else str(status))
        for closes, posted, status in zip(combined_df["application_closes"],
                                          combined_df["posted_date"],
                                          combined_df["status"])
    ]
    combined_df["is_active"] = ["true" if flag else "false" for flag in active]
    combined_df["status"] = [
        stored if str(stored).lower() in ("still running", "expired") else
        ("open" if flag else "closed")
        for stored, flag in zip(combined_df["status"], active)]
    return combined_df


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape Saudi Ministry of Health (moh.gov.sa) recruitment "
                    "announcements and its Work For Us recruitment plan.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="fetch at most N announcement detail pages "
                             "(test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new job rows (test runs)")
    parser.add_argument("--initial-days", type=int, default=INITIAL_WINDOW_DAYS,
                        metavar="N",
                        help="first-run window in days; 0 = whole archive "
                             "(default: %(default)s)")
    parser.add_argument("--no-workforus", action="store_true",
                        help="skip the Work For Us recruitment-plan tables")
    parser.add_argument("--no-archive", action="store_true",
                        help="skip the announcements archive (plan rows only)")
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
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    initial_window = args.initial_days if args.initial_days else None
    cutoff = compute_cutoff(existing_df, initial_window)
    log.info("Existing CSV has %d known rows; cutoff: %s",
             len(known_ids), cutoff or "none (whole archive)")

    counters = {"candidates": 0, "fetched": 0, "excluded_old": 0,
                "excluded_non_job": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0,
                "duplicates": 0, "errors": 0, "plan_rows": 0}
    new_rows, review_log = [], []
    today_iso = date.today().isoformat()

    # ---- 1. Work For Us recruitment plan ----
    if not args.no_workforus:
        page = get_text(session, WORK_FOR_US_URL)
        if page is None:
            log.warning("Work For Us page unavailable; skipping plan rows")
        else:
            for plan in parse_work_for_us(page):
                try:
                    row = build_plan_row(plan, today_iso)
                except Exception as exc:                 # never kill the run
                    log.warning("Skipping malformed plan row %s: %s", plan, exc)
                    counters["errors"] += 1
                    continue
                counters["plan_rows"] += 1
                if row["job_id"] in known_ids:
                    counters["duplicates"] += 1
                    continue
                # "Still Running" tracks are kept whatever their age
                if (row["is_active"] != "true"
                        and not within_window(row["posted_date"], cutoff)):
                    counters["excluded_old"] += 1
                    continue
                if not apply_classification(row):
                    counters["excluded_out_of_scope"] += 1
                    continue
                if row["needs_review"]:
                    counters["needs_review"] += 1
                    review_log.append({"job_id": row["job_id"],
                                       "title": row["title"],
                                       "reason": "out-of-scope-looking title",
                                       "job_url": row["job_url"]})
                known_ids.add(row["job_id"])
                new_rows.append(row)
                counters["new"] += 1
            log.info("Work For Us: %d recruitment-plan rows",
                     counters["plan_rows"])

    # ---- 2. Announcements archive ----
    if not args.no_archive:
        candidates = merge_candidates(discover_from_sitemap(session),
                                      discover_from_listing(session))
        counters["candidates"] = len(candidates)
        consecutive_failures = 0

        for candidate in candidates:
            if args.limit is not None and counters["new"] >= args.limit:
                log.info("Reached --limit=%d", args.limit)
                break
            if args.max_pages is not None and counters["fetched"] >= args.max_pages:
                log.info("Reached --max-pages=%d", args.max_pages)
                break

            job_id = candidate["slug"].replace(".aspx", "")
            if job_id in known_ids:
                counters["duplicates"] += 1
                continue
            # the slug/listing date lets old pages be skipped un-fetched
            known_date = candidate["listing_date"] or candidate["slug_date"]
            if not within_window(known_date, cutoff):
                counters["excluded_old"] += 1
                continue
            # cheap title-only rejection for tenders/e-consultations
            if candidate["listing_title"]:
                is_job, _ = classify_announcement(candidate["listing_title"])
                if not is_job and not _JOB_BODY_RE.search(
                        candidate["listing_title"]):
                    counters["excluded_non_job"] += 1
                    continue

            page = get_text(session, candidate["url"])
            counters["fetched"] += 1
            if page is None:
                consecutive_failures += 1
                counters["errors"] += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("%d consecutive page failures — stopping the "
                              "archive crawl", consecutive_failures)
                    break
                continue
            consecutive_failures = 0

            try:
                parsed = parse_announcement(
                    page, candidate["url"],
                    fallback_title=candidate["listing_title"],
                    fallback_date=candidate["listing_date"])
            except Exception as exc:                     # never kill the run
                log.warning("Could not parse %s: %s", candidate["url"], exc)
                counters["errors"] += 1
                continue

            if not parsed["title"]:
                log.warning("No title on %s; skipping", candidate["url"])
                counters["errors"] += 1
                continue
            # the page's own date is authoritative
            if not within_window(parsed["posted_date"], cutoff):
                counters["excluded_old"] += 1
                continue

            is_job, review = classify_announcement(parsed["title"],
                                                   parsed["body"])
            if not is_job:
                counters["excluded_non_job"] += 1
                log.debug("Not a job announcement: %s", parsed["title"])
                continue

            try:
                row = build_rich_row(candidate, parsed, review)
            except Exception as exc:
                log.warning("Skipping malformed announcement %s: %s",
                            candidate["url"], exc)
                counters["errors"] += 1
                continue

            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                log.debug("Out of scope: %s", parsed["title"])
                continue

            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({
                    "job_id": row["job_id"], "title": row["title"],
                    "reason": "recruitment-adjacent or out-of-scope-looking title",
                    "job_url": row["job_url"]})
            known_ids.add(job_id)
            new_rows.append(row)
            counters["new"] += 1
            log.info("+ %s (%s)", row["title"][:80], row["posted_date"])

    # ---- rich cumulative CSV ----
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
    else:
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
    if len(combined):
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        combined = refresh_activity(combined)
        for column in RICH_COLUMNS:
            if column not in combined.columns:
                combined[column] = ""
        combined = combined[[c for c in RICH_COLUMNS if c in combined.columns]]
        combined = combined.sort_values("posted_date", ascending=False,
                                        na_position="last")
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        log.info("No rows to write; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, count = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, count)

    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d rows)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Announcements known:      {:>5,}".format(counters["candidates"]))
    print("Detail pages fetched:     {:>5,}".format(counters["fetched"]))
    print("Recruitment-plan rows:    {:>5,}".format(counters["plan_rows"]))
    print("Excluded (not a job):     {:>5,}".format(counters["excluded_non_job"]))
    print("Excluded (out of scope):  {:>5,}".format(
        counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff or "no cutoff",
                                                    counters["excluded_old"]))
    print("Flagged needs_review:     {:>5,}".format(counters["needs_review"]))
    print("New jobs added:           {:>5,}".format(counters["new"]))
    print("Duplicates skipped:       {:>5,}".format(counters["duplicates"]))
    print("Errors (skipped rows):    {:>5,}".format(counters["errors"]))


if __name__ == "__main__":
    main()
