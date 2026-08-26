#!/usr/bin/env python3
"""Scrape job postings from pharmatutor.org (pharma career portal, India).

Data source
-----------
PharmaTutor is a Drupal site, fully server-rendered, no anti-bot (probed
2026-08-26). Its supply is Indian pharma/govt postings: government
pharmacist and CMHO notices, company R&D/regulatory/medical-writing roles,
walk-in manufacturing drives, research fellowships. Similar profile to
pharmarecruiter — most of the board is manufacturing/QC/dispensing the
taxonomy does not cover, so a large share is dropped as
excluded_out_of_scope; the keepers are medical writers, regulatory,
pharmacovigilance and clinical-research roles.

Discovery is the union of two sources (the reliefweb idiom — one source
alone cannot be trusted here):

* https://www.pharmatutor.org/rss.xml — the 20 newest posts, exact
  pubDate, immune to the page cache. The first line of discovery.
* https://www.pharmatutor.org/taxonomy/term/1754?page=N — the "vacancies"
  tag term page, the tag every job post carries. This is the ONE real
  archive: a Drupal term view with a working pager, 10 teasers per page,
  paginating months back (page 40 is July at Aug volumes, ~13 posts/day).

The obvious listing, /pharma-jobs?page=N, is a TRAP: the view has no
pager at all — the `page` param is ignored and every ?page=N URL is just
a separate page-cache key holding a snapshot of the same 6 newest cards
taken whenever that URL was first hit (Age ~1h, X-Drupal-Cache HIT), so
probing it looks like working pagination until the cache turns over and
every page serves the identical set. Measured live 2026-08-26.

Cards carry NO date, but every job URL embeds its posting month:

    /content/<month>-<year>/<slug>      e.g. /content/august-2026/job-...

so a URL whose month is strictly before the cutoff month is old without
fetching it. The term walk stops only on a page whose every card is
PROVABLY old by that URL month (or at the page cap/stale stop) — a
stale-cached or repeated page is harmless because the skip lists make
re-seen cards free.

Detail pages embed an Article (not JobPosting) JSON-LD in an @graph with
the exact `datePublished` (IST), clean `headline` and a short
`description`. The real content is the `field--name-body` div: free-form
editorial HTML with `<strong>Label :</strong> value` runs — Post/
Designation, Location ("Hyderabad / India"), Experience ("12+ years"),
Qualification, Salary ("Rs 16,500/- pm"), End Date ("30th September
2026") — labels vary by post type (company post vs govt notice vs walk-in
drive) and multi-role drives repeat them (first match wins, the
pharmarecruiter idiom). A `post-tags` div carries the site's curated tags
(role, qualifications, company, state, Government/Company Jobs) — passed
to classify_job as `skills`, kept in the rich CSV as a raw source column.

robots.txt is stock Drupal (only /core/, /admin/, /search/ etc.
disallowed); /pharma-jobs and /content/ are allowed. Verified at startup
with urllib.robotparser.

Verified quirks
---------------
* The exact posted date exists ONLY in the detail JSON-LD, so in-month
  URLs must be fetched before the cutoff can be judged. Out-of-window
  fetches are remembered in seen_old_ids.csv and dropped ids in
  out-of-scope.csv (full rows, reversible) — each URL is fetched at most
  once, ever. Steady state ≈ 1 RSS request + a handful of listing pages
  (the walk ends after MAX_STALE_PAGES consecutive pages needing zero
  detail fetches) + 1 detail request per genuinely new posting.
* No structured company field. The site titles posts "<role> at
  <employer>" almost universally; the employer is extracted from the
  title (or the "<College> invites applications" pattern), else left
  blank and flagged needs_review.
* Salary appears only on govt notices ("Rs 16,500/- pm") and rarely on
  company posts → parsed when present, otherwise "Not Disclosed", never
  invented.
* Govt notices name no Location label; their state arrives as a tag
  ("Chhattisgarh") — the tag list is scanned against the Indian states
  as a fallback.
* The body ends with a pasted alerts footer ("See All … B.Pharm Alerts …
  Subscribe") that is part of the body field — truncated from the
  description at the first "See All" (kept whole when that would leave
  almost nothing).
* One post often advertises a multi-role walk-in drive; the post title is
  kept as the job title (matching how the site presents it).

Classification (shared taxonomy)
--------------------------------
The whole board is crawled — fetch wide, filter tight. The keep/drop and
labeling decision belongs to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the detail page's tags, joined
* description = the body HTML, footer-truncated and HTML-stripped

in_scope False (manufacturing, QC/QA, dispensing pharmacist, academia) ->
DROPPED, counted excluded_out_of_scope, full row appended to
out-of-scope.csv. in_scope True fills category/sub_category/role_family
and the score-trace columns; needs_review True keeps the row AND appends
it to needs_review.csv.

Outputs
-------
* pharmatutor_jobs.csv — rich cumulative store (dedup key: job_id, the
  "<month>-<year>/<slug>" URL path), source of truth for the watermark.
* ../../jobs_csv/<DD-MM-YYYY>/pharmatutor.csv — the same jobs mapped to
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
from classification import classify_job, extract_qualification, CLUB_COLUMNS  # noqa: E402

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "pharmatutor"
SITE_BASE = "https://www.pharmatutor.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
RSS_URL = SITE_BASE + "/rss.xml"
# the "vacancies" tag term page — the one real paginated archive
# (see the /pharma-jobs pager trap in the module docstring)
LISTING_URL = SITE_BASE + "/taxonomy/term/1754"
JOB_URL_TEMPLATE = SITE_BASE + "/content/{}"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# The board posts a handful of jobs per day (6 cards per listing page).
# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 14
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 10
# The listing archive runs years deep; the date stop always fires long
# before this, so the cap is a runaway backstop only.
MAX_LISTING_PAGES = 120
# Steady-state walk stop: this many consecutive term pages needing zero
# detail fetches (~30 already-processed posts, several days of supply —
# deeper than any page-cache staleness) means the window is fully covered.
MAX_STALE_PAGES = 3
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "pharmatutor_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "position", "company", "city", "state",
    "country", "country_code", "country_dial_code", "salary_raw",
    "salary_min", "salary_max", "salary_period", "salary_currency",
    "experience_raw", "min_experience", "max_experience", "qualification",
    "tags", "category", "sub_category", "role_family", "all_families",
    "family_scores", "family_confidence", "matched_in", "company_type",
    "needs_review", "posted_date", "end_date", "description", "job_url",
    "scraped_at",
]

log = logging.getLogger("pharmatutor_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table|blockquote)[^>]*>",
                    " ", markup or "", flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


# ---- listing pages ----------------------------------------------------------

# The term page's teasers live in the infinite-scroll wrapper; sidebar
# blocks outside it link the same newest posts and must not leak in.
_WRAPPER_RE = re.compile(
    r"views-infinite-scroll-content-wrapper(.*?)(?:js-pager__items|$)", re.S)
# a text-bearing anchor is the teaser's title link, its text possibly
# span-wrapped (image anchors wrap <img>/<div> tags and never match)
_CARD_RE = re.compile(
    r'<a href="/content/([^"]+)"[^>]*>\s*(?:<span>)?\s*'
    r'([^<\s][^<]*?)\s*(?:</span>)?\s*</a>')


def parse_listing_cards(page_html):
    """[(job_id, title), ...] from one term page, in page order, deduped.

    job_id is the URL path after /content/ ("august-2026/<slug>") — the
    month-year segment keeps slugs unique across time and lets the URL
    itself date the posting to the month.
    """
    m = _WRAPPER_RE.search(page_html or "")
    if not m:
        return []
    cards, seen = [], set()
    for path, title in _CARD_RE.findall(m.group(1)):
        if path not in seen:
            seen.add(path)
            cards.append((path, clean_text(title)))
    return cards


# ---- RSS feed ---------------------------------------------------------------

_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_RSS_LINK_RE = re.compile(r"<link>\s*([^<\s]+)\s*</link>")
_RSS_TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.S)


def parse_rss_items(xml):
    """[(job_id, title), ...] from rss.xml (site-wide, 20 newest posts)."""
    items = []
    for block in _RSS_ITEM_RE.findall(xml or ""):
        link = _RSS_LINK_RE.search(block)
        m = re.search(r"/content/(.+)$", link.group(1)) if link else None
        if not m:
            continue
        title = _RSS_TITLE_RE.search(block)
        items.append((m.group(1), clean_text(title.group(1)) if title else ""))
    return items


_URL_MONTH_RE = re.compile(r"^([a-z]+)-(\d{4})/")


def parse_url_month(job_id):
    """(year, month) from an "august-2026/<slug>" job_id, or None."""
    m = _URL_MONTH_RE.match(job_id or "")
    if m and m.group(1) in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1)]
    return None


def url_month_before_cutoff(job_id, cutoff):
    """True when the URL's month ends before the cutoff date — old without
    fetching. Unknown URL shapes are never judged old."""
    ym = parse_url_month(job_id)
    if not ym:
        return False
    year, month = ym
    return (year, month) < (int(cutoff[:4]), int(cutoff[5:7]))


# ---- detail pages -----------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S)


def parse_article_ldjson(page_html):
    """The schema.org Article node of a detail page's @graph, or {}."""
    for m in _LDJSON_RE.finditer(page_html or ""):
        try:
            data = json.loads(m.group(1), strict=False)
        except ValueError as exc:
            log.debug("Unparseable ld+json block: %s", exc)
            continue
        nodes = data.get("@graph", []) if isinstance(data, dict) else data
        if isinstance(data, dict) and data.get("@type") == "Article":
            nodes = [data]
        for item in nodes if isinstance(nodes, list) else []:
            if isinstance(item, dict) and item.get("@type") == "Article":
                return item
    return {}


