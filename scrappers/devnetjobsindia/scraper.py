#!/usr/bin/env python3
"""Scrape development-sector job listings from devnetjobsindia.org.

Data source
-----------
DevNetJobsIndia is India's development/NGO job board — plain ASP.NET, no
anti-bot (probed 2026-08-26). The board's supply is programme roles at NGOs,
foundations and public-health projects: HIV/TI outreach workers, community
health, M&E, epidemiology, nutrition — real Public Health inventory, plus a
majority of out-of-scope admin/education/livelihood roles the classifier
drops.

Discovery is the sitemap:

    https://devnetjobsindia.org/sitemap.aspx     (robots.txt names it)

which lists EVERY active posting as
`jobdescription.aspx?job_id=<sequential int>` (~628 URLs, regenerated daily
— every <lastmod> is stamped with today's date, so lastmod carries no
per-job information). The homepage and the listing pages
(standard_jobs/highlighted_jobs/consulting_jobs/rfp_assignments) are all
strict subsets of the sitemap, and their cards carry NO posted date — only
"Apply by" — so the sitemap is the one crawl source and one request per run.

Every detail page embeds a schema.org JobPosting JSON-LD block with the
exact `datePosted`, `validThrough`, clean `title`, full HTML `description`,
`hiringOrganization.name` and a `jobLocation` address
(locality/region/country). Two site bugs to tolerate:

* the JSON-LD block ends with a stray extra `}` after the object — parsed
  with `raw_decode` (which stops at the end of the first object) instead of
  `json.loads`;
* the detail page's Apply-by span is mislabeled
  `ContentPlaceHolder1_JD1_lblPostedDate` — the visible "posted date" is the
  DEADLINE. Only the JSON-LD datePosted is trusted.

Detail pages also carry up to three "Relevant Sectors" tags
(`lblSector1..3`, e.g. "Health, Doctors, Nurses, HIV/AIDS, Nutrition") —
the site's own curated role signal, passed to classify_job as `skills` and
kept in the rich CSV as a raw source column.

robots.txt: `Allow: /` with only /FCKeditor/ and /admin/ disallowed.
Compliance is verified at startup with urllib.robotparser.

Verified quirks
---------------
* posted_date exists ONLY in the detail JSON-LD, so a new id's detail must
  be fetched before the cutoff can be judged. Out-of-window ids are
  remembered in seen_old_ids.csv and dropped ids in out-of-scope.csv (full
  rows, reversible) — the jobberman/michaelpage idiom — so each id is
  fetched at most once, ever. Steady state ≈ 1 sitemap request + 1 detail
  request per genuinely new posting.
* RFPs/tenders share the job_id space and the same JobPosting JSON-LD
  (they have their own rfp_assignments.aspx listing but appear in the
  sitemap like any job). They are procurement notices, not jobs, so a row
  whose title looks like one (RFP/EOI/tender/empanelment...) is marked
  `is_rfp` and — when the classifier keeps it — force-flagged into
  needs_review.csv rather than silently exported as a job.
* The board publishes NO salary anywhere → "Not Disclosed", blank club
  salary columns, never invented.
* `jobLocation.addressRegion` is the literal string "India" on nationwide
  postings — treated as no state, city fallback "India" in the club export.
* NGO employers don't fit the club's hospital|pharma enum; the usual
  company-text regex marks the odd CRO/diagnostics employer "pharma" and
  everything else defaults to "hospital" (the fleet-wide convention).

Classification (shared taxonomy)
--------------------------------
The whole board is crawled — fetch wide, filter tight. The keep/drop and
labeling decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the detail page's Relevant Sectors tags, joined
* description = the JSON-LD description, HTML-stripped

in_scope False (admin, livelihoods, education, clinical nursing) -> DROPPED,
counted excluded_out_of_scope, full row appended to out-of-scope.csv.
in_scope True fills category/sub_category/role_family and the score-trace
columns; needs_review True keeps the row AND appends it to needs_review.csv.

Outputs
-------
* devnetjobsindia_jobs.csv — rich cumulative store (dedup key: job_id),
  source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/devnetjobsindia.csv — the same jobs mapped to
  the HealthCareers.club CLUB_COLUMNS schema.
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

SITE = "devnetjobsindia"
SITE_BASE = "https://devnetjobsindia.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SITEMAP_URL = SITE_BASE + "/sitemap.aspx"
JOB_URL_TEMPLATE = SITE_BASE + "/jobdescription.aspx?job_id={}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# NGO postings trickle in slowly and stay open ~30 days; a slightly wider
# first-run window seeds a usable Public Health corpus. No salary filter
# (master spec): salaries captured, never filtered on — this board has none.
INITIAL_WINDOW_DAYS = 14
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "devnetjobsindia_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "state", "country",
    "sectors", "is_rfp", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "needs_review", "experience_min_years", "posted_date", "valid_through",
    "description", "job_url", "scraped_at",
]

log = logging.getLogger("devnetjobsindia_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table)[^>]*>", " ",
                    markup or "", flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


# ---- sitemap ----------------------------------------------------------------

_SITEMAP_ID_RE = re.compile(r"jobdescription\.aspx\?job_id=(\d+)", re.IGNORECASE)


def parse_sitemap_ids(xml):
    """All active job ids from sitemap.aspx, newest (highest id) first.

    <lastmod> is the sitemap's own generation date on every entry, so it is
    ignored; the ids are sequential, so descending order ~= newest first.
    """
    ids = {int(m) for m in _SITEMAP_ID_RE.findall(xml or "")}
    return [str(i) for i in sorted(ids, reverse=True)]


# ---- detail page ------------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S)
# The site appends a stray `}` after the JSON object, so json.loads raises
# "Extra data"; raw_decode stops at the end of the first object. strict=False
# tolerates raw control characters inside strings.
_LAX_DECODER = json.JSONDecoder(strict=False)

_SECTOR_RE = re.compile(r'id="ContentPlaceHolder1_JD1_lblSector\d">([^<]*)<')


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


def parse_sectors(page_html):
    """The detail page's Relevant Sectors tags, joined ("; ")."""
    return "; ".join(clean_text(s) for s in _SECTOR_RE.findall(page_html or "")
                     if clean_text(s))


