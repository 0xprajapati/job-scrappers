#!/usr/bin/env python3
"""Scrape healthcare job listings from shine.com (India).

Data source
-----------
shine.com is a Next.js site whose search pages are server-rendered with the
full result payload embedded in `<script id="__NEXT_DATA__">`:

    props.pageProps.initialState.jsrp.searchresult.data
        .results[20]  -- one JSON record per job card
        .count / .num_pages / .page

robots.txt disallows `/api/*`, so the JSON API is off-limits (master spec §1);
the `/job-search/` listing pages themselves are allowed and carry everything
we need — no detail-page fetches, no headless browser. A descriptive
User-Agent is accepted (verified: HTTP 200, same payload as a browser UA).

URL scheme (all verified live):

    https://www.shine.com/job-search/<query>-jobs?sort=1        page 1
    https://www.shine.com/job-search/<query>-jobs-<N>?sort=1    page N

`sort=1` = newest-first (default is relevance, which surfaces months-old
posts on page 1). Ordering is only *approximately* descending — promoted
cards interleave — so the early-stop rule is "stop when an entire page is
older than the cutoff", never "stop at the first old job".

`ind=13` = the "Medical / Healthcare" industry facet, and it DOES work as
a URL param (verified: count drops ~26k -> ~21k, the SSR payload echoes
query.ind = 13, and every returned card carries jInd "Medical /
Healthcare"; pagination and sort compose with it). Note the param is
`ind`, NOT the facet's field name `jIndID` — the latter is ignored. The
industry browse is the first crawl query (guaranteed industry-wide
coverage, master spec §2 "filter at the source"); the keyword queries
after it recover healthcare roles filed under OTHER industries.

Record fields used: id, jJT (title), jCName (company), jCID (company id),
jSal (salary display string), jJD (description HTML), jLoc (list of
locations), jPDate (posted datetime), jExpDate (expiry), jExp (experience
range display), jInd (industry name), jKwd (keywords), jSlug (detail-page
slug), jTypeC (1 full/2 part time), jEType (1 regular/2 contractual/
3 internship/4 work from home), jJobType (1 regular/2 walkin), jWM (work
mode flag; 0 everywhere in samples).

Verified quirks
---------------
* shine RE-DATES reposted/refreshed listings: with sort=1 the "healthcare"
  query showed 600+ jobs all dated "today" (page 30 was still today). The
  posted-date watermark therefore cannot bound crawl depth on its own —
  MAX_PAGES_PER_QUERY caps each query and the run prints a NOTE whenever a
  query is cut off by the cap rather than by the date window (no silent
  truncation). Dedup by job id absorbs the re-served reposts across runs.
* Every record also carries its industry in `jInd` ("Medical /
  Healthcare" for ~70% of bare healthcare-query results) — the per-record
  signal the healthcare gate uses for cards found via keyword queries.
* Salary strings: "Rs 4.0  - 4.5 Lakh/Yr", "< Rs 50,000  - 2.5 Lakh/Yr"
  (mixed absolute + lakh!), or "[Salary Hidden]" (the majority).
* jLoc can be ["All India"] — kept verbatim; it is a real answer, not a city.

Healthcare filter (master spec §2)
----------------------------------
Keyword searches drag in non-healthcare noise (BPO, insurance, IT — visible
in jInd). Gate, in order:

1. DENY_TITLE_KEYWORDS (telecallers, admissions counsellors, software…)
   -> excluded_non_healthcare, even at a healthcare employer.
2. ALLOW_TITLE_KEYWORDS (clinical + healthcare-business vocabulary) -> kept.
3. jInd == "Medical / Healthcare" -> kept (title said nothing; the category
   classifier decides whether it still needs human review).
4. jInd empty or "Others" -> kept, flagged needs_review (never silently
   dropped when the source is non-committal).
5. Any other named industry (IT Services, BFSI, BPO…) with a non-matching
   title -> excluded_non_healthcare.

Salary (master spec §3): capture, don't filter. Never excluded, never
invented. Monthly normalization: Lakh = 100,000 INR; /Yr divided by 12.

Outputs
-------
* shine_jobs.csv                        -- rich cumulative store (dedup key:
                                           job_id), watermark source of truth.
* needs_review.csv                      -- titles the classifier could not
                                           confidently place.
* ../../jobs_csv/<DD-MM-YYYY>/shine.csv -- HealthCareers.club 22-column
                                           schema, rewritten every run.

Time window (master spec §4): first run keeps INITIAL_WINDOW_DAYS (7) days;
later runs keep jobs newer than the stored max posted_date minus
WATERMARK_GRACE_DAYS (2). Run `python shine_scraper.py --help` for options.
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

SITE = "shine"
SITE_BASE = "https://www.shine.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SEARCH_PATH = "/job-search/"

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

# Query slugs crawled newest-first each run. A slug may carry extra query
# params after "?". The ind=13 industry browse comes first: it returns
# every job filed under "Medical / Healthcare" regardless of title. The
# keyword slugs then recover healthcare roles filed under other industries
# (e.g. a "Staff Nurse" at a company registered as Education). Dedup by
# job id makes the overlap between queries free.
SEARCH_QUERIES = [
    "healthcare?ind=13",
    "healthcare",
    "hospital",
    "medical",
    "staff-nurse",
    "doctor",
    "pharmacist",
    "physiotherapist",
    "lab-technician",
]

INITIAL_WINDOW_DAYS = 7        # master spec §4
WATERMARK_GRACE_DAYS = 2

# shine re-dates reposts (see module docstring), so the date window cannot
# bound depth on its own. 50 pages x 20 jobs bounds each query at 1,000
# newest slots per run; the run summary warns when a query hits the cap.
MAX_PAGES_PER_QUERY = 50

REQUEST_DELAY_SECONDS = 1.2
REQUEST_TIMEOUT_SECONDS = 45
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 20_000

HEALTHCARE_INDUSTRY = "Medical / Healthcare"
# Industries that are non-committal about the work itself; a title the
# classifier can't read + one of these -> keep, flag for review.
NEUTRAL_INDUSTRIES = {"", "Others"}

RICH_CSV = str(Path(__file__).resolve().parent / "shine_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_id", "location",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "employment_type", "is_walkin",
    "work_mode", "experience_raw", "experience_min_years",
    "experience_max_years", "industry", "keywords", "category",
    "company_type", "match_signal", "needs_review", "posted_date",
    "expires_date", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

# jTypeC / jEType / jJobType enum decodings (from the search facets block).
JOB_TYPE_C = {1: "Full time", 2: "Part time"}
EMPLOYMENT_TYPE = {1: "Regular", 2: "Contractual", 3: "Internship",
                   4: "Work from home"}

log = logging.getLogger("shine_scraper")

# ----------------------------------------------------------------------------
# Healthcare classification (master spec §2)
# ----------------------------------------------------------------------------

# Applied to titles only (jKwd/jJD mention "healthcare" too loosely).
ALLOW_TITLE_KEYWORDS = re.compile(
    r"(?:^|[^a-z])(?:"
    r"nurse|nursing|midwif\w*|\bgnm\b|\banm\b|"
    r"physician|doctor|surgeon|dentist|dental|mbbs|\bmd\b|intensivist|"
    r"practitioner|"
    r"pediatric\w*|paediatric\w*|geriatric\w*|obstetric\w*|gyn[ae]?colog\w*|"
    r"[a-z]{4,}ologist|diabetolog\w*|ayurved\w*|homeopath\w*|unani|"
    r"psychiatr\w*|psycholog\w*|psychotherap\w*|psychometri\w*|therapist|"
    r"mental[- ]?health|behaviou?ral[- ]?health|"
    r"clinical|clinician|clinic|medical|medicine|healthcare|health[- ]?care|"
    r"health\b|patient|telehealth|telemedicine|tele[- ]?consult\w*|"
    r"pharmac\w*|pharma\b|drug[- ]?safety|regulatory[- ]?affairs|"
    r"radiolog\w*|radiograph\w*|sonograph\w*|phlebotom\w*|patholog\w*|"
    r"paramedic\w*|epidemiolog\w*|oncolog\w*|cardiolog\w*|neurolog\w*|"
    r"dermatolog\w*|endocrinolog\w*|an[ae]sthes\w*|optometr\w*|"
    r"ophthalmolog\w*|dietit\w*|dietic\w*|nutrition\w*|"
    r"physiotherap\w*|occupational[- ]?therap\w*|speech[- ]?(?:therap|language)\w*|"
    r"audiolog\w*|respiratory[- ]?therap\w*|"
    r"caregiver|care[- ]?giver|home[- ]?health|hospice|hospital|"
    r"wellness|\brcm\b|revenue[- ]?cycle|prior[- ]?auth\w*|"
    r"\bicd(?:-10)?\b|\bcpt\b|coder|coding|claims?\b|"
    r"lab\b|laboratory|\bdmlt\b|"
    r"life[- ]?science|biotech|pharmacovigilance|"
    r"\bemr\b|\behr\b|\boet\b"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)

# Clearly non-healthcare occupations that healthcare-shaped queries drag in
# (the Telesales/FMCG cards on the healthcare query, IT roles at hospital
# chains, hospitality "hospital housekeeping vendor sales"…). DENY wins even
# when jInd says Medical / Healthcare: the occupation, not the employer,
# decides (same convention as the indeed scraper).
DENY_TITLE_KEYWORDS = re.compile(
    r"(?:^|[^a-z])(?:"
    r"telesales|telecaller|tele[- ]?calling|telemarket\w*|"
    r"admissions?[- ]?counsell?or|academic[- ]?counsel\w*|"
    r"education[- ]?counsel\w*|visa[- ]?counsell?or|career[- ]?counsel\w*|"
    r"sales[- ]?executive|business[- ]?development|"
    r"software[- ]?(?:engineer|developer)|web[- ]?developer|"
    r"frontend|front[- ]?end|backend|back[- ]?end|full[- ]?stack|devops|"
    r"java[- ]?developer|python[- ]?developer|\.net|salesforce|"
    r"data[- ]?engineer\w*|cloud[- ]?engineer|network[- ]?engineer|"
    r"civil[- ]?engineer|mechanical[- ]?engineer|electrical[- ]?engineer|"
    r"accountant|chartered[- ]?accountant|"
    r"data[- ]?annotat\w*|transcriber\b|transcription\w*|"
    r"graphic[- ]?designer|ui[- ]?designer|ux[- ]?designer|copywriter|"
    r"chef|housekeeping|driver|security[- ]?guard"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)


def is_healthcare(title, industry):
    """Return (keep, signal); see the gate order in the module docstring."""
    title, industry = title or "", (industry or "").strip()
    if DENY_TITLE_KEYWORDS.search(title):
        return (False, "deny")
    if ALLOW_TITLE_KEYWORDS.search(title):
        return (True, "title")
    if industry == HEALTHCARE_INDUSTRY:
        return (True, "industry")
    if industry in NEUTRAL_INDUSTRIES:
        return (True, "needs_review")
    return (False, "industry")


# Title -> club category enum (same regex family as the indeed scraper).
_NURSE_RE = re.compile(
    r"(?:^|[^a-z])(?:nurse|nursing|midwif\w*|\brn\b|\bgnm\b|\banm\b|"
    r"nursing[- ]?attendant)(?:[^a-z]|$)", re.IGNORECASE)
_PHARM_RE = re.compile(
    r"(?:^|[^a-z])(?:pharmacist|pharmacy|pharm\.?\s?d|dispenser|"
    r"pharmacolog\w*)(?:[^a-z]|$)", re.IGNORECASE)
# Psychology-family clinicians map to non_clinical in the club schema, but
# "...ologist" would drag them into doctors — checked before doctors.
_PSYCH_RE = re.compile(r"ps[cy]{1,2}h\w*olog|psychotherap", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"(?:^|[^a-z])(?:physician|doctor|surgeon|dentist|\bmd\b|mbbs|"
    r"psychiatrist|medical[- ]?director|medical[- ]?officer|intensivist|"
    r"[a-z]{4,}ologist|diabetolog\w*|general[- ]?practitioner|"
    r"p[ae]diatrician|"
    r"(?:family|internal|emergency)[- ]?medicine|\bgp\b)(?:[^a-z]|$)",
    re.IGNORECASE)
_NONCLINICAL_RE = re.compile(
    r"(?:^|[^a-z])(?:therapist|therapy|counselor|counsellor|psycholog\w*|"
    r"psychometri\w*|coach|caregiver|attendant|technician|"
    r"technologist|dietit\w*|dietic\w*|nutrition\w*|physiotherap\w*|"
    r"coder|coding|biller|billing|claims|transcription\w*|scribe|"
    r"coordinator|specialist|manager|director|analyst|administrator|"
    r"assistant|associate|executive|representative|consultant|advisor|"
    r"recruiter|scientist|researcher|writer|editor|educator|trainer|tutor|"
    r"faculty|reviewer|auditor|support|operations|lead|supervisor|"
    r"liaison|student|intern\w*|fellow\w*|officer|head\b|receptionist"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)


def classify_category(title):
    """Return (club category, ambiguous) for a kept title."""
    title = title or ""
    if _NURSE_RE.search(title):
        return ("nurses", False)
    if _PHARM_RE.search(title):
        return ("pharmacists", False)
    if _PSYCH_RE.search(title):
        return ("non_clinical", False)
    if _DOCTOR_RE.search(title):
        return ("doctors", False)
    if _NONCLINICAL_RE.search(title):
        return ("non_clinical", False)
    return ("non_clinical", True)


_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical|drug|"
    r"iqvia|parexel|syneos|pfizer|thermo ?fisher",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_value(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan", "null") else text


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|td)[^>]*>", " ", text or "",
                  flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_description(text, limit=DESCRIPTION_MAX_CHARS):
    """Cap at `limit` chars on a word boundary, marked with a trailing "…"."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    if len(spaced) >= limit * 0.5:
        cut = spaced
    return cut.rstrip() + "…"