# The page is littered with sidebar/page-builder blocks that reuse the
# body-field class, and "post-tags" appears in inlined CSS — every anchor
# below is scoped to the <article> element first.
_ARTICLE_RE = re.compile(r"<article[^>]*>(.*?)</article", re.S)
_BODY_RE = re.compile(
    r'<div class="post-content">(.*?)(?:<div class="post-tags|$)', re.S)
# Ad units and their scripts are embedded inside the body content.
_NOISE_RE = re.compile(r"<(script|style|ins)[^>]*>.*?</\1>", re.S | re.I)
# The alerts footer the editors paste into every body. Cutting at "See All"
# keeps the apply-link blockquote, which precedes it.
_FOOTER_RE = re.compile(r"See All", re.IGNORECASE)


def _article_html(page_html):
    m = _ARTICLE_RE.search(page_html or "")
    return m.group(1) if m else ""


def parse_body_html(page_html):
    """The article's post-content HTML, ad blocks and alerts footer removed."""
    m = _BODY_RE.search(_article_html(page_html))
    if not m:
        return ""
    body = _NOISE_RE.sub(" ", m.group(1))
    cut = _FOOTER_RE.search(body)
    if cut and cut.start() > 200:   # never truncate a body to almost nothing
        body = body[:cut.start()]
    return body


