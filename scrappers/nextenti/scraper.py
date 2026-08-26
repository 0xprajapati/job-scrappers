#!/usr/bin/env python3
"""Scrape healthcare job listings from nextenti.ai.

Data source
-----------
nextenti.ai is a client-rendered React SPA backed by a JSON microservice API.
The public job search needs a short-lived ANONYMOUS bearer token that the site
hands out with no credentials:

    GET  https://authenticator.nextenti.ai/token
         -> {"data": {"userId": "...", "accessToken": "gAAAA...(Fernet)"}}

    POST https://job-maintenance.nextenti.ai/api/search?page=N
         headers: Authorization: Bearer <accessToken>, userId: <userId>
         body:    {}                      (an empty filter object = all jobs)
         -> {"data": [ {job}, ... ], "totalCount": 792}

Pagination is a fixed 30 jobs/page (the `size` param is ignored); results are
sorted newest-first by `postDate`, which lets the incremental mode stop as soon
as it reaches jobs older than the watermark. Each job exposes: jobId, jobTitle,
organizationName, city, country, salaryRange ("66000 - 83000"), salaryType
(Monthly/Annual/None), experience ("2 - 5 years"), jobType, profession,
postDate (YYYY-MM-DD), jobDescription, organizationLogo, verifiedOrganization.

Outputs (per the repo README + master-scraper-spec.md)
-----------------------------------------------------
* nextenti_jobs.csv          — rich cumulative store (dedup key: job_id),
                               the source of truth used for the incremental
                               watermark. Never filtered on salary.
* ../../jobs_csv/<DD-MM-YYYY>/nextenti.csv
                             — the same jobs mapped to the shared
                               HealthCareers.club import schema (job_samples.csv).

Time window (master spec): first run keeps jobs posted in the last
INITIAL_WINDOW_DAYS; later runs keep only jobs newer than the newest postDate
already stored, minus WATERMARK_GRACE_DAYS of overlap (dedup absorbs it).

Run `python scraper.py --help` for options.
"""

import argparse
import html
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

# The shared two-level taxonomy classifier — the ONLY categorization allowed
# (see ../../instructions/taxonomy-migration-spec.md).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "nextenti"
SITE_BASE = "https://nextenti.ai"
ROBOTS_URL = SITE_BASE + "/robots.txt"
TOKEN_URL = "https://authenticator.nextenti.ai/token"
SEARCH_URL = "https://job-maintenance.nextenti.ai/api/search"
# The listing endpoint truncates jobDescription to 250 chars; the detail
# endpoint returns the full text (and cleaner experienceMin/Max integers).
DETAIL_URL = "https://job-maintenance.nextenti.ai/job-details"
SEARCH_PAGE_URL = "https://nextenti.ai/search-jobs"  # the crawl target robots-wise

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries are captured, never filtered on.
# First run keeps the last INITIAL_WINDOW_DAYS; later runs keep only jobs newer
# than the newest stored postDate minus WATERMARK_GRACE_DAYS of overlap.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 30                 # server-fixed page size (informational)
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "nextenti_jobs.csv"
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
# jobs_csv/ lives at the repo root, two levels up from scrappers/nextenti/.
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Rich (source-of-truth) columns — superset, keeps everything the API gives.
# `profession` is the raw source field (a classification *signal* only);
# category/sub_category + the trace columns come from the shared classifier.
RICH_COLUMNS = [
    "source", "job_id", "title", "company", "profession", "city", "country",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "experience_raw", "experience_min_years", "experience_max_years",
    "job_type", "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in", "needs_review",
    "company_type", "verified_organization",
    "company_logo", "posted_date", "description", "job_url", "scraped_at",
]

# The club CSV schema is the shared 23-column CLUB_COLUMNS contract imported
# from _shared/classification.py (is_active/expires_at are retired).

# India-only board; map country name -> (ISO code, dial code).
COUNTRY_META = {"india": ("IN", "+91")}

log = logging.getLogger("nextenti_scraper")

# ----------------------------------------------------------------------------
# Field parsing / classification
# ----------------------------------------------------------------------------

_RANGE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def parse_range(text):
    """Parse "66000 - 83000" or "2 - 5 years" -> (min, max) floats, or (None, None)."""
    if not text:
        return (None, None)
    nums = [float(n.replace(",", "")) for n in _RANGE_RE.findall(str(text))]
    if not nums:
        return (None, None)
    lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)


def parse_salary(salary_range):
    """Return (min_int, max_int) INR from a salaryRange string, or (None, None).

    Salaries are captured, never filtered on. A 0/blank range is left empty.
    """
    lo, hi = parse_range(salary_range)
    if lo is None or (lo == 0 and hi == 0):
        return (None, None)
    return (int(round(lo)), int(round(hi)))


def salary_period(salary_type):
    """Map nextenti salaryType -> club salary_period enum, or ""."""
    st = (salary_type or "").strip().lower()
    if st in ("monthly", "month", "per month"):
        return "per_month"
    if st in ("annual", "yearly", "annually", "per annum", "year"):
        return "per_annum"
    return ""


