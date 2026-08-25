#!/usr/bin/env python3
"""Unit tests for the apollohospitals scraper's parsers and the shared-
classifier wiring (plain `python test_filters.py`)."""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (apply_classification, build_description,
                     classify_job_type, compute_cutoff, job_to_rich_row,
                     parse_location, rich_row_to_club_row, strip_html,
                     CLUB_COLUMNS, INITIAL_WINDOW_DAYS, WATERMARK_GRACE_DAYS)


class TestClassificationWiring(unittest.TestCase):
    """The scraper delegates every keep/drop and labeling decision to the
    shared classify_job; these tests cover the wiring, not the engine."""

    @staticmethod
    def _row(title, site_category="", description=""):
        return {"title": title, "site_category": site_category,
                "description": description}

    def test_in_scope_role_gets_taxonomy_labels(self):
        row = self._row("Medical Coder", "Administration")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")
        self.assertEqual(row["role_family"], "Medical Coding")

    def test_public_health_role_in_scope(self):
        row = self._row("Epidemiologist")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Epidemiology")

    def test_nurse_requisition_is_dropped(self):
        row = self._row("Staff Nurse - Nursing", "Nursing")
        self.assertFalse(apply_classification(row))

    def test_doctor_requisition_is_dropped(self):
        row = self._row("Registrar - General Medicine", "Medical")
        self.assertFalse(apply_classification(row))

    def test_generic_admin_title_is_dropped(self):
        row = self._row("Manager - Center Operations", "Administration")
        self.assertFalse(apply_classification(row))


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
        "Id": "45716", "Title": "Executive - Medical Coding",
        "PostedDate": "2026-07-17",
        "PrimaryLocation": "Bangalore, Karnataka, India",
    }
    DETAIL = {
        "RequisitionType": "Administration", "JobSchedule": "Full time",
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
        self.assertEqual(row["site_category"], "Administration")
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
        # taxonomy labels come only from apply_classification
        self.assertEqual(row["category"], "")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")

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


class TestClubRow(unittest.TestCase):

    def test_club_schema_and_qualification(self):
        row = job_to_rich_row(TestRowBuilding.LISTING, TestRowBuilding.DETAIL)
        apply_classification(row)
        club = rich_row_to_club_row(row)
        self.assertEqual(set(club), set(CLUB_COLUMNS))
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Medical Coding")
        # StudyLevel is the structured qualification source
        self.assertEqual(club["qualification"], "Graduate")

    def test_qualification_extracted_when_no_study_level(self):
        row = job_to_rich_row(TestRowBuilding.LISTING, {})
        row["description"] = "Candidates must hold an MBBS degree."
        club = rich_row_to_club_row(row)
        self.assertEqual(club["qualification"], "MBBS")


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
