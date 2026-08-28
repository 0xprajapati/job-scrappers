#!/usr/bin/env python3
"""Scrape Jhpiego (Johns Hopkins global-health NGO) vacancies from
jobs-jhpiego.icims.com.

Data source
-----------
Jhpiego runs its careers board on the **iCIMS** ATS. robots.txt advertises
the sitemap and allows /jobs/* detail paths while disallowing the
referral/login/candidate/reminder and /connect paths — which this scraper
never touches (fetch() hard-asserts that).

Discovery is the sitemap (65 job URLs on the 2026-08-28 probe):

    https://jobs-jhpiego.icims.com/sitemap.xml
        -> /jobs/<numeric id>/<title-slug>/job

The numeric id is the stable job id; ids are sequential, so descending id
order ~= newest posting first. The sitemap's <lastmod> is a
re-crawl/modification stamp, NOT the posting date (the iconplc/Attrax
lesson holds on iCIMS too: a 2024 posting carries a 2024 lastmod but
recent rows are re-stamped) — it is ignored. The non-job /jobs/intro entry
never matches the id pattern.

Every detail page, fetched with ?in_iframe=1 (probe-verified: the iframe
variant serves the full page without the chrome), embeds ONE schema.org
JobPosting JSON-LD block with the exact `datePosted`, clean `title`, full
HTML `description`, an `employmentType` scalar ("OTHER" on every probed
page), an `occupationalCategory` carrying Jhpiego's hire-type tag
("Local"), and a `jobLocation` LIST of Places whose address has a REAL ISO
alpha-2 `addressCountry` plus an `addressLocality` city — with the string
"UNAVAILABLE" as iCIMS's placeholder in unused fields (streetAddress,
addressRegion, postalCode). Parsing uses raw_decode(strict=False) (the
iconplc/devnetjobsindia hygiene) so a stray trailing brace or raw control
character can never kill a run.

Verified quirks (probe 2026-08-28)
----------------------------------
* posted_date exists ONLY in the detail JSON-LD, so a new id's detail must
  be fetched before the cutoff can be judged. Out-of-window ids are
  remembered in seen_old_ids.csv and dropped ids in out-of-scope.csv (full
  rows, reversible) — the iconplc idiom — so each id is fetched at most
  once, ever. Steady state ≈ 1 sitemap request + 1 detail request per
  genuinely new posting.
* `validThrough` is EXACTLY datePosted + 1 year on every probed page — an
  iCIMS auto-stamp, not a real application deadline. Captured verbatim in
  the rich CSV, but treated as synthetic (documented here, never presented
  as a deadline).
* `addressCountry` is a real ISO alpha-2 code (IN, CI, GT, ...) — mapped
  through the fleet-vetted ISO table, never guessed for an unknown code.
  `addressLocality` is the city (sometimes a state name like "Gujrat" —
  exported as-is).
* Jhpiego's country offices post in FRENCH and SPANISH (francophone
  Africa, Latin America) — ~20 of the 65 sitemap slugs on the probe. Any
  non-English description is kept AND flagged needs_review via the undp
  stopword heuristic (looks_non_english) rather than published
  untranslated. The classifier still rules on keep/drop.
* `occupationalCategory` ("Local") is a hire-type tag, not a role signal —
  captured in the rich CSV (hire_type) and NOT passed to the classifier
  (title + description only; the skills-veto-is-per-board rule: nothing
  useful, nothing misleading, so nothing is passed).
* No baseSalary on any probed page; the schema.org shape is still parsed
  when present. Absent salary -> "Not Disclosed", blank club salary
  columns. Only USD/INR annual|monthly pay can be represented by the club
  enums; other currencies/periods stay rich-CSV-only.
* 403/404 are treated as permanent — never retried (politeness).

Classification (shared taxonomy)
--------------------------------
Title-only in-scope measured at just 6% on the probe sweep, but that
undercounts — the descriptions carry the health signal, so every new id's
description is fetched and the keep/drop decision belongs to the ONE
shared classifier, _shared/classification.classify_job(title, "",
description). Classifier inputs are ACCENT-FOLDED first
(fold_diacritics) so francophone titles like "Surveillance
Épidémiologique" can reach the English keyword engine — pure
normalization, the vocabulary stays the classifier's. in_scope False
(finance, HR, procurement, drivers...) -> DROPPED, counted
excluded_out_of_scope, full row appended to out-of-scope.csv.
needs_review True keeps the row AND appends it to needs_review.csv.

KNOWN LIMIT: the current shared engine requires Public Health to match in
the title or skills (FAMILY_REQUIRE_TITLE_OR_SKILLS), and this board has
no curated skills signal — so description-only PH rows (e.g. "Senior
Program Officer -TB", "Program Officer - CPHC") DROP under today's
_shared code even though the 2026-08-27 scope ruling
(taxonomy-classifier-decisions) reads them as in scope on NGO boards.
They land, full and reversible, in out-of-scope.csv; if _shared ever
grows the ruled description-only-PH path for NGO boards, delete
out-of-scope.csv and re-run with --since to re-adjudicate.

Outputs
-------
* jhpiego_jobs.csv — rich cumulative store (dedup key: job_id), source of
  truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/jhpiego.csv — the same jobs mapped to the
  HealthCareers.club CLUB_COLUMNS schema.
* seen_old_ids.csv — out-of-window ids (detail-fetch skip list).
* out-of-scope.csv — dropped rows + their skip list (reversible).
* needs_review.csv — kept but flagged.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(30 — NGO postings are long-lived); later runs keep only jobs newer than
the newest stored posted_date minus WATERMARK_GRACE_DAYS. `--since` widens
the window for backlog sweeps.

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
import unicodedata
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# The one shared classifier (see ../../instructions/master-scraper-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "jhpiego"
COMPANY_NAME = "Jhpiego"
SITE_BASE = "https://jobs-jhpiego.icims.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SITEMAP_URL = SITE_BASE + "/sitemap.xml"

# The iframe variant serves the full posting without the portal chrome
# (probe-verified 2026-08-28, HTTP 200 with the JobPosting JSON-LD).
DETAIL_SUFFIX = "?in_iframe=1"

# robots.txt disallows these /jobs/ sub-paths (referral/login/candidate/
# reminder) and /connect — never requested, and fetch() hard-asserts it.
FORBIDDEN_PATH_TOKENS = ("referral", "login", "candidate", "reminder",
                         "/connect")

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# NGO postings stay open for months; a wider first-run window seeds a
# usable corpus. No salary filter (master spec): captured, never filtered.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "jhpiego_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "country",
    "country_code", "location_raw", "locations_all", "hire_type",
    "employment_type_raw", "salary_raw", "salary_min", "salary_max",
    "salary_currency", "salary_period", "category", "sub_category",
    "role_family", "all_families", "family_scores", "family_confidence",
    "matched_in", "vetoed_by", "needs_review", "experience_min_years",
    "posted_date", "valid_through", "description", "job_url", "scraped_at",
]

log = logging.getLogger("jhpiego_scraper")

# ISO alpha-2 -> (country name, dial code): the fleet-vetted table from
# ../undp/ (itself inverted from ../impactpool/). Jhpiego's footprint is
# heavily African/Asian country offices plus the Baltimore HQ. An unknown
# code exports blank name/dial — never guessed.
COUNTRY_BY_CODE = {
    "AE": ("United Arab Emirates", "+971"), "AF": ("Afghanistan", "+93"),
    "AL": ("Albania", "+355"), "AM": ("Armenia", "+374"),
    "AO": ("Angola", "+244"), "AR": ("Argentina", "+54"),
    "AT": ("Austria", "+43"), "AU": ("Australia", "+61"),
    "AZ": ("Azerbaijan", "+994"), "BA": ("Bosnia and Herzegovina", "+387"),
    "BB": ("Barbados", "+1"), "BD": ("Bangladesh", "+880"),
    "BE": ("Belgium", "+32"), "BF": ("Burkina Faso", "+226"),
    "BG": ("Bulgaria", "+359"), "BI": ("Burundi", "+257"),
    "BJ": ("Benin", "+229"), "BO": ("Bolivia", "+591"),
    "BR": ("Brazil", "+55"), "BW": ("Botswana", "+267"),
    "CA": ("Canada", "+1"),
    "CD": ("Democratic Republic of the Congo", "+243"),
    "CF": ("Central African Republic", "+236"), "CG": ("Congo", "+242"),
    "CH": ("Switzerland", "+41"), "CI": ("Côte d'Ivoire", "+225"),
    "CL": ("Chile", "+56"), "CM": ("Cameroon", "+237"),
    "CN": ("China", "+86"), "CO": ("Colombia", "+57"),
    "CR": ("Costa Rica", "+506"), "CU": ("Cuba", "+53"),
    "CV": ("Cabo Verde", "+238"), "CY": ("Cyprus", "+357"),
    "CZ": ("Czechia", "+420"), "DE": ("Germany", "+49"),
    "DJ": ("Djibouti", "+253"), "DK": ("Denmark", "+45"),
    "DO": ("Dominican Republic", "+1"), "DZ": ("Algeria", "+213"),
    "EC": ("Ecuador", "+593"), "EE": ("Estonia", "+372"),
    "EG": ("Egypt", "+20"), "ER": ("Eritrea", "+291"), "ES": ("Spain", "+34"),
    "ET": ("Ethiopia", "+251"), "FI": ("Finland", "+358"),
    "FJ": ("Fiji", "+679"), "FR": ("France", "+33"), "GA": ("Gabon", "+241"),
    "GB": ("United Kingdom", "+44"), "GE": ("Georgia", "+995"),
    "GH": ("Ghana", "+233"), "GM": ("Gambia", "+220"),
    "GN": ("Guinea", "+224"), "GR": ("Greece", "+30"),
    "GT": ("Guatemala", "+502"), "GW": ("Guinea-Bissau", "+245"),
    "HK": ("Hong Kong", "+852"), "HN": ("Honduras", "+504"),
    "HR": ("Croatia", "+385"), "HT": ("Haiti", "+509"),
    "HU": ("Hungary", "+36"), "ID": ("Indonesia", "+62"),
    "IE": ("Ireland", "+353"), "IL": ("Israel", "+972"),
    "IN": ("India", "+91"), "IQ": ("Iraq", "+964"), "IR": ("Iran", "+98"),
    "IT": ("Italy", "+39"), "JM": ("Jamaica", "+1"), "JO": ("Jordan", "+962"),
    "JP": ("Japan", "+81"), "KE": ("Kenya", "+254"),
    "KG": ("Kyrgyzstan", "+996"), "KH": ("Cambodia", "+855"),
    "KI": ("Kiribati", "+686"), "KR": ("South Korea", "+82"),
    "KW": ("Kuwait", "+965"), "KZ": ("Kazakhstan", "+7"),
    "LA": ("Laos", "+856"), "LB": ("Lebanon", "+961"),
    "LK": ("Sri Lanka", "+94"), "LR": ("Liberia", "+231"),
    "LS": ("Lesotho", "+266"), "LT": ("Lithuania", "+370"),
    "LU": ("Luxembourg", "+352"), "LV": ("Latvia", "+371"),
    "LY": ("Libya", "+218"), "MA": ("Morocco", "+212"),
    "MD": ("Moldova", "+373"), "ME": ("Montenegro", "+382"),
    "MG": ("Madagascar", "+261"), "MK": ("North Macedonia", "+389"),
    "ML": ("Mali", "+223"), "MM": ("Myanmar", "+95"),
    "MN": ("Mongolia", "+976"), "MO": ("Macao", "+853"),
    "MR": ("Mauritania", "+222"), "MT": ("Malta", "+356"),
    "MW": ("Malawi", "+265"), "MX": ("Mexico", "+52"),
    "MY": ("Malaysia", "+60"), "MZ": ("Mozambique", "+258"),
    "NA": ("Namibia", "+264"), "NE": ("Niger", "+227"),
    "NG": ("Nigeria", "+234"), "NI": ("Nicaragua", "+505"),
    "NL": ("Netherlands", "+31"), "NO": ("Norway", "+47"),
    "NP": ("Nepal", "+977"), "NZ": ("New Zealand", "+64"),
    "OM": ("Oman", "+968"), "PA": ("Panama", "+507"), "PE": ("Peru", "+51"),
    "PG": ("Papua New Guinea", "+675"), "PH": ("Philippines", "+63"),
    "PK": ("Pakistan", "+92"), "PL": ("Poland", "+48"),
    "PS": ("Palestine", "+970"), "PT": ("Portugal", "+351"),
    "PY": ("Paraguay", "+595"), "QA": ("Qatar", "+974"),
    "RO": ("Romania", "+40"), "RS": ("Serbia", "+381"),
    "RU": ("Russia", "+7"), "RW": ("Rwanda", "+250"),
    "SA": ("Saudi Arabia", "+966"), "SB": ("Solomon Islands", "+677"),
    "SD": ("Sudan", "+249"), "SE": ("Sweden", "+46"),
    "SG": ("Singapore", "+65"), "SI": ("Slovenia", "+386"),
    "SK": ("Slovakia", "+421"), "SL": ("Sierra Leone", "+232"),
    "SN": ("Senegal", "+221"), "SO": ("Somalia", "+252"),
    "SS": ("South Sudan", "+211"), "SV": ("El Salvador", "+503"),
    "SY": ("Syria", "+963"), "SZ": ("Eswatini", "+268"),
    "TD": ("Chad", "+235"), "TG": ("Togo", "+228"), "TH": ("Thailand", "+66"),
    "TJ": ("Tajikistan", "+992"), "TL": ("Timor-Leste", "+670"),
    "TN": ("Tunisia", "+216"), "TR": ("Turkey", "+90"),
    "TW": ("Taiwan", "+886"), "TZ": ("Tanzania", "+255"),
    "UA": ("Ukraine", "+380"), "UG": ("Uganda", "+256"),
    "US": ("United States", "+1"), "UY": ("Uruguay", "+598"),
    "UZ": ("Uzbekistan", "+998"), "VE": ("Venezuela", "+58"),
    "VN": ("Vietnam", "+84"), "VU": ("Vanuatu", "+678"),
    "YE": ("Yemen", "+967"), "ZA": ("South Africa", "+27"),
    "ZM": ("Zambia", "+260"), "ZW": ("Zimbabwe", "+263"),
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
                    markup or "", flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


def _unavailable(value):
    """iCIMS writes the literal string "UNAVAILABLE" in unused address
    fields — treat it as blank."""
    text = clean_text(value)
    return "" if text.upper() == "UNAVAILABLE" else text


# ---- sitemap ----------------------------------------------------------------

_SITEMAP_JOB_RE = re.compile(
    r"<loc>\s*(https://jobs-jhpiego\.icims\.com/jobs/(\d+)/[^<]*?/job)\s*</loc>")


def parse_sitemap_jobs(xml):
    """(url, id) pairs from sitemap.xml, highest id first.

    <lastmod> is a modification stamp, not the posting date — ignored.
    ids are sequential, so descending id ~= newest posting first. The
    non-job /jobs/intro entry never matches the pattern.
    """
    seen = {}
    for url, job_id in _SITEMAP_JOB_RE.findall(xml or ""):
        seen[job_id] = url
    return [(seen[job_id], job_id)
            for job_id in sorted(seen, key=int, reverse=True)]


# ---- detail page ------------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S)
# Jhpiego's blocks parsed clean on every probe, but iCIMS gets defensive
# parsing anyway (the iconplc/devnetjobsindia hygiene): raw_decode stops at
# the end of the first JSON value; strict=False tolerates raw control
# characters inside strings.
_LAX_DECODER = json.JSONDecoder(strict=False)


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


def parse_iso_date(text):
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


# The JSON-LD employmentType is the literal "OTHER" on every probed page;
# the page's own header table carries the real value ("Employment Status:
# Full-Time" as a <dt>/<dd> pair) — parsed as a grounded supplement.
_EMPLOYMENT_STATUS_RE = re.compile(
    r"<dt[^>]*>\s*Employment\s+Status\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
    re.I | re.S)


def parse_employment_status(page_html):
    """The header table's Employment Status value ("Full-Time"), or ""."""
    match = _EMPLOYMENT_STATUS_RE.search(page_html or "")
    return strip_html(match.group(1)) if match else ""


