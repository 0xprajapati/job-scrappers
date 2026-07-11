"""Unit tests for the apna.co salary parser and card extraction.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from apna_scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    card_salary,
    card_to_row,
    compute_cutoff,
    extract_detail_sections,
    parse_salary_string,
)


def test_salary_string_parsing():
    """Salary strings parse to (min, max) — captured, never filtered on."""
    cases = [
        ("₹60,000 - ₹80,000 monthly", (60_000, 80_000)),
        ("₹30,000 - ₹45,000 monthly", (30_000, 45_000)),
        ("₹18,000 - ₹40,000 monthly", (18_000, 40_000)),
        ("₹0 - ₹149,999 monthly",     (0, 149_999)),  # ₹0 floor = undisclosed
        ("₹149,999 - ₹149,999",       (149_999, 149_999)),
    ]
    for raw, expected in cases:
        assert parse_salary_string(raw) == expected, raw

    assert parse_salary_string("") is None
    assert parse_salary_string(None) is None


def test_card_salary_uses_fixed_range():
    """Stored salary fields hold the FIXED range, not incentive-inflated max."""
    card = {"min_salary": 100_000, "max_salary": 145_000,
            "fixed_min_salary": 100_000, "fixed_max_salary": 140_000,
            "earning_potential": 145_000}
    raw, fmin, fmax = card_salary(card)
    assert raw == "₹100,000 - ₹145,000 monthly"  # display range verbatim
    assert (fmin, fmax) == (100_000, 140_000)     # fixed range stored

    # Missing fixed split falls back to display values
    card = {"min_salary": 40_000, "max_salary": 50_000}
    assert card_salary(card)[1:] == (40_000, 50_000)

    # No salary at all
    assert card_salary({}) == ("", None, None)


def test_compute_cutoff():
    expected_first = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    assert compute_cutoff(None) == expected_first
    df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-06", "2026-06-20"]})
    expected = (date(2026, 7, 6) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
    assert compute_cutoff(df) == expected


def test_card_to_row():
    card = {
        "id": 17000155,
        "title": " Gastroenterologist ",
        "organization": {"name": "Zed Consultancy Service"},
        "location_name": "Punjabi Bagh, Delhi",
        "address": {"area": "LAL QUARTER", "city": {"name": "Delhi-NCR"}},
        "min_salary": 100_000, "max_salary": 149_999,
        "fixed_min_salary": 100_000, "fixed_max_salary": 149_999,
        "ui_tags": [{"text": "Work from Office"}, {"text": "Full Time"},
                    {"text": "Min. 5 years"}],
        "experience_in_years": "Min. 5 Years",
        "english": "Good English",
        "department": {"name": "Healthcare / Doctor / Hospital Staff"},
        "education": "Graduate",
        "shift": "day",
        "gender": None,
        "created_on": "2026-06-25T00:00:00.000+00:00",
        "description": "we want   hire\ngastroenterologist",
        "public_url": "https://apna.co/job/new-delhi/gastroenterologist-17000155",
    }
    row = card_to_row(card)
    assert row["source"] == "apna.co"
    assert row["job_id"] == "17000155"
    assert row["title"] == "Gastroenterologist"
    assert row["company"] == "Zed Consultancy Service"
    assert row["salary_raw"] == "₹100,000 - ₹149,999 monthly"
    assert row["salary_min_monthly"] == 100_000
    assert row["work_mode"] == "Work from Office"
    assert row["job_type"] == "Full Time"
    assert row["department"] == "Healthcare / Doctor / Hospital Staff"
    assert row["posted_date"] == "2026-06-25"
    assert row["description"] == "we want hire gastroenterologist"

    # minimal card must not crash; work mode/type fall back to booleans
    row = card_to_row({"id": 1, "is_wfh": True, "is_part_time": True})
    assert row["work_mode"] == "Work from Home"
    assert row["job_type"] == "Part Time"
    assert row["salary_min_monthly"] is None

    # external/aggregated postings: public_url is None, public_url_v2 and the
    # employer's real application link are set
    row = card_to_row({
        "id": 1731181830,
        "title": "Laboratory Technician",
        "is_external_job": True,
        "public_url": None,
        "public_url_v2": "https://apna.co/job/ahmedabad/technician-laboratory-medicine-1731181830",
        "external_job_url": "https://employer.example.com/job/42200",
    })
    assert row["job_url"].endswith("-1731181830")
    assert row["apply_url"] == "https://employer.example.com/job/42200"


def test_extract_detail_sections():
    html = ('... "job_details_section":[{"id":"job_role","heading":"Job role",'
            '"sub_heading":"","data":[{"title":"Role / Category",'
            '"subtitle":"Doctor"},{"title":"Shift","subtitle":"Day Shift"}]},'
            '{"heading":"Job requirements","data":[{"title":"Degree/ '
            'Specialisation","subtitle":"Any MBBS"},{"title":"Gender",'
            '"subtitle":"Any gender"}]},{"heading":"About company","data":'
            '[{"title":"Address","subtitle":"Punjabi Bagh, Delhi"}]}] ...')
    sections = extract_detail_sections(html)
    assert sections["Job role"]["Role / Category"] == "Doctor"
    assert sections["Job requirements"]["Degree/ Specialisation"] == "Any MBBS"
    assert sections["About company"]["Address"] == "Punjabi Bagh, Delhi"

    # escaped variant (as found inside RSC string chunks)
    escaped = html.replace('"', '\\"')
    assert extract_detail_sections(escaped)["Job role"]["Shift"] == "Day Shift"

    assert extract_detail_sections("<html>nothing here</html>") == {}


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
