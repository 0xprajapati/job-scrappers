#!/usr/bin/env python3
"""Unit tests for the zulekhahospitals scraper's parsers — run with
`python test_filters.py`.

Example titles come from the Adrenalin MAX portal (CompanyID=ZULEKHA) and the
static openings table on zulekhacareers.com.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    classify_category, clean_facility, clean_text, clean_title,
    compute_cutoff, parse_experience, strip_html, within_window,
    WATERMARK_GRACE_DAYS,
)


class TestCleanTitle(unittest.TestCase):
    def test_requisition_suffix_stripped_and_recased(self):
        self.assertEqual(clean_title("CARE ADVISOR_608"), "Care Advisor")

    def test_acronyms_stay_uppercase(self):
        self.assertEqual(clean_title("PHLEBOTOMIST - DHA HOLDER"),
                         "Phlebotomist - DHA Holder")

    def test_mixed_case_untouched(self):
        self.assertEqual(clean_title("Consultant Internal Medicine with DHA"),
                         "Consultant Internal Medicine with DHA")

    def test_plain_title_without_suffix(self):
        self.assertEqual(clean_title("Clinical Psychologist - Counselor"),
                         "Clinical Psychologist - Counselor")


class TestCleanFacility(unittest.TestCase):
    def test_llc_and_dash_removed(self):
        self.assertEqual(clean_facility("Zulekha Hospital LLC -Dubai"),
                         "Zulekha Hospital Dubai")

    def test_sharjah_variant(self):
        self.assertEqual(clean_facility("Zulekha Hospital L.L.C - Sharjah"),
                         "Zulekha Hospital Sharjah")

    def test_empty(self):
        self.assertEqual(clean_facility(""), "")


class TestParseExperience(unittest.TestCase):
    def test_range_with_years(self):
        # portal shows doubled spaces: "3 - 7  Year(s)"
        self.assertEqual(parse_experience("3 - 7  Year(s)"), ("3", "7"))

    def test_single_number(self):
        self.assertEqual(parse_experience("10"), ("10", ""))

    def test_fresher(self):
        self.assertEqual(parse_experience("Fresher"), ("0", ""))

    def test_months_become_floor_years(self):
        self.assertEqual(parse_experience("6 - 18  Month(s)"), ("0", "1"))

    def test_free_text_range(self):
        self.assertEqual(parse_experience("2 - 5 years from Hospital background"),
                         ("2", "5"))

    def test_unparseable(self):
        self.assertEqual(parse_experience("Not specified"), ("", ""))
        self.assertEqual(parse_experience(""), ("", ""))


class TestClassifier(unittest.TestCase):
    def test_titles_decide_clinical_roles(self):
        self.assertEqual(classify_category("Registered Nurse - ICU"),
                         ("nurses", False))
        self.assertEqual(classify_category("Clinical Pharmacist"),
                         ("pharmacists", False))
        self.assertEqual(classify_category("Consultant Internal Medicine with DHA"),
                         ("doctors", False))
        self.assertEqual(classify_category("Consultant Paediatrics with DHA license"),
                         ("doctors", False))

    def test_psychologist_is_a_doctor_ologist(self):
        self.assertEqual(classify_category("Clinical Psychologist - Counselor"),
                         ("doctors", False))

    def test_technologist_is_not_a_doctor(self):
        category, _ = classify_category("Lab Technologist", "Non Clinical", "NC ")
        self.assertEqual(category, "non_clinical")

    def test_functional_area_confirms_non_clinical(self):
        self.assertEqual(classify_category("CARE ADVISOR_608", "Non Clinical", "NC "),
                         ("non_clinical", False))

    def test_unknown_title_kept_and_flagged(self):
        category, needs_review = classify_category("Phlebotomist - DHA holder")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)  # kept, never silently dropped


class TestCutoff(unittest.TestCase):
    def test_first_run_keeps_all_open_vacancies(self):
        self.assertIsNone(compute_cutoff(None))
        self.assertTrue(within_window("2020-11-29", None))

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-06-13", "2024-03-03"]})
        expected = (date(2026, 6, 13)
                    - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)
        self.assertTrue(within_window("2026-06-13", expected))
        self.assertFalse(within_window("2024-03-03", expected))
        self.assertFalse(within_window("", expected))

    def test_unparseable_dates_fall_back_to_keep_all(self):
        df = pd.DataFrame({"posted_date": ["not-a-date", ""]})
        self.assertIsNone(compute_cutoff(df))


class TestTextCleaning(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(
            strip_html("<p>Duties:<br/>Assist patients &amp; staff</p>"),
            "Duties: Assist patients & staff")

    def test_clean_text_whitespace_and_entities(self):
        self.assertEqual(clean_text("  Care  Advisor &amp; Guide \n"),
                         "Care Advisor & Guide")

    def test_none_safe(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(clean_text(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
