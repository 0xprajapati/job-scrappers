#!/usr/bin/env python3
"""Scrape Abt Global (US development implementer) vacancies from the Oracle
HCM Candidate Experience REST API on egpy.fa.us2.oraclecloud.com.

Data source
-----------
Abt Global's careers site is an Oracle HCM Candidate Experience tenant with
ONE published site, ``JoinAbt`` (probed 2026-08-28: 16 open requisitions,
TotalJobsCount agrees). Oracle CE exposes its public candidate API
unauthenticated — no key, no cookie, no signed parameter — exactly as on
the undp/novotech tenants this scraper is modeled on:

  discovery (one request, the whole board fits one page):
    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
        &finder=findReqs;siteNumber=JoinAbt,limit=500,sortBy=POSTING_DATES_DESC

  detail (one request per in-window requisition):
    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="<id>",siteNumber=JoinAbt

`finder` needs literal `;` and `,`, so URLs are built as strings — a
params dict would percent-encode the separators and Oracle answers 400.
The host serves no robots.txt (HTTP 404 => no restrictions, the undp
precedent); check_robots() still runs so a later-added robots.txt stops
the scraper instead of being ignored.

Verified quirks (probe 2026-08-28)
----------------------------------
* The listing's `PostedDate` is a plain date and matches the date half of
  the detail's `ExternalPostedStartDate` (sampled), so the time window is
  decided BEFORE any detail fetch. Oracle dates can be tenant-timezone
  (the kfshrc Riyadh-midnight lesson); the 2-day watermark grace absorbs
  any off-by-one.
* UNLIKE the novotech/omega tenants, `ExternalPostedEndDate` IS populated
  here (application deadlines ~2-4 weeks out) -> `valid_through` is real
  data on this board, captured from the detail.
* `Category` and `JobFunction` are ALSO populated ("Program Delivery",
  "Project and Program Management" / "Program Operations (Field)",
  "Project Management"...). They are generic org-structure tags — nothing
  in them mislabels a role as health (the skills-veto-is-per-board rule),
  so both are joined into the classifier's `skills` signal and stored in
  the rich `sectors` column. `JobFamily`, `skills` and
  `requisitionFlexFields` are dead ([]/null on every sampled detail).
* The posting text lives in `ExternalDescriptionStr` alone (up to ~30k
  chars of styled HTML); `ExternalResponsibilitiesStr` /
  `ExternalQualificationsStr` were empty on every sampled detail but are
  still concatenated in reading order (the novotech split-body lesson —
  costs nothing, survives a tenant reconfiguration).
* `PrimaryLocation` is "City, Country" or "City, ST, United States";
  `PrimaryLocationCountry` is ISO alpha-2. The trailing country segment
  sometimes uses an ALIAS that differs from the ISO name ("Kinshasa, DR
  Congo-Kinshasa" for CD) — COUNTRY_LOCATION_ALIASES drops those instead
  of leaking them into the city. Country names come from the code via the
  fleet-vetted ISO table — never guessed for an unknown code.
* Abt posts in country offices with French-speaking footprints (DRC,
  Madagascar, Côte d'Ivoire...). Sampled postings are English, but any
  French/Spanish-language description is kept AND flagged needs_review
  via the undp stopword heuristic (looks_non_english) rather than
  published untranslated.
* The board carries "Expression of Interest" / talent-pool /
  "Technical Advisory Panel" postings (3 of 16 on the probe). They sit in
  the same requisition id space as real vacancies, so they are kept when
  in scope but force-flagged needs_review (the undp RFP-title idiom).
* No salary anywhere on the board -> blank salary columns, never
  invented.

Classification (shared taxonomy)
--------------------------------
Fetch wide, filter tight: every open requisition is enumerated and the
keep/drop and labeling decision belongs entirely to
_shared/classification.classify_job(title, sectors, description).
in_scope False -> DROPPED, counted excluded_out_of_scope, full row (with
its vetoed_by trace) appended to out-of-scope.csv. needs_review True ->
kept AND appended to needs_review.csv. Expect a heavily Public Health
mix — epidemiologists, malaria chiefs-of-party, WASH, global health
security (60% title-only in-scope on the probe sweep, the densest board
of the sweep).

Outputs
-------
* abt_jobs.csv         — rich cumulative store (dedup key: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/abt.csv — the same jobs in CLUB_COLUMNS.
* seen_old_ids.csv     — out-of-window ids and their dates.
* out-of-scope.csv     — dropped rows + their skip list (reversible).
* needs_review.csv     — kept but flagged.

Time window: first run keeps requisitions posted in the last
INITIAL_WINDOW_DAYS (30 — a tiny slow-cadence board, the wider window
seeds the whole live board); later runs keep only those newer than the
newest stored posted_date minus WATERMARK_GRACE_DAYS of overlap.

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
from collections import Counter
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

SITE = "abt"
COMPANY_NAME = "Abt Global"

CE_HOST = "https://egpy.fa.us2.oraclecloud.com"
ROBOTS_URL = CE_HOST + "/robots.txt"
REST_BASE = CE_HOST + "/hcmRestApi/resources/latest"

# One published CE site (probed 2026-08-28). Kept as a tuple so a second
# site (the novotech situation) is a one-line change.
SITE_NUMBERS = ("JoinAbt",)

LIST_URL = (
    REST_BASE + "/recruitingCEJobRequisitions"
    "?onlyData=true"
    "&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
    "&finder=findReqs;siteNumber={site},limit={limit},sortBy=POSTING_DATES_DESC"
)
DETAIL_URL = (
    REST_BASE + "/recruitingCEJobRequisitionDetails"
    "?expand=all&onlyData=true"
    "&finder=ById;Id=%22{job_id}%22,siteNumber={site}"
)
JOB_URL_TEMPLATE = (
    CE_HOST + "/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"
)

# The whole board was 16 requisitions on 2026-08-28; 500 leaves a wide
# margin and TotalJobsCount is asserted against it.
LIST_PAGE_LIMIT = 500

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# Tiny slow-cadence implementer board (16 open reqs spanning ~3 weeks on
# the probe): a 30-day first window seeds the whole live board.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "abt_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "site_number", "title", "company", "city", "state",
    "country", "sectors", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "vetoed_by", "needs_review", "experience_min_years", "posted_date",
    "valid_through", "description", "job_url", "scraped_at",
]

log = logging.getLogger("abt_scraper")

# ISO alpha-2 -> (country name, dial code): the fleet-vetted table from
# ../undp/ (itself inverted from ../impactpool/). An unknown code exports
# blank name/dial — never guessed.
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

# country name -> (ISO alpha-2, dial code), for the club export, which only
# has the stored country name to work from.
CODE_BY_COUNTRY = {name: (code, dial)
                   for code, (name, dial) in COUNTRY_BY_CODE.items()}

# Abt's PrimaryLocation strings sometimes end in a country ALIAS that
# differs from the ISO table's name — "Kinshasa, DR Congo-Kinshasa" (CD)
# on the 2026-08-28 probe. Aliases keyed by ISO code; lowercased. Without
# the alias the trailing segment would leak into the city column.
COUNTRY_LOCATION_ALIASES = {
    "CD": {"dr congo-kinshasa", "dr congo", "congo-kinshasa"},
    "CG": {"congo-brazzaville"},
    "US": {"usa", "united states of america"},
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


def parse_requisition_list(payload):
    """(requisitions, total_count) from a recruitingCEJobRequisitions body.

    Oracle wraps the search result — including the requisition array — in a
    single-element `items` list.
    """
    items = (payload or {}).get("items") or []
    if not items:
        return [], 0
    result = items[0] or {}
    return (result.get("requisitionList") or []), result.get("TotalJobsCount")


def parse_requisition_detail(payload):
    """The single requisition object of a ...RequisitionDetails body, or {}."""
    items = (payload or {}).get("items") or []
    return items[0] if items and isinstance(items[0], dict) else {}


def parse_iso_date(text):
    """The date half of an Oracle timestamp ("2026-08-24T09:00:00+00:00")."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


