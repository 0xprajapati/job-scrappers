"""Unit tests for the publichealthcareer parsers, taxonomy wiring and window.

Run with:  python test_filters.py   (or: pytest test_filters.py)
"""

from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    classify_company_type,
    compute_cutoff,
    map_job_type,
    parse_detail_page,
    parse_salary_text,
    rich_row_to_club_row,
)


def test_parse_salary_text():
    s = parse_salary_text("INR 20,000 per month")
    assert (s["salary_min"], s["salary_max"]) == (20000, 20000)
    assert s["salary_period"] == "per_month" and s["salary_currency"] == "INR"

    s = parse_salary_text("INR 50,000 - 70,000 per month")
    assert (s["salary_min"], s["salary_max"]) == (50000, 70000)

    s = parse_salary_text("USD 1,200 per annum")
    assert s["salary_currency"] == "USD" and s["salary_period"] == "per_annum"

    s = parse_salary_text("Rs. 35,000 monthly")
    assert s["salary_currency"] == "INR" and s["salary_min"] == 35000

    # captured, never invented: absent/N-A stays empty
    assert parse_salary_text("") == {}
    assert parse_salary_text("N/A") == {}
    assert parse_salary_text("Negotiable") == {}
    assert parse_salary_text("Competitive") == {}


def test_map_job_type():
    assert map_job_type("Full Time") == "full_time"
    assert map_job_type("Part Time") == "part_time"
    assert map_job_type("Remote") == "remote"
    assert map_job_type(None) == "full_time"


def test_apply_classification_in_scope():
    """Wiring: an in-scope row is stamped with the two-level taxonomy."""
    row = {"title": "Faculty - Epidemiology", "job_category_raw": "",
           "description": ""}
    assert apply_classification(row) is True
    assert row["category"] == "Public Health"
    assert row["sub_category"] == "Epidemiology"
    assert row["role_family"] == "Public Health"

    # The site's job_category term is a signal, not a decision: it reaches the
    # classifier as `skills`, weighted below the title. One term hit alone
    # (weight 2) sits under the keep threshold and admits nothing; two do.
    row = {"title": "Officer", "description": "",
           "job_category_raw": "Pharmacovigilance"}
    assert apply_classification(row) is False

    row = {"title": "Officer", "description": "",
           "job_category_raw": "Pharmacovigilance and Drug Safety"}
    assert apply_classification(row) is True
    assert row["category"] == "Non Clinical"
    assert row["sub_category"] == "Pharmacovigilance"
    assert row["matched_in"] == "skills"


def test_apply_classification_drops_out_of_scope():
    """Wiring: bedside/clinical posts are dropped, not relabelled."""
    for title in ("Staff Nurse", "Clinical Pharmacist",
                  "Project Research Scientist-III (Medical Officer)"):
        row = {"title": title, "job_category_raw": "", "description": ""}
        assert apply_classification(row) is False, title
        assert row["category"] == ""
        assert row["sub_category"] == ""


def test_company_type():
    assert classify_company_type("Wadhwani AI") == "hospital"  # schema default
    assert classify_company_type("Sun Pharma Research Labs") == "pharma"


def test_parse_detail_page():
    page = '''
    <aside>
    <div class="pxp-single-job-side-info-label pxp-text-light">Experience</div>
    <div class="pxp-single-job-side-info-data">N/A</div>
    <div class="pxp-single-job-side-info-label pxp-text-light">Employment Type</div>
    <div class="pxp-single-job-side-info-data">Full Time</div>
    <div class="pxp-single-job-side-info-label pxp-text-light">Salary</div>
    <div class="pxp-single-job-side-info-data">INR 20,000 per month</div>
    <div class="pxp-single-job-side-info-label pxp-text-light">Website</div>
    <div class="pxp-single-job-side-info-data">https://www.wadhwaniai.org/</div>
    </aside>
    <script type="application/ld+json">{
        "hiringOrganization": {
            "@type": "Organization",
            "name": "Wadhwani AI ",
            "sameAs": "https://www.wadhwaniai.org"
        }
    }</script>'''
    d = parse_detail_page(page)
    assert d["company"] == "Wadhwani AI"
    assert d["salary_min"] == 20000 and d["salary_currency"] == "INR"
    assert d["job_type"] == "full_time"
    assert d["company_website"].startswith("https://www.wadhwaniai.org")
    assert "experience_raw" not in d  # N/A is dropped

    assert parse_detail_page("<html>bare page</html>") == {}


def test_club_row_currency_gate():
    base = {"country": "India", "city": "Delhi", "company": "X", "title": "T",
            "description": "d", "job_type": "full_time",
            "category": "Public Health", "sub_category": "Epidemiology",
            "company_type": "hospital", "job_url": "u", "posted_date": "2026-07-01"}
    # INR populates
    r = rich_row_to_club_row({**base, "salary_min": 20000, "salary_max": 30000,
                              "salary_period": "per_month", "salary_currency": "INR"})
    assert (r["min_salary"], r["max_salary"], r["salary_currency"]) == ("20000", "30000", "INR")
    # unsupported currency stays blank (nothing invented)
    r = rich_row_to_club_row({**base, "salary_min": 4000, "salary_max": 9000,
                              "salary_period": "per_month", "salary_currency": "AED"})
    assert (r["min_salary"], r["max_salary"], r["salary_currency"]) == ("", "", "")


def test_compute_cutoff():
    expected_first = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
    assert compute_cutoff(None) == expected_first
    df = pd.DataFrame({"posted_date": ["2026-07-23", "2026-07-07", "2026-06-18"]})
    expected = (date(2026, 7, 23) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
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
