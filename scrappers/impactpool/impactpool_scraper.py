#!/usr/bin/env python3
"""Scrape impact-sector public-health jobs from impactpool.org.

Why this source
---------------
Impactpool aggregates vacancies from UN agencies, multilateral banks and the
big international NGOs (UNICEF, WHO, World Bank, CHAI, UNOPS, ...) — the same
employer universe as ReliefWeb but with its own supply, dense in the Public
Health side of the taxonomy (health specialists, epidemiologists, nutrition
and WASH officers, M&E, health financing). Probed 2026-08-26.

Data source
-----------
Server-rendered HTML — no JSON anywhere (``/jobs/<id>.json`` and
``/search.json`` both answer 406, there is no JSON-LD, and the sitemap holds
only articles). Verified 2026-08-26:

* Listing: ``GET /search?per_page=100&page=N`` (a plain stock-UA fetch works).
  ``q`` exists but is NOT used — the unfiltered search lists the ENTIRE live
  board (~3,700 jobs / 37 pages at per_page=100). Each card is a
  ``<div class='job'>`` with the job id in its ``/jobs/<id>`` link and three
  color-coded fields: organization (#1C1B16 + bodyEmphasis), location
  (#63625B), grade/level (#75736C). Any field can be absent.
* Order: job id DESCENDING ~= newest first. Page 1 mixes in a few promoted
  older cards (verified: min id 1225276 among 1233xxx); pages 2+ are strictly
  descending. Dedup + the stop rule below absorb the promoted cards.
* Detail: ``GET /jobs/<id>`` — h1 title, an ``Application deadline:
  <Month D, YYYY> (N days)`` line, an Impactpool-written summary +
  "Candidate Requirements" card, and the full JD inside
  ``<div class='main-content'>`` (extracted by <div> depth balancing).
* There is NO posted date anywhere (cards carry only a "New" badge, details
  carry only the deadline), so posted_date/posted_at stay EMPTY — dates are
  never invented (master spec §6) — and the incremental crawl keys on the
  descending job id instead of a date watermark (see below).
* Location is a "|"-separated mix of cities and countries ("Islamabad |
  Pakistan", "Remote | Nigeria | Kenya", plain "Pretoria"). Tokens matching a
  country name become the country; known duty-station cities map to their
  country via DUTY_STATION_COUNTRIES; anything else stays city-only with the
  country blank rather than guessed.

robots.txt allows everything (only two Google ad bots are given explicit
Allow rules; there is no Disallow for ``*``). Checked at startup anyway.

Incremental crawl without dates
-------------------------------
known ids = rich store + out-of-scope.csv. List pages are crawled newest
first and the crawl stops at the first page that yields ZERO unseen ids —
correct because new ids only ever appear at the top (id-descending order;
page-1 promoted cards are old ids that are already known). Each unseen id's
detail page is fetched EXACTLY ONCE, ever: in-scope rows land in the rich
store, out-of-scope rows land in out-of-scope.csv (full rows — reversible,
and the skip list that prevents refetching). Detail failures are NOT
recorded, so they retry next run. First run walks the whole live board
(~37 list pages + ~3.7k detail fetches ≈ 1.5 h at the 1 s delay); steady
state is 1-2 list pages + details for the day's new postings.

Fetch wide, filter tight
------------------------
The crawl asks the site for EVERYTHING (the unfiltered search — no ``q``
keyword, no source facet) and every candidate is routed through the one
shared classifier ``_shared/classification.classify_job(title, skills,
description)``. The site has no curated role/sector field, so ``skills`` is
empty; the description signal is the real JD text plus Impactpool's own
summary/requirements card. ``in_scope == False`` rows are moved to
out-of-scope.csv and counted ``excluded_out_of_scope``; ``needs_review``
rows are kept AND flagged into needs_review.csv — never a silent drop.

Salary (master spec §3: capture, don't filter, don't invent)
------------------------------------------------------------
Neither cards nor detail pages expose salary — the club salary columns are
always empty. Grade strings ("P-4, International Professional ...") are kept
verbatim in the rich CSV (``grade``), never converted to pay or experience.

Outputs (per the repo README + master-scraper-spec.md)
------------------------------------------------------
* impactpool_jobs.csv       — rich cumulative store (dedup key: job_id).
* out-of-scope.csv          — dropped rows, full detail (skip list + review).
* needs_review.csv          — flagged in-scope titles.
* ../../jobs_csv/<DD-MM-YYYY>/impactpool.csv
                            — the same jobs mapped to the shared
                              HealthCareers.club CLUB_COLUMNS schema.

Run ``python impactpool_scraper.py --help`` for options.
"""

