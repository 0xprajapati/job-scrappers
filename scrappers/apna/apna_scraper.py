#!/usr/bin/env python3
"""Scrape healthcare job listings from apna.co into a deduplicated CSV.

Data source
-----------
apna.co is a general job board (~91k jobs), but its department listing pages
pre-filter to healthcare:

    https://apna.co/jobs/dep_healthcare_doctor_hospital_staff-jobs?page=N

Since ~Aug 2026 apna serves Next.js App Router pages: the old pages-router
__NEXT_DATA__ JSON is gone, listing cards are plain server-rendered HTML
(no dates, no salary floor detail), and the RSC flight stream on LISTING
pages carries no job JSON. Each DETAIL page, however, embeds the complete
job object (the same shape the old listing JSON had — fixed salary range,
description, education, shift, gender, created_on/last_updated, ui_tags,
organization) in its own flight stream (self.__next_f.push chunks).

The crawl is therefore two-phase:
1. Sweep listing pages (~25 cards each, 1 cheap request per page) collecting
   job URL + title stubs, until an empty page.
2. For stubs that survive the deny-title gate and aren't already in the CSV,
   fetch the detail page and extract the job object from the flight stream.

posted_date is last_updated (this is what apna itself publishes as
datePosted in the page's structured data; created_on can be months older
for re-upped postings) with created_on as fallback. Because the listing is
relevance-ordered and dates are only known after the detail fetch, jobs
older than the cutoff are counted excluded_old and NOT stored, so they may
be detail-fetched again on later runs (bounded: active postings expire
~10 days after their last re-up).

The salary filter uses the FIXED lower bound: earning potential (incentives)
never counts toward the threshold. A fixed floor of 0 means "salary not
disclosed" and is excluded.

Detail data already includes role category and education, so the old
--enrich second fetch is no longer needed (the flag remains a no-op for
compatibility; company_address is filled from the detail payload when
present).

Run `python apna_scraper.py --help` for options.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE_BASE = "https://apna.co"
ROBOTS_URL = SITE_BASE + "/robots.txt"
SOURCE = "apna.co"

# Department listing slugs to crawl. Healthcare only by default; append more
# slugs (e.g. "dep_beauty_fitness_personal_care-jobs") to widen coverage.
DEPARTMENT_SLUGS = [
    "dep_healthcare_doctor_hospital_staff-jobs",
]

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (personal research; contact: eleswarapu.madhav@gmail.com)"
)

# Time window (no salary filter — salaries are captured but never filtered on).
# First run keeps jobs posted in the last INITIAL_WINDOW_DAYS; later runs keep
# only jobs newer than the newest posted_date already in the CSV, minus
# WATERMARK_GRACE_DAYS of overlap (dedup absorbs the overlap).
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 3_000

DEFAULT_OUTPUT_CSV = "apna_jobs.csv"

WORK_MODE_TAGS = ("Work from Office", "Work from Home", "Field Job")
JOB_TYPE_TAGS = ("Full Time", "Part Time")

# Safety net (master spec §2): apna's healthcare department also carries
# aggregated external postings from big employers, and some of those are
# clearly non-healthcare. Titles matching these phrases are dropped.
DENY_TITLE_KEYWORDS = [
    "software developer", "software engineer", "web developer", "full stack",
    "front end", "frontend", "back end", "backend", "mobile app", "devops",
    "computer science", "data engineer", "network engineer", "hardware",
    "mechanical engineer", "civil engineer", "electrical engineer",
    "maintenance engineer", "design engineer", "fiber", "fibre",
    "accountant", "accounts executive", "chartered accountant", "cashier",
    "graphic designer", "ui designer", "ux designer", "video editor",
    "digital marketing", "marketing executive", "marketing manager",
    "sales executive", "sales manager", "business development",
    "telecaller", "tele caller", "receptionist", "front office", "front desk",
    "human resource", "hr executive", "hr manager", "recruiter",
    "housekeeping", "security guard", "security supervisor", "driver",
    "electrician", "plumber", "cook", "chef", "store keeper", "storekeeper",
    "purchase executive", "billing executive", "data entry",
    "bidding", "auction", "proposal manager",
]

_DENY_RE = re.compile(
    "|".join(r"\b" + re.escape(kw) + r"\b" for kw in DENY_TITLE_KEYWORDS),
    re.IGNORECASE)

CSV_COLUMNS = [
    "source", "job_id", "title", "company", "location",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "work_mode", "job_type", "experience_raw", "english_level", "department",
    "role_category", "education", "degree_specialisation", "shift", "gender",
    "posted_date", "description", "job_url", "apply_url", "scraped_at",
]

log = logging.getLogger("apna_scraper")

# ----------------------------------------------------------------------------
# Salary parsing
# ----------------------------------------------------------------------------

_SALARY_NUM_RE = re.compile(r"[0-9][0-9,]*")


def parse_salary_string(raw):
    """Parse a displayed salary like "₹60,000 - ₹80,000 monthly".

    Returns (min_monthly, max_monthly) as ints, or None if the string is
    missing or has no numbers. A single number means min == max. (A 0 lower
    bound parses as 0 — the caller's threshold check then excludes it.)
    """
    if not raw or not raw.strip():
        return None
    numbers = [int(n.replace(",", "")) for n in _SALARY_NUM_RE.findall(raw)]
    if not numbers:
        return None
    lo, hi = numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)


def compute_cutoff(existing_df):
    """Return the ISO date below which jobs are skipped.

    First run: today - INITIAL_WINDOW_DAYS. Later runs: the newest
    posted_date already saved, minus WATERMARK_GRACE_DAYS of overlap.
    """
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


def card_salary(card):
    """Extract (salary_raw, filter_min, display_max) from a card data object.

    Filtering uses the FIXED range (incentives excluded); the raw string
    mirrors what the card displays (min_salary - max_salary monthly).
    """
    disp_min, disp_max = card.get("min_salary"), card.get("max_salary")
    if disp_min is None and disp_max is None:
        return ("", None, None)
    raw = "₹{:,} - ₹{:,} monthly".format(disp_min or 0, disp_max or disp_min or 0)

    fixed_min = card.get("fixed_min_salary")
    if fixed_min is None:  # older cards may lack the split; fall back
        fixed_min = disp_min
    fixed_max = card.get("fixed_max_salary")
    if fixed_max is None:
        fixed_max = disp_max if disp_max is not None else fixed_min
    return (raw, fixed_min, fixed_max)


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    return session


def load_robots(session):
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        rp.parse([])
        return rp
    rp.parse(resp.text.splitlines() if resp.status_code < 400 else [])
    probe = "{}/jobs/{}".format(SITE_BASE, DEPARTMENT_SLUGS[0])
    if not rp.can_fetch(USER_AGENT, probe):
        sys.exit("robots.txt disallows {} — aborting.".format(probe))
    log.info("robots.txt check passed")
    return rp


def get_html(session, url, params=None):
    """GET with rate limiting and exponential-backoff retries."""
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
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Listing-page parsing
# ----------------------------------------------------------------------------

_JOB_ID_RE = re.compile(r"(?:-|/)(\d+)/?$")
_WS_RE = re.compile(r"\s+")
_CARD_RE = re.compile(
    r'<a[^>]*data-testid="job-card"[^>]*href="([^"]+)"(.*?)</a>', re.S)
_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def parse_listing_page(html):
    """Return (stubs, total_pages) from a department listing page.

    stubs is a list of {"job_url", "title"} dicts parsed from the
    server-rendered job-card anchors. total_pages is always None (the App
    Router pages don't report it; the crawl stops on the first empty page).
    Returns (None, None) if the page structure is unrecognizable (no App
    Router flight stream at all — bot wall or another redesign).
    """
    if "self.__next_f" not in (html or ""):
        return (None, None)
    stubs = []
    for match in _CARD_RE.finditer(html):
        href, body = match.group(1), match.group(2)
        title_match = _H2_RE.search(body)
        title = ""
        if title_match:
            title = _WS_RE.sub(
                " ", _HTML_TAG_RE.sub(" ", title_match.group(1))).strip()
        stubs.append({
            "job_url": href if href.startswith("http") else SITE_BASE + href,
            "title": title,
        })
    return (stubs, None)


def _flight_stream(html):
    """Concatenate the page's decoded self.__next_f.push text chunks."""
    parts = []
    for match in _FLIGHT_PUSH_RE.finditer(html or ""):
        try:
            parts.append(json.loads(match.group(1)))
        except ValueError:
            pass
    return "".join(parts)


def _grab_object(stream, anchor):
    """Parse the JSON object in `stream` that contains the `anchor` key.

    Walks back from the anchor to the enclosing '{', then does a
    string-aware balanced scan forward. Returns a dict or None.
    """
    at = stream.find(anchor)
    if at < 0:
        return None
    depth, j = 0, at
    while j > 0:
        j -= 1
        char = stream[j]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                break
            depth -= 1
    else:
        return None
    depth, in_str, esc = 0, False, False
    for k in range(j, len(stream)):
        char = stream[k]
        if esc:
            esc = False
            continue
        if char == "\\":
            esc = True
            continue
        if in_str:
            if char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stream[j:k + 1])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


_REF_RE = re.compile(r"^\$([0-9a-f]{1,4})$")


def _resolve_ref(stream, ref_id):
    """Resolve an RSC flight reference ("$3d") to its chunk's value.

    Chunks look like `3d:T5a2,<text…>` (text, hex BYTE length prefix) or
    `3d:{…}` / `3d:[…]` (JSON). Returns str/dict/list or None.
    """
    t_at = stream.find(ref_id + ":T")
    if t_at >= 0:
        head = stream[t_at + len(ref_id) + 2: t_at + len(ref_id) + 12]
        m = re.match(r"([0-9a-f]+),", head)
        if m:
            byte_len = int(m.group(1), 16)
            start = t_at + len(ref_id) + 2 + m.end()
            # binary-search the char count whose UTF-8 encoding is byte_len
            lo, hi = 0, min(len(stream) - start, byte_len)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(stream[start:start + mid].encode("utf-8")) <= byte_len:
                    lo = mid
                else:
                    hi = mid - 1
            return stream[start:start + lo]
    for open_char in ("{", "["):
        j_at = stream.find(ref_id + ":" + open_char)
        if j_at < 0:
            continue
        start = j_at + len(ref_id) + 1
        depth, in_str, esc = 0, False, False
        for k in range(start, len(stream)):
            char = stream[k]
            if esc:
                esc = False
                continue
            if char == "\\":
                esc = True
                continue
            if in_str:
                if char == '"':
                    in_str = False
                continue
            if char == '"':
                in_str = True
                continue
            if char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stream[start:k + 1])
                    except ValueError:
                        return None
        return None
    return None


