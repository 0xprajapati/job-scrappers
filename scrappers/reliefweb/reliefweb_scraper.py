#!/usr/bin/env python3
"""Scrape humanitarian public-health jobs from reliefweb.int.

Why this source
---------------
ReliefWeb (UN OCHA) is the primary job board of the humanitarian sector, and
its NGO/UN listings are dense in exactly the Public Health side of the
taxonomy: epidemiologists, M&E officers, community health programme managers,
nutrition specialists, disease-programme coordinators. Probed 2026-08-26.

Data source
-----------
The public RSS feed — no key, no registration:

    GET https://reliefweb.int/jobs/rss.xml[?advanced-search=(<FACET>)]

The JSON API is NOT an option: v1 is decommissioned and v2 requires a
registered appname (arbitrary appnames answer 403). The feed needs nothing.

Verified feed facts (2026-08-26):
* Each feed serves EXACTLY the latest 20 items — no pagination (`?page=` is
  ignored) and no `limit=`. The unfiltered feed turns over ~28 jobs/day, so
  it alone spans well under a day of postings.
* `?advanced-search=(<code><id>)` DOES filter the feed — the facet codes are
  the same ones the /jobs river UI puts in its URLs (C=country, T=theme,
  CC=career category, TY=type, S=organization). A filtered feed still serves
  20 items, but because only a fraction of postings carry a given facet those
  20 items span days-to-weeks — that is how this scraper reaches past the
  20-item cap: it unions the unfiltered feed with several in-scope-leaning
  facet feeds and dedupes on job id (see FEEDS).
* An EMPTY valid feed is normal, for two verified reasons: a cold facet
  combination can transiently serve 0 items (T4595 served 0, then 20 items
  minutes later), and a low-traffic facet can genuinely have zero open
  postings (T4596 HIV/Aids showed 0 on the HTML river too, 2026-08-26).
  Empty facet feeds are therefore logged, never treated as an error.
* Item `description` is the COMPLETE job body (median ~10k chars of HTML),
  prefixed with tagged metadata divs:
      <div class="tag country">Country: Kenya</div>      (0-n of these)
      <div class="tag source">Organization: ...</div>
      <div class="date closing">Closing date: 8 Sep 2026</div>  (optional)
  so classification never needs a detail-page fetch.
* Item `<category>` elements are an UNLABELED mix of country, organization,
  career category, job type and theme values. They are told apart by
  matching against the site's own closed vocabularies (CAREER_CATEGORIES /
  THEMES / JOB_TYPES, lifted from the /jobs river filter config) and the
  `<author>` organization; whatever remains is a country. Verified exact
  against the description's tagged country divs on 40/40 feed items.
* `pubDate` is the posting time on ReliefWeb, newest-first within a feed.

robots.txt: /jobs and /jobs/rss.xml are not disallowed (only /search/,
/admin/ etc. are). Checked at startup anyway; a descriptive User-Agent is
accepted.

Fetch wide, filter tight
------------------------
The facet feeds are RECALL ONLY — a way to surface more candidates than one
20-item feed can carry. No facet ever decides scope or category: every item
from every feed is routed through the one shared classifier
`_shared/classification.classify_job(title, skills, description)`, with the
site's career-category + theme tags as the weighted `skills` signal and the
HTML-stripped body as `description`. `in_scope == False` rows are dropped
and counted `excluded_out_of_scope`; `needs_review == True` rows are kept
AND flagged into needs_review.csv — never a silent drop. The raw facet
values stay in the rich CSV as source columns (career_category / themes /
job_type_raw / found_in_feeds).

Salary (master spec §3: capture, don't filter, don't invent)
------------------------------------------------------------
The feed carries no salary data at all, so the club salary columns are
always empty. Nothing is inferred.

Outputs (per the repo README + master-scraper-spec.md)
------------------------------------------------------
* reliefweb_jobs.csv        — rich cumulative store (dedup key: job_id),
                              source of the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/reliefweb.csv
                            — the same jobs mapped to the shared
                              HealthCareers.club 23-column schema.

Time window: a rolling WINDOW_DAYS (30) window applied every run — NOT the
usual stored-max watermark; see the WINDOW_DAYS comment for why a watermark
would lose jobs on this source.

Run `python reliefweb_scraper.py --help` for options.
"""

import argparse
import html as html_lib
import logging
import re
import sys
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import requests