def map_job_type(job_type):
    """Map nextenti jobType -> club job_type enum."""
    jt = (job_type or "").strip().lower()
    if "part" in jt:
        return "part_time"
    if "intern" in jt:
        return "part_time"
    if jt in ("remote", "work from home"):
        return "remote"
    if "hybrid" in jt:
        return "hybrid"
    return "full_time"  # default (the board is overwhelmingly Full Time)


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    """Strip HTML tags/entities and collapse whitespace (classifier input)."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", str(text)))).strip()


def apply_classification(row):
    """Run the shared classifier over one rich row and fill the taxonomy columns.

    The source `profession` field no longer decides the category — it is passed
    to classify_job as the `skills` signal only (and kept in the rich CSV as a
    raw source column). Returns the classify_job verdict; the caller drops the
    row when verdict["in_scope"] is False (counted as excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""), row.get("profession", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = "true" if verdict["needs_review"] else "false"
    return verdict


_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|life ?science|"
    r"diagnostic|clinical research", re.IGNORECASE)


def classify_company_type(name):
    """club company_type enum: hospital | pharma (default hospital)."""
    return "pharma" if _PHARMA_RE.search(name or "") else "hospital"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text):
    text = (text or "").lower().replace("&", " and ")
    return _SLUG_RE.sub("-", text).strip("-")


def build_job_url(job):
    """Reconstruct the public detail URL (slug--jobId), matching the site's format."""
    parts = "-".join(p for p in (
        slugify(job.get("organizationName")),
        slugify(job.get("jobTitle")),
        slugify(job.get("city")),
    ) if p)
    return "{}/jobs/{}--{}".format(SITE_BASE, parts, job.get("jobId"))


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Origin": SITE_BASE,
                            "Referer": SITE_BASE + "/"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    if not rp.can_fetch(USER_AGENT, SEARCH_PAGE_URL):
        sys.exit("robots.txt disallows {} — aborting.".format(SEARCH_PAGE_URL))
    log.info("robots.txt check passed")


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
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


def get_anonymous_token(session):
    """Mint a fresh anonymous bearer token; returns (accessToken, userId)."""
    data = _request(session, "GET", TOKEN_URL)
    if not data or "data" not in data:
        sys.exit("Could not obtain an anonymous token from nextenti — aborting.")
    tok = data["data"]
    return tok["accessToken"], tok["userId"]


def search_page(session, token, user_id, page):
    """Return (jobs, total_count) for one page, or (None, None) on failure."""
    headers = {"Authorization": "Bearer " + token, "userId": user_id,
               "Content-Type": "application/json"}
    data = _request(session, "POST", "{}?page={}".format(SEARCH_URL, page),
                    headers=headers, data="{}")
    if data is None:
        return (None, None)
    return (data.get("data") or [], data.get("totalCount"))