def _salary_amounts(raw):
    """Return (lo, hi, period) as full-figure floats in the ORIGINAL period.

    Numbers written with a comma ("50,000") are absolute rupees; bare
    numbers under 1,000 are lakhs when the string says Lakh. period is
    "Yr" / "Mo" / ""; (None, None, period) when nothing parseable.
    """
    text = raw.lower()
    is_lakh = "lakh" in text or "lpa" in text
    if "/yr" in text or "p.a" in text or "annum" in text or "year" in text:
        period = "Yr"
    elif "/mo" in text or "p.m" in text or "month" in text:
        period = "Mo"
    else:
        period = ""

    amounts = []
    for token in _NUM_RE.findall(raw):
        value = float(token.replace(",", ""))
        if is_lakh and "," not in token and value < 1000:
            value *= 100_000
        amounts.append(value)
    if not amounts or not period:
        return (None, None, period)
    return (min(amounts), max(amounts), period)


def parse_salary(raw):
    """Master spec §3 — capture, never filter, never invent.

    "Rs 4.0  - 4.5 Lakh/Yr"      -> (raw, "33333", "37500", "Yr")
    "< Rs 50,000  - 2.5 Lakh/Yr" -> (raw, "4167", "20833", "Yr")
    "[Salary Hidden]" / ""       -> ("Not Disclosed", "", "", "")

    The middle values are normalized INR/month (spec §3): yearly amounts
    divide by 12.
    """
    raw = clean_value(raw)
    if not raw or "hidden" in raw.lower() or "not disclosed" in raw.lower():
        return ("Not Disclosed", "", "", "")
    lo, hi, period = _salary_amounts(raw)
    if lo is None:
        return (raw, "", "", period)
    if period == "Yr":
        lo, hi = lo / 12, hi / 12
    return (raw, str(int(round(lo))), str(int(round(hi))), period)