def listify(value):
    """JSON-LD fields that are sometimes scalar, sometimes list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_locations(posting):
    """[(country_code, city), ...] from the JSON-LD jobLocation list.

    addressCountry is a real ISO alpha-2 code on this board;
    addressLocality is the city; the "UNAVAILABLE" placeholders in
    streetAddress/addressRegion/postalCode are never read as data.
    """
    out = []
    for place in listify(posting.get("jobLocation")):
        if not isinstance(place, dict):
            continue
        address = place.get("address") or {}
        code = _unavailable(address.get("addressCountry")).upper()
        city = _unavailable(address.get("addressLocality"))
        if code or city:
            out.append((code, city))
    return out


_SALARY_PERIODS = {"YEAR": "per_annum", "MONTH": "per_month",
                   "HOUR": "per_hour", "WEEK": "per_week", "DAY": "per_day"}


def parse_base_salary(base_salary):
    """schema.org baseSalary -> (raw, min, max, currency, period).

    Null on every probed Jhpiego page, but the standard shape is supported
    for when iCIMS starts emitting it. Absent -> ("Not Disclosed", "", "",
    "", ""); never invented.
    """
    if not isinstance(base_salary, dict):
        return "Not Disclosed", "", "", "", ""
    value = base_salary.get("value")
    if not isinstance(value, dict):
        value = base_salary
    try:
        lo = value.get("minValue", value.get("value"))
        hi = value.get("maxValue", lo)
        if lo is None:
            return "Not Disclosed", "", "", "", ""
        lo, hi = float(lo), float(hi if hi is not None else lo)
        if hi < lo:
            lo, hi = hi, lo
    except (TypeError, ValueError):
        return "Not Disclosed", "", "", "", ""
    currency = clean_text(base_salary.get("currency")).upper()
    unit = clean_text(value.get("unitText")).upper()
    period = _SALARY_PERIODS.get(unit, "")
    raw = "{} {:g} - {:g} {}".format(currency, lo, hi,
                                     ("per " + unit.lower()) if unit else "").strip()
    return raw, int(lo), int(hi), currency, period


# "Minimum 5 years...", "at least 3 years", "5+ years of program
# experience", "2-4 years' experience in M&E" — grounded extraction only,
# never inferred. Up to two words may sit between "years of" and
# "experience".
_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*"
               r"(?:\+|-\s*\d{1,2})?\s*years?", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
               r"(?:[A-Za-z/&-]+\s+){0,2}experience", re.IGNORECASE),
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


def job_type_from_employment(employment_types):
    """"OTHER" (every probed page) -> full_time; part-time -> part_time."""
    joined = " ".join(clean_text(t) for t in listify(employment_types)).lower()
    return "part_time" if "part" in joined else "full_time"


# ---- language ---------------------------------------------------------------

# Jhpiego's country offices post wholly in French and Spanish (francophone
# Africa, Latin America — ~20 of 65 sitemap slugs on the probe). Any
# non-English description is kept AND flagged needs_review rather than
# published untranslated — the undp keep-and-flag stance, with its cheap
# stopword heuristic: high-frequency FR/ES function words in the head of
# the text (English prose contains almost none of them standalone).
_NON_ENGLISH_STOPWORDS = (" le ", " la ", " les ", " des ", " une ", " pour ",
                          " dans ", " el ", " los ", " para ", " con ")
NON_ENGLISH_STOPWORD_THRESHOLD = 8
LANGUAGE_SNIFF_CHARS = 1000


def looks_non_english(description):
    """True when the description opens in French/Spanish, not English."""
    head = " " + clean_text(description)[:LANGUAGE_SNIFF_CHARS].lower() + " "
    hits = sum(head.count(word) for word in _NON_ENGLISH_STOPWORDS)
    return hits >= NON_ENGLISH_STOPWORD_THRESHOLD


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------

def fold_diacritics(text):
    """Strip accents so FR/ES text can reach the English keyword engine.

    "Surveillance Épidémiologique" -> "Surveillance Epidemiologique",
    which the shared `epidemiolog\\w*` keyword then matches. Pure
    normalization — the vocabulary and the verdict stay entirely the
    shared classifier's. Only the classifier input is folded; stored
    titles/descriptions keep their accents.
    """
    return "".join(ch for ch in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(ch))


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The classifier gets title + description only (accent-folded so
    francophone postings can match the shared keywords):
    occupationalCategory ("Local") is a hire-type tag with no role signal,
    so nothing is passed as `skills` (per-board skills rule — nothing
    useful, nothing misleading). Returns in_scope — False means DROP the
    row.
    """
    verdict = classify_job(fold_diacritics(row.get("title", "")), "",
                           fold_diacritics(row.get("description", "")))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["vetoed_by"] = verdict["vetoed_by"]
    # A FR/ES posting is kept but reviewed rather than silently published
    # untranslated (fleet convention, the undp precedent).
    row["needs_review"] = (verdict["needs_review"]
                           or looks_non_english(row.get("description", "")))
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
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml,application/xml"})
    return session


