#!/usr/bin/env python3
"""Unit tests for the dubaihealth scraper parsers — run: python test_filters.py"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    apply_classification, compute_cutoff, ddmmyyyy_to_iso, decode_taleo_text,
    listing_to_rich_row, parse_detail_html, parse_salary, rich_row_to_club_row,
    CLUB_COLUMNS,
)


class TestDates(unittest.TestCase):
    def test_ddmmyyyy(self):
        self.assertEqual(ddmmyyyy_to_iso("21/07/2026"), "2026-07-21")
        self.assertEqual(ddmmyyyy_to_iso("07/01/2026"), "2026-01-07")

    def test_bad_dates(self):
        for bad in ("", None, "2026-07-21", "31/02/2026", "yesterday"):
            self.assertEqual(ddmmyyyy_to_iso(bad), "")


class TestClassificationWiring(unittest.TestCase):
    """The scraper must delegate every keep/drop and label decision to the
    shared two-level classifier (scrappers/_shared/classification.py)."""

    def test_in_scope_role_is_stamped(self):
        row = {"title": "Clinical Research Coordinator",
               "department": "Research", "description": ""}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")
        self.assertTrue(row["matched_in"])

    def test_public_health_role_is_stamped(self):
        row = {"title": "Infection Control Practitioner",
               "department": "Infection Prevention", "description": ""}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_bedside_role_is_dropped(self):
        for title in ("Staff Nurse - ICU", "Consultant Cardiologist",
                      "Specialist Anaesthesiologist"):
            row = {"title": title, "department": "", "description": ""}
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_dietitian_is_public_health(self):
        # User decision 2026-08-25: dieticians are in scope under
        # Public Health -> Public Health Nutrition.
        row = {"title": "Clinical Dietitian 2 -UAE National",
               "department": "", "description": ""}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Public Health Nutrition")

    def test_club_row_uses_shared_contract(self):
        row = {"title": "Medical Coder", "department": "HIM",
               "description": "Assign ICD-10 codes.", "education": "Bachelor",
               "job_url": "https://x/y", "posted_date": "2026-07-21"}
        apply_classification(row)
        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Medical Coding")
        self.assertEqual(club["qualification"], "Bachelor")
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)


class TestSalary(unittest.TestCase):
    def test_unspecified(self):
        for raw in ("Unspecified", "", None, "N/A", "-"):
            self.assertEqual(parse_salary(raw), ("Not Disclosed", "", "", ""))

    def test_range(self):
        self.assertEqual(parse_salary("15,000 - 25,000"),
                         ("15,000 - 25,000", "15000", "25000", "AED"))

    def test_single_value(self):
        self.assertEqual(parse_salary("18000"), ("18000", "18000", "18000", "AED"))

    def test_text_without_numbers(self):
        self.assertEqual(parse_salary("Competitive"), ("Competitive", "", "", ""))


# A miniature of the real jobdetail.ftl state blob (same separators/offsets;
# HTML percent-encoded with ':' escaped as '%5C:').
_BLOB = ("<input type=\"hidden\" value=\""
         "ftlx0!|!ftlUtil_resetPage"
         "!%24!requisitionDescriptionInterface!|!descRequisition!|!rdPager!%24!"
         "Submission for Test!|!false!|!151832!|!false!|!true!|!Test Job!|!26000730!|!!|!"
         "!*!%3Cp style=%22margin%5C:0%22%3EDo the clinical work.%3C/p%3E!|!"
         "!*!%3Cp%3EDo the clinical work.%3C/p%3E!|!"
         "!*!%3Cp%3EBachelor degree required.%3C/p%3E!|!"
         "!*!%3Cp%3EBachelor degree required.%3C/p%3E"
         "!|!Nutrition!|!Nutrition!|!Dubai Health!|!Dubai Health!|!!|!!|!"
         "Bachelor!|!Bachelor!|!Regular!|!!|!Graduate job!|!!|!Staff!|!!|!"
         "UAE Only!|!UAE Only!|!No!|!No!|!12,000 - 18,000!|!12,000 - 18,000!|!"
         "Full time!|!Full time!|!21/07/2026!|!21/07/2026!|!26/07/2026!|!26/07/2026"
         "!|!false!|!151832"
         "!%24!ftlerrors!|!!|!csrftoken!|!abc\" />")


class TestDetailParser(unittest.TestCase):
    def setUp(self):
        self.detail = parse_detail_html(_BLOB)

    def test_description_joins_duties_and_qualifications(self):
        self.assertEqual(self.detail["description"],
                         "Do the clinical work. Qualifications: Bachelor degree required.")

    def test_fields(self):
        self.assertEqual(self.detail["department"], "Nutrition")
        self.assertEqual(self.detail["education"], "Bachelor")
        self.assertEqual(self.detail["contract_type"], "Regular")
        self.assertEqual(self.detail["job_level"], "Staff")
        self.assertEqual(self.detail["job_type"], "full_time")
        self.assertEqual(self.detail["salary_field"], "12,000 - 18,000")

    def test_dates(self):
        self.assertEqual(self.detail["detail_posted_date"], "2026-07-21")
        self.assertEqual(self.detail["expires_at"], "2026-07-26")

    def test_garbage_html_degrades_gracefully(self):
        self.assertEqual(parse_detail_html("<html>no blob here</html>"), {})
        self.assertEqual(parse_detail_html(""), {})

    def test_decode_taleo_text(self):
        self.assertEqual(decode_taleo_text("(Job Number%5C: 26000730)"),
                         "(Job Number: 26000730)")


class TestListingRow(unittest.TestCase):
    def test_row_from_requisition(self):
        req = {"jobId": "151832", "contestNo": "26000730",
               "column": ["Clinical Dietitian 2 -UAE National ",
                          "21/07/2026", "UAE Only"]}
        row = listing_to_rich_row(req)
        self.assertEqual(row["job_id"], "26000730")
        self.assertEqual(row["posted_date"], "2026-07-21")
        self.assertEqual(row["nationality_requirement"], "UAE Only")
        self.assertIn("job=26000730", row["job_url"])


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=30)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-21"]})
        self.assertEqual(compute_cutoff(df), "2026-07-19")

    def test_unparseable_dates_fall_back(self):
        df = pd.DataFrame({"posted_date": ["", "not-a-date"]})
        expected = (date.today() - timedelta(days=30)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