# The one shared classifier + club contract (taxonomy migration 2026-08-25).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "reliefweb"
SITE_BASE = "https://reliefweb.int"
ROBOTS_URL = SITE_BASE + "/robots.txt"
RSS_URL = SITE_BASE + "/jobs/rss.xml"

USER_AGENT = "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"

# Each feed is capped at its latest 20 items, so coverage comes from unioning
# facet feeds (recall only — classify_job alone decides scope). Codes and ids
# are the /jobs river's own filter vocabulary (window.reliefweb.advancedSearch,
# read 2026-08-26): T=theme.id, CC=career_categories.id.
FEEDS = [
    ("all", ""),                       # unfiltered — everything very recent
    ("theme-health", "(T4595)"),       # Health
    ("theme-hiv-aids", "(T4596)"),     # HIV/Aids -> Disease Programs
    ("theme-nutrition", "(T4593)"),    # Food and Nutrition -> PH Nutrition
    ("theme-wash", "(T4604)"),         # Water Sanitation Hygiene
    ("cc-monitoring-evaluation", "(CC6868)"),   # Monitoring and Evaluation
    ("cc-information-management", "(CC20971)"), # -> Health Informatics & Data
]

# Rolling date window, applied EVERY run — a deliberate deviation from the
# master spec's stored-max watermark. The watermark exists to early-stop
# deep pagination; this source has none (always exactly len(FEEDS) requests)
# and job_id dedup already prevents re-adds, so a watermark could only LOSE
# jobs: a facet feed that served 0 items this run (verified transient
# cold-cache behavior) reaches back weeks once warm, and a stored-max
# watermark would refuse everything it then serves. The fixed window's only
# job is keeping ancient stale postings out — low-volume facet feeds still
# carry items years old (verified: theme-nutrition served a 2023 item).
WINDOW_DAYS = 30

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
# Feed bodies are complete job postings, median ~10k plain-text chars.
# Safety valve against a pathological row, not a content budget.
DESCRIPTION_MAX_CHARS = 20_000

