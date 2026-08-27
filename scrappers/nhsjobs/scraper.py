#!/usr/bin/env python3
"""Scrape NHS Jobs (jobs.nhs.uk) — the UK NHS's central vacancy board.

Data source
-----------
jobs.nhs.uk is fully server-rendered plain HTML (probed 2026-08-27, stock
UA, no anti-bot, no JS needed). Two surfaces:

    search   https://www.jobs.nhs.uk/candidate/search/results?keyword=...
    detail   https://www.jobs.nhs.uk/candidate/jobadvert/<reference>

There is NO JSON-LD anywhere and NO robots.txt: /robots.txt, /sitemap.xml
and every other unknown path return the same 200 "Service Domain
Information" HTML page, so there is no sitemap to crawl and no
machine-readable crawl policy to honour beyond politeness. The scraper
still asks for /robots.txt at startup and only enforces it when the
response really is a robots file (see check_robots).

Enumeration: staffGroup is a true partition (fetch wide)
--------------------------------------------------------
`keyword=` (empty) returns the WHOLE board — 12,972 live adverts on
2026-08-27 — so no keyword is needed and none is used. Keyword matching
here is extremely loose anyway (`keyword=public health` alone returned
9,307 of those 12,972), which is exactly why a keyword would be a bad
scope: it is a relevance ranker, not a filter.

The board is enumerated instead through the search form's own `staffGroup`
facet, whose nine values were measured to partition the board EXACTLY:

    CLINICAL_SERVICES              1537   ADMINISTRATIVE_AND_CLERICAL 2325
    PROF_SCIENTIFIC_AND_TECHNICAL   366   ALLIED_HEALTH_PROF          2042
    ESTATES_AND_ANCILLARY          1477   HEALTHCARE_SCIENTISTS        316
    MEDICAL_AND_DENTAL             2097   NURSING_AND_MIDWIFERY_REGD  2808
    STUDENTS                          4   -------------------------  -----
                                          sum                        12972

Crawling all nine is therefore the widest possible enumeration — it is the
whole board, not a health facet — at the same page cost as the unfaceted
crawl (each advert appears in exactly one group), and it buys every row the
site's own curated occupational tag for free. The partition is re-asserted
at runtime against the unfaceted total: a drift means NHS added a group,
and the run warns loudly rather than silently under-crawling.

Each group is walked with `sort=publicationDateDesc` (newest first) and
retired once its pages stop holding anything on/after the cutoff. The nine
walks are interleaved a page at a time, so a run cut short by --limit or a
network failure holds a balanced slice of the board rather than all of
nursing and none of the administrative and scientific groups.

Cost: the listing card carries the posted date, employer, location, salary,
contract type and working pattern, so the date gate runs BEFORE any detail
fetch — old adverts cost zero detail requests, ever. Pages hold a fixed 10
results (a `pageSize` parameter is accepted and ignored). Deep pagination
works: page 1298 of 1298 was served normally.

The listing-stage veto short-circuit
------------------------------------
The board publishes ~1,000 new adverts a day and the great majority are
clinical, so a detail fetch for every one is the dominant cost. One skip is
taken, and only one, because it is provably free of judgement:
`taxonomy_keywords.classify_subcategory` applies the negative-keyword veto
to `title + " " + skills` and NEVER to the description. So for a row whose
title/staff-group already trips the veto, `classify_job(title, group, "")`
and `classify_job(title, group, <description>)` return the identical
verdict, and fetching the description cannot change it. Those rows are
dropped at the listing stage and recorded as `vetoed_by`.

Every other row IS fetched and classified on its full description — a
row that merely fails to score without a description is never dropped
early, because the description is exactly what would have scored it. The
scraper therefore never runs a second, weaker classifier of its own.

Verified quirks
---------------
* Detail pages render the same content twice (a `show-mobile` block and a
  `hide-mobile` block), duplicating several element ids. Every field is
  read from the FIRST occurrence only.
* Dates are rendered as human text ("27 August 2026") in both surfaces —
  parsed to ISO, never reformatted from a locale-dependent library.
* `payscheme-band` ("Band 7") exists only on Agenda-for-change adverts;
  medical/dental and non-AfC adverts leave it blank. It is captured raw as
  a source column and never used to decide scope.
* Salaries are GBP. The club schema's salary_currency enum only admits
  INR/USD, so the club CSV's salary columns stay blank (the reed
  precedent) and the verbatim range lives in the rich CSV.
* `employer_country` is "United Kingdom" on essentially every advert; a
  handful of overseas-recruitment adverts name another country and are
  kept as-is in the rich CSV.

Classification (shared taxonomy)
--------------------------------
The keep/drop and labelling decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the human-readable staffGroup label ("Administrative and
                Clerical", "Allied Health Professional", ...) — the site's
                own curated occupational tag, the same use reed makes of
                its taxonomyLevel1/2. It is kept in the rich CSV as a raw
                source column and never decides the category by itself.
* description = job summary + main duties + job responsibilities + the
                Person Specification criteria, HTML-stripped and joined.
                The person-spec bullets are folded into the description
                (weight x1) rather than passed as `skills` (weight x2):
                they are requirement prose, not tags, and smuggling them
                in at double weight would be a second classifier.

in_scope False -> DROPPED, counted excluded_out_of_scope, full row appended
to out-of-scope.csv (never the main CSV). in_scope True fills
category/sub_category/role_family and the score-trace columns;
needs_review True keeps the row AND appends it to needs_review.csv.

Outputs
-------
* nhsjobs_jobs.csv — rich cumulative store (dedup key: job_id = the NHS
  advert reference), source of truth for the incremental watermark.
* ../../jobs_csv/<DD-MM-YYYY>/nhsjobs.csv — the same jobs mapped to the
  HealthCareers.club CLUB_COLUMNS schema.
* out-of-scope.csv — dropped rows + their skip list (reversible).
* needs_review.csv — kept but flagged.

There is deliberately no seen_old_ids.csv (the devnetjobsindia layout has
one): there the posted date lived only on the detail page, so old ids had
to be remembered to avoid re-fetching them. Here the listing card carries
the date, so an out-of-window advert is recognised for free on every run
and never costs a detail request in the first place.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored posted_date minus
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
from urllib.parse import urlencode

import pandas as pd
import requests

# The one shared classifier (see ../../instructions/taxonomy-migration-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "nhsjobs"
SITE_BASE = "https://www.jobs.nhs.uk"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SEARCH_URL = SITE_BASE + "/candidate/search/results"
JOB_URL_TEMPLATE = SITE_BASE + "/candidate/jobadvert/{}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# The nine staffGroup facet values, measured 2026-08-27 to partition the
# board exactly (sum of the nine == the unfaceted total). Crawling all nine
# IS the whole board; the labels are the site's curated occupational tag.
STAFF_GROUPS = {
    "CLINICAL_SERVICES": "Additional Clinical Services",
    "PROF_SCIENTIFIC_AND_TECHNICAL": "Additional Professional Scientific and Technical",
    "ADMINISTRATIVE_AND_CLERICAL": "Administrative and Clerical",
    "ALLIED_HEALTH_PROF": "Allied Health Professional",
    "ESTATES_AND_ANCILLARY": "Estates and Ancillary",
    "HEALTHCARE_SCIENTISTS": "Healthcare Scientist",
    "MEDICAL_AND_DENTAL": "Medical and Dental",
    "NURSING_AND_MIDWIFERY_REGD": "Nursing and Midwifery Registered",
    "STUDENTS": "Students",
}

RESULTS_PER_PAGE = 10          # fixed by the site; pageSize is ignored

# NHS adverts are short-lived (typically two weeks to closing) and the board
# turns over ~1,000 adverts a day, so the first-run window is deliberately
# narrow — widen it with --since when seeding a bigger corpus. No salary
# filter (master spec §3): salaries are captured, never filtered on.
INITIAL_WINDOW_DAYS = 7
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000
# A group's walk stops after this many consecutive all-out-of-window pages.
# One page of tolerance absorbs the re-sort drift caused by adverts being
# published while the walk is in flight.
STALE_PAGE_TOLERANCE = 2
MAX_PAGES_PER_GROUP = 2_000    # runaway guard, far above the real ~300

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "nhsjobs_jobs.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "county", "postcode",
    "country", "staff_group", "pay_scheme", "band", "contract_type",
    "working_pattern", "salary_raw", "salary_min", "salary_max",
    "salary_currency", "salary_period", "category", "sub_category",
    "role_family", "all_families", "family_scores", "family_confidence",
    "matched_in", "needs_review", "vetoed_by", "posted_date", "closing_date",
    "employer_website", "description", "job_url", "scraped_at",
]

log = logging.getLogger("nhsjobs_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    markup = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup or "",
                    flags=re.S | re.IGNORECASE)
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table)[^>]*>", " ",
                    markup, flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_HUMAN_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")


def parse_human_date(text):
    """"27 August 2026" / "01 September 2026" -> "2026-08-27". "" if absent.

    Both surfaces render dates as English text only, so this is parsed by
    hand rather than through a locale-dependent library.
    """
    match = _HUMAN_DATE_RE.search(clean_text(text))
    if not match:
        return ""
    day, month, year = match.groups()
    month_num = _MONTHS.get(month.lower())
    if not month_num:
        return ""
    return "{}-{:02d}-{:02d}".format(year, month_num, int(day))


# ---- search results page ----------------------------------------------------

# Cards nest <li> elements, so they are split on the panel's opening tag
# rather than matched with a non-greedy .*?</li>.
_CARD_SPLIT_RE = re.compile(r'<li class="nhsuk-list-panel search-result')
_JOB_REF_RE = re.compile(r'href="/candidate/jobadvert/([^?"]+)')
_RESULT_COUNT_RE = re.compile(r"([\d,]+)\s+jobs?\s+found", re.IGNORECASE)


def parse_result_count(page_html):
    """The "N jobs found" total, or None when the page has no count."""
    match = _RESULT_COUNT_RE.search(page_html or "")
    return int(match.group(1).replace(",", "")) if match else None


def _card_field(card, test_id):
    """Text of a card's `data-test="<test_id>"` <li>, label stripped."""
    match = re.search(r'data-test="%s"[^>]*>(.*?)</li>' % re.escape(test_id),
                      card, re.S)
    if not match:
        return ""
    text = strip_html(match.group(1))
    return re.sub(r"^[A-Za-z ]+:\s*", "", text).strip()


