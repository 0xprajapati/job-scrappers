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
    CLUB_COLUMNS, INITIAL_WINDOW_DAYS, WATERMARK_GRACE_DAYS,
    apply_classification, apply_detail, clean_text, compute_cutoff,
    country_meta, family_name_for, job_to_rich_row, parse_job_type,
    parse_location, rich_row_to_club_row, strip_html,
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


class TestSharedClassifierWiring(unittest.TestCase):
    """apply_classification() wires the shared two-level classifier; the
    engine itself is covered by _shared/test_classification.py."""

    @staticmethod
    def row(title, job_family="", category_source="", description=""):
        return {"title": title, "job_family": job_family,
                "category_source": category_source,
                "description": description}

    def test_in_scope_role_gets_taxonomy_labels(self):
        row = self.row("Clinical Research Coordinator", "Other Functions",
                       "OTHERS",
                       "Run clinical trials to ICH-GCP; ethics submissions "
                       "and CRF completion.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")
        self.assertTrue(row["matched_in"])

    def test_public_health_role_in_scope(self):
        row = self.row("Infection Prevention and Control Officer",
                       "Other Functions", "",
                       "Hospital infection surveillance, hand hygiene audits, "
                       "outbreak investigation.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")

    def test_clinician_requisitions_are_dropped(self):
        # The chain's bread-and-butter postings are bedside/clinical work,
        # which the shared taxonomy puts out of scope.
        for title, family in [("NICU Nurse", "Nursing"),
                              ("Attending Consultant Radiology", "Clinicians"),
                              ("Senior Resident - Cardiology", "Clinicians"),
                              ("OT Technician", "Technicians"),
                              ("Physiotherapist", "Clinicians")]:
            row = self.row(title, family)
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "", title)

    def test_job_family_never_decides_the_category(self):
        # A finance title inside any ATS family stays out of scope; the
        # Fortis family name is only a `skills` signal + rich-CSV column.
        row = self.row("Executive - Finance", "Clinicians", "CLINICIAN")
        self.assertFalse(apply_classification(row))

    def test_family_name_is_source_data_only(self):
        self.assertEqual(family_name_for("300000787442029"), "Nursing")
        self.assertEqual(family_name_for(300000787441924), "Clinicians")
        self.assertEqual(family_name_for(category_code="TECHINICIANS"),
                         "Technicians")
        # Some requisitions only carry RequisitionType ("Clinicians")
        self.assertEqual(family_name_for(category_code="Clinicians"),
                         "Clinicians")
        self.assertEqual(family_name_for("999", "UNKNOWN"), "")


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
        # The builder never labels: apply_classification() stamps the
        # taxonomy after the detail fetch, so these start blank.
        self.assertEqual(row["category"], "")
        self.assertEqual(row["sub_category"], "")

    def test_apply_detail_refines_row(self):
        row = job_to_rich_row(LIST_JOB)
        apply_detail(row, DETAIL_JOB)
        self.assertEqual(row["job_family"], "Clinicians")
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
        row["title"] = "Clinical Research Coordinator"
        row["description"] = ""
        apply_classification(row)
        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        # is_active / expires_at are retired from the club contract.
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["city_name"], "Kalyan")
        self.assertEqual(club["company_name"], "Fortis Healthcare")
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        self.assertEqual(club["posted_at"], "2026-07-24")
        # StudyLevel is the structured qualification field for this source.
        self.assertEqual(club["qualification"], "Post Graduation")
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