def parse_location(primary_location, country_code):
    """(city, state, country_name) from Oracle's PrimaryLocation string.

    "Durham, NC, United States" + US -> ("Durham", "NC", "United States");
    "Kinshasa, DR Congo-Kinshasa" + CD -> ("Kinshasa", "",
    "Democratic Republic of the Congo") — the trailing segment is dropped
    when it repeats the country the ISO code already names, whether by the
    canonical name or a known alias (COUNTRY_LOCATION_ALIASES). The last
    remaining segment becomes the state when more than one is left.
    """
    location = clean_text(primary_location)
    code = clean_text(country_code).upper()
    country = COUNTRY_BY_CODE.get(code, ("", ""))[0]
    aliases = COUNTRY_LOCATION_ALIASES.get(code, set())

    parts = [p.strip() for p in location.split(",") if p.strip()]
    dropped_country = False
    if parts and country and (parts[-1].lower() == country.lower()
                              or parts[-1].lower() in aliases):
        parts = parts[:-1]
        dropped_country = True
    if not parts:
        return "", "", country
    if len(parts) == 1 or not dropped_country:
        # An unrecognised trailing segment is kept as part of the city
        # rather than promoted to a state on a guess.
        return ", ".join(parts), "", country
    return ", ".join(parts[:-1]), parts[-1], country


