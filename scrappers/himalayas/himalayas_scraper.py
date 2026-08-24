#!/usr/bin/env python3
"""Scrape REMOTE clinical-research / pharma-regulatory jobs from himalayas.app.

Why this source
---------------
This scraper was commissioned as "remote healthcare jobs from Glassdoor".
Glassdoor is not scrapeable within the master spec: its robots.txt disallows
/graph, /api/ and /api-web/ (the whole JSON layer), all SERP pagination
(/Job/*_IP*, /Jobs/*_P*.htm*) and the job detail pages (/job-listing/*_IE*.htm),
and it carries an explicit `Disallow: /` block for ClaudeBot / anthropic-ai /
GPTBot. On top of that every URL — including https://www.glassdoor.co.in/
index.htm — answers HTTP 403 with a "Security | Glassdoor" interstitial to any
non-browser client. himalayas.app was chosen as the replacement: it is a
remote-only board, so REMOTE is guaranteed at the source rather than inferred.

Data source
-----------
Public JSON feed, newest-first, offset paginated, 20 jobs per page:

    GET https://himalayas.app/jobs/api?offset=N
    -> {updatedAt, offset, limit, totalCount, jobs: [...20...]}

Job fields: title, excerpt, companyName, companySlug, companyLogo,
employmentType (Full Time/Part Time/Contractor/Intern/Other/Temporary/
Volunteer), minSalary, maxSalary, salaryPeriod (annual/hourly/monthly),
currency, seniority, locationRestrictions, timezoneRestrictions, categories,
parentCategories, description (HTML), pubDate + expiryDate (unix epoch
seconds), applicationLink, guid.

Verified quirks:
* pubDate is strictly descending across offsets, so the watermark early-stop
  works and we never crawl the whole 94k-job archive.
* The feed accepts NO filtering — category=, search=, q=, market= etc. are all
  silently ignored and return the identical unfiltered page. Healthcare has to
  be selected client-side (see below).
* limit= is ignored too; pages are always 20.
* "None" arrives as the *string* "None", not JSON null, in minSalary /
  maxSalary / currency. Treat it as missing.
* List-ish fields (categories, parentCategories, seniority,
  locationRestrictions) are Python-repr STRINGS — "['United States']" — not
  JSON arrays. They are parsed with ast.literal_eval.

robots.txt: `User-Agent: * / Allow: / / Disallow: /apply`. /jobs/api is
allowed; /apply is never requested. A plain descriptive User-Agent is accepted
— no browser impersonation needed.

Scope filter (master spec §2)
-----------------------------
Only eleven role families are in scope:

    Public Health · Clinical Data Management · Clinical Research ·
    Medical Writer · TMF · Medical Coding · Pharmacovigilance ·
    Regulatory Affairs · Medical Reviewer · MSL · HEOR

The gate matches the JOB TITLE against ROLE_FAMILIES; the first family that
matches labels the row in `role_family`. Everything else is counted
`excluded_out_of_scope` — this is a general job board and ~88% of even the
old broad healthcare catch was out of scope for these families.

The spec prefers source-side filtering, and this feed *looks* like it offers
it (there are exact `categories` slugs such as Clinical-Data-Management and
Pharmacovigilance), but measured against 7,930 stored jobs roughly half the
slug-only admissions were wrong: "Registered Dietitian" tagged
Clinical-Research, "Nurse Practitioner" tagged Public-Health, "Open
Application" tagged Medical-Affairs. The tagging is automated and loose, so
slugs are NOT an admission path — the employer's own title is.

A title carrying an in-scope term inside a plainly different profession
(legal counsel, quota-carrying sales, recruiting, engineering) is KEPT,
flagged needs_review and logged to needs_review.csv — never silently
dropped, per the spec.

Salary (master spec §3: capture, don't filter)
----------------------------------------------
Nothing is ever excluded for its salary. minSalary/maxSalary are stored
verbatim with their real currency in the rich CSV. The club schema's
salary_currency enum only allows INR/USD and salary_period only per_annum/
per_month, so club salary columns are populated only for USD/INR annual or
monthly pay; CAD/EUR/GBP/PLN/... and hourly rates keep their values in the
rich CSV and leave the club columns empty. Never invented.

Outputs (per the repo README + master-scraper-spec.md)
------------------------------------------------------
* himalayas_jobs.csv        — rich cumulative store (dedup key: job_id),
                              source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/himalayas.csv
                            — the same jobs mapped to the shared
                              HealthCareers.club 22-column schema.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS (2);
later runs keep only jobs newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

Run `python himalayas_scraper.py --help` for options.
"""

