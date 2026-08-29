#!/usr/bin/env python3
"""Scrape development-sector job listings from ngobox.org.

Data source
-----------
NGOBox (a CSRBOX property) is India's NGO/CSR job board — plain PHP, no
anti-bot (probed 2026-08-26). Supply is programme roles at NGOs, foundations
and CSR arms: district programme managers at public-health NGOs (Piramal
Swasthya), community health, M&E, WASH, nutrition — real Public Health
inventory, plus a majority of out-of-scope CSR/education/livelihood/admin
roles the classifier drops.

Discovery is the listing page, which holds two independently paginated
blocks (server-side, plain GET):

    https://ngobox.org/job_listing.php?page=N    "featured" jobs, 20/page
    https://ngobox.org/job_listing.php?page1=M   "standard" jobs, 10/page

Each block is paged until a page yields no unseen ids (the standard block's
cards repeat on every ?page=N, so only ?page1= reaches its tail). Cards
link `job-detail_<slug>_<id>` with a sequential numeric id (~109xxx in
Aug 2026). RFPs/EOIs live on a separate rfp_eoi_listing.php and never enter
this crawl.

Detail pages have NO JobPosting JSON-LD — fields are parsed from stable
HTML anchors:

* `<h1 class="card-header">` — title;
* `<strong>Organization: </strong>...</h4>` — hiring org;
* `<strong>Apply By: </strong>...</h2>` — deadline ("25 Sep 2026", or the
  literal "No Deadline");
* `<p class="card-text2"><strong>Location: </strong>City(State)</p>` —
  city optional ("(Odisha)" alone on state-wide postings);
* the description is everything from the first
  `<div class="row row_section font_chance12">` to the ad-script comment
  that follows the last section (includes the "How to apply" section).

The posted date appears NOWHERE in the visible page — but the HTML
`<title>` tag embeds it: `<title>Title-Org-25 Aug . 2026-NGO jobs in
India, ...`. Verified monotonic with job_id (109022→30 Jul, 109310→18 Aug,
109408→25 Aug 2026), so it is the posting date and drives the watermark.

robots.txt is a 404 (no robots policy published) — verified at startup
with urllib.robotparser, which treats a missing file as allow-all.

Verified quirks
---------------
* posted_date exists ONLY on the detail page (its <title>), so a new id's
  detail must be fetched before the cutoff can be judged. Out-of-window
  ids are remembered in seen_old_ids.csv and dropped ids in
  out-of-scope.csv (full rows, reversible) — the devnetjobs idiom — so
  each id is fetched at most once, ever. Steady state ≈ ~15 listing
  requests + 1 detail request per genuinely new posting.
* The featured block repeats the standard block's first page on every
  ?page=N — listing links are deduped on the numeric id.
* The board publishes NO salary anywhere → blank club salary columns,
  never invented.
* Location "City(State)" often has no space before the paren
  ("Mumbai(Maharashtra)"); state-wide postings are "(Odisha)" with no
  city. City falls back to state, then "India", in the club export.

Classification (shared taxonomy)
--------------------------------
The whole board is crawled — fetch wide, filter tight. The keep/drop and
labeling decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description). NGOBox
publishes no sector/skill tags, so `skills` is always "".

in_scope False (CSR, education, livelihoods, admin) -> DROPPED, counted
excluded_out_of_scope, full row appended to out-of-scope.csv.
in_scope True fills category/sub_category/role_family and the score-trace
columns; needs_review True keeps the row AND appends it to needs_review.csv.

Outputs
-------
* ngobox_jobs.csv — rich cumulative store (dedup key: job_id), source of
  truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/ngobox.csv — the same jobs mapped to the
  HealthCareers.club CLUB_COLUMNS schema.
* seen_old_ids.csv — out-of-window ids (detail-fetch skip list).
* out-of-scope.csv — dropped rows + their skip list (reversible).
* needs_review.csv — kept but flagged.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(14); later runs keep only jobs newer than the newest stored posted_date
minus WATERMARK_GRACE_DAYS of overlap.

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
import logging
import os
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

# The one shared classifier (see ../../instructions/taxonomy-migration-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "ngobox"
SITE_BASE = "https://ngobox.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
LISTING_URL = SITE_BASE + "/job_listing.php"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# NGO postings trickle in slowly and stay open ~30 days; a slightly wider
# first-run window seeds a usable Public Health corpus. No salary filter
# (master spec): salaries captured, never filtered on — this board has none.
INITIAL_WINDOW_DAYS = 14
WATERMARK_GRACE_DAYS = 2

# Each block is paged until a page adds no unseen ids; the caps are a
# runaway guard only (~12 featured + ~3 standard pages in Aug 2026).
MAX_PAGES_PER_BLOCK = 100

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "ngobox_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "state", "country",
    "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in", "needs_review",
    "experience_min_years", "posted_date", "valid_through", "description",
    "job_url", "scraped_at",
]

log = logging.getLogger("ngobox_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    markup = re.sub(r"<!--.*?-->", " ", markup or "", flags=re.S)
    markup = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup,
                    flags=re.S | re.IGNORECASE)
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table)[^>]*>", " ",
                    markup, flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


# ---- dates ------------------------------------------------------------------

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# "25 Aug . 2026", "25 Sep  2026", "30 Sep. 2026" — the site sprinkles
# stray dots/spaces between the month and year.
_SITE_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\s*\.?\s*(\d{4})")


def parse_site_date(text):
    """A '25 Aug . 2026'-style date as ISO YYYY-MM-DD, or ""."""
    match = _SITE_DATE_RE.search(clean_text(text))
    if not match:
        return ""
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name[:3].lower())
    if not month:
        return ""
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return ""


# ---- listing pages ----------------------------------------------------------

_LISTING_LINK_RE = re.compile(r'href="(job-detail_[^"]*?_(\d+))"')


def parse_listing_links(page_html):
    """[(job_id, absolute detail URL), ...] in page order, deduped."""
    links, seen = [], set()
    for href, job_id in _LISTING_LINK_RE.findall(page_html or ""):
        if job_id in seen:
            continue
        seen.add(job_id)
        links.append((job_id, urljoin(SITE_BASE + "/", html_lib.unescape(href))))
    return links


# ---- detail page ------------------------------------------------------------

_TITLE_RE = re.compile(r'<h1 class="card-header">(.*?)</h1>', re.S)
_ORG_RE = re.compile(r"<strong>Organization:\s*</strong>(.*?)</h4>", re.S)
_DEADLINE_RE = re.compile(r"<strong>Apply By:\s*</strong>(.*?)</h2>", re.S)
_LOCATION_RE = re.compile(
    r'<p class="card-text2"><strong>Location:\s*</strong>(.*?)</p>', re.S)
# The <title> tag is the ONLY place the posted date exists:
# <title>Job Title-Org Name-25 Aug . 2026-NGO jobs in India, ...
_POSTED_TITLE_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3,9}\s*\.?\s*\d{4})\s*-\s*NGO jobs in India")

_DESCRIPTION_START = '<div class="row row_section font_chance12">'
# End anchors after the last description section, tried in order.
_DESCRIPTION_ENDS = ["<!---<script", "pagead2", '<div id="footer"', "<footer"]


def parse_detail(page_html):
    """The detail page's fields as a dict (title/company/city/state/...)."""
    page_html = page_html or ""

    def first(regex):
        match = regex.search(page_html)
        return clean_text(match.group(1)) if match else ""

    deadline = first(_DEADLINE_RE)
    city, state = parse_location(first(_LOCATION_RE))
    return {
        "title": first(_TITLE_RE),
        "company": first(_ORG_RE),
        "city": city,
        "state": state,
        "posted_date": parse_site_date(first(_POSTED_TITLE_RE)),
        # "No Deadline" (and any other non-date) parses to ""
        "valid_through": parse_site_date(deadline),
        "description": parse_description(page_html),
    }