def build_sectors(detail):
    """Oracle's populated taxonomy tags, joined for the classifier.

    On THIS tenant `Category` ("Program Delivery") and `JobFunction`
    ("Program Operations (Field)") are real, generic org tags — nothing in
    them mislabels a role as health, so they pass through unmodified (the
    skills-veto-is-per-board rule). JobFamily is null tenant-wide.
    """
    tags = [clean_text(detail.get(key))
            for key in ("Category", "JobFunction", "JobFamily")]
    return "; ".join(t for t in tags if t)


def build_description(detail):
    """The posting text, concatenated from Oracle's three body fields.

    On this tenant everything sampled lives in ExternalDescriptionStr, but
    the responsibilities/qualifications fields are still concatenated in
    reading order (the novotech split-body lesson — costs nothing and
    survives a tenant reconfiguration), with ShortDescriptionStr as the
    last resort.
    """
    chunks = [strip_html(detail.get(key) or "")
              for key in ("ExternalDescriptionStr",
                          "ExternalResponsibilitiesStr",
                          "ExternalQualificationsStr")]
    text = " ".join(c for c in chunks if c).strip()
    if not text:
        text = strip_html(detail.get("ShortDescriptionStr") or "")
    return text[:DESCRIPTION_MAX_CHARS]


# ---- experience -------------------------------------------------------------

# Grounded prose extraction, same patterns as the rest of the fleet: never
# inferred, only lifted when the posting states it.
_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?",
               re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
               r"(?:[A-Za-z/&-]+\s+){0,2}experience",
               re.IGNORECASE),
]


def parse_experience_years(description):
    """Stated minimum years of experience, or "" when none is stated."""
    for regex in _EXPERIENCE_RES:
        match = regex.search(description or "")
        if match:
            years = int(match.group(1))
            if 0 < years <= 30:
                return str(years)
    return ""


# ---- language ---------------------------------------------------------------

# Abt has French-speaking country-office footprints (DRC, Madagascar,
# Côte d'Ivoire) and Latin-America work. Sampled postings are English, but
# any French/Spanish-language description is kept AND flagged needs_review
# rather than published untranslated — the undp keep-and-flag stance, with
# its cheap stopword heuristic: high-frequency FR/ES function words in the
# head of the text (English prose contains almost none of them standalone).
_NON_ENGLISH_STOPWORDS = (" le ", " la ", " les ", " des ", " une ", " pour ",
                          " dans ", " el ", " los ", " para ", " con ")
