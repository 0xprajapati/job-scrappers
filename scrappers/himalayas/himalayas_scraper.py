#!/usr/bin/env python3
"""Scrape REMOTE clinical-research / pharma-regulatory jobs from himalayas.app.

Why this source
---------------
This scraper was commissioned as "remote healthcare jobs from Glassdoor".
Glassdoor is not scrapeable within the master spec: its robots.txt disallows
/graph, /api/ and /api-web/ (the whole JSON layer), all SERP pagination
(/Job/*_IP*, /Jobs/*_P*.htm*) and the job detail pages (/job-listing/*_IE*.htm),
and it carries an explicit `Disallow: /` block for ClaudeBot / anthropic-ai /
GPTBot. On top of that every URL — including https://www.glassdoor.co.in/
index.htm — answers HTTP 403 with a "Security | Glassdoor" interstitial to any
non-browser client. himalayas.app was chosen as the replacement: it is a
remote-only board, so REMOTE is guaranteed at the source rather than inferred.

Data source
-----------
Public JSON feed, newest-first, offset paginated, 20 jobs per page:

    GET https://himalayas.app/jobs/api?offset=N
    -> {updatedAt, offset, limit, totalCount, jobs: [...20...]}

Job fields: title, excerpt, companyName, companySlug, companyLogo,
employmentType (Full Time/Part Time/Contractor/Intern/Other/Temporary/
Volunteer), minSalary, maxSalary, salaryPeriod (annual/hourly/monthly),
currency, seniority, locationRestrictions, timezoneRestrictions, categories,
parentCategories, description (HTML), pubDate + expiryDate (unix epoch
seconds), applicationLink, guid.

Verified quirks:
* pubDate is strictly descending across offsets, so the watermark early-stop
  works and we never crawl the whole 94k-job archive.
* The feed accepts NO filtering — category=, search=, q=, market= etc. are all
  silently ignored and return the identical unfiltered page. Healthcare has to
  be selected client-side (see below).
* limit= is ignored too; pages are always 20.
* "None" arrives as the *string* "None", not JSON null, in minSalary /
  maxSalary / currency. Treat it as missing.
* List-ish fields (categories, parentCategories, seniority,
  locationRestrictions) are Python-repr STRINGS — "['United States']" — not
  JSON arrays. They are parsed with ast.literal_eval.

robots.txt: `User-Agent: * / Allow: / / Disallow: /apply`. /jobs/api is
allowed; /apply is never requested. A plain descriptive User-Agent is accepted
— no browser impersonation needed.

Scope filter (taxonomy migration 2026-08-25)
--------------------------------------------
Classification is delegated ENTIRELY to the shared two-level classifier
`_shared/classification.classify_job(title, skills, description)`:

    category      "Non Clinical" | "Public Health"
    sub_category  one of the 20 shared sub-categories
    role_family   rich-CSV trace of the winning role family

Every candidate job is routed through classify_job with the feed's
`categories` slugs (hyphens flattened to spaces) as the `skills` signal and
the HTML-stripped description as `description`. `in_scope == False` rows are
dropped and counted `excluded_out_of_scope` — this is a general job board
and the overwhelming majority of listings are out of scope.

The feed's own `categories` slugs are auto-tagged and loose (measured
against 7,930 stored jobs roughly half of slug-only admissions were wrong:
"Registered Dietitian" tagged Clinical-Research, "Nurse Practitioner" tagged
Public-Health), so they are passed only as the weighted `skills` signal —
the shared scorer weights title x5 / skills x2 / description x1, so slugs
alone cannot admit a job without corroboration.

`needs_review == True` (in-scope but the title looks like a different
profession — legal counsel, quota sales, recruiting) keeps the row AND
appends it to needs_review.csv — never a silent drop.

Salary (master spec §3: capture, don't filter)
----------------------------------------------
Nothing is ever excluded for its salary. minSalary/maxSalary are stored
verbatim with their real currency in the rich CSV. The club schema's
salary_currency enum only allows INR/USD and salary_period only per_annum/
per_month, so club salary columns are populated only for USD/INR annual or
monthly pay; CAD/EUR/GBP/PLN/... and hourly rates keep their values in the
rich CSV and leave the club columns empty. Never invented.

Outputs (per the repo README + master-scraper-spec.md)
------------------------------------------------------
* himalayas_jobs.csv        — rich cumulative store (dedup key: job_id),
                              source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/himalayas.csv
                            — the same jobs mapped to the shared
                              HealthCareers.club 23-column schema.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS (2);
later runs keep only jobs newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

Run `python himalayas_scraper.py --help` for options.
"""

