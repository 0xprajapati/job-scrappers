#!/usr/bin/env python3
"""Scrape healthcare job listings from dubailivejobs.com.

Data source
-----------
dubailivejobs.com is a WordPress site (WP Job Manager content-farm style). The
custom `job_listing` type is not exposed via REST, but the standard **posts**
endpoint is, and posts are tagged with a clean **"Healthcare" category (id 86,
~92 posts)** of hospitals / clinics / pharmacies:

    GET https://www.dubailivejobs.com/wp-json/wp/v2/posts
        ?categories=86&per_page=100&page=N
        &_fields=id,date,link,title,content,excerpt

Each post is a *company careers page* (e.g. "Danat Al Emarat Hospital Careers")
covering many roles, not a single job — so `company` is taken from the title
and the club `category` is a best-effort guess (posts that don't name a
specific role are flagged `needs_review`). Posts come newest-first by date,
which lets incremental runs stop at the watermark.

Per-post salary / employment type / precise location live only in a
schema.org **JobPosting JSON-LD** in each post's HTML `<head>` (not in REST),
so those are fetched per NEW post when `--details` is on (default). Salaries
are in **AED**; the shared HealthCareers.club schema only allows INR/USD
currencies, so AED amounts are captured in the rich CSV but the club CSV's
salary fields are left blank (no invented conversion).

Outputs
-------
* dubailivejobs_jobs.csv               — rich cumulative store (dedup: post_id).
* ../../jobs_csv/<DD-MM-YYYY>/dubailivejobs.csv
                                       — shared job_samples.csv schema.

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS; later
runs keep only posts newer than the newest stored date minus
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

SITE = "dubailivejobs"
SITE_BASE = "https://www.dubailivejobs.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
POSTS_URL = SITE_BASE + "/wp-json/wp/v2/posts"
HEALTHCARE_CATEGORY_ID = 86  # WP "Healthcare" category

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PER_PAGE = 100
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "dubailivejobs_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# UAE site — country is fixed. (salary_currency AED is NOT in the club enum
# (INR/USD only), so club salary is left blank; raw AED kept in the rich CSV.)
COUNTRY_NAME = "United Arab Emirates"
COUNTRY_CODE = "AE"
COUNTRY_DIAL = "+971"
DEFAULT_CITY = "Dubai"

RICH_COLUMNS = [
    "source", "post_id", "title", "company", "city", "country",
    "salary_raw", "salary_currency_original", "salary_period",
    "job_type", "category", "company_type", "needs_review",
    "posted_date", "expires_at", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("dubailivejobs_scraper")

# ----------------------------------------------------------------------------
# Title / company / category parsing
# ----------------------------------------------------------------------------

# Marketing filler stripped from titles.
_FILLER_RE = re.compile(
    r"\b(free|urgent(ly)?|hiring|apply now|apply here|walk[ -]?in interview|"
    r"100%|staff required|staff recruitment|attractive salary|now|2024|2025|"
    r"2026|latest|new|jobs details|urgent hiring|recruitment)\b",
    re.IGNORECASE)
_SEP_RE = re.compile(r"\s*(?:\|\||[–—-]{1,2}|:)\s*")
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def parse_title(raw_title):
    """Return (display_title, company). The post title names a company careers
    page, e.g. "Danat Al Emarat Hospital Careers – Staff Required Urgently"."""
    title = clean_text(raw_title)
    # the first segment (before || or – ) is the meaningful part
    head = _SEP_RE.split(title)[0].strip() or title
    # company = head minus trailing "Careers/Jobs [in UAE/Dubai]"
    company = re.sub(
        r"\s*(careers?|jobs?|vacancies|recruitment)\b.*$", "",
        head, flags=re.IGNORECASE).strip()
    company = re.sub(r"\s+in\s+(uae|dubai|abu dhabi|sharjah).*$", "",
                     company, flags=re.IGNORECASE).strip()
    display = _FILLER_RE.sub("", head)
    display = _WS_RE.sub(" ", display).strip(" -|:")
    return (display or head, company or display or head)


_NURSE_RE = re.compile(r"\b(nurse|nursing|midwif)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\b(pharmac(y|ist|ies))\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|dentist|dental|consultant|"
    r"[a-z]+ologist|medical officer|gp)\b", re.IGNORECASE)
_CLINICAL_EMPLOYER_RE = re.compile(
    r"\b(hospital|clinic|medical|healthcare|health care|health point|"
    r"polyclinic|diagnostic|laborator)\b", re.IGNORECASE)


def classify_category(title):
    """club category enum: doctors|nurses|pharmacists|non_clinical.

    Returns (category, needs_review). These are company-careers posts, so a
    role is only assigned when the title names one; a generic clinical employer
    (a whole hospital) maps to non_clinical and is flagged for review.
    """
    if _PHARM_RE.search(title):
        return ("pharmacists", False)
    if _NURSE_RE.search(title):
        return ("nurses", False)
    if _DOCTOR_RE.search(title):
        return ("doctors", False)
    # a clinical employer with no specific role in the title -> mixed roles
    return ("non_clinical", bool(_CLINICAL_EMPLOYER_RE.search(title)) or True)


_PHARMA_RE = re.compile(r"pharmac|laborator|diagnostic|\blabs?\b", re.IGNORECASE)


def classify_company_type(company, title):
    """club company_type enum: hospital | pharma (default hospital)."""
    return "pharma" if _PHARMA_RE.search(company + " " + title) else "hospital"


# ----------------------------------------------------------------------------
# JobPosting JSON-LD (per-post detail)
# ----------------------------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def extract_job_posting(page_html):
    """Return the JobPosting node from a post's JSON-LD, or {}."""
    for m in _LDJSON_RE.finditer(page_html or ""):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in (nodes if isinstance(nodes, list) else [nodes]):
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                return node
    return {}