NON_ENGLISH_STOPWORD_THRESHOLD = 8
LANGUAGE_SNIFF_CHARS = 1000


def looks_non_english(description):
    """True when the description opens in French/Spanish, not English."""
    head = " " + clean_text(description)[:LANGUAGE_SNIFF_CHARS].lower() + " "
    hits = sum(head.count(word) for word in _NON_ENGLISH_STOPWORDS)
    return hits >= NON_ENGLISH_STOPWORD_THRESHOLD


# ---- talent pools / EOI -----------------------------------------------------

# Abt posts "Expression of Interest" talent pools and "Technical Advisory
# Panel" calls in the same requisition id space as real vacancies (3 of 16
# on the 2026-08-28 probe). They are real recruiting supply, so an in-scope
# one is kept — but force-flagged needs_review (the undp RFP-title idiom).
_EOI_TITLE_RE = re.compile(
    r"\b(expressions? of interest|eoi|talent pool|advisory panel|"
    r"request for proposals?|rfp|rfq|tender)\b", re.IGNORECASE)


def is_eoi_title(title):
    return bool(_EOI_TITLE_RE.search(title or ""))


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    The scraper never assigns a category itself.
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
    row["vetoed_by"] = verdict["vetoed_by"]
    # A non-English posting or an EOI/talent-pool call is kept but reviewed
    # rather than silently published.
    row["needs_review"] = (verdict["needs_review"]
                           or looks_non_english(row.get("description", ""))
                           or is_eoi_title(row.get("title", "")))
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
                            "Accept": "application/json"})
    return session


