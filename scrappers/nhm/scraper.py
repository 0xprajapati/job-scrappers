#!/usr/bin/env python3
"""Scrape recruitment/vacancy notices from nhm.gov.in (National Health Mission).

Data source
-----------
nhm.gov.in is the Ministry of Health & Family Welfare's central NHM portal.
It is a NIC PHP/CMS site (`index1.php`/`index4.php` pages) that publishes
guidelines, reports and announcements — it has NO structured job board and
no JSON API. Its "Join NHM" page (index4.php?linkid=363&lid=492) is empty;
actual NHM hiring is advertised on the individual state NHM portals
(nhm.assam.gov.in, upnrhm.gov.in, nhm.maharashtra.gov.in, ...).

What the central portal *does* occasionally carry is a recruitment
advertisement PDF in its announcement surfaces (homepage "What's New"
ticker, highlights.php, Join NHM, Archives). This scraper therefore:

1. fetches each ANNOUNCEMENT_PAGES surface (plain server-rendered HTML),
2. extracts every anchor (label + href),
3. keeps links whose label/filename matches RECRUIT_TITLE_RE
   (vacancy / recruitment / walk-in / applications invited / engagement
   of consultant / advertisement for the post ...),
4. classifies, dedups on the link URL and appends to the cumulative CSV.

Every captured row is flagged `needs_review=True` — these are notice PDFs,
not parsed job cards, so a human should verify before import.

Spec deviations (documented per master-scraper-spec §4):
* Announcement links carry no machine-readable posted date, so
  `posted_date` is the date the notice was FIRST SEEN by this scraper and
  the watermark/time-window logic is a no-op; dedup alone provides
  idempotency (running twice adds 0 rows).
* At the time this scraper was written (July 2026) the portal carried zero
  recruitment notices, so an empty run summary is the expected steady
  state, not a failure.

Outputs
-------
* nhm_jobs.csv                          — rich cumulative store (dedup: job_id)
* needs_review.csv                      — every new notice (all need review)
* ../../jobs_csv/<DD-MM-YYYY>/nhm.csv   — HealthCareers.club schema
                                          (written only when rows exist)

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "nhm"
SITE_BASE = "https://nhm.gov.in"
ROBOTS_URL = SITE_BASE + "/robots.txt"

# Announcement surfaces that server-render links to notice PDFs.
ANNOUNCEMENT_PAGES = [
    SITE_BASE + "/",                                            # What's New ticker
    SITE_BASE + "/highlights.php",                              # highlights archive
    SITE_BASE + "/index4.php?lang=1&level=0&linkid=363&lid=492",  # Join NHM
    SITE_BASE + "/index1.php?lang=1&level=1&sublinkid=1241&lid=620",  # Archives
]

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0

RICH_CSV = "nhm_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COUNTRY_NAME = "India"
COUNTRY_CODE = "IN"
COUNTRY_DIAL = "+91"
DEFAULT_CITY = "New Delhi"          # NHM/MoHFW national offices
COMPANY_NAME = "National Health Mission (MoHFW)"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "country",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "salary_currency", "job_type", "category", "needs_review",
    "posted_date", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("nhm_scraper")

# ----------------------------------------------------------------------------
# Parsing / classification
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ANCHOR_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)

# A link is a recruitment notice when its label or filename says so.
RECRUIT_TITLE_RE = re.compile(
    r"vacanc|recruit|walk[\s-]?in|applications?\s+(?:are\s+)?invited|"
    r"engagement\s+of|hiring\s+of|advertisement\s+for\s+(?:the\s+)?post|"
    r"contractual\s+(?:post|position|engagement)|job\s+opening|"
    r"expression\s+of\s+interest\s+for\s+(?:individual\s+)?consultant",
    re.IGNORECASE)

# Guidelines/reports that mention cadres but are not hiring notices.
DENY_TITLE_RE = re.compile(
    r"guideline|training\s+module|operational|report|newsletter|minutes|"
    r"curriculum|compendium|factsheet|survey|framework|policy",
    re.IGNORECASE)

_NURSE_RE = re.compile(r"\b(nurse|nursing|midwif|anm|gnm)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\b(pharmacist|pharmacy)\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|mbbs|dentist|medical officer|specialist)\b",
    re.IGNORECASE)


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def extract_links(page_html, base_url):
    """All (absolute_url, label) anchor pairs from server-rendered HTML."""
    out = []
    for m in _ANCHOR_RE.finditer(page_html or ""):
        href = clean_text(m.group(1))
        label = clean_text(_TAG_RE.sub(" ", m.group(2)))
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        out.append((urljoin(base_url, href), label))
    return out


def is_recruitment_notice(label, url):
    """True when the anchor looks like a hiring notice, not a guideline."""
    text = "{} {}".format(label or "", urlparse(url or "").path)
    if not RECRUIT_TITLE_RE.search(text):
        return False
    # "Recruitment Rules Guidelines" style docs: recruit-ish word present
    # but clearly a document, not an ad — drop only when a deny word hits
    # and no strong ad phrase ("applications invited", "walk-in") appears.
    strong = re.search(r"applications?\s+(?:are\s+)?invited|walk[\s-]?in",
                       text, re.IGNORECASE)
    if DENY_TITLE_RE.search(text) and not strong:
        return False
    return True


def classify_category(title):
    """HealthCareers.club category enum for a notice title."""
    if _NURSE_RE.search(title or ""):
        return "nurses"
    if _PHARM_RE.search(title or ""):
        return "pharmacists"
    if _DOCTOR_RE.search(title or ""):
        return "doctors"
    return "non_clinical"   # consultants / programme staff — NHM's usual ads


def make_job_id(url):
    """Stable id from the notice URL path (dedup key)."""
    parsed = urlparse(url or "")
    path = (parsed.path or "").strip("/")
    if parsed.query:
        path += "?" + parsed.query
    return path or url


def notice_to_rich_row(url, label):
    title = label or Path(urlparse(url).path).stem.replace("-", " ")
    return {
        "source": SITE,
        "job_id": make_job_id(url),
        "title": title[:200],
        "company": COMPANY_NAME,
        "city": DEFAULT_CITY,
        "country": COUNTRY_NAME,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": "full_time",
        "category": classify_category(title),
        "needs_review": True,     # notice PDF, not a parsed job card
        "posted_date": date.today().isoformat(),  # first-seen date (no date on site)
        "description": "Recruitment notice published on nhm.gov.in: {}. "
                       "See the linked notice for post details, eligibility "
                       "and how to apply.".format(title[:200]),
        "job_url": url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else value


def rich_row_to_club_row(r):
    return {
        "country_name": _blank(r.get("country")) or COUNTRY_NAME,
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": _blank(r.get("city")) or DEFAULT_CITY,
        "company_name": _blank(r.get("company")) or COMPANY_NAME,
        "company_type": "hospital",   # govt health mission; schema's general default
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
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        "expires_at": "",
    }


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def check_robots(session):
    """nhm.gov.in serves a custom 404 HTML page at /robots.txt (no rules)."""
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        body = resp.text if resp.status_code < 400 else ""
        if "<html" in body.lower():   # 404 page served with HTTP 200
            body = ""
        rp.parse(body.splitlines())
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in ANNOUNCEMENT_PAGES:
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch_page(session, url):
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
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


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
        description="Scrape recruitment notices from nhm.gov.in.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
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
    log.info("Existing CSV has %d known notices", len(known_ids))

    counters = {"pages": 0, "links": 0, "notices": 0,
                "new": 0, "duplicates": 0}
    new_rows, review_log = [], []
    seen_this_run = set()

    for page_url in ANNOUNCEMENT_PAGES:
        page_html = fetch_page(session, page_url)
        if page_html is None:
            continue
        counters["pages"] += 1
        for url, label in extract_links(page_html, page_url):
            counters["links"] += 1
            try:
                if not is_recruitment_notice(label, url):
                    continue
                counters["notices"] += 1
                row = notice_to_rich_row(url, label)
            except Exception as exc:      # one bad anchor must not kill the run
                log.warning("Skipping malformed link %s: %s", url, exc)
                continue
            if row["job_id"] in known_ids or row["job_id"] in seen_this_run:
                counters["duplicates"] += 1
                continue
            seen_this_run.add(row["job_id"])
            new_rows.append(row)
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "found_on": page_url})
            counters["new"] += 1
            log.info("New notice: %s", row["title"])

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
        if existing_df is None:
            combined.to_csv(args.output, index=False)  # header-only first run
        log.info("No new notices; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Pages fetched:         {:>5,}".format(counters["pages"]))
    print("Links scanned:         {:>5,}".format(counters["links"]))
    print("Recruitment notices:   {:>5,}".format(counters["notices"]))
    print("New notices added:     {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if counters["notices"] == 0:
        print("\nNote: nhm.gov.in is a policy portal; zero notices is the "
              "normal steady state.\nActual NHM hiring happens on the state "
              "NHM portals (see readme.md).")


if __name__ == "__main__":
    main()
