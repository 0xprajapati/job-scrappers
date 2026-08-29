#!/usr/bin/env python3
"""Scrape Kenvue vacancies from the Workday CXS JSON API.

Data source
-----------
Kenvue is the consumer-health company spun off from Johnson &
Johnson in 2023 — the house of NEUTROGENA, AVEENO, TYLENOL,
LISTERINE and BAND-AID. Its global careers board runs on Workday:
193-194 open postings on 2026-08-28, a small board by fleet standards.
Supply is consumer-goods shaped (brand marketing, sales, supply
chain, finance), with a genuinely in-scope minority in regulatory
affairs, quality, R&D, medical/clinical safety and manufacturing
science — most titles are dropped by the shared classifier.

Posting velocity is modest: offset 0 was "Posted Today", offset 90
"Posted 10 Days Ago" and offset 180 "Posted 30+ Days Ago" when
probed, i.e. roughly 8-10 requisitions a day.

Workday's Candidate Experience Site (CXS) API is public and unauthenticated —
no key, no cookie, no signed parameter (probed live 2026-08-28):

  listing (paged, newest-first):
    POST https://kenvue.wd5.myworkdayjobs.com/wday/cxs/kenvue/<site>/jobs
         {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}

    `limit` is HARD-CAPPED at 20 — asking for more returns HTTP 400. The
    response carries `total` and `jobPostings[]` with title, externalPath,
    locationsText, a *relative* postedOn label ("Posted Today", "Posted 3
    Days Ago", "Posted 30+ Days Ago") and bulletFields (the requisition id).
    `total` is only reliable on the offset=0 page (deeper pages report 0).

  detail (one GET per requisition):
    GET https://kenvue.wd5.myworkdayjobs.com/wday/cxs/kenvue/<site>/job/<externalPath tail>

    `jobPostingInfo` carries the full HTML description (stripped here), the
    exact posted date (`startDate`, YYYY-MM-DD), `timeType`, `jobReqId`,
    the primary `location`, `additionalLocations`, the country descriptor
    with its ISO `alpha2Code`, and the canonical public `externalUrl`.

Requests send an Accept-Language header and a Mozilla-compatible descriptive
User-Agent — some Workday hosts answer 406 to bare non-browser clients.

robots.txt quirk (tenant-specific, probe-verified 2026-08-28)
-------------------------------------------------------------
Unlike the other Workday tenants in this fleet, kenvue.wd5 serves

    User-agent: *
    Disallow: /kenvue/
    Disallow: /refreshFacet/

`Disallow: /kenvue/` covers the human-facing careers UI only. The CXS
API this scraper uses lives under /wday/cxs/kenvue/kenvue/... — a
different path prefix — and robotparser confirms can_fetch() True for
both the listing and the detail URL. The scraper therefore never
requests a disallowed path: the public /kenvue/job/... URL is only ever
*stored* as job_url for humans to click, never fetched. check_robots()
enforces this per-URL at startup, so if Kenvue ever widens the rule the
run aborts instead of ignoring it.

Location-format quirk (tenant adaptation in parse_city)
-------------------------------------------------------
Kenvue's locationsText is a four-segment macro-region string:

    "Europe/Middle East/Africa, Spain, Community of Madrid, Madrid"
    "Asia Pacific, India, Karnataka, Bangalore"

i.e. <region>, <country>, <state/province>, <city> — the city is LAST,
where the rest of the fleet puts it first. parse_city() therefore returns
the LAST surviving segment when three or more survive the skip filter
(dropping the country always leaves 3+ on this form), and the fleet's
first-survivor rule still applies to two-segment forms. Without it `city`
would be the macro-region ("Asia Pacific"); skipping only the region would
give the state ("Karnataka"). The sibling msd scraper carries the same
last-survivor rule for its "country - state - city" shape.

country / country_code / dial code never depend on this — they come from
the detail's ISO alpha2Code.

Crawl strategy
--------------
The listing is walked newest-first per Workday site. The relative postedOn
label decides the time window BEFORE any detail request is spent: a posting
older than the cutoff is recorded in seen_old_ids.csv and never fetched. The
authoritative posted_date is the detail's `startDate`; the window is
re-checked after the detail fetch (the relative label has day granularity).

Small boards (total <= EXHAUSTIVE_BOARD_MAX postings) are walked completely —
their default ordering is *mostly* newest-first but can pin stray old rows at
the top, so early-stopping would be unsound and walking everything costs only
a handful of listing pages. Large boards are early-stopped once
CONSECUTIVE_OLD_PAGES_STOP consecutive pages contain nothing inside the time
window, with a DEFAULT_MAX_PAGES hard cap per site as a backstop.

Kenvue's 193 postings sit under EXHAUSTIVE_BOARD_MAX, so this board takes
the exhaustive path and the early stop never engages. Ordering was checked
anyway (2026-08-28): offset 0 "Posted Today", offset 90 "Posted 10 Days
Ago", offset 180 "Posted 30+ Days Ago" — labels age monotonically with
offset, so newest-first holds and the early stop would be sound if the
board ever grew past the threshold.

Classification (shared taxonomy)
--------------------------------
Fetch wide, filter tight: every in-window posting is fetched and the
keep/drop and labeling decision belongs entirely to the ONE shared
classifier, _shared/classification.classify_job(title, skills, description).
Workday CXS exposes no curated role/category field on this tenant (no
jobFamily, no skills array), so `skills` is passed empty; the description is
the HTML-stripped detail text. in_scope False -> DROPPED, counted
excluded_out_of_scope, full row archived to out-of-scope.csv. needs_review
True keeps the row AND appends it to needs_review.csv. A non-English
(FR/ES/DE) description is kept but forced into review rather than silently
published untranslated. This scraper never assigns a category itself.

Salary: Workday postings on these boards never show pay -> salary_raw
"Not Disclosed", numeric fields empty, never invented (master spec §3).

Outputs
-------
* kenvue_jobs.csv — rich cumulative store (dedup key: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/kenvue.csv — the same jobs in CLUB_COLUMNS.
* seen_old_ids.csv — out-of-window ids and their (estimated) dates.
* out-of-scope.csv — dropped rows + their skip list (reversible).
* needs_review.csv — kept but flagged.

Time window: first run keeps postings from the last INITIAL_WINDOW_DAYS (7);
later runs keep only those newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

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

import pandas as pd
import requests

# The one shared classifier (see ../../instructions/master-scraper-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration — the only block that differs between the Workday scrapers
# ----------------------------------------------------------------------------

SITE = "kenvue"
COMPANY_NAME = "Kenvue"
COMPANY_TYPE = "pharma"          # club enum: hospital | pharma
HOST = "https://kenvue.wd5.myworkdayjobs.com"
TENANT = "kenvue"
WORKDAY_SITES = ['kenvue']       # note: lowercase site slug on this tenant

# ----------------------------------------------------------------------------
# Shared Workday CXS implementation (identical across the four scrapers)
# ----------------------------------------------------------------------------

ROBOTS_URL = HOST + "/robots.txt"
LIST_URL = HOST + "/wday/cxs/" + TENANT + "/{site}/jobs"
DETAIL_URL = HOST + "/wday/cxs/" + TENANT + "/{site}{external_path}"
PUBLIC_JOB_URL = HOST + "/{site}{external_path}"

LIST_PAGE_LIMIT = 20            # HARD Workday cap — limit > 20 answers HTTP 400
EXHAUSTIVE_BOARD_MAX = 200      # boards this small are walked completely
CONSECUTIVE_OLD_PAGES_STOP = 2  # early stop for big, newest-first boards
DEFAULT_MAX_PAGES = 40          # per Workday site, hard safety cap

USER_AGENT = ("Mozilla/5.0 (compatible; HealthCareersJobScraper/1.0; "
              "+https://github.com/0xprajapati/job-scrappers)")
ACCEPT_LANGUAGE = "en-US,en;q=0.9"

INITIAL_WINDOW_DAYS = 7
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "kenvue_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "workday_site", "title", "company", "hiring_org",
    "city", "country", "country_code", "location", "additional_locations",
    "time_type", "job_type", "salary_raw", "salary_min_monthly",
    "salary_max_monthly", "salary_period_original", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in", "needs_review",
    "experience_min_years", "posted_date", "description", "job_url",
    "scraped_at",
]

log = logging.getLogger(SITE + "_scraper")

# ISO alpha-2 -> (country name, dial code). Same table the undp/impactpool
# scrapers carry, extended with duty stations these global CRO/RCM boards
# post in that the UN universe did not cover. An unknown code exports blank
# name/dial — never guessed.
COUNTRY_BY_CODE = {
    "AE": ("United Arab Emirates", "+971"), "AF": ("Afghanistan", "+93"),
    "AL": ("Albania", "+355"), "AM": ("Armenia", "+374"),
    "AO": ("Angola", "+244"), "AR": ("Argentina", "+54"),
    "AT": ("Austria", "+43"), "AU": ("Australia", "+61"),
    "AZ": ("Azerbaijan", "+994"), "BA": ("Bosnia and Herzegovina", "+387"),
    "BB": ("Barbados", "+1"), "BD": ("Bangladesh", "+880"),
    "BE": ("Belgium", "+32"), "BF": ("Burkina Faso", "+226"),
    "BG": ("Bulgaria", "+359"), "BH": ("Bahrain", "+973"),
    "BI": ("Burundi", "+257"), "BJ": ("Benin", "+229"),
    "BO": ("Bolivia", "+591"), "BR": ("Brazil", "+55"),
    "BW": ("Botswana", "+267"), "BY": ("Belarus", "+375"),
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
    "IS": ("Iceland", "+354"), "IT": ("Italy", "+39"),
    "JM": ("Jamaica", "+1"), "JO": ("Jordan", "+962"), "JP": ("Japan", "+81"),
    "KE": ("Kenya", "+254"), "KG": ("Kyrgyzstan", "+996"),
    "KH": ("Cambodia", "+855"), "KI": ("Kiribati", "+686"),
    "KR": ("South Korea", "+82"), "KW": ("Kuwait", "+965"),
    "KZ": ("Kazakhstan", "+7"), "LA": ("Laos", "+856"),
    "LB": ("Lebanon", "+961"), "LK": ("Sri Lanka", "+94"),
    "LR": ("Liberia", "+231"), "LS": ("Lesotho", "+266"),
    "LT": ("Lithuania", "+370"), "LU": ("Luxembourg", "+352"),
    "LV": ("Latvia", "+371"), "LY": ("Libya", "+218"),
    "MA": ("Morocco", "+212"), "MD": ("Moldova", "+373"),
    "ME": ("Montenegro", "+382"), "MG": ("Madagascar", "+261"),
    "MK": ("North Macedonia", "+389"), "ML": ("Mali", "+223"),
    "MM": ("Myanmar", "+95"), "MN": ("Mongolia", "+976"),
    "MR": ("Mauritania", "+222"), "MT": ("Malta", "+356"),
    "MU": ("Mauritius", "+230"), "MW": ("Malawi", "+265"),
    "MX": ("Mexico", "+52"), "MY": ("Malaysia", "+60"),
    "MZ": ("Mozambique", "+258"), "NA": ("Namibia", "+264"),
    "NE": ("Niger", "+227"), "NG": ("Nigeria", "+234"),
    "NI": ("Nicaragua", "+505"), "NL": ("Netherlands", "+31"),
    "NO": ("Norway", "+47"), "NP": ("Nepal", "+977"),
    "NZ": ("New Zealand", "+64"), "OM": ("Oman", "+968"),
    "PA": ("Panama", "+507"), "PE": ("Peru", "+51"),
    "PG": ("Papua New Guinea", "+675"), "PH": ("Philippines", "+63"),
    "PK": ("Pakistan", "+92"), "PL": ("Poland", "+48"),
    "PR": ("Puerto Rico", "+1"), "PS": ("Palestine", "+970"),
    "PT": ("Portugal", "+351"), "PY": ("Paraguay", "+595"),
    "QA": ("Qatar", "+974"), "RO": ("Romania", "+40"),
    "RS": ("Serbia", "+381"), "RU": ("Russia", "+7"),
    "RW": ("Rwanda", "+250"), "SA": ("Saudi Arabia", "+966"),
    "SB": ("Solomon Islands", "+677"), "SD": ("Sudan", "+249"),
    "SE": ("Sweden", "+46"), "SG": ("Singapore", "+65"),
    "SI": ("Slovenia", "+386"), "SK": ("Slovakia", "+421"),
    "SL": ("Sierra Leone", "+232"), "SN": ("Senegal", "+221"),
    "SO": ("Somalia", "+252"), "SS": ("South Sudan", "+211"),
    "SV": ("El Salvador", "+503"), "SY": ("Syria", "+963"),
    "SZ": ("Eswatini", "+268"), "TD": ("Chad", "+235"),
    "TG": ("Togo", "+228"), "TH": ("Thailand", "+66"),
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

CODE_BY_COUNTRY = {name.lower(): (code, dial)
                   for code, (name, dial) in COUNTRY_BY_CODE.items()}

# Workday country descriptors that differ from the table's canonical names.
COUNTRY_NAME_FIXUPS = {
    "united states of america": "United States",
    "korea, republic of": "South Korea",
    "russian federation": "Russia",
    "viet nam": "Vietnam",
    "czech republic": "Czechia",
    "türkiye": "Turkey",
    "hong kong s.a.r.": "Hong Kong",
    "taiwan, china": "Taiwan",
}

# Aliases dropped from location strings when hunting for the city segment.
_COUNTRY_ALIASES = {"us", "usa", "u.s.", "u.s.a.", "united states",
                    "united states of america", "uk", "united kingdom"}

# Kenvue prefixes every location with one of Workday's four macro-regions.
# Never a city, and their presence in segment 0 marks the tenant's
# "<macro-region>, <country>, <state>, <city>" shape (city LAST).
_MACRO_REGIONS = {"asia pacific", "europe/middle east/africa",
                  "latin america", "north america"}

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


def parse_iso_date(text):
    """The date half of "2026-08-27" / "2026-08-27T04:58:17+00:00", or ""."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