import argparse
import ast
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

SITE = "himalayas"
SITE_BASE = "https://himalayas.app"
ROBOTS_URL = SITE_BASE + "/robots.txt"
API_URL = SITE_BASE + "/jobs/api"

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

# First-run window. The master spec §4 default is 7, deliberately narrowed to
# 2: this feed posts ~2,400 jobs/day, so every extra day of first-run window
# costs ~2,400 offsets (~9 min of crawl) for jobs that are already stale by
# the time they are imported. Later runs ignore this entirely and use the
# watermark instead.
INITIAL_WINDOW_DAYS = 2
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 20                 # server-fixed; limit= is ignored
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
MAX_OFFSET = 40_000            # hard stop; the archive is ~95k jobs deep
# Safety valve against a pathological row, NOT a content budget. Real
# descriptions run 600-9,900 plain-text chars (median ~3,900), so this never
# fires in practice — an earlier 3,000 cap silently truncated 84% of rows
# mid-word. Raise rather than lower if the feed ever grows longer posts.
DESCRIPTION_MAX_CHARS = 20_000

# Every listing on himalayas.app is remote — that is the whole premise of the
# board — so the club job_type enum value is always "remote" (the enum cannot
# express "remote AND part-time"; employmentType is kept in the rich CSV).
CLUB_JOB_TYPE = "remote"

RICH_CSV = str(Path(__file__).resolve().parent / "himalayas_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_slug", "company_logo",
    "locations", "country", "salary_raw", "salary_min", "salary_max",
    "salary_currency", "salary_period", "employment_type", "work_mode",
    "seniority", "role_family", "company_type", "match_signal",
    "categories",
    "parent_categories", "timezones", "posted_date", "expires_date",
    "description", "job_url", "scraped_at",
]

# The 21-column contract from job_samples.csv (repo README: "Every scraper
# MUST write the same columns as job_samples.csv"). NOTE: this changed after
# this scraper was first written — `qualification` was added and `is_active` /
# `expires_at` were removed. Verified against jobs_csv/19-08-2026/
# pharmabharat_categories.csv. The scraper still stores `expires_date` in the
# rich CSV; it simply no longer has a column in the club export.
CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience", "qualification",
    "min_salary", "max_salary", "salary_period", "salary_currency",
]

# Credentials to lift verbatim out of a description for `qualification`.
# Only explicit, unambiguous tokens — never inferred from the job title.
# Bare "MD" and "DO" are deliberately absent: "Remote, MD" is Maryland and
# "do" is an English verb. "M.D." with periods and "MD/DO" are safe.
_QUALIFICATION_PATTERNS = [
    (r"\bMBBS\b", "MBBS"),
    (r"\bM\.D\.|\bMD\s*/\s*DO\b|\bMD\s+degree\b", "MD"),
    (r"\bPharm\.?\s?D\b", "PharmD"),
    (r"\bB\.?\s?Pharm\b", "B.Pharm"),
    (r"\bM\.?\s?Pharm\b", "M.Pharm"),
    (r"\bPh\.?\s?D\b", "PhD"),
    (r"\bMPH\b", "MPH"),
    (r"\bDVM\b", "DVM"),
    (r"\bBSN\b", "BSN"),
    (r"\bMSN\b", "MSN"),
    (r"\bM\.?Sc\b|\bMaster of Science\b", "MSc"),
    (r"\bB\.?Sc\b|\bBachelor of Science\b", "BSc"),
    (r"\bMBA\b", "MBA"),
    (r"\bRN\b|\bRegistered Nurse\b", "RN"),
    (r"\bRAC\b", "RAC"),
    (r"\bCCRA\b", "CCRA"),
    (r"\bCCRP\b", "CCRP"),
    (r"\bRHIA\b", "RHIA"),
    (r"\bRHIT\b", "RHIT"),
    (r"\bCPC\b", "CPC"),
    (r"\bCCS\b", "CCS"),
    (r"\bCDISC\b", "CDISC"),
    (r"\bBachelor'?s?\s+degree\b", "Bachelor's degree"),
    (r"\bMaster'?s?\s+degree\b", "Master's degree"),
    (r"\bDoctorate\b|\bDoctoral degree\b", "Doctorate"),
    (r"\blife sciences?\b", "Life Sciences"),
]
_QUALIFICATION_RES = [(re.compile(p, re.IGNORECASE), label)
                      for p, label in _QUALIFICATION_PATTERNS]
