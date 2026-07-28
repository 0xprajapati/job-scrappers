#!/usr/bin/env python3
"""Unit tests for the swaasa scraper's parsers (plain `python test_filters.py`)."""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (city_from_title, classify_category, classify_company_type,
                     compute_cutoff, country_meta, parse_salary,
                     INITIAL_WINDOW_DAYS, WATERMARK_GRACE_DAYS)


class TestParseSalary(unittest.TestCase):
    """Worked examples taken verbatim from live swaasa listings."""

    def test_indian_range_per_year(self):
        out = parse_salary("₹3,00,000 – ₹5,00,000 per Year")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (300000, 500000))
        self.assertEqual(out["salary_period"], "per_annum")
        self.assertEqual(out["salary_currency"], "INR")

    def test_monthly_range_no_symbol(self):
        out = parse_salary("18000-30000 monthly")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (18000, 30000))
        self.assertEqual(out["salary_period"], "per_month")

    def test_bare_amount_reads_monthly(self):
        out = parse_salary("25,000")
        self.assertEqual(out["salary_min"], 25000)
        self.assertEqual(out["salary_max"], 25000)
        self.assertEqual(out["salary_period"], "per_month")

    def test_bare_range_with_spaces(self):
        out = parse_salary("30,000- 32,000")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (30000, 32000))
        self.assertEqual(out["salary_period"], "per_month")

    def test_bare_large_amount_reads_yearly(self):
        out = parse_salary("890000")
        self.assertEqual(out["salary_min"], 890000)
        self.assertEqual(out["salary_period"], "per_annum")

    def test_odd_indian_grouping(self):
        out = parse_salary("10,00000- 12,00000")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (1000000, 1200000))
        self.assertEqual(out["salary_period"], "per_annum")

    def test_rupee_range_without_period(self):
        out = parse_salary("₹6,00,000 - ₹80,00,000")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (600000, 8000000))
        self.assertEqual(out["salary_period"], "per_annum")

    def test_reversed_range_swapped(self):
        out = parse_salary("50,000 - 30,000 monthly")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (30000, 50000))

    def test_undisclosed_variants(self):
        self.assertEqual(parse_salary(""), {})
        self.assertEqual(parse_salary(None), {})
        out = parse_salary("Best in Industry")
        self.assertNotIn("salary_min", out)  # raw kept, nothing invented
        self.assertEqual(out["salary_raw"], "Best in Industry")

    def test_foreign_currency_kept_raw_only(self):
        out = parse_salary("SAR 3,000 - 5,000 monthly")
        self.assertNotIn("salary_min", out)
        self.assertIn("SAR", out["salary_raw"])


class TestClassifyCategory(unittest.TestCase):

    def test_title_wins_over_site_category(self):
        self.assertEqual(classify_category("Staff Nurse - ICU", "Healthcare"),
                         ("nurses", False))
        self.assertEqual(classify_category("Clinical Pharmacist", "Other")[0],
                         "pharmacists")

    def test_site_category_fallback(self):
        self.assertEqual(classify_category("General Physician Wanted", "Doctor"),
                         ("doctors", False))
        self.assertEqual(classify_category("BAMS Vacancy", "Doctor"),
                         ("doctors", False))
        self.assertEqual(classify_category("Lab Assistant", "Lab Technician"),
                         ("non_clinical", False))

    def test_ambiguous_category_flags_review(self):
        cat, review = classify_category(
            "Business Development Executive", "Sales/Marketing")
        self.assertEqual(cat, "non_clinical")
        self.assertTrue(review)

    def test_ambiguous_category_with_health_title_not_flagged(self):
        cat, review = classify_category(
            "Medical Representative - Cardio", "Sales/Marketing")
        self.assertFalse(review)

    def test_sales_title_never_inherits_clinical_category(self):
        # real case: Elbrit files pharma BDE jobs under site category "Doctor"
        cat, review = classify_category(
            "Business Development Executive (BDE) Jobs in Prayagraj", "Doctor")
        self.assertEqual(cat, "non_clinical")

    def test_junk_titles_flagged(self):
        for title in ("test", "Test jobs in Aster Medcity, Yderik", "rtyui"):
            _, review = classify_category(title, "Nursing")
            self.assertTrue(review, title)
        _, review = classify_category("Latest Staff Nurse Opening", "Nursing")
        self.assertFalse(review)


class TestCompanyAndCountry(unittest.TestCase):

    def test_company_type(self):
        self.assertEqual(
            classify_company_type("Elbrit Life Sciences Pvt Ltd", "Sales/Marketing"),
            "pharma")
        self.assertEqual(
            classify_company_type("Apollo Hospitals", "Doctor"), "hospital")
        self.assertEqual(
            classify_company_type("Acme Corp", "Medical Representative"), "pharma")

    def test_city_from_title(self):
        self.assertEqual(
            city_from_title("Nurse Jobs in Aster Medcity, Kochi"), "Kochi")
        self.assertEqual(
            city_from_title("Nursing jobs in Heritage healthcare India, "
                            "Bangalore (Test)"), "Bangalore")
        self.assertEqual(
            city_from_title("ICU Nurse Job Vacancy in Tirumalagiri, Hyderabad "
                            "at Citizen Hospital"), "Hyderabad")
        self.assertEqual(city_from_title("MSL Event"), "")
        self.assertEqual(
            city_from_title("Test jobs in Aster Medcity, Goth Pir Bakhsh "
                            "Hyderani"), "")

    def test_country_meta(self):
        self.assertEqual(country_meta("India"), ("India", "IN", "+91"))
        self.assertEqual(country_meta("Saudi Arabia"),
                         ("Saudi Arabia", "SA", "+966"))
        # dirty facet values (states/cities leaked into country) -> India
        self.assertEqual(country_meta("Telangana"), ("India", "IN", "+91"))
        self.assertEqual(country_meta(""), ("India", "IN", "+91"))


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
