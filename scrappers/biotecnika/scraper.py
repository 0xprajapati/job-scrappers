#!/usr/bin/env python3
"""Scrape job listings from www.biotecnika.org (biotech and life-sciences job portal, India).

Data source
-----------
Biotecnika is a WordPress site with a fully open REST API, so the scraper
never parses a listing page and never fetches a detail page — the posts
endpoint returns the FULL post body inline:

    GET https://www.biotecnika.org/wp-json/wp/v2/posts
        ?categories_exclude=<editorial ids>&per_page=100&page=N
        &_fields=id,date,modified,link,title,content,categories

One request covers 100 postings, newest-first, so a daily run costs one or
two requests. Corpus ~31,163 posts; ~419 new posts per 30 days
(measured 2026-08-27).

Separating jobs from news (the site's own taxonomy)
--------------------------------------------------
This site mixes vacancies with editorial (news digests, career advice,
admissions, exam alerts) in the SAME `post` type, so the post type alone is
not a filter. The `category` taxonomy is the separator, but the naive
choice — crawling the `jobs` category — silently drops real
vacancies: measured over 30 days, `jobs` held 353 of 419
posts while only 37 were genuinely editorial. The missing
postings are fellowship/JRF/project-associate vacancies that the site files
under `fellowship`/`biotech-internships-projects` and never tags `jobs`.

So the crawl is the INVERSE — fetch wide, filter tight: every post EXCEPT
the editorial categories

        `biotech-news`, `biotecnika-times`, `biotech-admissions`,
    `scholarships`, `exam-alerts`, `career-advice`, `events`, `videos`

whose ids are resolved from these slugs at startup (never hardcoded). The
two sets are effectively disjoint on the live site — only 19 of the
16,857 `jobs` posts also carry an editorial category — so the
exclusion removes editorial without eating vacancies.

Because the exclusion is wider than `jobs`, a kept post that is NOT
in `jobs` and shows no labelled job field is the residual
news-contamination risk: it is kept AND flagged `needs_review`, never
silently exported. Article-shaped titles (listicles, "how to", result/admit
card alerts) are flagged the same way.

Post structure
--------------
91% of posts carry their facts as labelled `<li><strong>Label:</strong>
value</li>` bullets under a "Job Details" heading, and ~1 in 7 uses a
two-column `<table>` ("Particulars | Details") instead. Both shapes are
parsed by the same label map: Company/Organisation/Institute, Position/Job
Title, Location, Experience, Qualification/Education, Salary/Stipend/
Fellowship Amount, Application Deadline/Last Date to Apply, Job Type.
There is NO schema.org JSON-LD anywhere on either site.

Verified quirks
---------------
* The post title is an SEO headline ("Research Associate Job at IISER |
  Earn Upto ₹71,920/Month | Apply Now"). The rich CSV keeps it verbatim in
  `title_raw`; `title` (and the club export) carry the clean_title() form —
  first useful "|" segment, marketing clauses stripped. When no labelled
  Location is published, city falls back to a conservative parse of the
  body's own location line (city_from_description); a bare "India" stays
  country-only.
* Company is published as a labelled field on only 35% of posts,
  so it falls back to a grounded parse of the post title, which follows
  house patterns ("Research Associate Jobs at Lupin", "Sun Pharma Hiring
  Chemistry Graduates", "Job at ICON plc"). It is never invented — an
  unparseable title leaves company blank and flags needs_review.
* `posted_date` is the WP `date` field and is trustworthy; `modified` is
  ignored (the sites re-touch old posts for SEO, which would re-date them).
* The "Application Deadline"/"Last Date to Apply" field is the apply-by
  date, mapped to `valid_through` — it is NOT a posted date.
* Salary is stated on only ~17% of posts, usually as a monthly
  fellowship stipend ("Rs. 67,000/- p.m.", "₹37,000 per month + HRA") or an
  LPA range. Parsed when present, "Not Disclosed" otherwise — never
  invented.
* `page` beyond the last returns HTTP 400 (WP convention), treated as
  end-of-crawl, not an error.
* Tags (`post_tag`) are unused site-wide (0 of 766 sampled posts had
  any); the ~5.5 categories per post are the whole curated signal.

Classification (shared taxonomy)
--------------------------------
Every kept post goes through the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the post's own WP category slugs, joined ("; ")
* description = the full post body, HTML-stripped

The category slugs are the site's curated role signal — kept in the rich
CSV as the raw `site_categories` column, passed as `skills`, and NEVER
allowed to decide the category. in_scope False -> DROPPED, counted
excluded_out_of_scope, full row appended to out-of-scope.csv. in_scope True
fills category/sub_category/role_family plus the score-trace columns;
needs_review True keeps the row AND appends it to needs_review.csv.

Measured in-scope rate through classify_job (2026-08-27, 60-day
live sample of 766 posts): 167 of 766 kept posts, 21.8%.

Outputs
-------
* biotecnika_jobs.csv                          — rich cumulative store (dedup: post id)
* ../../jobs_csv/<DD-MM-YYYY>/biotecnika.csv   — HealthCareers.club CLUB_COLUMNS export
* out-of-scope.csv                         — dropped rows (reversible)
* needs_review.csv                         — kept but flagged

Unlike devnetjobsindia there is no seen_old_ids.csv: date and body arrive in
the listing itself, so an old post costs nothing to re-see and the watermark
alone bounds the crawl.

Time window: first run keeps the last INITIAL_WINDOW_DAYS; later runs keep
only posts newer than the newest stored posted_date minus
WATERMARK_GRACE_DAYS of overlap.

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
# Configuration  (the only per-site block — everything below is shared logic)
# ----------------------------------------------------------------------------

SITE = "biotecnika"
SITE_BASE = "https://www.biotecnika.org"
ROBOTS_URL = SITE_BASE + "/robots.txt"
POSTS_URL = SITE_BASE + "/wp-json/wp/v2/posts"
CATEGORIES_URL = SITE_BASE + "/wp-json/wp/v2/categories"

# The site's own job/news separator. JOBS_SLUG is NOT used to narrow the
# crawl (it under-collects — see the module docstring); it only marks which
# kept posts the site itself calls jobs, which feeds the needs_review flag.
JOBS_SLUG = "jobs"

# Editorial categories excluded at the source. Resolved slug -> id at
# startup so a renumbered term can never silently widen the crawl.
EDITORIAL_SLUGS = [
    "biotech-news", "biotecnika-times", "biotech-admissions",
    "scholarships", "exam-alerts", "career-advice", "events", "videos",
]

# club enum hospital|pharma — this site's default industry.
DEFAULT_COMPANY_TYPE = "pharma"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 90
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 20_000

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "biotecnika_jobs.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# devnetjobsindia's rich schema, with the two source-specific slots adapted:
# `sectors` -> `site_categories` (this site's curated raw signal) and
# `is_rfp` dropped (no procurement notices in this post type). The salary
# block is appended because — unlike devnetjobsindia — these sites do
# publish stipends, and CLUB_COLUMNS has somewhere to put them.
RICH_COLUMNS = [
    "source", "job_id", "title", "position", "company", "city", "state",
    "country", "country_code", "country_dial_code", "site_categories",
    "in_jobs_category", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "needs_review", "job_type", "experience_raw", "experience_min_years",
    "experience_max_years", "qualification", "salary_raw", "salary_min",
    "salary_max", "salary_period", "salary_currency", "posted_date",
    "valid_through", "description", "job_url", "scraped_at", "title_raw",
]

log = logging.getLogger("biotecnika_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    """Block-level tags become spaces first, so bullets don't run together."""
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table|td|th)[^>]*>", " ",
                    markup or "", flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


