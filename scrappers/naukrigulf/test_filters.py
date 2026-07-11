#!/usr/bin/env python3
"""Unit tests for the naukrigulf scraper's parsers and filters.

Run with plain:  python test_filters.py
All worked examples come from real API responses captured on 2026-07-11.
"""

import unittest
from datetime import date

import pandas as pd

from naukrigulf_scraper import (
    classify_category,
    classify_company_type,
    compute_cutoff,
    epoch_to_date,
    job_to_rich_row,
    map_job_type,
    parse_location,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
)


class TestParseLocation(unittest.TestCase):
    def test_city_country_with_abbreviation(self):
        self.assertEqual(parse_location("Dubai - United Arab Emirates (UAE)"),
                         ("Dubai", "United Arab Emirates"))

    def test_country_only_listing(self):
        # naukrigulf repeats the country when no city is given
        self.assertEqual(parse_location("Qatar - Qatar"), ("Qatar", "Qatar"))

    def test_city_and_plain_country(self):
        self.assertEqual(parse_location("Riyadh - Saudi Arabia"),
                         ("Riyadh", "Saudi Arabia"))

    def test_empty(self):
        self.assertEqual(parse_location(""), ("", ""))
        self.assertEqual(parse_location(None), ("", ""))


class TestEpochToDate(unittest.TestCase):
    def test_real_listing_epoch(self):
        # from job 010726000671 (Assistant Nurse, posted 1 Jul 2026)
        self.assertEqual(epoch_to_date("1782878400"), "2026-07-01")

    def test_int_epoch(self):
        self.assertEqual(epoch_to_date(1783569600), "2026-07-09")

    def test_garbage(self):
        self.assertEqual(epoch_to_date(None), "")
        self.assertEqual(epoch_to_date("soon"), "")


class TestParseSalary(unittest.TestCase):
    def test_aed_range_from_detail_api(self):
        # from job 010726000671: AED 3,000 - 3,500, period not stated
        comp = {"IsCtcHidden": "false", "jobMinCurrency": "AED 3,000",
                "jobMaxCurrency": "3,500", "salaryTimeBrand": None}
        self.assertEqual(parse_salary(comp, "AED"),
                         ("AED 3,000 - 3,500", "3000", "3500", "AED", ""))

    def test_hidden_salary(self):
        comp = {"IsCtcHidden": "true", "jobMinCurrency": "AED 1,000"}
        self.assertEqual(parse_salary(comp, "AED"),
                         ("Not Disclosed", "", "", "", ""))

    def test_missing_compensation(self):
        self.assertEqual(parse_salary(None), ("Not Disclosed", "", "", "", ""))
        self.assertEqual(parse_salary({}), ("Not Disclosed", "", "", "", ""))

    def test_monthly_period_and_currency_from_string(self):
        comp = {"IsCtcHidden": "false", "jobMinCurrency": "QAR 8,000",
                "jobMaxCurrency": "12,000", "salaryTimeBrand": "Monthly"}
        self.assertEqual(parse_salary(comp, ""),
                         ("QAR 8,000 - 12,000", "8000", "12000", "QAR", "per_month"))

    def test_rupee_symbol_maps_to_inr(self):
        # seen live: currLabel "₹" with jobMinCurrency "₹800,000"
        comp = {"IsCtcHidden": "false", "jobMinCurrency": "₹800,000",
                "jobMaxCurrency": "₹1,100,000", "salaryTimeBrand": "Annual"}
        self.assertEqual(parse_salary(comp, "₹"),
                         ("₹800,000 - ₹1,100,000", "800000", "1100000",
                          "INR", "per_annum"))

    def test_reversed_range_is_normalized(self):
        comp = {"IsCtcHidden": "false", "jobMinCurrency": "USD 900",
                "jobMaxCurrency": "500", "salaryTimeBrand": "Annual"}
        self.assertEqual(parse_salary(comp, "USD"),
                         ("USD 900 - 500", "500", "900", "USD", "per_annum"))


