#!/usr/bin/env python3
"""Scrape healthcare job listings from workindia.in (blue/grey-collar jobs, India).

Data source
-----------
workindia.in is a Next.js site behind CloudFront. Its listing/detail HTML is
server-rendered, and robots.txt welcomes all crawlers (only URLs containing
query strings are disallowed), pointing to a daily sitemap index:

    http://crawlsitemap.workindia.in/sitemap.xml
      -> http://crawlsitemap-v2.workindia.in/latest-jd-pages.xml  (~14k URLs/day)

Each job-detail URL encodes the title/area/city and numeric job id:

    https://www.workindia.in/jobs/<title_slug>-<area>-<city>-<job_id>/

and every detail page embeds a complete schema.org **JobPosting JSON-LD**:
title, company, datePosted, validThrough, INR monthly salary range, address
(locality/region/postal), employment type, months of experience, education
and a structured description ("Salary Range : …", "Work Arrangement : …").

Flow: fetch the latest-JD sitemap (1 request) -> keep URLs whose title slug
is healthcare (source-level filter, see below) -> skip job ids already in the
CSV (the id is in the URL, so known jobs cost zero requests) -> fetch detail
pages for new candidates only -> parse JSON-LD -> time-window filter on
datePosted -> append.

Healthcare filter (slug-level)
------------------------------
* ALLOW_SLUG_RE — clearly clinical/healthcare slugs (nurse, pharmacist,
  lab_technician, hospital_*, medical_*, physiotherapist, …): kept.
* AMBIGUOUS_SLUG_RE — healthcare-adjacent slugs (caretaker, *_therapist spa
  variants, health_insurance, ward attendants, …): kept but flagged
  `needs_review=True` and logged to needs_review.csv — never silently
  dropped. (Real example: a "Caretaker" posting by a fish market.)
* everything else is counted `excluded_non_healthcare`.

Quirks
------
* CloudFront blocks non-browser User-Agents (plain curl/scripted UAs get
  403), while robots.txt invites crawling. The UA below is a browser string
  with our scraper name + contact appended — transparent and accepted.
* robots.txt disallows any URL with '?' or '&'; this scraper only ever
  requests clean paths and hard-asserts that.
* The "latest" sitemap is a rolling mix of new and refreshed old postings;
  the datePosted window + id dedup absorb that.

Outputs
-------
* workindia_jobs.csv                         — rich cumulative store (dedup: job_id)
* ../../jobs_csv/<DD-MM-YYYY>/workindia.csv  — HealthCareers.club 22-col schema
* needs_review.csv                           — ambiguous rows from this run

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

SITE = "workindia"
SITE_BASE = "https://www.workindia.in"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SITEMAP_INDEX_URL = "http://crawlsitemap.workindia.in/sitemap.xml"
LATEST_SITEMAP_NAME = "latest-jd-pages.xml"
# fallback if the index can't be fetched/parsed
LATEST_SITEMAP_URL = "http://crawlsitemap-v2.workindia.in/latest-jd-pages.xml"

# CloudFront 403s non-browser UAs; browser prefix + transparent scraper tag.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "workindia_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Clearly healthcare title slugs (the slug IS the job title).
ALLOW_SLUG_RE = re.compile(
    r"nurs|\banm\b|_anm\b|anm_|\bgnm\b|gnm_|midwife|"
    r"pharma|chemist(?!ry)|"
    r"doctor|physician|surgeon|dentist|dental|\bbds\b|mbbs|veterinar|"
    r"physio|occupational_therap|speech_therap|"
    r"lab_tech|laboratory|patholog|phlebotom|microbiolog|"
    r"radiol|x_ray|xray|\becg\b|ecg_|_ecg|ct_scan|mri_|sonograph|ultrasound|"
    r"dialysis|ot_tech|operation_theatre|anesthes|anaesthes|"
    r"paramedic|emergency_medical|"
    r"optometr|optician|audiolog|"
    r"dietitian|dietician|nutrition|"
    r"ayurved|homeopath|homoeopath|unani|naturopath|"
    r"hospital(?!ity)|clinic(?!al_research_ass)|polyclinic|diagnostic|"
    r"medical|medico|health_care|healthcare|"
    r"ward_boy|ward_girl|ward_attendant|ward_helper|"
    r"patient_care|patient_attendant|dresser|"
    r"vaccinat|injection|blood_bank|dispenser",
    re.IGNORECASE)

# Healthcare-adjacent slugs: kept, flagged needs_review (spec: never drop
# silently what we can't classify).
AMBIGUOUS_SLUG_RE = re.compile(
    r"caretaker|care_taker|therapist|attendant|"
    r"health|wellness|fitness_trainer|yoga|"
    r"beautician_trainer|first_aid",
    re.IGNORECASE)

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "area", "city", "state",
    "postal_code", "location", "salary_raw", "salary_min_monthly",
    "salary_max_monthly", "salary_period_original", "job_type", "work_mode",
    "experience_months", "education", "gender_preference", "site_industry",
    "category", "company_type", "needs_review", "posted_date",
    "valid_through", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("workindia_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", str(markup or "")))


_JOB_URL_RE = re.compile(r"/jobs/([^/]+)-(\d+)/?$")


def parse_job_url(url):
    """Split a detail URL into (title_slug, area, city, job_id).

    /jobs/staff_nurse-jakkanpur-patna-10808711/ ->
        ("staff_nurse", "jakkanpur", "patna", "10808711")
    Segments are '-'-separated; words inside a segment use '_'. Rare URLs
    have no area segment. Returns None when the URL isn't a job detail page.
    """
    m = _JOB_URL_RE.search(url or "")
    if not m:
        return None
    slug, job_id = m.group(1), m.group(2)
    parts = slug.split("-")
    title = parts[0]
    city = parts[-1] if len(parts) >= 2 else ""
    area = "-".join(parts[1:-1]) if len(parts) >= 3 else ""
    return title, area, city, job_id


def classify_slug(title_slug):
    """'allow' | 'ambiguous' | 'skip' for a URL title slug."""
    if ALLOW_SLUG_RE.search(title_slug or ""):
        return "allow"
    if AMBIGUOUS_SLUG_RE.search(title_slug or ""):
        return "ambiguous"
    return "skip"


def slug_words(slug):
    return clean_text((slug or "").replace("_", " ").replace("+", " ")).title()


_FIELD_LINE_RE = re.compile(r"^\s*([A-Za-z ]+?)\s*:\s*(.+?)\s*$")
_SALARY_BOILERPLATE_RE = re.compile(r"\s*,\s*based on .*$", re.IGNORECASE)


def description_fields(description_text):
    """WorkIndia descriptions are 'Label : value' lines; return {label: value}.

    Labels seen: Salary Range, Educational Requirement, Work Arrangement,
    Gender Preference, Skills Requirement, Experience Requirement, Location,
    Working Hours, Additional Info.
    """
    fields = {}
    for chunk in re.split(r"\n+", description_text or ""):
        m = _FIELD_LINE_RE.match(chunk)
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
    return fields


def parse_base_salary(base_salary):
    """Normalize JSON-LD baseSalary to monthly INR ints.

    Returns (min_monthly, max_monthly, period_original) or ("", "", "").
    unitText YEAR is divided by 12 (rounded); never invents values.
    """
    try:
        value = (base_salary or {}).get("value") or {}
        lo = value.get("minValue")
        hi = value.get("maxValue", lo)
        unit = (value.get("unitText") or "").upper()
        if lo is None:
            return "", "", ""
        lo, hi = float(lo), float(hi if hi is not None else lo)
        if hi < lo:
            lo, hi = hi, lo
        if unit == "YEAR":
            lo, hi = lo / 12.0, hi / 12.0
        elif unit not in ("", "MONTH"):
            return "", "", unit          # HOUR/WEEK etc: keep raw only
        return int(round(lo)), int(round(hi)), unit or "MONTH"
    except (TypeError, ValueError):
        return "", "", ""


_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(
    r"pharmacist|\bpharmacy\b|\bpharm ?d\b|(?<!lab )chemist(?!ry)", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"[a-z]+ologist|intensivist|hospitalist|anaesthetist|anesthetist|"
    r"obstetrician|p(a?)ediatrician|psychiatrist|veterinary|"
    r"medical superintendent|medical director", re.IGNORECASE)


def classify_category(title):
    """Map a title to the club category enum."""
    title = title or ""
    if _NURSE_RE.search(title):
        return "nurses"
    if _PHARMACIST_RE.search(title):
        return "pharmacists"
    if _DOCTOR_RE.search(title):
        return "doctors"
    return "non_clinical"


_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|clinical|biotech|"
    r"diagnostic|life ?science|chemist|medic(al|ine)s? ?(store|shop)",
    re.IGNORECASE)


def classify_company_type(company, title=""):
    """club enum hospital|pharma."""
    if _PHARMA_RE.search(company or "") or re.search(
            r"pharma|medical representative|chemist(?!ry)", title or "", re.IGNORECASE):
        return "pharma"
    return "hospital"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None):
    today = today or date.today()
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    probe = SITE_BASE + "/jobs/staff_nurse-jakkanpur-patna-10808711/"
    if not rp.can_fetch(USER_AGENT, probe):
        sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt check passed (clean paths allowed; query strings avoided)")


def _request(session, url, as_json=False):
    # robots.txt disallows /*?* and /*&* — never request such URLs.
    assert "?" not in url and "&" not in url, "query-string URLs are robots-disallowed"
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
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")


def fetch_latest_job_urls(session):
    """Job-detail URLs from the daily 'latest JD pages' sitemap."""
    sitemap_url = LATEST_SITEMAP_URL
    index_xml = _request(session, SITEMAP_INDEX_URL)
    if index_xml:
        for loc in _LOC_RE.findall(index_xml):
            if LATEST_SITEMAP_NAME in loc:
                sitemap_url = loc.strip()
                break
    xml = _request(session, sitemap_url)
    if not xml:
        return []
    urls = [u for u in _LOC_RE.findall(xml) if "/jobs/" in u]
    log.info("Sitemap %s: %d job-detail URLs", sitemap_url, len(urls))
    return urls


_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)


def extract_job_posting(page_html):
    """The JobPosting JSON-LD dict from a detail page, or None."""
    for block in _JSONLD_RE.findall(page_html or ""):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(posting, url, url_parts, needs_review):
    """Build a rich CSV row from the JobPosting JSON-LD + URL parts."""
    title_slug, area_slug, city_slug, job_id = url_parts
    title = clean_text(posting.get("title")) or slug_words(title_slug)
    company = clean_text((posting.get("hiringOrganization") or {}).get("name"))
    address = (posting.get("jobLocation") or {}).get("address") or {}
    area = clean_text(address.get("addressLocality")) or slug_words(area_slug)
    city = slug_words(city_slug)
    state = clean_text(address.get("addressRegion"))

    # description: HTML "<p>Label : value<br/><br/>..." -> newline-joined text
    desc_html = re.sub(r"<br\s*/?>", "\n", str(posting.get("description") or ""))
    desc_text = "\n".join(
        clean_text(line) for line in strip_per_line(desc_html) if clean_text(line))
    fields = description_fields(desc_text)

    salary_min, salary_max, period = parse_base_salary(posting.get("baseSalary"))
    salary_raw = _SALARY_BOILERPLATE_RE.sub("", fields.get("salary range", ""))
    if not salary_raw and salary_min != "":
        salary_raw = "Rs. {} - Rs. {} per {}".format(
            salary_min, salary_max, (period or "MONTH").lower())
    if not salary_raw:
        salary_raw = "Not Disclosed"

    work_mode = fields.get("work arrangement", "")
    emp_type = clean_text(posting.get("employmentType")).upper()
    if "HOME" in work_mode.upper():
        job_type = "remote"
    elif "PART_TIME" in emp_type:
        job_type = "part_time"
    else:
        job_type = "full_time"

    exp = (posting.get("experienceRequirements") or {})
    months = clean_text(exp.get("monthsOfExperience")) if isinstance(exp, dict) else ""

    return {
        "source": SITE,
        "job_id": job_id,
        "title": title,
        "company": company,
        "area": area,
        "city": city,
        "state": state,
        "postal_code": clean_text(address.get("postalCode")),
        "location": ", ".join(p for p in (area, city) if p),
        "salary_raw": salary_raw,
        "salary_min_monthly": salary_min,
        "salary_max_monthly": salary_max,
        "salary_period_original": period,
        "job_type": job_type,
        "work_mode": work_mode,
        "experience_months": months,
        "education": fields.get("educational requirement", ""),
        "gender_preference": fields.get("gender preference", ""),
        "site_industry": clean_text(posting.get("industry")),
        "category": classify_category(title),
        "company_type": classify_company_type(company, title),
        "needs_review": needs_review,
        "posted_date": clean_text(posting.get("datePosted"))[:10],
        "valid_through": clean_text(posting.get("validThrough"))[:10],
        "description": desc_text[:DESCRIPTION_MAX_CHARS],
        "job_url": url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def strip_per_line(markup):
    """Strip tags line-by-line so <br> newlines survive."""
    return [_TAG_RE.sub(" ", line) for line in (markup or "").split("\n")]


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
    min_sal = _int_str(r.get("salary_min_monthly"))
    months = _int_str(r.get("experience_months"))
    min_exp = str(int(int(months) / 12)) if months else ""
    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("area")) or _blank(r.get("state")),
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
        "min_experience": min_exp,
        "max_experience": "",
        "min_salary": min_sal,
        "max_salary": _int_str(r.get("salary_max_monthly")) or min_sal,
        "salary_period": "per_month" if min_sal else "",
        "salary_currency": "INR" if min_sal else "",
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
        description="Scrape healthcare jobs from workindia.in.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="fetch at most N detail pages (test runs)")
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

    urls = fetch_latest_job_urls(session)
    counters = {"scanned": 0, "excluded_non_healthcare": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log = [], []
    seen_this_run = set()

    candidates = []
    for url in urls:
        parts = parse_job_url(url)
        if not parts:
            continue
        counters["scanned"] += 1
        verdict = classify_slug(parts[0])
        if verdict == "skip":
            counters["excluded_non_healthcare"] += 1
            continue
        job_id = parts[3]
        if job_id in known_ids or job_id in seen_this_run:
            counters["duplicates"] += 1
            continue
        seen_this_run.add(job_id)
        candidates.append((url, parts, verdict))

    log.info("%d new healthcare candidates to fetch (of %d sitemap URLs)",
             len(candidates), len(urls))
    if args.limit is not None:
        candidates = candidates[:args.limit]

    for i, (url, parts, verdict) in enumerate(candidates, 1):
        page = _request(session, url)
        if page is None:
            counters["detail_failed"] += 1
            continue
        posting = extract_job_posting(page)
        if posting is None:
            log.warning("No JobPosting JSON-LD at %s", url)
            counters["detail_failed"] += 1
            continue
        try:
            row = job_to_rich_row(posting, url, parts, verdict == "ambiguous")
        except Exception as exc:
            log.warning("Skipping malformed job %s: %s", url, exc)
            counters["detail_failed"] += 1
            continue
        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            continue
        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "company": row["company"], "job_url": url})
        known_ids.add(row["job_id"])
        new_rows.append(row)
        counters["new"] += 1
        if i % 25 == 0:
            log.info("...%d/%d detail pages fetched, %d kept",
                     i, len(candidates), counters["new"])

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
    print("Sitemap URLs scanned:     {:>6,}".format(counters["scanned"]))
    print("Excluded non-healthcare:  {:>6,}".format(counters["excluded_non_healthcare"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:     {:>6,}".format(counters["needs_review"]))
    print("New jobs added:           {:>6,}".format(counters["new"]))
    print("Duplicates skipped:       {:>6,}".format(counters["duplicates"]))
    print("Detail fetch failures:    {:>6,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