# ---- listing ----------------------------------------------------------------

def parse_listing(payload):
    """(jobPostings, total) from one CXS /jobs response body.

    `total` is only trustworthy on the offset=0 page — Workday reports 0 on
    deeper offsets (probe-verified on IQVIA), so callers latch the first
    non-zero value.
    """
    payload = payload or {}
    return (payload.get("jobPostings") or []), (payload.get("total") or 0)


def listing_job_id(posting):
    """The requisition id: bulletFields[0], else the externalPath tail."""
    for bullet in (posting.get("bulletFields") or []):
        bullet = clean_text(bullet)
        if bullet:
            return bullet
    path = clean_text(posting.get("externalPath"))
    if "_" in path:
        return path.rsplit("_", 1)[-1]
    return path.rsplit("/", 1)[-1] if path else ""


_POSTED_DAYS_RE = re.compile(r"(\d+)\s*\+?\s*day", re.IGNORECASE)


def parse_posted_on(text, today=None):
    """Estimate a date from Workday's relative postedOn label, or None.

    "Posted Today" -> today, "Posted Yesterday" -> today-1, "Posted 3 Days
    Ago" -> today-3, "Posted 30+ Days Ago" -> today-30 (a floor — genuinely
    older; every window this scraper uses treats it as out of window).
    An unrecognised label returns None: the caller must treat that as
    in-window so the detail's exact startDate can decide.
    """
    text = clean_text(text).lower()
    if not text:
        return None
    today = today or date.today()
    if "today" in text or "just posted" in text:
        return today
    if "yesterday" in text:
        return today - timedelta(days=1)
    match = _POSTED_DAYS_RE.search(text)
    if match:
        return today - timedelta(days=int(match.group(1)))
    return None


