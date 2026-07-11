#!/usr/bin/env python3
"""Export scraped job CSVs to the HealthCareers.club import format.

Converts the rich per-source CSVs produced by the Docthub, jobslly and apna
scrapers into the shared 22-column schema defined by
https://github.com/0xprajapati/job-scrappers (see its README and
job_samples.csv), writing one file per site to:

    jobs_csv/<DD-MM-YYYY>/<site_name>.csv

Mapping notes (the club schema is stricter than our data):
- company_type allows only "hospital" | "pharma": companies matching pharma/
  CRO/lab keywords (or known pharma employers) become "pharma", everything
  else "hospital".
- category allows only doctors | nurses | pharmacists | non_clinical: mapped
  from the title (and Docthub's category / apna's role_category when the
  title alone is ambiguous). Allied-health roles (technicians, physio, etc.)
  necessarily land in non_clinical.
- job_type: apna "Work from Home" -> remote; pure part-time -> part_time;
  everything else full_time (no source marks hybrid explicitly except via
  text we don't parse).
- Salary is re-parsed from salary_raw so the exported amounts are the
  original full figures (never lakhs), with per_annum/per_month set from the
  source's period. Optional fields are left empty when the source is silent —
  nothing is invented.

Run:  python export_club_csv.py [--date DD-MM-YYYY] [--outdir jobs_csv]
"""

import argparse
import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent

# Preferred input per site: full crawl first, sample as fallback.
SITE_INPUTS = {
    "docthub": ["Docthub/docthub_jobs.csv", "Docthub/docthub_jobs_sample.csv"],
    "jobslly": ["jobslly/jobslly_jobs.csv"],
    "apna": ["apna/apna_jobs.csv", "apna/apna_jobs_sample.csv"],
}

CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "application_url",
    "posted_at", "min_experience", "max_experience",
    "min_salary", "max_salary", "salary_period", "salary_currency",
    "is_active", "expires_at",
]

ENUMS = {
    "company_type": {"hospital", "pharma"},
    "job_type": {"full_time", "part_time", "remote", "hybrid"},
    "category": {"doctors", "nurses", "pharmacists", "non_clinical"},
    "salary_period": {"per_annum", "per_month", ""},
    "salary_currency": {"INR", "USD", ""},
}

_PHARMA_RE = re.compile(
    r"pharma|therapeut|laborator|\bresearch\b|\bcro\b|clinical|biotech|"
    r"life ?science|sanofi|iqvia|parexel|lilly|novartis|accenture|optum|"
    r"clarivate|syneos|thermo fisher",
    re.IGNORECASE)

_NURSE_RE = re.compile(r"nurs|midwif|\bgnm\b|\banm\b", re.IGNORECASE)
_PHARMACIST_RE = re.compile(r"pharmacist|\bpharmacy\b|\bpharm ?d\b", re.IGNORECASE)
_DOCTOR_RE = re.compile(
    r"doctor|physician|surgeon|\bmbbs\b|dentist|medical officer|\brmo\b|"
    r"[a-z]+ologist|intensivist|hospitalist|anaesthetist|anesthetist|"
    r"obstetrician|p(a?)ediatrician|psychiatrist|veterinary|"
    r"medical superintendent|medical director|"
    # the club's job_samples.csv files MBBS-requiring corporate roles
    # (medical affairs, MSL, medical writers) under "doctors"
    r"medical affairs|medical science liaison|\bmsl\b|"
    r"medical\s+(\w+\s+)?writer",
    re.IGNORECASE)

_DOCTHUB_CATEGORY_MAP = {
    "Doctor / Surgeon": "doctors",
    "Medical Officer": "doctors",
    "Dentist": "doctors",
    "AYUSH/Alternative Therapy": "doctors",
    "Nursing": "nurses",
    "Pharmacist": "pharmacists",
}

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

log = logging.getLogger("export_club_csv")


def classify_category(title, fallback=""):
    """Map a job title to the club's category enum."""
    title = title or ""
    if _NURSE_RE.search(title):
        return "nurses"
    if _PHARMACIST_RE.search(title):
        return "pharmacists"
    if _DOCTOR_RE.search(title):
        return "doctors"
    return fallback or "non_clinical"


def classify_company_type(name):
    return "pharma" if _PHARMA_RE.search(name or "") else "hospital"


