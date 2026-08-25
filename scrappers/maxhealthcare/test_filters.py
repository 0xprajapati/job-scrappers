#!/usr/bin/env python3
"""Unit tests for the maxhealthcare scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real listings on
maxhealthcarecareers.peoplestrong.com (July 2026).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    CLUB_COLUMNS,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    apply_detail,
    classification_skills,
    compute_cutoff,
    detail_id_from_job,
    job_to_rich_row,
    parse_ctc_range,
    parse_exp_range,
    parse_location,
    parse_org,
    parse_salary_pair,
    rich_row_to_club_row,
    strip_html,
)


class TestSalaryParser(unittest.TestCase):
    """Real CTC values seen on the site — the API mixes absolute annual INR
    with lakhs-per-annum figures."""

    def test_absolute_annual_range(self):
        # Patient Care Coordinator MHC/28734: "200000.0000-400000.0000"
        parsed = parse_ctc_range("200000.0000-400000.0000")
        self.assertEqual(parsed["salary_min"], 200000)
        self.assertEqual(parsed["salary_max"], 400000)
        self.assertEqual(parsed["salary_period"], "per_annum")
        self.assertEqual(parsed["salary_currency"], "INR")
        self.assertEqual(parsed["salary_raw"], "INR 200,000 - 400,000 per year")

    def test_lakh_range(self):
        # Manager - Social Media MHC/28638: "13.0000-15.0000" == 13-15 LPA
        parsed = parse_ctc_range("13.0000-15.0000")
        self.assertEqual(parsed["salary_min"], 1300000)
        self.assertEqual(parsed["salary_max"], 1500000)

    def test_detail_lakh_strings(self):
        # Detail API for the same job: minSalary "13" / maxSalary "15"
        parsed = parse_salary_pair("13", "15")
        self.assertEqual(parsed["salary_min"], 1300000)
        self.assertEqual(parsed["salary_max"], 1500000)

    def test_detail_absolute_strings(self):
        # Detail API MHC/28734: minSalary "200000" / maxSalary "400000"
        parsed = parse_salary_pair("200000", "400000")
        self.assertEqual(parsed["salary_min"], 200000)
        self.assertEqual(parsed["salary_max"], 400000)

    def test_missing_is_not_disclosed(self):
        self.assertEqual(parse_ctc_range(None), {})
        self.assertEqual(parse_ctc_range(""), {})
        self.assertEqual(parse_ctc_range("0.0000-0.0000"), {})
        self.assertEqual(parse_salary_pair("0", "0"), {})
        self.assertEqual(parse_salary_pair(None, None), {})

    def test_single_value(self):
        parsed = parse_salary_pair("300000", None)
        self.assertEqual(parsed["salary_min"], 300000)
        self.assertEqual(parsed["salary_max"], 300000)

    def test_swapped_range(self):
        parsed = parse_salary_pair("400000", "300000")
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         (300000, 400000))


class TestExpRange(unittest.TestCase):
    def test_real_ranges(self):
        self.assertEqual(parse_exp_range("1-3 years"), ("1", "3"))
        self.assertEqual(parse_exp_range("8-10 years"), ("8", "10"))
        self.assertEqual(parse_exp_range("12-18 years"), ("12", "18"))

    def test_absent(self):
        self.assertEqual(parse_exp_range(None), ("", ""))
        self.assertEqual(parse_exp_range(""), ("", ""))

    def test_single_number(self):
        self.assertEqual(parse_exp_range("3 years"), ("3", "3"))


class TestClassificationWiring(unittest.TestCase):
    """The scraper delegates every keep/drop + label decision to
    _shared/classification.py; these tests check the wiring only (the engine
    itself is covered by _shared/test_classification.py)."""

    def test_skills_signal_joins_designation_department_and_tags(self):
        row = {"designation": "Clinical Research Coordinator",
               "department": "Clinical Research - Trials",
               "skills": "GCP; ICH"}
        self.assertEqual(classification_skills(row),
                         "Clinical Research Coordinator "
                         "Clinical Research - Trials GCP; ICH")

    def test_in_scope_role_is_labelled(self):
        row = {"title": "Clinical Research Coordinator",
               "designation": "Clinical Research Coordinator",
               "department": "Clinical Research - Clinical Research",
               "skills": "", "description": ""}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")

    def test_bedside_nursing_is_dropped(self):
        row = {"title": "Deputy Nursing Superintendent/Nursing "
                        "Superintendent- Nursing",
               "designation": "Deputy Nursing Superintendent",
               "department": "Nursing - Nursing", "skills": "",
               "description": ""}
        self.assertFalse(apply_classification(row))
        self.assertEqual(row["category"], "")
        self.assertEqual(row["sub_category"], "")

    def test_clinician_requisition_is_dropped(self):
        row = {"title": "Consultant - Oncology", "designation": "Consultant",
               "department": "Oncology & Oncosurgery - Medical Oncology",
               "skills": "", "description": ""}
        self.assertFalse(apply_classification(row))

    def test_hospital_front_office_is_dropped(self):
        row = {"title": "patient care coordinator",
               "designation": "Patient Care Coordinator",
               "department": "Front Office - Front Office - OPD",
               "skills": "", "description": ""}
        self.assertFalse(apply_classification(row))


class TestHierarchies(unittest.TestCase):
    def test_location_titlecases_state(self):
        country, state, city = parse_location(
            "India>HARYANA>Gurugram>Cluster 1>Cluster 1>Gurgaon>Gurgaon>"
            "Gurgaon-Max Hospital")
        self.assertEqual((country, state, city),
                         ("India", "Haryana", "Gurugram"))

    def test_location_two_word_state(self):
        _, state, city = parse_location(
            "India>UTTAR PRADESH>Lucknow>New Projects>New Projects>Lucknow>"
            "Lucknow>Lucknow - Max Super Speciality Hospital")
        self.assertEqual((state, city), ("Uttar Pradesh", "Lucknow"))

    def test_org_strips_formerly(self):
        group, entity, dept = parse_org(
            "MHC>Alps Hospital Limited (Formerly known as Max Hospitals and "
            "Allied Services Ltd)>Front Office>Front Office - OPD")
        self.assertEqual(group, "MHC")
        self.assertEqual(entity, "Alps Hospital Limited")
        self.assertEqual(dept, "Front Office - Front Office - OPD")

    def test_detail_id(self):
        self.assertEqual(detail_id_from_job(
            {"jobDetailUrl": "https://maxhealthcarecareers.peoplestrong.com/job/detail/MHC_28734"}),
            "MHC_28734")
        self.assertEqual(detail_id_from_job({"jobCode": "MHC/28734"}),
                         "MHC_28734")


class TestRowBuilding(unittest.TestCase):
    LISTING = {  # trimmed real listing (Patient Care Coordinator, Gurgaon)
        "organizationUnitComplete": "MHC>Alps Hospital Limited (Formerly "
            "known as Max Hospitals and Allied Services Ltd)>Front Office>"
            "Front Office - OPD",
        "jobPostedDate": "2026-07-24",
        "locationHierarchyComplete": "India>HARYANA>Gurugram>Cluster 1>"
            "Cluster 1>Gurgaon>Gurgaon>Gurgaon-Max Hospital",
        "jobDetailUrl": "https://maxhealthcarecareers.peoplestrong.com/job/detail/MHC_28734",
        "designation": "Patient Care Coordinator", "requisitionId": 1810337,
        "jobTitle": "patient care coordinator", "jobCode": "MHC/28734",
        "openings": 2, "organizationUnit": "Front Office",
        "jobClosureDate": "2026-08-23", "expRange": "1-3 years",
        "CTCRange": "200000.0000-400000.0000",
        "skills": {"mustTohave": [], "goodtohave": []},
        "minBudgetSalary": 0.0, "maxBudgetSalary": 0.0,
    }

    def test_rich_row(self):
        row = job_to_rich_row(self.LISTING)
        self.assertEqual(row["job_id"], "1810337")
        # taxonomy fields stay blank until apply_classification() runs
        self.assertEqual(row["category"], "")
        self.assertEqual(row["sub_category"], "")
        self.assertEqual(row["city"], "Gurugram")
        self.assertEqual(row["state"], "Haryana")
        self.assertEqual(row["company"], "Alps Hospital Limited")
        self.assertEqual(row["salary_min"], 200000)
        self.assertEqual(row["salary_max"], 400000)
        self.assertEqual(row["posted_date"], "2026-07-24")
        self.assertEqual(row["min_experience"], "1")
        self.assertEqual(row["max_experience"], "3")
        self.assertEqual(row["job_url"],
                         "https://maxhealthcarecareers.peoplestrong.com/job/detail/MHC_28734")
        self.assertFalse(row["needs_review"])

    def test_apply_detail_and_club_row(self):
        # same requisition shape, but an in-scope role so the club row is
        # the one the exporter would actually write
        listing = dict(self.LISTING, jobTitle="clinical research coordinator",
                       designation="Clinical Research Coordinator",
                       organizationUnitComplete="MHC>Alps Hospital Limited>"
                                                "Clinical Research>Trials")
        row = job_to_rich_row(listing)
        apply_detail(row, {
            "jobDescription": "<p>Coordinates ethics submissions and CRF "
                              "data for ongoing trials.</p>",
            "qualifications": ["Graduate"], "employmentType": "Employee",
        })
        self.assertTrue(row["description"].startswith("Coordinates ethics"))
        self.assertEqual(row["qualifications"], "Graduate")
        self.assertEqual(row["job_type"], "full_time")  # Employee -> full_time
        self.assertTrue(apply_classification(row))

        club = rich_row_to_club_row(row)
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        self.assertEqual(club["qualification"], "Graduate")
        self.assertEqual(club["min_salary"], "200000")
        self.assertEqual(club["max_salary"], "400000")
        self.assertEqual(club["salary_period"], "per_annum")
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)

    def test_detail_salary_fills_missing(self):
        listing = dict(self.LISTING, CTCRange=None)
        row = job_to_rich_row(listing)
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        apply_detail(row, {"minSalary": "13", "maxSalary": "15"})
        self.assertEqual(row["salary_min"], 1300000)
        self.assertEqual(row["salary_max"], 1500000)

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
