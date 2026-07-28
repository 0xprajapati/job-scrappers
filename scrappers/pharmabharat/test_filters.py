#!/usr/bin/env python3
"""Unit tests for the pharmabharat.com scraper's parsers.

Run with plain:  python test_filters.py
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    classify_category,
    classify_company_type,
    classify_job_type,
    compute_cutoff,
    extract_labeled_fields,
    parse_deadline,
    parse_experience,
    parse_location,
    parse_salary,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
)


class TestExtractLabeledFields(unittest.TestCase):
    # Real shape from post 68064 (Sandoz Artwork Operations, 2026-07-24).
    TABLE_HTML = """
    <h1>Job Details</h1>
    <figure class="wp-block-table"><table><thead>
    <tr><th>Particular</th><th>Details</th></tr></thead><tbody>
    <tr><td>Company</td><td>Sandoz</td></tr>
    <tr><td>Job Roles</td><td>Analyst &#8211; Artwork Operations</td></tr>
    <tr><td>Location</td><td>Telangana, India</td></tr>
    <tr><td>Job Type</td><td>Full-Time</td></tr>
    <tr><td>Experience</td><td>2&#8211;4 Years (Analyst)</td></tr>
    <tr><td>Application Deadline</td><td>28 July 2026</td></tr>
    </tbody></table></figure>
    """

    # Real shape from posts using labelled bullets.
    LI_HTML = """
    <ul>
      <li><strong>Company:</strong> Mitocon Biopharma</li>
      <li><strong>Experience:</strong> Freshers</li>
      <li><strong>Estimated Salary:</strong> ₹2.5 LPA – ₹10 LPA (depending on qualification)</li>
    </ul>
    """

    # Real shape from post 67962 (Indoco Remedies) — labelled paragraphs
    # where the value sits on the line after the label.
    PARA_HTML = """
    <h2>Job Overview</h2>
    <p><strong>Company Name:</strong></p><p>Indoco Remedies Limited</p>
    <p><strong>Position:</strong></p><p>Officer / Sr. Officer</p>
    <p><strong>Job Type:</strong></p><p>Walk-In Interview</p>
    <p><strong>Job Location:</strong></p><p>Baddi, Himachal Pradesh, India</p>
    """

    def test_table_rows(self):
        fields = extract_labeled_fields(self.TABLE_HTML)
        self.assertEqual(fields["company"], "Sandoz")
        self.assertEqual(fields["position"], "Analyst – Artwork Operations")
        self.assertEqual(fields["location"], "Telangana, India")
        self.assertEqual(fields["work_type"], "Full-Time")
        self.assertEqual(fields["deadline"], "28 July 2026")

    def test_labeled_bullets(self):
        fields = extract_labeled_fields(self.LI_HTML)
        self.assertEqual(fields["company"], "Mitocon Biopharma")
        self.assertEqual(fields["experience"], "Freshers")
        self.assertIn("2.5 LPA", fields["salary"])

    def test_labeled_paragraphs_value_on_next_line(self):
        fields = extract_labeled_fields(self.PARA_HTML)
        self.assertEqual(fields["company"], "Indoco Remedies Limited")
        self.assertEqual(fields["position"], "Officer / Sr. Officer")
        self.assertEqual(fields["location"], "Baddi, Himachal Pradesh, India")

    def test_table_wins_over_later_text(self):
        fields = extract_labeled_fields(
            self.TABLE_HTML + "<p><strong>Location:</strong> Somewhere Else</p>")
        self.assertEqual(fields["location"], "Telangana, India")

    def test_prose_salary_estimate_is_skipped(self):
        # "Salary" headings followed by the site's own prose must not be
        # captured as a salary value (master spec: never invent).
        html = ("<p>Salary:</p><p>Based on similar roles, the expected "
                "salary is competitive.</p>")
        self.assertNotIn("salary", extract_labeled_fields(html))

    def test_next_label_line_is_not_a_value(self):
        html = "<p><strong>Company:</strong></p><p><strong>Location:</strong> Pune</p>"
        fields = extract_labeled_fields(html)
        self.assertNotIn("company", fields)
        self.assertEqual(fields["location"], "Pune")

    def test_empty(self):
        self.assertEqual(extract_labeled_fields(""), {})
        self.assertEqual(extract_labeled_fields(None), {})


class TestParseSalary(unittest.TestCase):
    def test_empty_returns_empty_dict(self):
        self.assertEqual(parse_salary(""), {})
        self.assertEqual(parse_salary(None), {})

    def test_no_numbers_keeps_raw_only(self):
        result = parse_salary("Best in Industry")
        self.assertEqual(result["salary_raw"], "Best in Industry")
        self.assertNotIn("salary_min", result)

    def test_estimated_lpa_range(self):
        # Real shape from a July 2026 post.
        result = parse_salary("₹6.5 – ₹10 LPA (Estimated)")
        self.assertEqual(result["salary_min"], 650_000)
        self.assertEqual(result["salary_max"], 1_000_000)
        self.assertEqual(result["salary_period"], "per_annum")
        self.assertEqual(result["salary_currency"], "INR")

    def test_parenthetical_numbers_ignored(self):
        # "(based on 2025 standards)" must not be read as an amount.
        result = parse_salary("₹2.8 LPA – ₹4.2 LPA (based on 2025 standards)")
        self.assertEqual(result["salary_min"], 280_000)
        self.assertEqual(result["salary_max"], 420_000)

    def test_monthly_ctc(self):
        # Real shape: "Salary After Training" table row.
        result = parse_salary("₹21,900 CTC per Month")
        self.assertEqual(result["salary_min"], 21_900)
        self.assertEqual(result["salary_period"], "per_month")

    def test_indian_grouping_per_annum(self):
        result = parse_salary("₹8,00,000 – ₹13,00,000 per annum (CTC)")
        self.assertEqual(result["salary_min"], 800_000)
        self.assertEqual(result["salary_max"], 1_300_000)
        self.assertEqual(result["salary_period"], "per_annum")

    def test_k_suffix_range(self):
        result = parse_salary("15k-20k")
        self.assertEqual(result["salary_min"], 15_000)
        self.assertEqual(result["salary_max"], 20_000)
        self.assertEqual(result["salary_period"], "per_month")

    def test_swapped_range_is_ordered(self):
        result = parse_salary("5 - 3 LPA")
        self.assertEqual(result["salary_min"], 300_000)
        self.assertEqual(result["salary_max"], 500_000)

    def test_foreign_currency_keeps_raw_only(self):
        result = parse_salary("AED 5,000 per month")
        self.assertIn("salary_raw", result)
        self.assertNotIn("salary_min", result)


class TestParseExperience(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_experience("2–6 Years"), (2, 6))

    def test_range_with_parenthetical(self):
        # Real shape from post 68064.
        self.assertEqual(
            parse_experience("2–4 Years (Analyst), 5–8 Years (Specialist)"),
            (2, 4))

    def test_fresher_only(self):
        self.assertEqual(parse_experience("Freshers"), (0, 0))

    def test_freshers_to_n_years(self):
        self.assertEqual(
            parse_experience("Freshers (2026 pass-outs) to 8 Years"), (0, 8))

    def test_min_years(self):
        self.assertEqual(parse_experience("Min 3 years"), (3, ""))

    def test_plus_years(self):
        self.assertEqual(parse_experience("5+ years"), (5, ""))

    def test_empty(self):
        self.assertEqual(parse_experience(""), ("", ""))

    def test_unparseable(self):
        self.assertEqual(parse_experience("As per role"), ("", ""))


class TestParseDeadline(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse_deadline("28 July 2026"), "2026-07-28")

    def test_ordinal_and_weekday(self):
        self.assertEqual(parse_deadline("26th July, 2026 (Sunday)"), "2026-07-26")

    def test_unparseable(self):
        self.assertEqual(parse_deadline("As soon as possible"), "")
        self.assertEqual(parse_deadline(""), "")


class TestClassifiers(unittest.TestCase):
    def test_production_roles_are_non_clinical(self):
        category, review = classify_category(
            "Glenmark Pharma is Hiring for Production, QA & QC Jobs")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(review)

    def test_pharmacist_title(self):
        self.assertEqual(classify_category("Pharmacist Vacancy at AIIMS")[0],
                         "pharmacists")

    def test_pharmacist_site_category(self):
        self.assertEqual(
            classify_category("Hospital Vacancy 2026", ["pharmacist"])[0],
            "pharmacists")

    def test_doctor_title(self):
        self.assertEqual(classify_category("Medical Officer Vacancy")[0],
                         "doctors")

    def test_nurse_title(self):
        self.assertEqual(classify_category("Staff Nurse Openings in Mumbai")[0],
                         "nurses")

    def test_newsy_title_flagged(self):
        self.assertTrue(classify_category("Top 10 Pharma Companies in India")[1])

    def test_company_type_defaults_to_pharma(self):
        self.assertEqual(classify_company_type("Macleods Pharmaceuticals", ""),
                         "pharma")

    def test_hospital_company(self):
        self.assertEqual(classify_company_type("Apollo Hospital", ""), "hospital")

    def test_job_type_mapping(self):
        self.assertEqual(classify_job_type("Full-Time"), "full_time")
        self.assertEqual(classify_job_type("Walk-In Interview"), "full_time")
        self.assertEqual(classify_job_type("Full-Time", "Work From Office"),
                         "full_time")
        self.assertEqual(classify_job_type("Full-Time", "Remote"), "remote")
        self.assertEqual(classify_job_type("Hybrid"), "hybrid")
        self.assertEqual(classify_job_type("Part-time"), "part_time")
        self.assertEqual(classify_job_type("Internship"), "internship")
        self.assertEqual(classify_job_type(""), "full_time")


class TestParseLocation(unittest.TestCase):
    def test_city_state(self):
        self.assertEqual(parse_location("Mumbai, Maharashtra"),
                         ("Mumbai", "India", "IN", "+91"))

    def test_state_india(self):
        self.assertEqual(parse_location("Telangana, India"),
                         ("Telangana", "India", "IN", "+91"))

    def test_city_state_country(self):
        self.assertEqual(parse_location("Baddi, Himachal Pradesh, India"),
                         ("Baddi", "India", "IN", "+91"))

    def test_abroad(self):
        self.assertEqual(parse_location("Dubai, UAE"),
                         ("Dubai", "UAE", "AE", "+971"))

    def test_not_specified(self):
        self.assertEqual(parse_location("Not specified"),
                         ("", "India", "IN", "+91"))

    def test_empty(self):
        self.assertEqual(parse_location(""), ("", "India", "IN", "+91"))


class TestComputeCutoff(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-20", "bogus"]})
        expected = (date(2026, 7, 20) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_all_bogus_dates_fall_back_to_initial_window(self):
        df = pd.DataFrame({"posted_date": ["bogus", ""]})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