import argparse
import html as html_lib
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# The one shared classifier + club contract (taxonomy migration 2026-08-25).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "impactpool"
SITE_BASE = "https://www.impactpool.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SEARCH_URL = SITE_BASE + "/search"
JOB_URL_TEMPLATE = SITE_BASE + "/jobs/{}"

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

PER_PAGE = 100          # verified: per_page=100 is honored (default is 40)
# Safety ceiling only — the real stop is the first page with zero unseen ids
# (or zero cards). The whole board was 37 pages on 2026-08-26.
MAX_PAGES = 60

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
# Detail JDs run long (UNICEF example ~17k of HTML). Safety valve against a
# pathological row, not a content budget.
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "impactpool_jobs.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# posted_date exists in the schema for spec-conformance but is ALWAYS empty:
# the site exposes no posted date and dates are never invented.
RICH_COLUMNS = [
    "source", "job_id", "title", "company", "locations", "city", "country",
    "remote", "grade", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "needs_review", "company_type", "posted_date", "closing_date",
    "ip_summary", "description", "job_url", "scraped_at",
]

NEEDS_REVIEW_COLUMNS = ["job_id", "title", "company", "category",
                        "sub_category", "role_family", "matched_in",
                        "locations", "grade"]

# Impact-sector shorthand seen in card location strings -> the plain name
# the code table keys on.
COUNTRY_ALIASES = {
    "CAR": "Central African Republic",
    "DRC": "Democratic Republic of the Congo",
    "DR Congo": "Democratic Republic of the Congo",
    "Ivory Coast": "Côte d'Ivoire",
    "Lao PDR": "Laos",
    "oPt": "Palestine",
    "Türkiye": "Turkey",
    "UAE": "United Arab Emirates",
    "UK": "United Kingdom",
    "USA": "United States",
    "United States of America": "United States",
    "Viet Nam": "Vietnam",
}

