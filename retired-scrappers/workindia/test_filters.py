#!/usr/bin/env python3
"""Unit tests for the workindia scraper's parsers and filters.

Run with plain:  python test_filters.py
Worked examples come from real listings captured on 2026-08-08.
"""

import unittest
from datetime import date

import pandas as pd

from scraper import (
    classify_category,
    classify_company_type,
    classify_slug,
    compute_cutoff,
    description_fields,
    extract_job_posting,
    job_to_rich_row,
    parse_base_salary,
    parse_job_url,
    rich_row_to_club_row,
    slug_words,
)

# Real JobPosting JSON-LD (abridged) from
# https://www.workindia.in/jobs/nurse-saguna_more-patna-10823632/
REAL_POSTING = {
    "@type": "JobPosting",
    "title": "Nurse",
    "description": (
        "<p>Salary Range : Rs. 10000 - Rs. 15000 , based on skills, "
        "experience, and interview performance<br /><br />Educational "
        "Requirement : 12th Pass / Female Only<br /><br />Work Arrangement : "
        "Work From Office<br /><br />Gender Preference : Female only<br />"
        "<br />Experience Requirement : 1 - 2 Years of Experience<br /><br />"
        "Location : Saguna More<br /><br />Working Hours : 9:30 AM - 6:30 PM "
        "| Monday to Saturday</p>"),
    "identifier": {"@type": "PropertyValue",
                   "name": "Mt agency nursing care", "value": 10823632},
    "datePosted": "2026-08-08T05:10:17Z",
    "validThrough": "2026-09-17T00:00:00Z",
    "url": "https://www.workindia.in/jobs/nurse-saguna_more-patna-10823632/",
    "employmentType": "FULL_TIME",
    "experienceRequirements": {"monthsOfExperience": "12"},
    "jobLocation": {"@type": "Place", "address": {
        "addressLocality": "Saguna More", "addressRegion": "Bihar",
        "addressCountry": "IN", "postalCode": "801503"}},
    "hiringOrganization": {"name": "Mt agency nursing care",
                           "@type": "Organization"},
    "industry": "Nurse",
    "baseSalary": {"@type": "MonetaryAmount", "currency": "INR",
                   "value": {"@type": "QuantitativeValue", "minValue": 10000,
                             "maxValue": 15000, "unitText": "MONTH"}},
}

REAL_URL = "https://www.workindia.in/jobs/nurse-saguna_more-patna-10823632/"


class TestParseJobUrl(unittest.TestCase):
    def test_standard_url(self):
        self.assertEqual(
            parse_job_url("https://www.workindia.in/jobs/staff_nurse-jakkanpur-patna-10808711/"),
            ("staff_nurse", "jakkanpur", "patna", "10808711"))

    def test_multi_dash_area(self):
        # area made of several '-' separated tokens collapses into one
        title, area, city, job_id = parse_job_url(
            "https://www.workindia.in/jobs/nurse-raja_rajeshwari_nagar-bengaluru-10780685/")
        self.assertEqual((title, city, job_id),
                         ("nurse", "bengaluru", "10780685"))
        self.assertEqual(area, "raja_rajeshwari_nagar")

    def test_no_area_segment(self):
        self.assertEqual(parse_job_url("https://www.workindia.in/jobs/nurse-patna-123/"),
                         ("nurse", "", "patna", "123"))

    def test_non_job_url(self):
        self.assertIsNone(parse_job_url("https://www.workindia.in/nurse-jobs-in-patna/"))
        self.assertIsNone(parse_job_url("https://www.workindia.in/jobs/"))

    def test_slug_words(self):
        self.assertEqual(slug_words("saguna_more"), "Saguna More")
        self.assertEqual(slug_words(""), "")


class TestClassifySlug(unittest.TestCase):
    def test_clear_healthcare(self):
        for slug in ("nurse", "staff_nurse", "pharmacist", "lab_technician",
                     "physiotherapist", "medical_representative", "ward_boy",
                     "hospital_receptionist", "dental_assistant",
                     "x_ray_technician", "auxiliary_nurse_midwife_anm",
                     "ayurvedic_therapist", "receptionist_in_clinic"):
            self.assertEqual(classify_slug(slug), "allow", slug)

    def test_ambiguous_kept_for_review(self):
        for slug in ("caretaker", "skin_therapist", "health_insurance",
                     "spa_therapist"):
            self.assertEqual(classify_slug(slug), "ambiguous", slug)

    def test_non_healthcare_skipped(self):
        for slug in ("delivery_boy", "telecaller", "accountant",
                     "picker_packer", "hospitality_staff", "hotel_manager",
                     "field_sales_executive", "house_maid",
                     "chemistry_teacher"):
            self.assertEqual(classify_slug(slug), "skip", slug)

    def test_chemist_is_pharmacy_but_chemistry_is_not(self):
        self.assertEqual(classify_slug("chemist"), "allow")
        self.assertEqual(classify_slug("chemist_shop_assistant"), "allow")

    def test_hospitality_is_not_hospital(self):
        self.assertEqual(classify_slug("hospitality_executive"), "skip")
        self.assertEqual(classify_slug("hospital_attendant"), "allow")


