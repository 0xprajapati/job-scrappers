"""Unit tests for the dubailivejobs parsers, classifier and time window.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    classify_category,
    classify_company_type,
    compute_cutoff,
    detail_from_posting,
    extract_job_posting,
    parse_title,
)


def test_parse_title():
    t, c = parse_title("Danat Al Emarat Hospital Careers &#8211; Staff Required Urgently")
    assert c == "Danat Al Emarat Hospital", (t, c)
    t, c = parse_title("Aster Pharmacy Careers 2026 &#8211; 100% Hiring Started")
    assert c == "Aster Pharmacy"
    t, c = parse_title("Dubai London Clinic Jobs In Dubai || 100% Free Hiring")
    assert c == "Dubai London Clinic"


def test_classify_category():
    assert classify_category("Aster Pharmacy Careers") == ("pharmacists", False)
    assert classify_category("Nursing Staff Careers") == ("nurses", False)
    assert classify_category("Dr Samir Abbas Hospital - Physician") == ("doctors", False)
    # a whole-hospital careers page names no specific role -> non_clinical + review
    cat, review = classify_category("Danat Al Emarat Hospital")
    assert cat == "non_clinical" and review is True


def test_company_type():
    assert classify_company_type("Danat Al Emarat Hospital", "x") == "hospital"
    assert classify_company_type("Aster Pharmacy", "x") == "pharma"
    assert classify_company_type("City Diagnostic Laboratory", "x") == "pharma"


def test_extract_and_detail_jsonld():
    page = ('<html><head>'
            '<script type="application/ld+json">{"@type":"WebPage"}</script>'
            '<script type="application/ld+json">{"@context":"x","@graph":['
            '{"@type":"BreadcrumbList"},'
            '{"@type":"JobPosting","title":"Hospital Careers","baseSalary":'
            '{"@type":"MonetaryAmount","currency":"AED","value":'
            '{"@type":"QuantitativeValue","value":"4000 - 20000","unitText":"MONTH"}},'
            '"employmentType":["FULL_TIME","PART_TIME"],"jobLocation":'
            '{"@type":"Place","address":{"@type":"PostalAddress",'
            '"addressRegion":"Dubai","addressCountry":"United Arab Emirates"}},'
            '"validThrough":"2026-08-31T00:00:00"}]}</script></head></html>')
    posting = extract_job_posting(page)
    assert posting.get("title") == "Hospital Careers"
    d = detail_from_posting(posting)
    assert d["salary_raw"] == "4000 - 20000"
    assert d["salary_currency_original"] == "AED"
    assert d["salary_period"] == "per_month"
    assert d["job_type"] == "full_time"
    assert d["city"] == "Dubai"
    assert d["expires_at"] == "2026-08-31"
    assert extract_job_posting("<html>no ld</html>") == {}


def test_compute_cutoff():
    expected_first = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    assert compute_cutoff(None) == expected_first
    df = pd.DataFrame({"posted_date": ["2026-07-13", "2026-07-04", "2026-06-20"]})
    expected = (date(2026, 7, 13) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
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
