"""Unit tests for the nextenti parsers, classifier and time window.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    build_job_url,
    classify_category,
    classify_company_type,
    compute_cutoff,
    map_job_type,
    parse_range,
    parse_salary,
    salary_period,
    slugify,
)


def test_parse_salary_and_experience():
    assert parse_salary("66000 - 83000") == (66000, 83000)
    assert parse_salary("45000") == (45000, 45000)
    assert parse_salary("1,20,000 - 1,50,000") == (120000, 150000)
    # captured, never filtered: undisclosed/blank -> empty, not an exclusion
    assert parse_salary("") == (None, None)
    assert parse_salary(None) == (None, None)
    assert parse_salary("0 - 0") == (None, None)
    # experience ranges
    assert parse_range("2 - 5 years") == (2.0, 5.0)
    assert parse_range("Fresher") == (None, None)


def test_salary_period_and_job_type():
    assert salary_period("Monthly") == "per_month"
    assert salary_period("Annual") == "per_annum"
    assert salary_period(None) == ""
    assert map_job_type("Full Time") == "full_time"
    assert map_job_type("Part Time") == "part_time"
    assert map_job_type("Internship") == "part_time"
    assert map_job_type(None) == "full_time"


def test_classify_category():
    # profession-driven (the primary signal)
    assert classify_category("Doctor", "Anything") == ("doctors", False)
    assert classify_category("Nurse", "Staff X") == ("nurses", False)
    assert classify_category("Pharmacist", "X") == ("pharmacists", False)
    assert classify_category("Physiotherapy", "Physiotherapist") == ("non_clinical", False)
    assert classify_category("Human Resources", "Assistant Manager") == ("non_clinical", False)
    # generic profession -> fall back to the title
    assert classify_category("Others", "Consultant Cardiologist") == ("doctors", False)
    assert classify_category("Others", "Staff Nurse ICU") == ("nurses", False)
    # truly unknown -> non_clinical but flagged for review
    assert classify_category("", "Zonal Head") == ("non_clinical", True)


def test_company_type():
    assert classify_company_type("Sparsh Hospital") == "hospital"
    assert classify_company_type("Lambda Therapeutic Research Ltd") == "pharma"
    assert classify_company_type("Apollo Diagnostics Labs") == "pharma"
    assert classify_company_type("Andhra Prime Hospital") == "hospital"


def test_slug_and_url():
    assert slugify("Shiva & Shiva Orthopedic Hospital Pvt Ltd") == \
        "shiva-and-shiva-orthopedic-hospital-pvt-ltd"
    url = build_job_url({"organizationName": "Shiva & Shiva Orthopedic Hospital Pvt Ltd",
                         "jobTitle": "Assistant Manager ", "city": "Bengaluru",
                         "jobId": "4122046"})
    assert url == ("https://nextenti.ai/jobs/"
                   "shiva-and-shiva-orthopedic-hospital-pvt-ltd-assistant-manager-bengaluru--4122046")


def test_compute_cutoff():
    expected_first = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    assert compute_cutoff(None) == expected_first
    assert compute_cutoff(pd.DataFrame({"posted_date": []})) == expected_first
    df = pd.DataFrame({"posted_date": ["2026-07-11", "2026-07-04", "2026-06-20"]})
    expected = (date(2026, 7, 11) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
    assert compute_cutoff(df) == expected


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", name, "-", exc)
    raise SystemExit(1 if failures else 0)
