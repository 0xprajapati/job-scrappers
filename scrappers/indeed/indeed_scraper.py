#!/usr/bin/env python3
"""Scrape REMOTE healthcare job listings (India) from in.indeed.com.

How Indeed can (and cannot) be scraped
--------------------------------------
Indeed sits behind Cloudflare bot protection: every plain-HTTP client —
requests, curl, any script UA or browser UA — receives HTTP 403 with a
challenge page. There is NO open JSON API. The only working source is the
structured job-card model Indeed embeds in every search page for its own
React frontend:

    window.mosaic.providerData["mosaic-provider-jobcards"]
        .metaData.mosaicProviderJobCardsModel.results   -> [15 job cards]

Each card carries: jobkey, title, company, companyRating, formattedLocation,
remoteWorkModel.type (REMOTE_ALWAYS/...), taxonomyAttributes (job types,
benefits, "Remote" flag), salarySnippet.text + extractedSalary {min, max,
type MONTHLY/YEARLY/HOURLY/DAILY/WEEKLY}, pubDate (epoch ms),
formattedRelativeTime, snippet (short HTML description).

robots.txt (verified 2026-07-29) — the `User-agent: *` and the explicit
Claude/AI-bot groups get the SAME rules: search pages `/jobs?q=...` are
ALLOWED, but `/viewjob?` (detail pages), `/rc/`, `/graphql`, `/rss` and —
critically — all pagination (`Disallow: /*&start=`) are DISALLOWED. This
scraper therefore reads ONLY page 1 of each search query and never opens
detail pages; descriptions are the card snippets. `viewjob` URLs are still
written as application_url — that link is for the human applicant, it is
never fetched here.

Because of the 403 wall, this script does not fetch the site itself. It
ingests the embedded JSON obtained through a real browser session, in either
form:

  --from-html FILE/DIR ...   search pages saved from a browser (Cmd+S,
                             "Webpage, Complete" or single HTML). The mosaic
                             JSON is cut straight out of the page source.
  --from-json FILE ...       the compact card-array format produced by the
                             capture snippet in README.md (keys k,t,c,cr,
                             loc,rw,jt,sal,mn,mx,st,pd,rel,sn,ben,q).

Run `python indeed_scraper.py --help` for options. See README.md for the
per-query capture workflow and the DevTools snippet.

Query strategy (why multiple queries)
-------------------------------------
No pagination means max ~15 organic cards per query, so coverage comes from
breadth: one page each of QUERIES (healthcare, nurse, doctor, physician,
pharmacist, ...) on https://in.indeed.com/jobs?q=<q>&l=Remote, then dedup by
jobkey. l=Remote on in.indeed.com == remote-within-India listings.

Classification (shared two-level taxonomy)
------------------------------------------
Every candidate card goes through the shared classifier
`classify_job(title, skills, description)` from `_shared/classification.py`
(the ONLY categorization allowed — no per-scraper keyword lists). The card's
taxonomy-attribute labels (job types + benefits) travel as the `skills`
signal and the SERP snippet as `description`:

* `in_scope` False -> dropped, counted excluded_out_of_scope.
* in scope -> `category` ("Non Clinical" | "Public Health") and
  `sub_category` (one of the 20 splits) plus the role_family/score trace
  columns in the rich CSV.
* `needs_review` True -> kept AND appended to needs_review.csv (in-scope
  but the title looks like a different profession) — never silently
  dropped.

Remote-only gate: a card is remote when remoteWorkModel is REMOTE_* or
formattedLocation == "Remote" (city + REMOTE_ALWAYS means "remote, employer
in <city>", which counts). The l=Remote SERP occasionally pads in a
non-remote card ("expanded" result); those are excluded_not_remote.

Salary (master spec §3: capture, don't filter)
----------------------------------------------
extractedSalary is stored verbatim in the rich CSV (INR; min==-1 or max==-1
means an open "From X"/"Up to X" range). The club schema only expresses
INR/USD per_month/per_annum, so hourly/daily/weekly rates stay rich-only.
Never invented, never a reason to exclude.

Dates: pubDate is epoch ms pinned to ~05:00 UTC (US-Eastern midnight);
the UTC calendar date matches Indeed's own "N days ago" label, so dates are
taken in UTC — do NOT localize to IST (that can roll "just posted" into
tomorrow's date).

Outputs (repo README + instructions/master-scraper-spec.md)
-----------------------------------------------------------
* indeed_jobs.csv        — rich cumulative store (dedup key: jobkey),
                           watermark source for incremental runs.
* ../../jobs_csv/<DD-MM-YYYY>/indeed.csv
                         — HealthCareers.club 23-column schema.
* needs_review.csv       — in-scope rows whose title the classifier flags.

Time window: first run keeps jobs posted in the last INITIAL_WINDOW_DAYS
(30 — Indeed page-1 listings are all currently-active postings, so the
spec's 7-day feed window would discard live jobs; --window-days overrides).
Later runs keep jobs newer than (newest stored posted_date -
WATERMARK_GRACE_DAYS).
"""

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "indeed"
SITE_BASE = "https://in.indeed.com"
VIEWJOB_URL = SITE_BASE + "/viewjob?jk={}"   # written for humans, never fetched

