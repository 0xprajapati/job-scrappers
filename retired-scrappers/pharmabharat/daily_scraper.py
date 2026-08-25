#!/usr/bin/env python3
"""
PharmaBharat daily category scraper.

Pulls job posts for a chosen set of pharmabharat.com categories (by default the
ten non-clinical ones: CDM, Clinical Research, Medical Writer, TMF, Medical
Coding, Pharmacovigilance, Regulatory Affairs, Medical Reviewer, MSL,
HEOR/RWE) and writes them to CSV with the full job description.

Unlike scraper.py (whole-site -> 22-column club schema), this one:
  * filters server-side by category id,
  * reads the site's ACF custom fields (company/location/qualification/
    experience/salary/mode) instead of parsing facts back out of prose,
  * keeps the complete article body as readable text.

Incremental by default: each run resumes from the previous run's timestamp
(minus a grace window) and only reports posts that are new or edited since.

Outputs
-------
    jobs_csv/<DD-MM-YYYY>/pharmabharat_categories.csv   this run's new/updated,
                                                        club schema (job_samples.csv
                                                        + qualification, without
                                                        is_active / expires_at)
    pharmabharat_category_jobs.csv                      cumulative rich store

Usage
-----
    python daily_scraper.py                    # daily incremental run
    python daily_scraper.py --days 7           # last 7 days, ignore state
    python daily_scraper.py --since 2026-08-01
    python daily_scraper.py --categories tmf,medical-coding-jobs
    python daily_scraper.py --list-categories  # show every category slug
"""

import argparse
import csv
import errno
import fcntl
import html
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

# Field parsers shared with the whole-site scraper, so both pharmabharat CSVs
# normalise salary / experience / location the same way.
from scraper import (  # noqa: E402
    classify_company_type,
    classify_job_type,
    parse_experience,
    parse_location,
    parse_salary,
)

SITE_BASE = "https://pharmabharat.com"
POSTS_URL = SITE_BASE + "/wp-json/wp/v2/posts"
CATEGORIES_URL = SITE_BASE + "/wp-json/wp/v2/categories"

USER_AGENT = (
    "HealthCareersBot/1.0 (+https://healthcareers.club; daily job indexer; "
    "contact: eleswarapu.madhav@gmail.com)"
)

HERE = os.path.dirname(os.path.abspath(__file__))

# On a server the data usually belongs outside the code checkout (an EBS mount,
# a bind-mounted volume). PHARMABHARAT_DATA_DIR relocates every output at once;
# individual paths can still be overridden per-flag.
DATA_DIR = os.environ.get("PHARMABHARAT_DATA_DIR", HERE)

STATE_FILE = os.path.join(DATA_DIR, ".daily_state.json")
MASTER_CSV = os.path.join(DATA_DIR, "pharmabharat_category_jobs.csv")
LOCK_FILE = os.path.join(DATA_DIR, ".daily_scraper.lock")

# Per-run output follows the repo convention: jobs_csv/<DD-MM-YYYY>/<file>.csv,
# the same dated folders every other scraper in this repo writes into.
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
if os.environ.get("PHARMABHARAT_JOBS_CSV_DIR"):
    JOBS_CSV_DIR = os.environ["PHARMABHARAT_JOBS_CSV_DIR"]
elif os.environ.get("PHARMABHARAT_DATA_DIR"):
    # On a server the checkout is usually read-only, so keep jobs_csv beside
    # the rest of the scraped data instead of inside the code tree.
    JOBS_CSV_DIR = os.path.join(DATA_DIR, "jobs_csv")
else:
    JOBS_CSV_DIR = os.path.join(REPO_ROOT, "jobs_csv")

# scraper.py already owns "pharmabharat.csv" (the club 22-column export) in
# these same folders; this file must not overwrite it.
OUTPUT_BASENAME = "pharmabharat_categories.csv"

# The site's WP instance runs on Asia/Kolkata and its ?after= filter compares
# against post_date in SITE-LOCAL time (verified against the live API), not GMT.
# Pinning the window to IST makes a run behave identically whether the host is
# UTC (the EC2 default), IST, or anything else. India has no DST, so a fixed
# offset is correct year-round.
SITE_TZ = timezone(timedelta(hours=5, minutes=30))

# Exit codes, for cron/systemd alerting.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3