# country name -> (ISO alpha-2, dial code). Covers the countries that dominate
# the UN/NGO duty-station universe; anything unseen exports with empty
# code/dial code (never guessed).
COUNTRY_CODES = {
    "Afghanistan": ("AF", "+93"), "Albania": ("AL", "+355"),
    "Algeria": ("DZ", "+213"), "Angola": ("AO", "+244"),
    "Argentina": ("AR", "+54"), "Armenia": ("AM", "+374"),
    "Australia": ("AU", "+61"), "Austria": ("AT", "+43"),
    "Azerbaijan": ("AZ", "+994"), "Bangladesh": ("BD", "+880"),
    "Belgium": ("BE", "+32"), "Benin": ("BJ", "+229"),
    "Bolivia": ("BO", "+591"), "Bosnia and Herzegovina": ("BA", "+387"),
    "Botswana": ("BW", "+267"), "Brazil": ("BR", "+55"),
    "Bulgaria": ("BG", "+359"), "Burkina Faso": ("BF", "+226"),
    "Burundi": ("BI", "+257"), "Cambodia": ("KH", "+855"),
    "Cameroon": ("CM", "+237"), "Canada": ("CA", "+1"),
    "Central African Republic": ("CF", "+236"), "Chad": ("TD", "+235"),
    "Chile": ("CL", "+56"), "China": ("CN", "+86"),
    "Colombia": ("CO", "+57"), "Costa Rica": ("CR", "+506"),
    "Côte d'Ivoire": ("CI", "+225"), "Croatia": ("HR", "+385"),
    "Cuba": ("CU", "+53"), "Cyprus": ("CY", "+357"),
    "Czechia": ("CZ", "+420"),
    "Democratic Republic of the Congo": ("CD", "+243"),
    "Congo": ("CG", "+242"), "Denmark": ("DK", "+45"),
    "Djibouti": ("DJ", "+253"), "Dominican Republic": ("DO", "+1"),
    "Ecuador": ("EC", "+593"), "Egypt": ("EG", "+20"),
    "El Salvador": ("SV", "+503"), "Eritrea": ("ER", "+291"),
    "Estonia": ("EE", "+372"), "Eswatini": ("SZ", "+268"),
    "Ethiopia": ("ET", "+251"), "Fiji": ("FJ", "+679"),
    "Finland": ("FI", "+358"), "France": ("FR", "+33"),
    "Gabon": ("GA", "+241"), "Gambia": ("GM", "+220"),
    "Georgia": ("GE", "+995"), "Germany": ("DE", "+49"),
    "Ghana": ("GH", "+233"), "Greece": ("GR", "+30"),
    "Guatemala": ("GT", "+502"), "Guinea": ("GN", "+224"),
    "Guinea-Bissau": ("GW", "+245"), "Haiti": ("HT", "+509"),
    "Honduras": ("HN", "+504"), "Hungary": ("HU", "+36"),
    "India": ("IN", "+91"), "Indonesia": ("ID", "+62"),
    "Iran": ("IR", "+98"), "Iraq": ("IQ", "+964"),
    "Ireland": ("IE", "+353"), "Israel": ("IL", "+972"),
    "Italy": ("IT", "+39"), "Jamaica": ("JM", "+1"),
    "Japan": ("JP", "+81"), "Jordan": ("JO", "+962"),
    "Kazakhstan": ("KZ", "+7"), "Kenya": ("KE", "+254"),
    "Kiribati": ("KI", "+686"), "Kuwait": ("KW", "+965"),
    "Kyrgyzstan": ("KG", "+996"), "Laos": ("LA", "+856"),
    "Latvia": ("LV", "+371"), "Lebanon": ("LB", "+961"),
    "Lesotho": ("LS", "+266"), "Liberia": ("LR", "+231"),
    "Libya": ("LY", "+218"), "Lithuania": ("LT", "+370"),
    "Luxembourg": ("LU", "+352"), "Madagascar": ("MG", "+261"),
    "Malawi": ("MW", "+265"), "Malaysia": ("MY", "+60"),
    "Mali": ("ML", "+223"), "Malta": ("MT", "+356"),
    "Mauritania": ("MR", "+222"), "Mexico": ("MX", "+52"),
    "Moldova": ("MD", "+373"), "Mongolia": ("MN", "+976"),
    "Montenegro": ("ME", "+382"), "Morocco": ("MA", "+212"),
    "Mozambique": ("MZ", "+258"), "Myanmar": ("MM", "+95"),
    "Namibia": ("NA", "+264"), "Nepal": ("NP", "+977"),
    "Netherlands": ("NL", "+31"), "New Zealand": ("NZ", "+64"),
    "Nicaragua": ("NI", "+505"), "Niger": ("NE", "+227"),
    "Nigeria": ("NG", "+234"), "North Macedonia": ("MK", "+389"),
    "Norway": ("NO", "+47"), "Oman": ("OM", "+968"),
    "Pakistan": ("PK", "+92"), "Palestine": ("PS", "+970"),
    "Panama": ("PA", "+507"), "Papua New Guinea": ("PG", "+675"),
    "Paraguay": ("PY", "+595"), "Peru": ("PE", "+51"),
    "Philippines": ("PH", "+63"), "Poland": ("PL", "+48"),
    "Portugal": ("PT", "+351"), "Qatar": ("QA", "+974"),
    "Romania": ("RO", "+40"), "Russia": ("RU", "+7"),
    "Rwanda": ("RW", "+250"), "Saudi Arabia": ("SA", "+966"),
    "Senegal": ("SN", "+221"), "Serbia": ("RS", "+381"),
    "Sierra Leone": ("SL", "+232"), "Singapore": ("SG", "+65"),
    "Slovakia": ("SK", "+421"), "Slovenia": ("SI", "+386"),
    "Somalia": ("SO", "+252"), "South Africa": ("ZA", "+27"),
    "South Korea": ("KR", "+82"), "South Sudan": ("SS", "+211"),
    "Spain": ("ES", "+34"), "Sri Lanka": ("LK", "+94"),
    "Sudan": ("SD", "+249"), "Sweden": ("SE", "+46"),
    "Switzerland": ("CH", "+41"), "Syria": ("SY", "+963"),
    "Tajikistan": ("TJ", "+992"), "Tanzania": ("TZ", "+255"),
    "Thailand": ("TH", "+66"), "Timor-Leste": ("TL", "+670"),
    "Togo": ("TG", "+228"), "Tunisia": ("TN", "+216"),
    "Turkey": ("TR", "+90"), "Uganda": ("UG", "+256"),
    "Ukraine": ("UA", "+380"), "United Arab Emirates": ("AE", "+971"),
    "United Kingdom": ("GB", "+44"), "United States": ("US", "+1"),
    "Uruguay": ("UY", "+598"), "Uzbekistan": ("UZ", "+998"),
    "Vanuatu": ("VU", "+678"), "Venezuela": ("VE", "+58"),
    "Vietnam": ("VN", "+84"), "Yemen": ("YE", "+967"),
    "Zambia": ("ZM", "+260"), "Zimbabwe": ("ZW", "+263"),
}