class TestSalary(unittest.TestCase):
    def test_monthly_range(self):
        self.assertEqual(parse_base_salary(REAL_POSTING["baseSalary"]),
                         (10000, 15000, "MONTH"))

    def test_yearly_converted_to_monthly(self):
        base = {"value": {"minValue": 240000, "maxValue": 360000,
                          "unitText": "YEAR"}}
        self.assertEqual(parse_base_salary(base), (20000, 30000, "YEAR"))

    def test_missing_max_uses_min(self):
        base = {"value": {"minValue": 12000, "unitText": "MONTH"}}
        self.assertEqual(parse_base_salary(base), (12000, 12000, "MONTH"))

    def test_swapped_bounds(self):
        base = {"value": {"minValue": 25000, "maxValue": 20000,
                          "unitText": "MONTH"}}
        self.assertEqual(parse_base_salary(base), (20000, 25000, "MONTH"))

    def test_undisclosed_never_invented(self):
        self.assertEqual(parse_base_salary(None), ("", "", ""))
        self.assertEqual(parse_base_salary({}), ("", "", ""))
        self.assertEqual(parse_base_salary({"value": {}}), ("", "", ""))


class TestDescriptionFields(unittest.TestCase):
    def test_fields(self):
        text = ("Salary Range : Rs. 10000 - Rs. 15000 , based on skills\n"
                "Work Arrangement : Work From Office\n"
                "Working Hours : 9:30 AM - 6:30 PM | Monday to Saturday")
        fields = description_fields(text)
        self.assertIn("Rs. 10000", fields["salary range"])
        self.assertEqual(fields["work arrangement"], "Work From Office")
        # value keeps its own colons intact
        self.assertTrue(fields["working hours"].startswith("9:30 AM"))


class TestCategoryAndCompany(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(classify_category("Staff Nurse"), "nurses")
        self.assertEqual(classify_category("Pharmacist"), "pharmacists")
        self.assertEqual(classify_category("Doctor MBBS"), "doctors")
        self.assertEqual(classify_category("Lab Technician"), "non_clinical")
        self.assertEqual(classify_category("Chemist"), "pharmacists")
        self.assertEqual(classify_category("Chemistry Teacher"), "non_clinical")
        self.assertEqual(classify_category("Lab Chemist"), "non_clinical")

    def test_company_type(self):
        self.assertEqual(classify_company_type("Apollo Diagnostics"), "pharma")
        self.assertEqual(classify_company_type("City Hospital"), "hospital")
        self.assertEqual(
            classify_company_type("Sun Distributors", "Medical Representative"),
            "pharma")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        cutoff = compute_cutoff(None, today=date(2026, 8, 8))
        self.assertEqual(cutoff, "2026-07-09")  # 30 days back

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-05", ""]})
        self.assertEqual(compute_cutoff(df), "2026-08-03")  # max - 2 days

    def test_empty_dates_fall_back_to_window(self):
        df = pd.DataFrame({"posted_date": ["", "bad-date"]})
        self.assertEqual(compute_cutoff(df, today=date(2026, 8, 8)),
                         "2026-07-09")


class TestRowBuilding(unittest.TestCase):
    def make_row(self):
        return job_to_rich_row(REAL_POSTING, REAL_URL,
                               parse_job_url(REAL_URL), needs_review=False)

    def test_rich_row(self):
        row = self.make_row()
        self.assertEqual(row["job_id"], "10823632")
        self.assertEqual(row["title"], "Nurse")
        self.assertEqual(row["company"], "Mt agency nursing care")
        self.assertEqual(row["city"], "Patna")
        self.assertEqual(row["area"], "Saguna More")
        self.assertEqual(row["state"], "Bihar")
        self.assertEqual(row["posted_date"], "2026-08-08")
        self.assertEqual(row["salary_min_monthly"], 10000)
        self.assertEqual(row["salary_max_monthly"], 15000)
        self.assertEqual(row["salary_raw"], "Rs. 10000 - Rs. 15000")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["category"], "nurses")
        self.assertEqual(row["experience_months"], "12")

    def test_remote_from_work_arrangement(self):
        posting = dict(REAL_POSTING)
        posting["description"] = "<p>Work Arrangement : Work From Home</p>"
        row = job_to_rich_row(posting, REAL_URL, parse_job_url(REAL_URL), False)
        self.assertEqual(row["job_type"], "remote")

    def test_club_row(self):
        club = rich_row_to_club_row(self.make_row())
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["city_name"], "Patna")
        self.assertEqual(club["category"], "nurses")
        self.assertEqual(club["min_salary"], "10000")
        self.assertEqual(club["max_salary"], "15000")
        self.assertEqual(club["salary_period"], "per_month")
        self.assertEqual(club["salary_currency"], "INR")
        self.assertEqual(club["min_experience"], "1")  # 12 months
        self.assertEqual(club["posted_at"], "2026-08-08")
        self.assertEqual(club["expires_at"], "2026-09-17")

    def test_undisclosed_salary_club_row(self):
        posting = dict(REAL_POSTING)
        posting.pop("baseSalary")
        posting["description"] = "<p>Work Arrangement : Work From Office</p>"
        row = job_to_rich_row(posting, REAL_URL, parse_job_url(REAL_URL), False)
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        club = rich_row_to_club_row(row)
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")


class TestJsonLdExtraction(unittest.TestCase):
    def test_extracts_jobposting(self):
        html = ('<html><script type="application/ld+json">'
                '{"@type": "BreadCrumbList"}</script>'
                '<script type="application/ld+json">'
                '{"@type": "JobPosting", "title": "Nurse"}</script></html>')
        posting = extract_job_posting(html)
        self.assertEqual(posting["title"], "Nurse")

    def test_none_when_absent(self):
        self.assertIsNone(extract_job_posting("<html></html>"))
        self.assertIsNone(extract_job_posting(
            '<script type="application/ld+json">not json</script>'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