# ---- location ---------------------------------------------------------------

_REMOTE_RE = re.compile(r"\bremote\b|home[\s-]?based|work\s+from\s+home",
                        re.IGNORECASE)


def is_remote(*texts):
    return any(_REMOTE_RE.search(t or "") for t in texts)


def parse_country(detail_info):
    """(country_name, alpha2) from a jobPostingInfo block.

    The ISO code lives on jobRequisitionLocation.country.alpha2Code; the
    top-level country carries only the descriptor. Descriptors are folded to
    the table's canonical names ("United States of America" -> "United
    States") so the club export round-trips. Unknown stays as-is with a
    blank code — never guessed.
    """
    info = detail_info or {}
    descriptor = clean_text(((info.get("country") or {}).get("descriptor")))
    alpha2 = clean_text((((info.get("jobRequisitionLocation") or {})
                          .get("country") or {}).get("alpha2Code"))).upper()
    name = COUNTRY_NAME_FIXUPS.get(descriptor.lower(), descriptor)
    if alpha2 in COUNTRY_BY_CODE:
        return COUNTRY_BY_CODE[alpha2][0], alpha2
    code, _ = CODE_BY_COUNTRY.get(name.lower(), ("", ""))
    return name, code


def parse_city(location, country_name, alpha2=""):
    """Best-effort city from a Workday location string.

    These boards separate segments with commas ("Dallas, TX") or hyphens
    ("Germany-Berlin-Remote", "US - Remote"); segments naming the country,
    its aliases, its ISO code or a remote marker are dropped and the first
    remaining segment is the city ("" when nothing survives — e.g. a
    country-only or remote-only string).

    Kenvue tenant adaptation: every location on this board is the
    four-segment form "<macro-region>, <country>, <state/province>, <city>"
    ("Europe/Middle East/Africa, Spain, Community of Madrid, Madrid",
    "Asia Pacific, India, Karnataka, Bangalore" — probe-verified 2026-08-28),
    where the city is the LAST segment. Taking the first survivor would
    publish the macro-region ("Asia Pacific") as the city, and skipping only
    the region would publish the state ("Karnataka"). The shape is detected
    by segment 0 being one of Workday's four macro-regions, and only then is
    the LAST survivor taken; every other format keeps the fleet's
    first-survivor rule. The sibling msd scraper carries the same
    last-survivor rule for its "country - state - city" shape, detected the
    same way (a test on segment 0).

    The shape test is deliberately not a survivor count: dropping the
    country can collapse two segments at once — "Asia Pacific, Hong Kong,
    Hong Kong, Mongkok" and "North America, United States, Puerto Rico, Las
    Piedras" leave only two survivors — and a count gate would fall back to
    the first survivor and re-publish the macro-region.
    """
    location = clean_text(location)
    if not location:
        return ""
    drop = set(_COUNTRY_ALIASES)
    if country_name:
        drop.add(country_name.lower())
    if alpha2:
        drop.add(alpha2.lower())
    segments = [p.strip() for p in re.split(r"[,\-–]", location)]
    # "<macro-region>, <country>, <state>, <city>" — the city is LAST
    region_form = bool(segments) and segments[0].lower() in _MACRO_REGIONS
    if region_form:
        # This form is comma-delimited, so split on commas only: the bare
        # hyphen split would cut city names like "Val-de-Reuil" into pieces.
        segments = [p.strip() for p in location.split(",")]
    survivors = []
    for part in segments:
        if not part:
            continue
        low = part.lower()
        if low in drop or _REMOTE_RE.fullmatch(part):
            continue
        if re.fullmatch(r"\d+\s+locations?", low):
            continue
        # boards prefix locations with ISO codes / US state codes — never a city
        if re.fullmatch(r"[A-Z]{2,3}", part):
            continue
        if low in _MACRO_REGIONS:
            continue
        survivors.append(part)
    if not survivors:
        return ""
    return survivors[-1] if region_form else survivors[0]


