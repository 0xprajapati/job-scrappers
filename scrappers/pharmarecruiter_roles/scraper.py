#!/usr/bin/env python3
"""Role-targeted scrape of pharmarecruiter.in (pharma job portal, India).

The sibling `../pharmarecruiter` walks the whole `jobs` category. This one
asks the site only for the eleven in-scope role families, so a daily run
costs a couple of dozen requests instead of a full crawl.

Data source
-----------
pharmarecruiter.in is a WordPress site (Hostinger + Cloudflare) whose REST
API is fully open, so the scraper never parses listing pages:

    GET https://pharmarecruiter.in/wp-json/wp/v2/posts
        ?categories=<jobs-id>&search=<term>&per_page=50&page=N
        &_fields=id,date,link,title,content,categories

WordPress's `search` parameter is full text over the title AND the body, so
a listing that names the domain only in its requirements still comes back.
SEARCH_TERMS holds queries for all eleven role families and all ten Public
Health sub-categories ("pharmacovigilance", "drug safety", "regulatory
affairs", "medical writing", ...) and the crawl works one term at a time. The `jobs` category id is resolved from its slug at
startup via /wp-json/wp/v2/categories, so `pharma-news` articles are
skipped at source.

FIXED 2026-08-25: the main loop's stop condition
(`page_all_old or len(posts) < PAGE_SIZE`) used to end the WHOLE crawl
rather than advancing to the next search term, so in practice only the
first term was ever walked. It now advances the term cursor, and the term
list was widened from 17 to 56 queries in the same change (see
SEARCH_TERMS). Per-run cost is bounded by the date watermark, not by the
term count: each term stops at its first all-older-than-cutoff page, so a
steady-state run is roughly one request per term.

Every post embeds a labelled bullet list under a "Job Details" heading:

    Company Name / Organization: ...
    Experience: 0-7 Years | Fresher Only | Freshers (2026 pass-outs) to 8 Years
    Qualification: M.Pharm / M.Sc ...
    Location: Ahmedabad, India
    Work Type: Full-time, On-site | Government / On-site
    Salary: (rarely; usually undisclosed)

Quirks
------
* One post often advertises a multi-role walk-in drive; the post title is
  kept as the job title (matching how the site presents it).
* Salary is almost never stated. When a Salary/Stipend/CTC bullet exists it
  is parsed (LPA / K / per month conventions); otherwise "Not Disclosed".
* SEARCH_TERMS is a CRAWL-SIDE filter that only saves requests. Full-text
  search matches the body, so plenty of production/QC posts come back that
  merely mention "clinical research"; the keep/drop and labelling decision
  is the shared `classify_job`'s alone (scrappers/_shared/classification.py),
  fed title + site category slugs (as skills) + body text. Out-of-scope
  posts are dropped and counted as excluded_out_of_scope.
* One post can be returned by several search terms; job_id (the WP post id)
  dedupes them.
* `pharma-jobs-abroad` posts get their country parsed from the Location
  bullet; anything unrecognised stays India, the site's home market.

Outputs
-------
* pharmarecruiter_roles_jobs.csv                         — rich cumulative
                                                           store (dedup: post id)
* ../../jobs_csv/<DD-MM-YYYY>/pharmarecruiter_roles.csv  — the shared 23-column
                                                           club schema
* needs_review.csv                                       — rows kept AND flagged

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS
(narrowed to 2 here); later runs keep only jobs newer than the newest stored
date minus WATERMARK_GRACE_DAYS of overlap.

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS  # noqa: E402
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "pharmarecruiter_roles"
SITE_BASE = "https://pharmarecruiter.in"
ROBOTS_URL = SITE_BASE + "/robots.txt"
POSTS_URL = SITE_BASE + "/wp-json/wp/v2/posts"

# WordPress REST exposes full-text ?search=, so we ask the site for these
# roles instead of walking the whole jobs category (master spec §1).
# Widened 2026-08-25 from 17 terms to 56, to the same standard as
# shine_roles: every one of the eleven role families and all ten Public
# Health sub-categories now has at least one query. The old list had no
# Medical Reviewer term and only 2 of the 10 PH sub-categories.
#
# WordPress core search is a substring LIKE, ANDed across the words of the
# query, over title + body — so "clinical data" is a superset of "clinical
# data management", and "medical review" already covers "medical reviewer".
# Counts below were probed live on 2026-08-25 against
# ?categories=1&per_page=1 (X-WP-Total), out of 7,335 job posts.
#
# Short queries were each spot-checked against their top results, because
# LIKE matches inside unrelated words. Kept: cdisc (23), sdtm (19),
# icd-10 (47) — all return real programming/coding roles. Rejected:
# "heor" (38, matches "theory"/"theoretical"), "hmis" (6, no real hits),
# bare "hiv" (192 — it is a substring of "archive"; "hiv/aids" returns 9
# honest hits), and the bare acronyms cra, msl, tmf, cpc, edc, argus.
# Also absent: "drug regulatory" (1,193 — the AND of two common words,
# already covered by the two regulatory terms below) and
# "nutritionist"/"asha worker" (0 hits; this is a pharma portal).
# "dietitian"/"dietician" return 1 post each and are left out for the same
# reason — the Public Health nutrition decision of 2026-08-25 still applies
# to anything the other terms surface.
SEARCH_TERMS = [
    # Clinical Research
    "clinical research", "clinical trials", "clinical operations",
    "clinical research associate",
    # Clinical Data Management
    "clinical data management", "clinical data", "cdisc", "sdtm",
    # Pharmacovigilance
    "pharmacovigilance", "drug safety", "signal detection", "icsr",
    "aggregate reports",
    # Regulatory Affairs
    "regulatory affairs", "regulatory submissions", "regulatory intelligence",
    "dossier",
    # Medical Writer
    "medical writing", "medical writer", "scientific writing",
    "clinical study report",
    # Medical Coding
    "medical coding", "medical coder", "clinical coding", "icd-10",
    # MSL
    "medical science liaison", "medical affairs", "medical advisor",
    "medical information",
    # Medical Reviewer  (was entirely unrepresented before 2026-08-25)
    "medical review", "medical monitor", "safety physician",
    # HEOR
    "health economics", "market access", "real world evidence",
    # TMF
    "trial master file",
    # Public Health — one query per sub-category (was: "public health" and
    # "epidemiology" only). Thin counts are kept anyway: the date watermark
    # bounds each term to ~1 page per run, so an extra term costs one
    # request, not a crawl.
    "public health", "epidemiology", "epidemiologist",
    "disease surveillance", "monitoring and evaluation",
    "community health", "health promotion", "health educator",
    "tuberculosis", "hiv/aids", "malaria", "immunization", "vaccination",
    "nutrition", "infection control",
    "health informatics",
    "implementation research", "maternal health",
]
CATEGORIES_URL = SITE_BASE + "/wp-json/wp/v2/categories"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
# First-run window, narrowed from the master spec §4 default of 7 to 2:
# these feeds post fast, so every extra day of first-run window costs a lot of
# crawl for jobs that are already stale by import time. Later runs ignore this
# entirely and use the watermark.
INITIAL_WINDOW_DAYS = 2
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 50
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "pharmarecruiter_roles_jobs.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

JOBS_CATEGORY_SLUG = "jobs"          # scraped at the source
NEWS_CATEGORY_SLUG = "pharma-news"   # skipped unless also tagged jobs

# country name (lowercased) -> (ISO code, dial code); everything else is
# treated as India — pharma-jobs-abroad posts name the country in Location.
COUNTRY_META = {
    "india": ("IN", "+91"),
    "united arab emirates": ("AE", "+971"),
    "uae": ("AE", "+971"),
    "dubai": ("AE", "+971"),
    "abu dhabi": ("AE", "+971"),
    "saudi arabia": ("SA", "+966"),
    "qatar": ("QA", "+974"),
    "kuwait": ("KW", "+965"),
    "bahrain": ("BH", "+973"),
    "oman": ("OM", "+968"),
    "singapore": ("SG", "+65"),
    "malaysia": ("MY", "+60"),
    "united kingdom": ("GB", "+44"),
    "uk": ("GB", "+44"),
    "united states": ("US", "+1"),
    "usa": ("US", "+1"),
    "germany": ("DE", "+49"),
    "ireland": ("IE", "+353"),
    "australia": ("AU", "+61"),
    "canada": ("CA", "+1"),
    "new zealand": ("NZ", "+64"),
}

RICH_COLUMNS = [
    "source", "job_id", "title", "position", "company", "city", "country",
    "country_code", "country_dial_code", "salary_raw", "salary_min",
    "salary_max", "salary_period", "salary_currency", "job_type",
    "experience_raw", "min_experience", "max_experience", "qualification",
    "work_type_raw", "site_categories", "category", "sub_category",
    "role_family", "all_families", "family_scores", "family_confidence",
    "matched_in", "company_type", "needs_review", "posted_date",
    "description", "job_url", "scraped_at",
    # added 2026-08-27 (rightmost so old rows load cleanly; the store merge
    # in main() backfills "" for rows written before the columns existed):
    # title_raw    — the untouched SEO headline; `title` is clean_title()'d
    # location_raw — the untouched Location bullet; `city` is normalised
    "title_raw", "location_raw",
]

log = logging.getLogger("pharmarecruiter_roles_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


_FIRST_HEADING_RE = re.compile(r"<h[23][^>]*>", re.I)


def strip_seo_intro(content_html):
    """Drop the site's generated SEO paragraph(s) that open every post.

    Posts start with boilerplate ("Apply for X role in Y at Z. Explore
    pharma jobs…") before the first heading (always "About the Company"
    at the source); the real content runs from that heading. Posts with
    no heading are kept whole.
    """
    match = _FIRST_HEADING_RE.search(content_html or "")
    return content_html[match.start():] if match else content_html


# ---------------------------------------------------------------------------
# FIX 2026-08-27 (PHARMARECRUITER-02): the site's post headlines are SEO
# strings, not job titles — "Medical Writer Jobs in Delhi | Insignia Clinical
# Research Careers", "Piramal Pharma Walk-In Drive 2026 – Senior Research
# Associate ... Jobs in Ahmedabad". clean_title() de-SEOs them before storing;
# the untouched headline is preserved in the rich CSV's title_raw column.
# ---------------------------------------------------------------------------

MIN_CLEAN_TITLE_LEN = 10          # below this, fall back to the "|" cut only
_TITLE_EDGE_CHARS = " \t-–—:,.|"  # separators stripped off either end

# Leading marketing prefixes (the company is its own column, so losing the
# name from the title is fine). All are anchored and delimiter-bounded so a
# real title never matches by accident; the (?i:...) scoped flags keep the
# capital-letter lookaheads case-sensitive.
_TITLE_PREFIX_RES = [
    # "<Company> Walk-In Drive 2026 –", "<Company> Walk-In Drive:"
    re.compile(r"^[^:|]{2,60}?\b(?i:walk[-\s]?in\s+(?:drive|interview)s?)\s*"
               r"(?:(?:19|20)\d{2})?\s*[–—:-]+\s*(?=\S)"),
    # "<Company> [Pharma] Jobs 2026:", "<Company> Recruitment 2026:"
    re.compile(r"^[^:|]{2,60}?\b(?i:jobs|recruitment|hiring)\s+"
               r"(?:19|20)\d{2}\s*:\s*(?=\S)"),
    # "<Company> Hiring [2026][: – -]", "<Company> Hiring for"
    re.compile(r"^[^:|–—]{2,60}?\s(?i:hiring)\b\s*(?:(?:19|20)\d{2})?\s*"
               r"(?:[:–—-]+\s*|(?i:for)\s+)?(?=[A-Z0-9])"),
    # a bare "Hiring " left at the front once the company prefix is gone
    # ("<Company> Jobs 2026: Hiring Copywriting Associate ...")
    re.compile(r"^(?i:hiring)\s+(?:[:–—-]+\s*)?(?=[A-Z0-9])"),
]

# Trailing SEO tails, applied repeatedly until the title stops shrinking.
_TITLE_TAIL_RES = [
    # "... Central Monitor Hiring in Bangalore (Hybrid)" — the no-colon
    # guard keeps "Hiring in Dhaka: Regulatory Affairs ..." intact (the
    # role lives after the colon there)
    re.compile(r"\s(?i:hiring)\s+(?i:in)\s+[A-Z(][^:]*$"),
    # "Jobs in <locations>" and its variants: "Job Opening in Gurgaon",
    # "Remote Jobs India", "Jobs Mumbai Noida", "Jobs (Remote India)" — the
    # whole tail after Jobs/Careers must look like locations (capitalised
    # words, &/and/commas, years, parentheticals), so "Jobs at <Company>"
    # and "Jobs for M.Pharm Freshers" survive untouched.
    re.compile(r"\s(?:(?i:remote)\s+)?(?i:jobs?|careers?)\s+(?:(?i:opening)s?\s+)?"
               r"(?:(?i:in)\s+)?"
               r"(?:(?:[A-Z][\w.’'&-]*|&|,|(?i:and)|(?:19|20)\d{2}|\([^)]*\)|[–—-])\s*){1,6}$"),
    # "Recruitment 2026 – Hyderabad", "Jobs 2026 – Thane, Hyderabad & Bengaluru"
    re.compile(r"\s(?i:recruitment|jobs?|careers?|hiring)\s+(?:19|20)\d{2}\s*[–—:-]\s*.*$"),
    # bare trailing "Pharma Careers" / "Jobs" / "Recruitment" / year /
    # "Apply Now (for)" runs
    re.compile(r"[\s–—:,|-]+(?:(?i:pharma\s+)?(?i:careers?|jobs?|recruitment|hiring)"
               r"|(?i:apply\s+now(?:\s+for)?)|(?:19|20)\d{2})\s*$"),
    # a lone "Pharma" left dangling after a delimiter once "Careers" is gone
    # ("Job at Sun Pharma in Mumbai & Chennai: Pharma [Careers in India]")
    re.compile(r"\s*[:–—-]\s*(?i:pharma)$"),
]


def clean_title(raw_title):
    """De-SEO a post headline into a storable job title.

    Cut at the first "|"; strip walk-in-drive / hiring / "Jobs <year>:"
    prefixes and "Jobs in <locations>" / "Careers <year>" tails; collapse
    whitespace. If aggressive cleaning leaves fewer than
    MIN_CLEAN_TITLE_LEN characters ("Intern Jobs in Chennai & Bangalore |
    ..." -> "Intern"), fall back to the "|" cut alone.
    """
    base = clean_text(raw_title)
    pipe_cut = base.split("|", 1)[0].strip(_TITLE_EDGE_CHARS)
    text = pipe_cut
    for pattern in _TITLE_PREFIX_RES:
        text = pattern.sub("", text)
    while True:
        before = text
        for pattern in _TITLE_TAIL_RES:
            text = pattern.sub("", text).strip(_TITLE_EDGE_CHARS)
        if text == before:
            break
    text = clean_text(text).strip(_TITLE_EDGE_CHARS)
    if len(text) < MIN_CLEAN_TITLE_LEN:
        return pipe_cut or base
    return text


# FIX 2026-08-27 (PHARMARECRUITER-01): when the Company bullet is missing or
# holds the SEO page title ("Senior PV Scientist Jobs in Mumbai & Noida |
# Pharmacovigilance Careers in India"), company must be EMPTY — never the
# headline. Anything with a "|", a "Jobs in", or more than
# MAX_COMPANY_LEN characters is treated as a failed extraction.
MAX_COMPANY_LEN = 60
_BAD_COMPANY_RE = re.compile(r"\||\bjobs\s+in\b", re.IGNORECASE)


def clean_company(raw):
    """Company bullet -> employer name, or "" when extraction failed."""
    company = clean_text(raw)
    if not company:
        return ""
    if len(company) > MAX_COMPANY_LEN or _BAD_COMPANY_RE.search(company):
        return ""
    return company


_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S)

# Bullet labels -> canonical field. First match wins per field.
_LABEL_MAP = [
    (re.compile(r"^(company name|company|organization|organisation|hospital|employer)$", re.I), "company"),
    (re.compile(r"^(position|positions|designation|post|role|job title)$", re.I), "position"),
    (re.compile(r"^(location|job location|venue location|city)$", re.I), "location"),
    (re.compile(r"^(experience|experience required)$", re.I), "experience"),
    (re.compile(r"^(qualification|qualifications|education|eligibility)$", re.I), "qualification"),
    (re.compile(r"^(work type|job type|employment type)$", re.I), "work_type"),
    (re.compile(r"^(salary|stipend|ctc|pay|remuneration|pay scale|salary range)$", re.I), "salary"),
]


def extract_labeled_fields(content_html):
    """Pull `Label: value` bullets out of the post body.

    Posts write them as <li><strong>Label</strong>: value</li> inside the
    "Job Details" list, but labels also appear in other lists (venue,
    eligibility), so every <li> in the post is scanned; the first value
    seen for each field wins (Job Details comes first in the body).
    """
    fields = {}
    for li in _LI_RE.findall(content_html or ""):
        text = strip_html(li)
        if ":" not in text:
            continue
        label, _, value = text.partition(":")
        label, value = clean_text(label), clean_text(value)
        if not value:
            continue
        for pattern, field in _LABEL_MAP:
            if pattern.match(label) and field not in fields:
                fields[field] = value
                break
    return fields


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FOREIGN_CUR_RE = re.compile(r"\b(AED|SAR|QAR|KWD|BHD|OMR|USD|GBP|EUR)\b|[$£€]")
_LPA_RE = re.compile(r"lpa|lakh|lac", re.IGNORECASE)
_K_SUFFIX_RE = re.compile(r"\d\s*k\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"per\s*year|/\s*year|annum|p\.?a\b|yearly|annual|lpa", re.IGNORECASE)
_MONTH_RE = re.compile(r"per\s*month|/\s*month|month|p\.?m\b|stipend", re.IGNORECASE)


def parse_salary(raw):
    """Parse the (rare) salary bullets.

    Real-world shapes: "₹3.5 – 5 LPA", "Rs. 25,000 per month", "15k-20k",
    "4,50,000 P.A.", "Best in Industry", "As per company norms".

    Returns {} for empty input (caller keeps "Not Disclosed"); a raw-only
    dict when no usable amount is present (never invents values); else
    salary_min/max (int, full INR), salary_period (club enum) and
    salary_currency. LPA/lakh multiplies by 100,000; a trailing "k" by
    1,000. With no period stated, amounts >= 100,000 read as per-year,
    below as per-month (Indian norms).
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
    """"0-7 Years" -> (0, 7); "Fresher Only" -> (0, 0);
    "Freshers (2026 pass-outs) to 8 Years" -> (0, 8); "Min 3 years" -> (3, "");
    unparseable/empty -> ("", "")."""
    text = clean_text(raw)
    if not text:
        return "", ""
    fresher = bool(_FRESHER_RE.search(text))
    # strip parentheticals so "(2026 pass-outs)" isn't read as years
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


# Junk detection only — an article-shaped post title (listicle, guide, exam
# result) is kept AND flagged needs_review. It never decides a category.
_NEWSY_TITLE_RE = re.compile(
    r"^\s*top\s+\d+|how to\b|salary trends|career guide|tips for|"
    r"\bexam\b.*\bresult|admit card", re.IGNORECASE)


def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The site's own category slugs (jobs, production-jobs, ...) are the
    curated `skills` signal; the raw value stays in the rich CSV as the
    `site_categories` source column only and never decides the category —
    neither does the SEARCH_TERMS query that surfaced the post. Returns
    in_scope; False means DROP the row (excluded_out_of_scope).
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
    # local junk/missing-company flags survive alongside the classifier's
    row["needs_review"] = bool(verdict["needs_review"]) or bool(row.get("needs_review"))
    return verdict["in_scope"]


_HOSPITAL_RE = re.compile(r"hospital|clinic|medical college|nursing home", re.IGNORECASE)


def classify_company_type(company, title):
    """club enum hospital|pharma — pharma is this site's default."""
    if _HOSPITAL_RE.search(company or "") or _HOSPITAL_RE.search(title or ""):
        return "hospital"
    return "pharma"


_PAREN_RE = re.compile(r"\([^)]*\)")

# FIX 2026-08-27 (PHARMARECRUITER-03): multi-location bullets used to land
# whole in city ("India – Hyderabad; India – Bengaluru; India –
# Bengaluru-Remote"). normalize_city() cuts to the FIRST site, drops the
# "India –" prefix, and empties "Remote"-only and bare-"India" values; the
# untouched string is kept in the rich CSV's location_raw column.
_MULTI_SITE_SPLIT_RE = re.compile(r"\s*[;/|]\s*")
_INDIA_PREFIX_RE = re.compile(r"^india\b[\s–—:,-]*", re.IGNORECASE)
_NON_CITY_RE = re.compile(r"^(?:remote|work\s+from\s+home|wfh|india)$", re.IGNORECASE)


def normalize_city(raw):
    """First real city out of a (possibly multi-site) location string.

    "India – Hyderabad; India – Bengaluru" -> "Hyderabad";
    "Gurugram ; Kochi" -> "Gurugram"; "India – Remote" -> "";
    "Remote" -> ""; bare "India" -> "".
    """
    text = clean_text(raw)
    if not text:
        return ""
    text = _MULTI_SITE_SPLIT_RE.split(text)[0].strip(" ,")
    text = _INDIA_PREFIX_RE.sub("", text).strip(" ,")
    if not text or _NON_CITY_RE.match(text):
        return ""
    return text


def parse_location(raw):
    """Location bullet -> (city, country_name, iso, dial).

    "Ahmedabad, India" -> Ahmedabad / India; "Dubai, UAE" -> Dubai / UAE;
    "Karakhadi & Ankleshwar, Gujarat" -> Karakhadi & Ankleshwar / India;
    "Not specified (India-based)" -> "" / India. Unrecognised trailing
    segments (Indian states/cities) stay part of India. The city always
    passes through normalize_city() (PHARMARECRUITER-03), so semicolon
    lists, "India –" prefixes, "Remote" and bare "India" never reach the
    city column.
    """
    text = clean_text(_PAREN_RE.sub(" ", clean_text(raw)))
    if not text or re.match(r"not specified|not mentioned|various|pan.india|multiple", text, re.I):
        return "", "India", "IN", "+91"
    # first alternative wins when semicolons/slashes/pipes list several sites
    text = _MULTI_SITE_SPLIT_RE.split(text)[0].strip(" ,")
    # "Training at Indore", "Based in Hyderabad" -> keep just the place
    text = re.sub(r"^(?:training|based|posting|interviews?)\s+(?:at|in)\s+",
                  "", text, flags=re.IGNORECASE)
    parts = [clean_text(p) for p in text.split(",") if clean_text(p)]
    if not parts:
        return "", "India", "IN", "+91"
    country_key = parts[-1].lower()
    if country_key in COUNTRY_META:
        code, dial = COUNTRY_META[country_key]
        name = "India" if code == "IN" else parts[-1]
        city = parts[0] if len(parts) > 1 else ("" if code != "IN" else parts[0])
        return normalize_city(city), name, code, dial
    return normalize_city(parts[0]), "India", "IN", "+91"


_WORK_TYPE_MAP = [
    (re.compile(r"remote|work from home|wfh", re.I), "remote"),
    (re.compile(r"hybrid", re.I), "hybrid"),
    (re.compile(r"part.?time", re.I), "part_time"),
]


def classify_job_type(work_type_raw):
    text = clean_text(work_type_raw)
    for pattern, value in _WORK_TYPE_MAP:
        if pattern.search(text):
            return value
    return "full_time"


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df):
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


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
                # WP returns 400 for a page number past the last page
                return []
            if 400 <= resp.status_code < 500:
                log.warning("HTTP %d for %s", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


def fetch_category_map(session):
    """{id: slug} for all categories (one page covers the site's 14)."""
    data = _request(session, CATEGORIES_URL, params={"per_page": 100})
    if not data:
        sys.exit("Could not fetch category list — aborting.")
    return {c["id"]: c["slug"] for c in data}


def fetch_posts_page(session, page, jobs_category_id, search=None):
    """One page of the jobs category, newest-first (WP default order).

    `search` uses WordPress's built-in full-text parameter, which covers the
    post title AND body — so a listing that names the domain only in the
    requirements still comes back.
    """
    params = {
        "categories": jobs_category_id,
        "per_page": PAGE_SIZE,
        "page": page,
        "_fields": "id,date,link,title,content,categories",
    }
    if search:
        params["search"] = search
    return _request(session, POSTS_URL, params=params)


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def post_to_rich_row(post, category_map):
    title_raw = clean_text(post["title"]["rendered"])
    title = clean_title(title_raw)  # PHARMARECRUITER-02: de-SEO'd for storage
    content = post["content"]["rendered"]
    fields = extract_labeled_fields(content)

    # local flags only; the taxonomy fields are stamped by apply_classification()
    needs_review = bool(_NEWSY_TITLE_RE.search(title_raw))
    # PHARMARECRUITER-01: a failed extraction (missing bullet, or an SEO
    # page title in the bullet) leaves company EMPTY, never the headline
    company = clean_company(fields.get("company", ""))
    if not company:
        needs_review = True
    location_raw = clean_text(fields.get("location", ""))
    city, country_name, code, dial = parse_location(location_raw)
    min_exp, max_exp = parse_experience(fields.get("experience", ""))
    slugs = [category_map.get(c, str(c)) for c in post.get("categories", [])]

    row = {
        "source": SITE,
        "job_id": str(post["id"]),
        "title": title,
        "position": clean_text(fields.get("position", "")),
        "company": company,
        "city": city,
        "country": country_name,
        "country_code": code,
        "country_dial_code": dial,
        "salary_raw": "Not Disclosed",
        "salary_min": "",
        "salary_max": "",
        "salary_period": "",
        "salary_currency": "",
        "job_type": classify_job_type(fields.get("work_type", "")),
        "experience_raw": clean_text(fields.get("experience", "")),
        "min_experience": min_exp,
        "max_experience": max_exp,
        "qualification": clean_text(fields.get("qualification", ""))[:300],
        "work_type_raw": clean_text(fields.get("work_type", "")),
        "site_categories": "; ".join(slugs),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "company_type": classify_company_type(company, title),
        "needs_review": needs_review,
        "posted_date": clean_text(post.get("date", ""))[:10],
        "description": strip_html(strip_seo_intro(content)),
        "job_url": clean_text(post.get("link", "")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title_raw": title_raw,
        "location_raw": location_raw,
    }
    row.update(parse_salary(fields.get("salary", "")))
    return row


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


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
        "city_name": _blank(r.get("city")),
        # PHARMARECRUITER-01 (fixed 2026-08-27): this used to fall back to
        # the post title, which put SEO headlines ("Senior PV Scientist Jobs
        # in Mumbai & Noida | ...") into company_name. Unknown employer now
        # stays EMPTY.
        "company_name": _blank(r.get("company")),
        "company_type": _blank(r.get("company_type")) or "pharma",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        # the site labels a Qualification field on most posts; fall back to
        # lifting credentials out of the body when it is absent
        "qualification": (_blank(r.get("qualification"))
                          or extract_qualification(_blank(r.get("description")))),
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
        description="Scrape jobs from pharmarecruiter.in.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N pages of %d posts (test runs)" % PAGE_SIZE)
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    category_map = fetch_category_map(session)
    slug_to_id = {slug: cid for cid, slug in category_map.items()}
    jobs_category_id = slug_to_id.get(JOBS_CATEGORY_SLUG)
    if not jobs_category_id:
        sys.exit("Category '{}' not found on the site — aborting.".format(
            JOBS_CATEGORY_SLUG))

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "excluded_out_of_scope": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    new_rows, review_log = [], []
    # One search term at a time; the cursor advances when a term runs dry,
    # so every family gets its own full pagination.
    term_idx, page, empty_pages = 0, 1, 0

    while True:
        if term_idx >= len(SEARCH_TERMS):
            break
        term = SEARCH_TERMS[term_idx]
        if args.max_pages is not None and page > args.max_pages:
            term_idx, page, empty_pages = term_idx + 1, 1, 0
            continue
        posts = fetch_posts_page(session, page, jobs_category_id, search=term)
        page += 1
        if posts is None or posts == []:
            term_idx, page, empty_pages = term_idx + 1, 1, 0
            continue
        empty_pages = 0

        page_all_old = True
        for post in posts:
            counters["scanned"] += 1
            try:
                row = post_to_rich_row(post, category_map)
            except Exception as exc:
                log.warning("Skipping malformed post %s: %s", post.get("id"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
            # gate after the date check so an all-out-of-scope page does not
            # look like an all-old page and stop the newest-first crawl
            if not apply_classification(row):
                counters["excluded_out_of_scope"] += 1
                continue
            if row["job_id"] in known_ids:
                counters["duplicates"] += 1
                continue
            if row["needs_review"]:
                counters["needs_review"] += 1
                review_log.append({"job_id": row["job_id"], "title": row["title"],
                                   "site_categories": row["site_categories"]})
            known_ids.add(row["job_id"])
            new_rows.append(row)
            counters["new"] += 1

        # Newest-first WITHIN a term: once a whole page is older than the
        # cutoff, or the term returns a short page, that TERM is exhausted —
        # advance the cursor. Until 2026-08-25 this was a bare `break`, which
        # ended the whole crawl and left every later term unsearched, so only
        # the first term was ever walked.
        if page_all_old or len(posts) < PAGE_SIZE:
            log.debug("Term %r exhausted after %d page(s)", term, page - 1)
            term_idx, page, empty_pages = term_idx + 1, 1, 0
            continue

    # ---- rich cumulative CSV ----
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[[c for c in RICH_COLUMNS if c in combined.columns]]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)

    print("\n===== Run summary =====")
    print("Excluded (out of scope): {:>4,}".format(counters["excluded_out_of_scope"]))
    print("Posts scanned:         {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