# Duty-station city -> country, for cards that name only a city (very common:
# "Geneva", "Pretoria", "New York City"). Factual capital/UN-hub lookups
# only — an unknown city leaves the country BLANK, never guessed.
DUTY_STATION_COUNTRIES = {
    "Abidjan": "Côte d'Ivoire", "Abu Dhabi": "United Arab Emirates",
    "Abuja": "Nigeria", "Accra": "Ghana", "Addis Ababa": "Ethiopia",
    "Almaty": "Kazakhstan", "Amman": "Jordan", "Amsterdam": "Netherlands",
    "Ankara": "Turkey", "Antananarivo": "Madagascar", "Astana": "Kazakhstan",
    "Asunción": "Paraguay", "Baghdad": "Iraq", "Baku": "Azerbaijan",
    "Bamako": "Mali", "Bangkok": "Thailand", "Bangui": "Central African Republic",
    "Banjul": "Gambia", "Beijing": "China", "Beirut": "Lebanon",
    "Belgrade": "Serbia", "Berlin": "Germany", "Bern": "Switzerland",
    "Bishkek": "Kyrgyzstan", "Bogotá": "Colombia", "Bonn": "Germany",
    "Brasília": "Brazil", "Bratislava": "Slovakia", "Brazzaville": "Congo",
    "Brussels": "Belgium", "Bucharest": "Romania", "Budapest": "Hungary",
    "Buenos Aires": "Argentina", "Bujumbura": "Burundi", "Cairo": "Egypt",
    "Cape Town": "South Africa", "Caracas": "Venezuela",
    "Chisinau": "Moldova", "Colombo": "Sri Lanka", "Conakry": "Guinea",
    "Copenhagen": "Denmark", "Dakar": "Senegal", "Damascus": "Syria",
    "Dar es Salaam": "Tanzania", "Dhaka": "Bangladesh", "Doha": "Qatar",
    "Dubai": "United Arab Emirates", "Dushanbe": "Tajikistan",
    "Freetown": "Sierra Leone", "Gaborone": "Botswana", "Geneva": "Switzerland",
    "Guatemala City": "Guatemala", "Hanoi": "Vietnam", "Harare": "Zimbabwe",
    "Havana": "Cuba", "Helsinki": "Finland", "Islamabad": "Pakistan",
    "Istanbul": "Turkey", "Jakarta": "Indonesia",
    "Johannesburg": "South Africa", "Juba": "South Sudan",
    "Kabul": "Afghanistan", "Kampala": "Uganda", "Kathmandu": "Nepal",
    "Khartoum": "Sudan", "Kigali": "Rwanda", "Kinshasa": "Democratic Republic of the Congo",
    "Kuala Lumpur": "Malaysia", "Kuwait City": "Kuwait", "Kyiv": "Ukraine",
    "La Paz": "Bolivia", "Lagos": "Nigeria", "Lilongwe": "Malawi",
    "Lima": "Peru", "Lisbon": "Portugal", "Lomé": "Togo",
    "London": "United Kingdom", "Luanda": "Angola", "Lusaka": "Zambia",
    "Madrid": "Spain", "Managua": "Nicaragua", "Manila": "Philippines",
    "Maputo": "Mozambique", "Maseru": "Lesotho", "Mexico City": "Mexico",
    "Mogadishu": "Somalia", "Monrovia": "Liberia", "Montevideo": "Uruguay",
    "Montreal": "Canada", "Moscow": "Russia", "Mumbai": "India",
    "Muscat": "Oman", "N'Djamena": "Chad", "Nairobi": "Kenya",
    "New Delhi": "India", "New York": "United States",
    "New York City": "United States", "Niamey": "Niger",
    "Nouakchott": "Mauritania", "Oslo": "Norway", "Ottawa": "Canada",
    "Ouagadougou": "Burkina Faso", "Panama City": "Panama",
    "Paris": "France", "Phnom Penh": "Cambodia",
    "Port Moresby": "Papua New Guinea", "Port-au-Prince": "Haiti",
    "Prague": "Czechia", "Pretoria": "South Africa",
    "Qamishli": "Syria", "Quito": "Ecuador",
    "Rabat": "Morocco", "Riyadh": "Saudi Arabia", "Rome": "Italy",
    "San José": "Costa Rica", "San Salvador": "El Salvador",
    "São Paulo": "Brazil",
    "Santiago": "Chile", "Santo Domingo": "Dominican Republic",
    "Sarajevo": "Bosnia and Herzegovina", "Seoul": "South Korea",
    "Sharm el-Sheikh": "Egypt", "Singapore": "Singapore",
    "Stockholm": "Sweden", "Suva": "Fiji", "Tashkent": "Uzbekistan",
    "Tbilisi": "Georgia", "Tegucigalpa": "Honduras", "The Hague": "Netherlands",
    "Tirana": "Albania", "Tokyo": "Japan", "Tripoli": "Libya",
    "Tunis": "Tunisia", "Ulaanbaatar": "Mongolia", "Vienna": "Austria",
    "Vientiane": "Laos", "Warsaw": "Poland", "Washington D.C.": "United States",
    "Washington DC": "United States", "Windhoek": "Namibia",
    "Yangon": "Myanmar", "Yaoundé": "Cameroon", "Yerevan": "Armenia",
    "Zurich": "Switzerland",
}

