#!/usr/bin/env python3
"""Scrape healthcare job listings from foundit.in (ex Monster India).

How foundit can (and cannot) be scraped
---------------------------------------
foundit.in sits behind Akamai bot protection: every plain-HTTP client
(requests, curl — script UA or full browser header set alike) gets HTTP 403
on `/search/` and `/job/` pages; only the XML sitemaps answer to a plain
client. robots.txt (verified 2026-08-08) additionally disallows
`/middleware/` — foundit's private JSON search API — for ALL user agents,
so the API is off-limits (master spec §1) even if the 403 wall fell.

What IS both allowed and complete: the server-rendered SEO search pages
`/search/<keyword>-jobs[-N]`. They are permitted by the `User-agent: *`
robots group (only AI-training bots — GPTBot, ClaudeBot, CCBot… — are
excluded from `/search/`), and each page embeds the full result payload in
its React Server Components flight stream (`self.__next_f.push` chunks):

    "jobSearchAPIData": {
        "data": [20 job cards], "meta": {"paging": {"total": N}}, ...
    }

Every card carries: jobId, title, company.name, locations[].city,
minimum/maximumExperience.years, minimum/maximumSalary {currency,
absoluteValue (INR/yr), absoluteMonthlyValue (INR/mo)}, hideSalary,
postedAt/createdAt/updatedAt (epoch ms), industries[], functions[],
jobTypes[], employmentTypes[], skills[], jdUrl (relative), redirectUrl
(external ATS), totalApplicants — plus a description that is usually a
flight-stream text-chunk reference (`"$2b"` -> `2b:T<hexlen>,<html>`).

Because of the Akamai wall this script does not fetch the site itself
(exactly like this repo's indeed/ and naukri/ scrapers): a real browser
session drives the allowed SEO pages and dumps the extracted cards to
captures/<DD-MM-YYYY>.json, and this script transforms the capture offline.
See README.md for the capture snippet.

The SEO listing is relevance-ordered (freshness-weighted but NOT sorted by
date), and page suffixes are ignored as soon as any query parameter is
added, so there is no date-sorted crawl and no early stop: the capture
walks every page of each keyword (capped, see README) and this script's
date window keeps only in-window jobs.

Scope gate (2026-08-25 taxonomy migration, instructions/taxonomy-migration-spec.md)
-----------------------------------------------------------------------------------
foundit is NOT a broad "all healthcare" scraper. Every card goes through
the ONE shared classifier, `_shared/classification.classify_job(title,
skills, description)` — the same pipeline every scraper in this repo uses,
so all sources agree on scope by construction:

1. A negative-keyword veto drops lookalikes ("medical billing", "AR
   caller", "revenue cycle", "staff nurse", "lab technician"…) and the
   weighted role-family scorer (title x5 / skills x2 / description x1)
   must name a family — otherwise `in_scope` is False and the card is
   dropped, counted as excluded_out_of_scope (printed in the summary).
2. Kept cards carry `category` ("Non Clinical" | "Public Health"),
   `sub_category` (one of the 20 taxonomy sub-categories), the winning
   `role_family`, and the full score trace (`all_families`,
   `family_scores`, `family_confidence`, `matched_in`) in the rich CSV.
   A title that reads like a different profession is kept but flagged
   needs_review — never silently dropped (master spec §2).

The keyword SERPs the capture walks are just recall (ask foundit for these
roles instead of crawling all of healthcare); this scorer is the precision.

Salary (master spec §3): capture, don't filter — never excluded, never
invented. foundit exposes salary as INR/year (`absoluteValue`) plus a
precomputed INR/month (`absoluteMonthlyValue`). Cards with `hideSalary:
true` sometimes still carry values in the payload; they are stored (the
data is real) with salary_hidden=true recording that foundit's UI hides
them. All-zero values -> "Not Disclosed".

Dates: postedAt is epoch ms; converted to an IST (UTC+5:30) calendar date —
foundit is an India board and its "posted N days ago" labels follow IST
days. updatedAt > postedAt happens on refreshed listings; the watermark uses
posted_date only, so refreshed reposts are absorbed by jobId dedup.

Outputs (repo README + instructions/master-scraper-spec.md)
-----------------------------------------------------------
* foundit_jobs.csv       — rich cumulative store (dedup key: job_id),
                           watermark source for incremental runs.
* ../../jobs_csv/<DD-MM-YYYY>/foundit.csv
                         — HealthCareers.club 23-column schema.
* needs_review.csv       — kept rows whose title reads like a different
                           profession (rescued on skills/description).

Time window (master spec §4): first run keeps INITIAL_WINDOW_DAYS (7);
later runs keep jobs newer than (newest stored posted_date -
WATERMARK_GRACE_DAYS). Run `python foundit_scraper.py --help` for options.
"""

