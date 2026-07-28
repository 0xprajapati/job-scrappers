#!/usr/bin/env python3
"""Scrape healthcare job listings from dubai.dubizzle.com.

Data source
-----------
dubizzle Dubai's jobs vertical has a healthcare category listing (source-side
healthcare filter, master spec §2):

    https://dubai.dubizzle.com/jobs/medical-healthcare/          (?page=N)

Every listing page is server-rendered and embeds the full Algolia result set
in <script id="__NEXT_DATA__"> (spec preference #2), under the redux action
`listings/fetchListingDataForQuery/fulfilled`:

    payload.hits[]     — one object per ad: name (title), uuid (dedup key),
                         created_at (epoch secs), absolute_url (the public
                         detail URL, which also embeds the posted date),
                         location_list, category slugs and details_v2 with
                         Monthly Salary (AED bucket, e.g. "4,000 - 5,999"),
                         Employment Type, Remote Job, Minimum Work
                         Experience, Minimum Education Level, Industry,
                         Company Name (often "Confidential"), Benefits.
    payload.pagination — {page, totalPages, hitsPerPage, totalHits}

Detail pages (used by --enrich) embed `listings/detailRequest/fulfilled`
whose payload.listing carries the full plain-text `description`.

Why Playwright (headed)
-----------------------
The whole site sits behind Imperva/Incapsula Advanced Bot Protection.
Plain requests, curl_cffi Chrome impersonation AND headless Chromium all
receive the challenge interstitial; only a real (headed) Chromium session
passes. Per master spec §1 a headless/automated browser is the last resort —
verified to be the only working option here. The scraper therefore drives a
headed Playwright Chromium with a persistent profile dir (.pw_profile/) so
the Imperva cookies are reused across daily runs. Volume is tiny (the
category is currently a single page of ~12 ads), so one visible browser
window for a few seconds per day is the whole cost. `--headless` exists to
re-test headless mode should Imperva relax.

robots.txt (checked at startup): /jobs/ paths are allowed; the disallowed
/api/ endpoints are never called — everything comes from the public HTML.

Salary (master spec §3: capture, don't filter)
----------------------------------------------
Salaries are AED-per-month range buckets ("4,000 - 5,999"). The rich CSV
stores the verbatim bucket plus parsed min/max. The club CSV's
salary_currency enum only allows INR/USD, so its salary fields stay BLANK
(same decision as the dubailivejobs scraper) — no currency is invented.

Quirks
------
* The listing is NOT strictly date-sorted (highlighted ads first), so every
  run scans all pages and applies the time-window cutoff per job.
* Ads can stay live for years; the cutoff (not the listing) bounds the crawl.
* company_name is usually "Confidential" (hide_company_name=True).

Outputs (per the repo README + ../../instructions/master-scraper-spec.md)
-------------------------------------------------------------------------
* dubizzle_jobs.csv                          — rich cumulative store
                                               (dedup key: uuid)
* ../../jobs_csv/<DD-MM-YYYY>/dubizzle.csv   — HealthCareers.club 22-col schema
* needs_review.csv                           — titles with no healthcare signal

Time window: first run keeps the last INITIAL_WINDOW_DAYS; later runs keep
only jobs newer than the newest stored posted_date minus WATERMARK_GRACE_DAYS.

Run `python scraper.py --help` for options.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.robotparser
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "dubizzle"
SITE_BASE = "https://dubai.dubizzle.com"
ROBOTS_URL = SITE_BASE + "/robots.txt"
LISTING_URL = SITE_BASE + "/jobs/medical-healthcare/"

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# No salary filter (master spec §3): salaries captured, never filtered on.
INITIAL_WINDOW_DAYS = 30
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.5
PAGE_LOAD_TIMEOUT_MS = 60_000
CHALLENGE_WAIT_SECONDS = 6      # per poll while Imperva resolves
CHALLENGE_MAX_POLLS = 10
MAX_RETRIES = 3
MAX_EMPTY_PAGES = 3
MAX_PAGES_SAFETY = 40
DESCRIPTION_MAX_CHARS = 3_000

LOCAL_TZ = ZoneInfo("Asia/Dubai")   # created_at epochs -> site-local dates

SCRIPT_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRIPT_DIR / "dubizzle_jobs.csv")
REVIEW_CSV = str(SCRIPT_DIR / "needs_review.csv")
PROFILE_DIR = str(SCRIPT_DIR / ".pw_profile")
CLUB_CSV_DIR = SCRIPT_DIR.parents[1] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "internal_id", "title", "company", "city", "country",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "salary_currency", "job_type", "remote_job",
    "experience_raw", "experience_min_years", "experience_max_years",
    "education", "industry", "benefits", "gender", "company_size",
    "subcategory", "category", "company_type", "needs_review",
    "posted_date", "description", "job_url", "scraped_at",
]

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

log = logging.getLogger("dubizzle_scraper")

# ----------------------------------------------------------------------------
# Parsing helpers (pure functions — unit-tested in test_filters.py)
# ----------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", str(text or "")).strip()


def en(value):
    """dubizzle bilingual fields are {"en": ..., "ar": ...}; take English."""
    if isinstance(value, dict):
        value = value.get("en", "")
    return clean_text(value)


_NUM_RE = re.compile(r"\d[\d,]*")


def parse_salary_bucket(raw):
    """dubizzle "Monthly Salary" bucket -> (raw_str, min, max) in AED/month.

    Real values: "4,000 - 5,999", "2,000 - 3,999", "Unpaid", "Negotiable",
    or missing. Buckets are always monthly AED. Returns
    ("Not Disclosed", "", "") when there is no numeric range — never invents.
    """
    raw = clean_text(raw)
    numbers = [int(n.replace(",", "")) for n in _NUM_RE.findall(raw)]
    if not raw or not numbers:
        return ("Not Disclosed", "", "")
    lo = numbers[0]
    hi = numbers[1] if len(numbers) > 1 else lo
    if hi < lo:
        lo, hi = hi, lo
    return ("AED {} per month".format(raw), lo, hi)


_EXP_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*Year", re.IGNORECASE)
_EXP_PLUS_RE = re.compile(r"(\d+)\s*\+\s*Year", re.IGNORECASE)


def parse_experience(raw):
    """"1-2 Years" -> (1, 2); "5+ Years" -> (5, ""); "" -> ("", "")."""
    raw = clean_text(raw)
    m = _EXP_RANGE_RE.search(raw)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _EXP_PLUS_RE.search(raw)
    if m:
        return (int(m.group(1)), "")
    if re.search(r"fresh|no experience", raw, re.IGNORECASE):
        return (0, "")
    return ("", "")


_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|caregiver|care ?giver", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|\bgp\b|medical officer|"
    r"[a-z]+ologist|psychiatrist|p(a?)ediatrician|specialist doctor",
    re.IGNORECASE)

_HEALTHCARE_SIGNAL_RE = re.compile(
    r"medical|pharma|health|nurs|doctor|clinic|hospital|\blab\b|laborator|"
    r"diagnost|patient|dental|dh[ac]\b|moh\b|surgi|therap|physio|radiol|"
    r"patholog|wellness|care|midwif|wound|vaccin|optom|dermat", re.IGNORECASE)


def classify_category(title, subcategory="", industry=""):
    """Club enum doctors|nurses|pharmacists|non_clinical + needs_review flag.

    Everything scraped comes from the Medical/Healthcare category, so the
    source filter has already run; the classifier only picks the club bucket.
    A title with no healthcare word in title+subcategory+industry is still
    KEPT but flagged needs_review (master spec §2 — never silently dropped).
    """
    title = title or ""
    if _NURSE_RE.search(title):
        category = "nurses"
    elif _PHARMACIST_RE.search(title):
        category = "pharmacists"
    elif _DOCTOR_RE.search(title):
        category = "doctors"
    else:
        category = "non_clinical"
    haystack = " ".join(filter(None, [title, subcategory, industry]))
    needs_review = (category == "non_clinical"
                    and not _HEALTHCARE_SIGNAL_RE.search(haystack))
    return category, needs_review


_PHARMA_RE = re.compile(
    r"pharma|life ?science|biotech|laborator|diagnost|medical device|"
    r"med.?tech|vaccin", re.IGNORECASE)


def classify_company_type(company, industry, title):
    haystack = " ".join(filter(None, [company, industry, title]))
    return "pharma" if _PHARMA_RE.search(haystack) else "hospital"


def job_type_from(employment_type, remote_job):
    if clean_text(remote_job).lower() == "yes":
        return "remote"
    et = clean_text(employment_type).lower()
    if "part" in et:
        return "part_time"
    return "full_time"


def epoch_to_local_date(epoch):
    """created_at epoch seconds -> YYYY-MM-DD in the site's timezone."""
    try:
        epoch = int(epoch)
    except (TypeError, ValueError):
        return ""
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, tz=LOCAL_TZ).strftime("%Y-%m-%d")