def parse_raw_salary(raw, period_hint=""):
    """Return (min_full, max_full, period_enum) from a source salary_raw.

    Handles the three source formats:
      Docthub  "INR 600,000 - 3,000,000 P.A"   -> amounts verbatim
      jobslly  "INR 18 - 25 YEAR" (period LPA)  -> lakhs * 100,000
      apna     "₹100,000 - ₹125,000 monthly"    -> amounts verbatim
    Returns ("", "", "") when undisclosed/unparseable — never invents values.
    """
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("not disclosed", "nan"):
        return ("", "", "")
    numbers = [float(n.replace(",", "")) for n in _NUM_RE.findall(raw)]
    if not numbers:
        return ("", "", "")
    lo = numbers[0]
    hi = numbers[1] if len(numbers) > 1 else lo
    if hi < lo:
        lo, hi = hi, lo

    text = raw.upper() + " " + (period_hint or "").upper()
    if "LPA" in text or (max(numbers) < 1000 and "YEAR" in text):
        lo, hi = lo * 100_000, hi * 100_000
        period = "per_annum"
    elif "P.A" in text or "YEAR" in text or "ANNUM" in text:
        period = "per_annum"
    elif "P.M" in text or "MONTH" in text:
        period = "per_month"
    else:
        return ("", "", "")
    return (str(int(round(lo))), str(int(round(hi))), period)


def city_from_location(location, style):
    """Extract a city name from a source location string."""
    location = (location or "").strip()
    if not location:
        return ""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return ""
    # docthub/jobslly: "City, State" -> first part; apna: "Area, City" -> last
    city = parts[0] if style == "city_first" else parts[-1]
    city = city.split("/")[0].strip()          # "Mumbai/Bombay" -> "Mumbai"
    city = re.sub(r"\s*Region$", "", city, flags=re.IGNORECASE)
    return city


def base_row():
    return {col: "" for col in CLUB_COLUMNS} | {
        "country_name": "India", "country_code": "IN",
        "country_dial_code": "+91", "is_active": "true",
        "salary_currency": "",  # set together with amounts
    }


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _int_or_blank(value):
    value = _clean(value)
    if value == "":
        return ""
    try:
        return str(int(float(value)))
    except ValueError:
        return ""


def _finish_salary(row, raw, period_hint=""):
    lo, hi, period = parse_raw_salary(raw, period_hint)
    row["min_salary"], row["max_salary"], row["salary_period"] = lo, hi, period
    row["salary_currency"] = "INR" if lo else ""


def convert_docthub(df):
    for _, src in df.iterrows():
        row = base_row()
        row["city_name"] = city_from_location(_clean(src.get("location")), "city_first")
        row["company_name"] = _clean(src.get("company"))
        row["company_type"] = classify_company_type(row["company_name"])
        row["title"] = _clean(src.get("title"))
        row["description"] = _clean(src.get("description"))
        row["job_type"] = "part_time" if "part" in _clean(src.get("job_type")).lower() else "full_time"
        fallback = _DOCTHUB_CATEGORY_MAP.get(_clean(src.get("category")), "")
        row["category"] = classify_category(row["title"], fallback)
        row["application_url"] = _clean(src.get("job_url"))
        row["posted_at"] = _clean(src.get("posted_date"))
        row["min_experience"] = _int_or_blank(src.get("experience_min_years"))
        row["max_experience"] = _int_or_blank(src.get("experience_max_years"))
        _finish_salary(row, _clean(src.get("salary_raw")),
                       _clean(src.get("salary_period_original")))
        yield row


def convert_jobslly(df):
    for _, src in df.iterrows():
        row = base_row()
        row["city_name"] = city_from_location(_clean(src.get("location")), "city_first")
        row["company_name"] = _clean(src.get("company"))
        row["company_type"] = classify_company_type(row["company_name"])
        row["title"] = _clean(src.get("title"))
        row["description"] = _clean(src.get("description"))
        row["job_type"] = "part_time" if "part" in _clean(src.get("job_type")).lower() else "full_time"
        row["category"] = classify_category(row["title"])
        row["application_url"] = _clean(src.get("job_url"))
        row["posted_at"] = _clean(src.get("posted_date"))
        _finish_salary(row, _clean(src.get("salary_raw")),
                       _clean(src.get("salary_period_original")))
        yield row


