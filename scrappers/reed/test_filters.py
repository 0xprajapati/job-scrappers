#!/usr/bin/env python3
"""Unit tests for the reed scraper's parsers and filters.

Run with plain:  python test_filters.py
Worked examples come from real __NEXT_DATA__ payloads captured on 2026-07-12.
"""

import unittest
from datetime import date

import pandas as pd

from reed_scraper import (
    classify_category,
    classify_company_type,
    compute_cutoff,
    extract_page_props,
    iso_to_date,
    job_to_rich_row,
    listing_salary,
    map_job_type,
    parse_display_salary,
    rich_row_to_club_row,
    strip_html,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
)


class TestIsoToDate(unittest.TestCase):
    def test_display_date(self):
        self.assertEqual(iso_to_date("2026-07-12T11:12:05"), "2026-07-12")

    def test_garbage(self):
        self.assertEqual(iso_to_date(None), "")
        self.assertEqual(iso_to_date("recently"), "")


class TestParseDisplaySalary(unittest.TestCase):
    def test_annual_range(self):
        self.assertEqual(parse_display_salary("£41,000 - £42,000 per annum"),
                         ("£41,000 - £42,000 per annum", "41000", "42000",
                          "GBP", "per_annum"))

    def test_hourly(self):
        self.assertEqual(parse_display_salary("£18.85 - £23.85 per hour"),
                         ("£18.85 - £23.85 per hour", "19", "24",
                          "GBP", "per_hour"))

    def test_competitive_kept_raw_only(self):
        # real detail page: displaySalary "Competitive salary"
        self.assertEqual(parse_display_salary("Competitive salary"),
                         ("Competitive salary", "", "", "", ""))

    def test_missing(self):
        self.assertEqual(parse_display_salary(None),
                         ("Not Disclosed", "", "", "", ""))


class TestListingSalary(unittest.TestCase):
    def test_real_disclosed_annual(self):
        # real listing: School Nurse 41000-42000, type 5, currency 1
        job = {"salaryFrom": 41000, "salaryTo": 42000, "salaryType": 5,
               "salaryCurrencyId": 1, "salaryDescription": 1}
        self.assertEqual(listing_salary(job),
                         ("GBP 41000 - 42000 per annum", "41000", "42000",
                          "GBP", "per_annum"))

    def test_competitive_band_dropped(self):
        # real listing: type-64 "Competitive salary" rows still carry
        # search-band numbers (20000-70000) that must NOT be stored
        job = {"salaryFrom": 20000, "salaryTo": 70000, "salaryType": 5,
               "salaryCurrencyId": 1, "salaryDescription": 64}
        self.assertEqual(listing_salary(job),
                         ("Not Disclosed", "", "", "", ""))

    def test_hourly_type(self):
        job = {"salaryFrom": 38, "salaryTo": 45, "salaryType": 1,
               "salaryCurrencyId": 1, "salaryDescription": 1}
        self.assertEqual(listing_salary(job),
                         ("GBP 38 - 45 per hour", "38", "45", "GBP",
                          "per_hour"))

    def test_no_salary(self):
        self.assertEqual(listing_salary({}), ("Not Disclosed", "", "", "", ""))


class TestMapJobType(unittest.TestCase):
    def test_remote_and_hybrid(self):
        self.assertEqual(map_job_type("Remote", True, False), "remote")
        self.assertEqual(map_job_type("Hybrid", True, False), "hybrid")

    def test_on_site(self):
        self.assertEqual(map_job_type("On-Site", True, False), "full_time")
        self.assertEqual(map_job_type("On-Site", False, True), "part_time")

    def test_both_flags_mean_full_time(self):
        self.assertEqual(map_job_type("On-Site", True, True), "full_time")


class TestClassifyCategory(unittest.TestCase):
    def test_nurses(self):
        for title in ("School Nurse", "Clinical Nurse Advisor",
                      "Registered Midwife", "Ward Matron",
                      "Health Visitor", "RGN Nights"):
            self.assertEqual(classify_category(title), ("nurses", False), title)

    def test_doctors(self):
        for title in ("General Practitioner", "Consultant Psychiatrist",
                      "Speciality Doctor", "Cardiologist"):
            self.assertEqual(classify_category(title), ("doctors", False), title)

    def test_pharmacists(self):
        for title in ("Pharmacist", "Pharmacy Dispenser"):
            self.assertEqual(classify_category(title), ("pharmacists", False), title)

    def test_uk_care_roles_non_clinical(self):
        for title in ("Care Assistant", "Support Worker",
                      "Theatre Scrub Practitioner", "Occupational Therapist",
                      "Registered Home Manager", "Healthcare Assistant"):
            self.assertEqual(classify_category(title), ("non_clinical", False), title)

    def test_unknown_flagged_never_dropped(self):
        category, needs_review = classify_category("Wellbeing Champion")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_10_day_window(self):
        self.assertEqual(INITIAL_WINDOW_DAYS, 10)
        self.assertEqual(compute_cutoff(None, today=date(2026, 7, 12)),
                         "2026-07-02")

    def test_watermark_minus_grace(self):
        existing = pd.DataFrame({"posted_date": ["2026-07-08", "2026-07-11"]})
        self.assertEqual(WATERMARK_GRACE_DAYS, 2)
        self.assertEqual(compute_cutoff(existing), "2026-07-09")