log = logging.getLogger("impactpool_scraper")

# ----------------------------------------------------------------------------
# Listing-page parsing
# ----------------------------------------------------------------------------

# One search-result card. The trailing </a></div> closes the card link and
# the <div class='job'> wrapper (verified stable across 3 sampled pages).
_CARD_RE = re.compile(r"<div class='job'>.*?</a>\s*</div>", re.S)
_CARD_ID_RE = re.compile(r'href="/jobs/(\d+)"')
_CARD_TITLE_RE = re.compile(r"type='cardTitle'>(.*?)</div>", re.S)
# The three metadata fields are told apart by their inline colors (the title
# div uses #1C1B16 WITHOUT a trailing semicolon, the org div WITH one).
_CARD_ORG_RE = re.compile(r"style='color: #1C1B16;'[^>]*>\s*([^<]*)", re.S)
_CARD_LOC_RE = re.compile(r"style='color: #63625B;'[^>]*>(.*?)</div>", re.S)
_CARD_GRADE_RE = re.compile(r"style='color: #75736C;'[^>]*>(.*?)</div>", re.S)
_WS_RE = re.compile(r"\s+")


def _text(match, group=1):
    if not match:
        return ""
    return _WS_RE.sub(" ", html_lib.unescape(match.group(group))).strip()


def parse_cards(html):
    """One listing page -> list of card dicts (job_id, title, company,
    locations, grade). Cards without a /jobs/<id> link are skipped."""
    cards = []
    for m in _CARD_RE.finditer(html or ""):
        seg = m.group(0)
        id_match = _CARD_ID_RE.search(seg)
        if not id_match:
            continue
        cards.append({
            "job_id": id_match.group(1),
            "title": _text(_CARD_TITLE_RE.search(seg)),
            "company": _text(_CARD_ORG_RE.search(seg)),
            "locations": _text(_CARD_LOC_RE.search(seg)),
            "grade": _text(_CARD_GRADE_RE.search(seg)),
        })
    return cards


