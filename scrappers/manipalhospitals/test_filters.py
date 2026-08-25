#!/usr/bin/env python3
"""Unit tests for the manipalhospitals scraper's parsers and filters.

Run with plain `python test_filters.py`.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    apply_classification,
    classification_skills,
    compute_cutoff,
    epoch_ms_to_date,
    parse_experience,
    parse_job_salary,
    rich_row_to_club_row,
    strip_html,
    CLUB_COLUMNS,
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


class TestClassificationWiring(unittest.TestCase):
    """The scraper delegates every keep/drop + label decision to
    _shared/classification.py; these tests check the wiring only (the engine
    itself is covered by _shared/test_classification.py). Titles are from
    the live index (2026-07)."""

    @staticmethod
    def _row(title, department="", skills="", description=""):
        return {"title": title, "department": department, "skills": skills,
                "description": description}

    def test_skills_signal_joins_department_and_tags(self):
        row = self._row("Clinical Research Associate", "Clinical Research",
                        "GCP; CRF")
        self.assertEqual(classification_skills(row),
                         "Clinical Research GCP; CRF")

    def test_in_scope_role_is_labelled(self):
        row = self._row("Clinical Research Associate", "Clinical Research")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")

    def test_public_health_role_is_labelled(self):
        row = self._row("Infection Control Nurse", "Infection Control")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_bedside_nursing_is_dropped(self):
        row = self._row("Senior Nurse - ICU", "ICU (Intensive care Unit)")
        self.assertFalse(apply_classification(row))
        self.assertEqual(row["category"], "")

    def test_corporate_and_paramedical_titles_are_dropped(self):
        for title, dept in (("Consultant - HR", "Human Resources"),
                            ("Finance Head - Pune", "Unit Finance Controlling"),
                            ("Operation Theatre Technician", "OT")):
            self.assertFalse(apply_classification(self._row(title, dept)),
                             title)

    def test_club_row_uses_shared_columns(self):
        row = self._row("Clinical Research Associate", "Clinical Research",
                        description="MBBS preferred; coordinates trials.")
        apply_classification(row)
        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        self.assertIn("MBBS", club["qualification"])
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)


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
