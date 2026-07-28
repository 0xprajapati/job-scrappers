#!/usr/bin/env python3
"""Offline unit tests for the DHA (Sheryan opportunities) scraper parsers.

Every example is taken from a real posting on
services.dha.gov.ae/sheryan/wps/portal/home/opportunities.

Run with plain:  python test_filters.py
"""

import unittest

import scraper


class TestTextHelpers(unittest.TestCase):
    def test_recase_shouty_title(self):
        self.assertEqual(scraper.recase("GYNECOLOGIST"), "Gynecologist")
        self.assertEqual(scraper.recase("DENTAL ASSISTANT"), "Dental Assistant")

    def test_recase_keeps_acronyms(self):
        self.assertEqual(scraper.recase("GP DOCTOR"), "GP Doctor")
        self.assertEqual(scraper.recase("ICU NURSE"), "ICU Nurse")

    def test_recase_leaves_mixed_case_alone(self):
        self.assertEqual(scraper.recase("Registered Nurse"), "Registered Nurse")
        self.assertEqual(scraper.recase("General Practitioner"),
                         "General Practitioner")

    def test_clean_facility_strips_entity_suffixes(self):
        self.assertEqual(
            scraper.clean_facility("BELLA ROMA SPECIALTY HOSPITAL L.L.C"),
            "Bella Roma Specialty Hospital")
        self.assertEqual(
            scraper.clean_facility("Camali Clinic Child and Adult Mental Health FZ-LLC"),
            "Camali Clinic Child and Adult Mental Health")
        self.assertEqual(scraper.clean_facility("AIMS HEALTH CARE LLC"),
                         "Aims Health Care")
        self.assertEqual(scraper.clean_facility(" ZAIN CURA MEDICAL CENTER L L C "),
                         "Zain Cura Medical Center")

    def test_clean_facility_keeps_plain_names(self):
        self.assertEqual(scraper.clean_facility("Canadian Specialist Hospital"),
                         "Canadian Specialist Hospital")


class TestCategoryClassifier(unittest.TestCase):
    def test_portal_category_maps_to_club_enum(self):
        self.assertEqual(scraper.classify_category("Registered Nurse",
                                                   "Nurse and Midwife", "Medical"),
                         ("nurses", False))
        self.assertEqual(scraper.classify_category("General Practitioner",
                                                   "Physician", "Medical"),
                         ("doctors", False))
        self.assertEqual(scraper.classify_category("Orthodontist",
                                                   "Dentist", "Medical"),
                         ("doctors", False))

    def test_allied_health_lands_in_non_clinical(self):
        # No club bucket exists for allied health (export_club_csv.py note).
        self.assertEqual(scraper.classify_category("Physiotherapy Technician",
                                                   "Allied Health", "Medical"),
                         ("non_clinical", False))

    def test_tcm_follows_the_ayush_convention(self):
        self.assertEqual(
            scraper.classify_category(
                "Homeopathy Practitioner",
                "Traditional And Complementary Medicine (T&CM)", "Medical"),
            ("doctors", False))

    def test_pharmacist_title_beats_portal_category(self):
        # The portal files pharmacists under "Allied Health"; the title must win
        # so the pharmacists bucket is not lost.
        self.assertEqual(scraper.classify_category("Pharmacist",
                                                   "Allied Health", "Medical"),
                         ("pharmacists", False))

    def test_portal_category_beats_the_title_regex(self):
        # Real postings: allied-health roles whose titles read clinical must
        # follow the regulator's own bucket, not the title.
        for title in ("Clinical Psychologist",
                      "Speech and Language Pathologist - Therapist",
                      "Chiropractor - Part Time",
                      "DHA licenced Massage Specialist"):
            self.assertEqual(
                scraper.classify_category(title, "Allied Health", "Medical"),
                ("non_clinical", False), title)

    def test_title_decides_when_the_portal_is_silent(self):
        self.assertEqual(scraper.classify_category("Medical Officer", "", ""),
                         ("doctors", False))
        self.assertEqual(scraper.classify_category("Staff Nurse", "", ""),
                         ("nurses", False))

    def test_admin_vacancy_without_category_is_non_clinical(self):
        self.assertEqual(scraper.classify_category("Sales Executive", "", "Admin"),
                         ("non_clinical", False))
        self.assertEqual(scraper.classify_category("Receptionist", None, "Admin"),
                         ("non_clinical", False))

    def test_unmappable_job_is_kept_and_flagged(self):
        category, needs_review = scraper.classify_category("Team Member", "", "")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)          # kept, never dropped (spec §2)

    def test_company_type_enum(self):
        self.assertEqual(scraper.classify_company_type("Life Pharmacy"), "pharma")
        self.assertEqual(scraper.classify_company_type("Unilabs Diagnostics"),
                         "pharma")
        self.assertEqual(scraper.classify_company_type("Canadian Specialist Hospital"),
                         "hospital")


