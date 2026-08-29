#!/usr/bin/env python3
"""Scrape job listings from careers.fortrea.com (Fortrea — global CRO).

Data source
-----------
Fortrea (the CRO spun off from Labcorp in 2023) runs its career site on the
Phenom People platform — the same platform as swaasa.com, whose scraper this
one's widgets mechanics are cloned from.  One master search index (~368 jobs,
Aug 2026), global: US-heavy with China, India, Brazil, UK, Poland, Korea and
~15 more countries.  Being a CRO board it is dense in exactly this project's
Non Clinical taxonomy: CRA/clinical operations, clinical data management,
pharmacovigilance, regulatory, TMF, biometrics.

Primary source is Phenom's JSON widget endpoint (robots.txt disallows only
`*/px-widgets`, apply/chatbot/tracking paths — `/widgets` itself is allowed):

    POST https://careers.fortrea.com/widgets
        {"ddoKey": "refineSearch", "from": N, "size": 50,
         "sort": {"order": "desc", "field": "postedDate"}, ...}

A plain unauthenticated POST works — no cookies, tokens or signed headers
(probe-verified 2026-08-27).  It returns job cards as JSON, newest-first:
title, jobId (== reqId, the Workday requisition number), jobSeqNo, category,
type, city/state/country, `location`, ml_skills, descriptionTeaser,
postedDate, dateCreated, applyUrl (on fortrea.wd1.myworkdayjobs.com).

Detail pages (`/us/en/job/<jobId>`) embed `phApp.ddo.jobDetail` with the FULL
HTML description (~5-6k chars) plus a `remote` field ("Remote"/"Hybrid"/
"Other").  They are fetched for jobs the classifier keeps on card evidence
(default on; `--no-details` skips).

Verified quirks
---------------
* **postedDate is re-dated.**  Phenom refreshes `postedDate` on repost /
  re-index (only 31 of the 100 newest cards had postedDate == dateCreated;
  two spot-checked "posted today" jobs were created 2-3 weeks earlier).  The
  watermark still runs on postedDate — it is the sort key, so the
  newest-first early stop stays correct — and jobId dedup absorbs re-dated
  reposts.  `dateCreated` is kept in the rich CSV as the honest first-seen
  date.  (Same trap as shine's repost re-dating.)
* **Remote roles keep an HQ placeholder city.**  A remote US job carries
  city "Durham" / state "North Carolina" (Fortrea HQ) with the real signal
  in `location` == "Remote United States" (card) and `remote` == "Remote"
  (detail).  Remote rows export job_type "remote" with city_name "Remote"
  (the himalayas convention).
* **ml_skills' bare "business development" tag is sanitized.**  ml_skills
  are Phenom's auto-extracted JD tags and the shared taxonomy treats
  "business development" as a negative keyword.  The bare tag appears on
  in-scope roles that merely *liaise* with BD (measured 2026-08-27: 5 of the
  100 newest cards, including "Senior Clinical Project Manager - Oncology"
  and an RWE/HEOR principal — all vetoed solely by that tag, all admitted
  without it).  Real BD jobs are still dropped by their own titles
  ("Executive Director, Business Development" stays vetoed).  Dropping a tag
  can only remove a signal, never add one; the raw tag list stays in the
  rich CSV's `ml_skills` column.  (Per-board sanitize idiom — devnetjobs
  does the same for its funding-sector tag.)
* No structured salary field anywhere, but US descriptions often state
  "Pay Range: $90,000-$120,000 USD" — extracted from the description text
  only when explicitly written (annual USD ranges; hourly figures stay in
  salary_raw with the numeric columns blank).  Never invented.
* `country` is a full name ("United States of America", "Korea, Republic
  of") — mapped to ISO code + dial code via COUNTRY_META; an unmapped name
  is exported verbatim with code/dial left blank, never guessed.
* Single-employer board: company is always "Fortrea", company_type "pharma"
  (CRO — the fleet convention for the club's hospital|pharma enum).

Classification (shared taxonomy)
--------------------------------
Fetch wide, filter tight — the whole index is crawled and every candidate
goes through the ONE shared classifier, `_shared/classification.classify_job`:

* pass 1 on card evidence (title, sanitized ml_skills, descriptionTeaser)
  decides whether the job is worth a detail fetch; out-of-scope cards are
  dropped to out-of-scope.csv with the teaser as description.
* pass 2, for kept cards, re-runs classify_job with the full detail-page
  description; its verdict is final (a pass-2 flip to out-of-scope drops the
  row too).  Both passes are classify_job — no scraper-side category logic.

Outputs
-------
* fortrea_jobs.csv — rich cumulative store (dedup key: jobId), the source of
  truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/fortrea.csv — HealthCareers.club CLUB_COLUMNS.
* out-of-scope.csv — dropped rows + their skip list (reversible).
* needs_review.csv — kept but flagged.

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS (7);
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

# The one shared classifier (see ../../instructions/master-scraper-spec.md §2).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "fortrea"
SITE_BASE = "https://careers.fortrea.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
WIDGETS_URL = SITE_BASE + "/widgets"
JOB_URL_TMPL = SITE_BASE + "/us/en/job/{}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

COMPANY_NAME = "Fortrea"

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 7
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 50
REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "fortrea_jobs.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "job_seq_no", "title", "company", "site_category",
    "type_raw", "city", "state", "country", "country_code",
    "country_dial_code", "location_raw", "remote", "ml_skills",
    "salary_raw", "salary_min", "salary_max", "salary_period",
    "salary_currency", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "needs_review", "experience_min_years", "posted_date", "date_created",
    "description", "job_url", "apply_url", "scraped_at",
]

log = logging.getLogger("fortrea_scraper")

# Phenom `country` full name (lowercased) -> (ISO alpha-2, dial code).
# Covers Fortrea's operating countries (probe 2026-08-27) plus nearby
# markets; an unmapped name exports verbatim with code/dial blank — never
# guessed (devnetjobs convention).
COUNTRY_META = {
    "united states of america": ("US", "+1"),
    "united states": ("US", "+1"),
    "canada": ("CA", "+1"),
    "mexico": ("MX", "+52"),
    "brazil": ("BR", "+55"),
    "argentina": ("AR", "+54"),
    "chile": ("CL", "+56"),
    "colombia": ("CO", "+57"),
    "peru": ("PE", "+51"),
    "costa rica": ("CR", "+506"),
    "guatemala": ("GT", "+502"),
    "united kingdom": ("GB", "+44"),
    "ireland": ("IE", "+353"),
    "france": ("FR", "+33"),
    "germany": ("DE", "+49"),
    "belgium": ("BE", "+32"),
    "netherlands": ("NL", "+31"),
    "luxembourg": ("LU", "+352"),
    "spain": ("ES", "+34"),
    "portugal": ("PT", "+351"),
    "italy": ("IT", "+39"),
    "switzerland": ("CH", "+41"),
    "austria": ("AT", "+43"),
    "poland": ("PL", "+48"),
    "czech republic": ("CZ", "+420"),
    "czechia": ("CZ", "+420"),
    "hungary": ("HU", "+36"),
    "romania": ("RO", "+40"),
    "bulgaria": ("BG", "+359"),
    "serbia": ("RS", "+381"),
    "croatia": ("HR", "+385"),
    "greece": ("GR", "+30"),
    "denmark": ("DK", "+45"),
    "sweden": ("SE", "+46"),
    "norway": ("NO", "+47"),
    "finland": ("FI", "+358"),
    "ukraine": ("UA", "+380"),
    "turkey": ("TR", "+90"),
    "israel": ("IL", "+972"),
    "south africa": ("ZA", "+27"),
    "india": ("IN", "+91"),
    "china": ("CN", "+86"),
    "hong kong": ("HK", "+852"),
    "taiwan": ("TW", "+886"),
    "japan": ("JP", "+81"),
    "korea, republic of": ("KR", "+82"),
    "south korea": ("KR", "+82"),
    "singapore": ("SG", "+65"),
    "malaysia": ("MY", "+60"),
    "thailand": ("TH", "+66"),
    "viet nam": ("VN", "+84"),
    "vietnam": ("VN", "+84"),
    "philippines": ("PH", "+63"),
    "indonesia": ("ID", "+62"),
    "australia": ("AU", "+61"),
    "new zealand": ("NZ", "+64"),
}

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table)[^>]*>", " ",
                    str(markup or ""), flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


def parse_iso_date(text):
    """"2026-08-27T00:00:00.000+0000" -> "2026-08-27" ("" when absent)."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


