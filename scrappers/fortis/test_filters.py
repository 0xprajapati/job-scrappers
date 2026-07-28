#!/usr/bin/env python3
"""Unit tests for the Fortis scraper's parsers, classifier and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real listings on the Oracle Recruiting Cloud API
(site CX_1), e.g. Id 11900 "Attending Consultant Radiology" (Clinicians,
Mumbai) and Id 11829 "OT Technician" (Technicians, FHL- Manesar).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS, WATERMARK_GRACE_DAYS, apply_detail,
    classify_category, clean_text, compute_cutoff, country_meta,
    family_fallback, job_to_rich_row, parse_job_type, parse_location,
    rich_row_to_club_row, strip_html,
)

# Trimmed real payloads from the live API (2026-07-24).
LIST_JOB = {
    "Id": "11900",
    "Title": "Attending Consultant Radiology",
    "PostedDate": "2026-07-24",
    "PostingEndDate": None,
    "PrimaryLocationCountry": "IN",
    "JobFamily": None,
    "PrimaryLocation": "Mumbai, Maharashtra, India",
    "ShortDescriptionStr": "",
}

DETAIL_JOB = {
    "Id": "11900",
    "Title": "Attending Consultant Radiology",
    "Category": "CLINICIAN",
    "RequisitionId": "300008075675869",
    "JobSchedule": "Full time",
    "StudyLevel": "Post Graduation",
    "ExternalPostedEndDate": "2026-07-31T06:03:00+00:00",
    "JobFamilyId": 300000787441924,
    "ExternalDescriptionStr": "",
    "ExternalQualificationsStr": "",
    "ExternalResponsibilitiesStr": "",
    "workLocation": [{"LocationId": 300000003533633,
                      "LocationName": "IHL-Kalyan",
                      "TownOrCity": "Kalyan",
                      "Region2": "Maharashtra"}],
}


class TestClassifier(unittest.TestCase):
    def test_titles_map_to_club_categories(self):
        cases = [
            # (title, family_name, family_fallback) -> category
            ("NICU Nurse", "Nursing", "nurses", "nurses"),
            ("Staff Nurse - ICU", "", "", "nurses"),
            ("Clinical Pharmacist", "Medical Support", "non_clinical",
             "pharmacists"),
            ("Attending Consultant Radiology", "Clinicians", "doctors",
             "doctors"),
            ("Senior Resident - Cardiology", "Clinicians", "doctors",
             "doctors"),
            ("OT Technician", "Technicians", "non_clinical", "non_clinical"),
            ("Executive - Finance", "Other Functions", "non_clinical",
             "non_clinical"),
            # Allied health stays non_clinical even in the Clinicians family
            ("Physiotherapist", "Clinicians", "doctors", "non_clinical"),
            ("Audiologist", "Clinicians", "doctors", "non_clinical"),
            # Title-only (no family info in the list payload)
            ("Attending Consultant Non Invasive Cardiology", "", "",
             "doctors"),
        ]
        for title, family, fallback, expected in cases:
            category, _ = classify_category(title, family, fallback)
            self.assertEqual(category, expected, title)

    def test_consultant_is_doctor_only_in_clinicians_family(self):
        self.assertEqual(
            classify_category("Consultant - Anaesthesia", "Clinicians",
                              "doctors")[0], "doctors")
        self.assertEqual(
            classify_category("Consultant - Taxation", "Other Functions",
                              "non_clinical")[0], "non_clinical")

    def test_needs_review_only_for_signal_free_corporate_titles(self):
        # Generic corporate title in Other Functions -> flagged, never dropped
        _, review = classify_category("Executive - Procurement",
                                      "Other Functions", "non_clinical")
        self.assertTrue(review)
        # Same family but with a healthcare word -> not flagged
        _, review = classify_category("Medical Records Officer",
                                      "Other Functions", "non_clinical")
        self.assertFalse(review)
        # Known clinical family -> never flagged
        _, review = classify_category("OT Technician", "Technicians",
                                      "non_clinical")
        self.assertFalse(review)

    def test_family_fallback_from_both_identifiers(self):
        self.assertEqual(family_fallback("300000787442029"),
                         ("Nursing", "nurses"))
        self.assertEqual(family_fallback(300000787441924),
                         ("Clinicians", "doctors"))
        self.assertEqual(family_fallback(category_code="TECHINICIANS"),
                         ("Technicians", "non_clinical"))
        # Some requisitions only carry RequisitionType ("Clinicians")
        self.assertEqual(family_fallback(category_code="Clinicians"),
                         ("Clinicians", "doctors"))
        self.assertEqual(family_fallback("999", "UNKNOWN"), ("", ""))


class TestParsers(unittest.TestCase):
    def test_parse_location(self):
        self.assertEqual(parse_location("Mumbai, Maharashtra, India"),
                         ("Mumbai", "Maharashtra"))
        self.assertEqual(parse_location("Gurugram, India"), ("Gurugram", ""))
        self.assertEqual(parse_location(None), ("", ""))

    def test_country_meta(self):
        self.assertEqual(country_meta("IN"), ("India", "IN", "+91"))
        self.assertEqual(country_meta(""), ("India", "IN", "+91"))
        self.assertEqual(country_meta("AE"),
                         ("United Arab Emirates", "AE", "+971"))

    def test_parse_job_type(self):
        self.assertEqual(parse_job_type("Full time"), "full_time")
        self.assertEqual(parse_job_type("Part time"), "part_time")
        self.assertEqual(parse_job_type(None), "full_time")

    def test_text_helpers(self):
        self.assertEqual(strip_html("<p>MD&nbsp;Radiology</p>"), "MD Radiology")
        self.assertEqual(clean_text("  a \n b  "), "a b")


class TestRowBuilding(unittest.TestCase):
    def test_salary_never_invented(self):
        row = job_to_rich_row(LIST_JOB)
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        for field in ("salary_min", "salary_max", "salary_period",
                      "salary_currency"):
            self.assertEqual(row[field], "")
        club = rich_row_to_club_row(row)
        for field in ("min_salary", "max_salary", "salary_period",
                      "salary_currency"):
            self.assertEqual(club[field], "")

    def test_rich_row_from_real_listing(self):
        row = job_to_rich_row(LIST_JOB)
        self.assertEqual(row["job_id"], "11900")
        self.assertEqual(row["posted_date"], "2026-07-24")
        self.assertEqual((row["city"], row["state"]),
                         ("Mumbai", "Maharashtra"))
        self.assertTrue(row["job_url"].endswith("/sites/CX_1/job/11900"))
        self.assertEqual(row["company_type"], "hospital")
        # List payload has no family info; the title alone decides.
        self.assertEqual(row["category"], "doctors")

    def test_apply_detail_refines_row(self):
        row = job_to_rich_row(LIST_JOB)
        apply_detail(row, DETAIL_JOB)
        self.assertEqual(row["job_family"], "Clinicians")
        self.assertEqual(row["category"], "doctors")
        self.assertFalse(row["needs_review"])
        self.assertEqual(row["facility"], "IHL-Kalyan")
        # Facility address city overrides the vaguer PrimaryLocation
        self.assertEqual((row["city"], row["state"]),
                         ("Kalyan", "Maharashtra"))
        self.assertEqual(row["study_level"], "Post Graduation")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["closure_date"], "2026-07-31")
        self.assertEqual(row["requisition_id"], "300008075675869")

    def test_club_row_schema(self):
        row = job_to_rich_row(LIST_JOB)
        apply_detail(row, DETAIL_JOB)
        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["city_name"], "Kalyan")
        self.assertEqual(club["company_name"], "Fortis Healthcare")
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(club["category"], "doctors")
        self.assertEqual(club["posted_at"], "2026-07-24")
        self.assertEqual(club["expires_at"], "2026-07-31")
        self.assertEqual(club["is_active"], "true")
        # Empty description falls back to the hiring facility
        self.assertEqual(club["description"], "Hiring facility: IHL-Kalyan")


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        expected = (date.today()
                    - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-10", "2026-07-20", ""]})
        expected = (date(2026, 7, 20)
                    - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_unparseable_dates_fall_back_to_initial_window(self):
        df = pd.DataFrame({"posted_date": ["not-a-date", ""]})
        expected = (date.today()
                    - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
