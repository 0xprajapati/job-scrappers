#!/usr/bin/env python3
"""Scrape job listings from pharmabharat.com (pharma job portal, India).

Data source
-----------
pharmabharat.com is a WordPress site whose REST API is fully open (robots.txt
is empty), so the scraper never parses listing pages:

    GET https://pharmabharat.com/wp-json/wp/v2/posts
        ?per_page=50&page=N
        &_fields=id,date,link,title,content,categories

Posts come back newest-first with the FULL article HTML (~11,500 posts, a
few dozen per day), so the watermark stop keeps daily runs to a page or two.
Unlike pharmarecruiter.in there is no jobs/news category split: every
category on the site is a job-type taxonomy (production-jobs, qc-jobs,
clinical-research-jobs, pharmacovigilance-jobs, ...), so ALL posts are
scraped and category slugs are stored for reference.

Posts embed their facts in one of three shapes (newest posts mix all three):

    1. a "Job Details" table:  <tr><td>Company</td><td>Sandoz</td></tr>
    2. labelled bullets:       <li><strong>Experience:</strong> 2-6 Years</li>
    3. labelled paragraphs:    <strong>Company:</strong><br>Glenmark Pharma
       (label line, value on the same or the following text line)

extract_labeled_fields() tries all three, first value per field wins.

Quirks
------
* One post often advertises a multi-role drive; the post title is kept as
  the job title (matching how the site presents it).
* Many "Salary" values are the SITE'S estimates written in prose ("Based on
  industry standards..."). Only compact values containing a real amount are
  parsed; the raw string keeps qualifiers like "(Estimated)". Prose-only or
  absent salary stays "Not Disclosed" — never invented (master spec).
* Everything on the site is pharma-industry, so the healthcare filter is
  the source itself; club category is mapped from the title (pharmacist /
  doctor / nurse regexes, else non_clinical — production, QC, QA, CR, PV).
* "Application Deadline" bullets (walk-ins, government posts) are parsed
  into `expires_at` when they carry a "28 July 2026"-style date.

Outputs
-------
* pharmabharat_jobs.csv                         — rich cumulative store (dedup: post id)
* ../../jobs_csv/<DD-MM-YYYY>/pharmabharat.csv  — HealthCareers.club 22-col schema

Time window (master spec): first run keeps the last INITIAL_WINDOW_DAYS;
later runs keep only jobs newer than the newest stored date minus
WATERMARK_GRACE_DAYS of overlap.

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
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "pharmabharat"
SITE_BASE = "https://pharmabharat.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
POSTS_URL = SITE_BASE + "/wp-json/wp/v2/posts"
CATEGORIES_URL = SITE_BASE + "/wp-json/wp/v2/categories"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

PAGE_SIZE = 50
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
MAX_EMPTY_PAGES = 3
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = "pharmabharat_jobs.csv"
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

# country name (lowercased) -> (ISO code, dial code); everything else is
# treated as India, the site's home market.
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
    "work_type_raw", "department", "site_categories", "category",
    "company_type", "needs_review", "posted_date", "expires_at",
    "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("pharmabharat_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(text or "")).strip()


def strip_html(markup):
    return clean_text(_TAG_RE.sub(" ", markup or ""))


# Labels -> canonical field. First match wins per field.
_LABEL_MAP = [
    (re.compile(r"^(company name|company|organization|organisation|hospital|employer)$", re.I), "company"),
    (re.compile(r"^(position|positions|designation|post|role|job title|job role|job roles)$", re.I), "position"),
    (re.compile(r"^(location|locations|job location|city)$", re.I), "location"),
    (re.compile(r"^(experience|experience required|total experience)$", re.I), "experience"),
    (re.compile(r"^(qualification|qualifications|education|eligibility)$", re.I), "qualification"),
    (re.compile(r"^(job type|employment type|work type)$", re.I), "work_type"),
    (re.compile(r"^(work mode|mode of work)$", re.I), "work_mode"),
    (re.compile(r"^(department|departments)$", re.I), "department"),
    (re.compile(r"^(salary|estimated salary|expected salary|stipend|ctc|pay|remuneration|salary range)$", re.I), "salary"),
    (re.compile(r"^(application deadline|last date to apply|last date)$", re.I), "deadline"),
]

_MAX_LABEL_LEN = 30

_TABLE_ROW_RE = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", re.S)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S)

# Salary values must show a real amount; prose-only estimates are skipped.
_SALARY_VALUE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*[\d,.]|\d\s*(?:lpa|lakh|k\b)|\d{4,}",
                              re.IGNORECASE)


def _match_label(label):
    if len(label) > _MAX_LABEL_LEN:
        return None
    for pattern, field in _LABEL_MAP:
        if pattern.match(label):
            return field
    return None


def _accept(fields, field, value):
    if field == "salary" and not _SALARY_VALUE_RE.search(value):
        return
    if field not in fields:
        fields[field] = value


def extract_labeled_fields(content_html):
    """Pull `Label: value` facts out of the post body.

    Three shapes coexist on the site, tried in order of reliability:
    "Job Details" table rows, labelled <li> bullets, then labelled text
    lines (label ending in ":" with the value on the same or next line).
    The first value seen per field wins.
    """
    content_html = content_html or ""
    fields = {}

    for label_html, value_html in _TABLE_ROW_RE.findall(content_html):
        label, value = clean_text(strip_html(label_html)), strip_html(value_html)
        field = _match_label(label)
        if field and value:
            _accept(fields, field, value)

    for li in _LI_RE.findall(content_html):
        text = strip_html(li)
        if ":" not in text:
            continue
        label, _, value = text.partition(":")
        label, value = clean_text(label), clean_text(value)
        field = _match_label(label)
        if field and value:
            _accept(fields, field, value)

    # Paragraph shape: tags become line breaks, then scan label lines.
    lines = [clean_text(l) for l in _TAG_RE.sub("\n", content_html).split("\n")]
    lines = [l for l in lines if l]
    for i, line in enumerate(lines):
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label, value = clean_text(label), clean_text(value)
        field = _match_label(label)
        if not field:
            continue
        if not value and i + 1 < len(lines):
            nxt = lines[i + 1]
            # a following label line is not this field's value
            if not (":" in nxt and _match_label(clean_text(nxt.partition(":")[0]))):
                value = nxt
        if value:
            _accept(fields, field, value)
    return fields


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FOREIGN_CUR_RE = re.compile(r"\b(AED|SAR|QAR|KWD|BHD|OMR|USD|GBP|EUR)\b|[$£€]")
_LPA_RE = re.compile(r"lpa|lakh|lac", re.IGNORECASE)
_K_SUFFIX_RE = re.compile(r"\d\s*k\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"per\s*year|/\s*year|annum|p\.?a\b|yearly|annual|lpa", re.IGNORECASE)
_MONTH_RE = re.compile(r"per\s*month|/\s*month|month|p\.?m\b|stipend", re.IGNORECASE)
_PAREN_RE = re.compile(r"\([^)]*\)")


def parse_salary(raw):
    """Parse the salary field.

    Real-world shapes: "₹6.5 – ₹10 LPA (Estimated)", "₹21,900 CTC per Month",
    "₹8,00,000 – ₹13,00,000 per annum (CTC)", "₹2.5 LPA – ₹10 LPA
    (depending on qualification...)". Parentheticals are dropped before
    number extraction so "(2025 standards)" is never read as an amount, but
    the raw string keeps them so estimates stay identifiable.

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
    amount_text = clean_text(_PAREN_RE.sub(" ", text))
    numbers = [float(n.replace(",", "")) for n in _NUM_RE.findall(amount_text)]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return {"salary_raw": text[:120]}

    foreign = _FOREIGN_CUR_RE.search(amount_text)
    if foreign and foreign.group(0) not in ("USD", "$"):
        return {"salary_raw": text[:120]}
    currency = "USD" if foreign else "INR"

    if _LPA_RE.search(amount_text):
        numbers = [n * 100_000 for n in numbers]
    elif _K_SUFFIX_RE.search(amount_text):
        numbers = [n * 1_000 if n < 1_000 else n for n in numbers]

    lo, hi = numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]
    if hi < lo:
        lo, hi = hi, lo
    if _YEAR_RE.search(amount_text):
        period = "per_annum"
    elif _MONTH_RE.search(amount_text):
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
    """"2–6 Years" -> (2, 6); "Fresher Only" -> (0, 0);
    "Freshers (2026 pass-outs) to 8 Years" -> (0, 8); "Min 3 years" -> (3, "");
    unparseable/empty -> ("", "")."""
    text = clean_text(raw)
    if not text:
        return "", ""
    fresher = bool(_FRESHER_RE.search(text))
    # strip parentheticals so "(2026 pass-outs)" isn't read as years
    stripped = _PAREN_RE.sub(" ", text)
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


