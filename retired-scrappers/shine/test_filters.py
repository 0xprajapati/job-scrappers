#!/usr/bin/env python3
"""Unit tests for the shine.com scraper's parsers and filters.

Run with plain `python test_filters.py` (no pytest needed). Worked examples
come from real listings captured on 2026-08-08.
"""

import unittest

from shine_scraper import (
    apply_classification,
    classify_company_type,
    club_salary,
    compute_cutoff,
    decode_job_type,
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


class TestClassificationWiring(unittest.TestCase):
    """The shared classifier is the ONLY keep/drop + labeling decision.

    Engine internals live in _shared/test_classification.py; these tests
    only prove shine hands it the right signals and honours the verdict.
    """

    def test_in_scope_title_is_kept_and_labelled(self):
        row = {"title": "Pharmacovigilance Associate",
               "keywords": "drug safety, argus", "description": ""}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")
        self.assertEqual(row["role_family"], "Pharmacovigilance")
        self.assertIn("title", row["matched_in"])

    def test_public_health_title(self):
        row = {"title": "Public Health Epidemiologist",
               "keywords": "", "description": ""}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")

    def test_bedside_role_is_dropped(self):
        # The old scraper kept this as category=nurses; it is now out of scope.
        row = {"title": "Wanted Staff Nurse, GNM, DGNM, ANM & Midwifery",
               "keywords": "icu, ward", "description": ""}
        self.assertFalse(apply_classification(row))

    def test_telesales_card_is_dropped(self):
        # Real card: telesales for ayurvedic products, industry "Others".
        row = {"title": "Urgent Requirement | Telesales (Ayurvedic, "
                        "Healthcare & FMCG Sector)",
               "keywords": "", "description": ""}
        self.assertFalse(apply_classification(row))

    def test_industry_facet_is_not_a_signal(self):
        # jInd "Medical / Healthcare" no longer rescues an off-scope title.
        row = {"title": "Front Office Executive", "keywords": "",
               "description": ""}
        self.assertFalse(apply_classification(row))

    def test_skills_only_admission(self):
        # jKwd is passed as `skills` (weight 2): two distinct terms clear
        # MIN_SCORE_KEEP with no title hit at all.
        row = {"title": "Senior Associate",
               "keywords": "clinical trials, gcp, cra",
               "description": ""}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["role_family"], "Clinical Research")


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
            "description": "Medical coding, CPC certified.",
            "job_type": "Full time",
            "employment_type": "Regular",
            "category": "Non Clinical", "sub_category": "Medical Coding",
            "job_url": "https://www.shine.com/jobs/medical-coder/x/1",
            "posted_date": "2026-08-07", "experience_min_years": "1",
            "experience_max_years": "5",
            "salary_raw": "Rs 2.0  - 3.5 Lakh/Yr", "expires_date": "2026-09-19",
        }
        row = rich_row_to_club_row(rich)
        self.assertEqual(row["country_code"], "IN")
        self.assertEqual(row["city_name"], "Noida")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")
        self.assertEqual(row["qualification"], "CPC")
        self.assertNotIn("is_active", row)
        self.assertNotIn("expires_at", row)
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