def split_location(raw):
    """'Islamabad | Pakistan' -> ("Islamabad", "Pakistan", False).

    Returns (city, country, remote). Tokens are matched against the country
    table (via aliases); a city-only card gets its country from
    DUTY_STATION_COUNTRIES or stays blank — never guessed.
    """
    remote = False
    cities, countries = [], []
    for part in (raw or "").split("|"):
        part = _WS_RE.sub(" ", part).strip()
        if not part:
            continue
        low = part.lower()
        if low.startswith("remote") or "home based" in low or "home-based" in low:
            remote = True
            continue
        name = COUNTRY_ALIASES.get(part, part)
        if name in COUNTRY_CODES:
            countries.append(name)
        else:
            cities.append(part)
    city = cities[0] if cities else ""
    country = countries[0] if countries else DUTY_STATION_COUNTRIES.get(city, "")
    return city, country, remote


# ----------------------------------------------------------------------------
# Detail-page parsing
# ----------------------------------------------------------------------------

_DEADLINE_RE = re.compile(r"Application deadline:\s*([^<(]+?)\s*(?:\(|<)")
_SUMMARY_RE = re.compile(
    r"Summary by Impactpool</div>\s*<div class='ip-typography' type='body'>"
    r"(.*?)</div>", re.S)
_REQS_RE = re.compile(
    r"Candidate Requirements:</div>\s*<div class='ip-typography' type='body'>"
    r"(.*?)</div>", re.S)
_DIV_RE = re.compile(r"<div\b|</div>")
_TAG_RE = re.compile(r"<[^>]+>")


def parse_deadline(text):
    """"August 31, 2026" (the site's only deadline format) -> "2026-08-31"."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def extract_main_content(html):
    """The <div class='main-content'> block (the actual JD), found by <div>
    depth balancing — the JD itself nests divs, so a lazy regex would cut it
    short."""
    i = (html or "").find("class='main-content'")
    if i == -1:
        return ""
    start = html.rfind("<div", 0, i)
    if start == -1:
        return ""
    depth = 0
    for m in _DIV_RE.finditer(html, start):
        depth += 1 if m.group(0) != "</div>" else -1
        if depth == 0:
            return html[start:m.end()]
    return html[start:]


def strip_html(text):
    """Detail-page HTML -> plain text."""
    text = re.sub(r"</?(p|br|li|ul|ol|div|span|h\d|table|tr|td|th|a|strong|em|b|i)[^>]*>",
                  " ", text or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def truncate_description(text, limit=DESCRIPTION_MAX_CHARS):
    """Cap a description at `limit` chars on a word boundary, marked with a
    trailing "…" so a shortened description is never mistaken for a complete
    one."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    if len(spaced) >= limit * 0.5:
        cut = spaced
    return cut.rstrip() + "…"


def parse_detail(html):
    """One detail page -> {closing_date, description, ip_summary}.

    description  = the JD body (main-content).
    ip_summary   = Impactpool's own summary + Candidate Requirements card —
                   kept as its own column and added to the text the
                   classifier scores (extra recall; the JD alone stays the
                   exported description).
    """
    deadline_match = _DEADLINE_RE.search(html or "")
    # Some postings ship the summary card with empty body divs (verified:
    # /jobs/1233466) — empty bits are dropped so the column stays blank.
    summary_bits = [text for text in
                    (strip_html(m.group(1)) if m else ""
                     for m in (_SUMMARY_RE.search(html or ""),
                               _REQS_RE.search(html or "")))
                    if text]
    return {
        "closing_date": parse_deadline(deadline_match.group(1)
                                       if deadline_match else ""),
        "description": strip_html(extract_main_content(html)),
        "ip_summary": " ".join(summary_bits),
    }


# ----------------------------------------------------------------------------
# Classification — delegated to _shared/classification.classify_job
# ----------------------------------------------------------------------------

def classify_impactpool_job(title, description="", ip_summary=""):
    """Route one candidate through the shared classifier. The site has no
    curated role/sector field, so skills stays empty; Impactpool's summary
    card rides along in the description signal."""
    text = " ".join(part for part in (description, ip_summary) if part)
    return classify_job(title or "", skills="", description=text)


