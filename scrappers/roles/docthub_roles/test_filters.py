"""Unit tests for the DoctHub salary parser, classifier and time window.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from docthub_scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    classify_job,
    classify_title,
    compute_cutoff,
    normalize_salary_object,
    parse_salary_string,
)


def test_salary_string_parsing():
    """Salary strings are parsed and normalized to INR/month (never filtered)."""
    cases = [
        ("18K - 40K P.M",      (18_000, 40_000, "P.M")),
        ("30K - 50K P.M",      (30_000, 50_000, "P.M")),
        ("2.00L - 2.50L P.M",  (200_000, 250_000, "P.M")),
        ("8.00L - 10.00L P.A", (66_667, 83_333, "P.A")),
        ("45K P.M",            (45_000, 45_000, "P.M")),
    ]
    for raw, expected in cases:
        assert parse_salary_string(raw) == expected, raw

    for raw in ("Not Disclosed", "", None, "Negotiable", "competitive pay"):
        assert parse_salary_string(raw) is None, raw


def test_salary_object_normalization():
    monthly = {"currency": "INR", "type": "Monthly", "minAmount": 30_000, "maxAmount": 50_000}
    assert normalize_salary_object(monthly)[:3] == (30_000, 50_000, "P.M")

    yearly = {"currency": "INR", "type": "Yearly", "minAmount": 800_000, "maxAmount": 1_000_000}
    assert normalize_salary_object(yearly)[:3] == (66_667, 83_333, "P.A")

    single = {"currency": "INR", "type": "Monthly", "minAmount": 45_000, "maxAmount": 0}
    assert normalize_salary_object(single)[:3] == (45_000, 45_000, "P.M")

    # Undisclosed comes back from the API as type "0" with zeros — kept as
    # blank salary fields, no longer grounds for exclusion.
    undisclosed = {"currency": None, "type": "0", "minAmount": 0, "maxAmount": 0}
    assert normalize_salary_object(undisclosed) is None
    assert normalize_salary_object(None) is None
    assert normalize_salary_object({}) is None


def test_title_classifier():
    for title in ("Staff Nurse - ICU", "Medical Superintendent", "Dialysis Technician",
                  "Physiotherapist", "X-Ray Technician", "Pharmacist", "MBBS Doctor",
                  "Phlebotomist", "Optometrist", "Ward Boy", "Paramedic",
                  "Biomedical Engineer"):
        assert classify_title(title) == "allow", title

    for title in ("Software Developer", "Mechanical Engineer", "Civil Engineer",
                  "Full Stack Developer", "Accountant", "Graphic Designer",
                  "Digital Marketing Executive", "HR Executive", "Telecaller"):
        assert classify_title(title) == "deny", title

    assert classify_title("Zonal Manager") == "unknown"


def test_classify_job_decision_table():
    assert classify_job("Dermatologist", "Doctor / Surgeon") == (True, False)
    assert classify_job("Software Developer", "Paramedical / Technician") == (False, False)
    assert classify_job("Staff Nurse", "Housekeeping Department") == (False, False)
    assert classify_job("Clinical Pharmacist", "Pharmaceuticals") == (True, False)
    assert classify_job("Zonal Manager", "Pharmaceuticals") == (True, True)


def test_compute_cutoff():
    # First run (no CSV): last INITIAL_WINDOW_DAYS days
    expected_first = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    assert compute_cutoff(None) == expected_first
    assert compute_cutoff(pd.DataFrame({"posted_date": []})) == expected_first
    assert compute_cutoff(pd.DataFrame({"posted_date": ["garbage", ""]})) == expected_first

    # Later runs: newest saved posted_date minus the grace overlap
    df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-06", "2026-06-20"]})
    expected = (date(2026, 7, 6) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
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