# ---- title normalisation ----------------------------------------------------
#
# The WP post title is an SEO headline ("Research Associate Job at IISER |
# Earn Upto ₹71,920/Month | Apply Now"), kept verbatim as `title_raw`.
# `title` (and the club export) carry the clean_title() form: the first
# useful "|" segment with leading/trailing marketing clauses stripped. A
# strip is accepted only while at least _TITLE_MIN_LEN characters survive,
# so the cleaner can never reduce a title to a stub — and it never invents
# words, only removes them.

_TITLE_MIN_LEN = 10

# A whole "|"-separated segment that is nothing but marketing bait
# ("Apply Online Now", "Earn Upto ₹71,920/Month", "Hybrid Opportunity").
_JUNK_SEGMENT_RE = re.compile(
    r"^(?:apply\s+(?:online|now|here|today|before\s+the\s+deadline)"
    r"(?:\s+(?:now|here|today))?"
    r"|applications?\s+(?:are\s+)?open(?:\s+now)?"
    r"|earn\b.*|up\s?to\b.*|rs\.?\s*\d.*|₹\s*\d.*"
    r"|attractive\s+salary(?:\s+package)?"
    r"|(?:hiring|job)\s+alert"
    r"|don.?t\s+miss.*|hurry.*"
    r"|(?:(?:hybrid|remote|wfh|exciting|latest|golden)\s+)?opportunit(?:y|ies)"
    r")[\s!.]*$", re.IGNORECASE)

# Marketing glued to the FRONT of a segment ("Hiring Alert:", "Latest ...",
# "Rs. 67,000/- p.m. Research Associate Jobs at JNCASR", "Earn Upto
# Rs. 1,01,400/Month with Research Scientist Job at ACTREC").
_TITLE_LEAD_STRIPS = [
    re.compile(r"^(?:hiring|job)\s+alert\s*[:!\s–—-]+", re.IGNORECASE),
    re.compile(r"^(?:exciting|latest)\s+", re.IGNORECASE),
    re.compile(r"^(?:earn\s+)?(?:up\s?to\s*:?\s*)?(?:rs\.?|₹|inr)\s*[\d,.]+"
               r"\s*(?:/-)?\s*(?:(?:/|per\s+)(?:month|annum|year)|p\.?\s?m\.?)?"
               r"\s*!?\s*(?:pay|salary)?\s*(?:\b(?:with|at)\b)?\s*[:!–—-]*\s*",
               re.IGNORECASE),
]