RICH_CSV = str(Path(__file__).resolve().parent / "reliefweb_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
OUT_OF_SCOPE_CSV = str(Path(__file__).resolve().parent / "out-of-scope.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "locations", "country",
    "career_category", "themes", "job_type_raw", "category", "sub_category",
    "role_family", "all_families", "family_scores", "family_confidence",
    "matched_in", "needs_review", "company_type", "posted_date",
    "closing_date", "description", "job_url", "found_in_feeds", "scraped_at",
]

NEEDS_REVIEW_COLUMNS = ["job_id", "title", "company", "category",
                        "sub_category", "role_family", "matched_in",
                        "career_category", "themes"]

# The /jobs river's closed vocabularies (window.reliefweb.advancedSearch,
# read 2026-08-26). Used ONLY to tell the unlabeled <category> elements
# apart — never to decide scope. If ReliefWeb adds a value, the affected
# items misfile it as a country (visible in the rich CSV), nothing breaks.
CAREER_CATEGORIES = {
    "Administration/Finance", "Advocacy/Communications",
    "Donor Relations/Grants Management", "Human Resources",
    "Information and Communications Technology", "Information Management",
    "Logistics/Procurement", "Monitoring and Evaluation", "Other",
    "Program/Project Management",
}
THEMES = {
    "Agriculture", "Camp Coordination and Camp Management",
    "Climate Change and Environment", "Coordination", "Disaster Management",
    "Education", "Food and Nutrition", "Gender", "Health", "HIV/Aids",
    "Mine Action", "Peacekeeping and Peacebuilding",
    "Protection and Human Rights", "Recovery and Reconstruction",
    "Safety and Security", "Shelter and Non-Food Items",
    "Water Sanitation Hygiene",
}
JOB_TYPES = {"Job", "Consultancy", "Internship"}

# ReliefWeb's formal UN country names -> the plain name the code table keys on.
COUNTRY_ALIASES = {
    "Bolivia (Plurinational State of)": "Bolivia",
    "Democratic People's Republic of Korea": "North Korea",
    "Iran (Islamic Republic of)": "Iran",
    "Lao People's Democratic Republic (the)": "Laos",
    "Lao People's Democratic Republic": "Laos",
    "Micronesia (Federated States of)": "Micronesia",
    "occupied Palestinian territory": "Palestine",
    "Republic of Korea": "South Korea",
    "Republic of Moldova": "Moldova",
    "Russian Federation": "Russia",
    "Syrian Arab Republic": "Syria",
    "Türkiye": "Turkey",
    "United Kingdom of Great Britain and Northern Ireland": "United Kingdom",
    "United Republic of Tanzania": "Tanzania",
    "United States of America": "United States",
    "Venezuela (Bolivarian Republic of)": "Venezuela",
    "Viet Nam": "Vietnam",
}

# country name -> (ISO alpha-2, dial code). Covers the humanitarian-sector
# countries that dominate this board; anything unseen exports with empty
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
    "Burkina Faso": ("BF", "+226"), "Burundi": ("BI", "+257"),
    "Cambodia": ("KH", "+855"), "Cameroon": ("CM", "+237"),
    "Canada": ("CA", "+1"), "Central African Republic": ("CF", "+236"),
    "Chad": ("TD", "+235"), "Chile": ("CL", "+56"), "China": ("CN", "+86"),
    "Colombia": ("CO", "+57"), "Costa Rica": ("CR", "+506"),
    "Côte d'Ivoire": ("CI", "+225"), "Cuba": ("CU", "+53"),
    "Czechia": ("CZ", "+420"),
    "Democratic Republic of the Congo": ("CD", "+243"),
    "Congo": ("CG", "+242"), "Denmark": ("DK", "+45"),
    "Djibouti": ("DJ", "+253"), "Dominican Republic": ("DO", "+1"),
    "Ecuador": ("EC", "+593"), "Egypt": ("EG", "+20"),
    "El Salvador": ("SV", "+503"), "Eritrea": ("ER", "+291"),
    "Eswatini": ("SZ", "+268"), "Ethiopia": ("ET", "+251"),
    "Fiji": ("FJ", "+679"), "Finland": ("FI", "+358"),
    "France": ("FR", "+33"), "Gabon": ("GA", "+241"),
    "Gambia": ("GM", "+220"), "Georgia": ("GE", "+995"),
    "Germany": ("DE", "+49"), "Ghana": ("GH", "+233"),
    "Greece": ("GR", "+30"), "Guatemala": ("GT", "+502"),
    "Guinea": ("GN", "+224"), "Guinea-Bissau": ("GW", "+245"),
    "Haiti": ("HT", "+509"), "Honduras": ("HN", "+504"),
    "Hungary": ("HU", "+36"), "India": ("IN", "+91"),
    "Indonesia": ("ID", "+62"), "Iran": ("IR", "+98"),
    "Iraq": ("IQ", "+964"), "Ireland": ("IE", "+353"),
    "Israel": ("IL", "+972"), "Italy": ("IT", "+39"),
    "Jamaica": ("JM", "+1"), "Japan": ("JP", "+81"),
    "Jordan": ("JO", "+962"), "Kazakhstan": ("KZ", "+7"),
    "Kenya": ("KE", "+254"), "Kuwait": ("KW", "+965"),
    "Kyrgyzstan": ("KG", "+996"), "Laos": ("LA", "+856"),
    "Lebanon": ("LB", "+961"), "Lesotho": ("LS", "+266"),
    "Liberia": ("LR", "+231"), "Libya": ("LY", "+218"),
    "Madagascar": ("MG", "+261"), "Malawi": ("MW", "+265"),
    "Malaysia": ("MY", "+60"), "Mali": ("ML", "+223"),
    "Mauritania": ("MR", "+222"), "Mexico": ("MX", "+52"),
    "Moldova": ("MD", "+373"), "Mongolia": ("MN", "+976"),
    "Morocco": ("MA", "+212"), "Mozambique": ("MZ", "+258"),
    "Myanmar": ("MM", "+95"), "Namibia": ("NA", "+264"),
    "Nepal": ("NP", "+977"), "Netherlands": ("NL", "+31"),
    "New Zealand": ("NZ", "+64"), "Nicaragua": ("NI", "+505"),
    "Niger": ("NE", "+227"), "Nigeria": ("NG", "+234"),
    "North Korea": ("KP", "+850"), "Norway": ("NO", "+47"),
    "Oman": ("OM", "+968"), "Pakistan": ("PK", "+92"),
    "Palestine": ("PS", "+970"), "Panama": ("PA", "+507"),
    "Papua New Guinea": ("PG", "+675"), "Paraguay": ("PY", "+595"),
    "Peru": ("PE", "+51"), "Philippines": ("PH", "+63"),
    "Poland": ("PL", "+48"), "Portugal": ("PT", "+351"),
    "Qatar": ("QA", "+974"), "Romania": ("RO", "+40"),
    "Russia": ("RU", "+7"), "Rwanda": ("RW", "+250"),
    "Saudi Arabia": ("SA", "+966"), "Senegal": ("SN", "+221"),
    "Serbia": ("RS", "+381"), "Sierra Leone": ("SL", "+232"),
    "Singapore": ("SG", "+65"), "Somalia": ("SO", "+252"),
    "South Africa": ("ZA", "+27"), "South Korea": ("KR", "+82"),
    "South Sudan": ("SS", "+211"), "Spain": ("ES", "+34"),
    "Sri Lanka": ("LK", "+94"), "Sudan": ("SD", "+249"),
    "Sweden": ("SE", "+46"), "Switzerland": ("CH", "+41"),
    "Syria": ("SY", "+963"), "Tajikistan": ("TJ", "+992"),
    "Tanzania": ("TZ", "+255"), "Thailand": ("TH", "+66"),
    "Timor-Leste": ("TL", "+670"), "Togo": ("TG", "+228"),
    "Tunisia": ("TN", "+216"), "Turkey": ("TR", "+90"),
    "Uganda": ("UG", "+256"), "Ukraine": ("UA", "+380"),
    "United Arab Emirates": ("AE", "+971"), "United Kingdom": ("GB", "+44"),
    "United States": ("US", "+1"), "Uruguay": ("UY", "+598"),
    "Uzbekistan": ("UZ", "+998"), "Vanuatu": ("VU", "+678"),
    "Venezuela": ("VE", "+58"), "Vietnam": ("VN", "+84"),
    "Yemen": ("YE", "+967"), "Zambia": ("ZM", "+260"),
    "Zimbabwe": ("ZW", "+263"),
}