def details_map(hit):
    """Flatten details_v2 primary/secondary/tertiary into {slug: en-value}."""
    out = {}
    details = hit.get("details_v2") or {}
    for tier in ("primary", "secondary", "tertiary"):
        for item in details.get(tier) or []:
            slug = clean_text(item.get("slug"))
            if slug and slug not in out:
                out[slug] = en(item.get("value"))
    return out


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None):
    """First run: last INITIAL_WINDOW_DAYS; later: watermark minus grace."""
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max()
                    - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# __NEXT_DATA__ extraction (pure)
# ----------------------------------------------------------------------------

def extract_listing_payload(next_data):
    """__NEXT_DATA__ dict -> {"hits": [...], "pagination": {...}} or None."""
    try:
        actions = next_data["props"]["pageProps"]["reduxWrapperActionsGIPP"]
    except (KeyError, TypeError):
        return None
    for action in actions:
        if str(action.get("type", "")).endswith(
                "listings/fetchListingDataForQuery/fulfilled"):
            payload = action.get("payload") or {}
            if isinstance(payload.get("hits"), list):
                return payload
    return None


def extract_detail_listing(next_data):
    """__NEXT_DATA__ dict of a detail page -> payload.listing dict or {}."""
    try:
        actions = next_data["props"]["pageProps"]["reduxWrapperActionsGIPP"]
    except (KeyError, TypeError):
        return {}
    for action in actions:
        if str(action.get("type", "")).endswith("listings/detailRequest/fulfilled"):
            listing = (action.get("payload") or {}).get("listing")
            if isinstance(listing, dict):
                return listing
    return {}