_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"cardiologist|radiologist|pathologist|psychiatrist|intensivist", re.IGNORECASE)
_NEWSY_TITLE_RE = re.compile(
    r"^\s*top\s+\d+|how to\b|salary trends|career guide|tips for|"
    r"\bexam\b.*\bresult|admit card", re.IGNORECASE)


def classify_category(title, site_category_slugs=()):
    """Club category from the title (plus the site's own taxonomy). The
    site is pharma-industry, so most roles (production, QC, QA, clinical
    research, PV, regulatory) are non_clinical; only explicit pharmacist /
    doctor / nurse signals map to clinical buckets. Returns
    (category, needs_review) — news-shaped titles are kept but flagged,
    never silently dropped (master spec)."""
    title = title or ""
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title) or "pharmacist" in site_category_slugs:
        category = "pharmacists"
    elif _DOCTOR_RE.search(title):
        category = "doctors"
    else:
        category = "non_clinical"
    return category, bool(_NEWSY_TITLE_RE.search(title))


_HOSPITAL_RE = re.compile(r"hospital|clinic|medical college|nursing home", re.IGNORECASE)


def classify_company_type(company, title):
    """club enum hospital|pharma — pharma is this site's default."""
    if _HOSPITAL_RE.search(company or "") or _HOSPITAL_RE.search(title or ""):
        return "hospital"
    return "pharma"