# Marketing glued to the END of a segment ("– Apply Online Now",
# "Applications Open Now!", "Don't Miss This Opportunity!!", and
# trailing "Careers 2026"-style years).
_TITLE_TAIL_STRIPS = [
    re.compile(r"[\s!,:;|–—-]*\bapply\s+(?:online\s+|now\s+|here\s+|today\s+)*"
               r"(?:online|now|here|today)[\s!.]*$", re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bapply\s+now\s+for\b.*$", re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bapply\s+before\s+the\s+deadline[\s!.]*$",
               re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bapplications?\s+(?:are\s+)?open(?:\s+now)?[\s!.]*$",
               re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bapply[\s!.]*$", re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bearn\s+up\s?to\b.*$", re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bwith\s+(?:rs\.?|₹|inr)\s*[\d,.].*$", re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\battractive\s+salary(?:\s+package)?[\s!.]*$",
               re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\b(?:hiring|job)\s+alert[\s!.]*$", re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bdon.?t\s+miss\s+(?:this|the)\s+opportunity[\s!.]*$",
               re.IGNORECASE),
    re.compile(r"[\s!,:;|–—-]*\bis\s+hiring[\s!.]*$", re.IGNORECASE),
    re.compile(r"\s+20\d\d\s*[:!\s]*$"),
]


def clean_title(title):
    """SEO headline -> plain job title. Never invents — only removes.

    (1) split on "|" and take the first segment that is not pure
    marketing; (2) strip leading/trailing marketing clauses ("Apply Online
    Now", "Earn Upto ...", "Hiring Alert", trailing years); (3) collapse
    whitespace. Every cut is guarded by _TITLE_MIN_LEN, and a title made
    entirely of marketing falls back to its first segment.
    """
    original = clean_text(title)
    if not original:
        return original

    def _strip_leading(text):
        for pattern in _TITLE_LEAD_STRIPS:
            stripped = pattern.sub("", text).strip()
            if stripped != text and len(stripped) >= _TITLE_MIN_LEN:
                text = stripped
        return text

    def _strip_trailing(text):
        changed = True
        while changed:
            changed = False
            for pattern in _TITLE_TAIL_STRIPS:
                stripped = pattern.sub("", text).strip()
                if stripped != text and len(stripped) >= _TITLE_MIN_LEN:
                    text = stripped
                    changed = True
        return text

    for segment in original.split("|"):
        candidate = _strip_leading(clean_text(segment))
        if not candidate or _JUNK_SEGMENT_RE.match(candidate):
            continue
        candidate = _strip_trailing(candidate).strip(" |–—-:;,!")
        if len(candidate) >= _TITLE_MIN_LEN:
            return candidate
    # every segment was marketing — salvage what the first one has
    candidate = _strip_trailing(_strip_leading(clean_text(original.split("|")[0])))
    candidate = candidate.strip(" |–—-:;,!")
    return candidate if candidate else original


# ---- labelled fields (bullets + two-column tables) --------------------------

_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)

# Label -> canonical field. First value seen for each field wins ("Job
# Details" is the first list in the body).
_LABEL_MAP = [
    (re.compile(r"^(company|company name|organisation|organization|employer|"
                r"institute|institution|hiring organisation|"
                r"hiring organization|hospital)$", re.I), "company"),
    (re.compile(r"^(position|positions|job title|designation|post|post name|"
                r"job post|role|job role|vacancy|name of the post)$", re.I), "position"),
    (re.compile(r"^(location|job location|venue|place of posting|"
                r"work location|city)$", re.I), "location"),
    (re.compile(r"^(experience|experience required|work experience|"
                r"experience level)$", re.I), "experience"),
    (re.compile(r"^(qualification|qualifications|educational qualification|"
                r"essential qualification|education|eligibility|"
                r"eligibility criteria|minimum qualification)$", re.I), "qualification"),
    (re.compile(r"^(job type|employment type|work type|type of employment|"
                r"nature of job)$", re.I), "job_type"),
    (re.compile(r"^(salary|stipend|ctc|pay|pay scale|remuneration|"
                r"emoluments?|fellowship amount|salary range|"
                r"consolidated salary|monthly salary)$", re.I), "salary"),
    (re.compile(r"^(application deadline|last date to apply|last date|"
                r"apply before|apply by|deadline|closing date|"
                r"last date of application)$", re.I), "deadline"),
]