_TAGS_BLOCK_RE = re.compile(r'<div class="post-tags(.*)', re.S)
_TAG_ITEM_RE = re.compile(r'field__item"><a [^>]*>([^<]+)</a>')


def parse_tags(page_html):
    """The article's post-tags terms, joined ("; ")."""
    m = _TAGS_BLOCK_RE.search(_article_html(page_html))
    if not m:
        return ""
    return "; ".join(clean_text(t) for t in _TAG_ITEM_RE.findall(m.group(1))
                     if clean_text(t))


# Labels come as <strong>Label :</strong> value runs (the colon drifts in
# and out of the strong tag); the value runs to the next break/label.
_LABEL_RUN_RE = re.compile(
    r"<strong>\s*([^<]{2,60}?)\s*:?\s*</strong>\s*:?\s*"
    r"(.*?)(?=<br|</p|<strong|<h\d|$)", re.S)

# Bullet labels -> canonical field. First match wins per field
# (multi-role drives repeat the run for every role; the first is the lead).
_LABEL_MAP = [
    (re.compile(r"^(post|posts|position|positions|designation|job title|name of post)$", re.I), "position"),
    (re.compile(r"^(company name|company|organization|organisation|employer|hospital)$", re.I), "company"),
    (re.compile(r"^(location|job location|venue location|place of posting)$", re.I), "location"),
    (re.compile(r"^(experience|experience required)$", re.I), "experience"),
    (re.compile(r"^(qualification|qualifications|education qualification|educational qualification|education|eligibility)$", re.I), "qualification"),
    (re.compile(r"^(salary|stipend|ctc|pay|remuneration|pay scale|salary range|emoluments)$", re.I), "salary"),
    (re.compile(r"^(end date|last date|closing date|last date(?: for| of)? application(?: submission)?)$", re.I), "end_date"),
]