import argparse
import ast
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

# The one shared classifier + club contract (taxonomy migration 2026-08-25).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "himalayas"
SITE_BASE = "https://himalayas.app"
ROBOTS_URL = SITE_BASE + "/robots.txt"
API_URL = SITE_BASE + "/jobs/api"

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

# First-run window. The master spec §4 default is 7, deliberately narrowed to
# 2: this feed posts ~2,400 jobs/day, so every extra day of first-run window
# costs ~2,400 offsets (~9 min of crawl) for jobs that are already stale by
# the time they are imported. Later runs ignore this entirely and use the
# watermark instead.
INITIAL_WINDOW_DAYS = 2
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 20                 # server-fixed; limit= is ignored
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
MAX_OFFSET = 40_000            # hard stop; the archive is ~95k jobs deep
# Safety valve against a pathological row, NOT a content budget. Real
# descriptions run 600-9,900 plain-text chars (median ~3,900), so this never
# fires in practice — an earlier 3,000 cap silently truncated 84% of rows
# mid-word. Raise rather than lower if the feed ever grows longer posts.
DESCRIPTION_MAX_CHARS = 20_000

# Every listing on himalayas.app is remote — that is the whole premise of the
# board — so the club job_type enum value is always "remote" (the enum cannot
# express "remote AND part-time"; employmentType is kept in the rich CSV).
CLUB_JOB_TYPE = "remote"