def parse_job_type(time_type, remote):
    """Club enum full_time|part_time|remote from Workday's timeType."""
    if remote:
        return "remote"
    if "part" in clean_text(time_type).lower():
        return "part_time"
    return "full_time"


# ---- experience -------------------------------------------------------------

# Grounded prose patterns, same as the rest of the fleet: never inferred,
# only lifted when the posting states it.
_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?",
               re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
               r"(?:relevant\s+|work(?:ing)?\s+|professional\s+|prior\s+)?experience",
               re.IGNORECASE),
]


def parse_experience_years(description):
    for regex in _EXPERIENCE_RES:
        match = regex.search(description or "")
        if match:
            years = int(match.group(1))
            if 0 < years <= 30:
                return str(years)
    return ""


# ---- language ---------------------------------------------------------------

# Global boards post some vacancies wholly in German, French or Spanish and
# expose no English variant. Those rows are KEPT (never dropped) but flagged
# needs_review so no untranslated description is silently published — the
# UNDP-01 convention. Detection is a cheap stopword count over the head of
# the text; English prose contains almost none of these as standalone words.
_NON_ENGLISH_STOPWORDS = (
    " le ", " la ", " les ", " des ", " une ", " pour ", " dans ",   # FR
    " el ", " los ", " para ", " con ",                              # ES
    " der ", " und ", " für ", " eine ", " nicht ", " werden ",      # DE
)
NON_ENGLISH_STOPWORD_THRESHOLD = 8
LANGUAGE_SNIFF_CHARS = 1000


