#!/usr/bin/env python3
"""Unit tests for the shine.com scraper's parsers and filters.

Run with plain `python test_filters.py` (no pytest needed). Worked examples
come from real listings captured on 2026-08-08.
"""

import unittest

from shine_scraper import (
    classify_category,
    classify_company_type,
    club_salary,
    compute_cutoff,
    decode_job_type,
    is_healthcare,
    page_url,
    parse_date,
    parse_experience,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    truncate_description,
)

import pandas as pd
from datetime import date


class TestParseSalary(unittest.TestCase):
    # Real strings seen on the healthcare query, 2026-08-08.

    def test_plain_lakh_range(self):
        raw, lo, hi, period = parse_salary("Rs 2.0  - 3.5 Lakh/Yr")
        self.assertEqual(raw, "Rs 2.0  - 3.5 Lakh/Yr")
        self.assertEqual(lo, "16667")     # 200,000 / 12
        self.assertEqual(hi, "29167")     # 350,000 / 12
        self.assertEqual(period, "Yr")

    def test_integer_lakhs(self):
        _, lo, hi, period = parse_salary("Rs 12  - 24 Lakh/Yr")
        self.assertEqual((lo, hi, period), ("100000", "200000", "Yr"))

    def test_mixed_absolute_and_lakh(self):
        # "50,000" has a comma -> absolute rupees; "2.5" -> lakhs.
        _, lo, hi, period = parse_salary("< Rs 50,000  - 2.5 Lakh/Yr")
        self.assertEqual((lo, hi, period), ("4167", "20833", "Yr"))

    def test_hidden(self):
        self.assertEqual(parse_salary("[Salary Hidden]"),
                         ("Not Disclosed", "", "", ""))

    def test_empty_and_none(self):
        self.assertEqual(parse_salary(""), ("Not Disclosed", "", "", ""))
        self.assertEqual(parse_salary(None), ("Not Disclosed", "", "", ""))

    def test_monthly(self):
        _, lo, hi, period = parse_salary("Rs 25,000 - 35,000 /Month")
        self.assertEqual((lo, hi, period), ("25000", "35000", "Mo"))

    def test_no_period_captures_raw_only(self):
        raw, lo, hi, period = parse_salary("Rs 5 Lakh")
        self.assertEqual(raw, "Rs 5 Lakh")
        self.assertEqual((lo, hi), ("", ""))   # never invent a period

    def test_never_invents(self):
        raw, lo, hi, _ = parse_salary("Negotiable")
        self.assertEqual((lo, hi), ("", ""))


class TestClubSalary(unittest.TestCase):
    def test_yearly_full_figures(self):
        lo, hi, period, currency = club_salary("Rs 4.0  - 4.5 Lakh/Yr")
        self.assertEqual((lo, hi), ("400000", "450000"))
        self.assertEqual((period, currency), ("per_annum", "INR"))

    def test_hidden_is_empty(self):
        self.assertEqual(club_salary("Not Disclosed"), ("", "", "", ""))
        self.assertEqual(club_salary("[Salary Hidden]"), ("", "", "", ""))


class TestParseExperience(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_experience("1 to 5 Yrs"), ("1", "5"))

    def test_single(self):
        self.assertEqual(parse_experience("0 Yrs"), ("0", "0"))

    def test_singular_unit(self):
        self.assertEqual(parse_experience("0 to 1 Yr"), ("0", "1"))

    def test_empty(self):
        self.assertEqual(parse_experience(""), ("", ""))
        self.assertEqual(parse_experience(None), ("", ""))


class TestIsHealthcare(unittest.TestCase):
    def test_deny_wins_even_in_healthcare_industry(self):
        # Real card: telesales for ayurvedic products, industry "Others".
        keep, signal = is_healthcare(
            "Urgent Requirement | Telesales (Ayurvedic, Healthcare & FMCG "
            "Sector)", "Others")
        self.assertFalse(keep)
        self.assertEqual(signal, "deny")

    def test_clinical_title(self):
        keep, signal = is_healthcare("Staff Nurse - ICU", "Others")
        self.assertTrue(keep)
        self.assertEqual(signal, "title")

    def test_industry_rescues_blank_title(self):
        keep, signal = is_healthcare("Front Office Executive",
                                     "Medical / Healthcare")
        self.assertTrue(keep)
        self.assertEqual(signal, "industry")

    def test_neutral_industry_kept_for_review(self):
        keep, signal = is_healthcare("Ward Boy", "Others")
        self.assertTrue(keep)
        self.assertEqual(signal, "needs_review")

    def test_named_other_industry_excluded(self):
        keep, signal = is_healthcare("Process Associate",
                                     "IT Services & Consulting")
        self.assertFalse(keep)
        self.assertEqual(signal, "industry")

    def test_it_role_denied(self):
        keep, _ = is_healthcare("Java Developer - Hospital Chain",
                                "Medical / Healthcare")
        self.assertFalse(keep)


