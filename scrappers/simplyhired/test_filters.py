#!/usr/bin/env python3
"""Unit tests for the simplyhired scraper's parsers and filters.

Run with plain:  python test_filters.py
Worked examples come from real __NEXT_DATA__ payloads captured on 2026-07-12.
"""

import unittest
from datetime import date

import pandas as pd

from simplyhired_scraper import (
    classify_category,
    classify_company_type,
    compute_cutoff,
    epoch_ms_to_date,
    extract_page_props,
    job_to_rich_row,
    map_job_type,
    parse_city,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
)


class TestExtractPageProps(unittest.TestCase):
    def test_extracts_page_props(self):
        html = ('<html><script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"pageProps":{"resultCount":42,"jobs":[]}}}'
                '</script></html>')
        self.assertEqual(extract_page_props(html),
                         {"resultCount": 42, "jobs": []})

    def test_missing_or_bad_payload(self):
        self.assertIsNone(extract_page_props("<html>nope</html>"))
        self.assertIsNone(extract_page_props(
            '<script id="__NEXT_DATA__" type="application/json">{bad'
            '</script>'))


class TestParseCity(unittest.TestCase):
    def test_area_city_state(self):
        # real listing: "Saibaba Colony, Coimbatore, Tamil Nadu"
        self.assertEqual(parse_city("Saibaba Colony, Coimbatore, Tamil Nadu"),
                         "Coimbatore")

    def test_city_state(self):
        self.assertEqual(parse_city("Bengaluru, Karnataka"), "Bengaluru")
        self.assertEqual(parse_city("Delhi, Delhi"), "Delhi")

    def test_bare_region(self):
        self.assertEqual(parse_city("Goa"), "Goa")

    def test_empty(self):
        self.assertEqual(parse_city(""), "")
        self.assertEqual(parse_city(None), "")


class TestEpochMsToDate(unittest.TestCase):
    def test_real_listing_epoch_ms(self):
        # dateOnIndeed from a live listing (July 2026)
        self.assertEqual(epoch_ms_to_date(1783514221439), "2026-07-08")

    def test_garbage(self):
        self.assertEqual(epoch_ms_to_date(None), "")
        self.assertEqual(epoch_ms_to_date("recently"), "")


class TestParseSalary(unittest.TestCase):
    def test_monthly_range(self):
        # real: "₹18,000 - ₹25,000 a month"
        self.assertEqual(parse_salary("₹18,000 - ₹25,000 a month"),
                         ("₹18,000 - ₹25,000 a month", "18000", "25000",
                          "per_month"))

    def test_from_yearly_lakh_grouping(self):
        # real: "From ₹18,00,000 a year" (Indian digit grouping)
        self.assertEqual(parse_salary("From ₹18,00,000 a year"),
                         ("From ₹18,00,000 a year", "1800000", "1800000",
                          "per_annum"))

    def test_decimal_amounts(self):
        # real: "₹75,000.86 - ₹1,00,000.85 a month"
        raw = "₹75,000.86 - ₹1,00,000.85 a month"
        self.assertEqual(parse_salary(raw),
                         (raw, "75001", "100001", "per_month"))

    def test_hourly_kept_raw_but_not_normalized(self):
        raw = "Up to ₹350 an hour"
        self.assertEqual(parse_salary(raw), (raw, "", "", ""))

    def test_missing(self):
        self.assertEqual(parse_salary(None), ("Not Disclosed", "", "", ""))
        self.assertEqual(parse_salary(""), ("Not Disclosed", "", "", ""))


class TestMapJobType(unittest.TestCase):
    def test_full_time_wins_over_part_time(self):
        # real listings carry both: ["Part-time", "Full-time"]
        self.assertEqual(map_job_type(["Part-time", "Full-time"]), "full_time")

    def test_pure_part_time(self):
        self.assertEqual(map_job_type(["Part-time"]), "part_time")

    def test_remote_and_hybrid_hints(self):
        self.assertEqual(map_job_type(["Full-time"], ["Remote"]), "remote")
        self.assertEqual(map_job_type(["Full-time"], [], ["Hybrid work"]),
                         "hybrid")

    def test_defaults(self):
        self.assertEqual(map_job_type([]), "full_time")
        self.assertEqual(map_job_type(["Internship"]), "part_time")


class TestClassifyCategory(unittest.TestCase):
    def test_doctors(self):
        for title in ("bhms doctor", "MD MEDICINE (General Medicine)",
                      "General Dentist", "Consultant Pathologist",
                      "Duty Doctor", "Ayurvedic Physician"):
            self.assertEqual(classify_category(title), ("doctors", False), title)

    def test_nurses(self):
        for title in ("Skin therapist/ nurse practitioner", "Staff Nurse",
                      "GNM Nursing", "Nursing Sister"):
            self.assertEqual(classify_category(title), ("nurses", False), title)

    def test_pharmacists(self):
        for title in ("Pharmacist", "B Pharma Fresher", "Pharmacy Incharge"):
            self.assertEqual(classify_category(title), ("pharmacists", False), title)

    def test_known_non_clinical(self):
        for title in ("Patient Care Executive - Medical Receptionist",
                      "Home Health Care Manager", "Medical Billing Executive",
                      "Dialysis Technician", "Healthcare Recruiter Trainer"):
            self.assertEqual(classify_category(title), ("non_clinical", False), title)

    def test_unknown_flagged_never_dropped(self):
        category, needs_review = classify_category("Wellness Evangelist")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)