def looks_non_english(description):
    head = " " + clean_text(description)[:LANGUAGE_SNIFF_CHARS].lower() + " "
    hits = sum(head.count(word) for word in _NON_ENGLISH_STOPWORDS)
    return hits >= NON_ENGLISH_STOPWORD_THRESHOLD


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    Workday CXS has no curated role/category field on this tenant, so
    `skills` is empty — title and description carry the whole signal.
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
    row["needs_review"] = (verdict["needs_review"] or
                           looks_non_english(row.get("description", "")))
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
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        # Some Workday hosts answer 406 without an Accept-Language header.
        "Accept-Language": ACCEPT_LANGUAGE,
    })
    return session


def check_robots(session):
    """Verify HOST's robots.txt allows the CXS API paths; abort otherwise.

    myworkdayjobs.com hosts serve a robots.txt that disallows only
    /refreshFacet/; wd1.myworkdaysite.com answers HTTP 422 (no robots =>
    no restrictions). Any non-2xx is treated as no-robots, but a served
    file is honored per-URL so a later-added Disallow stops the scraper
    instead of being ignored.
    """
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        # The session's Accept: application/json makes Workday answer 406
        # for the (existing) robots.txt — ask for it as plain text.
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS,
                           headers={"Accept": "text/plain, */*"})
        if resp.status_code >= 400:
            log.info("No robots.txt on %s (HTTP %d) — crawling allowed",
                     HOST, resp.status_code)
            rp.parse([])
        else:
            rp.parse(resp.text.splitlines())
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for site in WORKDAY_SITES:
        for url in (LIST_URL.format(site=site),
                    DETAIL_URL.format(site=site, external_path="/job/x/y")):
            if not rp.can_fetch(USER_AGENT, url):
                sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed for %s", HOST)


