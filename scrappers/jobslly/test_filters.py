"""Unit tests for the jobslly salary normalizer and title classifier.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from jobslly_scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    classify_title,
    compute_cutoff,
    extract_job_posting,
    normalize_base_salary,
)


def _base(min_v, max_v, unit, currency="INR"):
    return {"@type": "MonetaryAmount", "currency": currency,
            "value": {"@type": "QuantitativeValue",
                      "minValue": min_v, "maxValue": max_v, "unitText": unit}}


def test_lakh_per_year_salaries():
    """Jobslly publishes yearly salaries in lakhs (18 means 18 LPA)."""
    assert normalize_base_salary(_base(18, 25, "YEAR"))[:3] == (150_000, 208_333, "LPA")
    assert normalize_base_salary(_base(7.6, 15, "YEAR"))[:3] == (63_333, 125_000, "LPA")
    assert normalize_base_salary(_base(3.6, 4.8, "YEAR"))[:3] == (30_000, 40_000, "LPA")
    assert normalize_base_salary(_base(3, 5, "YEAR"))[0] == 25_000


def test_raw_inr_and_monthly_salaries():
    # Values >= 1000 are raw INR, not lakhs
    assert normalize_base_salary(_base(600_000, 1_200_000, "YEAR"))[:3] == (50_000, 100_000, "P.A")
    assert normalize_base_salary(_base(45_000, 45_000, "MONTH"))[:3] == (45_000, 45_000, "P.M")
    # Monthly in lakhs (2 = 2L/month)
    assert normalize_base_salary(_base(2, 2.5, "MONTH"))[:3] == (200_000, 250_000, "P.M")


def test_missing_or_partial_salaries():
    assert normalize_base_salary(None) is None
    assert normalize_base_salary({}) is None
    assert normalize_base_salary(_base(None, None, "YEAR")) is None
    # "up to X" with no lower bound stays undisclosed (blank fields, job kept)
    assert normalize_base_salary(_base(None, 25, "YEAR")) is None
    # single value: min == max
    assert normalize_base_salary(_base(18, None, "YEAR"))[:3] == (150_000, 150_000, "LPA")
    # unknown unit -> unparseable
    assert normalize_base_salary(_base(18, 25, "HOUR")) is None


def test_compute_cutoff():
    expected_first = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    assert compute_cutoff(None) == expected_first
    df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-06", "2026-06-20"]})
    expected = (date(2026, 7, 6) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
    assert compute_cutoff(df) == expected


def test_title_classifier():
    for title in ("Medical Affairs Professional", "Senior Medical Regulatory Writer",
                  "Expert Scientific Writer (HEVA)", "Staff Nurse (ICU)",
                  "Pharmacovigilance Associate", "Consultant Gynecologist",
                  "Drug Safety Physician", "Clinical Data Associate"):
        assert classify_title(title) == "allow", title

    for title in ("Admissions Counselor / Sales Consultant", "HR Executive",
                  "Software Engineer", "Digital Marketing Manager"):
        assert classify_title(title) == "deny", title

    assert classify_title("Global Program Lead") == "unknown"


def test_extract_job_posting():
    html = """<html><head>
    <script type="application/ld+json">{"@type":"BreadcrumbList"}</script>
    <script type="application/ld+json">{"@context":"https://schema.org",
      "@type":"JobPosting","title":"Pharmacist","baseSalary":{"currency":"INR",
      "value":{"minValue":4,"maxValue":6,"unitText":"YEAR"}}}</script>
    <script type="application/ld+json">not json at all</script>
    </head><body></body></html>"""
    posting = extract_job_posting(html)
    assert posting is not None and posting["title"] == "Pharmacist"
    assert normalize_base_salary(posting["baseSalary"])[:3] == (33_333, 50_000, "LPA")
    assert extract_job_posting("<html><body>no ld+json</body></html>") is None


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