class TestCityAndJobType(unittest.TestCase):
    def test_city_defaults_to_dubai(self):
        self.assertEqual(scraper.detect_city("Registered Nurse for our homecare"),
                         "Dubai")

    def test_city_reads_the_stated_emirate(self):
        self.assertEqual(
            scraper.detect_city("We are looking GP for our Primary Health Care "
                                "in Abu Dhabi"), "Abu Dhabi")
        self.assertEqual(scraper.detect_city("Dental clinic in Sharjah"), "Sharjah")

    def test_dubai_wins_when_both_are_named(self):
        self.assertEqual(
            scraper.detect_city("Poly clinic in Al Satwa, Dubai; Sharjah branch soon"),
            "Dubai")

    def test_job_type_full_time_by_default(self):
        self.assertEqual(scraper.detect_job_type("Looking for Registered Nurse"),
                         "full_time")

    def test_part_time_only_when_unambiguous(self):
        self.assertEqual(scraper.detect_job_type("Part time dental assistant"),
                         "part_time")
        # Real posting OPP-2025-00000010 offers either -> keep the wider offer.
        self.assertEqual(
            scraper.detect_job_type("PART TIME OR FULL TIME, OBGY USG, COSMETIC GYNEC"),
            "full_time")

    def test_remote_posting(self):
        self.assertEqual(
            scraper.detect_job_type("Telehealth physician, work from home"),
            "remote")


class TestExperienceParser(unittest.TestCase):
    def test_at_least_n_years(self):
        # Real posting OPP-2024-00000001.
        self.assertEqual(
            scraper.parse_experience("Female or male with at least 4 year experience"),
            ("4", "4"))

    def test_range(self):
        self.assertEqual(scraper.parse_experience("3-5 years experience required"),
                         ("3", "5"))
        self.assertEqual(scraper.parse_experience("2 to 4 years experience"),
                         ("2", "4"))

    def test_open_ended(self):
        self.assertEqual(scraper.parse_experience("5+ years experience in ICU"),
                         ("5", ""))

    def test_experience_first_phrasing(self):
        self.assertEqual(scraper.parse_experience("Experience of 2 years in UAE"),
                         ("2", "2"))

    def test_reversed_range_is_ordered(self):
        self.assertEqual(scraper.parse_experience("experience 7 to 3 years"),
                         ("3", "7"))

    def test_no_experience_statement(self):
        self.assertEqual(scraper.parse_experience("Immediate joiners preferred"),
                         ("", ""))
        # A bare number that is not about experience must not be picked up.
        self.assertEqual(scraper.parse_experience("Clinic open 12 years in Dubai"),
                         ("", ""))