def country_meta(country):
    """(name, iso, dial); an unmapped name keeps its name, blanks the rest."""
    name = clean_text(country)
    code, dial = COUNTRY_META.get(name.lower(), ("", ""))
    return name, code, dial


# ---- salary (grounded extraction from the description text) -----------------

# US postings write "Pay Range: $90,000-$120,000 USD" (or "The pay range for
# this role is $X - $Y") inside the description.  Only an explicit
# pay/salary/compensation-range statement with two dollar amounts is read;
# figures under $10,000 are hourly/partial rates and stay raw-only (the club
# schema has no hourly period).  Nothing is ever invented.
_PAY_RANGE_RE = re.compile(
    r"(?:pay|salary|compensation)\s+range[^$\n]{0,80}?"
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|—|\bto\b)\s*"
    r"\$?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE)


def parse_salary_from_description(description):
    """Extract an explicitly stated annual USD pay range, else {}.

    Returns salary_raw always when a range statement is found; the numeric
    salary_min/max + per_annum/USD only when both figures are >= 10,000
    (annual amounts — hourly rates like "$25.00-$30.00" keep raw only).
    """
    text = clean_text(description)
    m = _PAY_RANGE_RE.search(text)
    if not m:
        return {}
    lo = float(m.group(1).replace(",", ""))
    hi = float(m.group(2).replace(",", ""))
    if hi < lo:
        lo, hi = hi, lo
    raw = clean_text(m.group(0))[:120]
    if lo < 10_000:
        return {"salary_raw": raw}
    return {"salary_raw": raw, "salary_min": str(int(lo)),
            "salary_max": str(int(hi)), "salary_period": "per_annum",
            "salary_currency": "USD"}