# The location block nests the town inside the employer's own <h3>:
#   <div data-test="search-result-location">
#     <h3>Employer name<div class="location-font-size">Town PostCode</div>
# so the employer is the <h3>'s text UP TO that inner <div>, and the place
# is the inner <div>'s text.
_LOCATION_BLOCK_RE = re.compile(
    r'data-test="search-result-location"[^>]*>(.*?)</div>', re.S)
_EMPLOYER_RE = re.compile(r"<h3[^>]*>(.*?)(?=<div|</h3>|$)", re.S)
_PLACE_RE = re.compile(r'class="location-font-size"[^>]*>(.*?)$', re.S)


def parse_search_cards(page_html):
    """Every advert card on one results page, as dicts of raw card fields."""
    cards = []
    for card in _CARD_SPLIT_RE.split(page_html or "")[1:]:
        ref = _JOB_REF_RE.search(card)
        if not ref:
            continue
        title = re.search(
            r'data-test="search-result-job-title"[^>]*>(.*?)</a>', card, re.S)
        location = _LOCATION_BLOCK_RE.search(card)
        employer, place = "", ""
        if location:
            block = location.group(1)
            employer_match = _EMPLOYER_RE.search(block)
            employer = strip_html(employer_match.group(1)) if employer_match else ""
            place_match = _PLACE_RE.search(block)
            place = strip_html(place_match.group(1)) if place_match else ""
        cards.append({
            "job_id": html_lib.unescape(ref.group(1)),
            "title": clean_text(strip_html(title.group(1))) if title else "",
            "company": employer,
            "location": place,
            "salary_raw": _card_field(card, "search-result-salary"),
            "posted_date": parse_human_date(
                _card_field(card, "search-result-publicationDate")),
            "closing_date": parse_human_date(
                _card_field(card, "search-result-closingDate")),
            "contract_type": _card_field(card, "search-result-jobType"),
            "working_pattern": _card_field(card, "search-result-workingPattern"),
        })
    return cards