import argparse
import html as html_lib
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# The ONE shared classifier (2026-08-25 taxonomy migration): classify_job
# gates every card to the two categories — Non Clinical, Public Health —
# with ten sub-categories each, and supplies the club-CSV column contract.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
from classification import CLUB_COLUMNS, classify_job, extract_qualification

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "foundit"
SITE_BASE = "https://www.foundit.in"

HERE = Path(__file__).resolve().parent
CAPTURES_DIR = HERE / "captures"
RICH_CSV = str(HERE / "foundit_jobs.csv")
NEEDS_REVIEW_CSV = str(HERE / "needs_review.csv")
CLUB_CSV_DIR = HERE.parents[1] / "jobs_csv"

INITIAL_WINDOW_DAYS = 7        # master spec §4
WATERMARK_GRACE_DAYS = 2
DESCRIPTION_MAX_CHARS = 20_000

IST = timezone(timedelta(hours=5, minutes=30))

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "location",
    "salary_raw", "salary_min_monthly", "salary_max_monthly",
    "salary_period_original", "salary_currency_original", "salary_hidden",
    "job_type", "employment_type", "experience_min_years",
    "experience_max_years", "industries", "functions", "skills",
    "category", "sub_category", "role_family", "sub_category_basis",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "company_type", "match_signal", "needs_review",
    "total_applicants", "posted_date", "updated_date", "description",
    "job_url", "apply_redirect_url", "found_via", "scraped_at",
]

# The club CSV contract is the shared 23-column CLUB_COLUMNS imported from
# _shared/classification.py — never hand-copied here.

log = logging.getLogger("foundit_scraper")

# ----------------------------------------------------------------------------
# Scope gate — the ONE shared classifier (2026-08-25 taxonomy migration)
# ----------------------------------------------------------------------------

def scope_card(title, skills, description):
    """Gate one card through the shared classify_job.

    Thin wrapper kept for the scraper's historical call shape: returns
    classify_job's verdict dict. `verdict["in_scope"]` is False when the
    negative-keyword veto fires or no role family scores; in-scope verdicts
    carry `category` ("Non Clinical" | "Public Health"), `sub_category`,
    `role_family`, `needs_review` and the full score trace.
    """
    return classify_job(title, skills, description)


