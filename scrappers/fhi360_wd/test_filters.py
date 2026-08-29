#!/usr/bin/env python3
"""Unit tests for the fhi360 scraper's parsers, cutoff and export mapping.

Run with plain:  python test_filters.py

Worked examples are trimmed real payloads captured live from the Workday CXS
API on 28 Aug 2026 (fhi.wd1.myworkdayjobs.com).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

import scraper
from scraper import (
    CLUB_COLUMNS,
    COMPANY_NAME,
    COMPANY_TYPE,
    COUNTRY_BY_CODE,
    INITIAL_WINDOW_DAYS,
    LIST_PAGE_LIMIT,
    RICH_COLUMNS,
    WATERMARK_GRACE_DAYS,
    WORKDAY_SITES,
    apply_classification,
    build_row,
    compute_cutoff,
    is_remote,
    listing_job_id,
    looks_non_english,
    parse_city,
    parse_country,
    parse_experience_years,
    parse_iso_date,
    parse_job_type,
    parse_listing,
    parse_posted_on,
    rich_row_to_club_row,
    strip_html,
)

TODAY = date(2026, 8, 28)

# One trimmed page of the board's real /jobs response (offset 0).
LIST_PAYLOAD = {'jobPostings': [{'bulletFields': ['Requisition - 2026201200'],
                  'externalPath': '/job/Manila-Philippines/Laboratory-Specialist_Requisition-2026201200',
                  'locationsText': 'Manila, Philippines',
                  'postedOn': 'Posted Today',
                  'title': 'Laboratory Specialist'},
                 {'bulletFields': ['Requisition - 2026200852'],
                  'externalPath': '/job/Rabat-Morocco/Monitoring--Evaluation--and-Learning--MEL--Officer_Requisition-2026200852',
                  'locationsText': 'Rabat, Morocco',
                  'postedOn': 'Posted Yesterday',
                  'title': 'Monitoring, Evaluation, and Learning (MEL) '
                           'Officer'}],
 'total': 39}

# One real (trimmed) detail payload for the first posting above.
DETAIL_PAYLOAD = {'hiringOrganization': {'name': 'Family Health International',
                                         'url': ''},
 'jobPostingInfo': {'country': {'descriptor': 'Philippines'},
                    'endDate': '2026-09-11',
                    'externalUrl': 'https://fhi.wd1.myworkdayjobs.com/FHI_360_External_Career_Portal/job/Manila-Philippines/Laboratory-Specialist_Requisition-2026201200',
                    'id': 'bed692dd61141001c93cc9a3961e0000',
                    'jobDescription': '<h2><span>FHI 360/Philippines </span>'
                                      '<span>(www.fhi360.org) through '
                                      '<b>Strengthening Infectious Disease '
                                      'Detection Systems (STRIDES)</b> '
                                      'project in the Philippines is seeking '
                                      'applications for a </span><b><span>'
                                      'Laboratory Specialist.</span></b>'
                                      '<br />\xa0</h2><p>FHI 360 is a '
                                      'nonprofit human development '
                                      'organization dedicated to improving '
                                      'lives in lasting ways.</p>',
                    'jobPostingId': 'Laboratory-Specialist_Requisition-2026201200',
                    'jobPostingSiteId': 'FHI_360_External_Career_Portal',
                    'jobReqId': 'Requisition - 2026201200',
                    'jobRequisitionLocation': {'country': {'alpha2Code': 'PH',
                                                           'descriptor': 'Philippines'},
                                               'descriptor': 'Philippines-Manila '
                                                             '(F; Paseo De '
                                                             'Roxas Bldg)'},
                    'location': 'Manila, Philippines',
                    'postedOn': 'Posted Today',
                    'startDate': '2026-08-28',
                    'timeType': 'Full time',
                    'title': 'Laboratory Specialist'}}

# What build_row must extract from that pair. The job_id is the requisition
# NUMBER — the "Requisition - " prefix is normalized away.
EXPECTED = {'city': 'Manila',
 'country': 'Philippines',
 'country_code': 'PH',
 'job_id': '2026201200',
 'job_type': 'full_time',
 'posted_date': '2026-08-28',
 'url_substr': '/FHI_360_External_Career_Portal/'}


class ListingParsingTests(unittest.TestCase):
    def test_listing_unwraps_postings_and_total(self):
        postings, total = parse_listing(LIST_PAYLOAD)
        self.assertEqual(total, LIST_PAYLOAD["total"])
        self.assertEqual(len(postings), len(LIST_PAYLOAD["jobPostings"]))

    def test_empty_payload_is_not_an_error(self):
        self.assertEqual(parse_listing({}), ([], 0))
        self.assertEqual(parse_listing(None), ([], 0))

    def test_job_id_is_the_normalized_requisition_number(self):
        # bulletFields carries "Requisition - 2026201200" — the prefix is
        # noise and its spacing differs from the externalPath tail's
        # "Requisition-2026201200", so only the number is the id.
        self.assertEqual(listing_job_id(LIST_PAYLOAD["jobPostings"][0]),
                         EXPECTED["job_id"])

    def test_job_id_falls_back_to_the_external_path_tail_normalized(self):
        posting = {"externalPath":
                   "/job/Rabat-Morocco/Some-Job_Requisition-2026200852",
                   "bulletFields": []}
        self.assertEqual(listing_job_id(posting), "2026200852")

    def test_both_id_sources_agree_after_normalization(self):
        posting = LIST_PAYLOAD["jobPostings"][1]
        stripped = dict(posting, bulletFields=[])
        self.assertEqual(listing_job_id(posting), listing_job_id(stripped))

    def test_no_id_at_all_is_blank_not_a_crash(self):
        self.assertEqual(listing_job_id({}), "")


class PostedOnTests(unittest.TestCase):
    def test_posted_today(self):
        self.assertEqual(parse_posted_on("Posted Today", TODAY), TODAY)

    def test_posted_yesterday(self):
        self.assertEqual(parse_posted_on("Posted Yesterday", TODAY),
                         TODAY - timedelta(days=1))

    def test_posted_n_days_ago(self):
        self.assertEqual(parse_posted_on("Posted 3 Days Ago", TODAY),
                         TODAY - timedelta(days=3))

    def test_posted_30_plus_days_ago_is_a_floor(self):
        self.assertEqual(parse_posted_on("Posted 30+ Days Ago", TODAY),
                         TODAY - timedelta(days=30))

    def test_unknown_label_returns_none_so_the_detail_decides(self):
        self.assertIsNone(parse_posted_on("Recently updated", TODAY))
        self.assertIsNone(parse_posted_on("", TODAY))
        self.assertIsNone(parse_posted_on(None, TODAY))


class LocationTests(unittest.TestCase):
    """FHI 360's real location string formats — city first, plus US remote."""

    def test_city_comes_before_the_country(self):
        self.assertEqual(parse_city("Manila, Philippines", "Philippines",
                                    "PH"), "Manila")
        self.assertEqual(parse_city("Rabat, Morocco", "Morocco", "MA"),
                         "Rabat")

    def test_usa_remote_any_has_no_city(self):
        # "USA-Remote (Any)" — the alias and the qualified remote marker
        # are both dropped; build_row then exports city "Remote".
        self.assertEqual(parse_city("USA-Remote (Any)", "United States",
                                    "US"), "")

    def test_us_remote_hub_strings_have_no_city(self):
        # "US-REMOTE-DC" / "US-REMOTE-NC" (captured live, req 2026201177):
        # the bare state abbreviation is not a city — the remote fallback
        # then exports city "Remote".
        self.assertEqual(parse_city("US-REMOTE-DC", "United States", "US"), "")
        self.assertEqual(parse_city("US-REMOTE-NC", "United States", "US"), "")

    def test_country_only_string_has_no_city(self):
        self.assertEqual(parse_city("Indonesia", "Indonesia", "ID"), "")

    def test_n_locations_placeholder_is_not_a_city(self):
        self.assertEqual(parse_city("2 Locations", "Morocco", "MA"), "")

    def test_remote_detection(self):
        self.assertTrue(is_remote("USA-Remote (Any)"))
        self.assertTrue(is_remote("Home Based"))
        self.assertFalse(is_remote("Manila, Philippines", "Rabat, Morocco"))

    def test_country_descriptor_is_folded_to_the_canonical_name(self):
        info = {"country": {"descriptor": "United States of America"},
                "jobRequisitionLocation": {"country": {"alpha2Code": "US"}}}
        self.assertEqual(parse_country(info), ("United States", "US"))

    def test_country_code_is_recovered_from_the_name_when_missing(self):
        info = {"country": {"descriptor": "Morocco"}}
        self.assertEqual(parse_country(info), ("Morocco", "MA"))

    def test_unknown_country_is_never_guessed(self):
        info = {"country": {"descriptor": "Atlantis"}}
        self.assertEqual(parse_country(info), ("Atlantis", ""))

    def test_job_type_enum(self):
        self.assertEqual(parse_job_type("Full time", False), "full_time")
        self.assertEqual(parse_job_type("Part time", False), "part_time")
        self.assertEqual(parse_job_type("Full time", True), "remote")
        self.assertEqual(parse_job_type("", False), "full_time")