_EXP_RE = re.compile(r"(\d+)(?:\s*to\s*(\d+))?\s*Yrs?", re.IGNORECASE)


def parse_experience(raw):
    """"1 to 5 Yrs" -> ("1", "5"); "0 Yrs" -> ("0", "0"); "" -> ("", "")."""
    match = _EXP_RE.search(clean_value(raw))
    if not match:
        return ("", "")
    lo = match.group(1)
    return (lo, match.group(2) or lo)


def parse_date(value):
    """"2026-07-27T14:02:15" -> "2026-07-27"; "" for anything else."""
    text = clean_value(value)
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else ""


def decode_job_type(record):
    """Return (rich job_type, rich employment_type, club job_type enum)."""
    type_c = JOB_TYPE_C.get(record.get("jTypeC"), "")
    e_type = EMPLOYMENT_TYPE.get(record.get("jEType"), "")
    if e_type == "Work from home":
        club = "remote"
    elif type_c == "Part time":
        club = "part_time"
    else:
        club = "full_time"
    return (type_c or "Full time", e_type, club)


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None, since=None):
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

def page_url(query, page):
    """"healthcare" -> .../healthcare-jobs?sort=1; page N appends -N to the
    slug; a "?k=v" suffix on the query becomes extra URL params."""
    slug, _, extra = query.partition("?")
    slug = slug if slug.endswith("-jobs") else slug + "-jobs"
    path = SEARCH_PATH + slug + ("-{}".format(page) if page > 1 else "")
    return SITE_BASE + path + "?sort=1" + ("&" + extra if extra else "")


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
    probe = page_url(SEARCH_QUERIES[0], 1)
    if not rp.can_fetch(USER_AGENT, probe):
        sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt check passed for %s", probe)
    return rp


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S)