# ---- detail page ------------------------------------------------------------

def _by_id(page_html, element_id):
    """Text of the FIRST element carrying `id="<element_id>"`.

    Detail pages render a show-mobile and a hide-mobile copy of several
    blocks, duplicating ids; only the first copy is ever read.
    """
    match = re.search(
        r'id="%s"[^>]*>(.*?)</(?:p|h1|h2|h3|h4|div|span|li|a)>'
        % re.escape(element_id), page_html or "", re.S)
    return strip_html(match.group(1)) if match else ""


# The employer-authored prose blocks are <p id="..."> wrappers holding raw
# employer HTML, and NHS nests <p> inside <p> without closing the wrapper —
# so the block's own </p> is unreliable and tag-depth counting is not safe.
# Each block is instead read up to the next section boundary, which the
# page's own template always emits: a heading, the end of the collapsible
# job-description <details>, or the start of the mobile/desktop twin.
_BLOCK_END_RE = re.compile(
    r'<h2\b|<h3\b|</details>|<div class="(?:show|hide)-mobile"', re.IGNORECASE)


def _block_by_id(page_html, element_id):
    """Text of the prose block opened by `id="<element_id>"`."""
    match = re.search(r'<[a-z]+[^>]*\sid="%s"[^>]*>' % re.escape(element_id),
                      page_html or "", re.IGNORECASE)
    if not match:
        return ""
    rest = page_html[match.end():]
    end = _BLOCK_END_RE.search(rest)
    return strip_html(rest[:end.start()] if end else rest[:DESCRIPTION_MAX_CHARS])