log = logging.getLogger("reliefweb_scraper")

# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------

_JOB_ID_RE = re.compile(r"/job/(\d+)(?:/|$)")


def job_id_from_link(link):
    """https://reliefweb.int/job/4227017/network-coordinator-kenya -> "4227017"."""
    match = _JOB_ID_RE.search(link or "")
    return match.group(1) if match else ""


def parse_rfc822_date(text):
    """"Wed, 26 Aug 2026 09:44:22 +0000" -> "2026-08-26"; "" if unparseable."""
    if not text:
        return ""
    try:
        return parsedate_to_datetime(text.strip()).date().isoformat()
    except (TypeError, ValueError):
        return ""


def parse_closing_date(text):
    """"8 Sep 2026" (the feed's only closing-date format) -> "2026-09-08"."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%d %b %Y").date().isoformat()
    except ValueError:
        return ""


_CLOSING_RE = re.compile(
    r'<div class="date closing">\s*Closing date:\s*([^<]*?)\s*</div>')
_META_DIV_RE = re.compile(
    r'<div class="(?:tag (?:country|source|city)|date closing)">[^<]*</div>')
_TAG_RE = re.compile(r"<[^>]+>")


def closing_date_from_description(desc_html):
    match = _CLOSING_RE.search(desc_html or "")
    return parse_closing_date(match.group(1)) if match else ""


def strip_html(text):
    """Job body HTML -> plain text. Drops the metadata divs first so
    "Country: Kenya Organization: ..." boilerplate never pollutes the
    description the classifier scores."""
    text = _META_DIV_RE.sub(" ", text or "")
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d|table|tr|td|th)[^>]*>", " ",
                  text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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


def split_categories(category_values, organization):
    """Tell the unlabeled <category> values apart.

    ["Kenya", "Save the Children", "Program/Project Management", "Job",
     "Health"] -> {"countries": ["Kenya"], "career_categories": [...],
                   "job_type": "Job", "themes": ["Health"]}

    Matching is against the river's closed vocabularies plus the item's
    <author> organization; the remainder is countries. Verified exact against
    the description's tagged country divs on 40/40 feed items.
    """
    out = {"countries": [], "career_categories": [], "job_type": "",
           "themes": []}
    organization = (organization or "").strip()
    for value in category_values:
        value = (value or "").strip()
        if not value or value == organization:
            continue
        if value in CAREER_CATEGORIES:
            out["career_categories"].append(value)
        elif value in THEMES:
            out["themes"].append(value)
        elif value in JOB_TYPES:
            out["job_type"] = value
        else:
            out["countries"].append(value)
    return out


def normalize_country(name):
    """ReliefWeb's formal UN name -> plain name ("Viet Nam" -> "Vietnam")."""
    name = (name or "").strip()
    return COUNTRY_ALIASES.get(name, name)


