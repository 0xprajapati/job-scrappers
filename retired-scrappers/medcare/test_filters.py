#!/usr/bin/env python3
"""Unit tests for the medcare scraper's parsers — run with `python test_filters.py`.

All example titles are real requisitions from the Oracle CX portal
(organization "Medcare Medical Hospitals and Medical Centres").
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    apply_classification, clean_text, compute_cutoff, display_title,
    map_job_type, rich_row_to_club_row, split_title, strip_html,
    within_window, CLUB_COLUMNS, WATERMARK_GRACE_DAYS,
)


class TestSplitTitle(unittest.TestCase):
    def test_role_dept_facility(self):
        role, dept, facility = split_title(
            "Registered Nurse.Endoscopy.Medcare Hospital Sharjah (Br)")
        self.assertEqual(role, "Registered Nurse")
        self.assertEqual(dept, "Endoscopy")
        self.assertEqual(facility, "Medcare Hospital Sharjah")

    def test_legal_suffix_stripped(self):
        _, _, facility = split_title(
            "Technician.Cardiology.Medcare Royal Speciality Hospital "
            "(Br of Aster DM Healthcare)")
        self.assertEqual(facility, "Medcare Royal Speciality Hospital")

    def test_run_together_facility_respaced(self):
        role, dept, facility = split_title("Audiologist.ENT.MedcareHospitalSharjah(Br)")
        self.assertEqual(role, "Audiologist")
        self.assertEqual(dept, "ENT")
        self.assertEqual(facility, "Medcare Hospital Sharjah")

    def test_for_facility_shape(self):
        role, dept, facility = split_title(
            "Cast Technician for Medcare Orthopedics and Spine Hospital (Br)")
        self.assertEqual(role, "Cast Technician")
        self.assertEqual(facility, "Medcare Orthopedics and Spine Hospital")

    def test_head_dash_shape(self):
        role, _, facility = split_title("Head - Endoscopy for Medcare Hospital")
        self.assertEqual(role, "Head - Endoscopy")
        self.assertEqual(facility, "Medcare Hospital")

    def test_plain_title(self):
        self.assertEqual(split_title("Medical Coder"), ("Medical Coder", "", ""))

    def test_llc_dots_are_not_separators(self):
        role, dept, facility = split_title(
            "Functional Nutritionist for Wellth By Medcare"
            "(Br of Medcare Hospital (L.L.C))")
        self.assertEqual(role, "Functional Nutritionist")
        self.assertEqual(dept, "")
        self.assertEqual(facility, "Wellth By Medcare")

    def test_display_title_skips_redundant_department(self):
        self.assertEqual(display_title("Registered Nurse", "Endoscopy"),
                         "Registered Nurse - Endoscopy")
        self.assertEqual(display_title("Charge Nurse", "Nursing Services"),
                         "Charge Nurse - Nursing Services")
        self.assertEqual(display_title("Medical Coder", ""), "Medical Coder")


class TestClassificationWiring(unittest.TestCase):
    """The engine itself is tested in _shared/test_classification.py; these
    only prove this scraper wires its fields into it correctly."""

    def _row(self, title, category_original="", department="", description=""):
        return {"title": title, "category_original": category_original,
                "department": department, "description": description}

    def test_in_scope_role_gets_category_and_sub_category(self):
        row = self._row("Medical Coder", "Enabling & Support")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")
        self.assertEqual(row["role_family"], "Medical Coding")
        self.assertTrue(row["matched_in"])

    def test_public_health_role(self):
        row = self._row("Infection Control Nurse", "Nursing")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_clinical_title_is_dropped(self):
        row = self._row("General Practitioner - Accident, Emergency And Trauma",
                        "Clinicians", "Accident, Emergency And Trauma")
        self.assertFalse(apply_classification(row))
        self.assertEqual(row["category"], "")

    def test_bedside_nurse_is_dropped_despite_oracle_facet(self):
        # the Oracle facet is only a signal now — it can never admit a job
        row = self._row("Registered Nurse - Endoscopy", "Nursing", "Endoscopy")
        self.assertFalse(apply_classification(row))


class TestClubRow(unittest.TestCase):
    def test_club_row_shape_and_qualification(self):
        row = self._make_row()
        club = rich_row_to_club_row(row, company_about="About Medcare")
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertNotIn("is_active", club)
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Medical Coding")
        # structured StudyLevel wins over description extraction
        self.assertEqual(club["qualification"], "Bachelors Degree")
        self.assertEqual(club["company_name"], "Medcare Hospital Sharjah")
        self.assertEqual(club["min_salary"], "")

    def _make_row(self):
        row = {"title": "Medical Coder", "category_original": "Enabling & Support",
               "department": "", "description": "MBBS holders may apply.",
               "facility": "Medcare Hospital Sharjah", "city": "Sharjah",
               "country": "United Arab Emirates", "job_type": "full_time",
               "study_level": "Bachelors Degree", "job_url": "https://x/1",
               "posted_date": "2025-01-02"}
        apply_classification(row)
        return row


class TestJobType(unittest.TestCase):
    def test_schedules(self):
        self.assertEqual(map_job_type("Full time"), "full_time")
        self.assertEqual(map_job_type("Part time"), "part_time")
        self.assertEqual(map_job_type(None), "full_time")


class TestCutoff(unittest.TestCase):
    def test_first_run_keeps_all_open_requisitions(self):
        self.assertIsNone(compute_cutoff(None))
        self.assertTrue(within_window("2020-11-29", None))

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2024-09-18", "2022-03-03"]})
        expected = (date(2024, 9, 18)
                    - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)
        self.assertTrue(within_window("2024-09-18", expected))
        self.assertFalse(within_window("2022-03-03", expected))
        self.assertFalse(within_window("", expected))

    def test_unparseable_dates_fall_back_to_keep_all(self):
        df = pd.DataFrame({"posted_date": ["not-a-date", ""]})
        self.assertIsNone(compute_cutoff(df))


class TestTextCleaning(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(
            strip_html("<p>Your Responsibilities:<br/>Perform &amp; assist</p>"),
            "Your Responsibilities: Perform & assist")

    def test_clean_text_whitespace_and_entities(self):
        self.assertEqual(clean_text("  Registered  Nurse &amp; Midwife \n"),
                         "Registered Nurse & Midwife")


if __name__ == "__main__":
    unittest.main(verbosity=2)