_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical|drug|"
    r"iqvia|parexel|syneos|pfizer|thermo ?fisher",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Field parsing
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def clean_value(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan", "null", "$undefined") else text


def strip_html(text):
    text = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|td|strong|em)[^>]*>", " ",
                  text or "", flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_description(text, limit=DESCRIPTION_MAX_CHARS):
    """Cap at `limit` chars on a word boundary, marked with a trailing "…"."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    if len(spaced) >= limit * 0.5:
        cut = spaced
    return cut.rstrip() + "…"


def _amount(salary_obj, key):
    """A numeric field from a foundit salary object; 0/absent -> None."""
    if not isinstance(salary_obj, dict):
        return None
    try:
        value = float(salary_obj.get(key) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_salary(min_salary, max_salary, hide_salary, currency_code):
    """Master spec §3 — capture, never filter, never invent.

    foundit exposes {absoluteValue: INR/yr, absoluteMonthlyValue: INR/mo}.
    Returns (salary_raw, min_monthly, max_monthly, period, currency, hidden):

      450000..600000/yr -> ("INR 450000-600000 P.A.", "37500", "50000",
                            "Yr", "INR", ...)
      only monthly      -> ("INR 20000-25000 P.M.", ..., "Mo", ...)
      all zero          -> ("Not Disclosed", "", "", "", "", ...)

    hideSalary=true cards sometimes still carry payload values; they are
    kept, with hidden="true" recording that foundit's UI hides them.
    """
    hidden = "true" if hide_salary else "false"
    currency = clean_value(currency_code) or "INR"

    yr_lo, yr_hi = _amount(min_salary, "absoluteValue"), _amount(max_salary, "absoluteValue")
    mo_lo = _amount(min_salary, "absoluteMonthlyValue")
    mo_hi = _amount(max_salary, "absoluteMonthlyValue")

    if yr_lo or yr_hi:
        lo, hi = yr_lo or yr_hi, yr_hi or yr_lo
        raw = "{} {:.0f}-{:.0f} P.A.".format(currency, lo, hi)
        min_mo = mo_lo if mo_lo else lo / 12
        max_mo = mo_hi if mo_hi else hi / 12
        return (raw, str(int(round(min_mo))), str(int(round(max_mo))),
                "Yr", currency, hidden)
    if mo_lo or mo_hi:
        lo, hi = mo_lo or mo_hi, mo_hi or mo_lo
        raw = "{} {:.0f}-{:.0f} P.M.".format(currency, lo, hi)
        return (raw, str(int(round(lo))), str(int(round(hi))),
                "Mo", currency, hidden)
    return ("Not Disclosed", "", "", "", "", hidden)


def epoch_ms_to_ist_date(value):
    """postedAt epoch ms -> IST calendar date "YYYY-MM-DD"; "" if unusable."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=IST).date().isoformat()


def decode_job_type(job_types, employment_types):
    """Return (rich job_type, rich employment_type, club enum)."""
    job_types = [clean_value(t) for t in (job_types or []) if clean_value(t)]
    employment_types = [clean_value(t) for t in (employment_types or [])
                        if clean_value(t)]
    joined = " ".join(job_types + employment_types).lower()
    if "work from home" in joined or "remote" in joined:
        club = "remote"
    elif "part time" in joined or "part-time" in joined:
        club = "part_time"
    else:
        club = "full_time"
    return (", ".join(job_types), ", ".join(employment_types), club)