_APNA_EXP_RE = re.compile(r"min\.?\s*([\d.]+)\s*(year|month)", re.IGNORECASE)


def convert_apna(df):
    for _, src in df.iterrows():
        row = base_row()
        row["city_name"] = city_from_location(_clean(src.get("location")), "city_last")
        row["company_name"] = _clean(src.get("company"))
        row["company_type"] = classify_company_type(row["company_name"])
        row["title"] = _clean(src.get("title"))
        row["description"] = _clean(src.get("description"))

        work_mode = _clean(src.get("work_mode"))
        job_type = _clean(src.get("job_type"))
        if "Work from Home" in work_mode:
            row["job_type"] = "remote"
        elif "Part Time" in job_type and "Full Time" not in job_type:
            row["job_type"] = "part_time"
        else:
            row["job_type"] = "full_time"

        role = _clean(src.get("role_category"))
        fallback = "doctors" if role.lower() == "doctor" else ""
        row["category"] = classify_category(row["title"], fallback)
        row["application_url"] = _clean(src.get("apply_url")) or _clean(src.get("job_url"))
        row["posted_at"] = _clean(src.get("posted_date"))

        exp = _APNA_EXP_RE.search(_clean(src.get("experience_raw")))
        if exp:
            years = float(exp.group(1))
            if exp.group(2).lower().startswith("month"):
                years /= 12
            row["min_experience"] = str(int(years))
        elif "fresher" in _clean(src.get("experience_raw")).lower():
            row["min_experience"] = "0"

        # apna salaries are always monthly; use the FIXED range already
        # stored in salary_min/max_monthly (incentives excluded).
        lo = _int_or_blank(src.get("salary_min_monthly"))
        hi = _int_or_blank(src.get("salary_max_monthly")) or lo
        if lo:
            row["min_salary"], row["max_salary"] = lo, hi
            row["salary_period"], row["salary_currency"] = "per_month", "INR"
        yield row


CONVERTERS = {"docthub": convert_docthub, "jobslly": convert_jobslly,
              "apna": convert_apna}


def validate(df, site):
    """Check the checklist items that can be verified mechanically."""
    problems = []
    if list(df.columns) != CLUB_COLUMNS:
        problems.append("header mismatch")
    for col, allowed in ENUMS.items():
        bad = set(df[col].fillna("")) - allowed
        if bad:
            problems.append("{}: bad enum values {}".format(col, sorted(bad)))
    for col in ("country_name", "city_name", "company_name", "title",
                "application_url", "posted_at"):
        empty = int((df[col].fillna("") == "").sum())
        if empty:
            problems.append("{}: {} empty required values".format(col, empty))
    bad_dates = int((~df["posted_at"].fillna("").str.match(r"^\d{4}-\d{2}-\d{2}$")).sum())
    if bad_dates:
        problems.append("posted_at: {} not YYYY-MM-DD".format(bad_dates))
    for problem in problems:
        log.error("%s.csv FAILED validation: %s", site, problem)
    return not problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export scraper CSVs to the HealthCareers.club format.")
    parser.add_argument("--date", default=date.today().strftime("%d-%m-%Y"),
                        help="run-date folder name, DD-MM-YYYY (default: today)")
    parser.add_argument("--outdir", default=str(BASE_DIR / "jobs_csv"),
                        help="base output directory (default: %(default)s)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    out_dir = Path(args.outdir) / args.date
    out_dir.mkdir(parents=True, exist_ok=True)

    for site, candidates in SITE_INPUTS.items():
        source_path = next((BASE_DIR / c for c in candidates
                            if (BASE_DIR / c).exists()), None)
        if source_path is None:
            log.warning("%s: no input CSV found (looked for %s) — skipped",
                        site, candidates)
            continue
        df = pd.read_csv(source_path)
        rows = list(CONVERTERS[site](df))
        out = pd.DataFrame(rows, columns=CLUB_COLUMNS)
        ok = validate(out, site)
        target = out_dir / "{}.csv".format(site)
        out.to_csv(target, index=False)
        log.info("%s: %d rows from %s -> %s%s", site, len(out),
                 source_path.relative_to(BASE_DIR), target,
                 "" if ok else "  (VALIDATION FAILED — fix before handoff)")


if __name__ == "__main__":
    main()