def fetch_job_card(session, job_url):
    """Fetch a detail page and return its embedded job object, or None."""
    html = get_html(session, job_url)
    if html is None:
        return None
    stream = _flight_stream(html)
    card = (_grab_object(stream, '"created_on"')
            or _grab_object(stream, '"public_url"'))
    if not isinstance(card, dict) or not card.get("title"):
        return None
    # Aggregated/external postings replace nested values with flight refs
    # ("$3d"): resolve one level of them (description text, organization…).
    for field, value in list(card.items()):
        if isinstance(value, str):
            ref = _REF_RE.match(value)
            if ref:
                card[field] = _resolve_ref(stream, ref.group(1))
            elif value == "$undefined":  # RSC placeholder for "no value"
                card[field] = None
    # App Router payload: department is a plain string named job_department.
    if not card.get("department") and isinstance(card.get("job_department"), str):
        card["department"] = {"name": card["job_department"]}
    # Some payload variants carry organization as a bare name string.
    if isinstance(card.get("organization"), str):
        card["organization"] = {"name": card["organization"]}
    if not isinstance(card.get("address"), dict):
        card["address"] = {}
    return card


def card_to_row(card):
    # External/aggregated postings have public_url=None but public_url_v2 set.
    job_url = card.get("public_url") or card.get("public_url_v2") or ""
    id_match = _JOB_ID_RE.search(job_url)
    job_id = str(card.get("id") or (id_match.group(1) if id_match else ""))

    raw, fixed_min, fixed_max = card_salary(card)

    tags = [t.get("text", "") for t in card.get("ui_tags") or []]
    work_mode = ", ".join(t for t in tags if t in WORK_MODE_TAGS)
    if not work_mode:
        work_mode = "Work from Home" if card.get("is_wfh") else "Work from Office"
    job_type = ", ".join(t for t in tags if t in JOB_TYPE_TAGS)
    if not job_type:
        job_type = "Part Time" if card.get("is_part_time") else "Full Time"

    address = card.get("address")
    if not isinstance(address, dict):
        address = {}
    location = card.get("location_name") or ""
    if not location:
        # city/area are dicts on organic postings, bare strings on
        # aggregated ones.
        city = address.get("city")
        if isinstance(city, dict):
            city = city.get("name")
        area = address.get("area")
        if isinstance(area, dict):
            area = area.get("name")
        location = ", ".join(x for x in (area, city)
                             if x and isinstance(x, str)
                             and not x.startswith("$"))

    description = card.get("description")
    if not isinstance(description, str):
        description = ""
    if "<" in description:  # aggregated postings ship rich HTML
        description = _HTML_TAG_RE.sub(" ", description)
    description = _WS_RE.sub(" ", description).strip()

    return {
        "source": SOURCE,
        "job_id": job_id,
        "title": (card.get("title") or "").strip(),
        "company": ((card.get("organization") or {}).get("name") or "").strip(),
        "location": location,
        "salary_raw": raw,
        "salary_min_monthly": fixed_min,
        "salary_max_monthly": fixed_max,
        "work_mode": work_mode,
        "job_type": job_type,
        "experience_raw": card.get("experience_in_years") or "",
        "english_level": card.get("english") or "",
        "department": ((card.get("department") or {}).get("name") or ""),
        "role_category": card.get("category") or "",
        "education": card.get("education") or "",
        "degree_specialisation": "",
        "shift": card.get("shift") or "",
        "gender": card.get("gender") or "",
        # last_updated is what apna publishes as datePosted; created_on can
        # be months older for re-upped postings.
        "posted_date": (card.get("last_updated")
                        or card.get("created_on") or "")[:10],
        "description": description[:DESCRIPTION_MAX_CHARS],
        "job_url": job_url,
        # external postings carry the employer's real application link
        "apply_url": card.get("external_job_url") or "",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----------------------------------------------------------------------------
# Detail-page enrichment
# ----------------------------------------------------------------------------

# Maps detail-page item titles (lowercased prefixes) to CSV columns.
_SECTION_FIELD_MAP = [
    ("role", "role_category"),
    ("degree", "degree_specialisation"),
    ("education", "education"),
    ("shift", "shift"),
    ("gender", "gender"),
    ("english", "english_level"),
    ("experience", "experience_raw"),
]


def extract_detail_sections(html):
    """Parse the "job_details_section" JSON embedded in a detail page.

    The app-router payload contains it either verbatim or with escaped
    quotes. Returns {section_heading: {item_title: subtitle}} or {}.
    """
    for text in (html, html.replace('\\"', '"')):
        idx = text.find('"job_details_section":')
        if idx < 0:
            continue
        start = text.find("[", idx)
        if start < 0:
            continue
        try:
            sections, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        result = {}
        for section in sections:
            if not isinstance(section, dict):
                continue
            items = {}
            for item in section.get("data") or []:
                title = (item.get("title") or "").strip()
                subtitle = item.get("subtitle")
                if title and isinstance(subtitle, str) and subtitle.strip():
                    items[title] = subtitle.strip()
            result[(section.get("heading") or "").strip()] = items
        return result
    return {}


def enrich_row(session, row):
    """Fill role_category, degree_specialisation etc. from the detail page."""
    html = get_html(session, row["job_url"])
    if html is None:
        return
    sections = extract_detail_sections(html)
    if not sections:
        log.warning("No job_details_section on %s", row["job_url"])
        return
    flat = {}
    for items in sections.values():
        flat.update(items)
    for title, value in flat.items():
        lowered = title.lower()
        for prefix, column in _SECTION_FIELD_MAP:
            if lowered.startswith(prefix):
                row[column] = value
                break
    about = sections.get("About company") or {}
    if about.get("Address"):
        row["company_address"] = about["Address"]
    if not row["apply_url"]:  # non-external jobs: apply through apna itself
        row["apply_url"] = row["job_url"]


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_known(csv_path):
    try:
        existing = pd.read_csv(csv_path, dtype=str)
    except FileNotFoundError:
        return set(), None
    if "source" in existing.columns:
        pairs = set(zip(existing["source"].fillna(""), existing["job_id"].fillna("")))
    else:  # tolerate CSVs from before the source column existed
        pairs = {(SOURCE, j) for j in existing["job_id"].fillna("")}
    return pairs, existing


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape healthcare jobs from apna.co into a CSV.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_CSV,
                        help="output CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="crawl at most N pages per department (test runs)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each NEW passing job's detail page for "
                             "role_category, degree, company address")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    load_robots(session)

    known_pairs, existing_df = load_known(args.output)
    log.info("Existing CSV has %d known jobs", len(known_pairs))
    cutoff = compute_cutoff(existing_df)
    log.info("Keeping jobs posted on/after %s", cutoff)

    counters = {"pages": 0, "listings": 0, "excluded_old": 0,
                "excluded_deny_title": 0, "new": 0, "duplicates": 0,
                "page_errors": 0}
    new_rows = []

    for slug in DEPARTMENT_SLUGS:
        base_url = "{}/jobs/{}".format(SITE_BASE, slug)
        page, total_pages = 1, None
        empty_streak = 0
        while True:
            if args.max_pages is not None and page > args.max_pages:
                break
            if total_pages is not None and page > total_pages:
                break
            html = get_html(session, base_url,
                            params={"page": page} if page > 1 else None)
            if html is None:
                counters["page_errors"] += 1
                break  # repeated failures on this department; move on
            stubs, reported_total = parse_listing_page(html)
            if stubs is None:
                counters["page_errors"] += 1
                log.error("Unrecognized page structure at %s?page=%d — stopping "
                          "this department", base_url, page)
                break
            if total_pages is None and reported_total:
                total_pages = int(reported_total)
                log.info("%s: %d pages reported", slug, total_pages)
            if not stubs:
                # The server occasionally returns a valid page with zero
                # cards mid-listing; only stop on a persistent run of them.
                if (total_pages is not None and page < total_pages
                        and empty_streak < 2):
                    empty_streak += 1
                    log.warning("%s: page %d returned no cards (transient?) — "
                                "continuing", slug, page)
                    page += 1
                    continue
                log.info("%s: page %d empty — done", slug, page)
                break
            empty_streak = 0

            counters["pages"] += 1
            for stub in stubs:
                counters["listings"] += 1
                if _DENY_RE.search(stub["title"] or ""):
                    counters["excluded_deny_title"] += 1
                    continue
                id_match = _JOB_ID_RE.search(stub["job_url"])
                if not id_match:
                    log.warning("No job id in %s — skipping", stub["job_url"])
                    continue
                key = (SOURCE, id_match.group(1))
                # Dedup BEFORE the detail fetch: known jobs cost no request.
                if key in known_pairs:
                    counters["duplicates"] += 1
                    continue
                card = fetch_job_card(session, stub["job_url"])
                if card is None:
                    log.warning("No job payload at %s — skipping",
                                stub["job_url"])
                    continue
                try:
                    row = card_to_row(card)
                except Exception as exc:  # never let one card kill the run
                    log.warning("Skipping malformed card on page %d: %s", page, exc)
                    continue
                if row["posted_date"] and row["posted_date"] < cutoff:
                    counters["excluded_old"] += 1
                    continue
                if not row["apply_url"]:  # non-external jobs: apply via apna
                    row["apply_url"] = row["job_url"]
                known_pairs.add(key)
                new_rows.append(row)
                counters["new"] += 1
            page += 1

    if args.enrich:
        # Detail pages are always fetched now and already carry the enrich
        # fields; the flag is kept as a no-op for old cron lines.
        log.info("--enrich is a no-op: detail data is captured on every run")

    columns = CSV_COLUMNS
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if existing_df is not None:
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["source", "job_id"],
                                                keep="first")
        else:
            combined = new_df
        for col in columns:
            if col not in combined.columns:
                combined[col] = ""
        ordered = [c for c in columns if c in combined.columns] + \
                  [c for c in combined.columns if c not in columns]
        combined[ordered].to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        log.info("No new jobs; %s left unchanged", args.output)

    print("\n===== Run summary =====")
    print("Pages scanned:      {:>6,}".format(counters["pages"]))
    print("Listings seen:      {:>6,}".format(counters["listings"]))
    print("Page errors:        {:>6,}".format(counters["page_errors"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Excluded (deny-list title): {:>2,}".format(counters["excluded_deny_title"]))
    print("New jobs added:     {:>6,}".format(counters["new"]))
    print("Duplicates skipped: {:>6,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