# ---- experience (grounded, devnetjobs idiom) --------------------------------

_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?",
               re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
               r"(?:relevant\s+|work(?:ing)?\s+|professional\s+)?experience",
               re.IGNORECASE),
]


def parse_experience_years(description):
    """The stated minimum years of experience, or "" when not stated."""
    for regex in _EXPERIENCE_RES:
        match = regex.search(description or "")
        if match:
            years = int(match.group(1))
            if 0 < years <= 30:
                return str(years)
    return ""


# ---- skills sanitization (see module docstring) -----------------------------

# ml_skills tags removed before classification: Phenom auto-tags JDs that
# merely mention liaising with BD, and the bare tag alone vetoes in-scope
# CRO roles (measured 2026-08-27: sole cause of 4 false drops in the 100
# newest cards; real BD jobs still drop on their titles).  The raw tag list
# stays in the rich CSV's ml_skills column, so the decision is auditable.
_SKILLS_TAG_BLOCKLIST = {"business development"}


def skills_for_classifier(ml_skills):
    """Join the card's ml_skills minus the blocklisted tags."""
    return "; ".join(t.strip() for t in (ml_skills or [])
                     if t.strip() and t.strip().lower() not in _SKILLS_TAG_BLOCKLIST)


# ---- job type / remote ------------------------------------------------------

_TYPE_MAP = {"full time": "full_time", "part time": "part_time",
             "casual": "part_time"}


def map_job_type(type_raw, remote):
    """Club job_type: remote wins, else the Phenom type field."""
    if remote:
        return "remote"
    return _TYPE_MAP.get(clean_text(type_raw).lower(), "full_time")


def is_remote_card(job):
    """Card-level remote signal: `location` leads with "Remote" (the card
    has no `remote` field; city/state hold the HQ placeholder)."""
    return clean_text(job.get("location")).lower().startswith("remote")


# ----------------------------------------------------------------------------
# Classification (shared module — the only keep/drop authority)
# ----------------------------------------------------------------------------