# One SERP each (page 1 only — robots.txt forbids &start= pagination).
QUERIES = [
    "healthcare", "nurse", "doctor", "physician", "pharmacist",
    "medical coder", "medical billing", "clinical research",
    "pharmacovigilance", "medical writer", "telemedicine", "dietitian",
    "psychologist", "physiotherapist", "counsellor",
    "medical transcriptionist",
    # 2026-08-25 role-family widening (fetch wide, filter at the gate).
    # No pagination means each extra query costs exactly one allowed SERP
    # load in the capture; these cover the eleven-family scope the original
    # broad-healthcare list barely touched.
    "regulatory affairs", "clinical data management", "drug safety",
    "medical affairs", "medical science liaison", "clinical trials",
    "health economics", "market access", "trial master file",
    "public health", "epidemiology",
    # 2026-08-25 Public Health widening: cover all ten PH sub-categories,
    # mirroring the shine_roles list (terms proven to carry PH density on
    # Indian boards). Each query costs one allowed SERP load in the capture.
    "epidemiologist", "disease surveillance",
    "public health program",
    "monitoring and evaluation",
    "community health officer", "asha",
    "health educator", "health promotion",
    "tuberculosis", "hiv", "malaria", "immunization", "vaccination",
    "public health nutrition", "nutritionist",
    "infection control",
    "health informatics", "hmis",
    "public health research",
]

INITIAL_WINDOW_DAYS = 30       # page-1 cards are live posts; see docstring
WATERMARK_GRACE_DAYS = 2
DESCRIPTION_MAX_CHARS = 3_000

RICH_CSV = str(Path(__file__).resolve().parent / "indeed_jobs.csv")
NEEDS_REVIEW_CSV = str(Path(__file__).resolve().parent / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "company_rating", "location",
    "salary_raw", "salary_min", "salary_max", "salary_currency",
    "salary_period", "employment_types", "work_mode", "benefits", "category",
    "sub_category", "role_family", "all_families", "family_scores",
    "family_confidence", "matched_in", "needs_review",
    "company_type", "search_query", "posted_date", "relative_time",
    "description", "job_url", "scraped_at",
]

# extractedSalary.type -> normalized period. Club enums only allow
# per_month/per_annum; the rest stay rich-CSV-only.
SALARY_PERIODS = {"MONTHLY": "per_month", "YEARLY": "per_annum",
                  "HOURLY": "per_hour", "DAILY": "per_day",
                  "WEEKLY": "per_week"}

log = logging.getLogger("indeed_scraper")

