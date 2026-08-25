#!/usr/bin/env python3
"""Scrape healthcare job listings from jobs.docthub.com into a deduplicated CSV.

Data source
-----------
DoctHub's job board is a Next.js app backed by a public JSON API:

    GET https://api.docthub.com/jobcenter/jobs?pageNumber=N&pageSize=M[&categories=ID]

The response contains structured job objects (numeric salary with a
Monthly/Yearly type, exact publish timestamps, org + location, and a
`code` slug ending in the job ID, e.g. "dermatologist-J120239").
`isFacet=true` additionally returns facet counts, including the 18 job
categories which exactly partition the full job set. We crawl per category
so every job carries its raw source facet, but the keep/drop and labeling
decision belongs to the shared two-level classifier alone
(`_shared/classification.py` -> classify_job).

Outputs
-------
* docthub_roles_jobs.csv                      -- cumulative rich store
* needs_review.csv                            -- in-scope but flagged titles
* ../../jobs_csv/<DD-MM-YYYY>/docthub_roles.csv -- HealthCareers.club
  22-column export (CLUB_COLUMNS), regenerated from the full rich store
  each run.

Run `python docthub_scraper.py --help` for options.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

API_BASE = "https://api.docthub.com/jobcenter"
JOBS_ENDPOINT = API_BASE + "/jobs"
SITE_BASE = "https://jobs.docthub.com"
ROBOTS_URLS = [SITE_BASE + "/robots.txt", "https://api.docthub.com/robots.txt"]

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (personal research; contact: eleswarapu.madhav@gmail.com)"
)

# Time window (no salary filter — salaries are captured but never filtered on).
# First run keeps jobs posted in the last INITIAL_WINDOW_DAYS; later runs keep
# only jobs newer than the newest posted_date already in the CSV, minus
# WATERMARK_GRACE_DAYS of overlap (dedup absorbs the overlap).
INITIAL_WINDOW_DAYS = 2
WATERMARK_GRACE_DAYS = 2

# Crawl-side scoping ONLY (saves requests; never decides keep/drop).
# All 18 docthub facet categories are crawled by default: iter_category_jobs
# stops as soon as a page is entirely older than the watermark cutoff, so in
# steady state every extra category costs ~1 request/run. Add a facet name to
# EXCLUDE_CATEGORIES only if it proves to be pure crawl waste; the final
# keep/drop and labeling decision is classify_job's alone.
CRAWL_ONLY_INCLUDED = False
INCLUDE_CATEGORIES = {
    "Clinical Research/ Data Science",
}
EXCLUDE_CATEGORIES = set()

PAGE_SIZE = 100                # jobs per API request (100 verified working)
REQUEST_DELAY_SECONDS = 1.0    # pause between successive API requests
REQUEST_TIMEOUT_SECONDS = 60   # per-request timeout (server can be slow)
MAX_RETRIES = 4                # retries per request, exponential backoff
BACKOFF_BASE_SECONDS = 3.0     # 3s, 6s, 12s, 24s

DEFAULT_OUTPUT_CSV = "docthub_roles_jobs.csv"
NEEDS_REVIEW_CSV = "needs_review.csv"

SITE = "docthub_roles"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Rich store schema. `source_category` is docthub's raw facet name (source
# data only — it never decides anything); `category` holds the two-level
# taxonomy ("Non Clinical" | "Public Health") with `sub_category`,
# `role_family` and the score-trace columns from classify_job.
CSV_COLUMNS = [
    "job_id", "title", "company", "location",
    "experience_min_years", "experience_max_years",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "source_category",
    "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "needs_review", "posted_date", "job_url", "scraped_at",
]

log = logging.getLogger("docthub_scraper")

# ----------------------------------------------------------------------------
# Salary parsing / normalization
# ----------------------------------------------------------------------------

_SALARY_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(K|L|LAKH|LAC)?", re.IGNORECASE)
_NON_DISCLOSED_RE = re.compile(r"not\s*disclosed|negotiable|confidential", re.IGNORECASE)


def parse_salary_string(raw):
    """Parse a display salary string like "18K - 40K P.M" or "8.00L - 10.00L P.A".

    Returns (min_monthly, max_monthly, period) with integer INR/month amounts
    and period "P.M" or "P.A", or None if missing/unparseable/not disclosed.
    K = 1,000; L = 100,000. P.A amounts are divided by 12. For ranges the two
    numbers are min and max; a single number means min == max.
    """
    if not raw or not raw.strip() or _NON_DISCLOSED_RE.search(raw):
        return None

    text = raw.strip()
    if re.search(r"P\.?\s*M\.?|per\s*month", text, re.IGNORECASE):
        period, divisor = "P.M", 1
    elif re.search(r"P\.?\s*A\.?|per\s*annum|per\s*year", text, re.IGNORECASE):
        period, divisor = "P.A", 12
    else:
        return None  # no recognizable period -> unparseable

    amounts = []
    for num, unit in _SALARY_AMOUNT_RE.findall(text):
        value = float(num)
        unit = (unit or "").upper()
        if unit == "K":
            value *= 1_000
        elif unit in ("L", "LAKH", "LAC"):
            value *= 100_000
        amounts.append(value)
    if not amounts:
        return None

    lo, hi = min(amounts[:2]), max(amounts[:2])
    return (int(round(lo / divisor)), int(round(hi / divisor)), period)


def normalize_salary_object(salary):
    """Normalize the API's structured salary object to monthly INR.

    The API returns e.g. {"currency": "INR", "type": "Monthly"|"Yearly",
    "minAmount": 30000, "maxAmount": 50000}. Undisclosed salaries come back
    with type "0" and zero amounts. Returns (min_monthly, max_monthly,
    period, raw_string) or None if the salary is not disclosed/unusable.
    """
    if not isinstance(salary, dict):
        return None
    stype = str(salary.get("type") or "").strip().lower()
    min_amt = salary.get("minAmount") or 0
    max_amt = salary.get("maxAmount") or 0
    if stype == "monthly":
        divisor, period = 1, "P.M"
    elif stype == "yearly":
        divisor, period = 12, "P.A"
    else:
        return None  # type "0" / unknown -> not disclosed
    if min_amt <= 0 and max_amt <= 0:
        return None
    if max_amt <= 0:
        max_amt = min_amt
    if min_amt > max_amt:
        min_amt, max_amt = max_amt, min_amt
    currency = salary.get("currency") or "INR"
    raw = "{} {:,.0f} - {:,.0f} {}".format(currency, min_amt, max_amt, period)
    return (int(round(min_amt / divisor)), int(round(max_amt / divisor)), period, raw)


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


# ----------------------------------------------------------------------------
# HTTP session
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": SITE_BASE,
        "Referer": SITE_BASE + "/all-jobs",
    })
    return session


def check_robots(session):
    """Abort if robots.txt disallows us. A missing robots.txt means allowed."""
    for robots_url in ROBOTS_URLS:
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
            continue
        if resp.status_code >= 400:
            continue  # no robots.txt -> allowed
        rp.parse(resp.text.splitlines())
        probe = JOBS_ENDPOINT if "api." in robots_url else SITE_BASE + "/all-jobs"
        if not rp.can_fetch(USER_AGENT, probe):
            sys.exit("robots.txt at {} disallows fetching {} — aborting.".format(robots_url, probe))
    log.info("robots.txt check passed")


def get_json(session, url, params=None):
    """GET a JSON document with rate limiting and exponential-backoff retries."""
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
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Fetching & pagination
# ----------------------------------------------------------------------------

def fetch_categories(session):
    """Return the live category facet list: [{"id", "value", "count"}, ...]."""
    data = get_json(session, JOBS_ENDPOINT + "/facets")
    if not data or not (data.get("facets") or {}).get("categories"):
        sys.exit("Could not fetch category facets from the API — aborting.")
    return data["facets"]["categories"]


def iter_category_jobs(session, category_id, page_size, cutoff, max_pages=None):
    """Yield job dicts for one category, paginating until exhausted.

    Listings come newest-first, so pagination stops early once an entire
    page is older than the cutoff date — daily runs only fetch the head.
    """
    page = 1
    seen = 0
    total = None
    while True:
        if max_pages is not None and page > max_pages:
            return
        data = get_json(session, JOBS_ENDPOINT, {
            "pageNumber": page, "pageSize": page_size,
            "isFacet": "false", "categories": category_id,
        })
        if data is None:
            log.error("Skipping rest of category %s (page %d failed)", category_id, page)
            return
        jobs = data.get("jobs") or []
        if total is None:
            total = data.get("totalRecords")
        if not jobs:
            return
        for job in jobs:
            yield job
        newest = max((job.get("publishedDate") or "")[:10] for job in jobs)
        if newest and newest < cutoff:
            log.debug("Category %s: page %d entirely older than %s — stopping",
                      category_id, page, cutoff)
            return
        seen += len(jobs)
        if total is not None and seen >= total:
            return
        page += 1


# ----------------------------------------------------------------------------
# Parsing a job record into a CSV row
# ----------------------------------------------------------------------------

_JOB_ID_RE = re.compile(r"-(J\d+)$")
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    """Flatten an HTML fragment to plain text (classifier + club export)."""
    text = _TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", text.replace("&nbsp;", " ")).strip()


def job_to_row(job, category_name, verdict):
    """Build one rich-CSV row from an API job object + its classify_job verdict."""
    code = job.get("code") or ""
    id_match = _JOB_ID_RE.search(code)
    job_id = id_match.group(1) if id_match else "J{}".format(job.get("id", ""))

    normalized = normalize_salary_object(job.get("salary"))
    if normalized:
        sal_min, sal_max, period, raw = normalized
    else:
        sal_min = sal_max = None
        period, raw = "", "Not Disclosed"

    work_exp = job.get("workExperience") or {}
    organization = job.get("organization") or {}
    location = (job.get("location") or {}).get("location") or ""
    if not location:
        address = organization.get("address") or {}
        location = ", ".join(x for x in (address.get("city"), address.get("state")) if x)

    published = job.get("publishedDate") or job.get("createdDate") or ""

    return {
        "job_id": job_id,
        "title": (job.get("title") or "").strip(),
        "company": (organization.get("name") or "").strip(),
        "location": location,
        "experience_min_years": work_exp.get("fromYear"),
        "experience_max_years": work_exp.get("toYear"),
        "salary_raw": raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": period,
        "job_type": job.get("employementType") or "",
        "source_category": category_name,
        "category": verdict["category"],
        "sub_category": verdict["sub_category"],
        "role_family": verdict["role_family"],
        "all_families": verdict["all_families"],
        "family_scores": verdict["family_scores"],
        "family_confidence": verdict["family_confidence"],
        "matched_in": verdict["matched_in"],
        "needs_review": bool(verdict["needs_review"]),
        "posted_date": published[:10],
        "job_url": "{}/{}".format(SITE_BASE, code) if code else "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----------------------------------------------------------------------------
# Optional detail-page enrichment
# ----------------------------------------------------------------------------

def enrich_row(session, row):
    """Fetch the job-detail API for description and apply link. Best-effort."""
    code = row["job_url"].rsplit("/", 1)[-1]
    detail = get_json(session, "{}/{}".format(JOBS_ENDPOINT, code))
    if not detail:
        return row
    row["description"] = strip_html(detail.get("description") or "")
    row["apply_url"] = detail.get("referenceLink") or row["job_url"]
    return row


# ----------------------------------------------------------------------------
# HealthCareers.club export (CLUB_COLUMNS from _shared/classification.py)
# ----------------------------------------------------------------------------

def _blank(value):
    """NaN/None-safe string."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _int_str(value):
    value = _blank(value)
    if value == "":
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def rich_row_to_club_row(r):
    """Map one rich-store row to the 22-column club schema.

    Salaries in the rich store are already normalized to INR/month, so the
    club export always uses per_month. Docthub has no structured
    qualification field; qualification comes from grounded extraction over
    the (--enrich) description — never inferred.
    """
    job_type = _blank(r.get("job_type")).lower()
    if "part" in job_type:
        club_type = "part_time"
    elif "intern" in job_type:
        club_type = "internship"
    else:
        club_type = "full_time"

    min_sal = _int_str(r.get("salary_min_monthly"))
    max_sal = _int_str(r.get("salary_max_monthly"))
    has_salary = bool(min_sal)

    city = _blank(r.get("location")).split(",")[0].strip()
    description = _blank(r.get("description"))

    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        "city_name": city,
        "company_name": _blank(r.get("company")),
        "company_type": "",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": description,
        "job_type": club_type,
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "application_url": _blank(r.get("apply_url")) or _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        "qualification": extract_qualification(description),
        "min_salary": min_sal if has_salary else "",
        "max_salary": (max_sal or min_sal) if has_salary else "",
        "salary_period": "per_month" if has_salary else "",
        "salary_currency": "INR" if has_salary else "",
    }


