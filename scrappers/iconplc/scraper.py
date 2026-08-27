#!/usr/bin/env python3
"""Scrape job listings from ICON plc's careers site (careers.iconplc.com).

Data source
-----------
ICON plc is a global CRO (clinical research organization) — its whole board
is CRO supply: CRAs, clinical trial management, clinical data management,
regulatory affairs, medical writing, pharmacovigilance, biostatistics. The
careers site runs on the Attrax ATS (probed 2026-08-27).

Discovery is the vacancies sitemap, which robots.txt advertises:

    https://careers.iconplc.com/vacanciessitemap.xml   (~880 job URLs)

Every entry is `/job/<title-slug>-jid-<numeric id>`; the jid is the stable
job id. The sitemap's <lastmod> is a re-crawl/modification stamp, NOT the
posting date (a Tokyo posting with datePosted 2025-08-25 carried lastmod
2026-08-25) — it is ignored. jids are sequential, so descending jid order
~= newest posting first.

robots.txt allows `/job/...` detail paths and names the sitemap, but
**disallows `/jobs?*`** — the faceted search pages. The crawl is therefore
SITEMAP ONLY: this scraper never touches a search URL, and fetch()
hard-asserts that.

Every detail page embeds ONE schema.org JobPosting JSON-LD block with the
exact `datePosted`/`validThrough`, clean `title`, full HTML `description`,
an `identifier` (the JR-prefixed requisition id), an `industry` list —
ICON's own department tag ("Clinical Operations", "Medical Writing",
"Regulatory Affairs", ...) — and a `jobLocation` LIST of Places whose only
address field is a combined `addressLocality` string. The blocks parsed
clean on every probe, but Attrax gets no benefit of the doubt: parsing uses
raw_decode(strict=False) (the devnetjobsindia hygiene) so a stray trailing
brace or raw control character can never kill a run.

Verified quirks
---------------
* `addressLocality` is the only location field and comes in several shapes:
  "India, Chennai" (Country, City), "United States of America" (country
  only), "California" (bare US state), "Chicago, IL" (US city + state
  abbrev), "US, Blue Bell (ICON)" / "Singapore, Singapore (Labs)" (site tag
  in parens), and "Regional United States (PRA)" (a home-based coverage
  region). parse_locality() handles each shape; "Regional ..." rows are
  marked remote and export city_name "Remote" (the himalayas convention).
  A bare "Georgia" is read as the US state, not the country — the country
  always appears as "Georgia, Tbilisi" (documented ambiguity).
* posted_date exists ONLY in the detail JSON-LD, so a new jid's detail must
  be fetched before the cutoff can be judged. Out-of-window jids are
  remembered in seen_old_ids.csv and dropped jids in out-of-scope.csv (full
  rows, reversible) — the devnetjobsindia idiom — so each jid is fetched at
  most once, ever. Steady state ≈ 1 sitemap request + 1 detail request per
  genuinely new posting.
* `baseSalary` was null on every probed page (ICON does not publish pay,
  even on US postings); the schema.org shape is still parsed when present.
  Absent salary → "Not Disclosed", blank club salary columns. Only
  USD/INR annual|monthly pay can be represented by the club enums; other
  currencies/periods stay rich-CSV-only.
* `employmentType` is a list (["Permanent"]); kept raw in the rich CSV,
  mapped to the club full_time/part_time enum.
* 403/404 are treated as permanent — never retried (politeness).

Classification (shared taxonomy)
--------------------------------
Title-only in-scope rate measured at 68% — the best board of the 2026-08
probe sweep — but the whole sitemap is still crawled and the keep/drop
decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the JSON-LD `industry` department tags, joined ("; ") —
                the site's curated role signal, passed through unmodified
                (skills-veto is per-board: nothing here mislabels roles,
                so nothing is stripped).
* description = the JSON-LD description, HTML-stripped.

in_scope False (finance, IT, facilities, HR...) -> DROPPED, counted
excluded_out_of_scope, full row appended to out-of-scope.csv.
needs_review True keeps the row AND appends it to needs_review.csv.

Outputs
-------
* iconplc_jobs.csv — rich cumulative store (dedup key: job_id), source of
  truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/iconplc.csv — the same jobs mapped to the
  HealthCareers.club CLUB_COLUMNS schema.
* seen_old_ids.csv — out-of-window jids (detail-fetch skip list).
* out-of-scope.csv — dropped rows + their skip list (reversible).
* needs_review.csv — kept but flagged.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(30 — CRO postings are long-lived); later runs keep only jobs newer than
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

SITE = "iconplc"
SITE_BASE = "https://careers.iconplc.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SITEMAP_URL = SITE_BASE + "/vacanciessitemap.xml"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# CRO postings stay open for months; a wider first-run window seeds a usable
# corpus. No salary filter (master spec): captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "iconplc_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "requisition_id", "title", "company", "city",
    "country", "country_code", "location_raw", "locations_all", "is_remote",
    "industry", "employment_type_raw", "salary_raw", "salary_min",
    "salary_max", "salary_currency", "salary_period", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in", "needs_review",
    "experience_min_years", "posted_date", "valid_through", "description",
    "job_url", "scraped_at",
]

log = logging.getLogger("iconplc_scraper")

# ----------------------------------------------------------------------------
# Location: ICON hires globally; addressLocality carries everything.
# ----------------------------------------------------------------------------

# lowercased locality country token -> (canonical name, ISO alpha-2, dial).
# Covers every country observed in the 2026-08-27 sitemap plus close
# neighbours; an unknown token exports blank code/dial, never guessed.
COUNTRY_MAP = {
    "us": ("United States of America", "US", "+1"),
    "usa": ("United States of America", "US", "+1"),
    "united states": ("United States of America", "US", "+1"),
    "united states of america": ("United States of America", "US", "+1"),
    "canada": ("Canada", "CA", "+1"),
    "mexico": ("Mexico", "MX", "+52"),
    "brazil": ("Brazil", "BR", "+55"),
    "argentina": ("Argentina", "AR", "+54"),
    "colombia": ("Colombia", "CO", "+57"),
    "chile": ("Chile", "CL", "+56"),
    "peru": ("Peru", "PE", "+51"),
    "uk": ("United Kingdom", "GB", "+44"),
    "united kingdom": ("United Kingdom", "GB", "+44"),
    "great britain & northern ireland": ("United Kingdom", "GB", "+44"),
    "great britain and northern ireland": ("United Kingdom", "GB", "+44"),
    # "Regional Great Britain (Northern Ireland)" paren-strips to this:
    "great britain": ("United Kingdom", "GB", "+44"),
    "ireland": ("Ireland", "IE", "+353"),
    "germany": ("Germany", "DE", "+49"),
    "france": ("France", "FR", "+33"),
    "spain": ("Spain", "ES", "+34"),
    "portugal": ("Portugal", "PT", "+351"),
    "netherlands": ("Netherlands", "NL", "+31"),
    "belgium": ("Belgium", "BE", "+32"),
    "italy": ("Italy", "IT", "+39"),
    "poland": ("Poland", "PL", "+48"),
    "sweden": ("Sweden", "SE", "+46"),
    "norway": ("Norway", "NO", "+47"),
    "denmark": ("Denmark", "DK", "+45"),
    "finland": ("Finland", "FI", "+358"),
    "switzerland": ("Switzerland", "CH", "+41"),
    "austria": ("Austria", "AT", "+43"),
    "czech republic": ("Czech Republic", "CZ", "+420"),
    "czechia": ("Czech Republic", "CZ", "+420"),
    "slovakia": ("Slovakia", "SK", "+421"),
    "hungary": ("Hungary", "HU", "+36"),
    "romania": ("Romania", "RO", "+40"),
    "bulgaria": ("Bulgaria", "BG", "+359"),
    "greece": ("Greece", "GR", "+30"),
    "serbia": ("Serbia", "RS", "+381"),
    "croatia": ("Croatia", "HR", "+385"),
    "lithuania": ("Lithuania", "LT", "+370"),
    "latvia": ("Latvia", "LV", "+371"),
    "estonia": ("Estonia", "EE", "+372"),
    "ukraine": ("Ukraine", "UA", "+380"),
    "georgia": ("Georgia", "GE", "+995"),   # only via "Georgia, <city>"
    "turkey": ("Turkey", "TR", "+90"),
    "israel": ("Israel", "IL", "+972"),
    "south africa": ("South Africa", "ZA", "+27"),
    "kenya": ("Kenya", "KE", "+254"),
    "egypt": ("Egypt", "EG", "+20"),
    "united arab emirates": ("United Arab Emirates", "AE", "+971"),
    "uae": ("United Arab Emirates", "AE", "+971"),
    "saudi arabia": ("Saudi Arabia", "SA", "+966"),
    "india": ("India", "IN", "+91"),
    "china": ("China", "CN", "+86"),
    "hong kong": ("Hong Kong", "HK", "+852"),
    "taiwan": ("Taiwan", "TW", "+886"),
    "japan": ("Japan", "JP", "+81"),
    "korea": ("South Korea", "KR", "+82"),
    "south korea": ("South Korea", "KR", "+82"),
    "singapore": ("Singapore", "SG", "+65"),
    "malaysia": ("Malaysia", "MY", "+60"),
    "thailand": ("Thailand", "TH", "+66"),
    "vietnam": ("Vietnam", "VN", "+84"),
    "philippines": ("Philippines", "PH", "+63"),
    "indonesia": ("Indonesia", "ID", "+62"),
    "australia": ("Australia", "AU", "+61"),
    "new zealand": ("New Zealand", "NZ", "+64"),
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
}
US_STATE_ABBREVS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}
_US = COUNTRY_MAP["us"]

_PAREN_RE = re.compile(r"\s*\([^)]*\)")          # site tags: (ICON) (PRA) (Labs)
_REMOTE_RE = re.compile(r"remote|home[\s-]?based|home[\s-]?working",
                        re.IGNORECASE)


def parse_locality(raw):
    """Split an Attrax addressLocality into location fields.

    Returns dict(country, country_code, dial, city, is_remote) — never
    raises. Shapes handled (all real, sitemap 2026-08-27):
      "India, Chennai"                -> India / Chennai
      "United States of America"     -> US, no city
      "California"                   -> US, city "California" (bare state;
                                        includes the ambiguous bare
                                        "Georgia" — the country form always
                                        arrives as "Georgia, Tbilisi")
      "Chicago, IL"                  -> US, city "Chicago"
      "US, Blue Bell (ICON)"         -> US / Blue Bell (paren tag stripped)
      "Regional United States (PRA)" -> US, remote (region coverage role)
    Unknown country tokens export blank code/dial, never guessed.
    """
    text = clean_text(raw)
    is_remote = bool(_REMOTE_RE.search(text))
    core = _PAREN_RE.sub("", text).strip(" ,")
    if re.match(r"(?i)^regional\b", core):
        is_remote = True
        core = re.sub(r"(?i)^regional\s+", "", core).strip(" ,")

    head, _, rest = (p.strip() for p in core.partition(","))
    result = {"country": "", "country_code": "", "dial": "",
              "city": "", "is_remote": is_remote}

    if rest:                                     # "Head, Rest"
        country = COUNTRY_MAP.get(head.lower())
        if country:                              # "India, Chennai"
            name, code, dial = country
            result.update(country=name, country_code=code, dial=dial,
                          city=_PAREN_RE.sub("", rest).strip(" ,"))
            return result
        rest_key = rest.lower().strip(".")
        if rest_key in US_STATE_ABBREVS or rest_key in US_STATE_NAMES:
            name, code, dial = _US                # "Chicago, IL"
            result.update(country=name, country_code=code, dial=dial,
                          city=head)
            return result
        result.update(city=core)                 # unknown "X, Y"
        return result

    if core.lower() in US_STATE_NAMES:           # bare "California"
        name, code, dial = _US
        result.update(country=name, country_code=code, dial=dial, city=core)
        return result
    country = COUNTRY_MAP.get(core.lower())
    if country:                                  # "United States of America"
        name, code, dial = country
        result.update(country=name, country_code=code, dial=dial)
        return result
    result.update(city=core)                     # unknown token
    return result


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

_SITEMAP_JOB_RE = re.compile(
    r"<loc>\s*(https://careers\.iconplc\.com/job/[^<]*-jid-(\d+))\s*</loc>")


def parse_sitemap_jobs(xml):
    """(url, jid) pairs from vacanciessitemap.xml, highest jid first.

    <lastmod> is a modification stamp, not the posting date — ignored.
    jids are sequential, so descending jid ~= newest posting first.
    """
    seen = {}
    for url, jid in _SITEMAP_JOB_RE.findall(xml or ""):
        seen[jid] = url
    return [(seen[jid], jid)
            for jid in sorted(seen, key=int, reverse=True)]


# ---- detail page ------------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S)
# ICON's blocks parsed clean on every probe, but Attrax gets defensive
# parsing anyway (devnetjobsindia's site appends a stray `}`): raw_decode
# stops at the end of the first JSON value; strict=False tolerates raw
# control characters inside strings.
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


def listify(value):
    """JSON-LD fields that are sometimes scalar, sometimes list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