def extract_labeled_fields(body_html):
    """Pull `<strong>Label :</strong> value` runs out of the body HTML."""
    fields = {}
    for label, value in _LABEL_RUN_RE.findall(body_html or ""):
        label, value = clean_text(label).rstrip(":").strip(), strip_html(value)
        if not value:
            continue
        for pattern, field in _LABEL_MAP:
            if pattern.match(label) and field not in fields:
                fields[field] = value
                break
    return fields


def parse_posted_date(article):
    """ISO date from the Article's datePublished (IST timestamp)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(article.get("datePublished")))
    return m.group(1) if m else ""


_END_DMY_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+),?\s+(\d{4})", re.I)
_END_MDY_RE = re.compile(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)


def parse_end_date(raw):
    """"30th September 2026" / "September 8, 2026" -> ISO, else ""."""
    text = clean_text(raw)
    m = _END_DMY_RE.search(text)
    if m and m.group(2).lower() in MONTHS:
        return "{}-{:02d}-{:02d}".format(m.group(3), MONTHS[m.group(2).lower()],
                                         int(m.group(1)))
    m = _END_MDY_RE.search(text)
    if m and m.group(1).lower() in MONTHS:
        return "{}-{:02d}-{:02d}".format(m.group(3), MONTHS[m.group(1).lower()],
                                         int(m.group(2)))
    return ""


# ---- field parsers (pharmarecruiter idioms) ---------------------------------

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FOREIGN_CUR_RE = re.compile(r"\b(AED|SAR|QAR|KWD|BHD|OMR|USD|GBP|EUR)\b|[$£€]")
_LPA_RE = re.compile(r"lpa|lakh|lac", re.IGNORECASE)
_K_SUFFIX_RE = re.compile(r"\d\s*k\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"per\s*year|/\s*year|annum|p\.?a\b|yearly|annual|lpa", re.IGNORECASE)
_MONTH_RE = re.compile(r"per\s*month|/\s*month|month|p\.?m\b|stipend", re.IGNORECASE)


def parse_salary(raw):
    """Parse the (mostly govt-notice) salary runs.

    Real-world shapes: "Rs 16,500/- pm", "Rs. 25,000 per month",
    "3.5 – 5 LPA", "As per norms". Returns {} for empty input (caller
    keeps "Not Disclosed"); a raw-only dict when no usable amount is
    present (never invents values); else salary_min/max (int, full INR),
    salary_period (club enum) and salary_currency. LPA/lakh multiplies by
    100,000; a trailing "k" by 1,000. With no period stated, amounts
    >= 100,000 read as per-year, below as per-month (Indian norms).
    """
    text = clean_text(raw)
    if not text:
        return {}
    numbers = [float(n.replace(",", "")) for n in _NUM_RE.findall(text)]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return {"salary_raw": text[:120]}

    foreign = _FOREIGN_CUR_RE.search(text)
    if foreign and foreign.group(0) not in ("USD", "$"):
        return {"salary_raw": text[:120]}
    currency = "USD" if foreign else "INR"

    if _LPA_RE.search(text):
        numbers = [n * 100_000 for n in numbers]
    elif _K_SUFFIX_RE.search(text):
        numbers = [n * 1_000 if n < 1_000 else n for n in numbers]

    lo, hi = numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]
    if hi < lo:
        lo, hi = hi, lo
    if _YEAR_RE.search(text):
        period = "per_annum"
    elif _MONTH_RE.search(text):
        period = "per_month"
    else:
        period = "per_annum" if hi >= 100_000 else "per_month"
    return {"salary_raw": text[:120], "salary_min": int(round(lo)),
            "salary_max": int(round(hi)), "salary_period": period,
            "salary_currency": currency}


_FRESHER_RE = re.compile(r"fresher|entry.level|no experience|0\s*year", re.IGNORECASE)
_EXP_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?y", re.IGNORECASE)
_EXP_MIN_RE = re.compile(
    r"(?:min(?:imum)?\.?\s*(?:of\s*)?)?(\d+(?:\.\d+)?)\s*\+?\s*y", re.IGNORECASE)


def parse_experience(raw):
    """"12+ years" -> (12, ""); "0-7 Years" -> (0, 7); "Fresher" -> (0, 0);
    unparseable/empty -> ("", "")."""
    text = clean_text(raw)
    if not text:
        return "", ""
    fresher = bool(_FRESHER_RE.search(text))
    stripped = re.sub(r"\([^)]*\)", " ", text)
    m = _EXP_RANGE_RE.search(stripped)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if fresher:
            lo = 0.0
        return int(lo), int(hi)
    m = _EXP_MIN_RE.search(stripped)
    if m:
        val = int(float(m.group(1)))
        if fresher:
            return 0, val
        return val, ""
    if fresher:
        return 0, 0
    return "", ""


# The site titles posts "<role> at <employer>" or "<employer> Hiring/
# looking for/Requires/invites ... <role>" almost universally.
_TITLE_AT_RE = re.compile(r"\bat\s+(.+?)\s*$", re.IGNORECASE)
_TITLE_VERB_RE = re.compile(
    r"^(.{2,60}?)\s+(?:is\s+)?(?:hiring|looking\s+for|requires?|"
    r"recruit(?:ing|ment|s)?|walk[\s-]?in|invites?|seeks?|interview|"
    r"offering|announces?)\b", re.IGNORECASE)
# generic lead words that mean the verb pattern matched a role phrase,
# not an employer ("Urgent Hiring ...", "Apply Online ...")
_NOT_A_COMPANY_RE = re.compile(
    r"^(?:apply|urgent|job|jobs|career|careers|vacancy|vacancies|wanted|"
    r"opening|openings|post|posts|walk|online|immediate|mega|direct|"
    r"latest|multiple|various|work|now|we|required?|\d)", re.IGNORECASE)


def company_from_title(title):
    # titles carry "| Freshers may apply"-style suffixes after a pipe
    text = clean_text(title).split("|")[0].strip()
    m = _TITLE_AT_RE.search(text)
    if m:
        return clean_text(m.group(1).strip(" .,-"))
    m = _TITLE_VERB_RE.search(text)
    if m:
        prefix = clean_text(m.group(1).strip(" .,-"))
        if prefix and not _NOT_A_COMPANY_RE.match(prefix):
            return prefix
    return ""


INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Delhi", "Jammu and Kashmir", "Ladakh", "Puducherry",
    "Chandigarh",
]
_STATE_LOOKUP = {s.lower(): s for s in INDIAN_STATES}

# country name (lowercased) -> (ISO code, dial code); everything else is
# treated as India, the board's home market (rare abroad posts name the
# country in the Location run, "Hyderabad / India" style).
COUNTRY_META = {
    "india": ("IN", "+91"),
    "united arab emirates": ("AE", "+971"),
    "uae": ("AE", "+971"),
    "saudi arabia": ("SA", "+966"),
    "qatar": ("QA", "+974"),
    "kuwait": ("KW", "+965"),
    "oman": ("OM", "+968"),
    "singapore": ("SG", "+65"),
    "united kingdom": ("GB", "+44"),
    "uk": ("GB", "+44"),
    "united states": ("US", "+1"),
    "usa": ("US", "+1"),
}


def parse_location(raw, tags=""):
    """Location run -> (city, state, country_name, iso, dial).

    "Hyderabad / India" -> Hyderabad / India; "Ahmedabad, Gujarat" ->
    Ahmedabad + Gujarat; govt notices have no Location run — their state
    arrives as a tag ("Chhattisgarh"), scanned as the fallback.
    """
    text = clean_text(re.sub(r"\([^)]*\)", " ", clean_text(raw)))
    city = state = ""
    country, code, dial = "India", "IN", "+91"
    if text and not re.match(r"not specified|not mentioned|various|pan.india|multiple",
                             text, re.I):
        parts = [clean_text(p) for p in re.split(r"[/,|]", text) if clean_text(p)]
        if parts:
            last = parts[-1].lower()
            if last in COUNTRY_META:
                code, dial = COUNTRY_META[last]
                country = "India" if code == "IN" else parts[-1]
                parts = parts[:-1]
            if parts and parts[-1].lower() in _STATE_LOOKUP:
                state = _STATE_LOOKUP[parts[-1].lower()]
                parts = parts[:-1]
            if parts:
                city = parts[0]
    if not state and code == "IN":
        for tag in (t.strip() for t in (tags or "").split(";")):
            if tag.lower() in _STATE_LOOKUP:
                state = _STATE_LOOKUP[tag.lower()]
                break
    return city, state, country, code, dial


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The detail page's tags are the curated `skills` signal; they stay in
    the rich CSV as a raw source column and never decide the category.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("tags", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    # local flags (missing company) survive alongside the classifier's
    row["needs_review"] = bool(verdict["needs_review"]) or bool(row.get("needs_review"))
    return verdict["in_scope"]


_HOSPITAL_RE = re.compile(
    r"hospital|clinic|medical college|nursing home|cmho|health mission|"
    r"health society|health department", re.IGNORECASE)


def classify_company_type(company, title):
    """club enum hospital|pharma — pharma is this site's default."""
    if _HOSPITAL_RE.search(company or "") or _HOSPITAL_RE.search(title or ""):
        return "hospital"
    return "pharma"


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
    for url in (LISTING_URL, JOB_URL_TEMPLATE.format("august-2026/example")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch(session, url, params=None):
    """GET one page with retries/backoff. Returns text or None."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
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


def fetch_listing_page(session, page):
    """One vacancies term page (page 0 = no param; Drupal pager is 0-based)."""
    params = {"page": page} if page else None
    return fetch(session, LISTING_URL, params=params)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(job_id, card_title, page_html):
    """Rich row from a detail page's Article JSON-LD + body + tags."""
    article = parse_article_ldjson(page_html)
    title = clean_text(article.get("headline")) or card_title

    body_html = parse_body_html(page_html)
    fields = extract_labeled_fields(body_html)
    tags = parse_tags(page_html)
    description = strip_html(body_html)[:DESCRIPTION_MAX_CHARS]

    company = clean_text(fields.get("company", "")) or company_from_title(title)
    city, state, country, code, dial = parse_location(fields.get("location", ""), tags)
    min_exp, max_exp = parse_experience(fields.get("experience", ""))

    row = {
        "source": SITE,
        "job_id": job_id,
        "title": title,
        "position": clean_text(fields.get("position", "")),
        "company": company,
        "city": city,
        "state": state,
        "country": country,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "experience_raw": clean_text(fields.get("experience", "")),
        "min_experience": min_exp,
        "max_experience": max_exp,
        "qualification": clean_text(fields.get("qualification", ""))[:300],
        "tags": tags,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": classify_company_type(company, title),
        # local flag only; apply_classification() ORs in the classifier's
        "needs_review": not company,
        "posted_date": parse_posted_date(article),
        "end_date": parse_end_date(fields.get("end_date", "")),
        "description": description,
        "job_url": JOB_URL_TEMPLATE.format(job_id),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    row.update(parse_salary(fields.get("salary", "")))
    return row


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _int_str(value):
    value = _blank(value)
    if value == "":
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def rich_row_to_club_row(r):
    min_sal = _int_str(r.get("salary_min"))
    currency = _blank(r.get("salary_currency"))
    has_salary = bool(min_sal) and currency in ("INR", "USD")
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")) or "India",
        "company_name": _blank(r.get("company")) or _blank(r.get("title")),
        "company_type": _blank(r.get("company_type")) or "pharma",
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
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        # structured source run (the "Qualification :" label) first, else
        # grounded extraction from the description — never inferred
        "qualification": _blank(r.get("qualification"))
            or extract_qualification(_blank(r.get("description"))),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
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
        description="Scrape pharma/govt job postings from pharmatutor.org.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail pages (test runs)")
    parser.add_argument("--max-pages", type=int, default=MAX_LISTING_PAGES,
                        metavar="N", help="listing-page cap (default: %(default)s)")
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

    counters = {"scanned": 0, "duplicates": 0, "skipped_old": 0,
                "skipped_out_of_scope": 0, "excluded_old": 0,
                "excluded_out_of_scope": 0, "needs_review": 0, "new": 0,
                "detail_failed": 0, "listing_pages": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    state = {"fetched": 0}

    def at_limit():
        return args.limit is not None and state["fetched"] >= args.limit

    def process_card(job_id, card_title):
        """Skip-list gates, one detail fetch, date gate, classification."""
        counters["scanned"] += 1
        if job_id in known_ids:
            counters["duplicates"] += 1
            return
        if job_id in seen_old:
            counters["skipped_old"] += 1
            return
        if job_id in out_of_scope_ids:
            counters["skipped_out_of_scope"] += 1
            return
        # the URL's own month segment dates it without a fetch
        if url_month_before_cutoff(job_id, cutoff):
            counters["excluded_old"] += 1
            return

        detail_html = fetch(session, JOB_URL_TEMPLATE.format(job_id))
        state["fetched"] += 1
        if not detail_html:
            counters["detail_failed"] += 1
            return
        try:
            row = build_row(job_id, card_title, detail_html)
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", job_id, exc)
            counters["detail_failed"] += 1
            return

        if row["posted_date"] and row["posted_date"] < cutoff:
            counters["excluded_old"] += 1
            seen_old.add(job_id)
            new_seen_old.append({"job_id": job_id,
                                 "posted_date": row["posted_date"]})
            return

        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            out_of_scope_ids.add(job_id)
            dropped_rows.append(row)
            return

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "company": row["company"], "tags": row["tags"]})
        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1

    # ---- discovery 1: the RSS feed (20 newest, immune to the page cache) ----
    rss_xml = fetch(session, RSS_URL)
    rss_items = parse_rss_items(rss_xml or "")
    log.info("RSS feed lists %d posts", len(rss_items))
    for job_id, card_title in rss_items:
        if at_limit():
            break
        process_card(job_id, card_title)

    # ---- discovery 2: the listing walk (fills the window past the feed) ----
    stale_pages = 0
    for page in range(args.max_pages):
        if at_limit():
            break
        listing_html = fetch_listing_page(session, page)
        if listing_html is None:
            if page == 0 and not rss_items:
                sys.exit("Could not fetch {} — aborting.".format(LISTING_URL))
            break
        cards = parse_listing_cards(listing_html)
        counters["listing_pages"] += 1
        if page == 0 and not cards and not rss_items:
            sys.exit("Listing page yielded zero cards — page shape changed? "
                     "Aborting.")
        if not cards:
            break

        fetched_before = state["fetched"]
        for job_id, card_title in cards:
            if at_limit():
                break
            process_card(job_id, card_title)

        # The page cache shuffles and repeats pages, so page order proves
        # nothing — stop only when every card on the page is provably old
        # by its URL month, or after MAX_STALE_PAGES consecutive pages
        # needing zero detail fetches (skip lists keep re-seen cards free).
        if all(url_month_before_cutoff(job_id, cutoff)
               for job_id, _ in cards):
            break
        stale_pages = stale_pages + 1 if state["fetched"] == fetched_before else 0
        if stale_pages >= MAX_STALE_PAGES:
            break
    else:
        log.warning("Hit the %d-page cap with in-window postings still "
                    "arriving — raise --max-pages if this was not a test run",
                    args.max_pages)

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
    print("Listing pages walked:         {:>6,}".format(counters["listing_pages"]))
    print("Cards scanned:                {:>6,}".format(counters["scanned"]))
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