def parse_location(text):
    """'Mumbai(Maharashtra)' / 'Hubli-Dharwad (Karnataka)' / '(Odisha)'
    -> (city, state). State-wide postings have no city."""
    text = clean_text(text)
    match = re.match(r"^(.*?)\s*\(([^)]*)\)", text)
    if match:
        return clean_text(match.group(1)), clean_text(match.group(2))
    return text, ""


def parse_description(page_html):
    """Visible text of the description sections (JD + How-to-apply)."""
    start = page_html.find(_DESCRIPTION_START)
    if start < 0:
        return ""
    start += len(_DESCRIPTION_START)
    end = -1
    for marker in _DESCRIPTION_ENDS:
        end = page_html.find(marker, start)
        if end > 0:
            break
    segment = page_html[start:end] if end > 0 else page_html[start:]
    return strip_html(segment)[:DESCRIPTION_MAX_CHARS]


# "Minimum 4 years of experience", "at least 3 years", "2+ years of
# experience", "2–4 years' experience" (the board favours en-dash ranges,
# whose lower bound is the minimum) — grounded extraction only, never
# inferred; the age-range pattern ("age group 25-40 years") never matches
# because it lacks the experience context / minimum keyword.
_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*(?:\+|[-–—]\s*\d{1,2})?\s*years?",
               re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|[-–—]\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
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


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    NGOBox publishes no sector/skill tags, so the `skills` signal is empty.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""), "",
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


# company_type (hospital|pharma) is a separate club field, NOT a category;
# NGOs default to "hospital" (the fleet-wide convention for the club enum).
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


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
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml,application/xml"})
    return session