def apply_classification(row):
    """Stamp classify_job's verdict onto a rich row; returns in_scope."""
    verdict = classify_job(row.get("title", ""),
                           skills_for_classifier(
                               (row.get("ml_skills") or "").split("; ")),
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


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, since=None):
    """First run: last INITIAL_WINDOW_DAYS. Later: watermark minus grace."""
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer (master spec §7)
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (WIDGETS_URL, JOB_URL_TMPL.format("264649")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, method, url, *, payload=None, as_json=True):
    """One HTTP call with retries/backoff on 429/5xx/network errors.

    4xx (incl. 403) is permanent — logged and returned as None, NEVER
    retried (politeness: a 403 means stop, not hammer)."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url, json=payload,
                                   timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s — not retrying", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_search_page(session, offset):
    """One page of the master search index, newest-first by postedDate.

    Returns (jobs, totalHits) — (None, None) on failure."""
    payload = {
        "lang": "en_us", "deviceType": "desktop", "country": "us",
        "pageName": "search-results", "ddoKey": "refineSearch",
        "from": offset, "size": PAGE_SIZE, "jobs": True, "counts": True,
        "all_fields": ["category", "country", "state", "city", "type"],
        "keywords": "", "global": True, "selected_fields": {},
        "locationData": {}, "sort": {"order": "desc", "field": "postedDate"},
    }
    data = _request(session, "POST", WIDGETS_URL, payload=payload)
    if not data:
        return None, None
    refine = data.get("refineSearch") or {}
    jobs = ((refine.get("data") or {}).get("jobs"))
    return jobs, refine.get("totalHits")


_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*")


def parse_job_detail(page_html):
    """phApp.ddo.jobDetail.data.job from a detail page, or {}."""
    m = _DDO_RE.search(page_html or "")
    if not m:
        return {}
    try:
        ddo, _ = json.JSONDecoder(strict=False).raw_decode(page_html[m.end():])
        return ((ddo.get("jobDetail") or {}).get("data") or {}).get("job") or {}
    except (ValueError, TypeError, AttributeError) as exc:
        log.warning("Could not parse detail ddo: %s", exc)
        return {}


def fetch_job_detail(session, job_id):
    page = _request(session, "GET", JOB_URL_TMPL.format(job_id), as_json=False)
    return parse_job_detail(page or "")


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job):
    """Rich row from one refineSearch card (taxonomy fields stamped later)."""
    title = clean_text(job.get("title"))
    country_name, code, dial = country_meta(job.get("country"))
    remote = is_remote_card(job)
    job_id = clean_text(job.get("jobId")) or clean_text(job.get("reqId"))
    teaser = clean_text(job.get("descriptionTeaser"))

    return {
        "source": SITE,
        "job_id": job_id,
        "job_seq_no": clean_text(job.get("jobSeqNo")),
        "title": title,
        "company": COMPANY_NAME,
        "site_category": clean_text(job.get("category")),
        "type_raw": clean_text(job.get("type")),
        "city": clean_text(job.get("city")),
        "state": clean_text(job.get("state")),
        "country": country_name,
        "country_code": code,
        "country_dial_code": dial,
        "location_raw": clean_text(job.get("location")),
        "remote": remote,
        "ml_skills": "; ".join(clean_text(s) for s in (job.get("ml_skills") or [])
                               if clean_text(s)),
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "experience_min_years": "",
        "posted_date": parse_iso_date(job.get("postedDate")),
        "date_created": parse_iso_date(job.get("dateCreated")),
        "description": teaser[:DESCRIPTION_MAX_CHARS],
        "job_url": JOB_URL_TMPL.format(job_id) if job_id else "",
        "apply_url": clean_text(job.get("applyUrl")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def merge_detail(row, detail):
    """Fold the detail page's full description + remote flag into a row."""
    if not detail:
        return False
    description = strip_html(detail.get("description"))[:DESCRIPTION_MAX_CHARS]
    if description:
        row["description"] = description
        row["experience_min_years"] = parse_experience_years(description)
        parsed = parse_salary_from_description(description)
        if parsed:
            row.update(parsed)
    if clean_text(detail.get("remote")).lower() == "remote":
        row["remote"] = True
    return bool(description)


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _truthy(value):
    return _blank(value).lower() in ("true", "1", "yes")