def detail_from_posting(posting):
    """Pull the useful, schema-representable fields from a JobPosting node."""
    out = {}
    base = posting.get("baseSalary") or {}
    val = (base.get("value") or {}) if isinstance(base, dict) else {}
    if val.get("value"):
        out["salary_raw"] = str(val.get("value"))
        out["salary_currency_original"] = base.get("currency") or ""
        unit = str(val.get("unitText") or "").upper()
        out["salary_period"] = {"MONTH": "per_month", "YEAR": "per_annum"}.get(unit, "")
    emp = posting.get("employmentType")
    if emp:
        emp = emp if isinstance(emp, list) else [emp]
        low = [e.lower() for e in emp]
        if any("part" in e for e in low) and not any("full" in e for e in low):
            out["job_type"] = "part_time"
        elif any("full" in e for e in low):
            out["job_type"] = "full_time"
    loc = (posting.get("jobLocation") or {})
    addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
    if addr.get("addressRegion"):
        out["city"] = addr["addressRegion"]
    if posting.get("validThrough"):
        out["expires_at"] = str(posting["validThrough"])[:10]
    return out


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
    if not rp.can_fetch(USER_AGENT, POSTS_URL):
        sys.exit("robots.txt disallows the posts API — aborting.")
    log.info("robots.txt check passed")


def _request(session, url, params=None, as_json=True):
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
            return resp.json() if as_json else resp.text
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_posts_page(session, page):
    return _request(session, POSTS_URL, {
        "categories": HEALTHCARE_CATEGORY_ID, "per_page": PER_PAGE, "page": page,
        "orderby": "date", "order": "desc",
        "_fields": "id,date,link,title,content,excerpt",
    })


def fetch_post_detail(session, link):
    page_html = _request(session, link, as_json=False)
    return detail_from_posting(extract_job_posting(page_html)) if page_html else {}


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def post_to_rich_row(post):
    display_title, company = parse_title((post.get("title") or {}).get("rendered", ""))
    category, needs_review = classify_category(display_title)
    body = (post.get("content") or {}).get("rendered", "")
    description = clean_text(_TAG_RE.sub(" ", body))

    return {
        "source": SITE,
        "post_id": str(post.get("id") or ""),
        "title": display_title,
        "company": company,
        "city": DEFAULT_CITY,
        "country": COUNTRY_NAME,
        "salary_raw": "",
        "salary_currency_original": "",
        "salary_period": "",
        "job_type": "full_time",
        "category": category,
        "company_type": classify_company_type(company, display_title),
        "needs_review": needs_review,
        "posted_date": (post.get("date") or "")[:10],
        "expires_at": "",
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": post.get("link") or "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def rich_row_to_club_row(r):
    return {
        "country_name": r.get("country", COUNTRY_NAME),
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": r.get("city", DEFAULT_CITY),
        "company_name": r.get("company", ""),
        "company_type": r.get("company_type", "hospital"),
        "company_logo": "",
        "company_about": "",
        "title": r.get("title", ""),
        "description": r.get("description", ""),
        "job_type": r.get("job_type", "full_time"),
        "category": r.get("category", "non_clinical"),
        "application_url": r.get("job_url", ""),
        "posted_at": r.get("posted_date", ""),
        "min_experience": "",
        "max_experience": "",
        # Salary is AED (not in the club INR/USD enum) -> left blank on purpose;
        # the raw AED amount is preserved in the rich CSV.
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        "expires_at": r.get("expires_at", ""),
    }


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
        description="Scrape healthcare jobs from dubailivejobs.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N REST pages (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip per-post JSON-LD fetches (no salary_raw / "
                             "employment type / expiry; faster)")
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
    known_ids = set(existing_df["post_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known posts; keeping posts on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    page = 1

    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        posts = fetch_posts_page(session, page)
        if not posts:  # None (error) or [] (past last page)
            break

        page_all_old = True
        for post in posts:
            counters["scanned"] += 1
            try:
                row = post_to_rich_row(post)
            except Exception as exc:
                log.warning("Skipping malformed post %s: %s", post.get("id"), exc)
                continue
            if row["posted_date"] and row["posted_date"] >= cutoff:
                page_all_old = False
            else:
                counters["excluded_old"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"post_id": row["post_id"], "title": row["title"],
                                   "company": row["company"]})
            if row["post_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if not args.no_details:
                detail = fetch_post_detail(session, row["job_url"])
                if detail:
                    row.update(detail)
                else:
                    counters["detail_failed"] += 1
            known_ids.add(row["post_id"])
            new_rows.append(row)
            counters["new"] += 1

        if page_all_old and posts:
            log.info("Page %d entirely older than %s — stopping", page, cutoff)
            break
        page += 1

    # ---- rich cumulative CSV ----
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="post_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[[c for c in RICH_COLUMNS if c in combined.columns]]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new posts; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Posts scanned:         {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New posts added:       {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