def _labelled_pairs(content_html):
    """Every (label, value) the post publishes, bullets then table rows."""
    pairs = []
    for li in _LI_RE.findall(content_html or ""):
        text = strip_html(li)
        if ":" in text and len(text) < 500:
            label, _, value = text.partition(":")
            pairs.append((clean_text(label), clean_text(value)))
    for tr in _TR_RE.findall(content_html or ""):
        cells = [strip_html(c) for c in _TD_RE.findall(tr)]
        if len(cells) == 2:
            pairs.append((clean_text(cells[0]), clean_text(cells[1])))
    return pairs


def extract_labeled_fields(content_html):
    """Canonical job fields from the post body's bullets and tables."""
    fields = {}
    for label, value in _labelled_pairs(content_html):
        if not value or not label:
            continue
        for pattern, field in _LABEL_MAP:
            if pattern.match(label) and field not in fields:
                fields[field] = value
                break
    return fields


def has_job_fields(content_html):
    """True when the post publishes at least one real vacancy field.

    Used only as a news-contamination signal for needs_review — an
    editorial post that slipped past the category exclusion almost never
    carries a Position/Location/Deadline/Salary label.
    """
    fields = extract_labeled_fields(content_html)
    return any(k in fields for k in
               ("position", "location", "deadline", "salary", "experience"))


# ---- company fallback from the title ----------------------------------------

# House title shapes, verified against live posts:
#   "Sun Pharma Hiring Chemistry Graduates | Analytical Executive Role"
#   "Novo Nordisk Careers 2026: CDM Jobs for Life Sciences Candidates"
#   "Research Associate Jobs at Lupin | Jobs in Pune"
#   "CDM Jobs in Bengaluru at ICON | Life Sciences Eligible"
#   "Clinical Trial Jobs at ICON in Bengaluru | Life Sciences Eligible"
# Grounded extraction only — no match leaves company blank (which flags
# needs_review rather than inventing an employer).
#
# The leading "<Company> Hiring/Careers" shape is tried FIRST: in
# "Lupin Hiring Research Associates at Pune" the trailing `at` is a city,
# and only the leading shape gets it right.
_TITLE_HIRING_RE = re.compile(
    r"^(.+?)\s+(?:is\s+)?(?:hiring|recruits?|recruiting|careers?\b)", re.IGNORECASE)
# Greedy `^.*` forces the RIGHTMOST `at`, so "Jobs in Bengaluru at ICON"
# yields ICON and not the city.
_TITLE_AT_RE = re.compile(r"^.*\bat\s+(.+)$", re.IGNORECASE)

# Trailing noise the titles append after the company name. `in` is here so
# "ICON in Bengaluru" trims back to "ICON"; `with` so "IISER with Rs.
# 58,000/- Per Month Pay" trims back to "IISER".
_COMPANY_TAIL_RE = re.compile(
    r"\s*(?:\||,|:|–|—|-)\s*.*$|\s+\b(?:apply|for|with|earn|in|jobs?|"
    r"vacanc\w*|now|online|here|hiring|recruitment|salary|upto|up to|"
    r"20\d\d)\b.*$", re.IGNORECASE)
_NOT_A_COMPANY_RE = re.compile(r"^[\d\W]|^(?:the|a|an|age|rs)\b", re.IGNORECASE)


def company_from_title(title):
    """Grounded company parse from the post title, or "" when unparseable."""
    text = clean_text(title)
    for regex in (_TITLE_HIRING_RE, _TITLE_AT_RE):
        match = regex.search(text)
        if not match:
            continue
        name = _COMPANY_TAIL_RE.sub("", clean_text(match.group(1))).strip(" .,|-–—")
        # a plausible organisation name, not a stray fragment or an amount
        if 2 <= len(name) <= 60 and not _NOT_A_COMPANY_RE.match(name):
            return name
    return ""


# ---- location ---------------------------------------------------------------

COUNTRY_META = {
    "india": ("India", "IN", "+91"),
    "united arab emirates": ("United Arab Emirates", "AE", "+971"),
    "uae": ("United Arab Emirates", "AE", "+971"),
    "singapore": ("Singapore", "SG", "+65"),
    "united kingdom": ("United Kingdom", "GB", "+44"),
    "uk": ("United Kingdom", "GB", "+44"),
    "united states": ("United States", "US", "+1"),
    "usa": ("United States", "US", "+1"),
    "us": ("United States", "US", "+1"),
    "germany": ("Germany", "DE", "+49"),
    "ireland": ("Ireland", "IE", "+353"),
    "switzerland": ("Switzerland", "CH", "+41"),
    "australia": ("Australia", "AU", "+61"),
    "canada": ("Canada", "CA", "+1"),
    "netherlands": ("Netherlands", "NL", "+31"),
}

# Indian states/UTs — so "Bengaluru, Karnataka, India, 560026" splits into
# city + state rather than treating the state as a country.
_INDIAN_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "karnātaka", "kerala", "madhya pradesh", "maharashtra", "manipur",
    "meghalaya", "mizoram", "nagaland", "odisha", "punjab", "rajasthan",
    "sikkim", "tamil nadu", "telangana", "tripura", "uttar pradesh",
    "uttarakhand", "west bengal", "delhi", "new delhi", "jammu and kashmir",
    "ladakh", "puducherry", "chandigarh", "andaman and nicobar islands",
    "dadra and nagar haveli", "daman and diu", "lakshadweep",
}

