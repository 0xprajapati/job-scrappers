#!/usr/bin/env python3
"""Scrape job listings from publichealthcareer.org.

Data source
-----------
publichealthcareer.org is a WordPress job board (pxp theme) for public-health
roles in India. It exposes a custom `job` post type through the standard REST
API — the primary, robust source:

    GET https://publichealthcareer.org/wp-json/wp/v2/job
        ?per_page=100&page=N
        &_fields=id,date,link,title,content,job_location,job_category,
                 job_type,job_level

Posts come newest-first. The taxonomy fields hold term IDs, resolved in one
batched request per taxonomy (job_location -> "Delhi", job_type ->
"Full Time", job_level -> "Entry-Level", job_category -> subject area).

Company and salary are NOT in REST; each job's detail page has a labeled
sidebar (Salary "INR 20,000 per month", Experience, Employment Type, Website)
and a hiringOrganization name. (The page's JobPosting JSON-LD is malformed —
missing commas — so fields are extracted from the sidebar markup and targeted
regexes instead.) Detail fetches happen for NEW jobs only and are ON by
default; the whole board is ~15 jobs.

Salaries are INR/USD, both valid club currencies, so club salary fields are
populated when the sidebar states an amount (never invented, never filtered).

Classification is the shared two-level taxonomy (scrappers/_shared/
classification.py): category "Non Clinical" | "Public Health" plus a
sub_category. The whole board is public-health domain, so most rows are in
scope — but the board also carries bedside and administrative posts, which are
dropped and counted as excluded_out_of_scope. The site's own job_category term
is the curated `skills` signal and stays in the rich CSV as a raw source
column (job_category_raw); it never decides the category itself.

Outputs
-------
* publichealthcareer_jobs.csv           — rich cumulative store (dedup: job id)
* ../../jobs_csv/<DD-MM-YYYY>/publichealthcareer.csv
                                        — HealthCareers.club 22-col schema
* needs_review.csv                      — kept but flagged
* out-of-scope.csv                      — rows the taxonomy dropped (reversible)

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored date minus
WATERMARK_GRACE_DAYS of overlap.

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

SITE = "publichealthcareer"
SITE_BASE = "https://publichealthcareer.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
API_BASE = SITE_BASE + "/wp-json/wp/v2"
JOBS_URL = API_BASE + "/job"
TAXONOMIES = ("job_location", "job_category", "job_type", "job_level")

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

RICH_CSV = "publichealthcareer_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# India-focused board; the club schema wants a country per row.
COUNTRY_NAME = "India"
COUNTRY_CODE = "IN"
COUNTRY_DIAL = "+91"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_website", "city",
    "country", "salary_raw", "salary_min", "salary_max", "salary_period",
    "salary_currency", "experience_raw", "job_type", "job_level",
    "job_category_raw", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "company_type", "needs_review",
    "posted_date", "description", "job_url", "scraped_at",
]

OUT_OF_SCOPE_CSV = "out-of-scope.csv"

log = logging.getLogger("publichealthcareer_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


_SALARY_RE = re.compile(
    r"\b(INR|USD|RS\.?|₹|\$)\s*([\d,]+(?:\.\d+)?)\s*(?:-|to)?\s*([\d,]+(?:\.\d+)?)?"
    r"\s*(?:per\s+(month|annum|year)|/(month|year)|(monthly|annually|yearly))?",
    re.IGNORECASE)


def parse_salary_text(text):
    """Parse "INR 20,000 per month" / "USD 800 - 1,200 per month" etc.

    Returns dict with salary_min/max (int), salary_period (club enum),
    salary_currency ("INR"/"USD") — or {} when absent/unparseable/N-A.
    Amounts are captured, never filtered.
    """
    text = clean_text(text)
    if not text or text.lower() in ("n/a", "na", "-", "not disclosed", "negotiable"):
        return {}
    m = _SALARY_RE.search(text)
    if not m:
        return {}
    cur_raw = m.group(1).upper().rstrip(".")
    currency = {"RS": "INR", "₹": "INR", "$": "USD"}.get(cur_raw, cur_raw)
    lo = float(m.group(2).replace(",", ""))
    hi = float(m.group(3).replace(",", "")) if m.group(3) else lo
    if hi < lo:
        lo, hi = hi, lo
    if lo <= 0:
        return {}
    period_word = (m.group(4) or m.group(5) or m.group(6) or "").lower()
    if period_word in ("annum", "year", "annually", "yearly"):
        period = "per_annum"
    else:
        period = "per_month"  # site defaults to monthly wording
    return {"salary_raw": clean_text(m.group(0))[:120], "salary_min": int(round(lo)),
            "salary_max": int(round(hi)), "salary_period": period,
            "salary_currency": currency}


def map_job_type(text):
    jt = (text or "").strip().lower()
    if "part" in jt or "intern" in jt or "fellow" in jt:
        return "part_time" if "part" in jt else "full_time"
    if "remote" in jt or "work from home" in jt:
        return "remote"
    if "hybrid" in jt:
        return "hybrid"
    return "full_time"


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The site's own job_category term (job_category_raw) is the curated
    `skills` signal; the raw value stays in the rich CSV as a source column
    only. Returns in_scope — False means DROP the row
    (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("job_category_raw", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = verdict["needs_review"]
    return verdict["in_scope"]


_PHARMA_ORG_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|biotech|diagnostic|life ?science",
    re.IGNORECASE)


def classify_company_type(company):
    """club enum hospital|pharma. NGOs/institutes have no fitting value;
    'hospital' is the schema's general-employer default."""
    return "pharma" if _PHARMA_ORG_RE.search(company or "") else "hospital"