MAX_QUALIFICATIONS = 8

# country name -> (ISO alpha-2, dial code). Covers the countries that actually
# appear in the feed; anything unseen exports with empty code/dial code.
COUNTRY_CODES = {
    "United States": ("US", "+1"), "Canada": ("CA", "+1"),
    "United Kingdom": ("GB", "+44"), "Ireland": ("IE", "+353"),
    "Germany": ("DE", "+49"), "France": ("FR", "+33"),
    "Spain": ("ES", "+34"), "Portugal": ("PT", "+351"),
    "Netherlands": ("NL", "+31"), "Belgium": ("BE", "+32"),
    "Italy": ("IT", "+39"), "Poland": ("PL", "+48"),
    "Sweden": ("SE", "+46"), "Norway": ("NO", "+47"),
    "Denmark": ("DK", "+45"), "Finland": ("FI", "+358"),
    "Switzerland": ("CH", "+41"), "Austria": ("AT", "+43"),
    "Czechia": ("CZ", "+420"), "Romania": ("RO", "+40"),
    "Greece": ("GR", "+30"), "Ukraine": ("UA", "+380"),
    "India": ("IN", "+91"), "Pakistan": ("PK", "+92"),
    "Philippines": ("PH", "+63"), "Singapore": ("SG", "+65"),
    "Australia": ("AU", "+61"), "New Zealand": ("NZ", "+64"),
    "Japan": ("JP", "+81"), "Indonesia": ("ID", "+62"),
    "Malaysia": ("MY", "+60"), "Vietnam": ("VN", "+84"),
    "Mexico": ("MX", "+52"), "Brazil": ("BR", "+55"),
    "Argentina": ("AR", "+54"), "Colombia": ("CO", "+57"),
    "Chile": ("CL", "+56"), "Peru": ("PE", "+51"),
    "Costa Rica": ("CR", "+506"), "Uruguay": ("UY", "+598"),
    "South Africa": ("ZA", "+27"), "Nigeria": ("NG", "+234"),
    "Kenya": ("KE", "+254"), "Egypt": ("EG", "+20"),
    "United Arab Emirates": ("AE", "+971"), "Saudi Arabia": ("SA", "+966"),
    "Israel": ("IL", "+972"), "Turkey": ("TR", "+90"),
}

SALARY_PERIODS = {"annual": "per_annum", "monthly": "per_month",
                  "hourly": "per_hour", "weekly": "per_week",
                  "daily": "per_day"}

log = logging.getLogger("himalayas_scraper")

# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------

def parse_listish(value):
    """The feed ships lists as Python-repr strings: "['United States']".

    Returns a list of strings for any of the string / real-list / empty forms.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value or value in ("None", "[]"):
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return []
    if isinstance(parsed, (list, tuple)):
        return [str(v) for v in parsed]
    return [str(parsed)]


def clean_value(value):
    """Feed sends missing numbers/strings as the literal string "None"."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("None", "nan", "") else text


