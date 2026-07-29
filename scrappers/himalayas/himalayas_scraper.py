#!/usr/bin/env python3
"""Scrape REMOTE healthcare job listings from himalayas.app.

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

Healthcare filter (master spec §2)
----------------------------------
The spec prefers source-side filtering, but this feed has none, so the gate is
a union of three signals measured on a 1,560-job sample:

1. parentCategories contains "Healthcare" — high precision, LOW RECALL: only
   38 of the 218 healthcare jobs in the sample had it; the field is empty for
   most postings.
2. A healthcare slug in `categories` — this recovered all 180 healthcare jobs
   that signal 1 missed.
3. A healthcare term in the title — the safety net for empty category lists.

A job matching none of the three is counted `excluded_non_healthcare` (this
is a general job board; ~97% of it is software/sales). A job that IS
healthcare but whose club category (doctors/nurses/pharmacists/non_clinical)
can't be pinned down is KEPT, flagged needs_review and logged to
needs_review.csv — never silently dropped, per the spec.

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

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS (7);
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

INITIAL_WINDOW_DAYS = 7        # master spec §4
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
    "seniority", "category", "company_type", "match_signal", "categories",
    "parent_categories", "timezones", "posted_date", "expires_date",
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
# Healthcare classification (master spec §2)
# ----------------------------------------------------------------------------

HEALTHCARE_PARENT_CATEGORY = "Healthcare"

# Healthcare terms for titles and category slugs. Deliberately excludes bare
# "care" (would swallow "Customer Care Representative") and bare "wellness".
ALLOW_TITLE_KEYWORDS = re.compile(
    r"(?:^|[^a-z])(?:"
    r"nurse|nursing|nurse[- ]?practitioner|midwif\w*|"
    r"physician|doctor|surgeon|dentist|dental|orthodont\w*|"
    r"psychiatr\w*|psycholog\w*|psychotherap\w*|therapist|therapy|"
    r"counselor|counsellor|counseling|counselling|"
    r"clinical|clinician|clinic|medical|medicine|healthcare|health[- ]?care|"
    r"patient|telehealth|telemedicine|behavioral[- ]?health|mental[- ]?health|"
    r"pharmac\w*|pharma|apothecary|"
    r"radiolog\w*|sonograph\w*|ultrasound|phlebotom\w*|"
    r"paramedic\w*|epidemiolog\w*|oncolog\w*|cardiolog\w*|neurolog\w*|"
    r"p[ae]?diatric\w*|geriatric\w*|obstetric\w*|gyn[ae]?colog\w*|"
    r"an[ae]sthesiolog\w*|dermatolog\w*|pathol\w*|"
    r"dietit\w*|dietic\w*|nutritionist|optometr\w*|ophthalmolog\w*|"
    r"chiroprac\w*|podiatr\w*|physiotherap\w*|occupational[- ]?therap\w*|"
    r"speech[- ]?language|audiolog\w*|respiratory[- ]?therap\w*|"
    r"caregiver|care[- ]?giver|home[- ]?health|hospice|"
    r"\brn\b|\blpn\b|\blvn\b|\bcna\b|\bnp\b|\bpa-c\b|\bmd\b|\bdo\b|"
    r"\blcsw\b|\blmft\b|\blmhc\b|\blpc\b|\blcpc\b|\bapr?n\b|\bcrna\b|"
    r"social[- ]?work\w*|hospital|nurse[- ]?manager|"
    r"life[- ]?science|biotech|pharmacovigilance|clinical[- ]?research"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)

# Occupations that are plainly NOT healthcare work even when the employer is a
# healthcare company. Healthcare employers tag such postings with slugs like
# "Healthcare-Web-Developer" / "Healthcare-Analytics" / "Healthcare-IT-Sales",
# which is enough to pass the categories gate — e.g. Insight Therapy Solutions
# hiring a "Freelance WordPress Developer".
#
# These are NOT dropped (master spec §2: never silently dropped). They are kept
# and forced to needs_review so a human decides whether an industry-adjacent
# tech/commercial role belongs on HealthCareers.club.
DENY_TITLE_KEYWORDS = re.compile(
    r"(?:^|[^a-z])(?:"
    r"software[- ]?(?:engineer|developer)(?:ing)?|web[- ]?developer|wordpress|"
    r"frontend|front[- ]?end|backend|back[- ]?end|full[- ]?stack|"
    r"devops|site[- ]?reliability|kubernetes|golang|javascript|typescript|"
    r"react|angular|node\.?js|python developer|android|ios[- ]?developer|"
    r"mobile[- ]?developer|machine[- ]?learning[- ]?engineer|"
    r"data[- ]?engineer(?:ing)?|business[- ]?intelligence|bi[- ]?analyst|"
    r"qa[- ]?engineer|test[- ]?engineer|salesforce|"
    r"translator|interpreter|transcriptionist|"
    r"graphic[- ]?designer|ux[- ]?designer|ui[- ]?designer|copywriter"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)


def _slug_text(slugs):
    return " ".join(str(s).replace("-", " ").replace("_", " ") for s in slugs)


def is_healthcare(title, categories, parent_categories):
    """Return (keep, signal) — signal names which of the three gates matched."""
    if HEALTHCARE_PARENT_CATEGORY in (parent_categories or []):
        return (True, "parent_category")
    if ALLOW_TITLE_KEYWORDS.search(title or ""):
        return (True, "title")
    if ALLOW_TITLE_KEYWORDS.search(_slug_text(categories or [])):
        return (True, "categories")
    return (False, "")


# Title -> club category enum. US-remote flavour: overwhelmingly licensed
# behavioural-health and telehealth roles.
_NURSE_RE = re.compile(
    r"(?:^|[^a-z])(?:nurse|nursing|midwif\w*|\brn\b|\blpn\b|\blvn\b|\bcna\b|"
    r"\bapr?n\b|\bcrna\b|nurse[- ]?practitioner|\bnp\b)(?:[^a-z]|$)",
    re.IGNORECASE)
_PHARM_RE = re.compile(
    r"(?:^|[^a-z])(?:pharmacist|pharmacy|pharm\.?\s?d|dispenser|"
    r"pharmacovigilance)(?:[^a-z]|$)", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"(?:^|[^a-z])(?:physician|doctor|surgeon|dentist|\bmd\b|\bdo\b|mbbs|"
    r"psychiatrist|medical[- ]?director|medical[- ]?officer|"
    r"[a-z]{4,}ologist|general[- ]?practitioner|intensivist|"
    r"an[ae]sthesiologist|radiologist|pathologist|"
    r"(?:family|internal|emergency)[- ]?medicine)(?:[^a-z]|$)",
    re.IGNORECASE)
# Licensed non-physician clinicians + everything administrative. The club
# schema has no "allied health" bucket, so these map to non_clinical.
_NONCLINICAL_RE = re.compile(
    r"(?:^|[^a-z])(?:therapist|therapy|counselor|counsellor|counseling|"
    r"counselling|psycholog\w*|psychotherap\w*|social[- ]?work\w*|\blcsw\b|"
    r"\blmft\b|\blmhc\b|\blpc\b|\blcpc\b|clinician|coach|caregiver|"
    r"technician|technologist|dietit\w*|dietic\w*|nutritionist|"
    r"phlebotom\w*|radiograph\w*|sonograph\w*|audiolog\w*|optometr\w*|"
    r"coordinator|specialist|manager|director|analyst|administrator|"
    r"assistant|associate|executive|representative|advisor|adviser|"
    r"consultant|recruiter|scientist|researcher|writer|educator|trainer|"
    r"sales|marketing|billing|coding|coder|scribe|receptionist|"
    r"support|operations|lead|supervisor|liaison|reviewer|auditor"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)


def classify_category(title):
    """Return (club category, needs_review) for a healthcare job title."""
    title = title or ""
    # A plainly non-healthcare occupation at a healthcare employer: keep it,
    # but never let it look confidently classified.
    if DENY_TITLE_KEYWORDS.search(title):
        return ("non_clinical", True)
    if _NURSE_RE.search(title):
        return ("nurses", False)
    if _PHARM_RE.search(title):
        return ("pharmacists", False)
    if _DOCTOR_RE.search(title):
        return ("doctors", False)
    if _NONCLINICAL_RE.search(title):
        return ("non_clinical", False)
    # Healthcare by category/parent signal but the title says nothing about
    # the role — keep it, flag it (master spec §2: never silently dropped).
    return ("non_clinical", True)


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
    NOT complete (see the README's "Feed drift" section): the list mutates
    while we walk it. Re-running the full window against an existing CSV is
    how missed jobs are recovered, and the watermark would otherwise clamp
    the re-run to the last day or two.
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

def job_to_rich_row(job, signal=""):
    title = clean_value(job.get("title"))
    category, needs_review = classify_category(title)

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
        "category": category,
        "company_type": classify_company_type(job.get("companyName")),
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
        "category": _clean(r.get("category")) or "non_clinical",
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        "min_salary": lo if exportable else "",
        "max_salary": (hi or lo) if exportable else "",
        "salary_period": period if exportable else "",
        "salary_currency": currency if exportable else "",
        "is_active": "true",
        "expires_at": _clean(r.get("expires_date")),
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
    """Re-apply classify_category to the stored rich CSV and rewrite outputs.

    `category` and `needs_review` are pure functions of the job title, so
    tuning the keyword lists does not require re-crawling. Makes no network
    requests and never adds, removes or re-dates a row.
    """
    df = load_existing(args.output)
    if df is None or df.empty:
        sys.exit("Nothing to reclassify: {} not found or empty.".format(args.output))

    before = df["category"].value_counts().to_dict()
    review_log = []
    categories = []
    for _, row in df.iterrows():
        category, needs_review = classify_category(row.get("title"))
        categories.append(category)
        if needs_review:
            review_log.append({"job_id": row.get("job_id", ""),
                               "title": row.get("title", ""),
                               "company": row.get("company", ""),
                               "match_signal": row.get("match_signal", ""),
                               "categories": row.get("categories", "")})
    df["category"] = categories
    df.to_csv(args.output, index=False)
    log.info("Reclassified %d rows in %s", len(df), args.output)
    log.info("category before: %s", before)
    log.info("category after:  %s", df["category"].value_counts().to_dict())

    target, n = write_club_csv(df, args.run_date)
    log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    pd.DataFrame(review_log,
                 columns=["job_id", "title", "company", "match_signal",
                          "categories"]).to_csv(NEEDS_REVIEW_CSV, index=False)
    log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Reclassify summary =====")
    print("Rows reclassified:    {:>6,}".format(len(df)))
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
                             "feed is not complete (see README: Feed drift); "
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

    counters = {"scanned": 0, "excluded_non_healthcare": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0, "refetched": 0}
    # Jobs already stored before this run, so an id seen that ISN'T in here is
    # a job the shifting feed served us twice — the drift signal.
    preexisting_ids = set(known_ids)
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

                keep, signal = is_healthcare(
                    job.get("title"),
                    parse_listish(job.get("categories")),
                    parse_listish(job.get("parentCategories")))
                if not keep:
                    counters["excluded_non_healthcare"] += 1
                    continue

                job_id = job_id_from_guid(
                    job.get("guid") or job.get("applicationLink"))
                if job_id in known_ids:
                    if job_id in preexisting_ids:
                        counters["duplicates"] += 1
                    else:
                        # served to us a second time within this same run
                        counters["refetched"] += 1
                    continue

                row = job_to_rich_row(job, signal)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job at offset %d: %s", offset, exc)
                continue

            if row.pop("needs_review", False):
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["title"],
                                   "company": row["company"],
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
            log.info("...offset %d, %d healthcare jobs kept so far",
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
    print("Excluded (non-healthcare): {:>6,}".format(counters["excluded_non_healthcare"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))
    print("Re-served within this run: {:>6,}".format(counters["refetched"]))
    if counters["refetched"] > counters["scanned"] * 0.02:
        print("\n  NOTE: the feed shifted under us — {:,} of {:,} scanned slots"
              "\n  were re-reads, so this pass is INCOMPLETE. Re-run with"
              "\n  --since {} to union another pass.".format(
                  counters["refetched"], counters["scanned"], cutoff))


if __name__ == "__main__":
    main()