_SALARY_PERIODS = {"YEAR": "per_annum", "MONTH": "per_month",
                   "HOUR": "per_hour", "WEEK": "per_week", "DAY": "per_day"}


def parse_base_salary(base_salary):
    """schema.org baseSalary -> (raw, min, max, currency, period).

    Null on every probed ICON page, but the standard shape is supported for
    when Attrax starts emitting it. Absent -> ("Not Disclosed", "", "", "",
    ""); never invented.
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


# "Minimum 5 years...", "at least 3 years", "5+ years of TMF experience",
# "2-4 years' experience in clinical monitoring" — grounded extraction
# only, never inferred. Up to two words may sit between "years of" and
# "experience" ("years of TMF experience").
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
    """["Permanent"] -> full_time; part-time variants -> part_time."""
    joined = " ".join(clean_text(t) for t in listify(employment_types)).lower()
    return "part_time" if "part" in joined else "full_time"


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The JSON-LD `industry` department tags are the curated `skills` signal,
    passed through unmodified (nothing on this board mislabels roles). They
    stay in the rich CSV as a raw source column and never decide the
    category themselves. Returns in_scope — False means DROP the row.
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("industry", ""),
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
    for url in (SITEMAP_URL, SITE_BASE + "/job/example-role-jid-1"):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed (/job/ detail paths + sitemap; "
             "/jobs?* search paths are disallowed and never requested)")


def fetch(session, url):
    """GET one page with retries/backoff. Returns text or None.

    4xx (403 included) is permanent — never retried. The robots-disallowed
    /jobs?* search paths are hard-asserted out.
    """
    assert "/jobs?" not in url, "search URLs are robots-disallowed"
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

def build_row(job_id, posting, url):
    """Rich row from a detail page's JobPosting JSON-LD."""
    title = clean_text(posting.get("title"))

    localities = []
    for place in listify(posting.get("jobLocation")):
        if isinstance(place, dict):
            address = place.get("address") or {}
            locality = clean_text(address.get("addressLocality"))
            if locality:
                localities.append(locality)
    primary = parse_locality(localities[0] if localities else "")

    description = strip_html(posting.get("description") or "")[:DESCRIPTION_MAX_CHARS]

    org = posting.get("hiringOrganization")
    company = clean_text(org.get("name")) if isinstance(org, dict) else ""

    identifier = posting.get("identifier")
    requisition = clean_text(identifier.get("value")) \
        if isinstance(identifier, dict) else ""

    industry = "; ".join(clean_text(i) for i in listify(posting.get("industry"))
                         if clean_text(i))

    salary_raw, sal_min, sal_max, sal_ccy, sal_period = \
        parse_base_salary(posting.get("baseSalary"))

    return {
        "source": SITE,
        "job_id": str(job_id),
        "requisition_id": requisition,
        "title": title,
        "company": company or "ICON",
        "city": primary["city"],
        "country": primary["country"],
        "country_code": primary["country_code"],
        "location_raw": localities[0] if localities else "",
        "locations_all": "; ".join(localities),
        "is_remote": primary["is_remote"],
        "industry": industry,
        "employment_type_raw": "; ".join(
            clean_text(t) for t in listify(posting.get("employmentType"))),
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
        "needs_review": False,
        "experience_min_years": parse_experience_years(description),
        "posted_date": parse_iso_date(posting.get("datePosted")),
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


def _is_true(value):
    return str(value).strip().lower() == "true"


def rich_row_to_club_row(r):
    """Map a rich row to the club's 23-column schema.

    Remote/regional rows use the himalayas convention: city_name "Remote".
    Only USD/INR annual|monthly salary is representable by the club enums;
    anything else stays rich-CSV-only (never converted).
    """
    currency = _blank(r.get("salary_currency"))
    period = _blank(r.get("salary_period"))
    lo, hi = _blank(r.get("salary_min")), _blank(r.get("salary_max"))
    exportable = (bool(lo) and currency in ("INR", "USD")
                  and period in ("per_month", "per_annum"))

    if _is_true(r.get("is_remote")):
        city = "Remote"
    else:
        city = _blank(r.get("city")) or _blank(r.get("country"))

    return {
        "country_name": _blank(r.get("country")),
        "country_code": _blank(r.get("country_code")),
        "country_dial_code": COUNTRY_MAP.get(
            _blank(r.get("country")).lower(), ("", "", ""))[2],
        "city_name": city,
        "company_name": _blank(r.get("company")) or "ICON",
        # ICON is a CRO — the club's hospital|pharma enum reads that as pharma.
        "company_type": "pharma",
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
        description="Scrape CRO jobs from careers.iconplc.com (sitemap only).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail pages "
                             "(first run used --limit 120)")
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
                "detail_failed": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0

    for url, job_id in jobs:                   # descending jid ~= newest first
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

        detail_html = fetch(session, url)
        fetched += 1
        posting = parse_job_posting(detail_html or "")
        if not posting:
            counters["detail_failed"] += 1
            log.warning("No JobPosting JSON-LD for jid=%s", job_id)
            continue
        try:
            row = build_row(job_id, posting, url)
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
            out_of_scope_ids.add(job_id)
            dropped_rows.append(row)
            continue

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "industry": row["industry"],
                               "location_raw": row["location_raw"],
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


if __name__ == "__main__":
    main()