def epoch_to_date(value):
    """1785311944 -> "2026-07-29"; "" for anything unparseable."""
    text = clean_value(value)
    if not text:
        return ""
    try:
        seconds = int(float(text))
    except ValueError:
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d)[^>]*>", " ", text or "",
                  flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_description(text, limit=DESCRIPTION_MAX_CHARS):
    """Cap a description at `limit` chars on a word boundary.

    Truncation is marked with a trailing "…" so a shortened description is
    never mistaken for a complete one, and never cuts mid-word.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    # Prefer the word boundary; fall back to the hard cut only when the text
    # is one pathological unbroken token and backing off would lose most of it.
    if len(spaced) >= limit * 0.5:
        cut = spaced
    return cut.rstrip() + "…"


def job_id_from_guid(guid):
    """.../jobs/android-software-engineer-5243463048 -> "5243463048".

    Most guids carry no numeric id, so the fallback is <company>/<job-slug>
    rather than the bare job slug — two employers both posting a
    "registered-nurse" would otherwise share a dedup key and one would be
    silently discarded.
    """
    guid = clean_value(guid)
    if not guid:
        return ""
    path = guid.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    match = re.search(r"(\d{6,})$", slug)
    if match:
        return match.group(1)
    # /companies/<company>/jobs/<slug> -> "<company>/<slug>"
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "jobs" and len(parts) >= 4:
        return "{}/{}".format(parts[-3], slug)
    return slug or guid


def parse_salary(min_salary, max_salary, currency, period):
    """Master spec §3 — capture, never filter, never invent.

    (120000, 150000, "USD", "annual") -> ("USD 120000 - 150000 per annum",
                                          "120000", "150000", "USD", "per_annum")
    ("None", "None", "None", "annual") -> ("Not Disclosed", "", "", "", "")
    """
    lo_text, hi_text = clean_value(min_salary), clean_value(max_salary)
    currency = clean_value(currency)
    period_key = clean_value(period).lower()
    period = SALARY_PERIODS.get(period_key, "")

    def as_int(text):
        try:
            return str(int(round(float(text))))
        except (TypeError, ValueError):
            return ""

    lo, hi = as_int(lo_text), as_int(hi_text)
    if not lo and not hi:
        return ("Not Disclosed", "", "", "", "")
    if not lo:
        lo = hi
    if not hi:
        hi = lo
    if int(hi) < int(lo):
        lo, hi = hi, lo
    raw = "{} {} - {}{}".format(currency or "?", lo, hi,
                                " " + period.replace("_", " ") if period else "")
    return (raw, lo, hi, currency, period)


# ----------------------------------------------------------------------------
# Role-family scope (replaces the old broad "is it healthcare?" gate)
# ----------------------------------------------------------------------------
#
# The site is scoped to eleven clinical-research / pharma-regulatory role
# families. A job is IN SCOPE only if its TITLE matches one of them.
#
# Why title-only, and not the feed's own `categories` slugs:
# the slugs looked like the ideal source-side signal (there are exact tags like
# Clinical-Data-Management, Pharmacovigilance, Medical-Science-Liaison), but
# measured against 7,930 stored jobs they admit mostly noise — the tagging is
# automated and loose. Slug-only admissions included "Registered Dietitian" and
# "Lead Account Manager, Clinical Services" under Clinical-Research, "Nurse
# Practitioner (Remote, SC License Required)" and "Perinatal Social Worker"
# under Public-Health, and "Open Application" under Medical-Affairs. Roughly
# half the slug-only matches were wrong, so they are not an admission path.
# The job title is the employer's own wording and is far more reliable.
#
# Order is precedence: the FIRST family that matches labels the job, so the
# specific families come before the broad ones (a "Medical Writer - Clinical
# Regulatory Documentation" is a Medical Writer, not Regulatory Affairs or
# Clinical Research).

ROLE_FAMILIES = [
    ("TMF",
     r"\btmf\b|trial master file"),

    ("HEOR",
     # Market access is the standard industry pairing with HEOR.
     r"\bheor\b|health econom\w*|outcomes research|\bhta\b|"
     r"market access|payer (?:evidence|value|strateg\w*)|"
     r"real[- ]world evidence|\brwe\b|health technology assessment"),

    ("Medical Reviewer",
     # Pharma sense only: medical monitoring / medical review at sponsors and
     # CROs. Deliberately EXCLUDES the two adjacent US markets this feed is
     # full of, which are a different job despite the shared words:
     #   * payer-side utilization review/management (43 roles: "Utilization
     #     Review Nurse-LVN/LPN", "Director of Utilization Management")
     #   * IME / disability peer review ("Board Certified Physician Reviewer -
     #     Orthopedic Spine Surgery", "Physician Disability Peer Reviewer")
     #   * MRO (Medical Review Officer) — workplace drug-testing
     r"medical review\w*|medical monitor\w*|clinical reviewer|"
     r"safety reviewer|medical safety review"),

    ("MSL",
     # "field medical" is NOT here: in this feed it appears in IME titles
     # like "Physician Reviewer - Field Medical Director, Radiology", which
     # are disability review, not MSL work.
     r"medical science liaison|\bmsl\b|medical affairs|scientific affairs|"
     r"medical advisor"),

    ("Pharmacovigilance",
     r"pharmacovigilance|\bpv\b|drug safety|\bgvp\b|"
     r"adverse event|safety (?:physician|scientist|officer|surveillance|"
     r"domain|database)|aggregate report\w*|signal detection|case processing|"
     r"\bpsur\b|\bpbrer\b|\bicsr\b"),

    ("Medical Coding",
     r"medical cod\w*|\bcoder\b|coding (?:specialist|auditor|analyst|manager|"
     r"quality|compliance|validation|audit)|\bcpc\b|\bccs\b|icd-?10|"
     r"risk adjustment|\bhcc\b|profee|\bdrg\b|\bapc\b coding|charge capture"),

    ("Medical Writer",
     r"medical writ\w*|scientific writ\w*|regulatory writ\w*|"
     r"medical editor|scientific editor|medical communications?|"
     r"publications? (?:manager|lead|specialist|associate|director)"),

    ("Regulatory Affairs",
     # NOT bare "regulatory compliance" — in US listings that is usually
     # revenue-cycle compliance ("Senior Regulatory Compliance and Revenue
     # Cycle Analyst"), which is a different job entirely.
     r"regulatory affairs?|regulatory (?:strateg\w*|submission\w*|"
     r"operation\w*|intelligence|specialist|associate|manager|director|lead|"
     r"scientist|labeling|labelling|publishing)|"
     r"\bctd\b|\bind\b\s|\bnda\b|\bmaa\b"),

    ("Clinical Data Management",
     # "CDM" alone is ambiguous — in US revenue-cycle listings it means Charge
     # Description Master ("Revenue Integrity & CDM Operations Manager").
     r"clinical data|clinical database|clinical programm\w*|"
     r"\bcdisc\b|\bsdtm\b|\bedc\b|"
     # generic "data management" only counts with a clinical/trial context —
     # otherwise it swallows "Manager, Client Data Management" and
     # "Configuration / Data Management Analyst- Federal Health".
     r"(?:data manage\w*|data steward|\bcdm\b)"
     r"(?=.*(?:clinical|trial|study|\bedc\b|\bcdisc\b|biometric))|"
     r"(?:clinical|trial|study)\b.*(?:data manage\w*|data steward)"),

    ("Public Health",
     # Biostatistics is deliberately absent: in this feed it is pharma
     # biometrics, not public health, and it was not in the requested scope.
     r"public health|epidemiolog\w*|population health|community health|"
     r"global health|health promotion|disease surveillance|health polic\w*"),

    ("Clinical Research",
     r"clinical research|clinical trial\w*|clinical stud\w*|"
     r"clinical operations|clinical monitor\w*|clinical development|"
     r"clinical project|clinical scientist|\bcra\b|"
     r"(?:study|trial) (?:manager|lead|coordinator|director|start[- ]?up|"
     r"specialist|associate)|principal investigator|"
     r"site (?:management|contracts|activation)|"
     r"research (?:associate|coordinator|nurse|physician)"),
]

_ROLE_FAMILY_RES = [(name, re.compile(pat, re.IGNORECASE))
                    for name, pat in ROLE_FAMILIES]

# Titles that carry an in-scope term but are plainly a different profession:
# legal counsel, quota-carrying sales, recruiting, engineering. "Senior Counsel,
# Global Commercial Legal - U.S. Market Access and Pricing" is a lawyer, not
# HEOR. These are KEPT but flagged, never silently dropped (master spec §2).
OUT_OF_SCOPE_TITLE = re.compile(
    r"(?:^|[^a-z])(?:"
    r"counsel|attorney|paralegal|"
    r"account (?:executive|manager)|sales (?:representative|rep|director|"
    r"manager|executive|specialist)|business development|"
    r"recruiter|talent acquisition|"
    r"software engineer|web developer|frontend|backend|data engineer"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)


def _slug_text(slugs):
    return " ".join(str(s).replace("-", " ").replace("_", " ") for s in slugs)


def match_role_family(title, categories=None):
    """Return (family, signal, needs_review) — ("", "", False) if out of scope.

    `categories` is accepted and ignored: the feed's slugs are too noisy to
    admit a job on their own (see the note above). The parameter is kept so
    callers don't have to care.
    """
    title = title or ""
    for name, title_re in _ROLE_FAMILY_RES:
        if title_re.search(title):
            return (name, "title", bool(OUT_OF_SCOPE_TITLE.search(title)))
    return ("", "", False)


# The club `category` column now carries the ROLE FAMILY itself (Clinical
# Research, Pharmacovigilance, ...). The previous profession classifier
# (doctors / nurses / pharmacists / non_clinical) has been retired: it was a
# poor fit here — every one of these families is a non-clinical desk role, so
# ~97% of rows collapsed into `non_clinical` and the column carried almost no
# information. ROLE_FAMILIES is now the single source of truth for both the
# scope gate and the category, so the two can never disagree.

CLUB_CATEGORIES = [name for name, _pat in ROLE_FAMILIES]


_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?|genomic",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None, since=None):
    """Watermark cutoff, or an explicit `since` override.

    `since` exists because a single offset-paginated pass over this feed is
    NOT complete (see the README's "Feed pagination" section): deep offset
    paging re-serves some rows and silently omits others. Re-walking the full
    window after the feed regenerates is how those jobs are recovered, and the
    watermark would otherwise clamp the re-run to the last day or two.
    """
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/json"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    if not rp.can_fetch(USER_AGENT, API_URL):
        sys.exit("robots.txt disallows {} — aborting.".format(API_URL))
    log.info("robots.txt check passed")


def fetch_page(session, offset):
    """Fetch one 20-job page. Returns (jobs, total) or (None, None)."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for offset %d in %.0fs (%s)",
                        attempt, MAX_RETRIES, offset, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(API_URL, params={"offset": offset},
                               timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d at offset %d — skipping",
                            resp.status_code, offset)
                return (None, None)
            resp.raise_for_status()
            payload = resp.json()
            return (payload.get("jobs") or [], payload.get("totalCount"))
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    log.error("Giving up on offset %d after %d retries (%s)",
              offset, MAX_RETRIES, last_error)
    return (None, None)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job, signal="", role_family="", scope_review=False):
    title = clean_value(job.get("title"))
    # The role family IS the category, so there is no separate "which
    # profession is this?" ambiguity left to flag. A row is doubtful only when
    # the scope match itself looked wrong (an in-scope term sitting inside a
    # counsel / sales / recruiter title).
    needs_review = scope_review

    categories = parse_listish(job.get("categories"))
    parent_categories = parse_listish(job.get("parentCategories"))
    locations = parse_listish(job.get("locationRestrictions"))
    seniority = parse_listish(job.get("seniority"))
    timezones = parse_listish(job.get("timezoneRestrictions"))

    salary_raw, sal_min, sal_max, sal_cur, sal_per = parse_salary(
        job.get("minSalary"), job.get("maxSalary"),
        job.get("currency"), job.get("salaryPeriod"))

    description = strip_html(job.get("description") or "") or \
        clean_value(job.get("excerpt"))

    url = clean_value(job.get("applicationLink")) or clean_value(job.get("guid"))

    return {
        "source": SITE,
        "job_id": job_id_from_guid(job.get("guid") or job.get("applicationLink")),
        "title": title,
        "company": clean_value(job.get("companyName")),
        "company_slug": clean_value(job.get("companySlug")),
        "company_logo": clean_value(job.get("companyLogo")),
        # Remote roles are hiring-region scoped, not city scoped: keep the full
        # eligibility list here, export the primary one to the club CSV.
        "locations": "; ".join(locations),
        "country": locations[0] if locations else "",
        "salary_raw": salary_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": sal_cur,
        "salary_period": sal_per,
        "employment_type": clean_value(job.get("employmentType")),
        "work_mode": "remote",
        "seniority": "; ".join(seniority),
        "company_type": classify_company_type(job.get("companyName")),
        "role_family": role_family,
        "match_signal": signal,
        "categories": "; ".join(categories),
        "parent_categories": "; ".join(parent_categories),
        "timezones": "; ".join(timezones),
        "posted_date": epoch_to_date(job.get("pubDate")),
        "expires_date": epoch_to_date(job.get("expiryDate")),
        "description": truncate_description(description),
        "job_url": url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "needs_review": needs_review,
    }


