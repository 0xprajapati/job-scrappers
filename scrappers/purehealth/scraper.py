#!/usr/bin/env python3
"""Scrape job openings from purehealth.ae (PureHealth Group, UAE).

Data source
-----------
purehealth.ae is a WordPress corporate/investor site with no careers section of
its own — every "careers" link points at the group's talent platform
talentone.ae, whose "Explore opportunities" buttons in turn open PureHealth's
**Oracle Fusion Recruiting (ORC) Candidate Experience** site:

    https://fa-eutv-saasfaprod1.fa.ocs.oraclecloud.com
        /hcmUI/CandidateExperience/en/sites/CX_6007

That SPA is fed by Oracle's public, token-free REST API (master spec §1.1 —
an underlying JSON API is the preferred source):

  * list    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
            ?onlyData=true
            &expand=requisitionList.workLocation,requisitionList.secondaryLocations
            &finder=findReqs;siteNumber=CX_6007,limit=…,offset=…,
                    sortBy=POSTING_DATES_DESC
            -> items[0].TotalJobsCount + items[0].requisitionList[]
               (Id, Title, PostedDate, PrimaryLocation, workLocation[])

  * detail  GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
            ?expand=all&onlyData=true&finder=ById;Id=<Id>,siteNumber=CX_6007
            -> Category, JobSchedule, JobShift, StudyLevel,
               ExternalPostedEndDate, ExternalDescriptionStr (HTML)

The list endpoint carries only a short teaser, so details are fetched by
default (the site posts a couple of dozen requisitions at most); `--no-details`
turns that off for a quick listing-only run.

Quirks
------
* The ATS `Category` is unreliable — "Cath Lab Staff Nurse" and "Assistant
  Nurse" are both filed under *Administration* — so the title decides the club
  category and `Category` is only the fallback (master spec §2). Titles that
  match neither are kept, flagged `needs_review` and logged to
  `needs_review.csv`.
* No salary anywhere in the API (no flexfields, no skills) ->
  `salary_raw = "Not Disclosed"`, numeric fields empty (master spec §3).
* No structured experience field either; min/max years are parsed
  conservatively out of the description text and left empty when unclear.
* Everything PureHealth posts is healthcare-sector employment, so the
  healthcare filter reduces to the category mapping above.

Time window: an ATS only lists currently OPEN requisitions (one here has been
open since 2025-11), so the first run keeps ALL of them
(`INITIAL_WINDOW_DAYS = None`); later runs use the master-spec watermark
(newest stored posted_date minus WATERMARK_GRACE_DAYS).

robots.txt: the Oracle host serves no robots.txt (404 => allowed), checked at
startup. purehealth.ae / talentone.ae are never crawled by this scraper — the
ATS host is the only target.

Outputs
-------
* purehealth_jobs.csv                        — rich cumulative store.
* ../../jobs_csv/<DD-MM-YYYY>/purehealth.csv — HealthCareers.club schema.

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
from urllib.parse import quote

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "purehealth"
CAREERS_PAGE = "https://talentone.ae/"

ATS_BASE = "https://fa-eutv-saasfaprod1.fa.ocs.oraclecloud.com"
SITE_NUMBER = "CX_6007"
REST_BASE = ATS_BASE + "/hcmRestApi/resources/latest"
LIST_URL = REST_BASE + "/recruitingCEJobRequisitions"
DETAIL_URL = REST_BASE + "/recruitingCEJobRequisitionDetails"
CE_SITE_URL = "{}/hcmUI/CandidateExperience/en/sites/{}".format(ATS_BASE, SITE_NUMBER)
JOB_URL_TEMPLATE = CE_SITE_URL + "/job/{}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# The ATS lists only OPEN requisitions, so the first run keeps everything
# (None = no initial window); later runs use the watermark (master spec §4).
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 25
MAX_EMPTY_PAGES = 3
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 5_000

RICH_CSV = "purehealth_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

COMPANY_NAME = "PureHealth"
COMPANY_ABOUT = (
    "PureHealth is the largest integrated healthcare platform in the UAE, "
    "operating hospitals, clinics, diagnostics, insurance, pharmacies and "
    "health-tech businesses — including SEHA, Sheikh Shakhbout Medical City "
    "and Daman — across the Emirates."
)
COUNTRY_NAME = "United Arab Emirates"
COUNTRY_CODE = "AE"
COUNTRY_DIAL = "+971"
DEFAULT_CITY = "Abu Dhabi"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "facility", "city", "country",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "job_type", "job_shift", "category",
    "category_original", "education", "experience_raw",
    "experience_min_years", "experience_max_years", "needs_review",
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

log = logging.getLogger("purehealth_scraper")

# ----------------------------------------------------------------------------
# Text cleaning
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text):
    """Collapse whitespace (incl. &nbsp;) and unescape HTML entities."""
    return _WS_RE.sub(
        " ", html_lib.unescape(str(text or "")).replace("\xa0", " ")).strip()


def strip_html(text):
    """Flatten the ATS's HTML description into one plain-text paragraph.

    Tags become spaces so that list items and headings never run together
    ("…policies.<li>Perform…" -> "…policies. Perform…").
    """
    return clean_text(_TAG_RE.sub(" ", str(text or "")))


def parse_city(work_locations, primary_location):
    """Best city for the row: the work location's town, else the leading part
    of "Abu Dhabi, United Arab Emirates", else DEFAULT_CITY."""
    for loc in work_locations or []:
        town = clean_text(loc.get("TownOrCity"))
        if town:
            return town
    primary = clean_text(primary_location)
    if primary and primary != COUNTRY_NAME:
        return primary.split(",")[0].strip() or DEFAULT_CITY
    return DEFAULT_CITY


def parse_facility(work_locations):
    """The ATS work-location label ("SEHA", "Sheikh Shakhbout Medical City
    (SSMC)", "Al Dar, Abu Dhabi") — kept verbatim in the rich CSV only, since
    some values are addresses rather than employers."""
    for loc in work_locations or []:
        name = clean_text(loc.get("LocationName"))
        if name:
            return name
    return ""


# ----------------------------------------------------------------------------
# Experience parsing (free text — the API has no experience field)
# ----------------------------------------------------------------------------

# "10-12 years", "2–3 years", "2 to 5 years", "5+ years" and the spelled-out
# "two (2) years" form the recruiters use — hence the optional closing paren.
_EXP_RANGE_RE = re.compile(
    r"(\d{1,2})\s*\)?\s*(?:[-–—]|\bto\b)\s*(\d{1,2})\s*\)?\s*\+?\s*year",
    re.IGNORECASE)
_EXP_SINGLE_RE = re.compile(r"(\d{1,2})\s*\)?\s*\+?\s*year", re.IGNORECASE)
_EXP_CONTEXT_RE = re.compile(r"experien|minimum|at least", re.IGNORECASE)


def parse_experience(description):
    """Return (raw_snippet, min_years, max_years) from a plain-text description.

    Only fragments that mention experience/minimum/at least are considered, so
    unrelated durations ("valid for 2 years") are ignored. Nothing found ->
    ("", "", "").
    """
    text = clean_text(description)
    if not text:
        return ("", "", "")
    for fragment in re.split(r"(?<=[.;•])\s+|\s{2,}", text):
        if not _EXP_CONTEXT_RE.search(fragment):
            continue
        match = _EXP_RANGE_RE.search(fragment)
        if match:
            lo, hi = int(match.group(1)), int(match.group(2))
            if hi < lo:
                lo, hi = hi, lo
            return (fragment.strip()[:200], str(lo), str(hi))
        match = _EXP_SINGLE_RE.search(fragment)
        if match:
            return (fragment.strip()[:200], str(int(match.group(1))), "")
    return ("", "", "")


# ----------------------------------------------------------------------------
# Category mapping (club enum: doctors | nurses | pharmacists | non_clinical)
# ----------------------------------------------------------------------------

_NURSE_RE = re.compile(r"\bnurs(e|es|ing)\b|\bmidwi(fe|ves|fery)\b", re.IGNORECASE)
_PHARM_RE = re.compile(r"\bpharmac(y|ist|ists|ies)\b", re.IGNORECASE)
# "-ologist" catches radiologist/cardiologist/…; psychologist, technologist and
# audiologist are allied-health roles, not physicians, so they are excluded.
_DOCTOR_RE = re.compile(
    r"\b(doctor|physician|surgeon|dentist|general practitioner|consultant "
    r"physician|specialist physician|registrar|medical officer|intensivist|"
    r"an(a)?esthetist|obstetrician|p(a)?ediatrician|psychiatrist)\b|"
    r"(?<!psych)(?<!audi)(?<!techn)ologist\b",
    re.IGNORECASE)

# ATS Category values -> club category, used only when the title is silent.
_ATS_CATEGORY_MAP = {
    "nursing": "nurses",
    "medical": "doctors",
    "pharmacy": "pharmacists",
    "allied health": "non_clinical",
    "administration": "non_clinical",
    "corporate": "non_clinical",
    "support services": "non_clinical",
}


def classify_category(title, ats_category=""):
    """Return (club_category, needs_review).

    The title wins whenever it matches a clinical pattern — the ATS files
    "Cath Lab Staff Nurse" under Administration, so its Category is only a
    fallback. A title the patterns miss and a Category the map misses are kept
    as non_clinical + needs_review (master spec §2 — never silently dropped).
    """
    if _PHARM_RE.search(title or ""):
        return ("pharmacists", False)
    if _NURSE_RE.search(title or ""):
        return ("nurses", False)
    if _DOCTOR_RE.search(title or ""):
        return ("doctors", False)
    mapped = _ATS_CATEGORY_MAP.get(clean_text(ats_category).lower())
    if mapped:
        return (mapped, False)
    return ("non_clinical", True)


def map_job_type(job_schedule):
    """Oracle JobSchedule -> club job_type. Unset means the recruiter left the
    field blank; hospital requisitions default to full_time."""
    schedule = clean_text(job_schedule).lower()
    if "part" in schedule:
        return "part_time"
    if "intern" in schedule:
        return "internship"
    return "full_time"


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def check_robots(session):
    """The Oracle ATS host serves no robots.txt (404 => everything allowed);
    abort if that ever changes to a disallow on the REST path."""
    robots_url = ATS_BASE + "/robots.txt"
    rp = urllib.robotparser.RobotFileParser(robots_url)
    try:
        resp = session.get(robots_url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
        return
    looks_like_robots = (resp.status_code < 400
                         and "<html" not in resp.text[:500].lower())
    rp.parse(resp.text.splitlines() if looks_like_robots else [])
    if not rp.can_fetch(USER_AGENT, LIST_URL):
        sys.exit("robots.txt disallows {} — aborting.".format(LIST_URL))
    log.info("robots.txt check passed (%s)", ATS_BASE)


def get_json(session, url, params):
    """GET with retries/backoff on 429/5xx/network errors. None = gave up."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d in %.0fs (%s)",
                        attempt, MAX_RETRIES, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, params=params,
                               timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def _finder(**pairs):
    """Build Oracle's "name;k=v,k=v" finder string (requests URL-encodes it)."""
    name = pairs.pop("_name")
    return name + ";" + ",".join("{}={}".format(k, v) for k, v in pairs.items())


def iter_requisitions(session, max_pages=None):
    """Yield requisition dicts newest-first, paging with limit/offset.

    Stops at TotalJobsCount, after MAX_EMPTY_PAGES consecutive empty pages, or
    when the caller's page budget runs out.
    """
    offset, page, empty_streak, total = 0, 0, 0, None
    while True:
        if max_pages is not None and page >= max_pages:
            log.info("Reached --max-pages=%d", max_pages)
            return
        params = {
            "onlyData": "true",
            "expand": "requisitionList.workLocation,"
                      "requisitionList.secondaryLocations",
            "finder": _finder(_name="findReqs", siteNumber=SITE_NUMBER,
                              limit=PAGE_SIZE, offset=offset,
                              sortBy="POSTING_DATES_DESC"),
        }
        data = get_json(session, LIST_URL, params)
        if data is None:
            log.error("Listing page at offset %d failed; stopping pagination",
                      offset)
            return
        items = data.get("items") or []
        block = items[0] if items else {}
        if total is None:
            total = block.get("TotalJobsCount")
            log.info("ATS site %s lists %s open requisitions",
                     SITE_NUMBER, total if total is not None else "?")
        batch = block.get("requisitionList") or []
        if not batch:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES or (
                    total is not None and offset >= total):
                return
        else:
            empty_streak = 0
            for req in batch:
                yield req
        offset += PAGE_SIZE
        page += 1
        if total is not None and offset >= total:
            return


def fetch_detail(session, job_id):
    """Full requisition record, or None when the detail call fails (the run
    continues with listing-only data)."""
    params = {
        "expand": "all",
        "onlyData": "true",
        "finder": _finder(_name="ById", Id=job_id, siteNumber=SITE_NUMBER),
    }
    data = get_json(session, DETAIL_URL, params)
    if not data:
        return None
    items = data.get("items") or []
    return items[0] if items else None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_rich_row(req, detail=None):
    detail = detail or {}
    title = clean_text(req.get("Title") or detail.get("Title"))
    work_locations = req.get("workLocation") or detail.get("workLocation") or []
    ats_category = clean_text(detail.get("Category"))
    category, needs_review = classify_category(title, ats_category)

    description = strip_html(detail.get("ExternalDescriptionStr")
                             or req.get("ShortDescriptionStr"))
    experience_raw, exp_min, exp_max = parse_experience(description)

    return {
        "source": SITE,
        "job_id": str(req.get("Id") or detail.get("Id") or ""),
        "title": title,
        "company": COMPANY_NAME,
        "facility": parse_facility(work_locations),
        "city": parse_city(work_locations, req.get("PrimaryLocation")),
        "country": COUNTRY_NAME,
        # the ATS exposes no pay data — capture, don't invent (master spec §3)
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
        "job_type": map_job_type(detail.get("JobSchedule")),
        "job_shift": clean_text(detail.get("JobShift")),
        "category": category,
        "category_original": ats_category,
        "education": clean_text(detail.get("StudyLevel")),
        "experience_raw": experience_raw,
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "needs_review": needs_review,
        "posted_date": clean_text(req.get("PostedDate")
                                  or detail.get("ExternalPostedStartDate"))[:10],
        "expires_at": clean_text(req.get("PostingEndDate")
                                 or detail.get("ExternalPostedEndDate"))[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": JOB_URL_TEMPLATE.format(quote(str(req.get("Id") or ""))),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def rich_row_to_club_row(r):
    def val(key):
        v = r.get(key, "")
        return "" if pd.isna(v) else str(v)

    return {
        "country_name": val("country") or COUNTRY_NAME,
        "country_code": COUNTRY_CODE,
        "country_dial_code": COUNTRY_DIAL,
        "city_name": val("city") or DEFAULT_CITY,
        # the employer of record is the group; work-location labels such as
        # "Al Dar, Abu Dhabi" are addresses, so they stay in the rich CSV only
        "company_name": COMPANY_NAME,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": COMPANY_ABOUT,
        "title": val("title"),
        "description": val("description"),
        "job_type": val("job_type") or "full_time",
        "category": val("category") or "non_clinical",
        "application_url": val("job_url") or CE_SITE_URL,
        "posted_at": val("posted_date"),
        "min_experience": val("experience_min_years"),
        "max_experience": val("experience_max_years"),
        # no salary data in the Oracle ORC API — left blank, never invented
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        "expires_at": val("expires_at"),
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date) or None = keep everything (first run: the
    ATS only lists open requisitions, so all of them are current)."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    if INITIAL_WINDOW_DAYS is not None:
        return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    return None


def within_window(posted_date, cutoff):
    if cutoff is None:
        return True
    return bool(posted_date) and posted_date >= cutoff


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
        description="Scrape PureHealth (purehealth.ae / talentone.ae) job "
                    "openings from the Oracle Recruiting Candidate Experience "
                    "API.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages of {} (test runs)"
                             .format(PAGE_SIZE))
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N requisitions (test runs)")
    parser.add_argument("--no-details", action="store_true",
                        help="skip the per-job detail call (faster, but no "
                             "full description / category / education)")
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
    log.info("Existing CSV has %d known jobs; cutoff: %s",
             len(known_ids), cutoff or "none (keeping all open requisitions)")

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "errors": 0}
    new_rows, review_log = [], []

    for req in iter_requisitions(session, max_pages=args.max_pages):
        if args.limit is not None and counters["scanned"] >= args.limit:
            log.info("Reached --limit=%d", args.limit)
            break
        counters["scanned"] += 1
        job_id = str(req.get("Id") or "")
        if not job_id:
            log.warning("Requisition without Id skipped: %s", req.get("Title"))
            counters["errors"] += 1
            continue

        posted_date = clean_text(req.get("PostedDate"))[:10]
        # listing is newest-first, so the window/dedup checks run before the
        # (costlier) detail call
        if not within_window(posted_date, cutoff):
            counters["excluded_old"] += 1
            continue
        if job_id in known_ids:
            counters["duplicates"] += 1
            continue

        detail = None
        if not args.no_details:
            try:
                detail = fetch_detail(session, job_id)
            except Exception as exc:                    # never kill the run
                log.warning("Detail fetch failed for %s: %s", job_id, exc)
            if detail is None:
                log.warning("No detail record for %s (%s); using listing data",
                            job_id, req.get("Title"))

        try:
            row = build_rich_row(req, detail)
        except Exception as exc:
            log.warning("Skipping malformed requisition %s: %s", job_id, exc)
            counters["errors"] += 1
            continue

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "category_original": row["category_original"]})
        known_ids.add(job_id)
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
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Requisitions scanned:  {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff or "no cutoff",
                                                    counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    print("Errors (skipped rows): {:>5,}".format(counters["errors"]))


if __name__ == "__main__":
    main()
