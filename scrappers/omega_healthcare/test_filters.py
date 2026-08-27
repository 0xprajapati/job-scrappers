#!/usr/bin/env python3
"""Unit tests for the Omega Healthcare scraper's parsers, cutoff and export
mapping.

Run with plain:  python test_filters.py

Worked examples are trimmed real payloads from the Oracle HCM Candidate
Experience API on fa-equm-saasfaprod1.fa.ocs.oraclecloud.com
(27 Aug 2026): the US Coder Physician (20031, site CX_1001, with skills,
pay and experience flex fields) and the description-less offshore Clinical
Executive (19771, site CX_2001, whose only signal is its flex fields).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

import scraper
from scraper import (
    CLUB_COLUMNS,
    COUNTRY_BY_CODE,
    FIRST_RUN_DETAIL_CAP,
    INITIAL_WINDOW_DAYS,
    LIST_PAGE_LIMIT,
    RICH_COLUMNS,
    SITE_NUMBERS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_description,
    build_row,
    build_skills,
    compute_cutoff,
    parse_experience_years,
    parse_flex_fields,
    parse_iso_date,
    parse_location,
    parse_pay,
    parse_requisition_detail,
    parse_requisition_list,
    rich_row_to_club_row,
)

LIST_PAYLOAD = {
    "items": [{
        "TotalJobsCount": 834,
        "requisitionList": [
            {"Id": "19766", "Title": "Clinical Executive",
             "PostedDate": "2026-08-27", "PostingEndDate": None,
             "PrimaryLocation": "Manila Rizal, Philippines",
             "PrimaryLocationCountry": "PH"},
            {"Id": "19289", "Title": "Executive - AR",
             "PostedDate": "2026-08-26", "PostingEndDate": None,
             "PrimaryLocation": "Manila Rizal, Philippines",
             "PrimaryLocationCountry": "PH"},
        ],
    }]
}

CODER_DESC = (
    "Scope: Full time multi-specialty profee coder with 2+ years recent "
    "experience coding general and trauma surgeries to include office "
    "visits and procedures as well as hospital visits and OR procedures. "
)

US_DETAIL_PAYLOAD = {
    "items": [{
        "Id": "20031",
        "Title": "Coder Physician",
        "PrimaryLocation": "Boca Raton, FL, United States",
        "PrimaryLocationCountry": "US",
        # Verified: this tenant's details carry NO dates at all.
        "ExternalPostedStartDate": None,
        "ExternalPostedEndDate": None,
        "ExternalDescriptionStr": "<div><p>" + CODER_DESC + "</p></div>",
        "ExternalQualificationsStr": "<p>CPC certification required.</p>",
        "Category": "Coding",
        "skills": [
            {"SectionName": "Skill", "Skill": "CPT Coding"},
            {"SectionName": "Skill", "Skill": "ICD10 Diagnostic Coding"},
            {"SectionName": "Skill", "Skill": "Medical Coding"},
        ],
        "requisitionFlexFields": [
            {"Prompt": "Region", "Value": "Field Employees - Hourly X4E"},
            {"Prompt": "Full-Time/Part-Time", "Value": "Full-Time"},
            {"Prompt": "Required Years of Experience", "Value": "2"},
            {"Prompt": "Minimum Pay", "Value": "21"},
            {"Prompt": "Maximum Pay", "Value": "28.25"},
        ],
    }]
}

OFFSHORE_DETAIL_PAYLOAD = {
    "items": [{
        "Id": "19771",
        "Title": "Clinical Executive",
        "PrimaryLocation": "Manila Rizal, Philippines",
        "PrimaryLocationCountry": "PH",
        "ExternalPostedStartDate": None,
        "ExternalPostedEndDate": None,
        # Verified: CX_2001 rows are usually description-less.
        "ExternalDescriptionStr": "",
        "Category": None,
        "skills": [],
        "requisitionFlexFields": [
            {"Prompt": "Speciality", "Value": "HCC"},
            {"Prompt": "Service Line", "Value": "Coding"},
            {"Prompt": "RFH For", "Value": "ABF"},
            {"Prompt": "Resource Type", "Value": "External"},
            {"Prompt": "Required By Date", "Value": "2026-08-31"},
        ],
    }]
}


class ListParsingTests(unittest.TestCase):
    def test_requisition_list_is_unwrapped_from_the_items_envelope(self):
        reqs, total = parse_requisition_list(LIST_PAYLOAD)
        self.assertEqual(total, 834)
        self.assertEqual([r["Id"] for r in reqs], ["19766", "19289"])

    def test_empty_payload_is_not_an_error(self):
        self.assertEqual(parse_requisition_list({}), ([], 0))
        self.assertEqual(parse_requisition_list({"items": []}), ([], 0))

    def test_detail_is_unwrapped_from_the_items_envelope(self):
        self.assertEqual(parse_requisition_detail(US_DETAIL_PAYLOAD)["Id"],
                         "20031")
        self.assertEqual(parse_requisition_detail({"items": []}), {})

    def test_flex_fields_become_a_prompt_keyed_dict(self):
        flex = parse_flex_fields(parse_requisition_detail(US_DETAIL_PAYLOAD))
        self.assertEqual(flex["Minimum Pay"], "21")
        self.assertEqual(flex["Required Years of Experience"], "2")


class SkillsSignalTests(unittest.TestCase):
    def test_us_row_combines_oracle_skills_and_category(self):
        detail = parse_requisition_detail(US_DETAIL_PAYLOAD)
        skills = build_skills(detail, parse_flex_fields(detail))
        self.assertIn("CPT Coding", skills)
        self.assertIn("ICD10 Diagnostic Coding", skills)
        self.assertIn("Coding", skills)

    def test_offshore_row_gets_its_signal_from_the_flex_fields(self):
        detail = parse_requisition_detail(OFFSHORE_DETAIL_PAYLOAD)
        skills = build_skills(detail, parse_flex_fields(detail))
        self.assertIn("HCC", skills)
        self.assertIn("Coding", skills)

    def test_operational_flex_prompts_are_not_skills(self):
        detail = parse_requisition_detail(OFFSHORE_DETAIL_PAYLOAD)
        skills = build_skills(detail, parse_flex_fields(detail))
        self.assertNotIn("ABF", skills)          # RFH For
        self.assertNotIn("External", skills)     # Resource Type
        self.assertNotIn("2026-08-31", skills)   # Required By Date

    def test_default_placeholders_are_dropped(self):
        skills = build_skills({}, {"Speciality": "Default",
                                   "Service Line": "Default"})
        self.assertEqual(skills, "")

    def test_duplicate_values_appear_once(self):
        skills = build_skills({"skills": [{"Skill": "Medical Coding"}],
                               "Category": "Coding"},
                              {"Service Line": "Coding"})
        self.assertEqual(skills.count("Coding"),
                         2)  # "Medical Coding" + one "Coding"


class LocationTests(unittest.TestCase):
    def test_us_city_state_country(self):
        self.assertEqual(parse_location("Boca Raton, FL, United States", "US"),
                         ("Boca Raton", "FL", "United States"))

    def test_philippines_city_country(self):
        self.assertEqual(parse_location("Manila Rizal, Philippines", "PH"),
                         ("Manila Rizal", "", "Philippines"))

    def test_state_only_us_location(self):
        self.assertEqual(parse_location("FL, United States", "US"),
                         ("", "FL", "United States"))

    def test_india_city_country(self):
        self.assertEqual(parse_location("Chennai, India", "IN"),
                         ("Chennai", "", "India"))

    def test_country_only_location(self):
        self.assertEqual(parse_location("United States", "US"),
                         ("", "", "United States"))

    def test_unknown_code_is_never_guessed(self):
        self.assertEqual(parse_location("Somewhere, Atlantis", "ZZ"),
                         ("Somewhere, Atlantis", "", ""))


class DateTests(unittest.TestCase):
    def test_plain_listing_date_passes_through(self):
        self.assertEqual(parse_iso_date("2026-08-27"), "2026-08-27")

    def test_oracle_timestamp_reduces_to_a_date(self):
        self.assertEqual(parse_iso_date("2026-08-27T15:39:48+00:00"),
                         "2026-08-27")

    def test_missing_date_is_blank(self):
        self.assertEqual(parse_iso_date(None), "")


class ExperienceTests(unittest.TestCase):
    def test_the_structured_flex_field_wins(self):
        self.assertEqual(
            parse_experience_years({"Required Years of Experience": "2"},
                                   "at least 15 years of experience"),
            "2")

    def test_prose_fallback_when_flex_is_silent(self):
        self.assertEqual(
            parse_experience_years({}, "2+ years recent experience coding"),
            "2")

    def test_nothing_stated_stays_blank(self):
        self.assertEqual(parse_experience_years({}, "A great opportunity."), "")


class PayTests(unittest.TestCase):
    def test_flex_pay_numbers_are_captured_verbatim(self):
        self.assertEqual(parse_pay({"Minimum Pay": "21",
                                    "Maximum Pay": "28.25"}),
                         ("21", "28.25"))

    def test_missing_pay_stays_blank(self):
        self.assertEqual(parse_pay({}), ("", ""))

    def test_non_numeric_junk_is_not_captured(self):
        self.assertEqual(parse_pay({"Minimum Pay": "Competitive"}), ("", ""))


class CutoffTests(unittest.TestCase):
    def test_first_run_uses_the_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_the_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-10", "2026-08-20"]})
        expected = (date(2026, 8, 20) -
                    timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_since_overrides_everything(self):
        df = pd.DataFrame({"posted_date": ["2026-08-20"]})
        self.assertEqual(compute_cutoff(df, since="2026-01-01"), "2026-01-01")

    def test_the_first_run_detail_cap_matches_the_documented_value(self):
        self.assertEqual(FIRST_RUN_DETAIL_CAP, 150)

    def test_the_listing_page_size_matches_oracles_hard_cap(self):
        self.assertEqual(LIST_PAGE_LIMIT, 200)


class BuildRowTests(unittest.TestCase):
    def setUp(self):
        self.us_row = build_row(
            "20031", "CX_1001",
            parse_requisition_detail(US_DETAIL_PAYLOAD), "2026-08-26")
        self.off_row = build_row(
            "19771", "CX_2001",
            parse_requisition_detail(OFFSHORE_DETAIL_PAYLOAD), "2026-08-27")

    def test_row_has_exactly_the_fleet_columns(self):
        self.assertEqual(sorted(self.us_row), sorted(RICH_COLUMNS))

    def test_posted_date_comes_from_the_listing_details_have_none(self):
        self.assertEqual(self.us_row["posted_date"], "2026-08-26")
        self.assertEqual(self.off_row["posted_date"], "2026-08-27")

    def test_a_dateless_detail_and_blank_listing_date_stay_blank(self):
        row = build_row("19771", "CX_2001",
                        parse_requisition_detail(OFFSHORE_DETAIL_PAYLOAD), "")
        self.assertEqual(row["posted_date"], "")

    def test_pay_is_captured_raw(self):
        self.assertEqual(self.us_row["pay_min_raw"], "21")
        self.assertEqual(self.us_row["pay_max_raw"], "28.25")
        self.assertEqual(self.off_row["pay_min_raw"], "")

    def test_experience_comes_from_the_flex_field(self):
        self.assertEqual(self.us_row["experience_min_years"], "2")

    def test_job_url_carries_the_requisitions_own_site(self):
        self.assertIn("/sites/CX_1001/job/20031", self.us_row["job_url"])
        self.assertIn("/sites/CX_2001/job/19771", self.off_row["job_url"])

    def test_offshore_description_is_empty_but_skills_signal_is_not(self):
        self.assertEqual(self.off_row["description"], "")
        self.assertIn("Coding", self.off_row["sectors"])

    def test_description_concatenates_body_and_qualifications(self):
        self.assertIn("profee coder", self.us_row["description"])
        self.assertIn("CPC certification", self.us_row["description"])


class ClassificationTests(unittest.TestCase):
    def _classify(self, payload, posted="2026-08-27"):
        row = build_row(payload["items"][0]["Id"], "CX_x",
                        parse_requisition_detail(payload), posted)
        return apply_classification(row), row

    def test_the_us_coder_is_in_scope_medical_coding(self):
        in_scope, row = self._classify(US_DETAIL_PAYLOAD)
        self.assertTrue(in_scope)
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")

    def test_an_opaque_bpo_voice_grade_is_dropped(self):
        row = {"title": "Executive - AR",
               "sectors": "Hospital Billing; Coverage & Authorization; Voice/AR",
               "description": ""}
        self.assertFalse(apply_classification(row))

    def test_dropped_rows_keep_their_veto_trace_column(self):
        row = {"title": "Executive - AR",
               "sectors": "Hospital Billing; Coverage & Authorization; Voice/AR",
               "description": ""}
        apply_classification(row)
        self.assertIn("vetoed_by", row)


class ClubExportTests(unittest.TestCase):
    def _club(self, **over):
        row = {"country": "United States", "city": "Boca Raton", "state": "FL",
               "company": "Omega Healthcare", "title": "Coder Physician",
               "description": "CPC certification required.",
               "category": "Non Clinical", "sub_category": "Medical Coding",
               "role_family": "Medical Coding",
               "job_url": "https://x/job/1", "posted_date": "2026-08-26",
               "experience_min_years": "2", "work_schedule": "Full-Time",
               "pay_min_raw": "21", "pay_max_raw": "28.25"}
        row.update(over)
        return rich_row_to_club_row(row)

    def test_club_row_has_exactly_the_club_columns(self):
        self.assertEqual(sorted(self._club()), sorted(CLUB_COLUMNS))

    def test_country_code_and_dial_code_come_back_from_the_country_name(self):
        club = self._club()
        self.assertEqual(club["country_code"], "US")
        self.assertEqual(club["country_dial_code"], "+1")

    def test_philippines_and_india_map_too(self):
        self.assertEqual(self._club(country="Philippines")["country_code"], "PH")
        self.assertEqual(self._club(country="India")["country_dial_code"], "+91")

    def test_part_time_flex_drives_the_job_type_enum(self):
        self.assertEqual(self._club(work_schedule="Part-Time")["job_type"],
                         "part_time")
        self.assertEqual(self._club()["job_type"], "full_time")

    def test_bare_pay_numbers_do_not_leak_into_the_club_salary_columns(self):
        club = self._club()
        for col in ("min_salary", "max_salary", "salary_period",
                    "salary_currency"):
            self.assertEqual(club[col], "")

    def test_qualification_is_lifted_from_the_description(self):
        self.assertIn("CPC", self._club()["qualification"])


class ComplianceTests(unittest.TestCase):
    def test_every_request_goes_to_the_oracle_candidate_experience_host(self):
        for url in (scraper.LIST_URL, scraper.DETAIL_URL, scraper.ROBOTS_URL,
                    scraper.JOB_URL_TEMPLATE):
            self.assertTrue(url.startswith(scraper.CE_HOST))

    def test_both_published_sites_are_crawled(self):
        self.assertEqual(SITE_NUMBERS, ("CX_1001", "CX_2001"))

    def test_country_table_is_iso_alpha_2_keyed(self):
        for code in COUNTRY_BY_CODE:
            self.assertRegex(code, r"^[A-Z]{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