def extract_qualification(description):
    """Lift explicit credentials verbatim out of a description.

    Grounded extraction, not inference: a token is only emitted when it
    literally appears in the posting. Returns "" when the description names
    none — the club schema's `qualification` is optional and the master spec
    forbids inventing what the source did not state.
    """
    text = description or ""
    if not text:
        return ""
    found = []
    for regex, label in _QUALIFICATION_RES:
        if label not in found and regex.search(text):
            found.append(label)
        if len(found) >= MAX_QUALIFICATIONS:
            break
    return ", ".join(found)


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the 22-column club schema.

    Only USD/INR annual or monthly pay can be represented by the club enums;
    other currencies and hourly rates keep their values in the rich CSV and
    leave the club salary columns empty rather than being misdeclared.
    """
    currency = _clean(r.get("salary_currency"))
    period = _clean(r.get("salary_period"))
    lo, hi = _clean(r.get("salary_min")), _clean(r.get("salary_max"))
    exportable = (bool(lo) and currency in ("INR", "USD")
                  and period in ("per_month", "per_annum"))

    country = _clean(r.get("country"))
    code, dial = COUNTRY_CODES.get(country, ("", ""))

    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        # Remote-only board: there is no city, and inventing one would be a lie.
        "city_name": "Remote",
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": _clean(r.get("company_logo")),
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": CLUB_JOB_TYPE,
        "category": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        "qualification": extract_qualification(_clean(r.get("description"))),
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


def write_club_csv(rich_df, run_date):
    club_rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(club_rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def reclassify(args):
    """Re-apply the role-family gate and category classifier to the stored CSV.

    Both are pure functions of the job title plus the stored `categories`
    slugs, so re-scoping does not require re-crawling. Makes no network
    requests and never re-dates a row.

    Rows that no longer match any in-scope role family are DROPPED — narrowing
    the scope has to narrow the stored data too, or the club export keeps
    emitting jobs the site no longer wants. Dropped rows are written to
    `out-of-scope.csv` next to the CSV rather than discarded, so a scope change
    is reversible and reviewable.
    """
    df = load_existing(args.output)
    if df is None or df.empty:
        sys.exit("Nothing to reclassify: {} not found or empty.".format(args.output))

    kept_rows, dropped_rows, review_log = [], [], []
    for _, row in df.iterrows():
        title = row.get("title", "")
        slugs = [s for s in str(row.get("categories", "") or "").split("; ") if s]
        family, signal, scope_review = match_role_family(title, slugs)
        if not family:
            dropped_rows.append(row)
            continue
        row = row.copy()
        row["role_family"] = family
        row["match_signal"] = signal
        kept_rows.append(row)
        if scope_review:
            review_log.append({"job_id": row.get("job_id", ""),
                               "title": title,
                               "company": row.get("company", ""),
                               "role_family": family,
                               "match_signal": signal,
                               "categories": row.get("categories", "")})

    kept = pd.DataFrame(kept_rows).reindex(columns=RICH_COLUMNS)
    kept.to_csv(args.output, index=False)
    log.info("Kept %d of %d stored rows; dropped %d now out of scope",
             len(kept), len(df), len(dropped_rows))
    log.info("role_family: %s", kept["role_family"].value_counts().to_dict())

    if dropped_rows:
        out = Path(args.output).with_name("out-of-scope.csv")
        pd.DataFrame(dropped_rows).reindex(
            columns=RICH_COLUMNS).to_csv(out, index=False)
        log.info("Wrote %s (%d dropped rows, kept for review)",
                 out, len(dropped_rows))

    target, n = write_club_csv(kept, args.run_date)
    log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    pd.DataFrame(review_log,
                 columns=["job_id", "title", "company", "role_family",
                          "match_signal", "categories"]).to_csv(
                              NEEDS_REVIEW_CSV, index=False)
    log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Reclassify summary =====")
    print("Rows in stored CSV:   {:>6,}".format(len(df)))
    print("Kept (in scope):      {:>6,}".format(len(kept)))
    print("Dropped (out of scope):{:>5,}".format(len(dropped_rows)))
    print("Flagged needs_review: {:>6,}".format(len(review_log)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape remote healthcare jobs from himalayas.app.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N API pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (for test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job posted "
                             "on/after this date. Use to re-run the full window "
                             "against an existing CSV — a single pass over this "
                             "feed is not complete (see README: Feed pagination); "
                             "unioning passes is how misses are recovered")
    parser.add_argument("--reclassify", action="store_true",
                        help="re-apply the category classifier to the stored "
                             "CSV and rewrite the outputs; makes no network "
                             "requests (use after tuning the keyword lists)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.reclassify:
        return reclassify(args)

    session = make_session()
    check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s%s",
             len(known_ids), cutoff, " (--since override)" if args.since else "")

    counters = {"scanned": 0, "excluded_out_of_scope": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0, "refetched": 0}
    # Jobs already stored before this run. `seen_this_run` catches the feed
    # serving the same job twice regardless of whether we already had it —
    # keying off preexisting_ids alone hides re-serves of known jobs entirely,
    # which is what made the pagination instability invisible at first.
    preexisting_ids = set(known_ids)
    seen_this_run = set()
    new_rows, review_log = [], []
    offset, page_no, total, empty_pages, stop = 0, 0, None, 0, False

    while not stop and offset < MAX_OFFSET:
        if args.max_pages is not None and page_no >= args.max_pages:
            break
        jobs, count = fetch_page(session, offset)
        page_no += 1
        if jobs is None:
            log.error("Offset %d failed after retries — stopping", offset)
            break
        if total is None and count is not None:
            total = count
            log.info("Feed reports %d jobs total (newest-first)", total)
        if not jobs:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            offset += PAGE_SIZE
            continue
        empty_pages = 0

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                posted = epoch_to_date(job.get("pubDate"))
                # Undated jobs can't be placed in the window; the feed is
                # newest-first, so treat them as in-window and let dedup work.
                is_recent = (not posted) or posted >= cutoff
                if is_recent:
                    page_all_old = False
                if not is_recent:
                    counters["excluded_old"] += 1
                    continue

                family, signal, scope_review = match_role_family(
                    job.get("title"), parse_listish(job.get("categories")))
                if not family:
                    counters["excluded_out_of_scope"] += 1
                    continue

                job_id = job_id_from_guid(
                    job.get("guid") or job.get("applicationLink"))
                if job_id in seen_this_run:
                    # the feed served this same job at two different offsets
                    counters["refetched"] += 1
                    continue
                seen_this_run.add(job_id)
                if job_id in preexisting_ids:
                    counters["duplicates"] += 1
                    continue

                row = job_to_rich_row(job, signal, family, scope_review)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job at offset %d: %s", offset, exc)
                continue

            if row.pop("needs_review", False):
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["title"],
                                   "company": row["company"],
                                   "role_family": row["role_family"],
                                   "match_signal": row["match_signal"],
                                   "categories": row["categories"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1
            if args.limit is not None and counters["new"] >= args.limit:
                stop = True
                break

        # pubDate is strictly descending across offsets (verified), so a page
        # entirely older than the cutoff means everything below it is too.
        if page_all_old:
            log.info("Offset %d entirely older than %s — stopping", offset, cutoff)
            break
        if total is not None and offset + PAGE_SIZE >= total:
            break
        offset += PAGE_SIZE
        if page_no % 25 == 0:
            log.info("...offset %d, %d in-scope jobs kept so far",
                     offset, counters["new"])

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
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Jobs scanned:              {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):    {:>6,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))
    print("Re-served at another offset: {:>4,}".format(counters["refetched"]))
    if counters["refetched"] > counters["scanned"] * 0.02:
        print("\n  NOTE: the feed's deep pagination is unstable — {:,} of {:,}"
              "\n  scanned slots re-served a job seen at an earlier offset, so"
              "\n  this pass does not cover the whole window. This is"
              "\n  DETERMINISTIC per feed snapshot: re-running now returns the"
              "\n  identical set. Coverage only improves after the feed"
              "\n  regenerates (check `updatedAt`). See README: Feed pagination."
              .format(counters["refetched"], counters["scanned"]))


if __name__ == "__main__":
    main()