def check_robots(session):
    """Verify robots.txt still allows the sitemap and /jobs/<id>/... paths.

    The live file advertises the sitemap and disallows only the
    referral/login/candidate/reminder and /connect paths — which this
    scraper never requests (fetch() hard-asserts it).
    """
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (SITEMAP_URL,
                SITE_BASE + "/jobs/1/example-role/job",
                SITE_BASE + "/jobs/1/example-role/job" + DETAIL_SUFFIX):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed (sitemap + /jobs/<id>/ detail paths; "
             "referral/login/candidate/connect paths are disallowed and "
             "never requested)")


def fetch(session, url):
    """GET one page with retries/backoff. Returns text or None.

    4xx (403 included) is permanent — never retried. The robots-disallowed
    referral/login/candidate/reminder//connect paths are hard-asserted out.
    """
    assert not any(token in url for token in FORBIDDEN_PATH_TOKENS), \
        "robots-disallowed path: {}".format(url)
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

def build_row(job_id, posting, url, page_html=""):
    """Rich row from a detail page's JobPosting JSON-LD (plus the page's
    own Employment Status header field, which is more truthful than the
    JSON-LD's constant "OTHER")."""
    title = clean_text(posting.get("title"))

    locations = parse_locations(posting)
    code, city = locations[0] if locations else ("", "")
    country = COUNTRY_BY_CODE.get(code, ("", ""))[0]

    description = strip_html(posting.get("description") or "")[:DESCRIPTION_MAX_CHARS]

    org = posting.get("hiringOrganization")
    company = clean_text(org.get("name")) if isinstance(org, dict) else ""

    salary_raw, sal_min, sal_max, sal_ccy, sal_period = \
        parse_base_salary(posting.get("baseSalary"))

    return {
        "source": SITE,
        "job_id": str(job_id),
        "title": title,
        "company": company or COMPANY_NAME,
        "city": city,
        "country": country,
        "country_code": code if country else "",
        "location_raw": ("{}, {}".format(city, code).strip(", ")
                         if (city or code) else ""),
        "locations_all": "; ".join(
            "{}, {}".format(c, k).strip(", ") for k, c in locations),
        # Jhpiego's hire-type tag ("Local") — a rich-CSV source column,
        # never a classifier signal.
        "hire_type": clean_text(posting.get("occupationalCategory")),
        "employment_type_raw": "; ".join(
            t for t in ([clean_text(x)
                         for x in listify(posting.get("employmentType"))]
                        + [parse_employment_status(page_html)]) if t),
        "salary_raw": salary_raw,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": sal_ccy,
        "salary_period": sal_period,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "vetoed_by": "",
        "needs_review": False,
        "experience_min_years": parse_experience_years(description),
        "posted_date": parse_iso_date(posting.get("datePosted")),
        # datePosted + 1 year on every probed page — an iCIMS auto-stamp,
        # captured verbatim but documented as synthetic.
        "valid_through": parse_iso_date(posting.get("validThrough")),
        "description": description,
        "job_url": clean_text(posting.get("url")) or url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the club's 23-column schema.

    Jhpiego is a global-health NGO -> company_type "hospital" (the
    fleet-wide convention for non-pharma employers in the club's
    hospital|pharma enum). Only USD/INR annual|monthly salary is
    representable by the club enums; anything else stays rich-CSV-only
    (never converted).
    """
    currency = _blank(r.get("salary_currency"))
    period = _blank(r.get("salary_period"))
    lo, hi = _blank(r.get("salary_min")), _blank(r.get("salary_max"))
    exportable = (bool(lo) and currency in ("INR", "USD")
                  and period in ("per_month", "per_annum"))

    country = _blank(r.get("country"))
    code = _blank(r.get("country_code"))
    dial = COUNTRY_BY_CODE.get(code, ("", ""))[1]

    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": _blank(r.get("city")) or country,
        "company_name": _blank(r.get("company")) or COMPANY_NAME,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": job_type_from_employment(_blank(r.get("employment_type_raw"))),
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _blank(r.get("experience_min_years")),
        "max_experience": "",
        # no structured qualification field in the JSON-LD — grounded
        # extraction from the description only, never inferred
        "qualification": extract_qualification(_blank(r.get("description"))),
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
    for col in RICH_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df[RICH_COLUMNS].to_csv(path, index=False)
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
        description="Scrape Jhpiego (global-health NGO) jobs from "
                    "jobs-jhpiego.icims.com (sitemap + JSON-LD).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail pages (test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job "
                             "posted on/after this date (backlog sweeps)")
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
    jobs = parse_sitemap_jobs(sitemap_xml)
    log.info("Sitemap lists %d active postings", len(jobs))
    if not jobs:
        sys.exit("Sitemap yielded zero job URLs — page shape changed? Aborting.")

    counters = {"scanned": 0, "duplicates": 0, "skipped_old": 0,
                "skipped_out_of_scope": 0, "excluded_old": 0,
                "excluded_out_of_scope": 0, "needs_review": 0, "new": 0,
                "detail_failed": 0, "skipped_robots": 0}
    veto_counts = {}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0

    for url, job_id in jobs:                    # descending id ~= newest first
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
        # robots.txt wildcards (/jobs/*referral etc.) also match a slug
        # that merely CONTAINS the token — e.g. a hypothetical "referral
        # coordinator" posting. Such a URL is robots-disallowed, so it is
        # skipped loudly instead of tripping fetch()'s hard assert.
        if any(token in url for token in FORBIDDEN_PATH_TOKENS):
            counters["skipped_robots"] += 1
            log.warning("Skipping robots-disallowed URL for id=%s: %s",
                        job_id, url)
            continue

        detail_html = fetch(session, url + DETAIL_SUFFIX)
        fetched += 1
        posting = parse_job_posting(detail_html or "")
        if not posting:
            counters["detail_failed"] += 1
            log.warning("No JobPosting JSON-LD for id=%s", job_id)
            continue
        try:
            row = build_row(job_id, posting, url, detail_html or "")
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", job_id, exc)
            counters["detail_failed"] += 1
            continue

        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            seen_old.add(job_id)
            new_seen_old.append({"job_id": job_id,
                                 "posted_date": row["posted_date"]})
            continue

        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            if row.get("vetoed_by"):
                veto_counts[row["vetoed_by"]] = \
                    veto_counts.get(row["vetoed_by"], 0) + 1
            out_of_scope_ids.add(job_id)
            dropped_rows.append(row)
            continue

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "country": row["country"],
                               "hire_type": row["hire_type"],
                               "sub_category": row["sub_category"],
                               "role_family": row["role_family"]})
        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1
        if fetched % 25 == 0:
            log.info("...%d detail pages fetched, %d kept so far",
                     fetched, counters["new"])

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
    print("Detail pages fetched:         {:>6,}".format(fetched))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Skipped (known old):          {:>6,}".format(counters["skipped_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail parse failures:        {:>6,}".format(counters["detail_failed"]))
    if counters["skipped_robots"]:
        print("Skipped (robots-disallowed):  {:>6,}".format(counters["skipped_robots"]))
    if veto_counts:
        print("Vetoes among dropped rows:")
        for veto, n in sorted(veto_counts.items(), key=lambda kv: -kv[1]):
            print("    {:<28} {:>4,}".format(veto, n))


if __name__ == "__main__":
    main()