# ----------------------------------------------------------------------------
# Detail-page extraction (sidebar labels; the JSON-LD is malformed)
# ----------------------------------------------------------------------------

# Label div, then everything (text node or wrapper div) up to the next label
# div / end of the sidebar. Values appear both as bare text nodes and inside
# wrapper divs depending on the field, so the segment is captured whole and
# tag-stripped.
_SIDEBAR_RE = re.compile(
    r'pxp-single-job-side-info-label[^>]*>\s*([^<]+?)\s*</div>(.{0,2000}?)'
    r'(?=<div[^>]*pxp-single-job-side-info-label|</aside|$)', re.S)
_ORG_NAME_RE = re.compile(
    r'"hiringOrganization"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', re.S)
_COMPANY_LINK_RE = re.compile(
    r'href="https://publichealthcareer\.org/companies/[a-z0-9-]+/?"[^>]*>\s*([^<]+)')


def parse_detail_page(page_html):
    """Extract company, salary, experience, employment type, website."""
    out = {}
    labels = {}
    for m in _SIDEBAR_RE.finditer(page_html or ""):
        labels[clean_text(m.group(1)).lower()] = strip_html(m.group(2))
    if labels.get("salary"):
        out.update(parse_salary_text(labels["salary"]))
        out.setdefault("salary_raw", labels["salary"][:120])
    exp = labels.get("experience", "")
    if exp and exp.lower() not in ("n/a", "na", "-"):
        out["experience_raw"] = exp[:80]
    if labels.get("employment type"):
        out["job_type"] = map_job_type(labels["employment type"])
    if labels.get("website"):
        out["company_website"] = labels["website"].split()[0][:200]
    if labels.get("location"):
        out["city"] = labels["location"].split()[0].strip(",")[:60]

    org = _ORG_NAME_RE.search(page_html or "")
    if org:
        out["company"] = clean_text(org.group(1))
    else:
        link = _COMPANY_LINK_RE.search(page_html or "")
        if link:
            out["company"] = clean_text(link.group(1))
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
    if not rp.can_fetch(USER_AGENT, JOBS_URL):
        sys.exit("robots.txt disallows the jobs API — aborting.")
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
            if 400 <= resp.status_code < 500:  # e.g. WP 400 past last page
                return None
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_jobs_page(session, page):
    return _request(session, JOBS_URL, {
        "per_page": PER_PAGE, "page": page, "orderby": "date", "order": "desc",
        "_fields": "id,date,link,title,content," + ",".join(TAXONOMIES),
    })


