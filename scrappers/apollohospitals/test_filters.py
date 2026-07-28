#!/usr/bin/env python3
"""Unit tests for the apollohospitals scraper's parsers
(plain `python test_filters.py`)."""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (build_description, classify_category, classify_job_type,
                     compute_cutoff, job_to_rich_row, parse_location,
                     strip_html, INITIAL_WINDOW_DAYS, WATERMARK_GRACE_DAYS)


class TestClassifyCategory(unittest.TestCase):
    """Titles taken verbatim from live CX_2 requisitions."""

    def test_nursing_titles(self):
        self.assertEqual(classify_category("Staff Nurse - Nursing", "Nursing"),
                         ("nurses", False))
        self.assertEqual(classify_category("Nurse Assistant - Nursing - 48936",
                                           "Nursing")[0], "nurses")
        self.assertEqual(classify_category("Deputy Clinical Nurse - Specialist",
                                           "Nursing")[0], "nurses")

    def test_doctor_titles(self):
        self.assertEqual(classify_category("Registrar - General Medicine",
                                           "Medical")[0], "doctors")
        self.assertEqual(classify_category("Medical Officer - General Medicine",
                                           "Medical")[0], "doctors")
        self.assertEqual(classify_category("Resident - Critical Care Medicine",
                                           "Medical")[0], "doctors")
        self.assertEqual(classify_category(
            "Academic Registrar - Neonatology", "")[0], "doctors")

    def test_title_wins_over_site_category(self):
        # a nurse filed under Paramedical still maps to nurses
        self.assertEqual(classify_category("Staff Nurse - ICU", "Paramedical")[0],
                         "nurses")
        self.assertEqual(classify_category("Clinical Pharmacist", "Administration")[0],
                         "pharmacists")

    def test_technologist_is_not_a_doctor(self):
        # "Technologist" must not hit the [a-z]+ologist doctor pattern
        self.assertEqual(classify_category("Technologist - Neurology",
                                           "Paramedical")[0], "non_clinical")
        self.assertEqual(classify_category("Technician - Emergency Medicine - 56177",
                                           "Paramedical")[0], "non_clinical")

    def test_site_category_fallback(self):
        self.assertEqual(classify_category("Ward Incharge", "Nursing")[0],
                         "nurses")
        self.assertEqual(classify_category("Technician - Laboratory Medicine",
                                           "Paramedical")[0], "non_clinical")

    def test_non_clinical_hospital_roles_kept_unflagged(self):
        for title in ("Manager - Center Operations", "Recruitment Specialist",
                      "Shift Engineer - Engineering Services",
                      "Senior Executive - Call Center"):
            category, review = classify_category(title, "Administration")
            self.assertEqual(category, "non_clinical", title)
            self.assertFalse(review, title)

    def test_junk_or_empty_titles_flagged(self):
        self.assertTrue(classify_category("test posting", "Nursing")[1])
        self.assertTrue(classify_category("", "Nursing")[1])
        self.assertFalse(classify_category("Staff Nurse - Nursing", "Nursing")[1])


class TestJobTypeAndLocation(unittest.TestCase):

    def test_job_schedule_mapping(self):
        self.assertEqual(classify_job_type("Full time"), "full_time")
        self.assertEqual(classify_job_type("Part time"), "part_time")
        self.assertEqual(classify_job_type("Fixed Term Contract"), "contract")
        # openings default to full_time when the field is absent
        self.assertEqual(classify_job_type(None), "full_time")
        self.assertEqual(classify_job_type(""), "full_time")

    def test_parse_location(self):
        self.assertEqual(parse_location("Bangalore, Karnataka, India"),
                         ("Bangalore", "Karnataka"))
        self.assertEqual(parse_location("Rourkela, Odisha, India"),
                         ("Rourkela", "Odisha"))
        self.assertEqual(parse_location("Karnataka, India"), ("Karnataka", ""))
        self.assertEqual(parse_location("India"), ("", ""))
        self.assertEqual(parse_location(None), ("", ""))


class TestRowBuilding(unittest.TestCase):

    LISTING = {
        "Id": "45716", "Title": "Staff Nurse - Nursing",
        "PostedDate": "2026-07-17",
        "PrimaryLocation": "Bangalore, Karnataka, India",
    }
    DETAIL = {
        "RequisitionType": "Nursing", "JobSchedule": "Full time",
        "StudyLevel": "Graduate",
        "ExternalPostedEndDate": "2026-07-28T08:12:00+00:00",
        "ExternalDescriptionStr":
            "<p><strong>Qualification:</strong></p><p>Graduate; 1st division</p>",
        "workLocation": [
            {"LocationName": "Apollo Hospitals, Bannerghatta Road, Bangalore"}],
    }

    def test_full_row(self):
        row = job_to_rich_row(self.LISTING, self.DETAIL)
        self.assertEqual(row["job_id"], "45716")
        self.assertEqual(row["category"], "nurses")
        self.assertEqual(row["company"],
                         "Apollo Hospitals, Bannerghatta Road, Bangalore")
        self.assertEqual((row["city"], row["state"]),
                         ("Bangalore", "Karnataka"))
        self.assertEqual(row["salary_raw"], "Not Disclosed")  # never disclosed
        self.assertEqual(row["salary_min"], "")
        self.assertEqual(row["posted_date"], "2026-07-17")
        self.assertEqual(row["posting_end_date"], "2026-07-28")
        self.assertIn("Qualification: Graduate; 1st division", row["description"])
        self.assertTrue(row["job_url"].endswith("/sites/CX_2/job/45716"))
        self.assertFalse(row["needs_review"])

    def test_row_without_detail_falls_back(self):
        row = job_to_rich_row(self.LISTING, {})
        self.assertEqual(row["company"], "Apollo Hospitals")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["description"], "")

    def test_strip_html_and_entities(self):
        self.assertEqual(
            strip_html("<p><strong>Job&nbsp;Purpose</strong></p> <p>Care</p>"),
            "Job Purpose Care")

    def test_empty_description_stays_empty(self):
        self.assertEqual(build_description(
            {"ExternalDescriptionStr": "", "ExternalQualificationsStr": None}), "")


class TestCutoff(unittest.TestCase):

    def test_first_run_uses_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-20", "bad"]})
        expected = (date(2026, 7, 20)
                    - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_all_dates_bad_falls_back(self):
        df = pd.DataFrame({"posted_date": ["", "not-a-date"]})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
