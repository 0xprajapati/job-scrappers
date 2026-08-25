"""Unit tests for the DoctHub salary parser, classifier and time window.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from docthub_scraper import (
    CLUB_COLUMNS,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    classify_job,
    compute_cutoff,
    job_to_row,
    normalize_salary_object,
    parse_salary_string,
    rich_row_to_club_row,
    strip_html,
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


def test_classifier_wiring_in_scope():
    """The shared classify_job labels in-scope roles with the two-level
    taxonomy (the engine itself is covered by _shared/test_classification)."""
    v = classify_job("Clinical Research Coordinator")
    assert v["in_scope"] is True
    assert v["category"] == "Non Clinical"
    assert v["sub_category"] == "Clinical Research"

    v = classify_job("Epidemiologist")
    assert v["in_scope"] is True
    assert v["category"] == "Public Health"
    assert v["sub_category"] == "Epidemiology"


def test_classifier_wiring_out_of_scope():
    """Clinical/bedside and unrelated titles drop — the docthub facet name
    no longer decides anything."""
    for title in ("Dermatologist", "Staff Nurse - ICU", "Software Developer",
                  "MBBS Doctor", "Ward Boy"):
        assert classify_job(title)["in_scope"] is False, title


def test_job_to_row_carries_taxonomy():
    job = {"code": "clinical-research-associate-J120239",
           "title": "Clinical Research Associate",
           "organization": {"name": "Acme CRO"},
           "location": {"location": "Pune, Maharashtra"},
           "workExperience": {"fromYear": 1, "toYear": 3},
           "salary": {"currency": "INR", "type": "Monthly",
                      "minAmount": 30_000, "maxAmount": 50_000},
           "employementType": "Full Time",
           "publishedDate": "2026-08-20T10:00:00Z"}
    verdict = classify_job(job["title"])
    row = job_to_row(job, "Clinical Research/ Data Science", verdict)
    assert row["job_id"] == "J120239"
    assert row["source_category"] == "Clinical Research/ Data Science"
    assert row["category"] == "Non Clinical"
    assert row["sub_category"] == "Clinical Research"
    assert row["role_family"] == "Clinical Research"
    assert row["posted_date"] == "2026-08-20"
    assert row["salary_min_monthly"] == 30_000


def test_club_row_schema():
    rich = {"title": "Clinical Research Associate", "company": "Acme CRO",
            "location": "Pune, Maharashtra", "category": "Non Clinical",
            "sub_category": "Clinical Research", "job_type": "Full Time",
            "salary_min_monthly": "30000", "salary_max_monthly": "50000",
            "experience_min_years": "1", "experience_max_years": "3",
            "posted_date": "2026-08-20",
            "description": "Requires B.Pharm and GCP experience.",
            "job_url": "https://jobs.docthub.com/clinical-research-associate-J120239"}
    club = rich_row_to_club_row(rich)
    assert sorted(club) == sorted(CLUB_COLUMNS)
    assert len(CLUB_COLUMNS) == 22
    assert "is_active" not in club and "expires_at" not in club
    assert club["category"] == "Non Clinical"
    assert club["sub_category"] == "Clinical Research"
    assert club["min_salary"] == "30000"
    assert club["salary_period"] == "per_month"
    assert club["salary_currency"] == "INR"
    assert "B.Pharm" in club["qualification"]


def test_strip_html():
    assert strip_html("<p>Nurses &nbsp; and <b>GCP</b></p>") == "Nurses and GCP"
    assert strip_html(None) == ""


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