def check_robots(session):
    """ngobox.org publishes no robots.txt (404) — treated as allow-all."""
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LISTING_URL, SITE_BASE + "/job-detail_x_109000"):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch(session, url, params=None):
    """GET one page with retries/backoff. Returns text or None."""
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
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def crawl_listing(session):
    """Every currently listed (job_id, detail URL), deduped across blocks.

    The two blocks page independently: ?page=N walks the featured block
    (the standard block's first 10 cards repeat on every such page) and
    ?page1=M walks the standard block. Each stops at the first page that
    adds no unseen id.
    """
    found = {}
    for param in ("page", "page1"):
        for page in range(1, MAX_PAGES_PER_BLOCK + 1):
            params = None if page == 1 else {param: page}
            page_html = fetch(session, LISTING_URL, params=params)
            if page_html is None:
                break
            links = parse_listing_links(page_html)
            new = [(i, u) for i, u in links if i not in found]
            found.update(new)
            log.debug("Listing %s=%d: %d links, %d new",
                      param, page, len(links), len(new))
            if not links or (not new and page > 1):
                break
    return sorted(found.items(), key=lambda kv: int(kv[0]), reverse=True)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(job_id, job_url, page_html):
    """Rich row from a detail page's parsed fields."""
    fields = parse_detail(page_html)
    return {
        "source": SITE,
        "job_id": str(job_id),
        "title": fields["title"],
        "company": fields["company"],
        "city": fields["city"],
        "state": fields["state"],
        "country": "IN",
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "experience_min_years": parse_experience_years(fields["description"]),
        "posted_date": fields["posted_date"],
        "valid_through": fields["valid_through"],
        "description": fields["description"],
        "job_url": job_url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    return {
        "country_name": "India",
        "country_code": _blank(r.get("country")) or "IN",
        "country_dial_code": "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")) or "India",
        "company_name": _blank(r.get("company")),
        "company_type": classify_company_type(_blank(r.get("company"))),
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _blank(r.get("experience_min_years")),
        "max_experience": "",
        # no structured qualification field on the board — grounded
        # extraction from the description only, never inferred
        "qualification": extract_qualification(_blank(r.get("description"))),
        # the board publishes no salary anywhere
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
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
        description="Scrape development-sector jobs from ngobox.org.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail pages (test runs)")
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
    seen_old = load_id_column(SEEN_OLD_CSV)
    out_of_scope_ids = load_id_column(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs (%d known-old, %d known "
             "out-of-scope skipped); keeping jobs posted on/after %s%s",
             len(known_ids), len(seen_old), len(out_of_scope_ids), cutoff,
             " (--since override)" if args.since else "")

    listed = crawl_listing(session)
    log.info("Listing pages yielded %d active postings", len(listed))
    if not listed:
        sys.exit("Listing crawl yielded zero job links — page shape changed? "
                 "Aborting.")

    counters = {"scanned": 0, "duplicates": 0, "skipped_old": 0,
                "skipped_out_of_scope": 0, "excluded_old": 0,
                "excluded_out_of_scope": 0, "needs_review": 0, "new": 0,
                "detail_failed": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0

    for job_id, job_url in listed:          # descending id ~= newest first
        counters["scanned"] += 1
        if job_id in known_ids:
            counters["duplicates"] += 1
            continue
        if job_id in seen_old:
            counters["skipped_old"] += 1
            continue
        if job_id in out_of_scope_ids:
            counters["skipped_out_of_scope"] += 1
            continue
        if args.limit is not None and fetched >= args.limit:
            break

        detail_html = fetch(session, job_url)
        fetched += 1
        try:
            row = build_row(job_id, job_url, detail_html or "")
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", job_id, exc)
            continue
        if not row["title"]:
            counters["detail_failed"] += 1
            log.warning("No parseable detail for job_id=%s", job_id)
            continue

        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            seen_old.add(job_id)
            new_seen_old.append({"job_id": job_id,
                                 "posted_date": row["posted_date"]})
            continue

        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            out_of_scope_ids.add(job_id)
            dropped_rows.append(row)
            continue

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "company": row["company"]})
        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1

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
    if new_seen_old:
        old_df = pd.DataFrame(new_seen_old)
        try:
            old_df = pd.concat([pd.read_csv(SEEN_OLD_CSV, dtype=str), old_df],
                               ignore_index=True)
        except FileNotFoundError:
            pass
        old_df.drop_duplicates(subset="job_id").to_csv(SEEN_OLD_CSV, index=False)
    if dropped_rows:
        total = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)
    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    print("\n===== Run summary =====")
    print("Listed postings scanned:      {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Skipped (known old):          {:>6,}".format(counters["skipped_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail parse failures:        {:>6,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