_CRITERIA_ID_RE = re.compile(
    r'id="(?:essential|desirable)_skill_\d+_criteria_\d+"[^>]*>(.*?)</li>', re.S)

# Ordered as the advert reads: summary, duties, full responsibilities, then
# the person specification. `about_organisation` is boilerplate about the
# trust, not the role, and is deliberately excluded from the classifier's
# description.
_DESCRIPTION_IDS = ("job_overview", "job_description", "job_description_large")


def parse_description(page_html):
    """Summary + duties + responsibilities + person-spec criteria, joined."""
    parts = [_block_by_id(page_html, i) for i in _DESCRIPTION_IDS]
    seen, criteria = set(), []
    for raw in _CRITERIA_ID_RE.findall(page_html or ""):
        text = strip_html(raw)
        if text and text not in seen:      # mobile/desktop duplicate blocks
            seen.add(text)
            criteria.append(text)
    parts.extend(criteria)
    return clean_text(" ".join(p for p in parts if p))[:DESCRIPTION_MAX_CHARS]


def parse_detail(page_html):
    """Structured fields of one advert detail page."""
    website = re.search(r'id="employer_website_url_link"[^>]*href="([^"]+)"',
                        page_html or "")
    return {
        "title": _by_id(page_html, "heading"),
        "company": _by_id(page_html, "employer_name"),
        "posted_date": parse_human_date(_by_id(page_html, "date_posted")),
        "closing_date": parse_human_date(_by_id(page_html, "closing_date")),
        "pay_scheme": _by_id(page_html, "payscheme-type"),
        "band": _by_id(page_html, "payscheme-band"),
        "salary_raw": _by_id(page_html, "range_salary"),
        "contract_type": _by_id(page_html, "contract_type"),
        "city": _by_id(page_html, "employer_town"),
        "county": _by_id(page_html, "employer_county"),
        "postcode": _by_id(page_html, "employer_postcode"),
        "country": _by_id(page_html, "employer_country"),
        "employer_website": clean_text(website.group(1)) if website else "",
        "description": parse_description(page_html),
    }


# ---- salary -----------------------------------------------------------------

_MONEY_RE = re.compile(r"£\s*([\d,]+(?:\.\d+)?)")
_PERIODS = [
    (re.compile(r"\ba year\b|\bper annum\b|\bpa\b", re.IGNORECASE), "per_annum"),
    (re.compile(r"\ba month\b|\bper month\b", re.IGNORECASE), "per_month"),
    (re.compile(r"\ba week\b|\bper week\b", re.IGNORECASE), "per_week"),
    (re.compile(r"\ban hour\b|\bper hour\b|\bhourly\b", re.IGNORECASE), "per_hour"),
    (re.compile(r"\ba session\b|\bper session\b", re.IGNORECASE), "per_session"),
]