_PAREN_RE = re.compile(r"\([^)]*\)")
_PIN_RE = re.compile(r"^\d{4,8}$")


def parse_location(raw):
    """Location field -> (city, state, country_name, iso, dial).

    "Bengaluru, Karnataka, India, 560066" -> Bengaluru / Karnataka / India;
    "Hyderabad" -> Hyderabad / "" / India; "Basel, Switzerland" ->
    Basel / "" / Switzerland; "Pan India"/"Multiple" -> all blank + India.
    Unrecognised trailing segments stay India, the sites' home market.
    """
    text = clean_text(_PAREN_RE.sub(" ", clean_text(raw)))
    if not text or re.match(r"not specified|not mentioned|various|multiple|"
                            r"pan.?india|across india|any location|remote", text, re.I):
        return "", "", "India", "IN", "+91"
    text = re.split(r"\s*[/|]\s*", text)[0].strip(" ,")
    parts = [clean_text(p) for p in text.split(",") if clean_text(p)]
    parts = [p for p in parts if not _PIN_RE.match(p)]     # drop postal codes
    if not parts:
        return "", "", "India", "IN", "+91"

    country_name, code, dial = "India", "IN", "+91"
    if parts[-1].lower() in COUNTRY_META:
        country_name, code, dial = COUNTRY_META[parts[-1].lower()]
        parts = parts[:-1] or [""]

    state = ""
    if len(parts) > 1 and parts[-1].lower() in _INDIAN_STATES:
        state = parts[-1]
        parts = parts[:-1]
    city = parts[0] if parts else ""
    if city.lower() in _INDIAN_STATES and not state:
        city, state = "", city
    return city, state, country_name, code, dial


# ---- city fallback from the post body ---------------------------------------
#
# ~1 in 5 posts publishes no labelled Location bullet, but the body usually
# still carries a location line in plain paragraphs ("Location: Bangalore,
# Bengaluru Reference Number: ...", "Primary Location: India, Hyderabad",
# "Location – Bangalore About the Company ..."). Extraction is deliberately
# conservative — a wrong city is worse than an empty one — so only a
# labelled Location/Locations value is read, and only two unambiguous
# shapes are accepted: a whitelisted Indian city anywhere in the value, or
# a "<City>, <State>" pair anchored on a real Indian state. A bare "India"
# stays country-only (city empty).

_KNOWN_CITIES = [
    "Bengaluru", "Bangalore", "Hyderabad", "Mumbai", "Navi Mumbai", "Thane",
    "Pune", "Chennai", "New Delhi", "Delhi", "Gurgaon", "Gurugram", "Noida",
    "Greater Noida", "Kolkata", "Ahmedabad", "Vadodara", "Surat",
    "Chandigarh", "Mohali", "Thiruvananthapuram", "Trivandrum", "Kochi",
    "Cochin", "Coimbatore", "Mysuru", "Mysore", "Mangaluru", "Mangalore",
    "Visakhapatnam", "Bhubaneswar", "Bhubaneshwar", "Bhopal", "Indore",
    "Nagpur", "Nashik", "Aurangabad", "Lucknow", "Kanpur", "Varanasi",
    "Patna", "Ranchi", "Raipur", "Jaipur", "Dehradun", "Guwahati",
    "Faridabad", "Ghaziabad", "Baddi", "Goa", "Hosur", "Vellore", "Ludhiana",
]
# longest-first so "New Delhi" beats "Delhi", "Navi Mumbai" beats "Mumbai"
_CITY_IN_TEXT_RE = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, _KNOWN_CITIES),
                               key=len, reverse=True)) + r")\b")
_DESC_LOCATION_RE = re.compile(
    r"\b(?:(?:job|work|primary|office|base)\s+)?locations?\s*[:\-–—]\s*",
    re.IGNORECASE)
_ABOUT_RE = re.compile(r"\bAbout\s+(?:the\s+)?\w")
_NOT_A_CITY_WORDS = {"india", "remote", "hybrid", "floor", "unit", "block",
                     "park", "road", "street", "phase", "sector", "tower",
                     "building", "area", "gate", "plot", "campus"}
_STATE_ALT = "|".join(sorted((re.escape(s.title()) for s in _INDIAN_STATES),
                             key=len, reverse=True))
_CITY_STATE_PAIR_RE = re.compile(
    r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+)?),\s+((?i:" + _STATE_ALT + r"))\b")