class TestExtractPageProps(unittest.TestCase):
    def test_round_trip(self):
        html = ('<script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"pageProps":{"searchResults":{"count": 653}}}}'
                '</script>')
        self.assertEqual(extract_page_props(html),
                         {"searchResults": {"count": 653}})

    def test_bad_html(self):
        self.assertIsNone(extract_page_props("<html></html>"))


class TestRowBuilding(unittest.TestCase):
    LISTING_JOB = {  # trimmed real listing jobDetail (jobId 56889147)
        "jobId": 56889147,
        "jobTitle": "Theatre Scrub Practitioner",
        "jobDescriptionSnippet": "Job Advert Theatre Scrub Practitioner...",
        "displayDate": "2026-07-12T11:12:05",
        "expiryDate": "2026-08-23T23:55:00",
        "displayLocationName": "Banbury",
        "countyLocation": "Oxfordshire",
        "isFullTime": True,
        "isPartTime": False,
        "ouName": "Appcastenterprise",
        "salaryCurrencyId": 1,
        "salaryDescription": 64,
        "salaryFrom": 20000,
        "salaryTo": 70000,
        "salaryType": 5,
        "remoteWorkingOption": "On-Site",
        "taxonomyLevel1": "Practitioner",
        "taxonomyLevel2": "Theatre Practitioner",
        "isPromoted": False,
        "url": "/jobs/theatre-scrub-practitioner/56889147",
        "logoImage": "",
    }
    DETAIL = {  # trimmed real consolidatedJobDetails.jobDetails
        "id": "56889147",
        "title": "Theatre Scrub Practitioner",
        "description": "<p><b>Job Advert</b></p><p>Theatre Scrub Practitioner"
                       " - Orthopaedics and/or Ophthalmology. Full Time.</p>",
        "displayDate": "2026-07-12T11:12:05",
        "expiryDate": "2026-08-23T23:55:00",
        "jobContractType": {"id": 1, "name": "Permanent"},
        "jobEmploymentHours": {"isFullTime": True, "isPartTime": False},
        "jobLocation": {"locationName": "Banbury",
                        "regionName": "South East England"},
        "jobSalary": {"displaySalary": "Competitive salary",
                      "salaryDescriptionType": 64, "currencyId": 1,
                      "from": 20000, "to": 70000},
        "jobSector": {"id": 1622, "name": "ODAs/ODPs/Theatre Nurses",
                      "parentId": 36, "parentName": "Health & Medicine"},
        "isAgency": "True", "isEmployer": "False", "isReed": "False",
    }

    def test_rich_row_listing_only_drops_competitive_band(self):
        row = job_to_rich_row(self.LISTING_JOB, None)
        self.assertEqual(row["job_id"], "56889147")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["salary_min"], "")
        self.assertEqual(row["city"], "Banbury")
        self.assertEqual(row["county"], "Oxfordshire")
        self.assertEqual(row["posted_date"], "2026-07-12")
        self.assertEqual(row["expires_date"], "2026-08-23")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["job_url"],
                         "https://www.reed.co.uk/jobs/theatre-scrub-practitioner/56889147")

    def test_rich_row_with_detail(self):
        row = job_to_rich_row(self.LISTING_JOB, self.DETAIL)
        self.assertEqual(row["salary_raw"], "Competitive salary")
        self.assertEqual(row["region"], "South East England")
        self.assertEqual(row["contract_type"], "Permanent")
        self.assertEqual(row["company_kind"], "agency")
        self.assertEqual(row["sector"], "ODAs/ODPs/Theatre Nurses")
        self.assertIn("Ophthalmology", row["description"])
        self.assertNotIn("<p>", row["description"])

    def test_club_row_uk_fields_and_expiry(self):
        rich = job_to_rich_row(self.LISTING_JOB, self.DETAIL)
        rich.pop("needs_review")
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["country_name"], "United Kingdom")
        self.assertEqual(club["country_code"], "GB")
        self.assertEqual(club["country_dial_code"], "+44")
        self.assertEqual(club["expires_at"], "2026-08-23")
        self.assertEqual(club["posted_at"], "2026-07-12")
        # GBP can't be represented by the club enum -> salary empty
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")
        self.assertEqual(club["is_active"], "true")

    def test_club_salary_stays_empty_even_when_disclosed_gbp(self):
        job = dict(self.LISTING_JOB, salaryDescription=1,
                   salaryFrom=41000, salaryTo=42000)
        rich = job_to_rich_row(job, None)
        self.assertEqual(rich["salary_min"], "41000")  # rich keeps it
        rich.pop("needs_review")
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["min_salary"], "")       # club can't hold GBP


class TestCompanyType(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(classify_company_type("Nurse Seekers"), "hospital")
        self.assertEqual(classify_company_type("Randox Laboratories"), "pharma")


class TestStripHtml(unittest.TestCase):
    def test_tags_and_entities(self):
        self.assertEqual(strip_html("<p>Scrub &amp; Recovery</p>"),
                         "Scrub & Recovery")


if __name__ == "__main__":
    unittest.main(verbosity=2)