def _request_json(session, method, url, json_body=None):
    """One JSON request with retries/backoff. Returns dict or None."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.request(method, url, json=json_body,
                                   timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except ValueError as exc:              # not JSON — a block page etc.
            log.warning("Non-JSON response from %s (%s)", url, exc)
            return None
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_listing_page(session, site, offset):
    body = {"appliedFacets": {}, "limit": LIST_PAGE_LIMIT,
            "offset": offset, "searchText": ""}
    return _request_json(session, "POST", LIST_URL.format(site=site), body)


def fetch_detail(session, site, external_path):
    url = DETAIL_URL.format(site=site, external_path=external_path)
    return _request_json(session, "GET", url)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(site, job_id, posting, detail_payload, today=None):
    """Rich row from one listing posting + its detail payload."""
    detail_payload = detail_payload or {}
    info = detail_payload.get("jobPostingInfo") or {}
    hiring_org = clean_text((detail_payload.get("hiringOrganization") or {})
                            .get("name"))

    title = clean_text(info.get("title")) or clean_text(posting.get("title"))
    location = clean_text(info.get("location")) or \
        clean_text(posting.get("locationsText"))
    additional = [clean_text(l) for l in (info.get("additionalLocations") or [])]
    country_name, alpha2 = parse_country(info)

    remote = is_remote(location, clean_text(posting.get("locationsText")),
                       *additional)
    city = parse_city(location, country_name, alpha2)
    if not city and remote:
        # himalayas convention: a remote row with no real locality says so.
        city = "Remote"

    description = strip_html(info.get("jobDescription") or "")[:DESCRIPTION_MAX_CHARS]
    time_type = clean_text(info.get("timeType"))

    external_path = clean_text(posting.get("externalPath"))
    job_url = clean_text(info.get("externalUrl")) or \
        PUBLIC_JOB_URL.format(site=site, external_path=external_path)

    posted_date = parse_iso_date(info.get("startDate"))
    if not posted_date:
        estimated = parse_posted_on(posting.get("postedOn"), today)
        posted_date = estimated.isoformat() if estimated else ""

    return {
        "source": SITE,
        "job_id": str(job_id),
        "workday_site": site,
        "title": title,
        "company": COMPANY_NAME,
        # The Workday legal entity ("Novasyte LLC (USA7)") — kept raw; the
        # brand the board belongs to is what the club export carries.
        "hiring_org": hiring_org,
        "city": city,
        "country": country_name,
        "country_code": alpha2,
        "location": clean_text(posting.get("locationsText")) or location,
        "additional_locations": "; ".join(l for l in additional if l),
        "time_type": time_type,
        "job_type": parse_job_type(time_type, remote),
        # Workday postings on these boards never show pay (master spec §3).
        "salary_raw": "Not Disclosed",
        "salary_min_monthly": "",
        "salary_max_monthly": "",
        "salary_period_original": "",
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
        "posted_date": posted_date,
        "description": description,
        "job_url": job_url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the CLUB_COLUMNS contract.

    Everything derives from stored rich columns so a full-store re-export
    reproduces the same rows. Salary columns stay empty — these boards
    disclose nothing and nothing is ever invented.
    """
    country = _blank(r.get("country"))
    code = _blank(r.get("country_code")).upper()
    if not code:
        code = CODE_BY_COUNTRY.get(country.lower(), ("", ""))[0]
    dial = COUNTRY_BY_CODE.get(code, ("", ""))[1]
    description = _blank(r.get("description"))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": _blank(r.get("city")),
        "company_name": _blank(r.get("company")) or COMPANY_NAME,
        "company_type": COMPANY_TYPE,
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": description,
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _blank(r.get("experience_min_years")),
        "max_experience": "",
        # no structured qualification field on Workday CXS — grounded
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
# Crawl
# ----------------------------------------------------------------------------