def get_job_detail(session, token, user_id, job_id):
    """Fetch the full job-detail record (untruncated description), or None."""
    headers = {"Authorization": "Bearer " + token, "userId": user_id}
    data = _request(session, "GET", "{}?jobId={}".format(DETAIL_URL, job_id),
                    headers=headers)
    if not data:
        return None
    return data.get("data") or None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    title = (job.get("jobTitle") or "").strip()
    company = (job.get("organizationName") or "").strip()
    profession = (job.get("profession") or "").strip()
    sal_min, sal_max = parse_salary(job.get("salaryRange"))
    exp_min, exp_max = parse_range(job.get("experience"))
    description = strip_html(job.get("jobDescription"))

    return {
        "source": SITE,
        "job_id": str(job.get("jobId") or ""),
        "title": title,
        "company": company,
        "profession": profession,
        "city": (job.get("city") or "").strip(),
        "country": (job.get("country") or "").strip(),
        "salary_raw": job.get("salaryRange") or "",
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_period": salary_period(job.get("salaryType")),
        "experience_raw": job.get("experience") or "",
        "experience_min_years": int(exp_min) if exp_min is not None else "",
        "experience_max_years": int(exp_max) if exp_max is not None else "",
        "job_type": map_job_type(job.get("jobType")),
        # category/sub_category + trace columns are filled by
        # apply_classification() once the full description is known.
        "company_type": classify_company_type(company),
        "verified_organization": bool(job.get("verifiedOrganization")),
        "company_logo": job.get("organizationLogo") or "",
        "posted_date": (job.get("postDate") or "")[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": build_job_url(job),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_detail(row, detail):
    """Overlay full-description detail-endpoint fields onto a listing row.

    The listing truncates jobDescription to 250 chars; the detail record has
    the full text plus cleaner integer experience bounds. Only non-empty
    detail values override (the listing stays the fallback).
    """
    full_desc = strip_html(detail.get("jobDescription"))
    if len(full_desc) > len(row.get("description") or ""):
        row["description"] = full_desc[:DESCRIPTION_MAX_CHARS]
    exp_min, exp_max = detail.get("experienceMin"), detail.get("experienceMax")
    if exp_min is not None:
        row["experience_min_years"] = int(exp_min)
    if exp_max is not None:
        row["experience_max_years"] = int(exp_max)
    return row


def _int_str(value):
    """Clean integer string for the club CSV, or "" — avoids "66000.0"."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, float) and pd.isna(value):
            return ""
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def rich_row_to_club_row(r):
    country = (r.get("country") or "India").strip() or "India"
    code, dial = COUNTRY_META.get(country.lower(), ("", ""))
    min_salary = _int_str(r.get("salary_min"))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": r.get("city", ""),
        "company_name": r.get("company", ""),
        "company_type": r.get("company_type", "hospital"),
        "company_logo": r.get("company_logo", ""),
        "company_about": "",
        "title": r.get("title", ""),
        "description": r.get("description", ""),
        "job_type": r.get("job_type", "full_time"),
        "category": r.get("category", ""),
        "sub_category": r.get("sub_category", ""),
        "role_family": r.get("role_family", ""),
        "application_url": r.get("job_url", ""),
        "posted_at": r.get("posted_date", ""),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        # nextenti has no structured qualification field — grounded extraction
        # from the description only (never inferred).
        "qualification": extract_qualification(r.get("description", "")),
        "min_salary": min_salary,
        "max_salary": _int_str(r.get("salary_max")),
        "salary_period": (r.get("salary_period", "") or "") if min_salary else "",
        "salary_currency": "INR" if min_salary else "",
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """First run: today - INITIAL_WINDOW_DAYS. Later: newest stored postDate
    minus WATERMARK_GRACE_DAYS of overlap."""
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


NEEDS_REVIEW_COLUMNS = ["job_id", "title", "profession", "category",
                        "sub_category"]


def append_needs_review(entries):
    """Append flagged rows to needs_review.csv, deduped on job_id."""
    new_df = pd.DataFrame(entries, columns=NEEDS_REVIEW_COLUMNS, dtype=str)
    try:
        old = pd.read_csv(NEEDS_REVIEW_CSV, dtype=str, keep_default_na=False)
        new_df = pd.concat([old, new_df], ignore_index=True)
    except FileNotFoundError:
        pass
    new_df = new_df.reindex(columns=NEEDS_REVIEW_COLUMNS).fillna("")
    new_df = new_df.drop_duplicates(subset="job_id", keep="last")
    new_df.to_csv(NEEDS_REVIEW_CSV, index=False)
    return len(new_df)


def write_club_csv(rich_df, run_date):
    club_rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(club_rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from nextenti.ai.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N pages (for test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip per-job detail fetches; keep the listing's "
                             "250-char truncated description (faster)")
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

    token, user_id = get_anonymous_token(session)
    log.info("Obtained anonymous token (userId %s...)", user_id[:8])

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    page, total = 0, None

    while True:
        if args.max_pages is not None and page >= args.max_pages:
            break
        jobs, total_count = search_page(session, token, user_id, page)
        if jobs is None:
            log.error("Page %d failed after retries — stopping", page)
            break
        if total is None and total_count is not None:
            total = total_count
            log.info("API reports %d total jobs (~%d pages)",
                     total, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if not jobs:
            break

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job on page %d: %s", page, exc)
                continue
            if row["posted_date"] and row["posted_date"] >= cutoff:
                page_all_old = False
            else:
                counters["excluded_old"] += 1
                continue
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            # Full description (+ cleaner experience) for NEW jobs only, so
            # daily incremental runs stay cheap.
            if not args.no_details:
                detail = get_job_detail(session, token, user_id, row["job_id"])
                if detail:
                    apply_detail(row, detail)
                else:
                    counters["detail_failed"] += 1
            # Classify AFTER the detail overlay so the full description is
            # available as a signal, not the listing's 250-char stub.
            verdict = apply_classification(row)
            if not verdict["in_scope"]:
                counters["excluded_out_of_scope"] += 1
                continue
            if verdict["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "profession": row["profession"],
                                   "category": row["category"],
                                   "sub_category": row["sub_category"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        # Newest-first ordering: once an entire page is older than the cutoff,
        # everything after it is older too.
        if page_all_old and jobs:
            log.info("Page %d entirely older than %s — stopping", page, cutoff)
            break
        page += 1

    # ---- write rich cumulative CSV (source of truth) ----
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

    # ---- always (re)write the club-schema CSV for this run date ----
    rich_csv_path = Path(args.output).resolve()
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)
        club_csv_path = target.resolve()
    else:
        club_csv_path = None

    if review_log:
        n_review = append_needs_review(review_log)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, n_review)

    print("\n===== Run summary =====")
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Excluded (out of scope): {:>3,}".format(counters["excluded_out_of_scope"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))
    print("Rich CSV stored at:    {}".format(rich_csv_path.as_uri()))
    if club_csv_path is not None:
        print("Club CSV stored at:    {}".format(club_csv_path.as_uri()))


if __name__ == "__main__":
    main()