class TestClassifyCategory(unittest.TestCase):
    def test_nurses(self):
        for title in ("Assistant Nurse – Home Healthcare", "Registered Midwife",
                      "Staff Nurse ICU", "Nursing Supervisor"):
            self.assertEqual(classify_category(title), ("nurses", False), title)

    def test_doctors(self):
        for title in ("General Physician", "Consultant Cardiologist",
                      "Medical Officer", "Specialist Registrar",
                      "Anaesthetist", "Paediatrician",
                      # Gulf-style specialty titles seen live on naukrigulf
                      "Specialist - Cardiology", "Consultant - Family Medicine",
                      "Orthopaedic"):
            self.assertEqual(classify_category(title), ("doctors", False), title)

    def test_allied_health_is_non_clinical_not_doctor(self):
        # "-ology" specialty rule must not swallow allied-health roles
        for title in ("Audiology", "Physiotherapist", "Speech Therapist",
                      "Radiographer"):
            self.assertEqual(classify_category(title), ("non_clinical", False), title)

    def test_pharmacists(self):
        for title in ("Pharmacist", "Pharmacy Manager", "Pharm D Intern"):
            self.assertEqual(classify_category(title), ("pharmacists", False), title)

    def test_known_non_clinical_not_flagged(self):
        for title in ("Accountant - Healthcare", "Healthcare Assistant",
                      "Healthcare Liaison Officer (HLO) - UAEN",
                      "Lab Technician", "HR Executive"):
            self.assertEqual(classify_category(title), ("non_clinical", False), title)

    def test_unknown_title_kept_but_flagged(self):
        category, needs_review = classify_category("Bariatric Program Lead")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)  # never dropped, only flagged


class TestClassifyCompanyType(unittest.TestCase):
    def test_hospital_default(self):
        self.assertEqual(classify_company_type("NMC healthcare LLC"), "hospital")

    def test_pharma_by_name(self):
        self.assertEqual(classify_company_type("Julphar Pharmaceuticals"), "pharma")
        self.assertEqual(classify_company_type("Gulf Diagnostic Center"), "pharma")

    def test_pharma_by_industry(self):
        self.assertEqual(
            classify_company_type("Some Trading FZE", "Pharma / Biotech"), "pharma")


class TestMapJobType(unittest.TestCase):
    def test_detail_api_values(self):
        self.assertEqual(map_job_type("Full Time", "On Site"), "full_time")
        self.assertEqual(map_job_type("Full Time", "Remote"), "remote")
        self.assertEqual(map_job_type("Full Time", "Hybrid"), "hybrid")
        self.assertEqual(map_job_type("Part Time", "On Site"), "part_time")

    def test_without_enrich_defaults_full_time(self):
        self.assertEqual(map_job_type(None, None, "Staff Nurse"), "full_time")
        self.assertEqual(map_job_type(None, None, "Nurse (Part Time)"), "part_time")


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        today = date(2026, 7, 11)
        expected = "2026-06-26"  # 11 Jul - 15 days
        self.assertEqual(INITIAL_WINDOW_DAYS, 15)
        self.assertEqual(compute_cutoff(None, today=today), expected)

    def test_later_runs_use_watermark_minus_grace(self):
        existing = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-09"]})
        self.assertEqual(WATERMARK_GRACE_DAYS, 2)
        self.assertEqual(compute_cutoff(existing), "2026-07-07")

    def test_unparseable_dates_fall_back_to_initial_window(self):
        existing = pd.DataFrame({"posted_date": ["", "unknown"]})
        today = date(2026, 7, 11)
        self.assertEqual(compute_cutoff(existing, today=today), "2026-06-26")


