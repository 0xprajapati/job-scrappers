#!/usr/bin/env python3
"""Scrape healthcare job listings from apna.co into a deduplicated CSV.

Data source
-----------
apna.co is a general job board (~91k jobs), but its department listing pages
pre-filter to healthcare:

    https://apna.co/jobs/dep_healthcare_doctor_hospital_staff-jobs?page=N

Listing pages are server-rendered Next.js: every job card's full data object
is embedded in the page's __NEXT_DATA__ JSON (props.pageProps.jobs[].data),
which is far more robust than parsing card markup — and it already includes
the FIXED salary range (`fixed_min_salary`/`fixed_max_salary`) separately
from incentive-inflated `max_salary`/`earning_potential`, plus description,
education, shift, gender and dates. pageProps.totalPages bounds pagination
(25 cards per page).

The salary filter uses the FIXED lower bound: earning potential (incentives)
never counts toward the threshold. A fixed floor of 0 means "salary not
disclosed" and is excluded.

With --enrich, each NEW passing job's detail page is fetched; the app-router
payload embeds a "job_details_section" JSON structure carrying Role/Category,
Degree/Specialisation and the About-company address.

Run `python apna_scraper.py --help` for options.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE_BASE = "https://apna.co"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SOURCE = "apna.co"

# Department listing slugs to crawl. Healthcare only by default; append more
# slugs (e.g. "dep_beauty_fitness_personal_care-jobs") to widen coverage.
DEPARTMENT_SLUGS = [
    "dep_healthcare_doctor_hospital_staff-jobs",
]

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (personal research; contact: eleswarapu.madhav@gmail.com)"
)

# Time window (no salary filter — salaries are captured but never filtered on).
# First run keeps jobs posted in the last INITIAL_WINDOW_DAYS; later runs keep
# only jobs newer than the newest posted_date already in the CSV, minus
# WATERMARK_GRACE_DAYS of overlap (dedup absorbs the overlap).
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

DEFAULT_OUTPUT_CSV = "apna_jobs.csv"

WORK_MODE_TAGS = ("Work from Office", "Work from Home", "Field Job")
JOB_TYPE_TAGS = ("Full Time", "Part Time")

# Safety net (master spec §2): apna's healthcare department also carries
# aggregated external postings from big employers, and some of those are
# clearly non-healthcare. Titles matching these phrases are dropped.
DENY_TITLE_KEYWORDS = [
    "software developer", "software engineer", "web developer", "full stack",
    "front end", "frontend", "back end", "backend", "mobile app", "devops",
    "computer science", "data engineer", "network engineer", "hardware",
    "mechanical engineer", "civil engineer", "electrical engineer",
    "maintenance engineer", "design engineer", "fiber", "fibre",
    "accountant", "accounts executive", "chartered accountant", "cashier",
    "graphic designer", "ui designer", "ux designer", "video editor",
    "digital marketing", "marketing executive", "marketing manager",
    "sales executive", "sales manager", "business development",
    "telecaller", "tele caller", "receptionist", "front office", "front desk",
    "human resource", "hr executive", "hr manager", "recruiter",
    "housekeeping", "security guard", "security supervisor", "driver",
    "electrician", "plumber", "cook", "chef", "store keeper", "storekeeper",
    "purchase executive", "billing executive", "data entry",
    "bidding", "auction", "proposal manager",
]

_DENY_RE = re.compile(
    "|".join(r"\b" + re.escape(kw) + r"\b" for kw in DENY_TITLE_KEYWORDS),
    re.IGNORECASE)

CSV_COLUMNS = [
    "source", "job_id", "title", "company", "location",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "work_mode", "job_type", "experience_raw", "english_level", "department",
    "role_category", "education", "degree_specialisation", "shift", "gender",
    "posted_date", "description", "job_url", "apply_url", "scraped_at",
]

log = logging.getLogger("apna_scraper")

# ----------------------------------------------------------------------------
# Salary parsing
# ----------------------------------------------------------------------------

_SALARY_NUM_RE = re.compile(r"[0-9][0-9,]*")


def parse_salary_string(raw):
    """Parse a displayed salary like "₹60,000 - ₹80,000 monthly".

    Returns (min_monthly, max_monthly) as ints, or None if the string is
    missing or has no numbers. A single number means min == max. (A 0 lower
    bound parses as 0 — the caller's threshold check then excludes it.)
    """
    if not raw or not raw.strip():
        return None
    numbers = [int(n.replace(",", "")) for n in _SALARY_NUM_RE.findall(raw)]
    if not numbers:
        return None
    lo, hi = numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)


def compute_cutoff(existing_df):
    """Return the ISO date below which jobs are skipped.

    First run: today - INITIAL_WINDOW_DAYS. Later runs: the newest
    posted_date already saved, minus WATERMARK_GRACE_DAYS of overlap.
    """
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


def card_salary(card):
    """Extract (salary_raw, filter_min, display_max) from a card data object.

    Filtering uses the FIXED range (incentives excluded); the raw string
    mirrors what the card displays (min_salary - max_salary monthly).
    """
    disp_min, disp_max = card.get("min_salary"), card.get("max_salary")
    if disp_min is None and disp_max is None:
        return ("", None, None)
    raw = "₹{:,} - ₹{:,} monthly".format(disp_min or 0, disp_max or disp_min or 0)

    fixed_min = card.get("fixed_min_salary")
    if fixed_min is None:  # older cards may lack the split; fall back
        fixed_min = disp_min
    fixed_max = card.get("fixed_max_salary")
    if fixed_max is None:
        fixed_max = disp_max if disp_max is not None else fixed_min
    return (raw, fixed_min, fixed_max)


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    return session


def load_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        rp.parse([])
        return rp
    rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    probe = "{}/jobs/{}".format(SITE_BASE, DEPARTMENT_SLUGS[0])
    if not rp.can_fetch(USER_AGENT, probe):
        sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt check passed")
    return rp


def get_html(session, url, params=None):
    """GET with rate limiting and exponential-backoff retries."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
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
# Listing-page parsing
# ----------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_JOB_ID_RE = re.compile(r"-(\d+)/?$")
_WS_RE = re.compile(r"\s+")


def parse_listing_page(html):
    """Return (cards, total_pages) from a department listing page.

    cards is a list of the raw job data dicts embedded in __NEXT_DATA__.
    Returns (None, None) if the page structure is unrecognizable.
    """
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return (None, None)
    try:
        page_props = json.loads(match.group(1))["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError):
        return (None, None)
    cards = [j["data"] for j in page_props.get("jobs") or []
             if isinstance(j, dict) and j.get("data")]
    return (cards, page_props.get("totalPages"))


def card_to_row(card):
    # External/aggregated postings have public_url=None but public_url_v2 set.
    job_url = card.get("public_url") or card.get("public_url_v2") or ""
    id_match = _JOB_ID_RE.search(job_url)
    job_id = str(card.get("id") or (id_match.group(1) if id_match else ""))

    raw, fixed_min, fixed_max = card_salary(card)

    tags = [t.get("text", "") for t in card.get("ui_tags") or []]
    work_mode = ", ".join(t for t in tags if t in WORK_MODE_TAGS)
    if not work_mode:
        work_mode = "Work from Home" if card.get("is_wfh") else "Work from Office"
    job_type = ", ".join(t for t in tags if t in JOB_TYPE_TAGS)
    if not job_type:
        job_type = "Part Time" if card.get("is_part_time") else "Full Time"

    address = card.get("address") or {}
    location = card.get("location_name") or ""
    if not location:
        city = (address.get("city") or {}).get("name") or ""
        location = ", ".join(x for x in (address.get("area"), city) if x)

    description = _WS_RE.sub(" ", card.get("description") or "").strip()

    return {
        "source": SOURCE,
        "job_id": job_id,
        "title": (card.get("title") or "").strip(),
        "company": ((card.get("organization") or {}).get("name") or "").strip(),
        "location": location,
        "salary_raw": raw,
        "salary_min_monthly": fixed_min,
        "salary_max_monthly": fixed_max,
        "work_mode": work_mode,
        "job_type": job_type,
        "experience_raw": card.get("experience_in_years") or "",
        "english_level": card.get("english") or "",
        "department": ((card.get("department") or {}).get("name") or ""),
        "role_category": "",
        "education": card.get("education") or "",
        "degree_specialisation": "",
        "shift": card.get("shift") or "",
        "gender": card.get("gender") or "",
        "posted_date": (card.get("created_on") or "")[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": job_url,
        # external postings carry the employer's real application link
        "apply_url": card.get("external_job_url") or "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----------------------------------------------------------------------------
# Detail-page enrichment
# ----------------------------------------------------------------------------

# Maps detail-page item titles (lowercased prefixes) to CSV columns.
_SECTION_FIELD_MAP = [
    ("role", "role_category"),
    ("degree", "degree_specialisation"),
    ("education", "education"),
    ("shift", "shift"),
    ("gender", "gender"),
    ("english", "english_level"),
    ("experience", "experience_raw"),
]


def extract_detail_sections(html):
    """Parse the "job_details_section" JSON embedded in a detail page.

    The app-router payload contains it either verbatim or with escaped
    quotes. Returns {section_heading: {item_title: subtitle}} or {}.
    """
    for text in (html, html.replace('\\"', '"')):
        idx = text.find('"job_details_section":')
        if idx < 0:
            continue
        start = text.find("[", idx)
        if start < 0:
            continue
        try:
            sections, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        result = {}
        for section in sections:
            if not isinstance(section, dict):
                continue
            items = {}
            for item in section.get("data") or []:
                title = (item.get("title") or "").strip()
                subtitle = item.get("subtitle")
                if title and isinstance(subtitle, str) and subtitle.strip():
                    items[title] = subtitle.strip()
            result[(section.get("heading") or "").strip()] = items
        return result
    return {}


def enrich_row(session, row):
    """Fill role_category, degree_specialisation etc. from the detail page."""
    html = get_html(session, row["job_url"])
    if html is None:
        return
    sections = extract_detail_sections(html)
    if not sections:
        log.warning("No job_details_section on %s", row["job_url"])
        return
    flat = {}
    for items in sections.values():
        flat.update(items)
    for title, value in flat.items():
        lowered = title.lower()
        for prefix, column in _SECTION_FIELD_MAP:
            if lowered.startswith(prefix):
                row[column] = value
                break
    about = sections.get("About company") or {}
    if about.get("Address"):
        row["company_address"] = about["Address"]
    if not row["apply_url"]:  # non-external jobs: apply through apna itself
        row["apply_url"] = row["job_url"]


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_known(csv_path):
    try:
        existing = pd.read_csv(csv_path, dtype=str)
    except FileNotFoundError:
        return set(), None
    if "source" in existing.columns:
        pairs = set(zip(existing["source"].fillna(""), existing["job_id"].fillna("")))
    else:  # tolerate CSVs from before the source column existed
        pairs = {(SOURCE, j) for j in existing["job_id"].fillna("")}
    return pairs, existing


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from apna.co into a CSV.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_CSV,
                        help="output CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="crawl at most N pages per department (test runs)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each NEW passing job's detail page for "
                             "role_category, degree, company address")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    load_robots(session)

    known_pairs, existing_df = load_known(args.output)
    log.info("Existing CSV has %d known jobs", len(known_pairs))
    cutoff = compute_cutoff(existing_df)
    log.info("Keeping jobs posted on/after %s", cutoff)

    counters = {"pages": 0, "listings": 0, "excluded_old": 0,
                "excluded_deny_title": 0, "new": 0, "duplicates": 0,
                "page_errors": 0}
    new_rows = []

    for slug in DEPARTMENT_SLUGS:
        base_url = "{}/jobs/{}".format(SITE_BASE, slug)
        page, total_pages = 1, None
        empty_streak = 0
        while True:
            if args.max_pages is not None and page > args.max_pages:
                break
            if total_pages is not None and page > total_pages:
                break
            html = get_html(session, base_url,
                            params={"page": page} if page > 1 else None)
            if html is None:
                counters["page_errors"] += 1
                break  # repeated failures on this department; move on
            cards, reported_total = parse_listing_page(html)
            if cards is None:
                counters["page_errors"] += 1
                log.error("Unrecognized page structure at %s?page=%d — stopping "
                          "this department", base_url, page)
                break
            if total_pages is None and reported_total:
                total_pages = int(reported_total)
                log.info("%s: %d pages reported", slug, total_pages)
            if not cards:
                # The server occasionally returns a valid page with zero
                # cards mid-listing; only stop on a persistent run of them.
                if (total_pages is not None and page < total_pages
                        and empty_streak < 2):
                    empty_streak += 1
                    log.warning("%s: page %d returned no cards (transient?) — "
                                "continuing", slug, page)
                    page += 1
                    continue
                log.info("%s: page %d empty — done", slug, page)
                break
            empty_streak = 0

            counters["pages"] += 1
            for card in cards:
                counters["listings"] += 1
                if _DENY_RE.search(card.get("title") or ""):
                    counters["excluded_deny_title"] += 1
                    continue
                try:
                    row = card_to_row(card)
                except Exception as exc:  # never let one card kill the run
                    log.warning("Skipping malformed card on page %d: %s", page, exc)
                    continue
                if row["posted_date"] and row["posted_date"] < cutoff:
                    counters["excluded_old"] += 1
                    continue
                key = (SOURCE, row["job_id"])
                if key in known_pairs:
                    counters["duplicates"] += 1
                    continue
                known_pairs.add(key)
                new_rows.append(row)
                counters["new"] += 1
            page += 1

    if args.enrich and new_rows:
        log.info("Enriching %d new jobs with detail pages...", len(new_rows))
        for row in new_rows:
            try:
                enrich_row(session, row)
            except Exception as exc:
                log.warning("Enrichment failed for %s: %s", row["job_url"], exc)

    columns = CSV_COLUMNS + (["company_address"] if args.enrich else [])
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if existing_df is not None:
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["source", "job_id"],
                                                keep="first")
        else:
            combined = new_df
        for col in columns:
            if col not in combined.columns:
                combined[col] = ""
        ordered = [c for c in columns if c in combined.columns] + \
                  [c for c in combined.columns if c not in columns]
        combined[ordered].to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        log.info("No new jobs; %s left unchanged", args.output)

    print("\n===== Run summary =====")
    print("Pages scanned:      {:>6,}".format(counters["pages"]))
    print("Listings seen:      {:>6,}".format(counters["listings"]))
    print("Page errors:        {:>6,}".format(counters["page_errors"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Excluded (deny-list title): {:>2,}".format(counters["excluded_deny_title"]))
    print("New jobs added:     {:>6,}".format(counters["new"]))
    print("Duplicates skipped: {:>6,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