class DateAndExperienceTests(unittest.TestCase):
    def test_workday_start_date_passes_through(self):
        self.assertEqual(parse_iso_date("2026-08-28"), "2026-08-28")
        self.assertEqual(parse_iso_date("2026-08-28T04:58:17+00:00"),
                         "2026-08-28")
        self.assertEqual(parse_iso_date(None), "")

    def test_stated_experience_is_lifted(self):
        self.assertEqual(parse_experience_years(
            "Requires a minimum of 5 years of experience in health "
            "information systems."), "5")
        self.assertEqual(parse_experience_years(
            "3+ years of relevant experience required."), "3")

    def test_experience_is_never_invented(self):
        self.assertEqual(parse_experience_years("A great opportunity."), "")


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
        self.posting = LIST_PAYLOAD["jobPostings"][0]
        self.row = build_row(WORKDAY_SITES[0], EXPECTED["job_id"],
                             self.posting, DETAIL_PAYLOAD, today=TODAY)

    def test_row_has_exactly_the_fleet_columns(self):
        self.assertEqual(sorted(self.row), sorted(RICH_COLUMNS))

    def test_core_fields_match_the_captured_payload(self):
        self.assertEqual(self.row["job_id"], EXPECTED["job_id"])
        self.assertEqual(self.row["city"], EXPECTED["city"])
        self.assertEqual(self.row["country"], EXPECTED["country"])
        self.assertEqual(self.row["country_code"], EXPECTED["country_code"])
        self.assertEqual(self.row["job_type"], EXPECTED["job_type"])
        self.assertEqual(self.row["posted_date"], EXPECTED["posted_date"])

    def test_the_workday_site_is_recorded_per_row(self):
        self.assertEqual(self.row["workday_site"], WORKDAY_SITES[0])

    def test_company_is_the_board_brand_not_the_legal_entity(self):
        self.assertEqual(self.row["company"], COMPANY_NAME)

    def test_the_legal_entity_is_kept_raw_in_hiring_org(self):
        self.assertEqual(self.row["hiring_org"], "Family Health International")

    def test_job_url_is_the_canonical_external_url(self):
        self.assertIn(EXPECTED["url_substr"], self.row["job_url"])

    def test_description_is_html_free(self):
        self.assertNotIn("<", self.row["description"])
        self.assertTrue(self.row["description"])

    def test_salary_is_not_disclosed_never_invented(self):
        self.assertEqual(self.row["salary_raw"], "Not Disclosed")
        self.assertEqual(self.row["salary_min_monthly"], "")
        self.assertEqual(self.row["salary_max_monthly"], "")

    def test_a_usa_remote_any_row_exports_city_remote(self):
        posting = {"title": "Project Manager", "bulletFields": ["Requisition - 1"],
                   "locationsText": "USA-Remote (Any)",
                   "postedOn": "Posted Today",
                   "externalPath": "/job/USA-Remote-Any/Project-Manager_Requisition-1"}
        detail = {"jobPostingInfo": {
            "title": "Project Manager", "location": "USA-Remote (Any)",
            "country": {"descriptor": "United States of America"},
            "jobRequisitionLocation": {"country": {"alpha2Code": "US"}},
            "startDate": "2026-08-28", "timeType": "Full time",
            "jobDescription": "<p>Remote role.</p>"}}
        row = build_row(WORKDAY_SITES[0], "1", posting, detail, today=TODAY)
        self.assertEqual(row["city"], "Remote")
        self.assertEqual(row["job_type"], "remote")
        self.assertEqual(row["country_code"], "US")

    def test_the_detail_start_date_wins_over_the_relative_label(self):
        # the listing label has day granularity; startDate is authoritative
        self.assertRegex(self.row["posted_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_a_missing_detail_falls_back_to_the_relative_label(self):
        row = build_row(WORKDAY_SITES[0], "X1",
                        {"title": "T", "locationsText": "Indonesia",
                         "postedOn": "Posted Yesterday", "externalPath": "/job/a/b_X1"},
                        {"jobPostingInfo": {"title": "T"}}, today=TODAY)
        self.assertEqual(row["posted_date"],
                         (TODAY - timedelta(days=1)).isoformat())


class ClassificationTests(unittest.TestCase):
    """Wiring only — the shared engine is covered by _shared's own tests."""

    def _row(self, title, description=""):
        return {"title": title, "description": description}

    def test_a_mel_role_lands_in_public_health(self):
        row = self._row("Monitoring, Evaluation, and Learning (MEL) Officer",
                        "Design M&E frameworks, indicators and data quality "
                        "assessments for a global health project.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Monitoring & Evaluation")
        self.assertTrue(row["role_family"])

    def test_a_health_information_systems_role_maps_correctly(self):
        row = self._row("Senior Health Information Systems Officer",
                        "Support DHIS2 health information systems and health "
                        "data interoperability for the ministry of health.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Health Informatics & Data")

    def test_out_of_scope_titles_are_dropped(self):
        self.assertFalse(apply_classification(self._row(
            "Finance Manager", "Oversee project accounting and donor "
            "financial reporting.")))
        self.assertFalse(apply_classification(self._row(
            "Accounting and Finance Officer",
            "Manage vouchers, ledgers and financial reports.")))

    def test_the_clinical_veto_is_respected(self):
        self.assertFalse(apply_classification(self._row(
            "Staff Nurse", "Bedside nursing care on the ward.")))

    def test_a_non_english_description_is_kept_but_flagged(self):
        french = ("Dans le cadre du programme de santé mondiale, nous "
                  "recherchons une personne pour la coordination des "
                  "activités dans les districts. Le candidat sera responsable "
                  "de la collecte des données pour le suivi et une évaluation "
                  "des campagnes dans les zones ciblées, et pour la "
                  "supervision des équipes dans le pays. Une expérience dans "
                  "le domaine de la santé publique est exigée pour ce poste.")
        self.assertTrue(looks_non_english(french))
        row = self._row("Chargé de Suivi et Évaluation", french)
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_english_descriptions_are_not_flagged_by_the_language_check(self):
        self.assertFalse(looks_non_english(
            "Strengthen infectious disease surveillance, support health "
            "information systems, and coordinate with ministry partners."))


class ClubExportTests(unittest.TestCase):
    def _club(self, **over):
        row = {"country": "Philippines", "country_code": "PH",
               "city": "Manila",
               "company": COMPANY_NAME,
               "title": "Monitoring, Evaluation, and Learning Officer",
               "description": "Requires a Master's degree in public health "
                              "or epidemiology.",
               "category": "Public Health",
               "sub_category": "Monitoring & Evaluation",
               "role_family": "Monitoring & Evaluation",
               "job_type": "full_time",
               "job_url": "https://x/job/1", "posted_date": "2026-08-28",
               "experience_min_years": "2"}
        row.update(over)
        return rich_row_to_club_row(row)

    def test_club_row_has_exactly_the_club_columns(self):
        self.assertEqual(sorted(self._club()), sorted(CLUB_COLUMNS))

    def test_dial_code_comes_from_the_stored_country_code(self):
        club = self._club()
        self.assertEqual(club["country_code"], "PH")
        self.assertEqual(club["country_dial_code"], "+63")

    def test_a_missing_code_is_recovered_from_the_country_name(self):
        club = self._club(country="Morocco", country_code="")
        self.assertEqual(club["country_code"], "MA")
        self.assertEqual(club["country_dial_code"], "+212")

    def test_an_unmapped_country_exports_blank_code_not_a_guess(self):
        club = self._club(country="Atlantis", country_code="")
        self.assertEqual(club["country_name"], "Atlantis")
        self.assertEqual(club["country_code"], "")

    def test_remote_rows_follow_the_himalayas_convention(self):
        club = self._club(city="Remote", job_type="remote")
        self.assertEqual(club["city_name"], "Remote")
        self.assertEqual(club["job_type"], "remote")

    def test_company_type_is_the_board_constant(self):
        self.assertEqual(self._club()["company_type"], COMPANY_TYPE)

    def test_ngo_boards_default_to_hospital_in_the_club_enum(self):
        self.assertEqual(COMPANY_TYPE, "hospital")

    def test_salary_is_never_invented(self):
        club = self._club()
        for col in ("min_salary", "max_salary", "salary_period",
                    "salary_currency"):
            self.assertEqual(club[col], "")

    def test_qualification_is_lifted_from_the_description(self):
        self.assertIn("Master", self._club()["qualification"])

    def test_experience_maps_to_min_experience(self):
        self.assertEqual(self._club()["min_experience"], "2")


class ComplianceTests(unittest.TestCase):
    def test_the_hard_workday_page_cap_is_respected(self):
        # limit > 20 answers HTTP 400 (probe-verified 2026-08-28)
        self.assertEqual(LIST_PAGE_LIMIT, 20)

    def test_every_configured_url_points_at_the_workday_host(self):
        for url in (scraper.LIST_URL, scraper.DETAIL_URL, scraper.ROBOTS_URL,
                    scraper.PUBLIC_JOB_URL):
            self.assertTrue(url.startswith(scraper.HOST))

    def test_the_polite_delay_meets_the_spec_floor(self):
        self.assertGreaterEqual(scraper.REQUEST_DELAY_SECONDS, 1.5)

    def test_workday_sites_are_configured(self):
        self.assertEqual(WORKDAY_SITES, ['FHI_360_External_Career_Portal'])

    def test_small_boards_are_walked_fully(self):
        # ~38 postings on 2026-08-28 — well inside the exhaustive-walk cap,
        # so the default ordering's stale-row pinning cannot hide new jobs.
        self.assertGreaterEqual(scraper.EXHAUSTIVE_BOARD_MAX, 200)

    def test_country_table_is_iso_alpha_2_keyed(self):
        for code in COUNTRY_BY_CODE:
            self.assertRegex(code, r"^[A-Z]{2}$")

    def test_strip_html_flattens_workday_markup(self):
        self.assertEqual(strip_html("<p><b>About Us:</b></p><p>Hi</p>"),
                         "About Us: Hi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