def city_from_description(description):
    """Conservative (city, state) from a labelled location line in the body.

    Returns ("", "") for a bare "India", "Remote", an unlabelled mention,
    or anything ambiguous — never guesses.
    """
    text = clean_text(description)
    for match in _DESC_LOCATION_RE.finditer(text):
        window = text[match.end():match.end() + 90]
        # the flattened body runs the next "Label: value" straight after
        # the location value — a colon can only belong to that next label,
        # and "About the Company" boilerplate ends the value the same way
        window = window.split(":", 1)[0]
        cut = _ABOUT_RE.search(window)
        if cut:
            window = window[:cut.start()]
        found = _CITY_IN_TEXT_RE.search(window)
        if found:
            city = found.group(0)
            # mirror parse_location: Delhi/Goa-style names file as states
            return ("", city) if city.lower() in _INDIAN_STATES else (city, "")
        pair = _CITY_STATE_PAIR_RE.search(window)
        if pair:
            city, state = pair.group(1), pair.group(2)
            if not set(re.split(r"[\s-]+", city.lower())) & _NOT_A_CITY_WORDS:
                return city, state
    return "", ""


# ---- experience -------------------------------------------------------------

_FRESHER_RE = re.compile(r"fresher|entry.level|no experience|0\s*year", re.IGNORECASE)
_EXP_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?y", re.IGNORECASE)
_EXP_MIN_RE = re.compile(
    r"(?:min(?:imum)?\.?\s*(?:of\s*)?)?(\d+(?:\.\d+)?)\s*\+?\s*y", re.IGNORECASE)


def parse_experience(raw):
    """"2-5 Years" -> (2, 5); "Freshers" -> (0, 0); "Min 3 years" -> (3, "");
    unparseable/empty -> ("", "")."""
    text = clean_text(raw)
    if not text:
        return "", ""
    fresher = bool(_FRESHER_RE.search(text))
    stripped = re.sub(r"\([^)]*\)", " ", text)   # "(2026 pass-outs)" isn't years
    match = _EXP_RANGE_RE.search(stripped)
    if match:
        lo, hi = float(match.group(1)), float(match.group(2))
        if hi < lo:
            lo, hi = hi, lo
        return (0 if fresher else int(lo)), int(hi)
    match = _EXP_MIN_RE.search(stripped)
    if match:
        val = int(float(match.group(1)))
        if val > 40:                              # an age limit, not experience
            return (0, 0) if fresher else ("", "")
        return (0, val) if fresher else (val, "")
    return (0, 0) if fresher else ("", "")


# ---- salary -----------------------------------------------------------------

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FOREIGN_CUR_RE = re.compile(r"\b(AED|SAR|QAR|KWD|BHD|OMR|USD|GBP|EUR|CHF)\b|[$£€]")
_LPA_RE = re.compile(r"lpa|lakh|lac", re.IGNORECASE)
_K_SUFFIX_RE = re.compile(r"\d\s*k\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"per\s*year|/\s*year|annum|p\.?\s?a\b|yearly|annual|lpa",
                      re.IGNORECASE)
_MONTH_RE = re.compile(r"per\s*month|/\s*month|month|p\.?\s?m\b|monthly|stipend",
                       re.IGNORECASE)


