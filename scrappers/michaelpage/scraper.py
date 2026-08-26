#!/usr/bin/env python3
"""Scrape healthcare job listings from michaelpage.co.in.

Data source
-----------
Michael Page India is a Drupal site. The healthcare vertical is a
server-rendered listing at

    https://www.michaelpage.co.in/jobs/healthcare?page=N        (30 cards/page)

so every job is healthcare-sector at the source (§2 of the master spec).
robots.txt allows /jobs/healthcare and /job-detail/... (the `*/jobs/*/*/*/`
disallow only matches deeper facet paths, and `?page=` is not the disallowed
`item_per_pages` parameter). There is no public JSON API; the listing cards
and the detail pages' schema.org JobPosting JSON-LD are the structured
sources (spec preference #2).

Listing cards carry: title, detail URL (with `ref/jn-...` job reference —
the dedup key), internal numeric id, location, contract type
(Permanent/Temporary), summary teaser and highlight bullets. They carry NO
posted date and NO salary.

Detail pages embed a JobPosting JSON-LD block with datePosted,
employmentType, industry, full HTML description, jobLocation and baseSalary
(annual INR amounts, e.g. 8000000-10000000; blank for the many confidential
mandates). JSON contains raw control characters — parsed with a
non-strict decoder.

Quirks
------
* The listing is NOT date-sorted (featured first), so every run scans all
  pages (~5 today) and the time-window cutoff is applied per job.
* posted_date exists ONLY on detail pages, so a new job's detail must be
  fetched before the cutoff can be checked. Jobs found out-of-window are
  recorded in seen_old_ids.csv so later runs skip their detail fetch.
* The hiring company is confidential (recruiter mandates): JSON-LD
  hiringOrganization is always "Michael Page". The client blurb lives in
  the description's "About Our Client" section; the highlight bullets
  (e.g. "Leading Private Equity Firm") become company_about.
* Roles are executive/corporate (sales heads, GMs, plant heads, medical
  affairs). The healthcare vertical is a SECTOR facet, so most mandates are
  commercial or manufacturing leadership and are dropped as
  excluded_out_of_scope; medical-affairs/MSL and clinical-research mandates
  are what survives.

Outputs
-------
* michaelpage_jobs.csv                          — rich cumulative store
  (dedup key: JN reference)
* ../../jobs_csv/<DD-MM-YYYY>/michaelpage.csv   — HealthCareers.club 22-col
  schema
* seen_old_ids.csv                              — out-of-window refs (skip list)
* out-of-scope.csv                              — dropped rows + their skip list
* needs_review.csv                              — kept but flagged

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored posted_date minus
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

SITE = "michaelpage"
SITE_BASE = "https://www.michaelpage.co.in"
ROBOTS_URL = SITE_BASE + "/robots.txt"
LISTING_URL = SITE_BASE + "/jobs/healthcare"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 30
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 2
MAX_PAGES_SAFETY = 100
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "michaelpage_jobs.csv"
SEEN_OLD_CSV = "seen_old_ids.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# city -> country override for the .co.in site's few non-India postings;
# everything else defaults to India.
COUNTRY_META = {
    "india": ("India", "IN", "+91"),
    "united arab emirates": ("United Arab Emirates", "AE", "+971"),
    "dubai": ("United Arab Emirates", "AE", "+971"),
    "singapore": ("Singapore", "SG", "+65"),
}

RICH_COLUMNS = [
    "source", "job_id", "internal_id", "title", "company", "company_about",
    "city", "state", "country", "country_code", "country_dial_code",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "salary_currency", "job_type", "contract_type", "site_industry",
    "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in",
    "company_type", "needs_review", "posted_date",
    "description", "job_url", "scraped_at",
]

OUT_OF_SCOPE_CSV = "out-of-scope.csv"

log = logging.getLogger("michaelpage_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


# ---- listing cards ----------------------------------------------------------

_CARD_SPLIT_RE = re.compile(r'<li class="views-row">')
_CARD_LINK_RE = re.compile(
    r'<h3>\s*<a\s+href="(/job-detail/[^"]+)"[^>]*\bid="job-(\d+)"[^>]*>'
    r'(.*?)</a>\s*</h3>', re.S)
_CARD_LOCATION_RE = re.compile(
    r'class="job-location">.*?</i>(.*?)</div>', re.S)
_CARD_CONTRACT_RE = re.compile(
    r'class="job-contract-type">.*?</i>(.*?)</div>', re.S)
_CARD_SUMMARY_RE = re.compile(
    r'class="job_advert__job-summary-text">(.*?)</div>', re.S)
_CARD_BULLETS_RE = re.compile(
    r'class="job_advert__job-desc-bullet-points"><ul>(.*?)</ul>', re.S)
_LI_RE = re.compile(r"<li>(.*?)</li>", re.S)
_REF_RE = re.compile(r"/ref/([a-z]{2}-\d{6}-\d+)", re.IGNORECASE)


def parse_listing_cards(page_html):
    """All job cards on a listing page -> list of dicts (may be empty)."""
    cards = []
    for chunk in _CARD_SPLIT_RE.split(page_html or "")[1:]:
        link = _CARD_LINK_RE.search(chunk)
        if not link:
            continue
        path, internal_id, title = link.groups()
        ref = _REF_RE.search(path)
        location = _CARD_LOCATION_RE.search(chunk)
        contract = _CARD_CONTRACT_RE.search(chunk)
        summary = _CARD_SUMMARY_RE.search(chunk)
        bullets_m = _CARD_BULLETS_RE.search(chunk)
        bullets = [strip_html(b) for b in _LI_RE.findall(
            bullets_m.group(1))] if bullets_m else []
        cards.append({
            "job_id": ref.group(1).upper() if ref else "job-" + internal_id,
            "internal_id": internal_id,
            "title": clean_text(title),
            "job_url": SITE_BASE + path.split("?")[0],
            "location": strip_html(location.group(1)) if location else "",
            "contract_type": strip_html(contract.group(1)) if contract else "",
            "summary": strip_html(summary.group(1)) if summary else "",
            "bullets": bullets,
        })
    return cards


# ---- detail JSON-LD ---------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S)
_LAX_DECODER = json.JSONDecoder(strict=False)  # payload has raw control chars


def parse_job_posting(page_html):
    """The schema.org JobPosting JSON-LD block of a detail page, or {}."""
    for m in _LDJSON_RE.finditer(page_html or ""):
        try:
            data, _ = _LAX_DECODER.raw_decode(m.group(1).strip())
        except ValueError as exc:
            log.debug("Unparseable ld+json block: %s", exc)
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def parse_base_salary(posting):
    """JobPosting.baseSalary -> club fields; {} when undisclosed.

    Real example (JN-042026-6994543): currency INR, minValue "8000000",
    maxValue "10000000", unitText "YEAR" — literal annual INR amounts.
    Most mandates are confidential and carry empty strings (never invented).
    """
    base = posting.get("baseSalary") or {}
    value = base.get("value") or {}

    def to_int(v):
        try:
            n = int(round(float(v)))
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    lo, hi = to_int(value.get("minValue")), to_int(value.get("maxValue"))
    if lo is None and hi is None:
        return {}
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if hi < lo:
        lo, hi = hi, lo
    currency = clean_text(base.get("currency")) or "INR"
    period = ("per_month"
              if clean_text(value.get("unitText")).upper() == "MONTH"
              else "per_annum")
    unit = "year" if period == "per_annum" else "month"
    return {
        "salary_raw": "{} {:,} - {:,} per {}".format(currency, lo, hi, unit),
        "salary_min": lo, "salary_max": hi,
        "salary_period": period, "salary_currency": currency,
    }


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The site's JSON-LD `industry` (site_industry) plus the card summary are
    the curated `skills` signal; the industry stays in the rich CSV as a raw
    source column only. Returns in_scope — False means DROP the row
    (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("site_industry", ""),
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


_PHARMA_RE = re.compile(
    r"pharma|life ?science|biotech|\bcro\b|clinical research|laborator|"
    r"diagnost|med.?tech|medical device|vaccin|api\b", re.IGNORECASE)


def classify_company_type(title, industry, summary):
    """Club enum hospital|pharma; recruiter mandates are matched on text."""
    haystack = " ".join(filter(None, [title, industry, summary]))
    return "pharma" if _PHARMA_RE.search(haystack) else "hospital"


def location_meta(locality, region=""):
    """Card/JSON-LD locality -> (city, country, iso, dial). The .co.in site
    is India-market; only explicit foreign localities override that."""
    city = clean_text(locality)
    key = city.lower()
    if key in COUNTRY_META:
        country, code, dial = COUNTRY_META[key]
        city = "" if key == country.lower() else city
        return city, country, code, dial
    if city.lower() in ("india", "international", ""):
        return "", "India", "IN", "+91"
    return city, "India", "IN", "+91"


_TEMP_RE = re.compile(r"temp|contract|interim", re.IGNORECASE)


def job_type_from(contract_type, employment_type):
    if _TEMP_RE.search(contract_type or ""):
        return "contract"
    if clean_text(employment_type).upper() in ("PART_TIME", "PART TIME"):
        return "part_time"
    return "full_time"


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
    for url in (LISTING_URL, LISTING_URL + "?page=1",
                SITE_BASE + "/job-detail/x/ref/jn-000000-0"):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, url, params=None):
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


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(card, posting):
    """Rich row from a listing card + its detail JobPosting JSON-LD."""
    title = card["title"] or clean_text(posting.get("title"))
    industry = clean_text(posting.get("industry"))
    summary = card["summary"]

    address = (posting.get("jobLocation") or {}).get("address") or {}
    locality = address.get("addressLocality") or card["location"]
    city, country, code, dial = location_meta(locality,
                                              address.get("addressRegion"))

    description = strip_html(posting.get("description") or "") or summary
    row = {
        "source": SITE,
        "job_id": card["job_id"],
        "internal_id": card["internal_id"],
        "title": title,
        "company": "Michael Page",   # recruiter; clients are confidential
        "company_about": "; ".join(card["bullets"])[:300],
        "city": city,
        "state": clean_text(address.get("addressRegion")),
        "country": country,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": job_type_from(card["contract_type"],
                                  posting.get("employmentType")),
        "contract_type": card["contract_type"],
        "site_industry": industry,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": classify_company_type(title, industry, summary),
        "needs_review": False,
        "posted_date": clean_text(posting.get("datePosted"))[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": card["job_url"],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    row.update(parse_base_salary(posting))
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
        "company_name": _blank(r.get("company")) or "Michael Page",
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": _blank(r.get("company_about")),
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
        # no structured qualification field on the site — grounded
        # extraction from the description only, never inferred
        "qualification": extract_qualification(_blank(r.get("description"))),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def load_seen_old(path):
    df = load_existing(path)
    if df is None or "job_id" not in df.columns:
        return set()
    return set(df["job_id"].dropna())


def load_out_of_scope_ids(path):
    """Refs already judged out of scope, so their detail page is not
    re-fetched every run.

    posted_date and the description only exist on the detail page, so the
    scope gate can only run after that fetch — the same reason
    seen_old_ids.csv exists. Rows are kept in full, so widening the taxonomy
    can recover them.
    """
    try:
        return set(pd.read_csv(path, dtype=str)["job_id"].dropna())
    except (FileNotFoundError, KeyError):
        return set()


def append_out_of_scope(rows, path=OUT_OF_SCOPE_CSV):
    """Append dropped rich rows, deduped on job_id. Moved, never discarded."""
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    try:
        df = pd.concat([pd.read_csv(path, dtype=str), df], ignore_index=True)
    except FileNotFoundError:
        pass
    df = df.fillna("").drop_duplicates(subset="job_id", keep="last")
    df.to_csv(path, index=False)
    return len(df)


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
        description="Scrape healthcare jobs from michaelpage.co.in.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages of %d (test runs)" % PAGE_SIZE)
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
    seen_old = load_seen_old(SEEN_OLD_CSV)
    out_of_scope_ids = load_out_of_scope_ids(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs (%d known-old skipped); "
             "keeping jobs posted on/after %s",
             len(known_ids), len(seen_old), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "skipped_out_of_scope": 0, "needs_review": 0, "new": 0,
                "duplicates": 0, "detail_failed": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    page, empty_pages = 0, 0

    # Listing is unsorted (featured jobs first), so scan all pages each run;
    # details are only fetched for refs we have never resolved before.
    while page < MAX_PAGES_SAFETY:
        if args.max_pages is not None and page >= args.max_pages:
            break
        page_html = _request(session, LISTING_URL,
                             params={"page": page} if page else None)
        page += 1
        cards = parse_listing_cards(page_html) if page_html else None
        if not cards:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        empty_pages = 0

        for card in cards:
            counters["scanned"] += 1
            if card["job_id"] in out_of_scope_ids:
                counters["skipped_out_of_scope"] += 1
                continue
            if card["job_id"] in known_ids or card["job_id"] in seen_old:
                counters["duplicates"] += 1
                continue
            detail_html = _request(session, card["job_url"])
            posting = parse_job_posting(detail_html or "")
            if not posting:
                counters["detail_failed"] += 1
                log.warning("No JobPosting JSON-LD for %s", card["job_url"])
            try:
                row = build_row(card, posting)
            except Exception as exc:
                log.warning("Skipping malformed job %s: %s", card["job_id"], exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                seen_old.add(row["job_id"])
                new_seen_old.append({"job_id": row["job_id"],
                                     "posted_date": row["posted_date"]})
                continue
            # Executive search over a sector facet: the classifier is what
            # decides whether the mandate is an in-scope role.
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                out_of_scope_ids.add(row["job_id"])
                dropped_rows.append(row)
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "site_industry": row["site_industry"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        if len(cards) < PAGE_SIZE:
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

    # ---- sidecars ----
    if new_seen_old:
        old_df = pd.DataFrame(sorted(
            [{"job_id": i["job_id"], "posted_date": i["posted_date"]}
             for i in new_seen_old], key=lambda d: d["job_id"]))
        try:
            prev = pd.read_csv(SEEN_OLD_CSV, dtype=str)
            old_df = pd.concat([prev, old_df], ignore_index=True)
        except FileNotFoundError:
            pass
        old_df.drop_duplicates(subset="job_id").to_csv(SEEN_OLD_CSV, index=False)
    if dropped_rows:
        total = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)
    if review_log:
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (out of scope): {:>3,}".format(counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>3,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    print("Detail parse failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