# Default target categories, by slug. Ids are resolved at runtime so a slug
# rename on the site surfaces as a clear error instead of silently scraping
# the wrong bucket.
DEFAULT_CATEGORIES = [
    "clinical-data-management-jobs",
    "clinical-research-jobs",
    "medical-writer-jobs",
    "tmf",
    "medical-coding-jobs",
    "pharmacovigilance-jobs",
    "regulatory-affairs-jobs",
    "medical-reviewer",
    "medical-science-liaison-jobs",
    "heor-rwe",
]

# Friendly labels for the columns; anything not listed falls back to the
# site's own category name.
CATEGORY_LABELS = {
    "clinical-data-management-jobs": "Clinical Data Management",
    "clinical-research-jobs": "Clinical Research",
    "medical-writer-jobs": "Medical Writer",
    "tmf": "TMF",
    "medical-coding-jobs": "Medical Coding",
    "pharmacovigilance-jobs": "Pharmacovigilance",
    "regulatory-affairs-jobs": "Regulatory Affairs",
    "medical-reviewer": "Medical Reviewer",
    "medical-science-liaison-jobs": "MSL",
    "heor-rwe": "HEOR",
}

COLUMNS = [
    "Post ID",
    "Category (matched)",
    "Date Posted",
    "Last Modified",
    "Job Title",
    "Company",
    "Position",
    "Location",
    "Qualification",
    "Experience",
    "Salary",
    "Mode of Application",
    "Apply Link(s)",
    "Contact Email(s)",
    "All Site Categories",
    "Job Post URL",
    "Full Description",
]

# Per-run output: the repo's job_samples.csv contract (see ../../README.md)
# plus "qualification", minus "is_active" / "expires_at".
CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience", "qualification",
    "min_salary", "max_salary", "salary_period", "salary_currency",
]

POST_FIELDS = "id,date,modified,link,slug,title,content,excerpt,categories,acf"

REQUEST_DELAY = 1.0          # seconds between requests (site etiquette)
BACKOFF_START = 3            # seconds, doubles per retry
MAX_RETRIES = 4
GRACE_HOURS = 48             # re-check window to catch backdated/edited posts

logger = logging.getLogger("pharmabharat.daily")


def log(msg):
    logger.info(msg)


def setup_logging(log_file=None, verbose=False, quiet=False):
    """stderr always (journald/CloudWatch pick it up); optional rotating-safe file."""
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%dT%H:%M:%S%z")
    if not quiet:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    if log_file:
        d = os.path.dirname(os.path.abspath(log_file))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    if quiet and not log_file:
        logger.addHandler(logging.NullHandler())


def site_now():
    """Current time in the site's timezone, regardless of the host's TZ."""
    return datetime.now(SITE_TZ)


class AlreadyRunning(Exception):
    pass


class SingleInstance(object):
    """flock guard so an overrunning cron job can never interleave writes."""

    def __init__(self, path, enabled=True):
        self.path = path
        self.enabled = enabled
        self.fh = None

    def __enter__(self):
        if not self.enabled:
            return self
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError as exc:
            self.fh.close()
            self.fh = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning(self.path)
            raise
        self.fh.write("%s\n" % os.getpid())
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None
        return False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def build_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def request_json(session, url, params=None):
    """GET returning (parsed_json, headers), with backoff on 429/5xx."""
    delay = BACKOFF_START
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = "HTTP %s" % r.status_code
                if attempt < MAX_RETRIES:
                    log("  %s -> retry in %ss" % (last_err, delay))
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError("giving up after %s: %s" % (attempt, last_err))
            r.raise_for_status()
            return r.json(), r.headers
        except requests.RequestException as exc:
            last_err = exc
            if attempt < MAX_RETRIES:
                log("  request error (%s) -> retry in %ss" % (exc, delay))
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable: %s" % last_err)