def parse_salary(raw):
    """Parse the Salary/Stipend/Fellowship-Amount field when present.

    Live shapes: "Rs. 67,000/- p.m.", "₹37,000 per month + HRA",
    "₹3.5 - 5 LPA", "As per institute norms", "Best in industry".
    Returns {} for empty input (caller keeps "Not Disclosed"), a raw-only
    dict when no usable amount is present (never invents), else min/max in
    full units plus period and currency. LPA/lakh multiplies by 100,000, a
    trailing "k" by 1,000; with no period stated, >= 100,000 reads per-year
    and below per-month (Indian norms).
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
        return {"salary_raw": text[:120]}          # unconverted — keep raw only
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


# ---- dates ------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DMY_RE = re.compile(r"\b(\d{1,2})[\s./-]+([A-Za-z]{3,9}|\d{1,2})[\s./-]+(\d{4})\b")
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def parse_deadline(raw):
    """Apply-by field -> ISO date, or "" when absent/unparseable."""
    text = clean_text(raw)
    if not text:
        return ""
    match = _ISO_RE.search(text)
    if match:
        return match.group(0)
    match = _DMY_RE.search(text)
    if not match:
        return ""
    day, month_raw, year = match.groups()
    month = (_MONTHS.get(month_raw[:3].lower()) if month_raw[:1].isalpha()
             else int(month_raw))
    if not month or not 1 <= month <= 12 or not 1 <= int(day) <= 31:
        return ""
    return "{}-{:02d}-{:02d}".format(year, month, int(day))


# ---- news contamination (junk detection only, never a category) -------------

_NEWSY_TITLE_RE = re.compile(
    r"^\s*(?:top|best)\s+\d+|\bhow to\b|\bwhat is\b|salary (?:trends|in india)|"
    r"career guide|\btips\b|\bexam\b.*\bresult|admit card|answer key|"
    r"syllabus|cut.?off|\btimes\b\s*\d{2}\.\d{2}\.\d{4}|question paper|"
    r"list of\b|difference between", re.IGNORECASE)


def is_newsy_title(title):
    return bool(_NEWSY_TITLE_RE.search(title or ""))


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The post's own WP category slugs are the curated `skills` signal; they
    stay in the rich CSV as the raw `site_categories` column and never
    decide the category. Returns in_scope — False means DROP the row
    (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("site_categories", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    # local news/company flags survive alongside the classifier's
    row["needs_review"] = bool(verdict["needs_review"]) or bool(row.get("needs_review"))
    return verdict["in_scope"]


# club enum hospital|pharma — NOT a taxonomy category, and never derived
# from the job title: almost every in-scope title here contains the word
# "Clinical", which would type every CRO (ICON, IQVIA, Medpace) as a
# hospital. Word-bounded `\bclinics?\b` so "Clinical" cannot match either.
_HOSPITAL_RE = re.compile(
    r"\bhospitals?\b|\bclinics?\b|medical college|nursing home|"
    r"health ?system|medical cent(?:re|er)|\bAIIMS\b|\bJIPMER\b", re.IGNORECASE)
_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?", re.IGNORECASE)


def classify_company_type(company, title):
    """club enum hospital|pharma — NOT a taxonomy category."""
    # the employer name decides; the title is consulted only when the post
    # published no company at all
    blob = clean_text(company) or clean_text(title)
    if _HOSPITAL_RE.search(blob):
        return "hospital"
    if _PHARMA_RE.search(blob):
        return "pharma"
    return DEFAULT_COMPANY_TYPE


_WORK_TYPE_MAP = [
    (re.compile(r"remote|work from home|\bwfh\b", re.I), "remote"),
    (re.compile(r"hybrid", re.I), "hybrid"),
    (re.compile(r"part.?time", re.I), "part_time"),
    (re.compile(r"intern(ship)?|trainee|apprentice", re.I), "internship"),
    (re.compile(r"contract|temporary|fixed.term|tenure", re.I), "contract"),
]


def classify_job_type(work_type_raw, title=""):
    blob = "{} {}".format(clean_text(work_type_raw), clean_text(title))
    for pattern, value in _WORK_TYPE_MAP:
        if pattern.search(blob):
            return value
    return "full_time"


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
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (POSTS_URL, CATEGORIES_URL):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def _request(session, url, params=None):
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
            if resp.status_code == 400:
                return []            # WP: page number past the last page
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_category_map(session):
    """{id: slug} for every category (the sites have 113-155, 2 pages)."""
    mapping = {}
    for page in (1, 2, 3):
        data = _request(session, CATEGORIES_URL,
                        params={"per_page": 100, "page": page,
                                "_fields": "id,slug"})
        if not data:
            break
        mapping.update({c["id"]: c["slug"] for c in data})
        if len(data) < 100:
            break
    if not mapping:
        sys.exit("Could not fetch the category list — aborting.")
    return mapping


def resolve_editorial_ids(category_map):
    """Editorial slugs -> ids. A slug that no longer exists is fatal: it
    would silently widen the crawl back into the news feed."""
    slug_to_id = {slug: cid for cid, slug in category_map.items()}
    missing = [s for s in EDITORIAL_SLUGS if s not in slug_to_id]
    if missing:
        sys.exit("Editorial categories {} are gone from {} — the job/news "
                 "separator changed; re-probe before running.".format(
                     missing, SITE_BASE))
    return [slug_to_id[s] for s in EDITORIAL_SLUGS]


def fetch_posts_page(session, page, editorial_ids):
    """One page of everything-but-editorial, newest-first (WP default)."""
    return _request(session, POSTS_URL, params={
        "categories_exclude": ",".join(str(i) for i in editorial_ids),
        "per_page": PAGE_SIZE,
        "page": page,
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date,link,title,content,categories",
    })


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def post_to_rich_row(post, category_map):
    """Rich row from one WP post object (body included in the listing)."""
    # The verbatim SEO headline stays in `title_raw`; every parse that
    # relies on the headline's own patterns (company, WFH/hybrid markers,
    # newsy-title detection) reads the RAW form, never the cleaned one.
    title_raw = clean_text(post["title"]["rendered"])
    title = clean_title(title_raw)
    content = post["content"]["rendered"]
    fields = extract_labeled_fields(content)
    slugs = [category_map.get(c, str(c)) for c in post.get("categories", [])]
    in_jobs_category = JOBS_SLUG in slugs

    company = clean_text(fields.get("company", "")) or company_from_title(title_raw)
    description = strip_html(content)[:DESCRIPTION_MAX_CHARS]
    city, state, country_name, code, dial = parse_location(fields.get("location", ""))
    if not city and not state and country_name == "India":
        # no labelled Location bullet — fall back to the body's location line
        city, state = city_from_description(description)
    min_exp, max_exp = parse_experience(fields.get("experience", ""))

    # Local (non-taxonomy) flags. The crawl is wider than the site's own
    # `jobs` category, so a post outside it that publishes no vacancy field
    # is the residual news-contamination risk — kept, never silent.
    needs_review = (is_newsy_title(title_raw)
                    or not company
                    or (not in_jobs_category and not has_job_fields(content)))

    row = {
        "source": SITE,
        "job_id": str(post["id"]),
        "title": title,
        "position": clean_text(fields.get("position", "")),
        "company": company,
        "city": city,
        "state": state,
        "country": country_name,
        "country_code": code,
        "country_dial_code": dial,
        "site_categories": "; ".join(slugs),
        "in_jobs_category": in_jobs_category,
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": needs_review,
        # the raw headline keeps WFH/Hybrid markers a "|" cut could drop
        "job_type": classify_job_type(fields.get("job_type", ""), title_raw),
        "experience_raw": clean_text(fields.get("experience", "")),
        "experience_min_years": min_exp,
        "experience_max_years": max_exp,
        "qualification": clean_text(fields.get("qualification", ""))[:300],
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "posted_date": clean_text(post.get("date", ""))[:10],
        "valid_through": parse_deadline(fields.get("deadline", "")),
        "description": description,
        "job_url": clean_text(post.get("link", "")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title_raw": title_raw,
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


_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def trim_company_name(name):
    """Club company_name over 60 chars sheds TRAILING parenthetical
    qualifiers ("... Pvt. Ltd. (Outsourced Contractor for Manpower
    Services)" -> "... Pvt. Ltd."). The rich CSV `company` keeps the full
    string; a name that is long without a trailing parenthetical is left
    alone rather than truncated mid-word."""
    name = clean_text(name)
    while len(name) > 60:
        trimmed = _TRAILING_PAREN_RE.sub("", name).rstrip(" ,-–—")
        if not trimmed or trimmed == name:
            break
        name = trimmed
    return name


def rich_row_to_club_row(r):
    min_sal = _int_str(r.get("salary_min"))
    currency = _blank(r.get("salary_currency"))
    has_salary = bool(min_sal) and currency in ("INR", "USD")
    return {
        "country_name": _blank(r.get("country")) or "India",
        "country_code": _blank(r.get("country_code")) or "IN",
        "country_dial_code": _blank(r.get("country_dial_code")) or "+91",
        "city_name": _blank(r.get("city")) or _blank(r.get("state")),
        "company_name": trim_company_name(_blank(r.get("company"))),
        "company_type": classify_company_type(_blank(r.get("company")),
                                              _blank(r.get("title"))),
        "company_logo": "",
        "company_about": "",
        # idempotent re-clean, so store rows from before the title fix can
        # never leak an SEO headline into the club export
        "title": clean_title(_blank(r.get("title"))),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        # the post's own Qualification field first, else a grounded
        # extraction from the body — never inferred
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
        description="Scrape jobs from www.biotecnika.org (WordPress REST API).")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N pages of %d posts (test runs)" % PAGE_SIZE)
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every post "
                             "published on/after this date")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    category_map = fetch_category_map(session)
    editorial_ids = resolve_editorial_ids(category_map)
    log.info("Excluding %d editorial categories at the source: %s",
             len(editorial_ids), ", ".join(EDITORIAL_SLUGS))

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    out_of_scope_ids = load_id_column(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs (%d known out-of-scope); "
             "keeping posts published on/after %s%s",
             len(known_ids), len(out_of_scope_ids), cutoff,
             " (--since override)" if args.since else "")

    counters = {"scanned": 0, "duplicates": 0, "skipped_out_of_scope": 0,
                "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "parse_failed": 0}
    new_rows, review_log, dropped_rows = [], [], []
    page, empty_pages = 1, 0

    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        posts = fetch_posts_page(session, page, editorial_ids)
        page += 1
        if posts is None or posts == []:
            empty_pages += 1
            if posts == [] or empty_pages >= MAX_EMPTY_PAGES:
                break
            continue
        empty_pages = 0

        page_all_old = True
        for post in posts:
            counters["scanned"] += 1
            try:
                row = post_to_rich_row(post, category_map)
            except Exception as exc:   # never let one post crash the run
                counters["parse_failed"] += 1
                log.warning("Skipping malformed post %s: %s", post.get("id"), exc)
                continue

            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False       # date check first, so an all-dropped
                                       # page never looks like an all-old one
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if row["job_id"] in out_of_scope_ids:
                counters["skipped_out_of_scope"] += 1
                continue
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                out_of_scope_ids.add(row["job_id"])
                dropped_rows.append(row)
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "company": row["company"],
                                   "site_categories": row["site_categories"],
                                   "in_jobs_category": row["in_jobs_category"],
                                   "job_url": row["job_url"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        # newest-first: once a whole page is older than the cutoff, stop.
        if page_all_old or len(posts) < PAGE_SIZE:
            break

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
        log.info("Wrote %s (%d rows to review)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Posts scanned:                {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Parse failures:               {:>6,}".format(counters["parse_failed"]))


if __name__ == "__main__":
    main()