def experience_years(value):
    """{"years": 3} / 3 / "3" -> "3"; anything else -> ""."""
    if isinstance(value, dict):
        value = value.get("years")
    text = clean_value(value)
    if text == "":
        return ""
    try:
        return str(int(float(text)))
    except ValueError:
        return ""


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, today=None, since=None):
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    today = today or date.today()
    return (today - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def card_signals(card):
    """Extract the (title, skills_str, description) the scorer reads."""
    title = clean_value(card.get("title"))
    skills = [clean_value(t) for t in card.get("skills") or []
              if clean_value(t)]
    description = truncate_description(
        strip_html(clean_value(card.get("description"))))
    return title, ", ".join(skills), description


def job_to_rich_row(card, verdict):
    """Build a rich row for an in-scope card.

    `verdict` is classify_job's dict (see scope_card): `category` holds the
    top-level "Non Clinical" | "Public Health", `sub_category` the finer
    split (with classify_job's family fallback for Non Clinical already
    applied), `role_family` the winning family, plus the score trace.
    """
    title, skills_str, description = card_signals(card)
    needs_review = verdict.get("needs_review", False)

    (salary_raw, sal_min, sal_max, sal_period, sal_currency,
     sal_hidden) = parse_salary(card.get("minSalary"), card.get("maxSalary"),
                                card.get("hideSalary"),
                                card.get("currencyCode"))
    job_type, employment_type, _ = decode_job_type(
        card.get("jobTypes"), card.get("employmentTypes"))

    locations = card.get("locations") or []
    if not isinstance(locations, list):
        locations = [str(locations)]
    locations = [clean_value(l) for l in locations if clean_value(l)]

    company = "" if card.get("hideCompanyName") else clean_value(
        card.get("companyName"))

    jd_url = clean_value(card.get("jdUrl"))
    if jd_url and not jd_url.startswith("http"):
        jd_url = SITE_BASE + jd_url

    return {
        "source": SITE,
        "job_id": clean_value(card.get("jobId")),
        "title": title,
        "company": company,
        "location": ", ".join(locations),
        "salary_raw": salary_raw,
        "salary_min_monthly": sal_min,
        "salary_max_monthly": sal_max,
        "salary_period_original": sal_period,
        "salary_currency_original": sal_currency,
        "salary_hidden": sal_hidden,
        "job_type": job_type,
        "employment_type": employment_type,
        "experience_min_years": experience_years(card.get("minExpYears")),
        "experience_max_years": experience_years(card.get("maxExpYears")),
        "industries": ", ".join(clean_value(t) for t in card.get("industries") or []
                                if clean_value(t)),
        "functions": ", ".join(clean_value(t) for t in card.get("functions") or []
                               if clean_value(t)),
        "skills": skills_str,
        "category": verdict["category"],
        "sub_category": verdict["sub_category"],
        "role_family": verdict["role_family"],
        "sub_category_basis": verdict.get("sub_category_basis", ""),
        "all_families": verdict.get("all_families", ""),
        "family_scores": verdict.get("family_scores", ""),
        "family_confidence": verdict.get("family_confidence", ""),
        "matched_in": verdict.get("matched_in", ""),
        "company_type": classify_company_type(company),
        "match_signal": "{}:{}".format(
            ",".join(card.get("foundVia") or []),
            verdict.get("matched_in", "")),
        "needs_review": "true" if needs_review else "false",
        "total_applicants": clean_value(card.get("totalApplicants")),
        "posted_date": epoch_ms_to_ist_date(card.get("postedAt")),
        "updated_date": epoch_ms_to_ist_date(card.get("updatedAt")),
        "description": description,
        "job_url": jd_url,
        "apply_redirect_url": clean_value(card.get("redirectUrl")),
        "found_via": ",".join(card.get("foundVia") or []),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    joined = (_clean(r.get("job_type")) + " "
              + _clean(r.get("employment_type"))).lower()
    if "work from home" in joined or "remote" in joined:
        club_type = "remote"
    elif "part time" in joined or "part-time" in joined:
        club_type = "part_time"
    else:
        club_type = "full_time"

    city = _clean(r.get("location")).split(",")[0].strip() or "India"

    # Club schema wants ORIGINAL full amounts + period; the rich CSV keeps
    # normalized monthly, so recompute the yearly figures when period is Yr.
    lo = _clean(r.get("salary_min_monthly"))
    hi = _clean(r.get("salary_max_monthly")) or lo
    period = _clean(r.get("salary_period_original"))
    currency = _clean(r.get("salary_currency_original"))
    if lo and period == "Yr":
        club_lo = str(int(round(float(lo) * 12)))
        club_hi = str(int(round(float(hi) * 12)))
        club_period = "per_annum"
    elif lo:
        club_lo, club_hi, club_period = lo, hi, "per_month"
    else:
        club_lo = club_hi = club_period = currency = ""
    if currency not in ("INR", "USD"):   # club enum only knows INR/USD
        club_lo = club_hi = club_period = currency = ""

    return {
        "country_name": "India",
        "country_code": "IN",
        "country_dial_code": "+91",
        "city_name": city,
        "company_name": _clean(r.get("company")) or "Confidential",
        "company_type": _clean(r.get("company_type")) or "hospital",
        "company_logo": "",
        "company_about": "",
        "title": _clean(r.get("title")),
        "description": _clean(r.get("description")),
        "job_type": club_type,
        # `category` is the top-level taxonomy value ("Non Clinical" |
        # "Public Health"); `sub_category` carries the finer split and
        # `role_family` the winning family.
        "category": _clean(r.get("category")),
        "sub_category": _clean(r.get("sub_category")),
        "role_family": _clean(r.get("role_family")),
        "application_url": _clean(r.get("job_url")),
        "posted_at": _clean(r.get("posted_date")),
        "min_experience": _clean(r.get("experience_min_years")),
        "max_experience": _clean(r.get("experience_max_years")),
        "qualification": extract_qualification(_clean(r.get("description"))),
        "min_salary": club_lo,
        "max_salary": club_hi,
        "salary_period": club_period,
        "salary_currency": currency,
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def newest_capture():
    files = sorted(CAPTURES_DIR.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


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
        description="Transform a foundit.in browser capture into the "
                    "healthcare jobs CSVs (no network access needed).")
    parser.add_argument("--capture", default=None, metavar="FILE",
                        help="capture JSON to ingest (default: newest file "
                             "in captures/)")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after N new jobs (for test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark; keep jobs posted "
                             "on/after this date")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    capture_path = Path(args.capture) if args.capture else newest_capture()
    if capture_path is None or not capture_path.exists():
        sys.exit("No capture file found in {} — run the browser capture "
                 "first (see README.md).".format(CAPTURES_DIR))
    with open(capture_path) as f:
        capture = json.load(f)
    cards = capture.get("jobs") or []
    log.info("Capture %s: %d cards (captured_at %s)", capture_path.name,
             len(cards), capture.get("captured_at", "?"))
    if capture.get("errors"):
        log.warning("Capture recorded %d fetch errors (coverage may be "
                    "partial): %s", len(capture["errors"]),
                    "; ".join(capture["errors"][:5]))
    for kw, stat in (capture.get("keywords") or {}).items():
        if stat.get("capped"):
            log.warning("Capture NOTE: keyword %r hit its page cap "
                        "(%s pages of %s total jobs) — coverage truncated",
                        kw, stat.get("pages"), stat.get("total"))

    existing_df = load_existing(args.output)
    known_ids = (set(existing_df["job_id"].dropna())
                 if existing_df is not None else set())
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs; keeping jobs posted on/after %s%s",
             len(known_ids), cutoff, " (--since override)" if args.since else "")

    counters = {"scanned": 0, "excluded_out_of_scope": 0, "excluded_old": 0,
                "needs_review": 0, "new": 0, "duplicates": 0}
    new_rows, review_log = [], []

    for card in cards:
        counters["scanned"] += 1
        try:
            posted = epoch_ms_to_ist_date(card.get("postedAt"))
            # Undated cards can't be placed in the window; keep them and let
            # dedup absorb re-appearances.
            if posted and posted < cutoff:
                counters["excluded_old"] += 1
                continue

            # Scope gate: the shared classify_job (see scope_card).
            title, skills_str, description = card_signals(card)
            verdict = scope_card(title, skills_str, description)
            if not verdict["in_scope"]:
                counters["excluded_out_of_scope"] += 1
                continue

            job_id = clean_value(card.get("jobId"))
            if not job_id:
                log.warning("Card without jobId — skipped (%r)", title)
                continue
            if job_id in known_ids:
                counters["duplicates"] += 1
                continue

            row = job_to_rich_row(card, verdict)
        except Exception as exc:       # never let one card crash the run
            log.warning("Skipping malformed card (%s)", exc)
            continue

        if row["needs_review"] == "true":
            counters["needs_review"] += 1
            review_log.append({
                "job_id": row["job_id"], "title": row["title"],
                "company": row["company"], "category": row["category"],
                "sub_category": row["sub_category"],
                "role_family": row["role_family"],
                "match_signal": row["match_signal"]})
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
        combined = (existing_df if existing_df is not None
                    else pd.DataFrame(columns=RICH_COLUMNS))
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- always (re)write the club-schema CSV for this run date ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    if review_log:
        review_df = pd.DataFrame(review_log)
        existing_review = load_existing(NEEDS_REVIEW_CSV)
        if existing_review is not None:
            review_df = pd.concat([existing_review, review_df],
                                  ignore_index=True)
            review_df = review_df.drop_duplicates(subset="job_id", keep="first")
        review_df.to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_df))

    print("\n===== Run summary =====")
    print("Cards scanned:             {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope)  : {:>6,}".format(counters["excluded_out_of_scope"]))
    print("Excluded (older than {}): {:>4,}".format(cutoff, counters["excluded_old"]))
    print("Flagged needs_review:      {:>6,}".format(counters["needs_review"]))
    print("New jobs added:            {:>6,}".format(counters["new"]))
    print("Duplicates skipped:        {:>6,}".format(counters["duplicates"]))


if __name__ == "__main__":
    main()
