#!/usr/bin/env python3
"""Unit tests for the manipalhospitals scraper's parsers and filters.

Run with plain `python test_filters.py`.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    classify_category,
    compute_cutoff,
    epoch_ms_to_date,
    parse_experience,
    parse_job_salary,
    strip_html,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
)


class TestSalaryParser(unittest.TestCase):
    """Worked examples from real detail responses."""

    def test_senior_icu_nurse_annual_range(self):
        # senior-nurse-mysuru-2024100706233613: 324000 / 360000
        parsed = parse_job_salary(324000, 360000)
        self.assertEqual(parsed["salary_min"], 324000)
        self.assertEqual(parsed["salary_max"], 360000)
        self.assertEqual(parsed["salary_period"], "per_annum")
        self.assertEqual(parsed["salary_currency"], "INR")
        self.assertEqual(parsed["salary_raw"], "INR 324,000 - 360,000 per year")

    def test_undisclosed_empty_strings(self):
        # manager-finance-accounts-pune-2026070812414673: "" / ""
        self.assertEqual(parse_job_salary("", ""), {})
        self.assertEqual(parse_job_salary(None, None), {})
        self.assertEqual(parse_job_salary(0, 0), {})

    def test_single_bound_fills_both(self):
        parsed = parse_job_salary(500000, None)
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         (500000, 500000))

    def test_swapped_bounds_are_reordered(self):
        parsed = parse_job_salary(360000, 324000)
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         (324000, 360000))

    def test_small_amounts_read_as_monthly(self):
        parsed = parse_job_salary(25000, 32000)
        self.assertEqual(parsed["salary_period"], "per_month")
        self.assertEqual(parsed["salary_raw"], "INR 25,000 - 32,000 per month")

    def test_string_numbers_accepted(self):
        parsed = parse_job_salary("324000", "360000.0")
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         (324000, 360000))


class TestClassifier(unittest.TestCase):
    """Titles from the live index (2026-07)."""

    def test_nurse_titles(self):
        for title in ("Senior Nurse - ICU", "Nurse - Emergency-AER",
                      "Staff Nurse", "Nursing Superintendent"):
            category, needs_review = classify_category(title)
            self.assertEqual(category, "nurses", title)
            self.assertFalse(needs_review, title)

    def test_nurse_by_department(self):
        category, _ = classify_category("Sister In-charge",
                                        "Nursing Administration")
        self.assertEqual(category, "nurses")

    def test_pharmacist(self):
        category, _ = classify_category("Clinical Pharmacist")
        self.assertEqual(category, "pharmacists")
        category, _ = classify_category("Pharmacy Incharge", "Pharmacy")
        self.assertEqual(category, "pharmacists")

    def test_doctor_titles(self):
        for title in ("Consultant - Radiology", "Medical Officer",
                      "Anaesthetist", "Intensivist - Critical Care"):
            category, _ = classify_category(title)
            self.assertEqual(category, "doctors", title)

    def test_hr_consultant_is_not_a_doctor(self):
        category, _ = classify_category("Consultant - HR", "Human Resources")
        self.assertEqual(category, "non_clinical")

    def test_ot_technician_clinical_support(self):
        # "Kanakapura Road - Operation Theatre Technician" (live listing)
        category, needs_review = classify_category(
            "Operation Theatre Technician", "OT")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(needs_review)

    def test_corporate_title_flagged(self):
        # "Finance Head - Pune" (live listing): kept, but flagged
        category, needs_review = classify_category(
            "Finance Head - Pune", "Unit Finance Controlling")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)

    def test_clinical_department_clears_flag(self):
        _, needs_review = classify_category(
            "Executive", "ICU (Intensive care Unit)")
        self.assertFalse(needs_review)


class TestExperienceParser(unittest.TestCase):

    def test_live_listing_values(self):
        # Finance Head - Pune: minYearOfExperience 5, maxYearOfExperience 10
        self.assertEqual(parse_experience(
            {"minYearOfExperience": 5, "maxYearOfExperience": 10}), ("5", "10"))

    def test_missing_values(self):
        self.assertEqual(parse_experience({}), ("", ""))
        self.assertEqual(parse_experience(
            {"minYearOfExperience": None, "maxYearOfExperience": "x"}), ("", ""))


class TestDateConversion(unittest.TestCase):

    def test_live_epoch(self):
        # Finance Head - Pune createdDate
        self.assertEqual(epoch_ms_to_date(1783482667000), "2026-07-08")

    def test_garbage(self):
        self.assertEqual(epoch_ms_to_date(None), "")
        self.assertEqual(epoch_ms_to_date("not-a-number"), "")


class TestCutoff(unittest.TestCase):

    def test_first_run_uses_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-15", ""]})
        expected = (date(2026, 7, 15) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_unparseable_dates_fall_back(self):
        df = pd.DataFrame({"posted_date": ["", "n/a"]})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


class TestStripHtml(unittest.TestCase):

    def test_nbsp_heavy_description(self):
        # the detail API glues words with &nbsp;
        markup = ("<p><strong><u>About&nbsp;the&nbsp;Job:</u></strong></p>"
                  "<p>We&nbsp;are&nbsp;looking</p>")
        self.assertEqual(strip_html(markup), "About the Job: We are looking")


if __name__ == "__main__":
    unittest.main(verbosity=2)