def parse_feed(xml_text):
    """One RSS document -> list of raw item dicts. Malformed XML -> []."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("Unparseable feed XML: %s", exc)
        return []
    items = []
    for item in root.findall(".//item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "author": (item.findtext("author") or "").strip(),
            "pubDate": (item.findtext("pubDate") or "").strip(),
            "description": item.findtext("description") or "",
            "categories": [c.text or "" for c in item.findall("category")],
        })
    return items


# ----------------------------------------------------------------------------
# Classification — delegated to _shared/classification.classify_job
# ----------------------------------------------------------------------------

def classify_feed_job(title, career_categories=None, themes=None,
                      description=""):
    """Route one candidate through the shared classifier. The site's career
    category + theme tags are the weighted `skills` signal (recall aid, not a
    decision — the scorer weights title x5 / skills x2 / description x1)."""
    skills = " ".join((career_categories or []) + (themes or []))
    return classify_job(title or "", skills=skills,
                        description=description or "")


# Humanitarian boards are NGO/UN/academic employers; the club company_type
# enum only offers hospital|pharma, and pharma-looking names (research
# institutes, labs) do occur.
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?|genomic",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(today=None, since=None):
    """Rolling WINDOW_DAYS cutoff, or an explicit `since` override.

    NOT a stored-max watermark — see the WINDOW_DAYS comment for why that
    would lose jobs on this source.
    """
    if since:
        return since
    today = today or date.today()
    return (today - timedelta(days=WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/rss+xml, application/xml, text/xml"})
    return session


def check_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    if not rp.can_fetch(USER_AGENT, RSS_URL):
        sys.exit("robots.txt disallows {} — aborting.".format(RSS_URL))
    log.info("robots.txt check passed")


def fetch_feed(session, advanced_search):
    """Fetch one feed (unfiltered or facet-filtered). Returns raw items or
    None on a permanent failure."""
    params = {"advanced-search": advanced_search} if advanced_search else None
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %r in %.0fs (%s)",
                        attempt, MAX_RETRIES, advanced_search or "all",
                        wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(RSS_URL, params=params,
                               timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for feed %r — skipping",
                            resp.status_code, advanced_search or "all")
                return None
            resp.raise_for_status()
            return parse_feed(resp.text)
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on feed %r after %d retries (%s)",
              advanced_search or "all", MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def job_to_rich_row(item, meta, verdict, description, feed_labels):
    """Build one rich-CSV row from a feed item + its classify_job verdict."""
    countries = [normalize_country(c) for c in meta["countries"]]
    return {
        "source": SITE,
        "job_id": job_id_from_link(item["link"]),
        "title": item["title"],
        "company": item["author"],
        # Multi-country postings are common (one role, several duty stations);
        # keep the full list here, export the primary one to the club CSV.
        "locations": "; ".join(countries),
        "country": countries[0] if countries else "",
        "career_category": "; ".join(meta["career_categories"]),
        "themes": "; ".join(meta["themes"]),
        "job_type_raw": meta["job_type"],
        "category": verdict["category"],
        "sub_category": verdict["sub_category"],
        "role_family": verdict["role_family"],
        "all_families": verdict["all_families"],
        "family_scores": verdict["family_scores"],
        "family_confidence": verdict["family_confidence"],
        "matched_in": verdict["matched_in"],
        "needs_review": verdict["needs_review"],
        "company_type": classify_company_type(item["author"]),
        "posted_date": parse_rfc822_date(item["pubDate"]),
        "closing_date": closing_date_from_description(item["description"]),
        "description": truncate_description(description),
        "job_url": item["link"],
        "found_in_feeds": "; ".join(feed_labels),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the 23-column CLUB_COLUMNS contract.

    The feed has no salary, city, experience or logo data — those columns
    stay empty rather than being invented. `qualification` uses the shared
    extract_qualification over the description. The club job_type enum
    cannot express Consultancy/Internship, so everything maps to full_time
    and the source value stays in the rich CSV (job_type_raw).
    """
    country = _clean(r.get("country"))
    code, dial = COUNTRY_CODES.get(country, ("", ""))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        # Duty stations are country-level on ReliefWeb; no city is given and
        # inventing one would be a lie.
        "city_name": "",
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": "full_time",
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        "qualification": extract_qualification(_clean(r.get("description"))),
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
            "career_category": get("career_category", ""),
            "themes": get("themes", "")}


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
    """Append dropped rows to out-of-scope.csv, deduped on job_id —
    reversible by design, rows are moved, never discarded."""
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

    Classification is a pure function of the stored title + career-category/
    theme tags + description, so re-scoping never requires re-crawling.
    Makes no network requests and never re-dates a row. Rows ruled out of
    scope are MOVED to out-of-scope.csv, never discarded.
    """
    df = load_existing(args.output)
    if df is None or df.empty:
        sys.exit("Nothing to reclassify: {} not found or empty.".format(args.output))
    df = df.fillna("")

    kept_rows, dropped_rows, review_log = [], [], []
    for _, row in df.iterrows():
        career = [s for s in str(row.get("career_category", "") or "").split("; ") if s]
        themes = [s for s in str(row.get("themes", "") or "").split("; ") if s]
        verdict = classify_feed_job(str(row.get("title", "") or ""),
                                    career, themes,
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape humanitarian public-health jobs from reliefweb.int.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (for test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job posted "
                             "on/after this date")
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
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(since=args.since)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s%s",
             len(known_ids), cutoff, " (--since override)" if args.since else "")

    counters = {"scanned": 0, "excluded_out_of_scope": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    # The facet feeds overlap heavily by design (a Health-themed M&E job is in
    # both feeds); candidates are unioned on job_id BEFORE classification and
    # the feeds each one appeared in are recorded (found_in_feeds).
    candidates = {}          # job_id -> raw item
    feeds_by_id = {}         # job_id -> [feed labels]
    empty_feeds = []

    for label, facet in FEEDS:
        items = fetch_feed(session, facet)
        if items is None:
            log.error("Feed %r failed permanently — continuing without it", label)
            continue
        if not items:
            # Normal, not an error: transient cold facet cache, or genuinely
            # zero open postings for the facet — see the module docstring.
            empty_feeds.append(label)
            log.warning("Feed %r returned 0 items (cold cache, or nothing "
                        "currently posted under this facet)", label)
            continue
        log.info("Feed %-26s %2d items (%s .. %s)", label, len(items),
                 parse_rfc822_date(items[-1]["pubDate"]),
                 parse_rfc822_date(items[0]["pubDate"]))
        for item in items:
            job_id = job_id_from_link(item["link"])
            if not job_id:
                log.warning("Item without a /job/<id> link skipped: %r",
                            item["link"])
                continue
            if job_id not in candidates:
                candidates[job_id] = item
                feeds_by_id[job_id] = []
            feeds_by_id[job_id].append(label)

    new_rows, review_log = [], []
    for job_id, item in candidates.items():
        counters["scanned"] += 1
        try:
            posted = parse_rfc822_date(item["pubDate"])
            if posted and posted < cutoff:
                counters["excluded_old"] += 1
                continue

            meta = split_categories(item["categories"], item["author"])
            description = strip_html(item["description"])
            verdict = classify_feed_job(item["title"],
                                        meta["career_categories"],
                                        meta["themes"], description)
            if not verdict["in_scope"]:
                counters["excluded_out_of_scope"] += 1
                continue
            if job_id in known_ids:
                counters["duplicates"] += 1
                continue

            row = job_to_rich_row(item, meta, verdict, description,
                                  feeds_by_id[job_id])
        except Exception as exc:  # never let one job crash the run
            log.warning("Skipping malformed job %s: %s", job_id, exc)
            continue

        if verdict["needs_review"]:
            counters["needs_review"] += 1
            review_log.append(review_entry(row))
        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1
        if args.limit is not None and counters["new"] >= args.limit:
            break

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
    print("Candidates (unioned, deduped): {:>5,}".format(counters["scanned"]))
    print("Excluded (out of scope):       {:>5,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:          {:>5,}".format(counters["needs_review"]))
    print("New jobs added:                {:>5,}".format(counters["new"]))
    print("Duplicates skipped:            {:>5,}".format(counters["duplicates"]))
    if empty_feeds:
        print("Feeds that served 0 items (cold cache or nothing posted — "
              "see module docstring): " + ", ".join(empty_feeds))


if __name__ == "__main__":
    main()
