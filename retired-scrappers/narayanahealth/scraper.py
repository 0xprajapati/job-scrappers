#!/usr/bin/env python3
"""Scrape job listings from jobs.narayanahealth.org (Narayana Health).

Data source
-----------
www.narayanahealth.org/careers has moved; the live portal is
jobs.narayanahealth.org — an SAP SuccessFactors Career Site Builder (RMK /
"Jobs2Web") site for Narayana Hrudayalaya Limited. Being a hospital chain's
own ATS, every posting is healthcare-industry at the source (§2 of the
master spec: filter at source).

robots.txt disallows the JSON backends (/services/, /applybutton/, ...) but
allows /search/ and the job detail pages, so per §1 of the master spec we
use the server-rendered HTML:

* Listing (newest-first, 10 rows/page, early stop once a page is entirely
  older than the cutoff):

      GET /search/?q=&sortColumn=referencedate&sortOrder=desc&startrow=N

  Each <tr class="data-row"> carries the title, the job URL
  (/NH-India/job/<City-Title-State-Postal>/<job_id>/), the location
  ("Jaipur, RJ, IN, 302033"), the posted date ("24 Jul 2026") and the
  requisition id (jobFacility column).

* Detail pages (fetched for NEW jobs only; `--no-details` skips) embed
  schema.org JobPosting microdata: datePosted, validThrough,
  hiringOrganization, plus the full HTML description inside
  <span class="jobdescription">.

Quirks
------
* No salary anywhere on the site -> salary_raw is always "Not Disclosed"
  (master spec §3: capture, never filter, never invent).
* No employment-type field -> job_type defaults to full_time.
* The requisition id (e.g. 14998) is display-only; the stable dedup key is
  the numeric id in the job URL (e.g. 58185744).

Outputs
-------
* narayanahealth_jobs.csv                         — rich cumulative store
  (dedup key: URL job id)
* ../../jobs_csv/<DD-MM-YYYY>/narayanahealth.csv  — HealthCareers.club
  22-col schema

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
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

SITE = "narayanahealth"
SITE_BASE = "https://jobs.narayanahealth.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SEARCH_URL = SITE_BASE + "/search/"
DEFAULT_COMPANY = "Narayana Hrudayalaya Limited"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): the site never shows salaries anyway.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 10                  # rows per search page (site-fixed)
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
MAX_PAGES_SAFETY = 200          # ~116 jobs today; hard stop regardless
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "narayanahealth_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# ISO country code (from the location line) -> (name, dial code). NH also
# runs Health City Cayman Islands, so keep the map extensible.
COUNTRY_META = {
    "IN": ("India", "+91"),
    "KY": ("Cayman Islands", "+1345"),
    "AE": ("United Arab Emirates", "+971"),
}

RICH_COLUMNS = [
    "source", "job_id", "requisition_id", "title", "company", "city",
    "state", "postal_code", "country", "country_code", "country_dial_code",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "salary_currency", "job_type", "category", "company_type",
    "needs_review", "posted_date", "valid_through", "description",
    "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("narayanahealth_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


def parse_listing_date(raw):
    """"24 Jul 2026" (search rows) -> "2026-07-24"; "" when unparseable."""
    raw = clean_text(raw)
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d %b %Y").date().isoformat()
    except ValueError:
        return ""


def parse_meta_date(raw):
    """"Fri Jul 24 00:00:00 UTC 2026" (microdata) -> "2026-07-24"."""
    raw = clean_text(raw)
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S UTC %Y").date().isoformat()
    except ValueError:
        return ""


def parse_location(raw):
    """"Jaipur, RJ, IN, 302033" -> (city, state, country_code, postal).

    The site renders "City, State, CC, Postal"; state can equal the city
    ("New Delhi, New Delhi, IN, 110096"). Missing parts come back "".
    """
    parts = [clean_text(p) for p in str(raw or "").split(",") if clean_text(p)]
    city = parts[0] if parts else ""
    rest = parts[1:]
    postal = rest.pop() if rest and re.fullmatch(r"\d{4,10}", rest[-1]) else ""
    country_code = rest.pop() if rest and re.fullmatch(r"[A-Z]{2}", rest[-1]) else ""
    state = rest[0] if rest else ""
    # Some listings carry only the country ("IN"): that's not a city.
    if not country_code and re.fullmatch(r"[A-Z]{2}", city):
        country_code, city = city, ""
    return city, state, country_code, postal


def country_meta(country_code):
    name, dial = COUNTRY_META.get(country_code or "IN", ("India", "+91"))
    return name, (country_code or "IN"), dial


def job_id_from_url(url):
    """/NH-India/job/Jaipur-Junior-Executive-RJ-302033/58185744/ -> 58185744"""
    m = re.search(r"/(\d+)/?$", str(url or ""))
    return m.group(1) if m else ""


_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"consultant|registrar|intensivist|hospitalist|[a-z]+ologist|"
    r"anaesthetist|anesthetist|obstetrician|p(a?)ediatrician|psychiatrist|"
    r"\bdnb\b|\bdmo\b|clinical associate|junior resident|senior resident",
    re.IGNORECASE)

# "Consultant"/"Registrar" titles are doctors only when nothing marks them
# as corporate ("Consultant - Finance" must not become a doctor).
_DOCTOR_TITLE_NEEDS_CONTEXT_RE = re.compile(r"consultant|registrar",
                                            re.IGNORECASE)
_NON_CLINICAL_TITLE_RE = re.compile(
    r"business development|\bsales\b|marketing|tele ?call|receptionist|"
    r"accountant|\bhr\b|human resource|\badmin|\bit\b|finance|billing|"
    r"housekeep|security|logistic|store|purchase|engineer|legal|audit",
    re.IGNORECASE)

# Healthcare signal for needs_review flagging (the site is a hospital
# chain's own ATS, so nothing is dropped — generic corporate titles with no
# healthcare word are only flagged).
_HEALTHCARE_SIGNAL_RE = re.compile(
    r"medical|pharma|health|nurs|doctor|clinic|hospital|\blab\b|laborator|"
    r"diagnost|patient|dental|surgi|therap|physio|radiol|patholog|dialysis|"
    r"icu|ward|emergency|paramedic|technician|cath|blood ?bank|cssd|"
    r"anaesth|anesth|biomedical|transplant|oncolog|cardiac|dietic|dietit",
    re.IGNORECASE)


def classify_category(title):
    """Map a title to the club category enum (doctors|nurses|pharmacists|
    non_clinical). Returns (category, needs_review)."""
    title = clean_text(title)
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title):
        category = "pharmacists"
    elif _DOCTOR_RE.search(title):
        if (_DOCTOR_TITLE_NEEDS_CONTEXT_RE.search(title)
                and _NON_CLINICAL_TITLE_RE.search(title)):
            category = "non_clinical"
        else:
            category = "doctors"
    else:
        category = "non_clinical"
    needs_review = category == "non_clinical" and \
        not _HEALTHCARE_SIGNAL_RE.search(title)
    return category, needs_review


# ----------------------------------------------------------------------------
# HTML extraction (RMK search rows + detail page microdata)
# ----------------------------------------------------------------------------

_ROW_RE = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
_TITLE_LINK_RE = re.compile(
    r'<a[^>]*class="jobTitle-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>|'
    r'<a[^>]*href="([^"]+)"[^>]*class="jobTitle-link"[^>]*>(.*?)</a>', re.S)
_LOCATION_RE = re.compile(r'<span class="jobLocation">\s*([^<]*?)\s*<', re.S)
_DATE_RE = re.compile(r'<span class="jobDate[^"]*">\s*([^<]+?)\s*<', re.S)
_FACILITY_RE = re.compile(r'<span class="jobFacility">\s*([^<]*?)\s*<', re.S)
_TOTAL_RE = re.compile(r"of\s*<b>\s*([\d,]+)", re.S)
_META_RE_TMPL = r'<meta itemprop="{}" content="([^"]*)"'


def parse_search_page(html):
    """One search-results page -> (rows, total_count). Bad rows are logged
    and skipped; a page with no data-rows returns ([], total)."""
    total = None
    m = _TOTAL_RE.search(html)
    if m:
        total = int(m.group(1).replace(",", ""))

    rows = []
    for block in _ROW_RE.findall(html):
        try:
            link = _TITLE_LINK_RE.search(block)
            if not link:
                continue
            href = link.group(1) or link.group(3)
            title = clean_text(link.group(2) or link.group(4))
            location = ""
            for loc in _LOCATION_RE.findall(block):
                if clean_text(loc):
                    location = clean_text(loc)
                    break
            date_match = _DATE_RE.search(block)
            facility = _FACILITY_RE.search(block)
            rows.append({
                "href": href.strip(),
                "title": title,
                "location": location,
                "posted_raw": clean_text(date_match.group(1)) if date_match else "",
                "requisition_id": clean_text(facility.group(1)) if facility else "",
            })
        except Exception as exc:            # never let one card kill the run
            log.warning("Skipping malformed search row: %s", exc)
    return rows, total


def parse_detail_page(html):
    """Detail page -> dict of microdata extras + plain-text description."""
    out = {}
    for prop, key in (("datePosted", "posted_date"),
                      ("validThrough", "valid_through")):
        m = re.search(_META_RE_TMPL.format(prop), html)
        if m:
            parsed = parse_meta_date(m.group(1))
            if parsed:
                out[key] = parsed
    m = re.search(_META_RE_TMPL.format("hiringOrganization"), html)
    if m and clean_text(m.group(1)):
        out["company"] = clean_text(m.group(1))

    start = html.find('class="jobdescription"')
    if start != -1:
        segment = html[start:]
        end = len(segment)
        for marker in ('<p class="job-location"', '<div class="applylink'):
            idx = segment.find(marker)
            if idx != -1:
                end = min(end, idx)
        description = strip_html(segment[len('class="jobdescription">'):end])
        if description:
            out["description"] = description[:DESCRIPTION_MAX_CHARS]
    return out


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
    for url in (SEARCH_URL + "?q=&startrow=0",
                SITE_BASE + "/NH-India/job/x/1/"):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed (search + job pages allowed)")


def fetch_html(session, url, params=None):
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


def fetch_search_page(session, startrow):
    html = fetch_html(session, SEARCH_URL, params={
        "q": "", "sortColumn": "referencedate", "sortOrder": "desc",
        "startrow": startrow})
    if html is None:
        return None, None
    return parse_search_page(html)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def listing_to_rich_row(listing):
    city, state, country_code, postal = parse_location(listing["location"])
    country_name, code, dial = country_meta(country_code)
    category, needs_review = classify_category(listing["title"])
    job_url = SITE_BASE + listing["href"] if listing["href"].startswith("/") \
        else listing["href"]
    return {
        "source": SITE,
        "job_id": job_id_from_url(listing["href"]),
        "requisition_id": listing.get("requisition_id", ""),
        "title": listing["title"],
        "company": DEFAULT_COMPANY,
        "city": city,
        "state": state,
        "postal_code": postal,
        "country": country_name,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",   # site never shows salaries
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": "full_time",         # no employment-type field on site
        "category": category,
        "company_type": "hospital",      # hospital chain's own ATS
        "needs_review": needs_review,
        "posted_date": parse_listing_date(listing["posted_raw"]),
        "valid_through": "",
        "description": "",
        "job_url": job_url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, extras):
    """Merge detail-page microdata into a rich row (in place)."""
    for key in ("posted_date", "valid_through", "company", "description"):
        if extras.get(key):
            row[key] = extras[key]
    return row


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def rich_row_to_club_row(r):
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state"))
        or _blank(r.get("country")) or "India",
        "company_name": _blank(r.get("company")) or DEFAULT_COMPANY,
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
        "min_salary": "",               # site never discloses salaries
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
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
        description="Scrape jobs from jobs.narayanahealth.org.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N search pages of %d (test runs)" % PAGE_SIZE)
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (no descriptions; faster)")
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
    startrow, page_count, empty_pages = 0, 0, 0
    total_records = None

    # Listing is newest-first: stop once a whole page is older than cutoff.
    while page_count < MAX_PAGES_SAFETY:
        if args.max_pages is not None and page_count >= args.max_pages:
            break
        rows, total = fetch_search_page(session, startrow)
        page_count += 1
        startrow += PAGE_SIZE
        if total is not None:
            total_records = total
        if rows is None:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        if not rows:
            break
        empty_pages = 0

        page_all_old = True
        for listing in rows:
            counters["scanned"] += 1
            try:
                row = listing_to_rich_row(listing)
            except Exception as exc:
                log.warning("Skipping malformed listing %s: %s",
                            listing.get("href"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
            if not row["job_id"]:
                log.warning("No job id in %s — skipped", listing.get("href"))
                continue
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if not args.no_details:
                detail_html = fetch_html(session, row["job_url"])
                if detail_html:
                    apply_detail(row, parse_detail_page(detail_html))
                else:
                    counters["detail_failed"] += 1
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["title"],
                                   "job_url": row["job_url"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        if page_all_old:
            log.info("Page %d entirely older than %s — stopping early",
                     page_count, cutoff)
            break
        if total_records is not None and startrow >= total_records:
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