def extract_results(html):
    """Return the searchresult.data dict from a listing page, or None."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
        return (payload["props"]["pageProps"]["initialState"]
                ["jsrp"]["searchresult"]["data"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def fetch_page(session, robots, query, page):
    """Fetch one listing page. Returns searchresult data dict or None."""
    url = page_url(query, page)
    if robots is not None and not robots.can_fetch(USER_AGENT, url):
        log.warning("robots.txt disallows %s — skipping", url)
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
            if 400 <= resp.status_code < 500:   # permanent — don't retry
                log.warning("HTTP %d at %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            data = extract_results(resp.text)
            if data is None:
                last_error = "no __NEXT_DATA__ payload"
                continue
            return data
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES,
              last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(record, query, signal):
    title = clean_value(record.get("jJT"))
    category, ambiguous = classify_category(title)
    needs_review = ambiguous or signal == "needs_review"

    salary_raw, sal_min, sal_max, sal_period = parse_salary(record.get("jSal"))
    exp_min, exp_max = parse_experience(record.get("jExp"))
    job_type, employment_type, _ = decode_job_type(record)

    locations = record.get("jLoc") or []
    if not isinstance(locations, list):
        locations = [str(locations)]
    locations = [clean_value(l) for l in locations if clean_value(l)]

    slug = clean_value(record.get("jSlug"))
    return {
        "source": SITE,
        "job_id": clean_value(record.get("id")),
        "title": title,
        "company": clean_value(record.get("jCName")),
        "company_id": clean_value(record.get("jCID")),
        "location": ", ".join(locations),
        "salary_raw": salary_raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": sal_period,
        "job_type": job_type,
        "employment_type": employment_type,
        "is_walkin": "true" if record.get("jJobType") == 2 else "false",
        "work_mode": clean_value(record.get("jWM")),
        "experience_raw": clean_value(record.get("jExp")),
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "industry": clean_value(record.get("jInd")),
        "keywords": clean_value(record.get("jKwd")),
        "category": category,
        "company_type": classify_company_type(record.get("jCName")),
        "match_signal": "{}:{}".format(query, signal),
        "needs_review": "true" if needs_review else "false",
        "posted_date": parse_date(record.get("jPDate")),
        "expires_date": parse_date(record.get("jExpDate")),
        "description": truncate_description(strip_html(record.get("jJD") or "")),
        "job_url": SITE_BASE + "/jobs/" + slug if slug else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def club_salary(salary_raw):
    """Full-figure INR amounts + period for the club schema.

    The rich CSV stores normalized monthly values (spec §3); the club export
    carries the ORIGINAL full amounts with their original period, matching
    export_club_csv.py: "Rs 4.0 - 4.5 Lakh/Yr" -> (400000, 450000, per_annum).
    """
    raw = _clean(salary_raw)
    if not raw or raw.lower() in ("not disclosed", "[salary hidden]"):
        return ("", "", "", "")
    lo, hi, period = _salary_amounts(raw)
    if lo is None:
        return ("", "", "", "")
    club_period = "per_annum" if period == "Yr" else "per_month"
    return (str(int(round(lo))), str(int(round(hi))), club_period, "INR")


def rich_row_to_club_row(r):
    job_type = _clean(r.get("job_type"))
    employment_type = _clean(r.get("employment_type"))
    if employment_type == "Work from home":
        club_type = "remote"
    elif job_type == "Part time":
        club_type = "part_time"
    else:
        club_type = "full_time"

    city = _clean(r.get("location")).split(",")[0].strip() or "All India"
    lo, hi, period, currency = club_salary(r.get("salary_raw"))

    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        "city_name": city,
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": club_type,
        "category": _clean(r.get("category")) or "non_clinical",
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": _clean(r.get("experience_min_years")),
        "max_experience": _clean(r.get("experience_max_years")),
        "min_salary": lo,
        "max_salary": hi,
        "salary_period": period,
        "salary_currency": currency,
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


def crawl_query(session, robots, query, cutoff, known_ids, counters,
                new_rows, review_log, max_pages, limit):
    """Crawl one query slug newest-first. Returns "cap" | "dated" | "end"."""
    empty_pages, num_pages = 0, None
    for page in range(1, max_pages + 1):
        data = fetch_page(session, robots, query, page)
        if data is None:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                return "end"
            continue
        empty_pages = 0
        if num_pages is None:
            num_pages = data.get("num_pages")
            log.info("[%s] %s results, %s pages", query,
                     data.get("count"), num_pages)

        results = data.get("results") or []
        if not results:
            return "end"

        page_all_old = True
        for record in results:
            counters["scanned"] += 1
            try:
                posted = parse_date(record.get("jPDate"))
                # Undated jobs can't be placed in the window; sort=1 is
                # newest-first, so treat them as in-window and let dedup work.
                is_recent = (not posted) or posted >= cutoff
                if is_recent:
                    page_all_old = False
                else:
                    counters["excluded_old"] += 1
                    continue

                keep, signal = is_healthcare(record.get("jJT"),
                                             record.get("jInd"))
                if not keep:
                    counters["excluded_non_healthcare"] += 1
                    continue

                job_id = clean_value(record.get("id"))
                if not job_id:
                    log.warning("[%s] page %d: record without id — skipped",
                                query, page)
                    continue
                if job_id in known_ids:
                    counters["duplicates"] += 1
                    continue

                row = job_to_rich_row(record, query, signal)
            except Exception as exc:   # never let one card crash the run
                log.warning("[%s] page %d: skipping malformed record (%s)",
                            query, page, exc)
                continue

            if row["needs_review"] == "true":
                counters["needs_review"] += 1
                review_log.append({
                    "job_id": row["job_id"], "title": row["title"],
                    "company": row["company"], "industry": row["industry"],
                    "match_signal": row["match_signal"]})
            known_ids.add(job_id)
            new_rows.append(row)
            counters["new"] += 1
            if limit is not None and counters["new"] >= limit:
                return "end"

        if page_all_old:
            log.info("[%s] page %d entirely older than %s — stopping",
                     query, page, cutoff)
            return "dated"
        if num_pages is not None and page >= num_pages:
            return "end"
    return "cap"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from shine.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_QUERY,
                        metavar="N",
                        help="page cap PER QUERY (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs total (for test runs)")
    parser.add_argument("--queries", default=None, metavar="A,B,C",
                        help="comma-separated query slugs (default: built-in "
                             "list of {})".format(len(SEARCH_QUERIES)))
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark; keep jobs posted "
                             "on/after this date")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    queries = ([q.strip() for q in args.queries.split(",") if q.strip()]
               if args.queries else SEARCH_QUERIES)

    session = make_session()
    robots = check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s%s",
             len(known_ids), cutoff, " (--since override)" if args.since else "")

    counters = {"scanned": 0, "excluded_non_healthcare": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    new_rows, review_log, capped_queries = [], [], []

    for query in queries:
        outcome = crawl_query(session, robots, query, cutoff, known_ids,
                              counters, new_rows, review_log,
                              args.max_pages, args.limit)
        log.info("[%s] done (%s); %d new so far", query, outcome, counters["new"])
        if outcome == "cap":
            capped_queries.append(query)
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
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        review_df = pd.DataFrame(review_log)
        existing_review = load_existing(NEEDS_REVIEW_CSV)
        if existing_review is not None:
            review_df = pd.concat([existing_review, review_df],
                                  ignore_index=True)
            review_df = review_df.drop_duplicates(subset="job_id", keep="first")
        review_df.to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_df))

    print("\n===== Run summary =====")
    print("Jobs scanned:              {:>6,}".format(counters["scanned"]))
    print("Excluded (non-healthcare): {:>6,}".format(counters["excluded_non_healthcare"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))
    if capped_queries:
        print("\n  NOTE: page cap ({} pages) hit before the date window closed"
              "\n  for: {}. Coverage of those queries is truncated — shine"
              "\n  re-dates reposts, so deeper pages may still hold in-window"
              "\n  jobs. Re-run with a higher --max-pages to go deeper."
              .format(args.max_pages, ", ".join(capped_queries)))


if __name__ == "__main__":
    main()