# ----------------------------------------------------------------------------
# Classification — all through the shared taxonomy engine
# ----------------------------------------------------------------------------

def card_skills(card):
    """Curated taxonomy-attribute labels of a card -> `skills` signal.

    Indeed's SERP model exposes taxonomy attributes (job types, benefits)
    per card; they are the closest thing to a curated skills/attributes
    field this source has, so they travel in classify_job's `skills` slot.
    """
    parts = []
    for key in ("jt", "ben"):
        raw = str(card.get(key) or "")
        parts.extend(p.strip() for p in raw.split("|") if p.strip())
    return " | ".join(parts)


def classify_card(card):
    """Run the shared classifier over one compact SERP card.

    Returns the classify_job verdict dict; in_scope False means the card
    must be DROPPED (counted excluded_out_of_scope).
    """
    return classify_job(str(card.get("t") or ""), card_skills(card),
                        str(card.get("sn") or ""))


_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical|drug|medic(?:al|ine)s? ?(?:corp|inc|ltd)|"
    r"iqvia|parexel|syneos|quanticate|soterius|pfizer|thermo ?fisher",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    text = _TAG_RE.sub(" ", text or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def epoch_ms_to_date(value):
    """1784523600000 -> "2026-07-20" (UTC — matches Indeed's 'N days ago')."""
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000.0, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def parse_salary(mn, mx, stype):
    """extractedSalary -> (raw, lo, hi, currency, period). -1 = open end.

    (15000, 20000, "MONTHLY") -> ("INR 15000 - 20000 per month",
                                  "15000", "20000", "INR", "per_month")
    (21000, -1, "MONTHLY")    -> ("INR from 21000 per month",
                                  "21000", "", "INR", "per_month")
    """
    def as_int(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f <= 0:                       # -1 open range / junk
            return None
        return int(round(f))

    lo, hi = as_int(mn), as_int(mx)
    period = SALARY_PERIODS.get((stype or "").strip().upper(), "")
    if lo is None and hi is None:
        return ("Not Disclosed", "", "", "", "")
    label = period.replace("_", " ") if period else ""
    if lo is not None and hi is not None:
        if hi < lo:
            lo, hi = hi, lo
        raw = "INR {} - {}{}".format(lo, hi, " " + label if label else "")
        return (raw, str(lo), str(hi), "INR", period)
    if lo is not None:
        raw = "INR from {}{}".format(lo, " " + label if label else "")
        return (raw, str(lo), "", "INR", period)
    raw = "INR up to {}{}".format(hi, " " + label if label else "")
    return (raw, "", str(hi), "INR", period)


def is_remote(card):
    """REMOTE_* model or an explicit Remote location/attribute."""
    rw = (card.get("rw") or "").upper()
    if rw.startswith("REMOTE"):
        return True
    return (card.get("loc") or "").strip().lower() == "remote"


# ----------------------------------------------------------------------------
# Input decoding: saved HTML pages / compact JSON captures
# ----------------------------------------------------------------------------

_MOSAIC_RE = re.compile(
    r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*')


def _balanced_json(text, start):
    """Return the {...} object starting at text[start] (brace matching)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unbalanced JSON braces")


def cards_from_html(html_text, source_name=""):
    """Extract mosaic results from a saved SERP page -> compact card dicts."""
    m = _MOSAIC_RE.search(html_text)
    if not m:
        log.warning("%s: no mosaic-provider-jobcards payload found", source_name)
        return []
    payload = json.loads(_balanced_json(html_text, m.end()))
    results = (payload.get("metaData", {})
               .get("mosaicProviderJobCardsModel", {})
               .get("results", []))
    qm = re.search(r'"searchQuery"\s*:\s*"([^"]*)"', html_text)
    query = qm.group(1) if qm else source_name
    cards = []
    for j in results:
        taxo = {t.get("label"): [a.get("label") for a in t.get("attributes", [])]
                for t in j.get("taxonomyAttributes") or []}
        ex = j.get("extractedSalary") or {}
        cards.append({
            "k": j.get("jobkey"),
            "t": strip_html(j.get("displayTitle") or j.get("title")),
            "c": j.get("company"),
            "cr": j.get("companyRating") or "",
            "loc": j.get("formattedLocation"),
            "rw": (j.get("remoteWorkModel") or {}).get("type", ""),
            "jt": "|".join(taxo.get("job-types-cc") or j.get("jobTypes") or []),
            "sal": strip_html((j.get("salarySnippet") or {}).get("text", "")),
            "mn": ex.get("min", ""), "mx": ex.get("max", ""),
            "st": ex.get("type", ""),
            "pd": j.get("pubDate"), "rel": j.get("formattedRelativeTime"),
            "sn": strip_html(j.get("snippet")),
            "ben": "|".join(taxo.get("benefits") or []),
            "q": query,
        })
    return cards


def load_cards(json_paths, html_paths):
    cards = []
    for path in json_paths:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):            # {"q":..., "jobs":[...]} form
            data = data.get("jobs", [])
        log.info("%s: %d cards", path, len(data))
        cards.extend(data)
    for path in html_paths:
        p = Path(path)
        files = sorted(p.glob("*.htm*")) if p.is_dir() else [p]
        for f in files:
            got = cards_from_html(f.read_text(encoding="utf-8",
                                              errors="replace"), f.name)
            log.info("%s: %d cards", f.name, len(got))
            cards.extend(got)
    return cards


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none") else text


def card_to_rich_row(card, verdict):
    """Compact card + in-scope classify_job verdict -> rich CSV row."""
    title = _clean(card.get("t"))
    raw, lo, hi, cur, period = parse_salary(card.get("mn"), card.get("mx"),
                                            card.get("st"))
    jobkey = _clean(card.get("k"))
    return {
        "source": SITE,
        "job_id": jobkey,
        "title": title,
        "company": _clean(card.get("c")),
        "company_rating": _clean(card.get("cr")),
        "location": _clean(card.get("loc")),
        "salary_raw": _clean(card.get("sal")) or raw,
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": cur,
        "salary_period": period,
        "employment_types": _clean(card.get("jt")),
        "work_mode": "remote",
        "benefits": _clean(card.get("ben")),
        "category": verdict["category"],
        "sub_category": verdict["sub_category"],
        "role_family": verdict["role_family"],
        "all_families": verdict["all_families"],
        "family_scores": verdict["family_scores"],
        "family_confidence": verdict["family_confidence"],
        "matched_in": verdict["matched_in"],
        "needs_review": "true" if verdict["needs_review"] else "false",
        "company_type": classify_company_type(card.get("c")),
        "search_query": _clean(card.get("q")),
        "posted_date": epoch_ms_to_date(card.get("pd")),
        "relative_time": _clean(card.get("rel")),
        "description": _clean(card.get("sn"))[:DESCRIPTION_MAX_CHARS],
        "job_url": VIEWJOB_URL.format(jobkey),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def rich_row_to_club_row(r):
    """Rich row -> 23-column club schema.

    Club salary enums only express INR/USD per_month/per_annum; hourly/daily/
    weekly rates stay in the rich CSV and leave club columns empty.
    """
    period = _clean(r.get("salary_period"))
    lo, hi = _clean(r.get("salary_min")), _clean(r.get("salary_max"))
    exportable = bool(lo) and period in ("per_month", "per_annum")

    location = _clean(r.get("location"))
    city = "" if location.lower() in ("remote", "india") else location.split(",")[0].strip()

    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        # "Remote in <city>" cards carry the employer's city; pure-remote
        # cards have none and inventing one would be a lie.
        "city_name": city or "Remote",
        "company_name": _clean(r.get("company")),
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": "remote",
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": "",
        "max_experience": "",
        # No structured qualification field in the SERP card model, so this
        # is always grounded extraction from the description — never inferred.
        "qualification": extract_qualification(_clean(r.get("description"))),
        "min_salary": lo if exportable else "",
        "max_salary": (hi or lo) if exportable else "",
        "salary_period": period if exportable else "",
        "salary_currency": "INR" if exportable else "",
    }


# ----------------------------------------------------------------------------
# Time window
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, window_days, today=None):
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=window_days)).isoformat()


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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Process Indeed India remote-healthcare SERP captures "
                    "into the shared CSVs (see README.md for capture steps).")
    parser.add_argument("--from-json", nargs="+", default=[], metavar="FILE",
                        help="compact card-JSON capture file(s)")
    parser.add_argument("--from-html", nargs="+", default=[], metavar="PATH",
                        help="saved SERP .html file(s) or a directory of them")
    parser.add_argument("--descriptions", metavar="FILE",
                        help="JSON {jobkey: full_description} captured from the "
                             "SERP right pane (&vjk=<jobkey>, an allowed URL); "
                             "replaces the snippet descriptions of matching "
                             "rows in the rich CSV and rewrites the club CSV")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--window-days", type=int, default=INITIAL_WINDOW_DAYS,
                        help="first-run posting window (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if not args.from_json and not args.from_html and not args.descriptions:
        parser.error(
            "Indeed answers HTTP 403 to every scripted client, so there is "
            "nothing to fetch directly. Capture search pages in a browser "
            "and pass --from-html/--from-json (workflow in README.md).")

    cards = load_cards(args.from_json, args.from_html)
    log.info("Loaded %d cards from %d capture(s)",
             len(cards), len(args.from_json) + len(args.from_html))

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    cutoff = compute_cutoff(existing_df, args.window_days)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s",
             len(known_ids), cutoff)

    counters = {"scanned": 0, "excluded_out_of_scope": 0,
                "excluded_not_remote": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    new_rows, review_log = [], []

    for card in cards:
        counters["scanned"] += 1
        try:
            if not _clean(card.get("k")):
                continue
            if not is_remote(card):
                counters["excluded_not_remote"] += 1
                log.debug("not remote: %s (%s)", card.get("t"), card.get("loc"))
                continue

            verdict = classify_card(card)
            if not verdict["in_scope"]:
                counters["excluded_out_of_scope"] += 1
                log.debug("out of scope: %s", card.get("t"))
                continue

            posted = epoch_ms_to_date(card.get("pd"))
            if posted and posted < cutoff:
                counters["excluded_old"] += 1
                continue

            if card.get("k") in known_ids:
                counters["duplicates"] += 1
                continue

            row = card_to_rich_row(card, verdict)
        except Exception as exc:      # one bad card must never crash the run
            log.warning("Skipping malformed card %r: %s", card.get("k"), exc)
            continue

        if row["needs_review"] == "true":
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "company": row["company"],
                               "search_query": row["search_query"]})
        known_ids.add(row["job_id"])
        new_rows.append(row)
        counters["new"] += 1
        if args.limit is not None and counters["new"] >= args.limit:
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
        combined = existing_df if existing_df is not None else pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- merge full descriptions captured from the SERP right pane ----
    if args.descriptions and len(combined):
        with open(args.descriptions, encoding="utf-8") as fh:
            full_desc = json.load(fh)
        mask = combined["job_id"].isin(full_desc)
        combined.loc[mask, "description"] = combined.loc[mask, "job_id"].map(
            lambda k: full_desc[k][:DESCRIPTION_MAX_CHARS].strip())
        combined.to_csv(args.output, index=False)
        log.info("Merged %d full descriptions into %s (%d rows matched)",
                 len(full_desc), args.output, int(mask.sum()))

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV, len(review_log))

    print("\n===== Run summary =====")
    print("Cards scanned:             {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):   {:>6,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (not remote):     {:>6,}".format(counters["excluded_not_remote"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
