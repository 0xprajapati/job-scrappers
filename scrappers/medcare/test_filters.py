#!/usr/bin/env python3
"""Unit tests for the medcare scraper's parsers — run with `python test_filters.py`.

All example titles are real requisitions from the Oracle CX portal
(organization "Medcare Medical Hospitals and Medical Centres").
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    classify_category, clean_text, compute_cutoff, display_title,
    map_job_type, split_title, strip_html, within_window,
    WATERMARK_GRACE_DAYS,
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


class TestClassifier(unittest.TestCase):
    def test_oracle_category_is_authoritative(self):
        self.assertEqual(classify_category("Head - Endoscopy for Medcare Hospital",
                                           "Nursing"), ("nurses", False))
        self.assertEqual(classify_category("General Practitioner.Accident, Emergency "
                                           "And Trauma.Medcare Hospital Sharjah (Br)",
                                           "Clinicians"), ("doctors", False))
        self.assertEqual(classify_category("Senior Pharmacist",
                                           "Senior Pharmacist"), ("pharmacists", False))
        self.assertEqual(classify_category("Associate.Insurance.Medcare Hospital (Br)",
                                           "Enabling & Support"), ("non_clinical", False))

    def test_nurse_title_overrides_nonclinical_bucket(self):
        # e.g. a Dental Nurse filed under Paramedical
        self.assertEqual(classify_category("Dental Nurse", "Paramedical"),
                         ("nurses", False))

    def test_title_fallback_without_category(self):
        self.assertEqual(classify_category("Registered Nurses"), ("nurses", False))
        self.assertEqual(classify_category("Clinical Pharmacist"), ("pharmacists", False))
        self.assertEqual(classify_category("Consultant Cardiologist"), ("doctors", False))

    def test_allied_ologist_is_not_a_doctor(self):
        category, _ = classify_category("Audiologist.ENT.MedcareHospitalSharjah(Br)")
        self.assertEqual(category, "non_clinical")

    def test_unknown_title_kept_and_flagged(self):
        category, needs_review = classify_category("Business Development Executive")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)  # kept, never silently dropped


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