def rich_row_to_club_row(r):
    remote = _truthy(r.get("remote"))
    currency = _blank(r.get("salary_currency"))
    has_salary = bool(_blank(r.get("salary_min"))) and currency in ("INR", "USD")
    return {
        "country_name": _blank(r.get("country")),
        "country_code": _blank(r.get("country_code")),
        "country_dial_code": _blank(r.get("country_dial_code")),
        # remote rows say "Remote" (himalayas convention) — the source city
        # is an HQ placeholder; on-site rows fall back city -> state.
        "city_name": "Remote" if remote else
                     (_blank(r.get("city")) or _blank(r.get("state"))),
        "company_name": _blank(r.get("company")) or COMPANY_NAME,
        "company_type": "pharma",   # CRO — fleet convention for the enum
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": map_job_type(_blank(r.get("type_raw")), remote),
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _blank(r.get("experience_min_years")),
        "max_experience": "",
        # no structured qualification field — grounded extraction only
        "qualification": extract_qualification(_blank(r.get("description"))),
        "min_salary": _blank(r.get("salary_min")) if has_salary else "",
        "max_salary": _blank(r.get("salary_max")) if has_salary else "",
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


def load_id_column(path):
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
        description="Scrape jobs from careers.fortrea.com (Phenom widgets API).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N search pages of %d (test runs)" % PAGE_SIZE)
    parser.add_argument("--no-details", action="store_true",
                        help="skip detail fetches (teaser-only classification)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job "
                             "posted on/after this date")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    out_of_scope_ids = load_id_column(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs (%d known out-of-scope); "
             "keeping jobs posted on/after %s%s",
             len(known_ids), len(out_of_scope_ids), cutoff,
             " (--since override)" if args.since else "")

    counters = {"scanned": 0, "duplicates": 0, "skipped_out_of_scope": 0,
                "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "detail_failed": 0}
    new_rows, review_log, dropped_rows = [], [], []
    sub_category_counts = {}
    offset, page_count, empty_pages = 0, 0, 0
    total_hits = None

    while True:
        if args.max_pages is not None and page_count >= args.max_pages:
            break
        if total_hits is not None and offset >= total_hits:
            break
        jobs, hits = fetch_search_page(session, offset)
        page_count += 1
        offset += PAGE_SIZE
        if hits is not None:
            total_hits = hits
        if not jobs:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        empty_pages = 0

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                row = job_to_rich_row(job)
            except Exception as exc:  # never let one card crash the run
                log.warning("Skipping malformed job %s: %s", job.get("jobId"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
            if not row["job_id"] or row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if row["job_id"] in out_of_scope_ids:
                counters["skipped_out_of_scope"] += 1
                continue

            # Pass 1 — card evidence (title, sanitized ml_skills, teaser)
            # decides whether the job earns a detail fetch.
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                out_of_scope_ids.add(row["job_id"])
                dropped_rows.append(row)
                continue

            # Pass 2 — the full detail-page description; verdict is final.
            if not args.no_details:
                detail = fetch_job_detail(session, row["job_id"])
                if not merge_detail(row, detail):
                    counters["detail_failed"] += 1
                elif not apply_classification(row):
                    counters["excluded_out_of_scope"] += 1
                    out_of_scope_ids.add(row["job_id"])
                    dropped_rows.append(row)
                    continue

            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "site_category": row["site_category"],
                                   "country": row["country"],
                                   "sub_category": row["sub_category"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1
            key = (row["category"], row["sub_category"])
            sub_category_counts[key] = sub_category_counts.get(key, 0) + 1

        # newest-first: once a whole page predates the cutoff, stop.
        if page_all_old or len(jobs) < PAGE_SIZE:
            break

    # ---- rich cumulative CSV (source of truth) ----
    if new_rows:
        new_df = pd.DataFrame(new_rows, dtype=str)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[RICH_COLUMNS]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        combined = existing_df if existing_df is not None else \
            pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV (full-store dump, regenerated every run) ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    # ---- sidecars ----
    if dropped_rows:
        total = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)
    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    print("\n===== Run summary =====")
    print("Cards scanned:                {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    if not args.no_details:
        print("Detail fetch failures:        {:>6,}".format(counters["detail_failed"]))
    if sub_category_counts:
        print("Top sub-categories (this run):")
        top = sorted(sub_category_counts.items(), key=lambda kv: -kv[1])
        for (cat, sub), n in top[:10]:
            print("  {:>4,}  {} / {}".format(n, cat, sub or "(blank)"))


if __name__ == "__main__":
    main()
