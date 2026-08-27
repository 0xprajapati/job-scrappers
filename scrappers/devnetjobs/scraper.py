#!/usr/bin/env python3
"""Scrape international development job listings from devnetjobs.org.

Data source
-----------
DevNetJobs.org is the GLOBAL twin of devnetjobsindia.org — the same ASP.NET
platform, the same `jobdescription.aspx?job_id=<int>` URL scheme, the same
sitemap, the same JSON-LD contract (and the same two site bugs).  Its supply
is worldwide development / UN-adjacent / INGO postings: WHO, UNICEF, MSF,
CARE, IFRC, national NGOs — duty stations in ~120 countries.

Why this is a separate scraper and not a flag on the devnetjobsindia one:
the crawl and detail layers are twins, but the two boards differ in three
ways that reach into every row.

* **Separate id spaces.** The global board is at job_id ~313xxx and the
  India board at ~302xxx.  They are separate databases: 302833 is a live
  RFP on the India board and does not exist on the global one.  Each board
  therefore needs its own rich CSV, its own skip lists and its own
  watermark — one directory per board, the way the fleet is laid out.
* **Location is a different shape.** The India board publishes
  `addressRegion` (an Indian state) and its club export hard-codes
  India/IN/+91.  The global board publishes NO region and gives
  `addressCountry` as an ISO alpha-2 code — sometimes several
  ("KE, GB"), sometimes absent (worldwide-remote postings).  So the club
  mapping is genuinely different code, not a parameter.
* **The markup differs.** The global board emits ASP.NET's full control ids
  (`ctl00_ContentPlaceHolder1_JD1_lblSector1`); the India board strips the
  `ctl00_` prefix.  The India scraper's sector regex does not match this
  board's HTML.

Discovery is the sitemap:

    https://devnetjobs.org/sitemap.aspx        (named by robots.txt)

which lists EVERY active posting as `jobdescription.aspx?job_id=<sequential
int>` (856 URLs, Aug 2026) plus ~27 static pages.  The homepage and the
listing pages (standard/highlighted/consulting/rfp_assignments) are strict
subsets, and their cards carry only "Apply by" — no posted date — so the
sitemap is the one crawl source and one request per run.  Every `<lastmod>`
is the sitemap's own generation date and carries no per-job information.

Every detail page embeds a schema.org JobPosting JSON-LD block with
`datePosted`, `validThrough`, a clean `title`, the full description,
`hiringOrganization.name` and a `jobLocation` address.  Two site bugs,
inherited from the shared platform, are tolerated:

* the JSON-LD block ends with a stray extra `}` after the object — parsed
  with `raw_decode` (which stops at the end of the first object) instead of
  `json.loads`, which raises "Extra data";
* the detail page's date span is mislabeled
  `ctl00_ContentPlaceHolder1_JD1_lblPostedDate` — the visible text next to
  it reads "Apply by:", i.e. the control holds the DEADLINE, not the posted
  date.  Only the JSON-LD `datePosted` is trusted.

Detail pages carry up to three "Relevant Sectors" tags (`lblSector1..3`,
e.g. "Health, Doctors, Nurses, HIV/AIDS") — the site's own curated role
signal, passed to classify_job as `skills` and kept in the rich CSV as a raw
source column.  Unlike the India board they are OPTIONAL: 26 of 60 sampled
global postings carry none, so the classifier leans on title+description
more often here.

robots.txt: `Allow: /` with only /FCKeditor/ and /admin/ disallowed.
Compliance is verified at startup with urllib.robotparser.  Note both
`www.devnetjobs.org` and `http://` 301 to `https://devnetjobs.org` — the
canonical apex host is used directly.

Verified quirks
---------------
* posted_date exists ONLY in the detail JSON-LD, so a new id's detail must
  be fetched before the cutoff can be judged.  Out-of-window ids are
  remembered in seen_old_ids.csv and dropped ids in out-of-scope.csv (full
  rows, reversible) — the devnetjobsindia/jobberman idiom — so each id is
  fetched at most once, ever.  Steady state ≈ 1 sitemap request + 1 detail
  request per genuinely new posting.
* `addressCountry` is an ISO alpha-2 code, not a name.  Multi-country
  postings give a comma list ("SO, KE") — the first code is the duty
  station and the rest are kept in the rich `country_codes` column.  An
  unknown code exports its code but leaves country_name/dial blank rather
  than guessing.
* Remote postings set `jobLocationType: "TELECOMMUTE"` and may drop
  `jobLocation` entirely in favour of
  `applicantLocationRequirements: {"name": "Worldwide"}` — those export as
  job_type "remote" with city_name "Remote".  A remote posting with NO
  addressCountry exports country_name "Worldwide" (code/dial blank — there
  is no ISO code to give), mirroring the himalayas convention so the
  fleet's remote-only rows never ship all-blank country columns.
* RFPs/tenders share the job_id space and the same JobPosting JSON-LD
  (they have their own rfp_assignments.aspx listing but appear in the
  sitemap like any job).  They are procurement notices, not jobs, so a row
  whose title looks like one (RFP/EOI/tender/empanelment...) is marked
  `is_rfp` and — when the classifier keeps it — force-flagged into
  needs_review.csv rather than silently exported as a job.
* A sizeable minority of postings are in French, Spanish or German
  ("Recrutement D'un Consultant...", "Consultoría...").  The shared
  classifier is English-keyword-based, so these fall out of scope unless
  their English description carries the signal.  That is a known,
  deliberate blind spot — the fix would be widening the taxonomy keywords,
  which scrapers must never do.
* The board publishes NO salary anywhere → blank club salary columns,
  never invented.
* INGO/UN employers don't fit the club's hospital|pharma enum; the usual
  company-text regex marks the odd CRO/diagnostics employer "pharma" and
  everything else defaults to "hospital" (the fleet-wide convention).

Classification (shared taxonomy)
--------------------------------
The whole board is crawled — fetch wide, filter tight.  The keep/drop and
labeling decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the detail page's Relevant Sectors tags, joined ("" when
                the posting has none — common on this board)
* description = the JSON-LD description, HTML-stripped

in_scope False (admin, fundraising, logistics, education, IT) -> DROPPED,
counted excluded_out_of_scope, full row appended to out-of-scope.csv.
in_scope True fills category/sub_category/role_family and the score-trace
columns; needs_review True keeps the row AND appends it to needs_review.csv.

Outputs
-------
* devnetjobs_jobs.csv — rich cumulative store (dedup key: job_id), source
  of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/devnetjobs.csv — the same jobs mapped to the
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

SITE = "devnetjobs"
# The apex host is canonical: www. and http:// both 301 here.
SITE_BASE = "https://devnetjobs.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SITEMAP_URL = SITE_BASE + "/sitemap.aspx"
JOB_URL_TEMPLATE = SITE_BASE + "/jobdescription.aspx?job_id={}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# INGO/UN postings stay open ~30 days; a 14-day first window seeds a usable
# corpus without walking the whole 856-id board's history. No salary filter
# (master spec): salaries captured, never filtered on — this board has none.
INITIAL_WINDOW_DAYS = 14
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "devnetjobs_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "country",
    "country_codes", "remote", "sectors", "is_rfp", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in", "needs_review",
    "experience_min_years", "posted_date", "valid_through", "description",
    "job_url", "scraped_at",
]

log = logging.getLogger("devnetjobs_scraper")

# ISO alpha-2 (what this board's addressCountry actually carries) ->
# (country name, dial code).  Covers the development-sector duty stations
# that dominate the board; an unseen code exports its own code with a blank
# name and dial code (never guessed).
COUNTRY_BY_CODE = {
    "AE": ("United Arab Emirates", "+971"), "AF": ("Afghanistan", "+93"),
    "AL": ("Albania", "+355"), "AM": ("Armenia", "+374"),
    "AO": ("Angola", "+244"), "AR": ("Argentina", "+54"),
    "AT": ("Austria", "+43"), "AU": ("Australia", "+61"),
    "AZ": ("Azerbaijan", "+994"), "BA": ("Bosnia and Herzegovina", "+387"),
    "BD": ("Bangladesh", "+880"), "BE": ("Belgium", "+32"),
    "BF": ("Burkina Faso", "+226"), "BG": ("Bulgaria", "+359"),
    "BI": ("Burundi", "+257"), "BJ": ("Benin", "+229"),
    "BO": ("Bolivia", "+591"), "BR": ("Brazil", "+55"),
    "BT": ("Bhutan", "+975"), "BW": ("Botswana", "+267"),
    "CA": ("Canada", "+1"), "CD": ("Democratic Republic of the Congo", "+243"),
    "CF": ("Central African Republic", "+236"), "CG": ("Congo", "+242"),
    "CH": ("Switzerland", "+41"), "CI": ("Cote d'Ivoire", "+225"),
    "CL": ("Chile", "+56"), "CM": ("Cameroon", "+237"),
    "CN": ("China", "+86"), "CO": ("Colombia", "+57"),
    "CR": ("Costa Rica", "+506"), "CU": ("Cuba", "+53"),
    "CV": ("Cabo Verde", "+238"), "CY": ("Cyprus", "+357"),
    "CZ": ("Czechia", "+420"), "DE": ("Germany", "+49"),
    "DJ": ("Djibouti", "+253"), "DK": ("Denmark", "+45"),
    "DO": ("Dominican Republic", "+1"), "DZ": ("Algeria", "+213"),
    "EC": ("Ecuador", "+593"), "EE": ("Estonia", "+372"),
    "EG": ("Egypt", "+20"), "ER": ("Eritrea", "+291"),
    "ES": ("Spain", "+34"), "ET": ("Ethiopia", "+251"),
    "FI": ("Finland", "+358"), "FJ": ("Fiji", "+679"),
    "FR": ("France", "+33"), "GA": ("Gabon", "+241"),
    "GB": ("United Kingdom", "+44"), "GE": ("Georgia", "+995"),
    "GH": ("Ghana", "+233"), "GM": ("Gambia", "+220"),
    "GN": ("Guinea", "+224"), "GQ": ("Equatorial Guinea", "+240"),
    "GR": ("Greece", "+30"), "GT": ("Guatemala", "+502"),
    "GW": ("Guinea-Bissau", "+245"), "GY": ("Guyana", "+592"),
    "HN": ("Honduras", "+504"), "HR": ("Croatia", "+385"),
    "HT": ("Haiti", "+509"), "HU": ("Hungary", "+36"),
    "ID": ("Indonesia", "+62"), "IE": ("Ireland", "+353"),
    "IL": ("Israel", "+972"), "IN": ("India", "+91"),
    "IQ": ("Iraq", "+964"), "IR": ("Iran", "+98"),
    "IS": ("Iceland", "+354"), "IT": ("Italy", "+39"),
    "JM": ("Jamaica", "+1"), "JO": ("Jordan", "+962"),
    "JP": ("Japan", "+81"), "KE": ("Kenya", "+254"),
    "KG": ("Kyrgyzstan", "+996"), "KH": ("Cambodia", "+855"),
    "KP": ("North Korea", "+850"), "KR": ("South Korea", "+82"),
    "KW": ("Kuwait", "+965"), "KZ": ("Kazakhstan", "+7"),
    "LA": ("Laos", "+856"), "LB": ("Lebanon", "+961"),
    "LK": ("Sri Lanka", "+94"), "LR": ("Liberia", "+231"),
    "LS": ("Lesotho", "+266"), "LT": ("Lithuania", "+370"),
    "LU": ("Luxembourg", "+352"), "LV": ("Latvia", "+371"),
    "LY": ("Libya", "+218"), "MA": ("Morocco", "+212"),
    "MD": ("Moldova", "+373"), "ME": ("Montenegro", "+382"),
    "MG": ("Madagascar", "+261"), "MK": ("North Macedonia", "+389"),
    "ML": ("Mali", "+223"), "MM": ("Myanmar", "+95"),
    "MN": ("Mongolia", "+976"), "MR": ("Mauritania", "+222"),
    "MT": ("Malta", "+356"), "MU": ("Mauritius", "+230"),
    "MV": ("Maldives", "+960"), "MW": ("Malawi", "+265"),
    "MX": ("Mexico", "+52"), "MY": ("Malaysia", "+60"),
    "MZ": ("Mozambique", "+258"), "NA": ("Namibia", "+264"),
    "NE": ("Niger", "+227"), "NG": ("Nigeria", "+234"),
    "NI": ("Nicaragua", "+505"), "NL": ("Netherlands", "+31"),
    "NO": ("Norway", "+47"), "NP": ("Nepal", "+977"),
    "NZ": ("New Zealand", "+64"), "OM": ("Oman", "+968"),
    "PA": ("Panama", "+507"), "PE": ("Peru", "+51"),
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
    "TD": ("Chad", "+235"), "TG": ("Togo", "+228"),
    "TH": ("Thailand", "+66"), "TJ": ("Tajikistan", "+992"),
    "TL": ("Timor-Leste", "+670"), "TN": ("Tunisia", "+216"),
    "TR": ("Turkey", "+90"), "TZ": ("Tanzania", "+255"),
    "UA": ("Ukraine", "+380"), "UG": ("Uganda", "+256"),
    "US": ("United States", "+1"), "UY": ("Uruguay", "+598"),
    "UZ": ("Uzbekistan", "+998"), "VE": ("Venezuela", "+58"),
    "VN": ("Vietnam", "+84"), "VU": ("Vanuatu", "+678"),
    "WS": ("Samoa", "+685"), "YE": ("Yemen", "+967"),
    "ZA": ("South Africa", "+27"), "ZM": ("Zambia", "+260"),
    "ZW": ("Zimbabwe", "+263"),
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

# This board emits ASP.NET's full control ids (ctl00_ prefix); the India
# board strips them. The prefix is optional so one regex reads both.
_SECTOR_RE = re.compile(
    r'id="(?:ctl00_)?ContentPlaceHolder1_JD1_lblSector\d"[^>]*>([^<]*)<')


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
    """The detail page's Relevant Sectors tags, joined ("; ").

    Optional on this board — 26 of 60 sampled postings carry none, and the
    site pads unused slots with empty spans.
    """
    return "; ".join(clean_text(s) for s in _SECTOR_RE.findall(page_html or "")
                     if clean_text(s))


def parse_iso_date(text):
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


def parse_country_codes(address_country):
    """"SO, KE" -> ["SO", "KE"]; "" / None -> []. ISO alpha-2 only."""
    return [c for c in re.findall(r"[A-Za-z]{2}", str(address_country or "").upper())]


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
# The trailing \b keeps reference numbers like "(RFP500586)" out.
#
# The last two alternatives catch the shapes that dodge the vocabulary
# above entirely — real 2026-08-27 titles "Calling for Data Collection
# Teams for Fowash Project Endline Survey" and "Software Development,
# Update, and Maintenance Support Services for Infectious Disease
# Surveillance...".  They are deliberately narrow: `calling for` alone
# would flag ordinary ads ("Calling for applications"), so a
# procurement-shaped object ("...Teams/Services/Consultants for") is
# required.  Over-flagging is the safe direction here — the flag only
# routes a row to needs_review.csv, it never drops it.
_RFP_TITLE_RE = re.compile(
    r"\b(rfp|rfq|eoi|tender|empanelment|request for proposals?|"
    r"expressions? of interest|call for proposals?|"
    r"calling for\s+(?:\w+\s+){0,3}?(?:teams?|services|consultants?|"
    r"agenc(?:y|ies)|firms?|vendors?|suppliers?)\b|"
    r"support services for)\b", re.IGNORECASE)


def is_rfp_title(title):
    return bool(_RFP_TITLE_RE.search(title or ""))


# The board tags every posting with 0-3 sector labels drawn from a closed
# 16-item vocabulary.  They are the site's curated role signal and are
# passed to the classifier as `skills` — with ONE removed first.
#
# "Fundraising, Business Development, Grants Writer" describes how the
# hiring ORGANISATION is funded, not what the role does, and the shared
# taxonomy treats "Business Development" as a negative keyword.  The tag
# therefore vetoes health roles that merely sit in a fundraising-adjacent
# unit: measured 2026-08-27, it appears on 78 of 547 in-window postings and
# is the sole cause of all 78 "Business Development" vetoes on this board —
# never once triggered by a title or description.
#
# Dropping the tag only removes a mislabeled signal; it can never add one.
# The raw, unmodified tag list stays in the rich CSV's `sectors` column, so
# the decision is visible and reversible.
_SKILLS_TAG_BLOCKLIST = ("Fundraising, Business Development, Grants Writer",)


def sectors_for_classifier(sectors):
    """The sector tags minus the funding-sector label (see above).

    Raw `sectors` is what gets stored; this is what gets classified.
    """
    return "; ".join(t.strip() for t in str(sectors or "").split(";")
                     if t.strip() and t.strip() not in _SKILLS_TAG_BLOCKLIST)


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The detail page's Relevant Sectors tags are the curated `skills` signal,
    minus the funding-sector label (see sectors_for_classifier). They stay
    in the rich CSV as a raw source column and never decide the category.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           sectors_for_classifier(row.get("sectors", "")),
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
# INGOs default to "hospital" (the fleet-wide convention for the club enum).
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
    for url in (SITEMAP_URL, JOB_URL_TEMPLATE.format(313685)):
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

    location = posting.get("jobLocation")
    address = (location or {}).get("address") or {}
    city = clean_text(address.get("addressLocality"))
    codes = parse_country_codes(address.get("addressCountry"))

    # Worldwide-remote postings drop jobLocation entirely and state the
    # requirement instead: {"@type": "Country", "name": "Worldwide"}.
    alr = posting.get("applicantLocationRequirements")
    remote = clean_text(posting.get("jobLocationType")).upper() == "TELECOMMUTE"
    if not city and isinstance(alr, dict):
        city = clean_text(alr.get("name"))

    description = strip_html(posting.get("description") or "")[:DESCRIPTION_MAX_CHARS]

    org = posting.get("hiringOrganization")
    company = clean_text(org.get("name")) if isinstance(org, dict) else ""

    return {
        "source": SITE,
        "job_id": str(job_id),
        "title": title,
        "company": company,
        "city": city,
        # first code = the duty station; the rest are kept raw below
        "country": codes[0] if codes else "",
        "country_codes": ", ".join(codes),
        "remote": remote,
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


def _truthy(value):
    return _blank(value).lower() in ("true", "1", "yes")


# Remote postings whose `city` carries one of these region placeholders (the
# source's addressLocality / applicantLocationRequirements name) have no real
# locality; the club city_name says "Remote" instead — the himalayas
# convention, so the fleet's remote rows read the same downstream.
_REMOTE_REGION_PLACEHOLDERS = {"", "remote", "worldwide", "anywhere",
                               "home based", "home-based", "global"}


def rich_row_to_club_row(r):
    code = _blank(r.get("country")).upper()
    name, dial = COUNTRY_BY_CODE.get(code, ("", ""))
    remote = _truthy(r.get("remote"))
    city = _blank(r.get("city"))
    # A remote posting with NO duty-station country (no addressCountry in
    # the JSON-LD) is hireable worldwide: country_name says "Worldwide" with
    # code/dial left blank (there is no ISO code to give), never all-blank
    # country columns. Mirrors the himalayas convention of putting the
    # source's hiring-region name in country_name and "Remote" in city_name.
    if remote and not code:
        name = "Worldwide"
    if remote and city.lower() in _REMOTE_REGION_PLACEHOLDERS:
        city = "Remote"
    return {
        # An unseen ISO code keeps its code but leaves the name and dial
        # code blank — never guessed.
        "country_name": name,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": city or name or ("Remote" if remote else ""),
        "company_name": _blank(r.get("company")),
        "company_type": classify_company_type(_blank(r.get("company"))),
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": "remote" if remote else "full_time",
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
        description="Scrape international development jobs from devnetjobs.org.")
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
                               "country": row["country"],
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