class TestRowBuilding(unittest.TestCase):
    LISTING_JOB = {  # trimmed real listing payload (job 010726000671)
        "Designation": "Assistant Nurse – Home Healthcare",
        "Location": "Dubai - United Arab Emirates (UAE)",
        "jobInfo": "Job Summary We are looking for a compassionate Assistant Nurse...",
        "Experience": {"Min": "1", "Max": "2"},
        "Company": {"Name": "NADZ HEALTHCARE FZCO ", "Id": "278358"},
        "JobId": "010726000671",
        "JdURL": "https://www.naukrigulf.com/assistant-nurse-home-healthcare-jobs-"
                 "in-dubai-uae-in-nadz-healthcare-fzco-1-to-2-years-n-cd-278358-"
                 "jid-010726000671",
        "LatestPostedDate": "1782878400",
        "Vacancies": "1",
        "LogoUrl": None,
    }
    DETAIL_JOB = {  # trimmed real detail payload for the same job
        "Description": "<p>Job Summary</p><p>We are looking for a compassionate "
                       "and dedicated Assistant Nurse.</p>",
        "IndustryType": "Medical / Healthcare / Diagnostics / Medical Devices",
        "FunctionalArea": "Doctor / Nurse / Paramedics / Hospital Technicians",
        "Compensation": {"IsCtcHidden": "false", "jobMinCurrency": "AED 3,000",
                         "jobMaxCurrency": "3,500", "salaryTimeBrand": None},
        "Other": {"currLabel": "AED"},
        "Company": {"Profile": "<p>Home healthcare provider in Dubai.</p>"},
        "DesiredCandidate": {"Education": "Bachelor of Science(Nursing)"},
        "employmentType": "Full Time",
        "locationType": "On Site",
    }

    def test_rich_row_from_listing_and_detail(self):
        row = job_to_rich_row(self.LISTING_JOB, self.DETAIL_JOB)
        self.assertEqual(row["job_id"], "010726000671")
        self.assertEqual(row["city"], "Dubai")
        self.assertEqual(row["country"], "United Arab Emirates")
        self.assertEqual(row["country_code"], "AE")
        self.assertEqual(row["country_dial_code"], "+971")
        self.assertEqual(row["company"], "NADZ HEALTHCARE FZCO")
        self.assertEqual(row["category"], "nurses")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["salary_raw"], "AED 3,000 - 3,500")
        self.assertEqual(row["salary_min"], "3000")
        self.assertEqual(row["salary_currency"], "AED")
        self.assertEqual(row["posted_date"], "2026-07-01")
        self.assertEqual(row["experience_min_years"], "1")
        self.assertEqual(row["experience_max_years"], "2")
        self.assertIn("compassionate", row["description"])
        self.assertFalse(row["needs_review"])

    def test_club_row_omits_non_usd_inr_salary(self):
        rich = job_to_rich_row(self.LISTING_JOB, self.DETAIL_JOB)
        rich.pop("needs_review")
        club = rich_row_to_club_row(rich)
        # AED cannot be represented by the club enum -> salary left empty
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["max_salary"], "")
        self.assertEqual(club["salary_period"], "")
        self.assertEqual(club["salary_currency"], "")
        # required fields present and enum-valid
        self.assertEqual(club["country_name"], "United Arab Emirates")
        self.assertEqual(club["country_code"], "AE")
        self.assertEqual(club["posted_at"], "2026-07-01")
        self.assertEqual(club["category"], "nurses")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["is_active"], "true")

    def test_club_row_exports_usd_salary_with_period(self):
        rich = job_to_rich_row(self.LISTING_JOB, dict(
            self.DETAIL_JOB,
            Compensation={"IsCtcHidden": "false", "jobMinCurrency": "USD 2,000",
                          "jobMaxCurrency": "3,000", "salaryTimeBrand": "Monthly"},
            Other={"currLabel": "USD"}))
        rich.pop("needs_review")
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["min_salary"], "2000")
        self.assertEqual(club["max_salary"], "3000")
        self.assertEqual(club["salary_period"], "per_month")
        self.assertEqual(club["salary_currency"], "USD")

    def test_listing_only_row_uses_jobinfo_and_defaults(self):
        row = job_to_rich_row(self.LISTING_JOB, None)
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["salary_min"], "")
        self.assertEqual(row["job_type"], "full_time")
        self.assertIn("compassionate", row["description"])


class TestStripHtml(unittest.TestCase):
    def test_tags_and_entities(self):
        self.assertEqual(
            strip_html("<p>Nurses &amp; Midwives</p><ul><li>DHA License</li></ul>"),
            "Nurses & Midwives DHA License")

    def test_empty(self):
        self.assertEqual(strip_html(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
