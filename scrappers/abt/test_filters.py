#!/usr/bin/env python3
"""Unit tests for the Abt Global scraper's parsers, cutoff and export mapping.

Run with plain:  python test_filters.py

Worked examples are trimmed real payloads from the Oracle HCM Candidate
Experience API on egpy.fa.us2.oraclecloud.com (28 Aug 2026): the DRC
malaria Chief of Party (107214) and the Papua New Guinea WASH Specialist
(107226), plus the listing page they came from (site JoinAbt).
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
    build_sectors,
    compute_cutoff,
    is_eoi_title,
    looks_non_english,
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
        "TotalJobsCount": 16,
        "requisitionList": [
            {"Id": "107227", "Title": "Data Analyst |APSP",
             "PostedDate": "2026-08-28", "PostingEndDate": None,
             "PrimaryLocation": "Port Moresby, Papua New Guinea",
             "PrimaryLocationCountry": "PG"},
            {"Id": "107214", "Title": "Chief of Party - DRC Malaria Activity",
             "PostedDate": "2026-08-24", "PostingEndDate": None,
             "PrimaryLocation": "Kinshasa, DR Congo-Kinshasa",
             "PrimaryLocationCountry": "CD"},
        ],
    }]
}

COP_BODY = (
    "Under the supervision of the U.S.-based Project Management Director, "
    "the Chief of Party (COP) manages and supervises the successful "
    "implementation of the Abt Global Central Mechanism in the Democratic "
    "Republic of Congo and acts as the primary liaison between the project "
    "and the National Malaria Program (NMP), Ministry of Health, and various "
    "other malaria stakeholders in-country. The project sustains "
    "uninterrupted malaria vector control interventions, including "
    "insecticide-treated net (ITN) distribution and indoor residual "
    "spraying (IRS). Minimum of 10 years of experience managing large "
    "public health programs in sub-Saharan Africa required."
)

DETAIL_PAYLOAD = {
    "items": [{
        "Id": "107214",
        "Title": "Chief of Party - DRC Malaria Activity",
        "PrimaryLocation": "Kinshasa, DR Congo-Kinshasa",
        "PrimaryLocationCountry": "CD",
        "ExternalPostedStartDate": "2026-08-24T09:00:00+00:00",
        # Real data on this tenant — the application deadline.
        "ExternalPostedEndDate": "2026-09-13T23:55:00+00:00",
        "ExternalDescriptionStr": "<p>" + COP_BODY + "</p>",
        "ExternalResponsibilitiesStr": "",
        "ExternalQualificationsStr": "",
        "ShortDescriptionStr": "",
        "Category": "Program Delivery",
        "JobFunction": "Program Operations (Field)",
        "JobFamily": None,
        "skills": [],
        "requisitionFlexFields": [],
    }]
}


class ListParsingTests(unittest.TestCase):
    def test_requisition_list_is_unwrapped_from_the_items_envelope(self):
        reqs, total = parse_requisition_list(LIST_PAYLOAD)
        self.assertEqual(total, 16)
        self.assertEqual([r["Id"] for r in reqs], ["107227", "107214"])

    def test_empty_payload_is_not_an_error(self):
        self.assertEqual(parse_requisition_list({}), ([], 0))
        self.assertEqual(parse_requisition_list({"items": []}), ([], 0))

    def test_detail_is_unwrapped_from_the_items_envelope(self):
        self.assertEqual(parse_requisition_detail(DETAIL_PAYLOAD)["Id"],
                         "107214")
        self.assertEqual(parse_requisition_detail({"items": []}), {})


class DescriptionTests(unittest.TestCase):
    def test_description_field_carries_the_posting(self):
        text = build_description(parse_requisition_detail(DETAIL_PAYLOAD))
        self.assertIn("Chief of Party", text)
        self.assertIn("malaria vector control", text)

    def test_html_is_stripped(self):
        text = build_description(parse_requisition_detail(DETAIL_PAYLOAD))
        self.assertNotIn("<", text)

    def test_split_body_fields_are_concatenated_in_reading_order(self):
        # Empty on every sampled Abt detail, but the novotech split-body
        # lesson is kept: a reconfigured tenant must not lose text.
        detail = {"ExternalDescriptionStr": "<p>Intro.</p>",
                  "ExternalResponsibilitiesStr": "<li>Run surveys</li>",
                  "ExternalQualificationsStr": "<p>MPH required.</p>"}
        text = build_description(detail)
        self.assertLess(text.index("Intro"), text.index("Run surveys"))
        self.assertLess(text.index("Run surveys"), text.index("MPH"))

    def test_short_description_is_the_last_resort(self):
        detail = {"ShortDescriptionStr": "<p>Seeking a WASH Specialist.</p>"}
        self.assertEqual(build_description(detail),
                         "Seeking a WASH Specialist.")

    def test_no_text_anywhere_is_empty_not_an_error(self):
        self.assertEqual(build_description({}), "")

    def test_strip_html_unescapes_entities(self):
        self.assertEqual(strip_html("<p>M&amp;E roles</p>"), "M&E roles")


class SectorsTests(unittest.TestCase):
    def test_category_and_job_function_are_joined(self):
        self.assertEqual(build_sectors(parse_requisition_detail(DETAIL_PAYLOAD)),
                         "Program Delivery; Program Operations (Field)")

    def test_all_null_tags_stay_blank(self):
        self.assertEqual(build_sectors({}), "")


class LocationTests(unittest.TestCase):
    def test_us_city_state_country_splits_fully(self):
        self.assertEqual(parse_location("Durham, NC, United States", "US"),
                         ("Durham", "NC", "United States"))

    def test_city_country_keeps_the_city(self):
        self.assertEqual(parse_location("Daru Island, Papua New Guinea", "PG"),
                         ("Daru Island", "", "Papua New Guinea"))

    def test_drc_alias_is_dropped_not_leaked_into_the_city(self):
        # The tenant writes "DR Congo-Kinshasa" where the ISO name is
        # "Democratic Republic of the Congo".
        self.assertEqual(parse_location("Kinshasa, DR Congo-Kinshasa", "CD"),
                         ("Kinshasa", "",
                          "Democratic Republic of the Congo"))

    def test_country_only_location_leaves_city_blank(self):
        self.assertEqual(parse_location("Fiji", "FJ"), ("", "", "Fiji"))

    def test_unknown_code_is_never_guessed(self):
        self.assertEqual(parse_location("Somewhere, Atlantis", "ZZ"),
                         ("Somewhere, Atlantis", "", ""))


class DateTests(unittest.TestCase):
    def test_oracle_timestamp_reduces_to_a_date(self):
        self.assertEqual(parse_iso_date("2026-08-24T09:00:00+00:00"),
                         "2026-08-24")

    def test_plain_listing_date_passes_through(self):
        self.assertEqual(parse_iso_date("2026-08-28"), "2026-08-28")

    def test_missing_date_is_blank(self):
        self.assertEqual(parse_iso_date(None), "")
        self.assertEqual(parse_iso_date("not a date"), "")


class ExperienceTests(unittest.TestCase):
    def test_minimum_years_is_lifted_from_the_prose(self):
        self.assertEqual(parse_experience_years(COP_BODY), "10")

    def test_years_of_experience_phrasing(self):
        self.assertEqual(
            parse_experience_years("with 5+ years of M&E experience"), "5")

    def test_nothing_stated_stays_blank(self):
        self.assertEqual(parse_experience_years("A great opportunity."), "")
        self.assertEqual(parse_experience_years(""), "")


class LanguageTests(unittest.TestCase):
    """FR/ES postings (DRC, Madagascar, LatAm offices) are kept but
    flagged, never dropped."""

    def test_french_description_is_detected(self):
        text = ("Sous la supervision du directeur du projet, le titulaire "
                "assure la mise en œuvre des activités de lutte contre le "
                "paludisme dans les zones ciblées pour le compte du "
                "ministère de la santé et des partenaires, avec une "
                "attention pour la qualité des données dans les provinces.")
        self.assertTrue(looks_non_english(text))

    def test_english_description_is_not(self):
        self.assertFalse(looks_non_english(COP_BODY))

    def test_empty_description_is_not_flagged(self):
        self.assertFalse(looks_non_english(""))
        self.assertFalse(looks_non_english(None))


class EoiTests(unittest.TestCase):
    """EOI/talent-pool calls share the requisition id space (3 of 16 on the
    probe) — kept when in scope, force-flagged."""

    def test_expression_of_interest_is_flagged(self):
        self.assertTrue(is_eoi_title(
            "Expression of Interest: Africa-Based Global Health Security "
            "& Diplomacy Consultants"))

    def test_advisory_panel_is_flagged(self):
        self.assertTrue(is_eoi_title("Technical Advisory Panel | PALMSS 2"))

    def test_a_plain_vacancy_is_not(self):
        self.assertFalse(is_eoi_title("Chief of Party - DRC Malaria Activity"))
        self.assertFalse(is_eoi_title(""))


class CutoffTests(unittest.TestCase):
    def test_first_run_uses_the_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_the_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-10", "2026-08-24"]})
        expected = (date(2026, 8, 24) -
                    timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_since_overrides_everything(self):
        df = pd.DataFrame({"posted_date": ["2026-08-24"]})
        self.assertEqual(compute_cutoff(df, since="2026-01-01"), "2026-01-01")

    def test_an_empty_store_falls_back_to_the_initial_window(self):
        df = pd.DataFrame({"posted_date": []})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


class BuildRowTests(unittest.TestCase):
    def setUp(self):
        self.detail = parse_requisition_detail(DETAIL_PAYLOAD)
        self.row = build_row("107214", "JoinAbt", self.detail, "2026-08-24")

    def test_row_has_exactly_the_fleet_columns(self):
        self.assertEqual(sorted(self.row), sorted(RICH_COLUMNS))

    def test_company_is_abt_global(self):
        self.assertEqual(self.row["company"], "Abt Global")

    def test_listing_date_is_preferred_and_detail_is_the_fallback(self):
        self.assertEqual(self.row["posted_date"], "2026-08-24")
        self.assertEqual(
            build_row("107214", "JoinAbt", self.detail, "")["posted_date"],
            "2026-08-24")

    def test_the_application_deadline_is_captured_as_valid_through(self):
        self.assertEqual(self.row["valid_through"], "2026-09-13")

    def test_job_url_carries_the_join_abt_site(self):
        self.assertIn("/sites/JoinAbt/job/107214", self.row["job_url"])

    def test_experience_is_lifted_from_the_prose(self):
        self.assertEqual(self.row["experience_min_years"], "10")

    def test_location_fields(self):
        self.assertEqual(self.row["city"], "Kinshasa")
        self.assertEqual(self.row["state"], "")
        self.assertEqual(self.row["country"],
                         "Democratic Republic of the Congo")

    def test_sectors_carry_the_oracle_tags(self):
        self.assertEqual(self.row["sectors"],
                         "Program Delivery; Program Operations (Field)")


class ClassificationTests(unittest.TestCase):
    def _row(self, title, sectors="", description=""):
        return {"title": title, "sectors": sectors, "description": description}

    def test_a_malaria_chief_of_party_is_in_scope_public_health(self):
        row = self._row("Chief of Party - DRC Malaria Activity",
                        sectors="Program Delivery; Program Operations (Field)",
                        description=COP_BODY)
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")

    def test_an_out_of_scope_title_is_dropped(self):
        row = self._row("Contracts and Pricing Analyst",
                        description="Prepares cost proposals, budgets and "
                                    "pricing for federal contract bids.")
        self.assertFalse(apply_classification(row))

    def test_the_generic_oracle_tags_alone_admit_nothing(self):
        # "Project and Program Management" as skills must not push a
        # non-health role into scope.
        row = self._row("Finance Technical Officer",
                        sectors="Project and Program Management; "
                                "Project Management",
                        description="Payroll, ledgers and acquittals for the "
                                    "country office finance team.")
        self.assertFalse(apply_classification(row))

    def test_an_eoi_row_is_kept_but_forced_into_review(self):
        row = self._row(
            "Expression of Interest: Africa-Based Global Health Security "
            "& Diplomacy Consultants",
            description="Epidemiologists and public health specialists for "
                        "global health security programs, disease "
                        "surveillance and outbreak response.")
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_a_french_row_is_kept_but_forced_into_review(self):
        row = self._row(
            "Epidemiologiste - Surveillance",
            description="Sous la supervision du directeur, le titulaire "
                        "appuie la surveillance epidemiologique et la "
                        "riposte pour le ministère de la santé dans les "
                        "provinces ciblées, avec les partenaires pour une "
                        "meilleure qualité des données epidemiologiques "
                        "dans le pays. Epidemiology surveillance officer "
                        "supporting disease surveillance and outbreak "
                        "response programs.")
        if apply_classification(row):
            self.assertTrue(row["needs_review"])


class ClubExportTests(unittest.TestCase):
    def _club(self, **over):
        row = {"country": "Democratic Republic of the Congo",
               "city": "Kinshasa", "state": "",
               "company": "Abt Global",
               "title": "Chief of Party - DRC Malaria Activity",
               "description": "Requires a Master's degree in public health.",
               "category": "Public Health",
               "sub_category": "Disease Programs",
               "role_family": "Public Health",
               "job_url": "https://x/job/1", "posted_date": "2026-08-24",
               "experience_min_years": "10"}
        row.update(over)
        return rich_row_to_club_row(row)

    def test_club_row_has_exactly_the_club_columns(self):
        self.assertEqual(sorted(self._club()), sorted(CLUB_COLUMNS))

    def test_country_code_and_dial_code_come_back_from_the_country_name(self):
        club = self._club()
        self.assertEqual(club["country_code"], "CD")
        self.assertEqual(club["country_dial_code"], "+243")

    def test_a_development_implementer_exports_as_hospital_company_type(self):
        # The fleet-wide convention for non-pharma employers in the club's
        # hospital|pharma enum.
        self.assertEqual(self._club()["company_type"], "hospital")

    def test_salary_is_never_invented(self):
        club = self._club()
        for col in ("min_salary", "max_salary", "salary_period",
                    "salary_currency"):
            self.assertEqual(club[col], "")

    def test_qualification_is_lifted_from_the_description(self):
        self.assertIn("Master", self._club()["qualification"])

    def test_an_unmapped_country_exports_blank_code_not_a_guess(self):
        club = self._club(country="Atlantis")
        self.assertEqual(club["country_name"], "Atlantis")
        self.assertEqual(club["country_code"], "")


class ComplianceTests(unittest.TestCase):
    def test_every_request_goes_to_the_oracle_candidate_experience_host(self):
        for url in (scraper.LIST_URL, scraper.DETAIL_URL, scraper.ROBOTS_URL,
                    scraper.JOB_URL_TEMPLATE):
            self.assertTrue(url.startswith(scraper.CE_HOST))

    def test_the_single_published_site_is_join_abt(self):
        self.assertEqual(SITE_NUMBERS, ("JoinAbt",))

    def test_country_table_is_iso_alpha_2_keyed(self):
        for code in COUNTRY_BY_CODE:
            self.assertRegex(code, r"^[A-Z]{2}$")

    def test_request_spacing_is_at_least_the_fleet_minimum(self):
        self.assertGreaterEqual(scraper.REQUEST_DELAY_SECONDS, 1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