def build_row(hit, now_utc=None):
    """One Algolia hit -> one rich-CSV row dict."""
    d = details_map(hit)
    title = en(hit.get("name"))
    slugs = (hit.get("category") or {}).get("slug") or []
    subcategory = clean_text(slugs[-1] if slugs else "").replace("-", " ")
    industry = d.get("industry", "")
    category, needs_review = classify_category(title, subcategory, industry)

    salary_raw, sal_min, sal_max = parse_salary_bucket(d.get("salary"))
    exp_min, exp_max = parse_experience(d.get("required_work_experience"))

    company = clean_text(d.get("company_name"))
    if not company or d.get("hide_company_name", "").lower() == "true":
        company = "Confidential"

    locations = (hit.get("location_list") or {}).get("en") or []
    city = clean_text(locations[-1] if locations else "Dubai")

    url = en(hit.get("absolute_url"))
    posted = epoch_to_local_date(hit.get("created_at") or hit.get("added"))
    now_utc = now_utc or datetime.now(timezone.utc)
    return {
        "source": SITE,
        "job_id": clean_text(hit.get("uuid")) or url,
        "internal_id": clean_text(hit.get("id")),
        "title": title,
        "company": company,
        "city": city or "Dubai",
        "country": "United Arab Emirates",
        "salary_raw": salary_raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": "per_month" if sal_min != "" else "",
        "salary_currency": "AED" if sal_min != "" else "",
        "job_type": job_type_from(d.get("required_commitment"),
                                  d.get("remote_job")),
        "remote_job": d.get("remote_job", ""),
        "experience_raw": d.get("required_work_experience", ""),
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "education": d.get("required_education_level", ""),
        "industry": industry,
        "benefits": d.get("benefits", ""),
        "gender": d.get("gender", ""),
        "company_size": d.get("company_size", ""),
        "subcategory": subcategory,
        "category": category,
        "company_type": classify_company_type(company, industry, title),
        "needs_review": needs_review,
        "posted_date": posted,
        "description": "",   # filled by --enrich from the detail page
        "job_url": url,
        "scraped_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----------------------------------------------------------------------------
# Club-schema conversion
# ----------------------------------------------------------------------------

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
    # AED cannot be represented by the club salary_currency enum (INR/USD),
    # so club salary fields stay blank; the AED figures live in the rich CSV.
    description = _blank(r.get("description"))
    if not description:
        bits = [_blank(r.get("title"))]
        if _blank(r.get("experience_raw")):
            bits.append("Experience: " + _blank(r.get("experience_raw")))
        if _blank(r.get("education")):
            bits.append("Education: " + _blank(r.get("education")))
        if _blank(r.get("salary_raw")) not in ("", "Not Disclosed"):
            bits.append("Salary: " + _blank(r.get("salary_raw")))
        if _blank(r.get("benefits")):
            bits.append("Benefits: " + _blank(r.get("benefits")))
        description = ". ".join(bits)
    return {
        "country_name": "United Arab Emirates",
        "country_code": "AE",
        "country_dial_code": "+971",
        "city_name": _blank(r.get("city")) or "Dubai",
        "company_name": _blank(r.get("company")) or "Confidential",
        "company_type": _blank(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": description,
        "job_type": _blank(r.get("job_type")) or "full_time",
        "category": _blank(r.get("category")) or "non_clinical",
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _int_str(r.get("experience_min_years")),
        "max_experience": _int_str(r.get("experience_max_years")),
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
        "is_active": "true",
        "expires_at": "",
    }


# ----------------------------------------------------------------------------
# Browser layer (Playwright, headed — see module docstring)
# ----------------------------------------------------------------------------

def check_robots():
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        req = urllib.request.Request(ROBOTS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            rp.parse(resp.read().decode("utf-8", "replace").splitlines())
    except OSError as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LISTING_URL, LISTING_URL + "?page=2"):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


class Browser:
    """Thin Playwright wrapper: goto URL -> parsed __NEXT_DATA__ dict."""

    def __init__(self, headless=False):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="chromium",           # full build; headless-shell is blocked
            headless=headless,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self):
        try:
            self._ctx.close()
        finally:
            self._pw.stop()

    def fetch_next_data(self, url):
        """Load url, wait out the Imperva challenge, return __NEXT_DATA__."""
        for attempt in range(MAX_RETRIES):
            try:
                self._page.goto(url, wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT_MS)
                for poll in range(CHALLENGE_MAX_POLLS):
                    content = self._page.evaluate(
                        "() => {const s = document.getElementById('__NEXT_DATA__');"
                        " return s ? s.textContent : null;}")
                    if content:
                        time.sleep(REQUEST_DELAY_SECONDS)
                        return json.loads(content)
                    time.sleep(CHALLENGE_WAIT_SECONDS)
                    if poll in (3, 6):    # challenge sometimes needs a reload
                        self._page.goto(url, wait_until="domcontentloaded",
                                        timeout=PAGE_LOAD_TIMEOUT_MS)
                log.warning("Challenge never cleared for %s (attempt %d/%d)",
                            url, attempt + 1, MAX_RETRIES)
            except Exception as exc:
                log.warning("Load failed for %s (attempt %d/%d): %s",
                            url, attempt + 1, MAX_RETRIES, exc)
                time.sleep(3 * (2 ** attempt))
        return None


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
        description="Scrape healthcare jobs from dubai.dubizzle.com.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop after N listing pages (test runs)")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch each NEW job's detail page for the full "
                             "description (1 extra page load per job)")
    parser.add_argument("--headless", action="store_true",
                        help="try headless Chromium (currently blocked by "
                             "Imperva; headed is the working default)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    check_robots()

    existing_df = load_existing(args.output)
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    cutoff = compute_cutoff(existing_df)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_old": 0, "needs_review": 0,
                "new": 0, "duplicates": 0}
    new_rows, review_log = [], []

    browser = Browser(headless=args.headless)
    try:
        page_num, total_pages, empty_pages = 1, 1, 0
        # Listing is not date-sorted (highlighted ads first): scan all pages,
        # apply the cutoff per job. The category is currently a single page.
        while page_num <= min(total_pages, MAX_PAGES_SAFETY):
            if args.max_pages is not None and page_num > args.max_pages:
                break
            url = LISTING_URL if page_num == 1 else (
                LISTING_URL + "?page={}".format(page_num))
            payload = extract_listing_payload(browser.fetch_next_data(url) or {})
            page_num += 1
            if not payload:
                empty_pages += 1
                if empty_pages >= MAX_EMPTY_PAGES:
                    log.error("%d consecutive unparseable pages — stopping",
                              empty_pages)
                    break
                continue
            empty_pages = 0
            pagination = payload.get("pagination") or {}
            total_pages = int(pagination.get("totalPages") or total_pages)

            for hit in payload["hits"]:
                counters["scanned"] += 1
                try:
                    row = build_row(hit)
                except Exception as exc:
                    log.warning("Skipping malformed hit: %s", exc)
                    continue
                if not row["job_id"] or not row["title"]:
                    log.warning("Skipping hit without id/title: %s", row["job_url"])
                    continue
                if row["job_id"] in known_ids:
                    counters["duplicates"] += 1
                    continue
                if row["posted_date"] and row["posted_date"] < cutoff:
                    counters["excluded_old"] += 1
                    continue
                if row["needs_review"]:
                    counters["needs_review"] += 1
                    review_log.append({"job_id": row["job_id"],
                                       "title": row["title"],
                                       "subcategory": row["subcategory"]})
                known_ids.add(row["job_id"])
                new_rows.append(row)
                counters["new"] += 1

        if args.enrich:
            for row in new_rows:
                listing = extract_detail_listing(
                    browser.fetch_next_data(row["job_url"]) or {})
                description = clean_text(listing.get("description"))
                if description:
                    row["description"] = description[:DESCRIPTION_MAX_CHARS]
                else:
                    log.warning("No description found for %s", row["job_url"])
    finally:
        browser.close()

    # ---- rich cumulative CSV ----
    if new_rows:
        new_df = pd.DataFrame(new_rows).astype(str)
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
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    # ---- needs-review sidecar ----
    if review_log:
        review_df = pd.DataFrame(review_log)
        try:
            prev = pd.read_csv(REVIEW_CSV, dtype=str)
            review_df = pd.concat([prev, review_df], ignore_index=True)
        except FileNotFoundError:
            pass
        review_df.drop_duplicates(subset="job_id").to_csv(REVIEW_CSV, index=False)

    print("\n===== Run summary =====")
    print("Jobs scanned:            {:>5,}".format(counters["scanned"]))
    print("Excluded (older than {}): {:>3,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:    {:>5,}".format(counters["needs_review"]))
    print("New jobs added:          {:>5,}".format(counters["new"]))
    print("Duplicates skipped:      {:>5,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