def parse_salary(raw):
    """"£49,387 to £56,515 a year" -> (49387, 56515, "GBP", "per_annum").

    Captured verbatim, never invented: "Depends on experience" and
    "Negotiable" yield ("", "", "", "").
    """
    raw = clean_text(raw)
    amounts = [a.replace(",", "") for a in _MONEY_RE.findall(raw)]
    if not amounts:
        return "", "", "", ""
    values = [str(int(round(float(a)))) for a in amounts]
    period = ""
    for regex, name in _PERIODS:
        if regex.search(raw):
            period = name
            break
    return values[0], values[-1], "GBP", period


# ---- job_type ---------------------------------------------------------------

def map_job_type(working_pattern, contract_type=""):
    """Club job_type enum from the advert's working pattern."""
    pattern = (working_pattern or "").lower()
    if "hybrid" in pattern or "hybrid" in (contract_type or "").lower():
        return "hybrid"
    if "remote" in pattern or "home working" in pattern:
        return "remote"
    if "part time" in pattern or "part-time" in pattern:
        if "full time" not in pattern and "full-time" not in pattern:
            return "part_time"
    return "full_time"


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The staffGroup label is the site's curated occupational tag and is
    passed as the `skills` signal (the reed idiom); it stays in the rich
    CSV as a raw source column and never decides the category on its own.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("staff_group", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    row["needs_review"] = verdict["needs_review"]
    row["vetoed_by"] = verdict["vetoed_by"]
    return verdict["in_scope"]


def listing_veto(title, staff_group):
    """The negative keyword that already blocks this card, or "".

    `classify_subcategory` runs the veto over `title + " " + skills` and
    never over the description, so a card that trips it here would trip it
    identically after a detail fetch — the description cannot rescue it.
    This is the ONLY pre-fetch drop, and it is the shared classifier's own
    verdict, not a second one: a card that merely fails to score without a
    description is NOT dropped here.
    """
    return classify_job(title, staff_group, "")["vetoed_by"]


# company_type (hospital|pharma) is a separate club field, NOT a category;
# NHS trusts and their suppliers default to "hospital" (fleet convention).
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
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return session


def check_robots(session):
    """Honour robots.txt when the site actually serves one.

    jobs.nhs.uk answers /robots.txt with its generic 200 HTML page rather
    than a robots file, and feeding HTML to RobotFileParser would silently
    "allow everything" for the wrong reason. So the response is only
    enforced when it really is text/plain; otherwise the absence is logged.
    """
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    content_type = resp.headers.get("Content-Type", "")
    if resp.status_code >= 400 or "text/plain" not in content_type:
        log.info("No robots.txt served (HTTP %d, %s) — crawling politely at "
                 "%.1fs/request", resp.status_code,
                 content_type.split(";")[0] or "no content-type",
                 REQUEST_DELAY_SECONDS)
        return
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    rp.parse(resp.text.splitlines())
    for url in (SEARCH_URL, JOB_URL_TEMPLATE.format("C9999-26-0001")):
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


def search_url(staff_group=None, page=1):
    params = [("keyword", ""), ("sort", "publicationDateDesc"),
              ("language", "en"), ("page", str(page))]
    if staff_group:
        params.append(("staffGroup", staff_group))
    return SEARCH_URL + "?" + urlencode(params)


def assert_partition(session):
    """Re-check that the nine staff groups still cover the whole board.

    A mismatch means NHS added or renamed a group and the crawl would
    silently miss adverts — worth a loud warning, not a silent pass.
    """
    total_html = fetch(session, search_url())
    board_total = parse_result_count(total_html or "")
    if board_total is None:
        log.warning("Could not read the unfaceted board total — "
                    "skipping the staffGroup partition check")
        return None, {}
    counts = {}
    for group in STAFF_GROUPS:
        html_page = fetch(session, search_url(group))
        counts[group] = parse_result_count(html_page or "") or 0
    covered = sum(counts.values())
    if covered != board_total:
        log.warning("staffGroup partition DRIFT: the nine groups cover %d of "
                    "%d live adverts (%d unreachable) — NHS has probably "
                    "added a staff group; update STAFF_GROUPS.",
                    covered, board_total, board_total - covered)
    else:
        log.info("staffGroup partition verified: %d adverts across %d groups "
                 "== the unfaceted board total", covered, len(counts))
    return board_total, counts


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(card, detail, staff_group_label):
    """Rich row from a search card plus its detail page.

    Detail values win where both surfaces carry a field (the card truncates
    and the detail page is the advert itself); the card fills the fields the
    detail page does not publish (working pattern).
    """
    salary_raw = detail.get("salary_raw") or card.get("salary_raw", "")
    lo, hi, currency, period = parse_salary(salary_raw)
    return {
        "source": SITE,
        "job_id": card["job_id"],
        "title": detail.get("title") or card.get("title", ""),
        "company": detail.get("company") or card.get("company", ""),
        "city": detail.get("city") or card.get("location", ""),
        "county": detail.get("county", ""),
        "postcode": detail.get("postcode", ""),
        "country": detail.get("country") or "United Kingdom",
        "staff_group": staff_group_label,
        "pay_scheme": detail.get("pay_scheme", ""),
        "band": detail.get("band", ""),
        "contract_type": detail.get("contract_type") or card.get("contract_type", ""),
        "working_pattern": card.get("working_pattern", ""),
        "salary_raw": salary_raw,
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": currency,
        "salary_period": period,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "vetoed_by": "",
        "posted_date": detail.get("posted_date") or card.get("posted_date", ""),
        "closing_date": detail.get("closing_date") or card.get("closing_date", ""),
        "employer_website": detail.get("employer_website", ""),
        "description": detail.get("description", ""),
        "job_url": JOB_URL_TEMPLATE.format(card["job_id"]),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def card_only_row(card, staff_group_label, vetoed_by):
    """A veto-dropped row, built from the card alone (no detail fetch)."""
    lo, hi, currency, period = parse_salary(card.get("salary_raw", ""))
    row = build_row(card, {}, staff_group_label)
    row.update({"salary_min": lo, "salary_max": hi,
                "salary_currency": currency, "salary_period": period,
                "vetoed_by": vetoed_by})
    return row


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map to the 23-column club schema (CLUB_COLUMNS, shared).

    GBP cannot be represented by the club salary_currency enum (INR/USD
    only), so the club salary columns stay blank and the verbatim range
    lives on in the rich CSV — the reed precedent. Never invented.
    """
    return {
        "country_name": _blank(r.get("country")) or "United Kingdom",
        "country_code": "GB",
        "country_dial_code": "+44",
        "city_name": _blank(r.get("city")) or _blank(r.get("county")) or "United Kingdom",
        "company_name": _blank(r.get("company")),
        "company_type": classify_company_type(_blank(r.get("company"))),
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": map_job_type(_blank(r.get("working_pattern")),
                                 _blank(r.get("contract_type"))),
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        # NHS publishes no years-of-experience field and the adverts state
        # requirements in prose — left blank rather than inferred.
        "min_experience": "",
        "max_experience": "",
        "qualification": extract_qualification(_blank(r.get("description"))),
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


def append_out_of_scope(rows, path=None):
    """Append dropped rich rows, deduped on job_id. Moved, never discarded."""
    path = path or OUT_OF_SCOPE_CSV      # resolved at call time, not def time
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


def iter_cards(session, groups, cutoff, counters, max_pages=MAX_PAGES_PER_GROUP):
    """Yield (card, staff_group_label) newest-first, ROUND-ROBIN by group.

    Each group is walked with sort=publicationDateDesc and retired once
    STALE_PAGE_TOLERANCE consecutive pages hold nothing on/after the cutoff.

    The groups are interleaved a page at a time rather than drained one at
    a time. The full crawl reads the same pages either way, but a run that
    is cut short — `--limit`, a network failure, an interrupt — then holds
    a balanced slice of the board instead of 100% of nursing and 0% of the
    administrative and scientific groups that carry most of the in-scope
    supply.
    """
    live = {group: {"page": 1, "stale": 0, "kept": 0}
            for group in groups}
    while live:
        for group in list(live):
            state = live[group]
            label = groups[group]
            if state["page"] > max_pages:
                del live[group]
                continue
            page_html = fetch(session, search_url(group, state["page"]))
            if page_html is None:
                log.warning("Listing fetch failed: %s page %d — retiring group",
                            group, state["page"])
                del live[group]
                continue
            cards = parse_search_cards(page_html)
            counters["listing_pages"] += 1
            if not cards:
                del live[group]
                continue
            fresh = 0
            for card in cards:
                counters["scanned"] += 1
                if card["posted_date"] and card["posted_date"] < cutoff:
                    counters["excluded_old"] += 1
                    continue
                fresh += 1
                state["kept"] += 1
                yield card, label
            state["stale"] = 0 if fresh else state["stale"] + 1
            state["page"] += 1
            if state["stale"] >= STALE_PAGE_TOLERANCE or len(cards) < RESULTS_PER_PAGE:
                log.info("%-48s %5d in-window adverts over %d pages",
                         label, state["kept"], state["page"] - 1)
                del live[group]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape NHS Jobs (jobs.nhs.uk).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail pages "
                             "(test runs; the crawl is round-robin across "
                             "staff groups, so a limited run stays balanced)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job "
                             "posted on/after this date")
    parser.add_argument("--staff-group", action="append", metavar="GROUP",
                        choices=sorted(STAFF_GROUPS),
                        help="crawl only these staffGroup(s) (repeatable); "
                             "default is all nine, i.e. the whole board")
    parser.add_argument("--no-partition-check", action="store_true",
                        help="skip the 10-request staffGroup partition audit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    groups = ({g: STAFF_GROUPS[g] for g in args.staff_group}
              if args.staff_group else dict(STAFF_GROUPS))
    if args.staff_group:
        log.info("Crawling %d of %d staff groups (--staff-group): %s",
                 len(groups), len(STAFF_GROUPS), ", ".join(sorted(groups)))

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    out_of_scope_ids = load_id_column(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs (%d known out-of-scope skipped); "
             "keeping jobs posted on/after %s%s", len(known_ids),
             len(out_of_scope_ids), cutoff,
             " (--since override)" if args.since else "")

    if not args.no_partition_check:
        assert_partition(session)

    counters = {"scanned": 0, "listing_pages": 0, "duplicates": 0,
                "skipped_out_of_scope": 0, "excluded_old": 0,
                "excluded_vetoed": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "detail_failed": 0}
    new_rows, review_log, dropped_rows = [], [], []
    fetched = 0

    for card, label in iter_cards(session, groups, cutoff, counters):
        job_id = card["job_id"]
        if job_id in known_ids:
            counters["duplicates"] += 1
            continue
        if job_id in out_of_scope_ids:
            counters["skipped_out_of_scope"] += 1
            continue

        # The shared classifier's own veto, decided on title + staff group —
        # the description provably cannot change it, so no detail fetch.
        vetoed_by = listing_veto(card["title"], label)
        if vetoed_by:
            counters["excluded_vetoed"] += 1
            counters["excluded_out_of_scope"] += 1
            out_of_scope_ids.add(job_id)
            dropped_rows.append(card_only_row(card, label, vetoed_by))
            continue

        if args.limit is not None and fetched >= args.limit:
            log.info("--limit %d reached — stopping", args.limit)
            break

        detail_html = fetch(session, JOB_URL_TEMPLATE.format(job_id))
        fetched += 1
        if not detail_html:
            counters["detail_failed"] += 1
            continue
        try:
            row = build_row(card, parse_detail(detail_html), label)
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed advert %s: %s", job_id, exc)
            counters["detail_failed"] += 1
            continue
        if not row["title"] or not row["description"]:
            counters["detail_failed"] += 1
            log.warning("Advert %s parsed with no title/description — "
                        "page shape changed?", job_id)
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
                               "staff_group": row["staff_group"],
                               "band": row["band"],
                               "category": row["category"],
                               "sub_category": row["sub_category"],
                               "role_family": row["role_family"],
                               "job_url": row["job_url"]})
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
    if dropped_rows:
        total = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total)
    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    considered = counters["new"] + counters["excluded_out_of_scope"]
    rate = (100.0 * counters["new"] / considered) if considered else 0.0
    print("\n===== Run summary =====")
    print("Listing pages fetched:        {:>6,}".format(counters["listing_pages"]))
    print("Adverts scanned:              {:>6,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("  ...of which vetoed on title:{:>6,}  (no detail fetch)".format(
        counters["excluded_vetoed"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail parse failures:        {:>6,}".format(counters["detail_failed"]))
    print("Detail pages fetched:         {:>6,}".format(fetched))
    print("In-scope rate:                {:>9.2f}%  ({:,} of {:,} classified)"
          .format(rate, counters["new"], considered))


if __name__ == "__main__":
    main()