def fetch_categories(session):
    """{slug: {'id':…, 'name':…, 'count':…}} across all pages."""
    out = {}
    by_id = {}
    page = 1
    while True:
        data, headers = request_json(
            session, CATEGORIES_URL,
            {"per_page": 100, "page": page, "_fields": "id,name,slug,count"},
        )
        if not data:
            break
        for c in data:
            name = html.unescape(c["name"])
            out[c["slug"]] = {"id": c["id"], "name": name, "count": c["count"]}
            by_id[c["id"]] = name
        total_pages = int(headers.get("X-WP-TotalPages", 1) or 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return out, by_id


def fetch_posts(session, cat_id, after_iso):
    """All posts in a category published on/after after_iso (newest first)."""
    posts = []
    page = 1
    while True:
        params = {
            "categories": cat_id,
            "after": after_iso,
            "per_page": 100,
            "page": page,
            "orderby": "date",
            "order": "desc",
            "_fields": POST_FIELDS,
        }
        data, headers = request_json(session, POSTS_URL, params)
        if not data:
            break
        posts.extend(data)
        total_pages = int(headers.get("X-WP-TotalPages", 1) or 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return posts


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------
BLOCK_END = r"</(p|h1|h2|h3|h4|h5|h6|li|tr|div|blockquote|figcaption)>"

# Hosts that are the portal itself or its social channels -- never an employer
# apply page. Matched on the netloc only, so an apply URL carrying a
# "?source=Pharmabharat.com" tracking param is still kept.
SKIP_HOSTS = re.compile(
    r"(^|\.)(pharmabharat\.com|whatsapp\.com|t\.me|telegram\.me|telegram\.org|"
    r"instagram\.com|youtube\.com|youtu\.be|facebook\.com|twitter\.com|x\.com)$",
    re.I,
)


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").strip()


def html_to_text(c):
    """Article HTML -> readable plain text, keeping headings, bullets, links."""
    c = re.sub(r"<script.*?</script>", " ", c, flags=re.S | re.I)
    c = re.sub(r"<style.*?</style>", " ", c, flags=re.S | re.I)
    # Figures usually wrap images -- but this site puts its "Job Details"
    # table inside <figure class="wp-block-table">, which must survive.
    def _figure(m):
        return m.group(0) if re.search(r"<table", m.group(0), re.I) else " "
    c = re.sub(r"<figure.*?</figure>", _figure, c, flags=re.S | re.I)
    c = re.sub(r"<(img|source)[^>]*>", " ", c, flags=re.I)

    def _inline(m):
        url, label = html.unescape(m.group(1)).strip(), strip_tags(m.group(2))
        if url.startswith("#") or not label:
            return label
        return "%s [%s]" % (label, url)

    c = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', _inline, c, flags=re.S | re.I)
    c = re.sub(r"<h[1-6][^>]*>", "\n\n## ", c, flags=re.I)
    c = re.sub(r"<li[^>]*>", "\n- ", c, flags=re.I)
    c = re.sub(r"<br\s*/?>", "\n", c, flags=re.I)
    c = re.sub(r"<t[hd][^>]*>", " | ", c, flags=re.I)
    c = re.sub(BLOCK_END, "\n", c, flags=re.I)
    c = re.sub(r"<[^>]+>", "", c)
    c = html.unescape(c).replace("\xa0", " ")

    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in c.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            blank = True
            continue
        if out and blank:
            out.append("")
        blank = False
        out.append(ln)
    return "\n".join(out).strip()


def extract_links(c):
    links, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', c, flags=re.S | re.I):
        url = html.unescape(m.group(1)).strip()
        if url.startswith("#") or url.startswith("mailto:"):
            continue
        host = (urlparse(url).netloc or "").split("@")[-1].split(":")[0]
        if SKIP_HOSTS.search(host):
            continue
        if "linkedin.com" in host and re.search(r"/company/pharma", url, re.I):
            continue
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def extract_emails(c):
    found = re.findall(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", strip_tags(c)
    )
    out = []
    lowered = set()
    for e in found:
        if e.lower() not in lowered:
            lowered.add(e.lower())
            out.append(e)
    return out


def acf_field(acf, key):
    v = (acf or {}).get(key)
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return re.sub(r"\s+", " ", str(v)).strip()


def build_row(post, matched_labels, cat_names_by_id):
    acf = post.get("acf") or {}
    content = post["content"]["rendered"]
    all_cats = ", ".join(
        sorted(cat_names_by_id.get(c, str(c)) for c in post.get("categories", []))
    )
    return {
        "Post ID": str(post["id"]),
        "Category (matched)": " | ".join(sorted(matched_labels)),
        "Date Posted": post["date"][:10],
        "Last Modified": post.get("modified", "")[:19],
        "Job Title": strip_tags(post["title"]["rendered"]),
        "Company": acf_field(acf, "company_name"),
        "Position": acf_field(acf, "position_name"),
        "Location": acf_field(acf, "location"),
        "Qualification": acf_field(acf, "qualification"),
        "Experience": acf_field(acf, "experience"),
        "Salary": acf_field(acf, "salary"),
        "Mode of Application": acf_field(acf, "mode_of_inteview"),
        "Apply Link(s)": " ; ".join(extract_links(content)),
        "Contact Email(s)": " ; ".join(extract_emails(content)),
        "All Site Categories": all_cats,
        "Job Post URL": post["link"],
        "Full Description": html_to_text(content),
    }


def _int_str(v):
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return ""


def to_club_row(r):
    """Rich row -> CLUB_COLUMNS.

    title           = the site's ACF "position" (post title if empty)
    category        = the matched PharmaBharat category label(s)
    application_url = the post's Apply Link(s) (post URL if none)
    """
    post_title = r.get("Job Title", "")
    company = r.get("Company", "")
    city, country, code, dial = parse_location(r.get("Location", ""))
    min_exp, max_exp = parse_experience(r.get("Experience", ""))
    sal = parse_salary(r.get("Salary", ""))
    min_sal = _int_str(sal.get("salary_min"))
    has_salary = bool(min_sal) and sal.get("salary_currency") in ("INR", "USD")
    return {
        "country_name": country or "India",
        "country_code": code or "IN",
        "country_dial_code": dial or "+91",
        "city_name": city,
        "company_name": company or post_title,
        "company_type": classify_company_type(company, post_title),
        "company_logo": "",
        "company_about": "",
        "title": r.get("Position", "") or post_title,
        "description": r.get("Full Description", ""),
        "job_type": classify_job_type(r.get("Mode of Application", "")),
        "category": r.get("Category (matched)", ""),
        "application_url": r.get("Apply Link(s)", "") or r.get("Job Post URL", ""),
        "posted_at": r.get("Date Posted", ""),
        "min_experience": _int_str(min_exp),
        "max_experience": _int_str(max_exp),
        "qualification": r.get("Qualification", ""),
        "min_salary": min_sal if has_salary else "",
        "max_salary": _int_str(sal.get("salary_max")) if has_salary else "",
        "salary_period": sal.get("salary_period", "") if has_salary else "",
        "salary_currency": sal.get("salary_currency", "") if has_salary else "",
    }


def _day_key(club_row):
    return club_row.get("application_url", "") + "\n" + club_row.get("title", "")


# --------------------------------------------------------------------------
# state + csv
# --------------------------------------------------------------------------
def load_state(path=STATE_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def parse_state_time(raw):
    """last_run -> aware datetime in site tz. Older naive stamps are read as IST."""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SITE_TZ)
    return dt.astimezone(SITE_TZ)


def save_state(state, path=STATE_FILE):
    _atomic_write(path, lambda f: json.dump(state, f, indent=2))


def _atomic_write(path, writer, encoding=None, newline=None):
    """Write via temp file + rename.

    A run killed mid-write (spot-instance reclaim, OOM, deploy restart) must not
    leave a half-written master CSV behind; os.replace is atomic within a
    filesystem, so readers see either the old file or the complete new one.
    """
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def read_csv_rows(path):
    if not os.path.exists(path):
        return []
    csv.field_size_limit(10 ** 7)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, columns=COLUMNS):
    def _w(f):
        w = csv.DictWriter(f, fieldnames=columns, quoting=csv.QUOTE_ALL,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})

    _atomic_write(path, _w, encoding="utf-8-sig", newline="")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Daily category scraper for pharmabharat.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES),
                   help="comma-separated category slugs (default: the ten "
                        "non-clinical ones)")
    p.add_argument("--days", type=int, default=None, metavar="N",
                   help="scrape the last N days, ignoring saved state")
    p.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                   help="scrape posts published on/after this date")
    p.add_argument("--first-run-days", type=int, default=7, metavar="N",
                   help="window used when no state exists (default: 7)")
    p.add_argument("--master", default=MASTER_CSV,
                   help="cumulative CSV of every job ever seen")
    p.add_argument("--jobs-csv-dir", "--daily-dir", dest="jobs_csv_dir",
                   default=JOBS_CSV_DIR,
                   help="jobs_csv root; the run writes "
                        "<root>/<DD-MM-YYYY>/" + OUTPUT_BASENAME)
    p.add_argument("--run-date", default=None, metavar="DD-MM-YYYY",
                   help="label for this run's output file (default: today)")
    p.add_argument("--no-master", action="store_true",
                   help="do not read or update the cumulative CSV")
    p.add_argument("--no-state", action="store_true",
                   help="do not read or write .daily_state.json")
    p.add_argument("--list-categories", action="store_true",
                   help="print every category slug on the site and exit")
    p.add_argument("--state-file", default=STATE_FILE,
                   help="path to the run-state JSON")
    p.add_argument("--lock-file", default=LOCK_FILE,
                   help="path to the single-instance lock")
    p.add_argument("--no-lock", action="store_true",
                   help="skip the single-instance lock (not advised under cron)")
    p.add_argument("--log-file", default=os.environ.get("PHARMABHARAT_LOG_FILE"),
                   help="also append logs here")
    p.add_argument("--summary-json", default=None, metavar="PATH",
                   help="write run stats as JSON (for CloudWatch/monitoring)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--quiet", action="store_true", help="no stderr output")
    argv = p.parse_args(argv)

    setup_logging(argv.log_file, argv.verbose, argv.quiet)

    session = build_session()

    cat_index, cat_names_by_id = fetch_categories(session)

    if argv.list_categories:
        for slug in sorted(cat_index):
            info = cat_index[slug]
            print("%-40s id=%-5s posts=%-6s %s"
                  % (slug, info["id"], info["count"], info["name"]))
        return EXIT_OK

    wanted = [s.strip() for s in argv.categories.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in cat_index]
    if unknown:
        logger.error("unknown category slug(s): %s" % ", ".join(unknown))
        log("Run with --list-categories to see valid slugs.")
        return EXIT_CONFIG

    # ---- decide the window -------------------------------------------------
    state = {} if argv.no_state else load_state(argv.state_file)
    now = site_now()
    if argv.since:
        after_dt = datetime.strptime(argv.since, "%Y-%m-%d").replace(tzinfo=SITE_TZ)
        why = "--since %s" % argv.since
    elif argv.days is not None:
        after_dt = now - timedelta(days=argv.days)
        why = "--days %s" % argv.days
    elif state.get("last_run"):
        after_dt = parse_state_time(state["last_run"]) - timedelta(hours=GRACE_HOURS)
        why = "since last run (%s, minus %sh grace)" % (
            state["last_run"], GRACE_HOURS)
    else:
        after_dt = now - timedelta(days=argv.first_run_days)
        why = "first run, last %s days" % argv.first_run_days

    # The API compares ?after= against post_date in site-local time, so send a
    # naive site-local stamp -- never the host's clock and never UTC.
    after_iso = after_dt.astimezone(SITE_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    log("Host TZ %s | site time now %s (IST)"
        % (time.tzname[0], now.strftime("%Y-%m-%dT%H:%M:%S")))
    log("Window: posts after %s IST  (%s)" % (after_iso, why))

    # ---- fetch -------------------------------------------------------------
    posts = {}
    matched = {}
    for slug in wanted:
        cid = cat_index[slug]["id"]
        label = CATEGORY_LABELS.get(slug, cat_index[slug]["name"])
        found = fetch_posts(session, cid, after_iso)
        log("  %-34s %3d post(s)" % (slug, len(found)))
        for post in found:
            posts[post["id"]] = post
            matched.setdefault(post["id"], set()).add(label)
        time.sleep(REQUEST_DELAY)

    if not posts:
        log("No posts in window. Nothing to write.")
        if not argv.no_state:
            state["last_run"] = now.isoformat(timespec="seconds")
            save_state(state, argv.state_file)
        if argv.summary_json:
            write_summary(argv.summary_json, now, after_iso, 0, 0, 0,
                          state.get("total_known", 0), None)
        return EXIT_OK

    rows = [build_row(p_, matched[pid], cat_names_by_id)
            for pid, p_ in posts.items()]
    rows.sort(key=lambda r: (r["Date Posted"], r["Job Title"]), reverse=True)

    # ---- diff against the cumulative store ---------------------------------
    master_rows = [] if argv.no_master else read_csv_rows(argv.master)
    master_by_id = {r.get("Post ID"): r for r in master_rows if r.get("Post ID")}

    new_rows, updated_rows = [], []
    for r in rows:
        prev = master_by_id.get(r["Post ID"])
        if prev is None:
            new_rows.append(r)
        elif prev.get("Last Modified") != r["Last Modified"]:
            updated_rows.append(r)
        master_by_id[r["Post ID"]] = r

    changed = new_rows + updated_rows

    # ---- write -------------------------------------------------------------
    run_label = argv.run_date or now.strftime("%d-%m-%Y")
    daily_path = os.path.join(argv.jobs_csv_dir, run_label, OUTPUT_BASENAME)
    todays = changed if not argv.no_master else rows

    # Several runs a day are normal (cron retry, manual re-run). Merge into the
    # day's file rather than overwriting it, so a later run that finds nothing
    # new cannot wipe out what an earlier one collected. The day file is the
    # club schema (no Post ID), so application_url + title is the merge key.
    if todays or not os.path.exists(daily_path):
        existing = read_csv_rows(daily_path)
        day_by_key = {}
        for r in existing:
            if set(CLUB_COLUMNS) - set(r):
                continue  # file from before the schema change: rebuild it
            day_by_key[_day_key(r)] = r
        for r in todays:
            club = to_club_row(r)
            day_by_key[_day_key(club)] = club
        day_rows = list(day_by_key.values())
        day_rows.sort(key=lambda r: (r.get("posted_at", ""), r.get("title", "")),
                      reverse=True)
        write_csv(daily_path, day_rows, CLUB_COLUMNS)
    else:
        day_rows = read_csv_rows(daily_path)

    if not argv.no_master:
        merged = list(master_by_id.values())
        merged.sort(key=lambda r: (r.get("Date Posted", ""), r.get("Job Title", "")),
                    reverse=True)
        write_csv(argv.master, merged)

    if not argv.no_state:
        state["last_run"] = now.isoformat(timespec="seconds")
        state["total_known"] = len(master_by_id)
        save_state(state, argv.state_file)

    # ---- report ------------------------------------------------------------
    log("")
    log("Fetched in window : %d" % len(rows))
    log("New jobs          : %d" % len(new_rows))
    log("Updated jobs      : %d" % len(updated_rows))
    log("Run CSV           : %s  (%d row(s))" % (daily_path, len(day_rows)))
    if not argv.no_master:
        log("Master CSV        : %s  (%d total)" % (argv.master, len(master_by_id)))

    if argv.summary_json:
        write_summary(argv.summary_json, now, after_iso, len(rows),
                      len(new_rows), len(updated_rows), len(master_by_id),
                      daily_path)
    return EXIT_OK


def write_summary(path, now, after_iso, fetched, new, updated, total, daily_path):
    _atomic_write(path, lambda f: json.dump({
        "run_at": now.isoformat(timespec="seconds"),
        "window_after_ist": after_iso,
        "fetched_in_window": fetched,
        "new_jobs": new,
        "updated_jobs": updated,
        "total_known": total,
        "run_csv": daily_path,
    }, f, indent=2))


def cli(argv=None):
    """Wrapper owning the lock and turning any crash into a clean exit code."""
    lock_path = LOCK_FILE
    use_lock = True
    raw = sys.argv[1:] if argv is None else list(argv)
    if "--no-lock" in raw:
        use_lock = False
    if "--lock-file" in raw:
        try:
            lock_path = raw[raw.index("--lock-file") + 1]
        except IndexError:
            pass
    if "--list-categories" in raw or "-h" in raw or "--help" in raw:
        use_lock = False

    try:
        with SingleInstance(lock_path, enabled=use_lock):
            return main(argv)
    except AlreadyRunning as exc:
        setup_logging()
        logger.error("Another run holds %s -- exiting without doing work." % exc)
        return EXIT_LOCKED
    except KeyboardInterrupt:
        logger.error("Interrupted.")
        return EXIT_ERROR
    except SystemExit:
        raise
    except Exception:
        setup_logging()
        logger.error("Run failed:\n%s" % traceback.format_exc())
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(cli())
