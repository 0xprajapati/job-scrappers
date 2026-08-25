#!/usr/bin/env python3
"""Unit tests for the naukrigulf scraper's parsers and filters.

Run with plain:  python test_filters.py
All worked examples come from real API responses captured on 2026-07-11.
"""

import unittest
from datetime import date

import pandas as pd

from naukrigulf_scraper import (
    classification_skills_signal,
    classify_company_type,
    compute_cutoff,
    epoch_to_date,
    job_to_rich_row,
    map_job_type,
    parse_location,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    CLUB_COLUMNS,
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


class TestSkillsSignal(unittest.TestCase):
    """IndustryType/FunctionalArea feed classify_job as skills only."""

    def test_joined(self):
        self.assertEqual(
            classification_skills_signal("Pharma / Biotech",
                                         "Regulatory Affairs"),
            "Pharma / Biotech, Regulatory Affairs")

    def test_empty_without_enrich(self):
        self.assertEqual(classification_skills_signal(None, None), "")
        self.assertEqual(classification_skills_signal("", "  "), "")


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
    """Real payload shape (job 010726000671), retitled to an in-scope role —
    the shared classifier now drops bedside roles this board is full of."""

    LISTING_JOB = {
        "Designation": "Regulatory Affairs Specialist",
        "Location": "Dubai - United Arab Emirates (UAE)",
        "jobInfo": "Prepare and submit regulatory dossiers...",
        "Experience": {"Min": "1", "Max": "2"},
        "Company": {"Name": "Julphar Pharmaceuticals ", "Id": "278358"},
        "JobId": "010726000671",
        "JdURL": "https://www.naukrigulf.com/regulatory-affairs-specialist-jobs-"
                 "in-dubai-uae-cd-278358-jid-010726000671",
        "LatestPostedDate": "1782878400",
        "Vacancies": "1",
        "LogoUrl": None,
    }
    DETAIL_JOB = {
        "Description": "<p>Prepare regulatory submissions and maintain product "
                       "registrations with SFDA and MOH. Requires a B.Pharm.</p>",
        "IndustryType": "Pharma / Biotech / Clinical Research",
        "FunctionalArea": "Regulatory Affairs",
        "Compensation": {"IsCtcHidden": "false", "jobMinCurrency": "AED 3,000",
                         "jobMaxCurrency": "3,500", "salaryTimeBrand": None},
        "Other": {"currLabel": "AED"},
        "Company": {"Profile": "<p>Pharma manufacturer in the Gulf.</p>"},
        "DesiredCandidate": {"Education": "Bachelor of Pharmacy"},
        "employmentType": "Full Time",
        "locationType": "On Site",
    }

    # ---- shared-classifier wiring (the engine itself is tested in _shared) --

    def test_in_scope_role_gets_two_level_category(self):
        row = job_to_rich_row(self.LISTING_JOB, self.DETAIL_JOB)
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Regulatory Affairs")
        self.assertEqual(row["role_family"], "Regulatory Affairs")
        self.assertFalse(row["needs_review"])

    def test_out_of_scope_title_returns_none(self):
        # the original real payload: bedside nursing is out of taxonomy scope
        nurse = dict(self.LISTING_JOB,
                     Designation="Assistant Nurse – Home Healthcare",
                     jobInfo="We are looking for a compassionate Assistant Nurse")
        self.assertIsNone(job_to_rich_row(nurse, None))

    # ---- field mapping --------------------------------------------------

    def test_rich_row_from_listing_and_detail(self):
        row = job_to_rich_row(self.LISTING_JOB, self.DETAIL_JOB)
        self.assertEqual(row["job_id"], "010726000671")
        self.assertEqual(row["city"], "Dubai")
        self.assertEqual(row["country"], "United Arab Emirates")
        self.assertEqual(row["country_code"], "AE")
        self.assertEqual(row["country_dial_code"], "+971")
        self.assertEqual(row["company"], "Julphar Pharmaceuticals")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["salary_raw"], "AED 3,000 - 3,500")
        self.assertEqual(row["salary_min"], "3000")
        self.assertEqual(row["salary_currency"], "AED")
        self.assertEqual(row["posted_date"], "2026-07-01")
        self.assertEqual(row["experience_min_years"], "1")
        self.assertEqual(row["experience_max_years"], "2")
        # raw source facets stay as source columns only
        self.assertEqual(row["functional_area"], "Regulatory Affairs")
        self.assertIn("regulatory submissions", row["description"])

    def test_club_row_matches_contract_and_omits_aed_salary(self):
        rich = job_to_rich_row(self.LISTING_JOB, self.DETAIL_JOB)
        club = rich_row_to_club_row(rich)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertEqual(len(CLUB_COLUMNS), 22)
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)
        # AED cannot be represented by the club enum -> salary left empty
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["max_salary"], "")
        self.assertEqual(club["salary_period"], "")
        self.assertEqual(club["salary_currency"], "")
        # required fields present and enum-valid
        self.assertEqual(club["country_name"], "United Arab Emirates")
        self.assertEqual(club["country_code"], "AE")
        self.assertEqual(club["posted_at"], "2026-07-01")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Regulatory Affairs")
        self.assertEqual(club["job_type"], "full_time")
        # structured Education field wins over description extraction
        self.assertEqual(club["qualification"], "Bachelor of Pharmacy")

    def test_club_row_exports_usd_salary_with_period(self):
        rich = job_to_rich_row(self.LISTING_JOB, dict(
            self.DETAIL_JOB,
            Compensation={"IsCtcHidden": "false", "jobMinCurrency": "USD 2,000",
                          "jobMaxCurrency": "3,000", "salaryTimeBrand": "Monthly"},
            Other={"currLabel": "USD"}))
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
        self.assertIn("regulatory dossiers", row["description"])


class TestStripHtml(unittest.TestCase):
    def test_tags_and_entities(self):
        self.assertEqual(
            strip_html("<p>Nurses &amp; Midwives</p><ul><li>DHA License</li></ul>"),
            "Nurses & Midwives DHA License")

    def test_empty(self):
        self.assertEqual(strip_html(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
