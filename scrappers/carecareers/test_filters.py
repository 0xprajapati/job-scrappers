#!/usr/bin/env python3
"""Unit tests for the carecareers scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real listings on carecareers.peoplestrong.com
(July 2026).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_detail,
    classify_category,
    compute_cutoff,
    detail_id_from_job,
    job_to_rich_row,
    parse_budget_salary,
    parse_exp_range,
    parse_location,
    parse_org,
    rich_row_to_club_row,
    strip_html,
)


class TestSalaryParser(unittest.TestCase):
    """Real budget values seen on the site (annual INR)."""

    def test_pharmacist_range(self):
        # Pharmacist QCI/P/1806731: 216000.0 - 240000.0
        parsed = parse_budget_salary(216000.0, 240000.0)
        self.assertEqual(parsed["salary_min"], 216000)
        self.assertEqual(parsed["salary_max"], 240000)
        self.assertEqual(parsed["salary_period"], "per_annum")
        self.assertEqual(parsed["salary_currency"], "INR")
        self.assertEqual(parsed["salary_raw"], "INR 216,000 - 240,000 per year")

    def test_consultant_range_strings(self):
        # Detail API returns strings: "2700000" - "2900000"
        parsed = parse_budget_salary("2700000", "2900000")
        self.assertEqual(parsed["salary_min"], 2700000)
        self.assertEqual(parsed["salary_max"], 2900000)

    def test_missing_is_not_disclosed(self):
        self.assertEqual(parse_budget_salary(None, None), {})
        self.assertEqual(parse_budget_salary(0, 0), {})
        self.assertEqual(parse_budget_salary("", "0"), {})

    def test_single_value(self):
        parsed = parse_budget_salary(300000, None)
        self.assertEqual(parsed["salary_min"], 300000)
        self.assertEqual(parsed["salary_max"], 300000)

    def test_swapped_range(self):
        parsed = parse_budget_salary(400000, 300000)
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         (300000, 400000))


class TestExpRange(unittest.TestCase):
    def test_real_ranges(self):
        self.assertEqual(parse_exp_range("1-2 years"), ("1", "2"))
        self.assertEqual(parse_exp_range("5-7 years"), ("5", "7"))
        self.assertEqual(parse_exp_range("2-5 years"), ("2", "5"))

    def test_absent(self):
        self.assertEqual(parse_exp_range(None), ("", ""))
        self.assertEqual(parse_exp_range(""), ("", ""))

    def test_single_number(self):
        self.assertEqual(parse_exp_range("3 years"), ("3", "3"))


class TestClassifier(unittest.TestCase):
    """Titles/departments from real listings."""

    def test_staff_nurse(self):
        category, review = classify_category(
            "Staff Nurse", "Staff Nurse", "Nursing Administration - Wards")
        self.assertEqual(category, "nurses")
        self.assertFalse(review)

    def test_pharmacist(self):
        category, review = classify_category(
            "Pharmacist - Logistic", "Pharmacist", "Logistics - Pharmacy OP")
        self.assertEqual(category, "pharmacists")
        self.assertFalse(review)

    def test_consultant_clinical_is_doctor(self):
        category, review = classify_category(
            "Consultant - Internal", "Consultant", "Clinical - Internal Medicine")
        self.assertEqual(category, "doctors")
        self.assertFalse(review)

    def test_consultant_typo_is_doctor(self):
        # Real listing "Counsultant" in Clinical Administration - Orthopaedics
        category, _ = classify_category(
            "Counsultant", "Junior Consultant",
            "Clinical Administration - Orthopaedics")
        self.assertEqual(category, "doctors")

    def test_pathology_technician_non_clinical_no_review(self):
        category, review = classify_category(
            "Technician/Executive - Pathology", "Executive",
            "Laboratory - Pathology")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(review)  # clear healthcare signal

    def test_marketing_executive_flagged(self):
        category, review = classify_category(
            "Senior Executive", "Senior Executive",
            "Marketing & Business Development - Marketing")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(review)  # no healthcare word anywhere

    def test_admission_executive_not_doctor(self):
        category, _ = classify_category(
            "Executive - Admission | General Administration", "Executive",
            "General Administration Services - Administraton General")
        self.assertEqual(category, "non_clinical")


class TestHierarchies(unittest.TestCase):
    def test_location(self):
        country, state, city = parse_location(
            "India>Maharashtra>Aurangabad>Shahanoorwadi-1>CCIIGMA 1>Z3>R2")
        self.assertEqual((country, state, city),
                         ("India", "Maharashtra", "Aurangabad"))

    def test_org(self):
        group, entity, dept = parse_org(
            "Quality Care India Limited>United CIIGMA Institute of Medical "
            "Sciences Pvt Ltd.>Logistics>Pharmacy IP")
        self.assertEqual(group, "Quality Care India Limited")
        self.assertEqual(entity,
                         "United CIIGMA Institute of Medical Sciences Pvt Ltd.")
        self.assertEqual(dept, "Logistics - Pharmacy IP")

    def test_detail_id(self):
        self.assertEqual(detail_id_from_job(
            {"jobDetailUrl": "https://carecareers.peoplestrong.com/job/detail/QCI_P_1806731"}),
            "QCI_P_1806731")
        self.assertEqual(detail_id_from_job({"jobCode": "QCI/P/1806731"}),
                         "QCI_P_1806731")


class TestRowBuilding(unittest.TestCase):
    LISTING = {  # trimmed real listing (Pharmacist, Aurangabad)
        "organizationUnitComplete": "Quality Care India Limited>United CIIGMA "
            "Institute of Medical Sciences Pvt Ltd.>Logistics>Pharmacy IP",
        "jobPostedDate": "2026-07-20",
        "locationHierarchyComplete": "India>Maharashtra>Aurangabad>"
            "Shahanoorwadi-1>CCIIGMA 1>Z3>R2 - Shahanoorwadi-1",
        "jobDetailUrl": "https://carecareers.peoplestrong.com/job/detail/QCI_P_1806731",
        "designation": "Pharmacist", "requisitionId": 1806731,
        "jobTitle": "Pharmacist", "jobCode": "QCI/P/1806731", "openings": 1,
        "organizationUnit": "Quality Care India Limited",
        "jobClosureDate": "2026-08-19", "expRange": "1-2 years",
        "skills": {"mustTohave": [], "goodtohave": ["Good Commuincation"]},
        "minBudgetSalary": 216000.0, "maxBudgetSalary": 240000.0,
    }

    def test_rich_row(self):
        row = job_to_rich_row(self.LISTING)
        self.assertEqual(row["job_id"], "1806731")
        self.assertEqual(row["category"], "pharmacists")
        self.assertEqual(row["city"], "Aurangabad")
        self.assertEqual(row["company"],
                         "United CIIGMA Institute of Medical Sciences Pvt Ltd.")
        self.assertEqual(row["salary_min"], 216000)
        self.assertEqual(row["posted_date"], "2026-07-20")
        self.assertEqual(row["min_experience"], "1")
        self.assertEqual(row["job_url"],
                         "https://carecareers.peoplestrong.com/job/detail/QCI_P_1806731")
        self.assertFalse(row["needs_review"])

    def test_apply_detail_and_club_row(self):
        row = job_to_rich_row(self.LISTING)
        apply_detail(row, {
            "jobDescription": "<ul><li>Dispensing of medicines.</li></ul>",
            "qualifications": ["B.Pharm"], "employmentType": "Regular",
        })
        self.assertEqual(row["description"], "Dispensing of medicines.")
        self.assertEqual(row["qualifications"], "B.Pharm")
        self.assertEqual(row["job_type"], "full_time")  # Regular -> full_time

        club = rich_row_to_club_row(row)
        self.assertEqual(club["category"], "pharmacists")
        self.assertEqual(club["min_salary"], "216000")
        self.assertEqual(club["salary_period"], "per_annum")
        self.assertEqual(club["expires_at"], "2026-08-19")
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["company_type"], "hospital")

    def test_strip_html(self):
        self.assertEqual(strip_html("<p>a&amp;b</p><br>c"), "a&b c")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark(self):
        df = pd.DataFrame({"posted_date": ["2026-07-10", "2026-07-20", ""]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 7, 20) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_all_dates_bad_falls_back(self):
        df = pd.DataFrame({"posted_date": ["", "not-a-date"]})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