class TestCutoff(unittest.TestCase):
    def test_first_run_matches_source_filter(self):
        self.assertEqual(INITIAL_WINDOW_DAYS, 15)
        self.assertEqual(compute_cutoff(None, today=date(2026, 7, 12)),
                         "2026-06-27")

    def test_watermark_minus_grace(self):
        existing = pd.DataFrame({"posted_date": ["2026-07-05", "2026-07-10"]})
        self.assertEqual(WATERMARK_GRACE_DAYS, 2)
        self.assertEqual(compute_cutoff(existing), "2026-07-08")


class TestRowBuilding(unittest.TestCase):
    LISTING_JOB = {  # trimmed real listing payload
        "jobKey": "sfOKFX5pCK6jHnNhq_zBPvg_I6Lfvj2Ni4zfMvX5mloaZxQgtfk8nQ",
        "title": "bhms doctor",
        "company": "MODERN HOMEOPATHY PVT LTD",
        "location": "Goa",
        "salaryInfo": "₹18,000 - ₹25,000 a month",
        "dateOnIndeed": 1782287450027,
        "jobTypes": ["Full-time"],
        "remoteAttributes": [],
        "snippet": "Provide guidance on healthy lifestyle habits…",
        "sponsored": True,
        "botUrl": "/job/sfOKFX5pCK6jHnNhq_zBPvg_I6Lfvj2Ni4zfMvX5mloaZxQgtfk8nQ",
    }
    DETAIL = {  # trimmed real /job/<key> pageProps
        "jobDescriptionHtml": "<p>Provide guidance on <b>healthy</b> lifestyle "
                              "habits, nutrition, and preventive care.</p>",
        "formattedLocation": "Panaji, Goa",
        "state": "GA",
        "jobTypes": ["Full-time"],
        "workSettings": [],
        "compensation": "₹18,000 - ₹25,000 a month",
        "datePublished": 1782287450027,
        "employerName": "MODERN HOMEOPATHY PVT LTD",
        "employerSquareLogoUrl": None,
        "qualifications": ["Bachelor's degree", "Patient care"],
        "benefits": ["Paid time off"],
    }

    def test_rich_row_listing_only(self):
        row = job_to_rich_row(self.LISTING_JOB, None)
        self.assertEqual(row["job_id"], self.LISTING_JOB["jobKey"])
        self.assertEqual(row["city"], "Goa")
        self.assertEqual(row["category"], "doctors")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["salary_min"], "18000")
        self.assertEqual(row["salary_period"], "per_month")
        self.assertEqual(row["posted_date"], "2026-06-24")
        self.assertTrue(row["sponsored"])
        self.assertIn("healthy lifestyle", row["description"])
        self.assertEqual(row["job_url"],
                         "https://www.simplyhired.co.in/job/" + row["job_id"])

    def test_rich_row_with_detail_prefers_detail_fields(self):
        row = job_to_rich_row(self.LISTING_JOB, self.DETAIL)
        self.assertEqual(row["city"], "Panaji")
        self.assertEqual(row["state"], "GA")
        self.assertIn("preventive care", row["description"])
        self.assertNotIn("<p>", row["description"])
        self.assertEqual(row["qualifications"],
                         "Bachelor's degree|Patient care")

    def test_club_row_exports_inr_salary(self):
        rich = job_to_rich_row(self.LISTING_JOB, self.DETAIL)
        rich.pop("needs_review")
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["country_dial_code"], "+91")
        self.assertEqual(club["min_salary"], "18000")
        self.assertEqual(club["max_salary"], "25000")
        self.assertEqual(club["salary_period"], "per_month")
        self.assertEqual(club["salary_currency"], "INR")
        self.assertEqual(club["category"], "doctors")
        self.assertEqual(club["is_active"], "true")
        self.assertEqual(club["posted_at"], "2026-06-24")

    def test_club_row_undisclosed_salary_stays_empty(self):
        job = dict(self.LISTING_JOB, salaryInfo=None)
        rich = job_to_rich_row(job, None)
        rich.pop("needs_review")
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")


class TestCompanyType(unittest.TestCase):
    def test_defaults_hospital(self):
        self.assertEqual(classify_company_type("Multispeciality Hospital"),
                         "hospital")

    def test_pharma(self):
        self.assertEqual(classify_company_type("Sun Pharma Distributors"),
                         "pharma")
        self.assertEqual(classify_company_type("Metropolis Diagnostics"),
                         "pharma")


class TestStripHtml(unittest.TestCase):
    def test_tags_and_entities(self):
        self.assertEqual(
            strip_html("<p>Nurses &amp; Doctors</p><ul><li>BLS</li></ul>"),
            "Nurses & Doctors BLS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
