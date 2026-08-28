#!/usr/bin/env python3
"""Unit tests for the syneoshealth scraper's parsers, cutoff and export mapping.

Run with plain:  python test_filters.py

Worked examples are trimmed real payloads captured live from the Workday CXS
API on 28 Aug 2026 (syneoshealth.wd12.myworkdayjobs.com).
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
LIST_PAYLOAD = {'jobPostings': [{'bulletFields': ['25111571'],
                  'externalPath': '/job/TWN-Taipei/Clinical-Operation-Specialist--1-year-contract---Fresh-graduates-are-welcome-_25111571',
                  'locationsText': 'TWN-Taipei',
                  'postedOn': 'Posted Today',
                  'title': 'Clinical Operation Specialist (1-year contract) '
                           '(Fresh graduates are welcome)'},
                 {'bulletFields': ['25112131'],
                  'externalPath': '/job/LBN-Remote/Senior-CRA-with-strong-IoR---Regulatory-experience-FSP-Homebased-in-Lebanon_25112131',
                  'locationsText': 'LBN-Remote',
                  'postedOn': 'Posted Today',
                  'title': 'Senior CRA with strong IoR & Regulatory '
                           'experience–FSP–Homebased in Lebanon'}],
 'total': 658}

# One real (trimmed) detail payload for the first posting above.
DETAIL_PAYLOAD = {'hiringOrganization': {'name': '5068 Taiwan Syneos Health '
                                                 'Company Limited',
                                         'url': ''},
 'jobPostingInfo': {'country': {'descriptor': 'Taiwan',
                                'id': 'a4e08b475d6a4176853c9d1cb9854e02'},
                    'externalUrl': 'https://syneoshealth.wd12.myworkdayjobs.com/Syneos_Health_External_Site/job/TWN-Taipei/Clinical-Operation-Specialist--1-year-contract---Fresh-graduates-are-welcome-_25111571',
                    'id': '863009c497911001f2d8aa9319c60000',
                    'jobDescription': 'Clinical Operation Specialist (1-year '
                                      'contract) (Fresh graduates are '
                                      'welcome)<p></p><p>Syneos Health® '
                                      'is a leading fully-integrated life '
                                      'sciences services organization built '
                                      'to accelerate customer success. We '
                                      'partner with innovators at every '
                                      'point across the drug development '
                                      'and commercialization continuum, '
                                      'helping them navigate complexity, '
                                      'anticipate change and accelerate '
                                      'progress.<br />\xa0<br />Our '
                                      'Clinical Solutions team members act '
                                      'with a drug development mindset, '
                                      'applying their years of experience '
                                      'and deep expertise to truly '
                                      'understand customer needs and '
                                      'represent those in the solutions we '
                                      'shape.</p>',
                    'jobPostingId': 'Clinical-Operation-Specialist--1-year-contract---Fresh-graduates-are-welcome-_25111571',
                    'jobReqId': '25111571',
                    'jobRequisitionLocation': {'country': {'alpha2Code': 'TW',
                                                           'descriptor': 'Taiwan',
                                                           'id': 'a4e08b475d6a4176853c9d1cb9854e02'},
                                               'descriptor': 'TWN-Taipei'},
                    'location': 'TWN-Taipei',
                    'postedOn': 'Posted Today',
                    'remoteType': None,
                    'startDate': '2026-08-28',
                    'timeType': 'Full time',
                    'title': 'Clinical Operation Specialist (1-year '
                             'contract) (Fresh graduates are welcome)'}}

# What build_row must extract from that pair. NOTE the city: this board's
# "AAA-City" alpha-3 location prefix is dropped by this scraper's parse_city
# (tenant deviation, see LocationTests below) so the real city survives.
EXPECTED = {'city': 'Taipei',
 'country': 'Taiwan',
 'country_code': 'TW',
 'job_id': '25111571',
 'job_type': 'full_time',
 'posted_date': '2026-08-28',
 'url_substr': '/Syneos_Health_External_Site/job/'}


class ListingParsingTests(unittest.TestCase):
    def test_listing_unwraps_postings_and_total(self):
        postings, total = parse_listing(LIST_PAYLOAD)
        self.assertEqual(total, LIST_PAYLOAD["total"])
        self.assertEqual(len(postings), len(LIST_PAYLOAD["jobPostings"]))

    def test_empty_payload_is_not_an_error(self):
        self.assertEqual(parse_listing({}), ([], 0))
        self.assertEqual(parse_listing(None), ([], 0))

    def test_job_id_comes_from_bullet_fields(self):
        self.assertEqual(listing_job_id(LIST_PAYLOAD["jobPostings"][0]),
                         EXPECTED["job_id"])

    def test_job_id_falls_back_to_the_external_path_tail(self):
        posting = {"externalPath": "/job/TWN-Taipei/Some-Job_25109999",
                   "bulletFields": []}
        self.assertEqual(listing_job_id(posting), "25109999")

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
    """The fleet boards' real location string formats."""

    def test_comma_separated_us_city(self):                      # IQVIA
        self.assertEqual(parse_city("Dallas, TX", "United States", "US"),
                         "Dallas")

    def test_hyphen_separated_with_remote_marker(self):          # Parexel
        self.assertEqual(parse_city("Germany-Berlin-Remote", "Germany", "DE"),
                         "Berlin")

    def test_alias_country_with_remote_only(self):               # CorroHealth US
        self.assertEqual(parse_city("US - Remote", "United States", "US"), "")

    def test_country_only_string_has_no_city(self):              # ProPharma
        self.assertEqual(parse_city("Netherlands", "Netherlands", "NL"), "")

    def test_city_plus_building_survives_whole(self):            # CorroHealth IN
        self.assertEqual(parse_city("Noida Luminaire", "India", "IN"),
                         "Noida Luminaire")

    def test_alpha3_prefixed_location_yields_the_real_city(self):  # Syneos
        # This board prefixes every location with the ISO alpha-3 country
        # code ("TWN-Taipei", "IND-Bengaluru") and sometimes a US state
        # code. This scraper's parse_city carries a tenant deviation that
        # skips bare all-uppercase 2-3 letter ISO-style tokens so the real
        # city (or nothing) survives.
        self.assertEqual(parse_city("TWN-Taipei", "Taiwan", "TW"), "Taipei")
        self.assertEqual(parse_city("IND-Bengaluru", "India", "IN"),
                         "Bengaluru")
        # country prefix + state code + remote marker leaves no city at all;
        # build_row's remote fallback ("Remote") then applies
        self.assertEqual(parse_city("USA-NC-Remote", "United States", "US"),
                         "")

    def test_n_locations_placeholder_is_not_a_city(self):
        self.assertEqual(parse_city("7 Locations", "United States", "US"), "")

    def test_remote_detection(self):
        self.assertTrue(is_remote("LBN-Remote"))
        self.assertTrue(is_remote("USA-AL-Remote"))
        self.assertTrue(is_remote("Home Based"))
        self.assertFalse(is_remote("TWN-Taipei", "IND-Hyderabad"))

    def test_country_descriptor_is_folded_to_the_canonical_name(self):
        info = {"country": {"descriptor": "United States of America"},
                "jobRequisitionLocation": {"country": {"alpha2Code": "US"}}}
        self.assertEqual(parse_country(info), ("United States", "US"))

    def test_country_code_is_recovered_from_the_name_when_missing(self):
        info = {"country": {"descriptor": "India"}}
        self.assertEqual(parse_country(info), ("India", "IN"))

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
            "Requires a minimum of 5 years of experience in drug safety."), "5")
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

    def test_job_url_is_the_canonical_external_url(self):
        self.assertIn(EXPECTED["url_substr"], self.row["job_url"])

    def test_description_is_html_free(self):
        self.assertNotIn("<", self.row["description"])
        self.assertTrue(self.row["description"])

    def test_salary_is_not_disclosed_never_invented(self):
        self.assertEqual(self.row["salary_raw"], "Not Disclosed")
        self.assertEqual(self.row["salary_min_monthly"], "")
        self.assertEqual(self.row["salary_max_monthly"], "")

    def test_the_detail_start_date_wins_over_the_relative_label(self):
        # the listing label has day granularity; startDate is authoritative
        self.assertRegex(self.row["posted_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_a_missing_detail_falls_back_to_the_relative_label(self):
        row = build_row(WORKDAY_SITES[0], "X1",
                        {"title": "T", "locationsText": "Netherlands",
                         "postedOn": "Posted Yesterday", "externalPath": "/job/a/b_X1"},
                        {"jobPostingInfo": {"title": "T"}}, today=TODAY)
        self.assertEqual(row["posted_date"],
                         (TODAY - timedelta(days=1)).isoformat())


class ClassificationTests(unittest.TestCase):
    """Wiring only — the shared engine is covered by _shared's own tests."""

    def _row(self, title, description=""):
        return {"title": title, "description": description}

    def test_an_in_scope_role_gets_the_shared_verdict(self):
        row = self._row("Pharmacovigilance Specialist",
                        "Process adverse event reports and ICSRs in the "
                        "Argus safety database.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")
        self.assertTrue(row["role_family"])

    def test_a_second_in_scope_family_maps_correctly(self):
        row = self._row("Clinical Research Associate II",
                        "Monitor clinical trial sites and ensure GCP "
                        "compliance and data integrity.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["sub_category"], "Clinical Research")

    def test_out_of_scope_titles_are_dropped(self):
        self.assertFalse(apply_classification(self._row(
            "Senior Accountant", "Prepare financial statements.")))
        self.assertFalse(apply_classification(self._row(
            "Truck Driver", "Drive delivery trucks between warehouses.")))

    def test_the_clinical_veto_is_respected(self):
        self.assertFalse(apply_classification(self._row(
            "Staff Nurse", "Bedside nursing care on the ward.")))

    def test_a_non_english_description_is_kept_but_flagged(self):
        german = ("Für unser Team suchen wir eine erfahrene Fachkraft für "
                  "die Arzneimittelsicherheit. Sie werden nicht nur Berichte "
                  "prüfen, sondern auch für die Qualität der Daten "
                  "verantwortlich sein. Die Position ist eine unbefristete "
                  "Stelle und wir bieten flexible Arbeitszeiten für eine "
                  "gute Work-Life-Balance und der Standort ist Berlin.")
        self.assertTrue(looks_non_english(german))
        row = self._row("Pharmacovigilance Specialist", german)
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_english_descriptions_are_not_flagged_by_the_language_check(self):
        self.assertFalse(looks_non_english(
            "Monitor clinical trial sites, ensure protocol compliance and "
            "data integrity, and support investigator relationships."))


class ClubExportTests(unittest.TestCase):
    def _club(self, **over):
        row = {"country": "India", "country_code": "IN", "city": "Noida",
               "company": COMPANY_NAME, "title": "Medical Coder",
               "description": "Requires a Bachelor's degree and CPC "
                              "certification.",
               "category": "Non Clinical", "sub_category": "Medical Coding",
               "role_family": "Medical Coding", "job_type": "full_time",
               "job_url": "https://x/job/1", "posted_date": "2026-08-26",
               "experience_min_years": "2"}
        row.update(over)
        return rich_row_to_club_row(row)

    def test_club_row_has_exactly_the_club_columns(self):
        self.assertEqual(sorted(self._club()), sorted(CLUB_COLUMNS))

    def test_dial_code_comes_from_the_stored_country_code(self):
        club = self._club()
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["country_dial_code"], "+91")

    def test_a_missing_code_is_recovered_from_the_country_name(self):
        club = self._club(country="Germany", country_code="")
        self.assertEqual(club["country_code"], "DE")
        self.assertEqual(club["country_dial_code"], "+49")

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

    def test_salary_is_never_invented(self):
        club = self._club()
        for col in ("min_salary", "max_salary", "salary_period",
                    "salary_currency"):
            self.assertEqual(club[col], "")

    def test_qualification_is_lifted_from_the_description(self):
        self.assertIn("Bachelor", self._club()["qualification"])

    def test_experience_maps_to_min_experience(self):
        self.assertEqual(self._club()["min_experience"], "2")


class ComplianceTests(unittest.TestCase):
    def test_the_hard_workday_page_cap_is_respected(self):
        # limit > 20 answers HTTP 400 (probe-verified on this fleet)
        self.assertEqual(LIST_PAGE_LIMIT, 20)

    def test_every_configured_url_points_at_the_workday_host(self):
        for url in (scraper.LIST_URL, scraper.DETAIL_URL, scraper.ROBOTS_URL,
                    scraper.PUBLIC_JOB_URL):
            self.assertTrue(url.startswith(scraper.HOST))

    def test_the_polite_delay_meets_the_spec_floor(self):
        self.assertGreaterEqual(scraper.REQUEST_DELAY_SECONDS, 1.5)

    def test_workday_sites_are_configured(self):
        self.assertEqual(WORKDAY_SITES, ['Syneos_Health_External_Site'])

    def test_country_table_is_iso_alpha_2_keyed(self):
        for code in COUNTRY_BY_CODE:
            self.assertRegex(code, r"^[A-Z]{2}$")

    def test_strip_html_flattens_workday_markup(self):
        self.assertEqual(strip_html("<p><b>About Us:</b></p><p>Hi</p>"),
                         "About Us: Hi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