def crawl_site(session, site, cutoff, state, args):
    """Walk one Workday site's listing newest-first inside the time window.

    Mutates `state` (shared across sites): known_ids, seen_old,
    out_of_scope_ids, counters, new_rows, review_log, new_seen_old,
    dropped_rows, fetched. Returns False when the run-wide --limit was hit.
    """
    counters = state["counters"]
    offset, page_no, total = 0, 0, None
    pages_without_in_window = 0
    max_pages = args.max_pages or DEFAULT_MAX_PAGES

    while True:
        if page_no >= max_pages:
            log.info("[%s] page cap (%d) reached — stopping", site, max_pages)
            break
        payload = fetch_listing_page(session, site, offset)
        if payload is None:
            log.error("[%s] listing page at offset %d failed — stopping this "
                      "site", site, offset)
            break
        postings, page_total = parse_listing(payload)
        if total is None and page_total:
            total = page_total
            log.info("[%s] board lists %d open postings", site, total)
        if not postings:
            log.info("[%s] empty page at offset %d — done", site, offset)
            break
        page_no += 1
        page_had_in_window = False

        for posting in postings:
            counters["scanned"] += 1
            job_id = listing_job_id(posting)
            if not job_id:
                continue
            estimated = parse_posted_on(posting.get("postedOn"))
            in_window = estimated is None or estimated.isoformat() >= cutoff
            if in_window:
                page_had_in_window = True
            if job_id in state["known_ids"]:
                counters["duplicates"] += 1
                continue
            if job_id in state["out_of_scope_ids"]:
                counters["skipped_out_of_scope"] += 1
                continue
            if not in_window:
                if job_id in state["seen_old"]:
                    counters["skipped_old"] += 1
                else:
                    counters["excluded_old"] += 1
                    state["seen_old"].add(job_id)
                    state["new_seen_old"].append(
                        {"job_id": job_id,
                         "posted_date": estimated.isoformat()})
                continue

            if args.limit is not None and state["fetched"] >= args.limit:
                log.info("--limit %d reached — stopping the crawl", args.limit)
                return False

            detail = fetch_detail(session, site,
                                  clean_text(posting.get("externalPath")))
            state["fetched"] += 1
            if not detail or not detail.get("jobPostingInfo"):
                counters["detail_failed"] += 1
                log.warning("[%s] no detail for job_id=%s", site, job_id)
                continue
            try:
                row = build_row(site, job_id, posting, detail)
            except Exception as exc:  # one bad job never crashes the run (§7)
                log.warning("[%s] skipping malformed job %s: %s",
                            site, job_id, exc)
                continue

            # The exact startDate is authoritative; the relative label that
            # admitted the posting has only day granularity.
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                state["seen_old"].add(job_id)
                state["new_seen_old"].append(
                    {"job_id": job_id, "posted_date": row["posted_date"]})
                continue

            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                state["out_of_scope_ids"].add(job_id)
                state["dropped_rows"].append(row)
                continue

            if row["needs_review"]:
                counters["needs_review"] += 1
                state["review_log"].append(
                    {"job_id": row["job_id"], "title": row["title"],
                     "workday_site": site, "country": row["country"],
                     "sub_category": row["sub_category"]})
            state["known_ids"].add(job_id)
            state["new_rows"].append(row)
            counters["new"] += 1

        offset += LIST_PAGE_LIMIT
        if total is not None and offset >= total:
            log.info("[%s] walked the whole board (%d postings)", site, total)
            break
        if total is not None and total > EXHAUSTIVE_BOARD_MAX:
            # Big board: newest-first ordering holds (probe-verified), so
            # consecutive fully-old pages mean the window has closed.
            pages_without_in_window = (0 if page_had_in_window
                                       else pages_without_in_window + 1)
            if pages_without_in_window >= CONSECUTIVE_OLD_PAGES_STOP:
                log.info("[%s] %d consecutive pages outside the window — "
                         "stopping at offset %d", site,
                         pages_without_in_window, offset)
                break
    return True


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape {} vacancies from the Workday CXS API.".format(
            COMPANY_NAME))
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="listing pages per Workday site "
                             "(default: {})".format(DEFAULT_MAX_PAGES))
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N detail records (test runs)")
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
    cutoff = compute_cutoff(existing_df, since=args.since)
    state = {
        "known_ids": known_ids,
        "seen_old": load_id_column(SEEN_OLD_CSV),
        "out_of_scope_ids": load_id_column(OUT_OF_SCOPE_CSV),
        "counters": {"scanned": 0, "duplicates": 0, "skipped_old": 0,
                     "skipped_out_of_scope": 0, "excluded_old": 0,
                     "excluded_out_of_scope": 0, "needs_review": 0, "new": 0,
                     "detail_failed": 0},
        "new_rows": [], "review_log": [], "new_seen_old": [],
        "dropped_rows": [], "fetched": 0,
    }
    log.info("Existing CSV has %d known jobs (%d known-old, %d known "
             "out-of-scope skipped); keeping jobs posted on/after %s%s",
             len(known_ids), len(state["seen_old"]),
             len(state["out_of_scope_ids"]), cutoff,
             " (--since override)" if args.since else "")

    for site in WORKDAY_SITES:
        if not crawl_site(session, site, cutoff, state, args):
            break                              # run-wide --limit reached

    counters = state["counters"]

    # ---- rich cumulative CSV (source of truth) ----
    if state["new_rows"]:
        new_df = pd.DataFrame(state["new_rows"], dtype=str)
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
    if state["new_seen_old"]:
        old_df = pd.DataFrame(state["new_seen_old"])
        try:
            old_df = pd.concat([pd.read_csv(SEEN_OLD_CSV, dtype=str), old_df],
                               ignore_index=True)
        except FileNotFoundError:
            pass
        old_df.drop_duplicates(subset="job_id").to_csv(SEEN_OLD_CSV, index=False)
    if state["dropped_rows"]:
        total_dropped = append_out_of_scope(state["dropped_rows"])
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(state["dropped_rows"]), OUT_OF_SCOPE_CSV, total_dropped)
    if state["review_log"]:
        pd.DataFrame(state["review_log"]).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(state["review_log"]))

    print("\n===== Run summary =====")
    print("Postings scanned:             {:>6,}".format(counters["scanned"]))
    print("Detail records fetched:       {:>6,}".format(state["fetched"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Skipped (known old):          {:>6,}".format(counters["skipped_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail failures:              {:>6,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