def check_robots(session):
    """Verify the Oracle CE host allows the two REST paths.

    The host served no robots.txt when probed (HTTP 404), which means no
    restrictions — the same precedent as the undp/novotech tenants; the
    check still runs so a later-added robots.txt stops the scraper instead
    of being ignored.
    """
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 404:
            log.info("No robots.txt on %s (HTTP 404) — crawling allowed", CE_HOST)
            rp.parse([])
        elif resp.status_code < 400:
            rp.parse(resp.text.splitlines())
        else:
            rp.parse([])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LIST_URL.format(site=SITE_NUMBERS[0], limit=LIST_PAGE_LIMIT),
                DETAIL_URL.format(site=SITE_NUMBERS[0], job_id="0")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch_json(session, url):
    """GET one JSON document with retries/backoff. Returns dict or None."""
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
            return resp.json()
        except ValueError as exc:            # not JSON — a block page etc.
            log.warning("Non-JSON response from %s (%s)", url, exc)
            return None
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(job_id, site_number, detail, posted_date):
    """Rich row from one requisition detail + the listing's posted date."""
    title = clean_text(detail.get("Title"))
    country_code = clean_text(detail.get("PrimaryLocationCountry")).upper()
    city, state, country = parse_location(detail.get("PrimaryLocation"),
                                          country_code)
    description = build_description(detail)

    return {
        "source": SITE,
        "job_id": str(job_id),
        "site_number": site_number,
        "title": title,
        "company": COMPANY_NAME,
        "city": city,
        "state": state,
        "country": country,
        # Populated on this tenant: Category + JobFunction, the classifier's
        # skills signal (generic org tags, nothing misleading).
        "sectors": build_sectors(detail),
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
        "posted_date": (parse_iso_date(posted_date) or
                        parse_iso_date(detail.get("ExternalPostedStartDate"))),
        # Real data on this tenant (application deadlines ~2-4 weeks out).
        "valid_through": parse_iso_date(detail.get("ExternalPostedEndDate")),
        "description": description,
        "job_url": JOB_URL_TEMPLATE.format(site=site_number, job_id=job_id),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the CLUB_COLUMNS contract.

    Everything is derived from stored rich columns so a full-store re-export
    reproduces the same rows. Abt Global is a development implementer /
    NGO-species employer -> company_type "hospital" (the fleet-wide
    convention for non-pharma employers in the club's hospital|pharma
    enum). No salary is published anywhere on the board, so those columns
    stay empty rather than invented.
    """
    country = _blank(r.get("country"))
    code, dial = CODE_BY_COUNTRY.get(country, ("", ""))
    description = _blank(r.get("description"))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": _blank(r.get("city")),
        "company_name": _blank(r.get("company")) or COMPANY_NAME,
        "company_type": "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": description,
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
        "qualification": extract_qualification(description),
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
    }


# ----------------------------------------------------------------------------
# Stores
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


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape Abt Global vacancies from the Oracle HCM "
                    "Candidate Experience API (site JoinAbt).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail records (test runs)")
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

    counters = Counter()
    veto_counts = Counter()
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0

    for site_number in SITE_NUMBERS:
        payload = fetch_json(session, LIST_URL.format(site=site_number,
                                                      limit=LIST_PAGE_LIMIT))
        if payload is None:
            log.error("Could not fetch the %s requisition list — skipping site",
                      site_number)
            counters["list_failed"] += 1
            continue
        requisitions, total = parse_requisition_list(payload)
        log.info("Oracle CE site %s lists %d open requisitions (TotalJobsCount=%s)",
                 site_number, len(requisitions), total)
        if total is not None and len(requisitions) < total:
            log.warning("Only %d of %d requisitions returned at limit=%d — "
                        "RAISE LIST_PAGE_LIMIT, the board has outgrown one page",
                        len(requisitions), total, LIST_PAGE_LIMIT)

        for req in requisitions:          # POSTING_DATES_DESC = newest first
            counters["scanned"] += 1
            job_id = clean_text(req.get("Id"))
            if not job_id:
                continue
            if job_id in known_ids:
                counters["duplicates"] += 1
                continue
            if job_id in out_of_scope_ids:
                counters["skipped_out_of_scope"] += 1
                continue

            # The listing's PostedDate matches the detail's
            # ExternalPostedStartDate, so the window is decided before
            # spending a detail request.
            posted_date = parse_iso_date(req.get("PostedDate"))
            if posted_date and posted_date < cutoff:
                if job_id in seen_old:
                    counters["skipped_old"] += 1
                else:
                    counters["excluded_old"] += 1
                    seen_old.add(job_id)
                    new_seen_old.append({"job_id": job_id,
                                         "posted_date": posted_date})
                continue

            if args.limit is not None and fetched >= args.limit:
                break

            payload = fetch_json(session, DETAIL_URL.format(site=site_number,
                                                            job_id=job_id))
            fetched += 1
            detail = parse_requisition_detail(payload)
            if not detail:
                counters["detail_failed"] += 1
                log.warning("No requisition detail for job_id=%s", job_id)
                continue
            try:
                row = build_row(job_id, site_number, detail, posted_date)
            except Exception as exc:  # never let one job crash the run
                log.warning("Skipping malformed job %s: %s", job_id, exc)
                continue

            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                if row.get("vetoed_by"):
                    veto_counts[row["vetoed_by"]] += 1
                out_of_scope_ids.add(job_id)
                dropped_rows.append(row)
                continue

            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"],
                                   "title": row["title"],
                                   "sectors": row["sectors"],
                                   "country": row["country"],
                                   "sub_category": row["sub_category"]})
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
        total_dropped = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total_dropped)
    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    print("\n===== Run summary =====")
    print("Requisitions scanned:         {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Skipped (known old):          {:>6,}".format(counters["skipped_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail parse failures:        {:>6,}".format(counters["detail_failed"]))
    if veto_counts:
        print("Vetoes among dropped rows:")
        for veto, n in veto_counts.most_common():
            print("    {:<28} {:>4,}".format(veto, n))


if __name__ == "__main__":
    main()