def parse_location(raw):
    """Location field -> (city, country_name, iso, dial).

    "Mumbai, Maharashtra" -> Mumbai / India; "Telangana, India" ->
    Telangana / India; "Dubai, UAE" -> Dubai / UAE; "Not specified" ->
    "" / India. Unrecognised trailing segments (Indian states/cities)
    stay part of India, the site's home market.
    """
    text = clean_text(_PAREN_RE.sub(" ", clean_text(raw)))
    if not text or re.match(r"not specified|not mentioned|various|pan.india|multiple", text, re.I):
        return "", "India", "IN", "+91"
    # first alternative wins when slashes/pipes list several sites
    text = re.split(r"\s*[/|]\s*", text)[0].strip(" ,")
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
        return city, name, code, dial
    return parts[0], "India", "IN", "+91"


_WORK_TYPE_MAP = [
    (re.compile(r"remote|work from home|wfh", re.I), "remote"),
    (re.compile(r"hybrid", re.I), "hybrid"),
    (re.compile(r"part.?time", re.I), "part_time"),
    (re.compile(r"intern|apprentice|trainee|stipend", re.I), "internship"),
]


def classify_job_type(work_type_raw, work_mode_raw=""):
    """Club enum from Job Type + Work Mode ("Walk-In Interview" and
    "Work From Office" both count as on-site full_time)."""
    text = clean_text(work_type_raw) + " " + clean_text(work_mode_raw)
    for pattern, value in _WORK_TYPE_MAP:
        if pattern.search(text):
            return value
    return "full_time"


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
_DEADLINE_RE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})")


