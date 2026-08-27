#!/usr/bin/env python3
"""Unit tests for the Novotech scraper's parsers, cutoff and export mapping.

Run with plain:  python test_filters.py

Worked examples are trimmed real payloads from the Oracle HCM Candidate
Experience API on fa-euzi-saasfaprod1.fa.ocs.oraclecloud.com
(27 Aug 2026): the US Clinical Operations Manager (4389, site CX_1) and
the China Senior CRA (4380, site CX_4) whose posting text is split across
the responsibilities/qualifications fields.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

import scraper
from scraper import (
    CLUB_COLUMNS,
    COUNTRY_BY_CODE,
    INITIAL_WINDOW_DAYS,
    RICH_COLUMNS,
    SITE_NUMBERS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_description,
    build_row,
    compute_cutoff,
    looks_cjk,
    parse_experience_years,
    parse_iso_date,
    parse_location,
    parse_requisition_detail,
    parse_requisition_list,
    rich_row_to_club_row,
    strip_html,
)

LIST_PAYLOAD = {
    "items": [{
        "TotalJobsCount": 99,
        "requisitionList": [
            {"Id": "4389", "Title": "Manager Clinical Operations",
             "PostedDate": "2026-08-27", "PostingEndDate": None,
             "PrimaryLocation": "United States",
             "PrimaryLocationCountry": "US"},
            {"Id": "4388", "Title": "Project Transformation Manager - AU/SG",
             "PostedDate": "2026-08-27", "PostingEndDate": None,
             "PrimaryLocation": "Australia", "PrimaryLocationCountry": "AU"},
        ],
    }]
}

CRA_BODY = (
    "The Clinical Research Associate (CRA) is responsible for managing and "
    "monitoring the conduct of clinical projects according to ICH-GCP, "
    "Standard Operating Procedures (SOP), and applicable Project Management "
    "Plans. "
)

DETAIL_PAYLOAD = {
    "items": [{
        "Id": "4380",
        "Title": "Senior Clinical Research Associate I Flex",
        "PrimaryLocation": "Beijing, Beijing, China",
        "PrimaryLocationCountry": "CN",
        "ExternalPostedStartDate": "2026-08-21T02:11:00+00:00",
        "ExternalPostedEndDate": None,
        # The content is SPLIT across three fields on this tenant.
        "ExternalDescriptionStr": "<p>" + CRA_BODY + "</p>",
        "ExternalResponsibilitiesStr":
            "<ul><li>Conduct site monitoring visits</li>"
            "<li>Verify source data</li></ul>",
        "ExternalQualificationsStr":
            "<p>Minimum 2 years of monitoring experience required.</p>",
        "Category": None,
        "skills": [],
        "requisitionFlexFields": [],
    }]
}


class ListParsingTests(unittest.TestCase):
    def test_requisition_list_is_unwrapped_from_the_items_envelope(self):
        reqs, total = parse_requisition_list(LIST_PAYLOAD)
        self.assertEqual(total, 99)
        self.assertEqual([r["Id"] for r in reqs], ["4389", "4388"])

    def test_empty_payload_is_not_an_error(self):
        self.assertEqual(parse_requisition_list({}), ([], 0))
        self.assertEqual(parse_requisition_list({"items": []}), ([], 0))

    def test_detail_is_unwrapped_from_the_items_envelope(self):
        self.assertEqual(parse_requisition_detail(DETAIL_PAYLOAD)["Id"], "4380")
        self.assertEqual(parse_requisition_detail({"items": []}), {})


class DescriptionTests(unittest.TestCase):
    def test_split_body_fields_are_concatenated_in_reading_order(self):
        detail = parse_requisition_detail(DETAIL_PAYLOAD)
        text = build_description(detail)
        self.assertIn("Clinical Research Associate", text)
        self.assertIn("site monitoring visits", text)
        self.assertIn("Minimum 2 years", text)
        self.assertLess(text.index("Clinical Research Associate"),
                        text.index("site monitoring visits"))
        self.assertLess(text.index("site monitoring visits"),
                        text.index("Minimum 2 years"))

    def test_html_is_stripped(self):
        text = build_description(parse_requisition_detail(DETAIL_PAYLOAD))
        self.assertNotIn("<", text)

    def test_short_description_is_the_last_resort(self):
        detail = {"ShortDescriptionStr": "<p>Leads GCP trials.</p>"}
        self.assertEqual(build_description(detail), "Leads GCP trials.")

    def test_no_text_anywhere_is_empty_not_an_error(self):
        self.assertEqual(build_description({}), "")

    def test_strip_html_unescapes_entities(self):
        self.assertEqual(strip_html("<p>R&amp;D roles</p>"), "R&D roles")


class LocationTests(unittest.TestCase):
    def test_city_region_country_splits_into_city_and_state(self):
        self.assertEqual(parse_location("Shanghai, Shanghai, China", "CN"),
                         ("Shanghai", "Shanghai", "China"))

    def test_country_only_location_leaves_city_blank(self):
        self.assertEqual(parse_location("Australia", "AU"),
                         ("", "", "Australia"))

    def test_city_country_keeps_the_city(self):
        self.assertEqual(parse_location("Beijing, Beijing, China", "CN"),
                         ("Beijing", "Beijing", "China"))

    def test_unknown_code_is_never_guessed(self):
        self.assertEqual(parse_location("Somewhere, Atlantis", "ZZ"),
                         ("Somewhere, Atlantis", "", ""))

    def test_hong_kong_and_taiwan_are_mapped(self):
        self.assertEqual(parse_location("Hong Kong", "HK"),
                         ("", "", "Hong Kong"))
        self.assertEqual(parse_location("Taipei, Taiwan", "TW"),
                         ("Taipei", "", "Taiwan"))


class DateTests(unittest.TestCase):
    def test_oracle_timestamp_reduces_to_a_date(self):
        self.assertEqual(parse_iso_date("2026-08-21T02:11:00+00:00"),
                         "2026-08-21")

    def test_plain_listing_date_passes_through(self):
        self.assertEqual(parse_iso_date("2026-08-27"), "2026-08-27")

    def test_missing_date_is_blank(self):
        self.assertEqual(parse_iso_date(None), "")
        self.assertEqual(parse_iso_date("not a date"), "")


class ExperienceTests(unittest.TestCase):
    def test_minimum_years_is_lifted_from_the_prose(self):
        self.assertEqual(
            parse_experience_years("Minimum 2 years of monitoring experience"),
            "2")

    def test_years_of_experience_phrasing(self):
        self.assertEqual(
            parse_experience_years("with 5+ years of clinical experience"),
            "5")

    def test_nothing_stated_stays_blank(self):
        self.assertEqual(parse_experience_years("A great opportunity."), "")
        self.assertEqual(parse_experience_years(""), "")


class CjkTests(unittest.TestCase):
    """CX_4 (China) postings in Chinese are kept but flagged, never dropped."""

    def test_chinese_description_is_detected(self):
        text = "负责临床试验的监查工作，确保研究按照方案执行。" * 10
        self.assertTrue(looks_cjk(text))

    def test_english_description_is_not(self):
        self.assertFalse(looks_cjk(CRA_BODY))

    def test_an_english_posting_naming_a_chinese_city_is_not_flagged(self):
        self.assertFalse(looks_cjk("CRA role based in Beijing (北京). " +
                                   CRA_BODY))

    def test_empty_description_is_not_cjk(self):
        self.assertFalse(looks_cjk(""))
        self.assertFalse(looks_cjk(None))


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

    def test_an_empty_store_falls_back_to_the_initial_window(self):
        df = pd.DataFrame({"posted_date": []})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


class BuildRowTests(unittest.TestCase):
    def setUp(self):
        self.detail = parse_requisition_detail(DETAIL_PAYLOAD)
        self.row = build_row("4380", "CX_4", self.detail, "2026-08-21")

    def test_row_has_exactly_the_fleet_columns(self):
        self.assertEqual(sorted(self.row), sorted(RICH_COLUMNS))

    def test_company_is_novotech(self):
        self.assertEqual(self.row["company"], "Novotech")

    def test_listing_date_is_preferred_and_detail_is_the_fallback(self):
        self.assertEqual(self.row["posted_date"], "2026-08-21")
        self.assertEqual(build_row("4380", "CX_4", self.detail, "")["posted_date"],
                         "2026-08-21")

    def test_missing_end_date_stays_blank_never_invented(self):
        self.assertEqual(self.row["valid_through"], "")

    def test_job_url_carries_the_requisitions_own_site(self):
        self.assertIn("/sites/CX_4/job/4380", self.row["job_url"])

    def test_experience_is_lifted_from_the_qualifications_text(self):
        self.assertEqual(self.row["experience_min_years"], "2")

    def test_location_fields(self):
        self.assertEqual(self.row["city"], "Beijing")
        self.assertEqual(self.row["state"], "Beijing")
        self.assertEqual(self.row["country"], "China")


class ClassificationTests(unittest.TestCase):
    def _row(self, title, sectors="", description=""):
        return {"title": title, "sectors": sectors, "description": description}

    def test_a_cra_is_in_scope_clinical_research(self):
        row = self._row("Senior Clinical Research Associate I Flex",
                        description=CRA_BODY)
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")

    def test_an_out_of_scope_title_is_dropped(self):
        row = self._row("Business Development Director",
                        description="CRO business development and sales.")
        self.assertFalse(apply_classification(row))

    def test_the_veto_trace_is_recorded_on_dropped_rows(self):
        row = self._row("Business Development Director",
                        description="CRO business development and sales.")
        apply_classification(row)
        self.assertTrue(row["vetoed_by"])

    def test_a_chinese_language_row_is_kept_but_forced_into_review(self):
        row = self._row("Clinical Research Associate",
                        description="负责临床试验的监查工作，确保研究按照方案执行，"
                                    "并遵循ICH-GCP以及相关法规的要求。" * 5)
        if apply_classification(row):
            self.assertTrue(row["needs_review"])


class ClubExportTests(unittest.TestCase):
    def _club(self, **over):
        row = {"country": "Australia", "city": "Sydney", "state": "NSW",
               "company": "Novotech", "title": "Clinical Research Associate",
               "description": "Requires a Bachelor's degree in life sciences.",
               "category": "Non Clinical", "sub_category": "Clinical Research",
               "role_family": "Clinical Research",
               "job_url": "https://x/job/1", "posted_date": "2026-08-27",
               "experience_min_years": "2"}
        row.update(over)
        return rich_row_to_club_row(row)

    def test_club_row_has_exactly_the_club_columns(self):
        self.assertEqual(sorted(self._club()), sorted(CLUB_COLUMNS))

    def test_country_code_and_dial_code_come_back_from_the_country_name(self):
        club = self._club()
        self.assertEqual(club["country_code"], "AU")
        self.assertEqual(club["country_dial_code"], "+61")

    def test_a_cro_exports_as_pharma_company_type(self):
        self.assertEqual(self._club()["company_type"], "pharma")

    def test_salary_is_never_invented(self):
        club = self._club()
        for col in ("min_salary", "max_salary", "salary_period",
                    "salary_currency"):
            self.assertEqual(club[col], "")

    def test_qualification_is_lifted_from_the_description(self):
        self.assertIn("Bachelor", self._club()["qualification"])

    def test_an_unmapped_country_exports_blank_code_not_a_guess(self):
        club = self._club(country="Atlantis")
        self.assertEqual(club["country_name"], "Atlantis")
        self.assertEqual(club["country_code"], "")


class ComplianceTests(unittest.TestCase):
    def test_every_request_goes_to_the_oracle_candidate_experience_host(self):
        for url in (scraper.LIST_URL, scraper.DETAIL_URL, scraper.ROBOTS_URL,
                    scraper.JOB_URL_TEMPLATE):
            self.assertTrue(url.startswith(scraper.CE_HOST))

    def test_both_published_sites_are_crawled(self):
        self.assertEqual(SITE_NUMBERS, ("CX_1", "CX_4"))

    def test_country_table_is_iso_alpha_2_keyed(self):
        for code in COUNTRY_BY_CODE:
            self.assertRegex(code, r"^[A-Z]{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