# UN/NGO/multilateral employers; the club company_type enum only offers
# hospital|pharma, and pharma-looking names (research institutes, labs,
# medicines initiatives) do occur.
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?|genomic|"
    r"medicines",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (SEARCH_URL, JOB_URL_TEMPLATE.format("1")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch(session, url, params=None):
    """GET one URL with retry/backoff. Returns the HTML or None on a
    permanent failure."""
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
    log.error("Giving up on %s after %d retries (%s)",
              url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(card, detail, verdict):
    """One rich-CSV row from a listing card + its detail page + verdict."""
    city, country, remote = split_location(card["locations"])
    return {
        "source": SITE,
        "job_id": card["job_id"],
        "title": card["title"],
        "company": card["company"],
        "locations": card["locations"],
        "city": city,
        "country": country,
        "remote": "yes" if remote else "",
        "grade": card["grade"],
        "category": verdict["category"],
        "sub_category": verdict["sub_category"],
        "role_family": verdict["role_family"],
        "all_families": verdict["all_families"],
        "family_scores": verdict["family_scores"],
        "family_confidence": verdict["family_confidence"],
        "matched_in": verdict["matched_in"],
        "needs_review": verdict["needs_review"],
        "company_type": classify_company_type(card["company"]),
        "posted_date": "",              # the site exposes none — never invented
        "closing_date": detail["closing_date"],
        "ip_summary": truncate_description(detail["ip_summary"]),
        "description": truncate_description(detail["description"]),
        "job_url": JOB_URL_TEMPLATE.format(card["job_id"]),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the CLUB_COLUMNS contract.

    No salary, experience, posted date or logo exists on this source — those
    columns stay empty rather than being invented. The club job_type enum
    (full_time|part_time|remote|hybrid) can't express Consultancy/Internship;
    remote-flagged duty stations map to "remote", everything else to
    "full_time", and the source's grade string stays in the rich CSV.
    """
    country = _clean(r.get("country"))
    code, dial = COUNTRY_CODES.get(country, ("", ""))
    description = _clean(r.get("description"))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": _clean(r.get("city")),
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": description,
        "job_type": "remote" if _clean(r.get("remote")) else "full_time",
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": "",                # source exposes none — never invented
        "min_experience": "",
        "max_experience": "",
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
    df = load_existing(path)
    if df is None or "job_id" not in df.columns:
        return set()
    return set(df["job_id"].dropna())


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
            "locations": get("locations", ""), "grade": get("grade", "")}


def append_needs_review(entries, overwrite=False):
    """Append flagged rows to needs_review.csv, deduped on job_id.
    `overwrite=True` (used by --reclassify) replaces the file with the
    freshly computed full set so stale flags can't linger."""
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
    """Append dropped rows to out-of-scope.csv, deduped on job_id — the
    detail-fetch skip list, and reversible by design (rows are moved, never
    discarded)."""
    try:
        old = pd.read_csv(target, dtype=str, keep_default_na=False)
        combined = pd.concat([old, dropped_df], ignore_index=True)
    except FileNotFoundError:
        combined = dropped_df
    combined = combined.fillna("").drop_duplicates(subset="job_id",
                                                   keep="last")
    combined.to_csv(target, index=False)
    return len(combined)


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------

def reclassify(args):
    """Re-run classify_job over the stored CSV and rewrite the outputs.

    Classification is a pure function of the stored title + description +
    ip_summary, so re-scoping never requires re-crawling. Makes no network
    requests. Rows ruled out of scope are MOVED to out-of-scope.csv, never
    discarded.
    """
    df = load_existing(args.output)
    if df is None or df.empty:
        sys.exit("Nothing to reclassify: {} not found or empty.".format(args.output))
    df = df.fillna("")

    kept_rows, dropped_rows, review_log = [], [], []
    for _, row in df.iterrows():
        verdict = classify_impactpool_job(str(row.get("title", "") or ""),
                                          str(row.get("description", "") or ""),
                                          str(row.get("ip_summary", "") or ""))
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
    if len(kept):
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


def crawl_listing(session, known_ids, max_pages):
    """Walk /search newest-first and collect unseen candidate cards.

    Stops after 3 consecutive pages with zero cards (end of the board, with
    spec §7's transient-empty tolerance) or at the first page with zero
    UNSEEN ids — safe because ordering is id-descending, so nothing new can
    appear on a later page (page-1 promoted cards are old, already-known
    ids). Returns (candidates ordered as found, pages_crawled, duplicates).
    """
    candidates = {}
    pages = duplicates = empty_streak = 0
    for page in range(1, max_pages + 1):
        html = fetch(session, SEARCH_URL,
                     params={"per_page": PER_PAGE, "page": page})
        if html is None:
            log.error("Listing page %d failed permanently — stopping the "
                      "listing crawl (details of already-found cards still run)",
                      page)
            break
        pages = page
        cards = parse_cards(html)
        if not cards:
            empty_streak += 1
            if empty_streak >= 3:
                log.info("3 consecutive empty pages at page %d — end of the "
                         "board", page)
                break
            log.warning("Page %d has no job cards — tolerating (%d/3)",
                        page, empty_streak)
            continue
        empty_streak = 0
        fresh = [c for c in cards
                 if c["job_id"] not in known_ids and c["job_id"] not in candidates]
        duplicates += sum(1 for c in cards if c["job_id"] in known_ids)
        log.info("Page %-3d %3d cards, %3d unseen (ids %s..%s)",
                 page, len(cards), len(fresh),
                 cards[0]["job_id"], cards[-1]["job_id"])
        for card in fresh:
            candidates[card["job_id"]] = card
        if not fresh:
            log.info("Page %d yielded no unseen ids — stopping (id-descending "
                     "order guarantees later pages are all known)", page)
            break
    return list(candidates.values()), pages, duplicates


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape impact-sector public-health jobs from impactpool.org.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail pages (test runs)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, metavar="N",
                        help="listing-page safety ceiling (default: %(default)s)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--reclassify", action="store_true",
                        help="re-apply the classifier to the stored CSV and "
                             "rewrite the outputs; makes no network requests")
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
    stored_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    out_of_scope_ids = load_id_column(OUT_OF_SCOPE_CSV)
    known_ids = stored_ids | out_of_scope_ids
    log.info("Existing store: %d in-scope + %d out-of-scope known ids",
             len(stored_ids), len(out_of_scope_ids))

    candidates, pages, duplicates = crawl_listing(session, known_ids,
                                                  args.max_pages)
    log.info("Listing crawl: %d pages, %d unseen candidates, %d known cards",
             pages, len(candidates), duplicates)

    counters = {"scanned": len(candidates), "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "detail_failed": 0,
                "duplicates": duplicates}
    new_rows, review_log, dropped_rows = [], [], []
    fetched = 0

    for card in candidates:                  # id-descending ~= newest first
        if args.limit is not None and fetched >= args.limit:
            break
        detail_html = fetch(session, JOB_URL_TEMPLATE.format(card["job_id"]))
        fetched += 1
        if detail_html is None:
            # NOT recorded anywhere — a failed id stays unseen and is
            # retried on the next run.
            counters["detail_failed"] += 1
            continue
        try:
            detail = parse_detail(detail_html)
            verdict = classify_impactpool_job(card["title"],
                                              detail["description"],
                                              detail["ip_summary"])
            row = build_row(card, detail, verdict)
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", card["job_id"], exc)
            counters["detail_failed"] += 1
            continue

        if not verdict["in_scope"]:
            counters["excluded_out_of_scope"] += 1
            dropped_rows.append(row)
            continue
        if verdict["needs_review"]:
            counters["needs_review"] += 1
            review_log.append(review_entry(row))
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
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- out-of-scope store (skip list; full rows, reversible) ----
    if dropped_rows:
        total = move_out_of_scope(pd.DataFrame(dropped_rows, dtype=str))
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        n_review = append_needs_review(review_log)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, n_review)

    print("\n===== Run summary =====")
    print("Unseen candidates found:   {:>6,}".format(counters["scanned"]))
    print("Detail pages fetched:      {:>6,}".format(fetched))
    print("Excluded (out of scope):   {:>6,}".format(counters["excluded_out_of_scope"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicate cards skipped:   {:>6,}".format(counters["duplicates"]))
    print("Detail fetch failures:     {:>6,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
