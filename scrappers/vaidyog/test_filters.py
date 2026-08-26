#!/usr/bin/env python3
"""Unit tests for the vaidyog scraper's parsers (plain `python test_filters.py`)."""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (CLUB_COLUMNS, classify_company_type, classify_row,
                     compute_cutoff, normalize_state, parse_experience,
                     parse_salary, posted_date_from_id, rich_row_to_club_row,
                     INITIAL_WINDOW_DAYS, WATERMARK_GRACE_DAYS)


class TestParseSalary(unittest.TestCase):
    """Worked examples taken verbatim from live vaidyog listings."""

    def test_monthly_range(self):
        out = parse_salary({"min": 15000, "max": 30000})
        self.assertEqual((out["salary_min"], out["salary_max"]), (15000, 30000))
        self.assertEqual(out["salary_period"], "per_month")
        self.assertEqual(out["salary_currency"], "INR")

    def test_annual_range(self):
        out = parse_salary({"min": 1800000, "max": 2400000})  # Pediatrician 18-24 LPA
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (1800000, 2400000))
        self.assertEqual(out["salary_period"], "per_annum")

    def test_thousands_shorthand(self):
        out = parse_salary({"min": 30, "max": 40})  # "Need Duty doctor" 30-40k
        self.assertEqual((out["salary_min"], out["salary_max"]), (30000, 40000))
        self.assertEqual(out["salary_period"], "per_month")

    def test_undisclosed_zero_zero(self):
        self.assertEqual(parse_salary({"min": 0, "max": 0}), {})

    def test_missing(self):
        self.assertEqual(parse_salary(None), {})
        self.assertEqual(parse_salary({}), {})

    def test_junk_kept_raw_only(self):
        out = parse_salary({"min": 0, "max": 2})  # Area Sales Manager junk
        self.assertEqual(out.get("salary_raw"), "0-2")
        self.assertNotIn("salary_min", out)

    def test_reversed_range_swapped(self):
        out = parse_salary({"min": 30000, "max": 15000})
        self.assertEqual((out["salary_min"], out["salary_max"]), (15000, 30000))

    def test_open_bottom_range(self):
        out = parse_salary({"min": 0, "max": 22000})  # "Nurse" 0-22k
        self.assertEqual(out["salary_max"], 22000)
        self.assertEqual(out["salary_period"], "per_month")


class TestPostedDate(unittest.TestCase):
    def test_objectid_timestamp(self):
        # 0x6a605fa9 = 2026-07-22 UTC (live "Tele Caller" posting)
        self.assertEqual(posted_date_from_id("6a605fa9be769782f06fe6ba"),
                         "2026-07-22")

    def test_not_an_objectid(self):
        self.assertEqual(posted_date_from_id("hello"), "")
        self.assertEqual(posted_date_from_id(""), "")
        self.assertEqual(posted_date_from_id(None), "")


class TestSharedClassifierWiring(unittest.TestCase):
    """classify_row routes every candidate through _shared/classification.

    Only the wiring is tested (signals in, verdict written back onto the
    row); the engine itself is covered by _shared/test_classification.py.
    """

    @staticmethod
    def row(title, skills="", description=""):
        return {"title": title, "skills": skills, "description": description,
                "category": "", "sub_category": "", "role_family": "",
                "all_families": "", "family_scores": "",
                "family_confidence": "", "matched_in": "",
                "needs_review": False}

    def test_in_scope_role_labels_the_row(self):
        row = self.row("Clinical Research Coordinator")
        verdict = classify_row(row)
        self.assertTrue(verdict["in_scope"])
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")

    def test_clinical_title_is_out_of_scope(self):
        # this is a clinical board: nurses/doctors/tele-callers all drop
        for title in ("Staff Nurse", "Tele Caller"):
            verdict = classify_row(self.row(title))
            self.assertFalse(verdict["in_scope"], title)

    def test_key_skills_travel_as_skills_signal(self):
        row = self.row("Pharmacovigilance Associate",
                       skills="Drug Safety; ICSR",
                       description="case processing")
        verdict = classify_row(row)
        self.assertTrue(verdict["in_scope"])
        self.assertEqual(row["sub_category"], "Pharmacovigilance")

    def test_public_health_split(self):
        row = self.row("Community Health Worker")
        self.assertTrue(classify_row(row)["in_scope"])
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Community Health")


class TestClubMapping(unittest.TestCase):
    """The 23-column CLUB_COLUMNS contract from _shared/classification."""

    RICH = {"country": "India", "country_code": "IN",
            "country_dial_code": "+91", "city": "Pune", "state": "Maharashtra",
            "company": "Acme CRO", "company_type": "pharma",
            "title": "Clinical Research Coordinator",
            "description": "Requires a B.Pharm and GCP knowledge.",
            "job_type": "full_time", "category": "Non Clinical",
            "sub_category": "Clinical Research", "posted_date": "2026-08-01",
            "salary_min": "15000", "salary_max": "30000",
            "salary_period": "per_month", "salary_currency": "INR",
            "min_experience": "0", "max_experience": "3",
            "job_url": "https://healthcarejobs.vaidyog.com/"}

    def test_columns_match_shared_contract(self):
        row = rich_row_to_club_row(self.RICH)
        self.assertEqual(sorted(row), sorted(CLUB_COLUMNS))
        self.assertEqual(len(CLUB_COLUMNS), 23)
        self.assertNotIn("is_active", row)
        self.assertNotIn("expires_at", row)

    def test_category_never_defaulted(self):
        row = rich_row_to_club_row(self.RICH)
        self.assertEqual((row["category"], row["sub_category"]),
                         ("Non Clinical", "Clinical Research"))

    def test_qualification_grounded_in_description(self):
        self.assertIn("B.Pharm",
                      rich_row_to_club_row(self.RICH)["qualification"])
        blank = dict(self.RICH, description="Great growth opportunity.")
        self.assertEqual(rich_row_to_club_row(blank)["qualification"], "")


class TestCompanyType(unittest.TestCase):
    def test_pharma(self):
        self.assertEqual(classify_company_type("Apollo Diagnostics"), "pharma")

    def test_hospital_default(self):
        self.assertEqual(classify_company_type("Dental Den"), "hospital")


class TestNormalizeState(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(normalize_state("Maharastra"), "Maharashtra")
        self.assertEqual(normalize_state("Tamilnadu"), "Tamil Nadu")
        self.assertEqual(normalize_state("west bengal "), "West Bengal")

    def test_clean_kept(self):
        self.assertEqual(normalize_state("Karnataka"), "Karnataka")


class TestParseExperience(unittest.TestCase):
    def test_ints(self):
        self.assertEqual(parse_experience({"min": 0, "max": 3}), ("0", "3"))

    def test_floats(self):
        self.assertEqual(parse_experience({"min": 1.5, "max": 3.0}), ("1", "3"))

    def test_missing(self):
        self.assertEqual(parse_experience(None), ("", ""))
        self.assertEqual(parse_experience({}), ("", ""))


class TestComputeCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-20", ""]})
        expected = (date(2026, 7, 20) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