class TestClassifyCategory(unittest.TestCase):
    def test_nurse(self):
        self.assertEqual(
            classify_category("Wanted Staff Nurse, GNM, DGNM, ANM & Midwifery"),
            ("nurses", False))

    def test_pharmacist(self):
        self.assertEqual(classify_category("Pharmacist - Retail"),
                         ("pharmacists", False))

    def test_doctor(self):
        self.assertEqual(classify_category("Nephrologist"), ("doctors", False))
        self.assertEqual(classify_category("Psychiatrist"), ("doctors", False))

    def test_psychologist_is_non_clinical_not_doctor(self):
        self.assertEqual(classify_category("Clinical Psychologist"),
                         ("non_clinical", False))

    def test_non_clinical(self):
        self.assertEqual(classify_category("Medical Coding Executive"),
                         ("non_clinical", False))

    def test_ambiguous_flagged(self):
        category, ambiguous = classify_category("Ward Boy")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(ambiguous)


class TestCompanyType(unittest.TestCase):
    def test_pharma(self):
        self.assertEqual(classify_company_type("Sun Pharma Industries"),
                         "pharma")
        self.assertEqual(classify_company_type("Metropolis Diagnostics"),
                         "pharma")

    def test_hospital_default(self):
        self.assertEqual(classify_company_type("Apollo Hospitals"), "hospital")
        self.assertEqual(classify_company_type(""), "hospital")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        self.assertEqual(
            compute_cutoff(None, today=date(2026, 8, 8)), "2026-08-01")

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-06"]})
        self.assertEqual(compute_cutoff(df), "2026-08-04")

    def test_since_override(self):
        self.assertEqual(compute_cutoff(None, since="2026-07-01"), "2026-07-01")

    def test_empty_dates_fall_back_to_window(self):
        df = pd.DataFrame({"posted_date": ["", ""]})
        self.assertEqual(
            compute_cutoff(df, today=date(2026, 8, 8)), "2026-08-01")


class TestSmallParsers(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(parse_date("2026-07-27T14:02:15"), "2026-07-27")
        self.assertEqual(parse_date(None), "")
        self.assertEqual(parse_date("soon"), "")

    def test_page_url(self):
        self.assertEqual(page_url("healthcare", 1),
                         "https://www.shine.com/job-search/healthcare-jobs?sort=1")
        self.assertEqual(page_url("staff-nurse", 3),
                         "https://www.shine.com/job-search/staff-nurse-jobs-3?sort=1")

    def test_page_url_with_industry_param(self):
        self.assertEqual(
            page_url("healthcare?ind=13", 1),
            "https://www.shine.com/job-search/healthcare-jobs?sort=1&ind=13")
        self.assertEqual(
            page_url("healthcare?ind=13", 2),
            "https://www.shine.com/job-search/healthcare-jobs-2?sort=1&ind=13")

    def test_strip_html(self):
        self.assertEqual(
            strip_html("<p><strong>Staff Nurse</strong></p>\r\n<p>ICU &amp; "
                       "ER</p>"),
            "Staff Nurse ICU & ER")

    def test_truncate_marks_cut(self):
        text = "word " * 100
        cut = truncate_description(text.strip(), limit=50)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(len(cut), 51)

    def test_decode_job_type(self):
        self.assertEqual(decode_job_type({"jTypeC": 1, "jEType": 1}),
                         ("Full time", "Regular", "full_time"))
        self.assertEqual(decode_job_type({"jTypeC": 2, "jEType": 1}),
                         ("Part time", "Regular", "part_time"))
        self.assertEqual(decode_job_type({"jTypeC": 1, "jEType": 4}),
                         ("Full time", "Work from home", "remote"))


class TestClubRow(unittest.TestCase):
    def test_full_mapping(self):
        rich = {
            "location": "Noida, Delhi", "company": "Apollo Hospitals",
            "company_type": "hospital", "title": "Staff Nurse",
            "description": "ICU nurse.", "job_type": "Full time",
            "employment_type": "Regular", "category": "nurses",
            "job_url": "https://www.shine.com/jobs/staff-nurse/x/1",
            "posted_date": "2026-08-07", "experience_min_years": "1",
            "experience_max_years": "5",
            "salary_raw": "Rs 2.0  - 3.5 Lakh/Yr", "expires_date": "2026-09-19",
        }
        row = rich_row_to_club_row(rich)
        self.assertEqual(row["country_code"], "IN")
        self.assertEqual(row["city_name"], "Noida")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["category"], "nurses")
        self.assertEqual((row["min_salary"], row["max_salary"]),
                         ("200000", "350000"))
        self.assertEqual(row["salary_period"], "per_annum")
        self.assertEqual(row["salary_currency"], "INR")
        self.assertEqual(row["posted_at"], "2026-08-07")

    def test_city_falls_back_to_all_india(self):
        row = rich_row_to_club_row({"location": "", "salary_raw": ""})
        self.assertEqual(row["city_name"], "All India")


if __name__ == "__main__":
    unittest.main(verbosity=2)
