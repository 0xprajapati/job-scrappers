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
    CLUB_COLUMNS,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_detail,
    classify_row,
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


class TestSharedClassifierWiring(unittest.TestCase):
    """The shared classify_job is the only keep/drop authority; these tests
    only check the wiring (the engine itself is covered by
    _shared/test_classification.py)."""

    @staticmethod
    def _row(title, designation="", department="", description=""):
        return {"title": title, "designation": designation,
                "department": department, "skills": "",
                "description": description,
                "category": "", "sub_category": "", "role_family": "",
                "all_families": "", "family_scores": "",
                "family_confidence": "", "matched_in": "",
                "needs_review": False}

    def test_in_scope_role_gets_taxonomy_labels(self):
        row = self._row("Clinical Research Coordinator", "Coordinator",
                        "Clinical Research")
        verdict = classify_row(row)
        self.assertTrue(verdict["in_scope"])
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], verdict["role_family"])

    def test_clinical_title_is_out_of_scope(self):
        # Hospital ATS: nursing requisitions are vetoed and dropped
        row = self._row("Staff Nurse", "Staff Nurse",
                        "Nursing Administration - Wards")
        verdict = classify_row(row)
        self.assertFalse(verdict["in_scope"])

    def test_department_signal_reaches_classifier(self):
        # Bare title scores in-scope only through the department (skills x2)
        row = self._row("Associate", department="Pharmacovigilance | Drug Safety")
        verdict = classify_row(row)
        self.assertTrue(verdict["in_scope"])
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")


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
        self.assertEqual(row["category"], "")  # filled by classify_row later
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

        row["category"], row["sub_category"] = "Non Clinical", "Clinical Research"
        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))  # shared contract
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        self.assertEqual(club["qualification"], "B.Pharm")  # structured field wins
        self.assertEqual(club["min_salary"], "216000")
        self.assertEqual(club["salary_period"], "per_annum")
        self.assertNotIn("is_active", club)   # retired
        self.assertNotIn("expires_at", club)  # retired
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["company_type"], "hospital")

    def test_qualification_extracted_when_no_structured_field(self):
        row = job_to_rich_row(self.LISTING)
        row["description"] = "Applicants must hold an MPH degree."
        club = rich_row_to_club_row(row)
        self.assertIn("MPH", club["qualification"])

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
