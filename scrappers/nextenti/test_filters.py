"""Unit tests for the nextenti parsers, classifier and time window.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from scraper import (
    CLUB_COLUMNS,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_job_url,
    classify_company_type,
    compute_cutoff,
    map_job_type,
    parse_range,
    parse_salary,
    rich_row_to_club_row,
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


def _row(title, profession="", description=""):
    return {"title": title, "profession": profession,
            "description": description, "category": "", "sub_category": "",
            "role_family": "", "all_families": "", "family_scores": "",
            "family_confidence": "", "matched_in": "", "needs_review": ""}


def test_shared_classifier_wiring_in_scope():
    # the wiring only — the engine is covered by _shared/test_classification
    row = _row("Clinical Research Coordinator", "Others")
    verdict = apply_classification(row)
    assert verdict["in_scope"] is True
    assert row["category"] == "Non Clinical"
    assert row["sub_category"] == "Clinical Research"
    assert row["role_family"] == "Clinical Research"
    assert row["needs_review"] in ("true", "false")


def test_shared_classifier_wiring_out_of_scope():
    # a clinical title drops even when `profession` says Doctor: the raw
    # profession field is only a skills signal, it no longer decides anything
    for title, profession in (("Consultant Cardiologist", "Doctor"),
                              ("Staff Nurse ICU", "Nurse"),
                              ("Zonal Head", "")):
        verdict = apply_classification(_row(title, profession))
        assert verdict["in_scope"] is False, title
        assert verdict["category"] == ""


def test_profession_travels_as_skills_signal():
    # profession corroborates a weak title through the skills channel
    weak = apply_classification(_row("Senior Associate"))
    assert weak["in_scope"] is False
    helped = apply_classification(
        _row("Senior Associate", "Pharmacovigilance",
             "ICSR case processing and signal detection."))
    assert helped["in_scope"] is True
    assert helped["role_family"] == "Pharmacovigilance"


def test_club_row_matches_shared_contract():
    row = _row("Clinical Research Coordinator", "Others",
               "Requires B.Pharm and GCP knowledge.")
    apply_classification(row)
    club = rich_row_to_club_row(dict(row, country="India", city="Pune",
                                     salary_min=15000, salary_max=30000,
                                     salary_period="per_month"))
    assert sorted(club) == sorted(CLUB_COLUMNS)
    assert len(CLUB_COLUMNS) == 23
    assert "is_active" not in club and "expires_at" not in club
    assert club["category"] == "Non Clinical"
    assert club["sub_category"] == "Clinical Research"
    assert "B.Pharm" in club["qualification"]


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