def parse_deadline(raw):
    """"28 July 2026" / "28th July, 2026 (Sunday)" -> "2026-07-28";
    anything else -> ""."""
    m = _DEADLINE_RE.search(clean_text(raw or ""))
    if not m:
        return ""
    month = _MONTHS.get(m.group(2).lower())
    if not month:
        return ""
    try:
        return date(int(m.group(3)), month, int(m.group(1))).isoformat()
    except ValueError:
        return ""


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
    """{id: slug} for all categories (one page covers the site's ~40)."""
    data = _request(session, CATEGORIES_URL, params={"per_page": 100})
    if not data:
        sys.exit("Could not fetch category list — aborting.")
    return {c["id"]: c["slug"] for c in data}


def fetch_posts_page(session, page):
    """One page of posts, newest-first (WP default order). No category
    filter: every category on this site is a job-type taxonomy."""
    return _request(session, POSTS_URL, params={
        "per_page": PAGE_SIZE,
        "page": page,
        "_fields": "id,date,link,title,content,categories",
    })


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def post_to_rich_row(post, category_map):
    title = clean_text(post["title"]["rendered"])
    content = post["content"]["rendered"]
    fields = extract_labeled_fields(content)
    slugs = [category_map.get(c, str(c)) for c in post.get("categories", [])]

    category, needs_review = classify_category(title, slugs)
    company = clean_text(fields.get("company", ""))
    if not company:
        needs_review = True
    city, country_name, code, dial = parse_location(fields.get("location", ""))
    min_exp, max_exp = parse_experience(fields.get("experience", ""))

    row = {
        "source": SITE,
        "job_id": str(post["id"]),
        "title": title,
        "position": clean_text(fields.get("position", ""))[:200],
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
        "job_type": classify_job_type(fields.get("work_type", ""),
                                      fields.get("work_mode", "")),
        "experience_raw": clean_text(fields.get("experience", ""))[:120],
        "min_experience": min_exp,
        "max_experience": max_exp,
        "qualification": clean_text(fields.get("qualification", ""))[:300],
        "work_type_raw": clean_text(" / ".join(
            v for v in (fields.get("work_type"), fields.get("work_mode")) if v))[:120],
        "department": clean_text(fields.get("department", ""))[:200],
        "site_categories": "; ".join(slugs),
        "category": category,
        "company_type": classify_company_type(company, title),
        "needs_review": needs_review,
        "posted_date": clean_text(post.get("date", ""))[:10],
        "expires_at": parse_deadline(fields.get("deadline", "")),
        "description": strip_html(content)[:DESCRIPTION_MAX_CHARS],
        "job_url": clean_text(post.get("link", "")),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        "company_name": _blank(r.get("company")) or _blank(r.get("title")),
        "company_type": _blank(r.get("company_type")) or "pharma",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": _blank(r.get("description")),
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")) or "non_clinical",
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("min_experience")),
        "max_experience": _int_str(r.get("max_experience")),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(r.get("salary_max")) if has_salary else "",
        "salary_period": _blank(r.get("salary_period")) if has_salary else "",
        "salary_currency": currency if has_salary else "",
        "is_active": "true",
        "expires_at": _blank(r.get("expires_at")),
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
        description="Scrape jobs from pharmabharat.com.")
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

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0}
    new_rows, review_log = [], []
    page, empty_pages = 1, 0

    while True:
        if args.max_pages is not None and page > args.max_pages:
            break
        posts = fetch_posts_page(session, page)
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
            except Exception as exc:
                log.warning("Skipping malformed post %s: %s", post.get("id"), exc)
                continue
            if row["posted_date"] and row["posted_date"] < cutoff:
                counters["excluded_old"] += 1
                continue
            page_all_old = False
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

        # newest-first: once a whole page is older than the cutoff, stop.
        if page_all_old or len(posts) < PAGE_SIZE:
            break

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
        pd.DataFrame(review_log).to_csv("needs_review.csv", index=False)

    print("\n===== Run summary =====")
    print("Posts scanned:         {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:  {:>5,}".format(counters["needs_review"]))
    print("New jobs added:        {:>5,}".format(counters["new"]))
    print("Duplicates skipped:    {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