RICH_CSV = str(Path(__file__).resolve().parent / "himalayas_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
OUT_OF_SCOPE_CSV = str(Path(__file__).resolve().parent / "out-of-scope.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# Rich schema (taxonomy migration 2026-08-25): `category` holds
# "Non Clinical" | "Public Health"; the family value that used to sit in the
# club `category` moved into `role_family`; the shared score trace
# (all_families / family_scores / family_confidence / matched_in) and the
# needs_review flag are stored so every admission stays auditable. The old
# `match_signal` column (trace of the removed local two-tier gate) is
# superseded by `matched_in`.
RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_slug", "company_logo",
    "locations", "country", "salary_raw", "salary_min", "salary_max",
    "salary_currency", "salary_period", "employment_type", "work_mode",
    "seniority", "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in", "needs_review",
    "company_type", "categories", "parent_categories", "timezones",
    "posted_date", "expires_date", "description", "job_url", "scraped_at",
]

NEEDS_REVIEW_COLUMNS = ["job_id", "title", "company", "category",
                        "sub_category", "role_family", "matched_in",
                        "categories"]

# country name -> (ISO alpha-2, dial code). Covers the countries that actually
# appear in the feed; anything unseen exports with empty code/dial code.
COUNTRY_CODES = {
    "United States": ("US", "+1"), "Canada": ("CA", "+1"),
    "United Kingdom": ("GB", "+44"), "Ireland": ("IE", "+353"),
    "Germany": ("DE", "+49"), "France": ("FR", "+33"),
    "Spain": ("ES", "+34"), "Portugal": ("PT", "+351"),
    "Netherlands": ("NL", "+31"), "Belgium": ("BE", "+32"),
    "Italy": ("IT", "+39"), "Poland": ("PL", "+48"),
    "Sweden": ("SE", "+46"), "Norway": ("NO", "+47"),
    "Denmark": ("DK", "+45"), "Finland": ("FI", "+358"),
    "Switzerland": ("CH", "+41"), "Austria": ("AT", "+43"),
    "Czechia": ("CZ", "+420"), "Romania": ("RO", "+40"),
    "Greece": ("GR", "+30"), "Ukraine": ("UA", "+380"),
    "India": ("IN", "+91"), "Pakistan": ("PK", "+92"),
    "Philippines": ("PH", "+63"), "Singapore": ("SG", "+65"),
    "Australia": ("AU", "+61"), "New Zealand": ("NZ", "+64"),
    "Japan": ("JP", "+81"), "Indonesia": ("ID", "+62"),
    "Malaysia": ("MY", "+60"), "Vietnam": ("VN", "+84"),
    "Mexico": ("MX", "+52"), "Brazil": ("BR", "+55"),
    "Argentina": ("AR", "+54"), "Colombia": ("CO", "+57"),
    "Chile": ("CL", "+56"), "Peru": ("PE", "+51"),
    "Costa Rica": ("CR", "+506"), "Uruguay": ("UY", "+598"),
    "South Africa": ("ZA", "+27"), "Nigeria": ("NG", "+234"),
    "Kenya": ("KE", "+254"), "Egypt": ("EG", "+20"),
    "United Arab Emirates": ("AE", "+971"), "Saudi Arabia": ("SA", "+966"),
    "Israel": ("IL", "+972"), "Turkey": ("TR", "+90"),
}

SALARY_PERIODS = {"annual": "per_annum", "monthly": "per_month",
                  "hourly": "per_hour", "weekly": "per_week",
                  "daily": "per_day"}

log = logging.getLogger("himalayas_scraper")

# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------

def parse_listish(value):
    """The feed ships lists as Python-repr strings: "['United States']".

    Returns a list of strings for any of the string / real-list / empty forms.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value or value in ("None", "[]"):
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return []
    if isinstance(parsed, (list, tuple)):
        return [str(v) for v in parsed]
    return [str(parsed)]


def clean_value(value):
    """Feed sends missing numbers/strings as the literal string "None"."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("None", "nan", "") else text


def epoch_to_date(value):
    """1785311944 -> "2026-07-29"; "" for anything unparseable."""
    text = clean_value(value)
    if not text:
        return ""
    try:
        seconds = int(float(text))
    except ValueError:
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d)[^>]*>", " ", text or "",
                  flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_description(text, limit=DESCRIPTION_MAX_CHARS):
    """Cap a description at `limit` chars on a word boundary.

    Truncation is marked with a trailing "…" so a shortened description is
    never mistaken for a complete one, and never cuts mid-word.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    # Prefer the word boundary; fall back to the hard cut only when the text
    # is one pathological unbroken token and backing off would lose most of it.
    if len(spaced) >= limit * 0.5:
        cut = spaced
    return cut.rstrip() + "…"


def job_id_from_guid(guid):
    """.../jobs/android-software-engineer-5243463048 -> "5243463048".

    Most guids carry no numeric id, so the fallback is <company>/<job-slug>
    rather than the bare job slug — two employers both posting a
    "registered-nurse" would otherwise share a dedup key and one would be
    silently discarded.
    """
    guid = clean_value(guid)
    if not guid:
        return ""
    path = guid.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    match = re.search(r"(\d{6,})$", slug)
    if match:
        return match.group(1)
    # /companies/<company>/jobs/<slug> -> "<company>/<slug>"
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "jobs" and len(parts) >= 4:
        return "{}/{}".format(parts[-3], slug)
    return slug or guid


def parse_salary(min_salary, max_salary, currency, period):
    """Master spec §3 — capture, never filter, never invent.

    (120000, 150000, "USD", "annual") -> ("USD 120000 - 150000 per annum",
                                          "120000", "150000", "USD", "per_annum")
    ("None", "None", "None", "annual") -> ("Not Disclosed", "", "", "", "")
    """
    lo_text, hi_text = clean_value(min_salary), clean_value(max_salary)
    currency = clean_value(currency)
    period_key = clean_value(period).lower()
    period = SALARY_PERIODS.get(period_key, "")

    def as_int(text):
        try:
            return str(int(round(float(text))))
        except (TypeError, ValueError):
            return ""

    lo, hi = as_int(lo_text), as_int(hi_text)
    if not lo and not hi:
        return ("Not Disclosed", "", "", "", "")
    if not lo:
        lo = hi
    if not hi:
        hi = lo
    if int(hi) < int(lo):
        lo, hi = hi, lo
    raw = "{} {} - {}{}".format(currency or "?", lo, hi,
                                " " + period.replace("_", " ") if period else "")
    return (raw, lo, hi, currency, period)


# ----------------------------------------------------------------------------
# Classification — delegated to _shared/classification.classify_job
# ----------------------------------------------------------------------------
#
# The local ROLE_FAMILIES list and its two-tier rescue gate were removed in
# the 2026-08-25 taxonomy migration: no scraper defines its own category
# regexes any more. The feed's auto-tagged `categories` slugs (measured:
# ~half wrong when trusted alone) are passed only as the weighted `skills`
# signal; the description (HTML-stripped) as `description`. The final
# keep/drop and labeling decision is classify_job's alone.


def _slug_text(slugs):
    """Feed slugs -> scorer-friendly text ("Clinical-Data-Management" ->
    "Clinical Data Management"). Pure normalization, not classification."""
    return " ".join(str(s).replace("-", " ").replace("_", " ") for s in slugs)


def classify_feed_job(title, categories=None, description=""):
    """Route one candidate job through the shared classifier."""
    return classify_job(title or "", skills=_slug_text(categories or []),
                        description=description or "")


_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?|genomic",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None, since=None):
    """Watermark cutoff, or an explicit `since` override.

    `since` exists because a single offset-paginated pass over this feed is
    NOT complete (see the README's "Feed pagination" section): deep offset
    paging re-serves some rows and silently omits others. Re-walking the full
    window after the feed regenerates is how those jobs are recovered, and the
    watermark would otherwise clamp the re-run to the last day or two.
    """
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/json"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    if not rp.can_fetch(USER_AGENT, API_URL):
        sys.exit("robots.txt disallows {} — aborting.".format(API_URL))
    log.info("robots.txt check passed")


def fetch_page(session, offset):
    """Fetch one 20-job page. Returns (jobs, total) or (None, None)."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for offset %d in %.0fs (%s)",
                        attempt, MAX_RETRIES, offset, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(API_URL, params={"offset": offset},
                               timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d at offset %d — skipping",
                            resp.status_code, offset)
                return (None, None)
            resp.raise_for_status()
            payload = resp.json()
            return (payload.get("jobs") or [], payload.get("totalCount"))
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    log.error("Giving up on offset %d after %d retries (%s)",
              offset, MAX_RETRIES, last_error)
    return (None, None)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(job, verdict):
    """Build one rich-CSV row from a feed job + its classify_job verdict."""
    title = clean_value(job.get("title"))

    categories = parse_listish(job.get("categories"))
    parent_categories = parse_listish(job.get("parentCategories"))
    locations = parse_listish(job.get("locationRestrictions"))
    seniority = parse_listish(job.get("seniority"))
    timezones = parse_listish(job.get("timezoneRestrictions"))

    salary_raw, sal_min, sal_max, sal_cur, sal_per = parse_salary(
        job.get("minSalary"), job.get("maxSalary"),
        job.get("currency"), job.get("salaryPeriod"))

    description = strip_html(job.get("description") or "") or \
        clean_value(job.get("excerpt"))

    url = clean_value(job.get("applicationLink")) or clean_value(job.get("guid"))

    return {
        "source": SITE,
        "job_id": job_id_from_guid(job.get("guid") or job.get("applicationLink")),
        "title": title,
        "company": clean_value(job.get("companyName")),
        "company_slug": clean_value(job.get("companySlug")),
        "company_logo": clean_value(job.get("companyLogo")),
        # Remote roles are hiring-region scoped, not city scoped: keep the full
        # eligibility list here, export the primary one to the club CSV.
        "locations": "; ".join(locations),
        "country": locations[0] if locations else "",
        "salary_raw": salary_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": sal_cur,
        "salary_period": sal_per,
        "employment_type": clean_value(job.get("employmentType")),
        "work_mode": "remote",
        "seniority": "; ".join(seniority),
        "company_type": classify_company_type(job.get("companyName")),
        "category": verdict["category"],
        "sub_category": verdict["sub_category"],
        "role_family": verdict["role_family"],
        "all_families": verdict["all_families"],
        "family_scores": verdict["family_scores"],
        "family_confidence": verdict["family_confidence"],
        "matched_in": verdict["matched_in"],
        "needs_review": verdict["needs_review"],
        "categories": "; ".join(categories),
        "parent_categories": "; ".join(parent_categories),
        "timezones": "; ".join(timezones),
        "posted_date": epoch_to_date(job.get("pubDate")),
        "expires_date": epoch_to_date(job.get("expiryDate")),
        "description": truncate_description(description),
        "job_url": url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the 23-column CLUB_COLUMNS contract.

    `category` / `sub_category` come straight from the shared classifier's
    values stored in the rich row. `qualification` uses the shared
    extract_qualification over the description — this feed has no structured
    qualification field, and inferring one is forbidden.

    Only USD/INR annual or monthly pay can be represented by the club enums;
    other currencies and hourly rates keep their values in the rich CSV and
    leave the club salary columns empty rather than being misdeclared.
    """
    currency = _clean(r.get("salary_currency"))
    period = _clean(r.get("salary_period"))
    lo, hi = _clean(r.get("salary_min")), _clean(r.get("salary_max"))
    exportable = (bool(lo) and currency in ("INR", "USD")
                  and period in ("per_month", "per_annum"))

    country = _clean(r.get("country"))
    code, dial = COUNTRY_CODES.get(country, ("", ""))

    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        # Remote-only board: there is no city, and inventing one would be a lie.
        "city_name": "Remote",
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": _clean(r.get("company_logo")),
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": CLUB_JOB_TYPE,
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        "qualification": extract_qualification(_clean(r.get("description"))),
        "min_salary": lo if exportable else "",
        "max_salary": (hi or lo) if exportable else "",
        "salary_period": period if exportable else "",
        "salary_currency": currency if exportable else "",
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
    club_rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(club_rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


def review_entry(row):
    """One needs_review.csv line from a rich row (dict or Series)."""
    get = row.get
    return {"job_id": get("job_id", ""), "title": get("title", ""),
            "company": get("company", ""), "category": get("category", ""),
            "sub_category": get("sub_category", ""),
            "role_family": get("role_family", ""),
            "matched_in": get("matched_in", ""),
            "categories": get("categories", "")}


def append_needs_review(entries, overwrite=False):
    """Append flagged rows to needs_review.csv, deduped on job_id.

    `overwrite=True` (used by --reclassify, which recomputes the whole store)
    replaces the file with the freshly computed full set instead, so stale
    flags from a previous taxonomy can't linger.
    """
    new_df = pd.DataFrame(entries, columns=NEEDS_REVIEW_COLUMNS, dtype=str)
    if not overwrite:
        try:
            old = pd.read_csv(NEEDS_REVIEW_CSV, dtype=str,
                              keep_default_na=False)
            new_df = pd.concat([old, new_df], ignore_index=True)
        except FileNotFoundError:
            pass
    new_df = new_df.reindex(columns=NEEDS_REVIEW_COLUMNS).fillna("")
    new_df = new_df.drop_duplicates(subset="job_id", keep="last")
    new_df.to_csv(NEEDS_REVIEW_CSV, index=False)
    return len(new_df)


def move_out_of_scope(dropped_df, target=OUT_OF_SCOPE_CSV):
    """Append dropped rows to out-of-scope.csv, deduped on job_id.

    Reversible by design: rows are moved, never discarded. Columns are the
    union of whatever the file already has and the current rich schema, so
    older vintages of the file keep their extra columns.
    """
    try:
        old = pd.read_csv(target, dtype=str, keep_default_na=False)
        combined = pd.concat([old, dropped_df], ignore_index=True)
    except FileNotFoundError:
        combined = dropped_df
    combined = combined.fillna("").drop_duplicates(subset="job_id",
                                                   keep="last")
    combined.to_csv(target, index=False)
    return len(combined)


def reclassify(args):
    """Re-run classify_job over the stored CSV and rewrite the outputs.

    Classification is a pure function of the stored title + `categories`
    slugs + description, so re-scoping never requires re-crawling. Makes no
    network requests and never re-dates a row.

    Rows the shared classifier rules out of scope are MOVED to
    `out-of-scope.csv` (append + dedupe on job_id) rather than discarded, so
    a taxonomy change is reversible and reviewable.
    """
    df = load_existing(args.output)
    if df is None or df.empty:
        sys.exit("Nothing to reclassify: {} not found or empty.".format(args.output))
    df = df.fillna("")

    kept_rows, dropped_rows, review_log = [], [], []
    for _, row in df.iterrows():
        title = str(row.get("title", "") or "")
        slugs = [s for s in str(row.get("categories", "") or "").split("; ") if s]
        verdict = classify_feed_job(title, slugs,
                                    str(row.get("description", "") or ""))
        if not verdict["in_scope"]:
            dropped_rows.append(row)
            continue
        row = row.copy()
        for col in ("category", "sub_category", "role_family", "all_families",
                    "family_scores", "family_confidence", "matched_in",
                    "needs_review"):
            row[col] = verdict[col]
        kept_rows.append(row)
        if verdict["needs_review"]:
            review_log.append(review_entry(row))

    kept = pd.DataFrame(kept_rows).reindex(columns=RICH_COLUMNS).fillna("")
    kept.to_csv(args.output, index=False)
    log.info("Kept %d of %d stored rows; moved %d now out of scope",
             len(kept), len(df), len(dropped_rows))
    log.info("category: %s", kept["category"].value_counts().to_dict())
    log.info("sub_category: %s", kept["sub_category"].value_counts().to_dict())

    if dropped_rows:
        total = move_out_of_scope(pd.DataFrame(dropped_rows))
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)

    target, n = write_club_csv(kept, args.run_date)
    log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    n_review = append_needs_review(review_log, overwrite=True)
    log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, n_review)

    print("\n===== Reclassify summary =====")
    print("Rows in stored CSV:   {:>6,}".format(len(df)))
    print("Kept (in scope):      {:>6,}".format(len(kept)))
    print("Moved (out of scope): {:>6,}".format(len(dropped_rows)))
    print("Flagged needs_review: {:>6,}".format(len(review_log)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape remote healthcare jobs from himalayas.app.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N API pages (for test runs)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (for test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job posted "
                             "on/after this date. Use to re-run the full window "
                             "against an existing CSV — a single pass over this "
                             "feed is not complete (see README: Feed pagination); "
                             "unioning passes is how misses are recovered")
    parser.add_argument("--reclassify", action="store_true",
                        help="re-apply the category classifier to the stored "
                             "CSV and rewrite the outputs; makes no network "
                             "requests (use after tuning the keyword lists)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.reclassify:
        return reclassify(args)

    session = make_session()
    check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s%s",
             len(known_ids), cutoff, " (--since override)" if args.since else "")

    counters = {"scanned": 0, "excluded_out_of_scope": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0, "refetched": 0}
    # Jobs already stored before this run. `seen_this_run` catches the feed
    # serving the same job twice regardless of whether we already had it —
    # keying off preexisting_ids alone hides re-serves of known jobs entirely,
    # which is what made the pagination instability invisible at first.
    preexisting_ids = set(known_ids)
    seen_this_run = set()
    new_rows, review_log = [], []
    offset, page_no, total, empty_pages, stop = 0, 0, None, 0, False

    while not stop and offset < MAX_OFFSET:
        if args.max_pages is not None and page_no >= args.max_pages:
            break
        jobs, count = fetch_page(session, offset)
        page_no += 1
        if jobs is None:
            log.error("Offset %d failed after retries — stopping", offset)
            break
        if total is None and count is not None:
            total = count
            log.info("Feed reports %d jobs total (newest-first)", total)
        if not jobs:
            empty_pages += 1
            if empty_pages >= MAX_EMPTY_PAGES:
                break
            offset += PAGE_SIZE
            continue
        empty_pages = 0

        page_all_old = True
        for job in jobs:
            counters["scanned"] += 1
            try:
                posted = epoch_to_date(job.get("pubDate"))
                # Undated jobs can't be placed in the window; the feed is
                # newest-first, so treat them as in-window and let dedup work.
                is_recent = (not posted) or posted >= cutoff
                if is_recent:
                    page_all_old = False
                if not is_recent:
                    counters["excluded_old"] += 1
                    continue

                verdict = classify_feed_job(
                    job.get("title"), parse_listish(job.get("categories")),
                    strip_html(job.get("description") or ""))
                if not verdict["in_scope"]:
                    counters["excluded_out_of_scope"] += 1
                    continue

                job_id = job_id_from_guid(
                    job.get("guid") or job.get("applicationLink"))
                if job_id in seen_this_run:
                    # the feed served this same job at two different offsets
                    counters["refetched"] += 1
                    continue
                seen_this_run.add(job_id)
                if job_id in preexisting_ids:
                    counters["duplicates"] += 1
                    continue

                row = job_to_rich_row(job, verdict)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job at offset %d: %s", offset, exc)
                continue

            if verdict["needs_review"]:
                counters["needs_review"] += 1
                review_log.append(review_entry(row))
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1
            if args.limit is not None and counters["new"] >= args.limit:
                stop = True
                break

        # pubDate is strictly descending across offsets (verified), so a page
        # entirely older than the cutoff means everything below it is too.
        if page_all_old:
            log.info("Offset %d entirely older than %s — stopping", offset, cutoff)
            break
        if total is not None and offset + PAGE_SIZE >= total:
            break
        offset += PAGE_SIZE
        if page_no % 25 == 0:
            log.info("...offset %d, %d in-scope jobs kept so far",
                     offset, counters["new"])

    # ---- write rich cumulative CSV (source of truth) ----
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
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        n_review = append_needs_review(review_log)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, n_review)

    print("\n===== Run summary =====")
    print("Jobs scanned:              {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):    {:>6,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))
    print("Re-served at another offset: {:>4,}".format(counters["refetched"]))
    if counters["refetched"] > counters["scanned"] * 0.02:
        print("\n  NOTE: the feed's deep pagination is unstable — {:,} of {:,}"
              "\n  scanned slots re-served a job seen at an earlier offset, so"
              "\n  this pass does not cover the whole window. This is"
              "\n  DETERMINISTIC per feed snapshot: re-running now returns the"
              "\n  identical set. Coverage only improves after the feed"
              "\n  regenerates (check `updatedAt`). See README: Feed pagination."
              .format(counters["refetched"], counters["scanned"]))


if __name__ == "__main__":
    main()