def write_club_csv(rich_df, run_date):
    """Regenerate ../../jobs_csv/<run_date>/docthub_roles.csv from the store."""
    rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_known_ids(csv_path):
    try:
        existing = pd.read_csv(csv_path, dtype=str)
    except FileNotFoundError:
        return set(), None
    return set(existing["job_id"].dropna()), existing


def append_needs_review(rows, path):
    if not rows:
        return
    review = pd.DataFrame(rows)
    try:
        known = set(pd.read_csv(path, dtype=str)["job_id"].dropna())
        review = review[~review["job_id"].isin(known)]
        review.to_csv(path, mode="a", header=False, index=False)
    except FileNotFoundError:
        review.to_csv(path, index=False)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from jobs.docthub.com into a CSV.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="crawl at most N pages per category (for test runs)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE,
                        help="jobs per API request (default: %(default)s)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each NEW passing job's detail page for "
                             "description and apply_url (extra requests)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    categories = fetch_categories(session)

    known_ids, existing_df = load_known_ids(args.output)
    log.info("Existing CSV has %d known job ids", len(known_ids))
    cutoff = compute_cutoff(existing_df)
    log.info("Keeping jobs posted on/after %s", cutoff)

    counters = {
        "scanned": 0, "excluded_category": 0, "excluded_out_of_scope": 0,
        "excluded_old": 0, "needs_review": 0, "new": 0, "duplicates": 0,
    }
    new_rows, review_log = [], []

    for cat in categories:
        name, cat_id, count = cat["value"], cat["id"], cat["count"]
        if name in EXCLUDE_CATEGORIES:
            counters["scanned"] += count
            counters["excluded_category"] += count
            log.info("Skipping excluded category %r (%d jobs)", name, count)
            continue
        if CRAWL_ONLY_INCLUDED and name not in INCLUDE_CATEGORIES:
            log.info("Skipping out-of-scope category %r (%d jobs)", name, count)
            continue

        log.info("Crawling category %r (%d jobs)...", name, count)
        for job in iter_category_jobs(session, cat_id, args.page_size, cutoff,
                                      args.max_pages):
            counters["scanned"] += 1
            title = job.get("title") or ""
            verdict = classify_job(
                title,
                skills=job.get("skills") or job.get("keySkills") or "",
                description=strip_html(
                    job.get("description") or job.get("jobDescription") or ""))
            if not verdict["in_scope"]:
                counters["excluded_out_of_scope"] += 1
                continue

            row = job_to_row(job, name, verdict)
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            if verdict["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({
                    "job_id": row["job_id"], "title": row["title"],
                    "source_category": name, "salary_raw": row["salary_raw"],
                    "posted_date": row["posted_date"],
                })

            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue

            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

    if args.enrich and new_rows:
        log.info("Enriching %d new jobs with detail pages...", len(new_rows))
        for row in new_rows:
            enrich_row(session, row)

    columns = CSV_COLUMNS + (["description", "apply_url"] if args.enrich else [])
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if existing_df is not None:
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset="job_id", keep="first")
        else:
            combined = new_df
    else:
        combined = existing_df
    if combined is not None:
        for col in columns:  # keep column order stable across runs
            if col not in combined.columns:
                combined[col] = ""
        ordered = [c for c in columns if c in combined.columns] + \
                  [c for c in combined.columns if c not in columns]
        combined = combined[ordered]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        log.info("No jobs stored yet; %s not written", args.output)

    append_needs_review(review_log, NEEDS_REVIEW_CSV)

    # ---- always regenerate the club-schema CSV from the full rich store ----
    if combined is not None and len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    print("\n===== Run summary =====")
    print("Total jobs scanned:            {:>7,}".format(counters["scanned"]))
    print("Excluded (crawl-skipped cat):  {:>7,}".format(counters["excluded_category"]))
    print("Excluded (out of scope):       {:>7,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:          {:>7,}".format(counters["needs_review"]))
    print("New jobs added:                {:>7,}".format(counters["new"]))
    print("Duplicates skipped:            {:>7,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
