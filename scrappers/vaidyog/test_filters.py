#!/usr/bin/env python3
"""Unit tests for the vaidyog scraper's parsers (plain `python test_filters.py`)."""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (classify_category, classify_company_type, compute_cutoff,
                     normalize_state, parse_experience, parse_salary,
                     posted_date_from_id, INITIAL_WINDOW_DAYS,
                     WATERMARK_GRACE_DAYS)


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


class TestClassifyCategory(unittest.TestCase):
    def test_nurse(self):
        self.assertEqual(classify_category("wanted nursing candidates")[0], "nurses")
        self.assertEqual(classify_category("GNM")[0], "nurses")

    def test_doctor(self):
        self.assertEqual(classify_category("Full-Time Radiologist")[0], "doctors")
        self.assertEqual(classify_category("RMO(MBBS)")[0], "doctors")
        self.assertEqual(classify_category("BAMS/BHMS doctors needed")[0], "doctors")
        self.assertEqual(classify_category("Female Ultrasonologist (MD)")[0], "doctors")

    def test_pharmacist(self):
        self.assertEqual(classify_category("Pharmacist - Retail")[0], "pharmacists")

    def test_non_clinical(self):
        self.assertEqual(classify_category("Tele Caller")[0], "non_clinical")
        self.assertEqual(classify_category("Front Desk Executive")[0], "non_clinical")

    def test_junk_flagged(self):
        category, needs_review = classify_category("testing")
        self.assertTrue(needs_review)
        _, needs_review = classify_category("Dental Assistant")
        self.assertFalse(needs_review)


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
