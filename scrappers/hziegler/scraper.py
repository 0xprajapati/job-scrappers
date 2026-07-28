#!/usr/bin/env python3
"""Scrape healthcare job listings from hziegler.com (Helen Ziegler & Associates).

Data source
-----------
Helen Ziegler and Associates (HZA) is a North-American recruitment agency that
places nurses, allied-health professionals and physicians with hospitals in
Saudi Arabia, the UAE and Canada. The site is a small hand-built static site
(~54 live postings) with no JSON API, no /robots.txt (404 -> nothing
disallowed) and no sitemap.

Two structured surfaces are used (master spec §1, preference #2/#3):

1. **Index pages** server-render the complete job directory, grouped by
   `<h2 id="jobs_<section>">` (nursing / allied-health-clinical-services /
   physicians) and `<h3>` role groups, with `<li><a href="/jobs/...">Title -
   City, Country</a></li>` cards:

       /                                     (all 54 jobs)
       /locations/saudi-arabia.html          (49)
       /locations/united-arab-emirates.html  (2)
       /locations/canada.html                (3)

   All four are scanned and merged on URL, so a layout change on one page
   still leaves the run with a full inventory.

2. **Detail pages** embed a schema.org **JobPosting JSON-LD** block carrying
   `title`, `datePosted` (real ISO date), `employmentType`, `jobLocation`
   and an HTML `description` — this is the authoritative record. The visible
   left column additionally holds the employer intro paragraph that the
   JSON-LD description omits, and the right column's first `featureBox`
   names the hiring hospital (`/employers/<slug>.html`) plus its blurb.

Quirks
------
* **No salary anywhere** on the site (agency practice) -> `salary_raw =
  "Not Disclosed"`, numeric salary fields empty. Postings do list benefits
  ("Tax-free income", housing, airfare) and those stay in the description.
* `hiringOrganization` in the JSON-LD is always "Helen Ziegler and
  Associates" (the recruiter). The real employer comes from the right-column
  employer box; confidential mandates name a location instead of a hospital
  ("Riyadh", "Canada (Confidential)") and are relabelled
  `CONFIDENTIAL_COMPANY`.
* Every posting on the site is a clinical/healthcare role, so the healthcare
  filter is a classification step, not an exclusion step; a title with no
  healthcare signal at all is still KEPT and flagged `needs_review`.
* HTTP `Last-Modified` is a site-wide rebuild timestamp (identical on every
  page) and is deliberately NOT used as a date — `datePosted` is.
* Index links have no dates, so the time window can only be applied after a
  detail fetch; out-of-window jobs are recorded in `seen_old_ids.csv` so
  later runs skip re-fetching them.

Time window (master spec §4): an agency's live inventory keeps mandates open
for years (dates here range from 2022 to this month), so like the ATS-backed
scrapers the FIRST run keeps every open job (`INITIAL_WINDOW_DAYS = None`);
later runs keep only jobs newer than the newest stored `posted_date` minus
`WATERMARK_GRACE_DAYS`.

Outputs
-------
* hziegler_jobs.csv                          — rich cumulative store
                                               (dedup key: job_id = URL slug)
* ../../jobs_csv/<DD-MM-YYYY>/hziegler.csv   — HealthCareers.club 22-col schema
* needs_review.csv                           — titles with no healthcare signal
* seen_old_ids.csv                           — out-of-window slugs (skip list)

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

SITE = "hziegler"
SITE_BASE = "https://www.hziegler.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"

# Index surfaces, most complete first; merged on job URL.
LISTING_URLS = [
    SITE_BASE + "/",
    SITE_BASE + "/locations/saudi-arabia.html",
    SITE_BASE + "/locations/united-arab-emirates.html",
    SITE_BASE + "/locations/canada.html",
]

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec §3): salaries are captured, never filtered on.
# None = keep every open job on the first run (agency inventory, not a feed).
INITIAL_WINDOW_DAYS = None
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 4_000
COMPANY_ABOUT_MAX_CHARS = 500

RECRUITER_NAME = "Helen Ziegler and Associates"
CONFIDENTIAL_COMPANY = "Confidential Client (via Helen Ziegler & Associates)"

BASE_DIR = Path(__file__).resolve().parent
RICH_CSV = BASE_DIR / "hziegler_jobs.csv"
REVIEW_CSV = BASE_DIR / "needs_review.csv"
SEEN_OLD_CSV = BASE_DIR / "seen_old_ids.csv"
CLUB_CSV_DIR = BASE_DIR.parents[1] / "jobs_csv"

# addressCountry (JSON-LD) -> (country_name, iso2, dial code)
COUNTRY_META = {
    "saudi arabia": ("Saudi Arabia", "SA", "+966"),
    "united arab emirates": ("United Arab Emirates", "AE", "+971"),
    "uae": ("United Arab Emirates", "AE", "+971"),
    "canada": ("Canada", "CA", "+1"),
    "united states": ("United States", "US", "+1"),
    "usa": ("United States", "US", "+1"),
}
# Trailing tokens in "City, Region, Country" strings that are country names.
_COUNTRY_TOKENS = set(COUNTRY_META) | {"ksa"}

# HZA's own section slugs -> club category enum (fallback only; the title
# classifier below wins, because e.g. "Psychologist" is filed under physicians).
SECTION_CATEGORY = {
    "nursing": "nurses",
    "physicians": "doctors",
    "allied-health-clinical-services": "non_clinical",
}

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_about", "employer_url",
    "is_confidential", "city", "state", "country", "country_code",
    "country_dial_code", "salary_raw", "salary_min", "salary_max",
    "salary_period", "salary_currency", "job_type", "site_section",
    "role_group", "category", "company_type", "needs_review",
    "min_experience", "max_experience", "posted_date", "description",
    "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("hziegler_scraper")

# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    """HTML fragment -> flat text (comments dropped, tags become spaces)."""
    return clean_text(_TAG_RE.sub(" ", _COMMENT_RE.sub(" ", markup or "")))


# ----------------------------------------------------------------------------
# Index parsing
# ----------------------------------------------------------------------------

_SECTION_SPLIT_RE = re.compile(r'<h2 id="jobs_([a-z0-9-]+)"\s*>(.*?)</h2>', re.S)
_ENTRY_RE = re.compile(
    r'<h3>(?P<group>.*?)</h3>'
    r'|<li><a href="(?P<url>/jobs/[^"]+)">(?P<label>.*?)</a></li>', re.S)
_SLUG_RE = re.compile(r"/jobs/(.+?)\.html?$", re.IGNORECASE)


def job_id_from_url(path):
    """URL path -> dedup key (the job slug), e.g. 'rn-nicu--king-faisal-medina'."""
    m = _SLUG_RE.search((path or "").split("?")[0])
    return m.group(1) if m else clean_text(path)


def parse_index(page_html):
    """An index page -> list of card dicts (may be empty).

    Cards carry only what the directory shows: URL, "Title - City, Country"
    label, the HZA section and the <h3> role group they sit under.
    """
    cards = []
    parts = _SECTION_SPLIT_RE.split(page_html or "")
    # parts = [preamble, slug1, heading1, body1, slug2, heading2, body2, ...]
    for i in range(1, len(parts) - 2, 3):
        section, body = parts[i], parts[i + 2]
        group = ""
        for m in _ENTRY_RE.finditer(body):
            if m.group("group") is not None:
                group = strip_html(m.group("group"))
                continue
            path = m.group("url")
            label = clean_text(m.group("label"))
            cards.append({
                "job_id": job_id_from_url(path),
                "job_url": SITE_BASE + path,
                "label": label,
                "label_location": label.split(" - ")[-1] if " - " in label else "",
                "site_section": section,
                "role_group": group,
            })
    return cards


# ----------------------------------------------------------------------------
# Detail-page parsing
# ----------------------------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S)
_LAX_DECODER = json.JSONDecoder(strict=False)

_LEFT_COL_RE = re.compile(r"left-column'>(.*?)id=\"applySection\"", re.S)
_MARKDOWN_RE = re.compile(
    r'<div class="markdown markdown-body">(.*?)</div>', re.S)
_TRAILING_MORE_RE = re.compile(r"\s*\bmore\s*$", re.IGNORECASE)
_EMPLOYER_BOX_RE = re.compile(
    r"right-column'>.*?<div class=\"featureBox[^\"]*\"\s*>\s*"
    r"<h1>(?:<a href=\"(?P<url>/employers/[^\"]+)\">)?(?P<name>.*?)"
    r"(?:</a>)?</h1>(?P<body>.*?)</div>", re.S)


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


def parse_page_description(page_html):
    """Left-column markdown blocks -> flat text.

    Preferred over the JSON-LD description because the page also carries the
    employer intro paragraph ("KFSH&RC is a modern, 1,300+ bed ... facility")
    that the JSON-LD copy starts after. Returns "" when nothing is found.
    """
    left = _LEFT_COL_RE.search(page_html or "")
    if not left:
        return ""
    blocks = [strip_html(b) for b in _MARKDOWN_RE.findall(left.group(1))]
    return clean_text(" ".join(b for b in blocks if b))


def parse_employer(page_html):
    """Right-column employer box -> (name, about, employer_url).

    The first featureBox of a job page is the hiring employer; confidential
    mandates put a location name there ("Riyadh", "Canada (Confidential)")
    instead of a hospital.
    """
    m = _EMPLOYER_BOX_RE.search(page_html or "")
    if not m:
        return "", "", ""
    name = strip_html(m.group("name"))
    # The box ends with a " more" link back to the employer page; drop only
    # that trailing token (the blurbs themselves say "more than 1,000 beds").
    about = _TRAILING_MORE_RE.sub("", strip_html(m.group("body"))).strip()
    url = m.group("url") or ""
    return name, about[:COMPANY_ABOUT_MAX_CHARS], (SITE_BASE + url if url else "")


def resolve_company(employer_name, city, country):
    """(company_name, is_confidential) for the club schema.

    HZA names the location instead of the hospital when the client is
    confidential, so an employer whose name is just the job's city/country
    (or that says so outright) is reported as a confidential mandate.
    """
    name = clean_text(employer_name)
    if not name:
        return CONFIDENTIAL_COMPANY, True
    lowered = name.lower()
    if "confidential" in lowered:
        return CONFIDENTIAL_COMPANY, True
    location_names = {clean_text(city).lower(), clean_text(country).lower()}
    location_names.discard("")
    if lowered in location_names:
        return CONFIDENTIAL_COMPANY, True
    return name, False


# ---- location ---------------------------------------------------------------

def location_meta(locality, country_hint="", label_location=""):
    """JSON-LD address -> (city, state, country, iso2, dial).

    Handles "Riyadh, Saudi Arabia", "Barrie, Ontario, Canada",
    "Abu Dhabi, UAE" and the occasional empty locality (falls back to the
    index label's "Title - <location>" suffix).
    """
    raw = clean_text(locality) or clean_text(label_location)
    parts = [p for p in (clean_text(p) for p in raw.split(",")) if p]
    country_name, code, dial = "", "", ""
    if parts and parts[-1].lower() in _COUNTRY_TOKENS:
        country_name, code, dial = COUNTRY_META.get(
            parts[-1].lower(), (parts[-1], "", ""))
        parts = parts[:-1]
    hint = clean_text(country_hint).lower()
    if not country_name and hint:
        country_name, code, dial = COUNTRY_META.get(
            hint, (clean_text(country_hint), "", ""))
    city = parts[0] if parts else ""
    state = parts[1] if len(parts) > 1 else ""
    if city and country_name and city.lower() == country_name.lower():
        city = ""  # "Family Physician - Canada": country only, no city
    return city, state, country_name, code, dial


# ---- classification ---------------------------------------------------------

_NURSE_RE = re.compile(
    r"\bnurs\w*|midwif|\brn\b|\bnp\b|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"physician|surgeon|\bdoctor\b|\bmbbs\b|dentist|medical officer|"
    r"consultant|chairman|resident|registrar|intensivist|hospitalist|"
    r"anesthesiologist|anaesthetist|obstetrician|p(a?)ediatrician|"
    r"psychiatrist|[a-z]+ologist", re.IGNORECASE)
# Allied-health roles map to the club's non_clinical bucket; checked before
# _DOCTOR_RE's broad "-ologist" catch-all would claim e.g. Psychologist.
_ALLIED_RE = re.compile(
    r"therapist|technologist|technician|psycholog|dietit|dietician|"
    r"nutritionist|radiograph|sonograph|paramedic|audiolog|optometrist|"
    r"physiotherap|respiratory therap|social worker", re.IGNORECASE)

_HEALTHCARE_SIGNAL_RE = re.compile(
    r"nurs|\brn\b|medical|medicine|health|doctor|physician|surgeo|surgic|"
    r"clinic|hospital|patient|pharma|dental|therap|radiol|patholog|"
    r"laborator|\blab\b|diagnost|oncolog|cardio|p(a?)ediatric|psych|"
    r"icu\b|\ber\b|emergency|midwif|anesth|anaesth|transplant|"
    r"[a-z]+ologist", re.IGNORECASE)


def classify_category(title, section="", role_group=""):
    """-> (club category enum, needs_review).

    Title first, section second: HZA files "Psychologist" under PHYSICIANS
    and allied roles occasionally under NURSING, so the section is only a
    fallback for titles the regexes cannot place.
    """
    title = title or ""
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title):
        category = "pharmacists"
    elif _ALLIED_RE.search(title):
        category = "non_clinical"
    elif _DOCTOR_RE.search(title):
        category = "doctors"
    else:
        category = SECTION_CATEGORY.get(section, "non_clinical")
    haystack = " ".join(filter(None, [title, section, role_group]))
    needs_review = not _HEALTHCARE_SIGNAL_RE.search(haystack)
    return category, needs_review


_PHARMA_RE = re.compile(
    r"pharma|life ?science|biotech|\bcro\b|clinical research|laborator|"
    r"diagnostic centre|med.?tech|medical device", re.IGNORECASE)


def classify_company_type(company, description=""):
    """Club enum hospital|pharma. HZA places into hospitals almost without
    exception; the pharma regex only catches the rare lab/pharma mandate."""
    return "pharma" if _PHARMA_RE.search(company or "") else (
        "pharma" if _PHARMA_RE.search((description or "")[:200]) else "hospital")


# ---- experience -------------------------------------------------------------

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20,
}
_MIN_EXP_RE = re.compile(
    r"minimum(?:\s+of)?\s+(?P<n>\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")\s*\+?\s*"
    r"(?:\(\d+\)\s*)?years?", re.IGNORECASE)


def parse_min_experience(description):
    """Years of experience required, from the requirements prose.

    Real examples: "Must have a minimum of two years current experience as an
    RN" -> 2; "Minimum three years experience as a registered PT" -> 3;
    "A minimum of 10 years of experience as a Consultant" -> 10 (the first
    match wins; later "minimum of four years administrative" is secondary).
    Returns "" when the posting states no requirement — never a guess.
    """
    m = _MIN_EXP_RE.search(description or "")
    if not m:
        return ""
    token = m.group("n").lower()
    years = _WORD_NUMBERS.get(token)
    if years is None:
        try:
            years = int(token)
        except ValueError:
            return ""
    return str(years) if 0 < years <= 40 else ""


def job_type_from(employment_type):
    value = clean_text(employment_type).upper().replace(" ", "_")
    if value in ("PART_TIME", "PARTTIME"):
        return "part_time"
    if value in ("CONTRACTOR", "TEMPORARY", "CONTRACT"):
        return "contract"
    return "full_time"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    """Watermark cutoff (ISO date) or "" when everything open should be kept."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max()
                    - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    if INITIAL_WINDOW_DAYS is None:
        return ""
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def check_robots(session):
    """Honor robots.txt per-URL. hziegler.com serves a 404 (no rules)."""
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in LISTING_URLS + [SITE_BASE + "/jobs/example-job.html"]:
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, url):
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
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(card, posting, page_html):
    """Rich row from an index card + its detail page."""
    title = clean_text(posting.get("title")) or card["label"].split(" - ")[0]
    address = (posting.get("jobLocation") or {}).get("address") or {}
    city, state, country, code, dial = location_meta(
        address.get("addressLocality"), address.get("addressCountry"),
        card["label_location"])

    employer_name, employer_about, employer_url = parse_employer(page_html)
    company, is_confidential = resolve_company(employer_name, city, country)

    description = (parse_page_description(page_html)
                   or strip_html(posting.get("description")))
    category, needs_review = classify_category(
        title, card["site_section"], card["role_group"])

    return {
        "source": SITE,
        "job_id": card["job_id"],
        "title": title,
        "company": company,
        "company_about": employer_about,
        "employer_url": employer_url,
        "is_confidential": is_confidential,
        "city": city,
        "state": state,
        "country": country,
        "country_code": code,
        "country_dial_code": dial,
        # The site publishes no pay for any posting (master spec §3).
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": job_type_from(posting.get("employmentType")),
        "site_section": card["site_section"],
        "role_group": card["role_group"],
        "category": category,
        "company_type": classify_company_type(company, description),
        "needs_review": needs_review,
        "min_experience": parse_min_experience(description),
        "max_experience": "",
        "posted_date": clean_text(posting.get("datePosted"))[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": card["job_url"],
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def rich_row_to_club_row(r):
    return {
        "country_name": _blank(r.get("country")),
        "country_code": _blank(r.get("country_code")),
        "country_dial_code": _blank(r.get("country_dial_code")),
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
        "company_name": _blank(r.get("company")) or CONFIDENTIAL_COMPANY,
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": _blank(r.get("company_about")),
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")) or "non_clinical",
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _blank(r.get("min_experience")),
        "max_experience": _blank(r.get("max_experience")),
        # hziegler.com discloses no pay — these stay empty by design.
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        "expires_at": "",
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


def collect_cards(session, listing_urls):
    """Merge the index pages into one URL-keyed card list (first page wins)."""
    cards, seen = [], set()
    for url in listing_urls:
        page_html = _request(session, url)
        if not page_html:
            log.warning("Index page unavailable: %s", url)
            continue
        found = parse_index(page_html)
        log.info("%s -> %d job links", url, len(found))
        for card in found:
            if card["job_id"] in seen:
                continue
            seen.add(card["job_id"])
            cards.append(card)
    return cards


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
        description="Scrape healthcare jobs from hziegler.com.")
    parser.add_argument("--output", default=str(RICH_CSV),
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new detail pages (test runs)")
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
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    seen_old = load_seen_old(SEEN_OLD_CSV)
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs (%d known-old skipped); %s",
             len(known_ids), len(seen_old),
             "keeping jobs posted on/after {}".format(cutoff) if cutoff
             else "first run: keeping every open job")

    cards = collect_cards(session, LISTING_URLS)
    if not cards:
        log.error("No job links found on any index page — site layout changed?")

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0, "detail_failed": 0}
    new_rows, review_log, new_seen_old = [], [], []

    for card in cards:
        counters["scanned"] += 1
        if card["job_id"] in known_ids or card["job_id"] in seen_old:
            counters["duplicates"] += 1
            continue
        if args.limit is not None and counters["new"] >= args.limit:
            log.info("--limit %d reached; stopping detail fetches", args.limit)
            break
        page_html = _request(session, card["job_url"])
        if not page_html:
            counters["detail_failed"] += 1
            continue
        posting = parse_job_posting(page_html)
        if not posting:
            # Not fatal: the index label + page text still carry the essentials.
            log.warning("No JobPosting JSON-LD for %s", card["job_url"])
        try:
            row = build_row(card, posting, page_html)
        except Exception as exc:            # one bad page must not kill the run
            counters["detail_failed"] += 1
            log.warning("Skipping malformed job %s: %s", card["job_id"], exc)
            continue
        if cutoff and row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            seen_old.add(row["job_id"])
            new_seen_old.append({"job_id": row["job_id"],
                                 "posted_date": row["posted_date"]})
            continue
        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "site_section": row["site_section"],
                               "job_url": row["job_url"]})
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
        combined = combined[RICH_COLUMNS]
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

    # ---- sidecars ----
    if new_seen_old:
        old_df = pd.DataFrame(new_seen_old)
        try:
            old_df = pd.concat([pd.read_csv(SEEN_OLD_CSV, dtype=str), old_df],
                               ignore_index=True)
        except FileNotFoundError:
            pass
        old_df.drop_duplicates(subset="job_id").to_csv(SEEN_OLD_CSV, index=False)
    if review_log:
        review_df = pd.DataFrame(review_log)
        try:
            review_df = pd.concat([pd.read_csv(REVIEW_CSV, dtype=str), review_df],
                                  ignore_index=True)
        except FileNotFoundError:
            pass
        review_df.drop_duplicates(subset="job_id").to_csv(REVIEW_CSV, index=False)

    print("\n===== Run summary =====")
    print("Jobs scanned:          {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(
        cutoff or "n/a", counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))
    print("Detail fetch failures: {:>5,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
