#!/usr/bin/env python3
"""Scrape job listings from swaasa.com (healthcare job portal, India).

Data source
-----------
swaasa.com runs on the Phenom People career-site platform. The site's nav
sub-pages (Abroad Jobs, Healthcare Jobs, Freshers Jobs, Latest Jobs,
`c/<category>-jobs`, …) are all filtered views over ONE search index, so this
scraper crawls only the master index and keeps `category` / `country` as
columns instead of visiting sub-pages (which would only re-fetch overlapping
slices).

Primary source is Phenom's JSON widget endpoint (robots.txt does not disallow
it — only `*/px-widgets`, tracking and apply paths are disallowed):

    POST https://www.swaasa.com/widgets
        {"ddoKey": "refineSearch", "from": N, "size": 50,
         "sort": {"order": "desc", "field": "postedDate"}, ...}

which returns the same `refineSearch` payload the search-results page embeds
in `phApp.ddo`, sorted newest-first, 50 jobs per page: title, jobId,
category, city/state/country, salary string, description teaser, postedDate,
company name, jobSeqNo. Total index ~22,900 jobs but only a handful post per
day, so the watermark stop keeps daily runs to a few pages.

Detail pages (`/in/en/job/<jobSeqNo>`) embed `phApp.ddo.jobDetail` with the
FULL description; they are fetched for NEW jobs only (default on, small
volume; `--no-details` skips).

Quirks
------
* Salary strings are free-form: "₹3,00,000 – ₹5,00,000 per Year",
  "18000-30000 monthly", "25,000", "890000", "Best in Industry", empty.
  Amounts are captured never filtered; when no period is stated, amounts
  >= 100,000 are read as per-year, below as per-month (Indian norms).
* The `country` field is dirty (states/cities/odd countries leak in);
  anything not in COUNTRY_META is treated as India, the site's home market.
* `category` is a healthcare taxonomy (Doctor, Nursing, Pharmacist, …).
  A few adjacent categories (Sales/Marketing, Insurance, …) are kept but
  flagged needs_review when the title shows no healthcare keyword.

Outputs
-------
* swaasa_jobs.csv                         — rich cumulative store (dedup: jobId)
* ../../jobs_csv/<DD-MM-YYYY>/swaasa.csv  — HealthCareers.club 22-col schema

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

SITE = "swaasa"
SITE_BASE = "https://www.swaasa.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
WIDGETS_URL = SITE_BASE + "/widgets"
JOB_URL_TMPL = SITE_BASE + "/in/en/job/{}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 50
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "swaasa_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Site categories that are healthcare-industry but not obviously clinical;
# kept, and flagged needs_review unless the title carries a healthcare word.
AMBIGUOUS_CATEGORIES = {
    "sales/marketing", "insurance business development", "insurance operations",
    "insurance sales", "insurance claims & tpa services", "other",
    "data science & analytics", "supply chain & procurement",
    "product management", "healthcare it & engineering",
}

ALLOW_TITLE_RE = re.compile(
    r"medical|pharma|health|nurs|doctor|clinic|hospital|\blab\b|diagnost|"
    r"patient|dental|surgi|therap|physio|radiol|patholog|ayurved|wellness|"
    r"\bmr\b|life ?science", re.IGNORECASE)

# country name (lowercased) -> (ISO code, dial code); everything else is
# treated as India — the site's own country facet leaks states and cities.
COUNTRY_META = {
    "india": ("IN", "+91"),
    "united arab emirates": ("AE", "+971"),
    "saudi arabia": ("SA", "+966"),
    "qatar": ("QA", "+974"),
    "kuwait": ("KW", "+965"),
    "bahrain": ("BH", "+973"),
    "oman": ("OM", "+968"),
    "singapore": ("SG", "+65"),
    "malaysia": ("MY", "+60"),
    "maldives": ("MV", "+960"),
    "united kingdom": ("GB", "+44"),
    "united states": ("US", "+1"),
    "germany": ("DE", "+49"),
    "ireland": ("IE", "+353"),
    "australia": ("AU", "+61"),
    "canada": ("CA", "+1"),
    "new zealand": ("NZ", "+64"),
    "netherlands": ("NL", "+31"),
    "tajikistan": ("TJ", "+992"),
}

RICH_COLUMNS = [
    "source", "job_id", "job_seq_no", "title", "company", "city", "state",
    "country", "country_code", "country_dial_code", "salary_raw",
    "salary_min", "salary_max", "salary_period", "salary_currency",
    "job_type", "site_category", "category", "company_type", "skills",
    "needs_review", "posted_date", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("swaasa_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FOREIGN_CUR_RE = re.compile(r"\b(AED|SAR|QAR|KWD|BHD|OMR|USD|GBP|EUR)\b|[$£€]")
_YEAR_RE = re.compile(r"per\s*year|year|annum|p\.?a\b|yearly|annual|lpa", re.IGNORECASE)
_MONTH_RE = re.compile(r"per\s*month|month|p\.?m\b", re.IGNORECASE)


def parse_salary(raw):
    """Parse swaasa's free-form salary strings.

    Real examples: "₹3,00,000 – ₹5,00,000 per Year", "18000-30000 monthly",
    "25,000", "30,000- 32,000", "890000", "10,00000- 12,00000",
    "₹6,00,000 - ₹80,00,000", "Best in Industry", "".

    Returns {} when no amount is stated (never invents values), else
    salary_min/max (int), salary_period (club enum), salary_currency.
    When no period is written: max >= 100,000 reads as per-year, else
    per-month (Indian salary norms). Non-INR/USD currencies keep the raw
    string only (club schema accepts INR/USD alone).
    """
    text = clean_text(raw)
    if not text:
        return {}
    numbers = [float(n.replace(",", "")) for n in _NUM_RE.findall(text)]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return {"salary_raw": text[:120]}

    foreign = _FOREIGN_CUR_RE.search(text)
    if foreign and foreign.group(0) not in ("USD", "$"):
        return {"salary_raw": text[:120]}
    currency = "USD" if foreign else "INR"

    lo, hi = numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]
    if hi < lo:
        lo, hi = hi, lo
    if _YEAR_RE.search(text):
        period = "per_annum"
    elif _MONTH_RE.search(text):
        period = "per_month"
    else:
        period = "per_annum" if hi >= 100_000 else "per_month"
    return {"salary_raw": text[:120], "salary_min": int(round(lo)),
            "salary_max": int(round(hi)), "salary_period": period,
            "salary_currency": currency}


_JUNK_TITLE_RE = re.compile(
    r"^\s*test\b|\btest jobs?\b|\bdummy\b|asdf|qwer|rtyui", re.IGNORECASE)

_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"[a-z]+ologist|intensivist|hospitalist|anaesthetist|anesthetist|"
    r"obstetrician|p(a?)ediatrician|psychiatrist|"
    r"medical superintendent|medical director|"
    r"medical affairs|medical science liaison|\bmsl\b", re.IGNORECASE)

# Employers file sales/support roles under clinical site categories (e.g.
# pharma BDE jobs under "Doctor"); these titles never inherit the category.
_NON_CLINICAL_TITLE_RE = re.compile(
    r"business development|\bsales\b|marketing|tele ?call|receptionist|"
    r"accountant|\bhr\b|\badmin", re.IGNORECASE)

_CATEGORY_FALLBACK = {
    "doctor": "doctors",
    "dentist": "doctors",
    "medical affair": "doctors",
    "nursing": "nurses",
    "nurse": "nurses",
    "pharmacist": "pharmacists",
}


def classify_category(title, site_category):
    """Map to the club category enum; title regexes win over the site
    category so e.g. a "Staff Nurse" filed under Healthcare maps to nurses.
    Returns (category, needs_review)."""
    title = title or ""
    cat_lower = (site_category or "").strip().lower()
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title):
        category = "pharmacists"
    elif _DOCTOR_RE.search(title):
        category = "doctors"
    elif _NON_CLINICAL_TITLE_RE.search(title):
        category = "non_clinical"
    else:
        category = _CATEGORY_FALLBACK.get(cat_lower, "non_clinical")
    needs_review = bool(_JUNK_TITLE_RE.search(title)) or (
        cat_lower in AMBIGUOUS_CATEGORIES
        and not ALLOW_TITLE_RE.search(title))
    return category, needs_review


_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|clinical|biotech|"
    r"diagnostic|life ?science", re.IGNORECASE)
_PHARMA_CATEGORY_RE = re.compile(
    r"pharma|medical representative", re.IGNORECASE)


def classify_company_type(company, site_category):
    """club enum hospital|pharma."""
    if _PHARMA_RE.search(company or "") or _PHARMA_CATEGORY_RE.search(site_category or ""):
        return "pharma"
    return "hospital"


_PAREN_RE = re.compile(r"\([^)]*\)")


def city_from_title(title):
    """Best-effort city from titles like "Nurse Jobs in Aster Medcity, Kochi"
    — the last comma segment, when it looks like a place name. Used only when
    the listing itself carries no location at all (mostly QA postings)."""
    text = _PAREN_RE.sub(" ", title or "")
    text = re.split(r"\s+at\s+", text)[0]
    if "," not in text:
        return ""
    segment = clean_text(text.rsplit(",", 1)[1])
    if 1 <= len(segment.split()) <= 3 and not re.search(r"jobs?|test", segment, re.I):
        return segment
    return ""


def country_meta(country):
    """(name, iso, dial); unknown/dirty values collapse to India."""
    key = clean_text(country).lower()
    if key in COUNTRY_META:
        code, dial = COUNTRY_META[key]
        return clean_text(country), code, dial
    return "India", "IN", "+91"


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
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (WIDGETS_URL, JOB_URL_TMPL.format("SWJOB")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, method, url, *, payload=None, as_json=True):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url, json=payload,
                                   timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_search_page(session, offset):
    """One page of the master search index, newest-first."""
    payload = {
        "lang": "en_in", "deviceType": "desktop", "country": "in",
        "pageName": "search-results", "ddoKey": "refineSearch",
        "from": offset, "size": PAGE_SIZE, "jobs": True, "counts": True,
        "all_fields": ["category", "type", "country", "state", "city"],
        "keywords": "", "global": True, "selected_fields": {},
        "sort": {"order": "desc", "field": "postedDate"}, "locationData": {},
    }
    data = _request(session, "POST", WIDGETS_URL, payload=payload)
    if not data:
        return None
    return ((data.get("refineSearch") or {}).get("data") or {}).get("jobs")


_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*")


def fetch_full_description(session, job_seq_no):
    """Full description from the detail page's phApp.ddo.jobDetail."""
    page = _request(session, "GET", JOB_URL_TMPL.format(job_seq_no),
                    as_json=False)
    if not page:
        return ""
    m = _DDO_RE.search(page)
    if not m:
        return ""
    try:
        ddo, _ = json.JSONDecoder().raw_decode(page[m.end():])
        job = ((ddo.get("jobDetail") or {}).get("data") or {}).get("job") or {}
        return strip_html(job.get("description") or "")
    except (ValueError, TypeError) as exc:
        log.warning("Could not parse detail ddo for %s: %s", job_seq_no, exc)
        return ""


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    title = clean_text(job.get("title"))
    site_category = clean_text(job.get("category"))
    category, needs_review = classify_category(title, site_category)
    company = clean_text(job.get("jobCompanyName"))
    country_name, code, dial = country_meta(job.get("country"))
    job_seq_no = clean_text(job.get("jobSeqNo"))
    # many listings omit city/state; fall back to "City, State, Country",
    # then to the title itself (QA postings carry no location fields at all)
    city = clean_text(job.get("city"))
    if not city:
        location = clean_text(job.get("location") or job.get("cityStateCountry"))
        city = location.split(",")[0].strip() if location else city_from_title(title)
    # no company AND no location data — hallmark of the site's test postings
    if not company and not clean_text(job.get("location")) and not clean_text(job.get("city")):
        needs_review = True

    row = {
        "source": SITE,
        "job_id": clean_text(job.get("jobId")) or job_seq_no,
        "job_seq_no": job_seq_no,
        "title": title,
        "company": company,
        "city": city,
        "state": clean_text(job.get("state")),
        "country": country_name,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": "full_time",   # site lists openings; no employment-type field
        "site_category": site_category,
        "category": category,
        "company_type": classify_company_type(company, site_category),
        "skills": "; ".join((job.get("ml_skills") or [])[:15]),
        "needs_review": needs_review,
        "posted_date": clean_text(job.get("postedDate"))[:10],
        "description": clean_text(job.get("descriptionTeaser"))[:DESCRIPTION_MAX_CHARS],
        "job_url": JOB_URL_TMPL.format(job_seq_no) if job_seq_no else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    parsed = parse_salary(job.get("salary"))
    row.update(parsed)
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
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
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
        "min_experience": "",
        "max_experience": "",
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
        "is_active": "true",
        "expires_at": "",
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
    parser = argparse.ArgumentParser(description="Scrape jobs from swaasa.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N search pages of %d (test runs)" % PAGE_SIZE)
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (teaser descriptions only; faster)")
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
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    offset, page_count, empty_pages = 0, 0, 0

    while True:
        if args.max_pages is not None and page_count >= args.max_pages:
            break
        jobs = fetch_search_page(session, offset)
        page_count += 1
        offset += PAGE_SIZE
        if not jobs:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        empty_pages = 0

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:
                log.warning("Skipping malformed job %s: %s", job.get("jobId"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "site_category": row["site_category"]})
            if not args.no_details and row["job_seq_no"]:
                full = fetch_full_description(session, row["job_seq_no"])
                if full:
                    row["description"] = full[:DESCRIPTION_MAX_CHARS]
                else:
                    counters["detail_failed"] += 1
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        # newest-first: once a whole page is older than the cutoff, stop.
        if page_all_old or len(jobs) < PAGE_SIZE:
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

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