def resolve_terms(session, jobs):
    """Batch-resolve taxonomy term ids -> names: {taxonomy: {id: name}}."""
    lookup = {}
    for tax in TAXONOMIES:
        ids = sorted({tid for job in jobs for tid in (job.get(tax) or [])})
        lookup[tax] = {}
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            terms = _request(session, "{}/{}".format(API_BASE, tax), {
                "include": ",".join(map(str, batch)),
                "per_page": 100, "_fields": "id,name"})
            for term in terms or []:
                lookup[tax][term["id"]] = clean_text(term["name"])
    return lookup


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job, terms):
    def term_name(tax):
        ids = job.get(tax) or []
        return terms.get(tax, {}).get(ids[0], "") if ids else ""

    title = clean_text((job.get("title") or {}).get("rendered", ""))
    category_term = term_name("job_category")
    description = strip_html((job.get("content") or {}).get("rendered", ""))

    return {
        "source": SITE,
        "job_id": str(job.get("id") or ""),
        "title": title,
        "company": "",             # filled from the detail page
        "company_website": "",
        "city": term_name("job_location"),
        "country": COUNTRY_NAME,
        "salary_raw": "",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "experience_raw": "",
        "job_type": map_job_type(term_name("job_type")),
        "job_level": term_name("job_level"),
        "job_category_raw": category_term,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": "hospital",  # refined after company is known
        "needs_review": False,
        "posted_date": (job.get("date") or "")[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": job.get("link") or "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else value


def rich_row_to_club_row(r):
    currency = _blank(r.get("salary_currency"))
    valid_currency = currency in ("INR", "USD")
    min_sal = _blank(r.get("salary_min"))
    def int_str(v):
        v = _blank(v)
        if v == "":
            return ""
        try:
            return str(int(float(v)))
        except (TypeError, ValueError):
            return ""
    return {
        "country_name": _blank(r.get("country")) or COUNTRY_NAME,
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": _blank(r.get("city")),
        "company_name": _blank(r.get("company")) or _blank(r.get("title")),
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        # the board has no structured qualification field — grounded
        # extraction from the description only, never inferred
        "qualification": extract_qualification(_blank(r.get("description"))),
        "min_salary": int_str(min_sal) if valid_currency else "",
        "max_salary": int_str(r.get("salary_max")) if valid_currency else "",
        "salary_period": _blank(r.get("salary_period")) if valid_currency and int_str(min_sal) else "",
        "salary_currency": currency if valid_currency and int_str(min_sal) else "",
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
        description="Scrape jobs from publichealthcareer.org.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N REST pages (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip per-job detail fetches (no company/salary/"
                             "experience; faster)")
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

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "duplicates": 0,
                "detail_failed": 0}
    new_rows, review_log = [], []
    page = 1
    pending = []  # (job, ) new in-window jobs before term resolution

    all_jobs = []
    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        jobs = fetch_jobs_page(session, page)
        if not jobs:
            break
        all_jobs.extend(jobs)
        newest_seen_old = all((j.get("date") or "")[:10] < cutoff for j in jobs)
        if newest_seen_old:
            break
        if len(jobs) < PER_PAGE:
            break
        page += 1

    terms = resolve_terms(session, all_jobs)

    for job in all_jobs:
        counters["scanned"] += 1
        try:
            row = job_to_rich_row(job, terms)
        except Exception as exc:
            log.warning("Skipping malformed job %s: %s", job.get("id"), exc)
            continue
        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            continue
        # Scope gate before the detail fetch: an out-of-scope job costs no
        # request. The REST content already carries the description, so the
        # classifier has every signal it will ever get for this row.
        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            continue
        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "job_category": row["job_category_raw"]})
        if row["job_id"] in known_ids:
            counters["duplicates"] += 1
            continue
        if not args.no_details:
            page_html = _request(session, row["job_url"], as_json=False)
            if page_html:
                detail = parse_detail_page(page_html)
                row.update(detail)
                if row.get("company"):
                    row["company_type"] = classify_company_type(row["company"])
            else:
                counters["detail_failed"] += 1
        known_ids.add(row["job_id"])
        new_rows.append(row)
        counters["new"] += 1

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
    print("Excluded (out of scope): {:>3,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