def parse_iso_date(text):
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


# "Minimum 4 years of experience", "at least 3 years", "2+ years of
# experience", "3-5 years' experience" — grounded extraction only, never
# inferred; the age-range pattern ("age group 25-40 years") never matches
# because it lacks the experience context / minimum keyword.
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


# RFPs/tenders share the sitemap and the JobPosting JSON-LD but are
# procurement notices, not jobs — detected on the title, kept + flagged.
_RFP_TITLE_RE = re.compile(
    r"\b(rfp|rfq|eoi|tender|empanelment|request for proposals?|"
    r"expressions? of interest|call for proposals?)\b", re.IGNORECASE)


def is_rfp_title(title):
    return bool(_RFP_TITLE_RE.search(title or ""))


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The detail page's Relevant Sectors tags are the curated `skills` signal;
    they stay in the rich CSV as a raw source column and never decide the
    category. Returns in_scope — False means DROP the row
    (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("sectors", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    # An in-scope RFP is a procurement notice about an in-scope programme,
    # not a job opening — always reviewed by a human before export.
    row["needs_review"] = verdict["needs_review"] or bool(row.get("is_rfp"))
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
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (SITEMAP_URL, JOB_URL_TEMPLATE.format(300000)):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch(session, url):
    """GET one page with retries/backoff. Returns text or None."""
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
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
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

def build_row(job_id, posting, sectors):
    """Rich row from a detail page's JobPosting JSON-LD + sector tags."""
    title = clean_text(posting.get("title"))

    address = (posting.get("jobLocation") or {}).get("address") or {}
    city = clean_text(address.get("addressLocality"))
    state = clean_text(address.get("addressRegion"))
    if state.lower() == "india":     # nationwide posting, not a state
        state = ""

    description = strip_html(posting.get("description") or "")[:DESCRIPTION_MAX_CHARS]

    org = posting.get("hiringOrganization")
    company = clean_text(org.get("name")) if isinstance(org, dict) else ""

    return {
        "source": SITE,
        "job_id": str(job_id),
        "title": title,
        "company": company,
        "city": city,
        "state": state,
        "country": clean_text(address.get("addressCountry")) or "IN",
        "sectors": sectors,
        "is_rfp": is_rfp_title(title),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "experience_min_years": parse_experience_years(description),
        "posted_date": parse_iso_date(posting.get("datePosted")),
        "valid_through": parse_iso_date(posting.get("validThrough")),
        "description": description,
        "job_url": clean_text(posting.get("url")) or JOB_URL_TEMPLATE.format(job_id),
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
        description="Scrape development-sector jobs from devnetjobsindia.org.")
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

    sitemap_xml = fetch(session, SITEMAP_URL)
    if sitemap_xml is None:
        sys.exit("Could not fetch {} — aborting.".format(SITEMAP_URL))
    all_ids = parse_sitemap_ids(sitemap_xml)
    log.info("Sitemap lists %d active postings", len(all_ids))
    if not all_ids:
        sys.exit("Sitemap yielded zero job ids — page shape changed? Aborting.")

    counters = {"scanned": 0, "duplicates": 0, "skipped_old": 0,
                "skipped_out_of_scope": 0, "excluded_old": 0,
                "excluded_out_of_scope": 0, "needs_review": 0, "new": 0,
                "detail_failed": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0

    for job_id in all_ids:                     # descending id ~= newest first
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

        detail_html = fetch(session, JOB_URL_TEMPLATE.format(job_id))
        fetched += 1
        posting = parse_job_posting(detail_html or "")
        if not posting:
            counters["detail_failed"] += 1
            log.warning("No JobPosting JSON-LD for job_id=%s", job_id)
            continue
        try:
            row = build_row(job_id, posting, parse_sectors(detail_html))
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", job_id, exc)
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
                               "company": row["company"],
                               "sectors": row["sectors"],
                               "is_rfp": row["is_rfp"]})
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
    print("Sitemap postings scanned:     {:>6,}".format(counters["scanned"]))
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