class TestDetailParsing(unittest.TestCase):
    # Trimmed copy of a real viewOpportunityDetails page.
    PAGE = (
        "<script>\n"
        "var description = 'Looking for Registered Nurse for our homecare "
        "facility.\\nCandidate willing to join immediately preferred';\n"
        "var jobRequirement = '[DHA licence or eligibility letter, "
        "2 years experience]';\n"
        "var jobID ='OPP-2026-00000037';\n"
        "</script>\n"
        '<span class="text-neutral-700 fs-sm-2 text-decoration-none number-ar-ltr"'
        ' >+971504241497</span>\n'
        '<span class="text-neutral-700 fs-sm-2 text-decoration-none number-ar-ltr">'
        '+97143697770</span>\n'
        '<a class="text-neutral-700" href="mailto:hr@ych.ae">hr@ych.ae</a>\n'
    )

    def test_parses_all_detail_fields(self):
        detail = scraper.parse_detail_html(self.PAGE)
        self.assertEqual(
            detail["description"],
            "Looking for Registered Nurse for our homecare facility. "
            "Candidate willing to join immediately preferred")
        self.assertEqual(detail["requirements"],
                         "DHA licence or eligibility letter; 2 years experience")
        self.assertEqual(detail["contact_email"], "hr@ych.ae")
        self.assertEqual(detail["contact_phones"], "+971504241497; +97143697770")

    def test_missing_fields_degrade_quietly(self):
        self.assertEqual(scraper.parse_detail_html("<html>nothing here</html>"), {})
        self.assertEqual(scraper.parse_detail_html(""), {})

    def test_js_escapes_are_decoded(self):
        page = "var description = 'Dr\\'s clinic\\nAl Barsha, Dubai';"
        self.assertEqual(scraper.parse_detail_html(page)["description"],
                         "Dr's clinic Al Barsha, Dubai")


class TestRowBuilding(unittest.TestCase):
    OPPORTUNITY = {
        "jobTitle": "Registered Nurse",
        "jobID": "OPP-2026-00000037",
        "vacancyType": "Medical",
        "category": "Nurse and Midwife",
        "facilityID": "3219478",
        "facilityName": "YOUR CHOICE HEALTHCARE L.L.C",
        "national": "All Nationalities",
        "interested": "393",
    }

    def build(self):
        return scraper.opportunity_to_rich_row(
            self.OPPORTUNITY, "https://services.dha.gov.ae/job", "2026-07-27")

    def test_listing_row(self):
        row = self.build()
        self.assertEqual(row["job_id"], "OPP-2026-00000037")
        self.assertEqual(row["company"], "Your Choice Healthcare")
        self.assertEqual(row["category"], "nurses")
        self.assertFalse(row["needs_review"])
        self.assertEqual(row["first_seen_date"], "2026-07-27")
        self.assertEqual(row["posted_date"], "")     # never published by DHA

    def test_salary_is_never_invented(self):
        row = self.build()
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["salary_min_monthly"], "")
        self.assertEqual(row["salary_max_monthly"], "")

    def test_detail_enriches_city_type_and_experience(self):
        row = scraper.apply_detail(self.build(), {
            "description": "Part time nurse for our Sharjah branch, "
                           "at least 3 years experience",
            "requirements": "DHA licence",
        })
        self.assertEqual(row["city"], "Sharjah")
        self.assertEqual(row["job_type"], "part_time")
        self.assertEqual(row["experience_min_years"], "3")

    def test_club_row_matches_the_contract(self):
        club = scraper.rich_row_to_club_row(self.build())
        self.assertEqual(sorted(club), sorted(scraper.CLUB_COLUMNS))
        self.assertEqual(club["country_code"], "AE")
        self.assertEqual(club["category"], "nurses")
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(club["job_type"], "full_time")
        self.assertIn(club["salary_currency"], ("", "INR", "USD"))
        self.assertEqual(club["min_salary"], "")
        # posted_at falls back to first seen, since DHA publishes no date.
        self.assertEqual(club["posted_at"], "2026-07-27")


class TestDetailUrl(unittest.TestCase):
    TEMPLATE = ("p0/IZ7_A=CZ6_B=MEjobId!_jobId_=locale!_locale_="
                "action!viewOpportunityDetails==/")

    def test_builds_public_detail_url(self):
        url = scraper.build_detail_url("https://services.dha.gov.ae/x/",
                                       self.TEMPLATE, "OPP-2026-00000037")
        self.assertIn("MEjobId!OPP-2026-00000037=locale!en=", url)
        self.assertTrue(url.endswith("?jobId=OPP-2026-00000037&locale=en"))

    def test_falls_back_to_the_board_url(self):
        self.assertEqual(scraper.build_detail_url("https://x/", "", "OPP-1"),
                         scraper.LANDING_URL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
