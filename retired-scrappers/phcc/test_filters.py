#!/usr/bin/env python3
"""Unit tests for the PHCC scraper's parsers, classifier and cutoff logic.

Every example below is real text taken from careers.phcc.gov.qa (requisitions
IRC40133, IRC40241-40244, IRC40254, IRC40274, IRC40339, IRC40463-40464,
IRC40560, IRC40565, IRC40694, IRC42618, IRC42621) unless marked as a synthetic
edge case.

Run with plain `python test_filters.py` — no pytest required.
"""

import sys
import unittest

import pandas as pd

import scraper


class TestCleanTitle(unittest.TestCase):
    """Most PHCC titles are already clean; a few carry the raw HR position
    path and must be unpacked AND flagged."""

    def test_normal_titles_pass_through_unflagged(self):
        for raw in ["Specialist Emergency Medicine", "Consultant Paediatrician",
                    "Midwife", "Nurse", "Lab Technologist",
                    "Director of Pharmacy", "Radiology Technologist",
                    "Clinical Coding Officer"]:
            title, review = scraper.clean_title(raw, "Operations")
            self.assertEqual(title, raw)
            self.assertFalse(review, raw)

    def test_coded_title_is_unpacked_and_flagged(self):
        # IRC40694 — real requisition
        title, review = scraper.clean_title(
            "063.Operations.Allied Health.Technologist", "080000000.Operations")
        self.assertEqual(title, "Allied Health Technologist")
        self.assertTrue(review)

    def test_coded_title_without_matching_org_keeps_all_words(self):
        title, review = scraper.clean_title("101.Nursing.Staff Nurse", "Medicine")
        self.assertEqual(title, "Nursing Staff Nurse")
        self.assertTrue(review)

    def test_unpacking_never_returns_empty(self):
        # synthetic: every segment is stripped -> fall back to the raw title
        title, review = scraper.clean_title("063.Operations", "080000000.Operations")
        self.assertEqual(title, "063.Operations")
        self.assertTrue(review)

    def test_whitespace_is_collapsed(self):
        title, _ = scraper.clean_title("  Consultant   Family  Medicine ", "")
        self.assertEqual(title, "Consultant Family Medicine")


class TestCleanOrganization(unittest.TestCase):
    def test_cost_centre_code_is_stripped(self):
        self.assertEqual(scraper.clean_organization("080000000.Operations"),
                         "Operations")

    def test_plain_names_pass_through(self):
        self.assertEqual(scraper.clean_organization("Operations"), "Operations")
        self.assertEqual(
            scraper.clean_organization("Business & Health Intelligence"),
            "Business & Health Intelligence")

    def test_empty(self):
        self.assertEqual(scraper.clean_organization(""), "")
        self.assertEqual(scraper.clean_organization(None), "")


class TestParsePostedDate(unittest.TestCase):
    """iRecruitment renders dd-Mon-yyyy."""

    def test_real_listing_dates(self):
        self.assertEqual(scraper.parse_posted_date("23-Jul-2026"), "2026-07-23")
        self.assertEqual(scraper.parse_posted_date("24-Feb-2026"), "2026-02-24")
        self.assertEqual(scraper.parse_posted_date("02-Feb-2026"), "2026-02-02")
        self.assertEqual(scraper.parse_posted_date("11-Jan-2026"), "2026-01-11")
        self.assertEqual(scraper.parse_posted_date("17-Dec-2025"), "2025-12-17")

    def test_unparseable_returns_empty_rather_than_guessing(self):
        self.assertEqual(scraper.parse_posted_date("last week"), "")
        self.assertEqual(scraper.parse_posted_date(""), "")
        self.assertEqual(scraper.parse_posted_date(None), "")


class TestParseCity(unittest.TestCase):
    def test_city_country_pair(self):
        self.assertEqual(scraper.parse_city("Doha,QA"), "Doha")
        self.assertEqual(scraper.parse_city("Al Rayyan, QA"), "Al Rayyan")

    def test_bare_country_falls_back_to_default(self):
        # IRC40339 really is listed as just "QA"
        self.assertEqual(scraper.parse_city("QA"), scraper.DEFAULT_CITY)
        self.assertEqual(scraper.parse_city("Qatar"), scraper.DEFAULT_CITY)
        self.assertEqual(scraper.parse_city(""), scraper.DEFAULT_CITY)


class TestMapJobType(unittest.TestCase):
    def test_observed_value(self):
        self.assertEqual(scraper.map_job_type("Full Time"), "full_time")

    def test_other_statuses(self):
        self.assertEqual(scraper.map_job_type("Part Time"), "part_time")
        self.assertEqual(scraper.map_job_type("Contractor"), "contract")
        self.assertEqual(scraper.map_job_type("Temporary"), "contract")
        self.assertEqual(scraper.map_job_type("Internship"), "internship")

    def test_blank_defaults_to_full_time(self):
        self.assertEqual(scraper.map_job_type(""), "full_time")


class TestExperienceParser(unittest.TestCase):
    """Worked examples from real PHCC "Job Requirements" sections."""

    def test_at_least_n_years(self):
        # IRC42618 Midwife
        text = ("At least 5 years’ experience working within a Maternity "
                "setting (experience in all maternity settings are desirable, "
                "however Antenatal and postnatal care experience is essential), "
                "Neonatal/Pediatric experience is an added advantage.")
        self.assertEqual(scraper.parse_min_experience_years(text), "5")

    def test_minimum_n_years(self):
        # IRC40254 Lab Technologist
        text = ("Bachelor of Science in Laboratory Technology • DHP license • "
                "CPR course certificate • Minimum 2 years of experience")
        self.assertEqual(scraper.parse_min_experience_years(text), "2")

    def test_lowest_requirement_wins_when_tiered(self):
        text = ("Minimum 6 years of experience for Consultant grade; "
                "at least 3 years experience for Specialist grade.")
        self.assertEqual(scraper.parse_min_experience_years(text), "3")

    def test_not_less_than_phrasing(self):
        self.assertEqual(
            scraper.parse_min_experience_years(
                "Not less than 4 years post-qualification experience."),
            "4")

    def test_range_takes_the_floor(self):
        self.assertEqual(
            scraper.parse_min_experience_years("Minimum of 3-5 years experience"),
            "3")

    def test_no_requirement_returns_empty(self):
        # IRC40133 Director of Pharmacy has no description at all
        self.assertEqual(scraper.parse_min_experience_years(""), "")
        self.assertEqual(
            scraper.parse_min_experience_years(
                "Bachelor’s degree in nursing or equivalent as recognized by "
                "MOPH Qatar."),
            "")

    def test_training_requirement_is_not_mistaken_for_experience(self):
        # IRC40694 Allied Health Technologist — the 1 year is internship
        text = ("Bachelor of Science in Physiotherapy or equivalent. • "
                "Licensed to practice Physiotherapy at point of hire • "
                "DHP License • CPR course certificate. • Minimum 1 year of "
                "internship/training in a hospital or equivalent. • "
                "Minimum 3 years of experience in physiotherapy")
        self.assertEqual(scraper.parse_min_experience_years(text), "3")

    def test_other_training_spans_are_discarded_too(self):
        for text in ["Minimum 2 years of residency training required.",
                     "At least 1 year probation applies.",
                     "Minimum 1 year of fellowship. Minimum 4 years experience."]:
            years = scraper.parse_min_experience_years(text)
            self.assertNotEqual(years, "1", text)
            self.assertNotEqual(years, "2", text)

    def test_unrelated_numbers_are_ignored(self):
        # "2 year contract" is a term, not a requirement; "25 years of age"
        # is neither
        self.assertEqual(
            scraper.parse_min_experience_years(
                "This is a 2 year contract. Applicants must be over 25 years "
                "of age."),
            "")

    def test_implausible_years_rejected(self):
        self.assertEqual(
            scraper.parse_min_experience_years("Minimum 99 years experience"),
            "")


class TestClassificationWiring(unittest.TestCase):
    """Every keep/drop and label decision belongs to the shared two-level
    classifier (scrappers/_shared/classification.py). The ProfessionalArea
    facet is only a `skills` signal now."""

    @staticmethod
    def _row(title, area="", description="", requirements="", needs_review=False):
        return {"title": title, "category_original": area,
                "description": description, "job_requirements": requirements,
                "needs_review": needs_review}

    def test_in_scope_role_is_stamped(self):
        row = self._row("Medical Coding Officer",
                        "Health Information Management(HIM)")
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")
        self.assertEqual(row["role_family"], "Medical Coding")
        self.assertTrue(row["matched_in"])

    def test_public_health_role_is_stamped(self):
        row = self._row("Infection Control Practitioner",
                        "Other Allied Health Services")
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_bedside_roles_are_dropped(self):
        for title, area in [("Midwife", "Nursing"),
                            ("Specialist Paediatrician", "Physicians"),
                            ("Director of Pharmacy", "Pharmacy"),
                            ("Radiology Technologist", "Radiology")]:
            row = self._row(title, area)
            self.assertFalse(scraper.apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_facet_alone_cannot_admit_a_bedside_role(self):
        # the facet is a signal, never the verdict
        row = self._row("Staff Nurse", "Health Information Management(HIM)")
        self.assertFalse(scraper.apply_classification(row))

    def test_requirements_feed_the_description_signal(self):
        row = self._row(
            "Officer", "Governance",
            requirements="Maintain the trial master file and manage "
                         "clinical trial documentation to ICH-GCP.")
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")

    def test_hr_code_title_flag_survives_classification(self):
        row = self._row("Clinical Research Coordinator", "Governance",
                        needs_review=True)
        self.assertTrue(scraper.apply_classification(row))
        self.assertTrue(row["needs_review"])


class TestCutoff(unittest.TestCase):
    def test_first_run_keeps_everything(self):
        # INITIAL_WINDOW_DAYS is None: an ATS lists only open vacancies
        self.assertIsNone(scraper.compute_cutoff(None))
        self.assertIsNone(scraper.compute_cutoff(pd.DataFrame()))

    def test_watermark_is_newest_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-01-11", "2026-07-23",
                                           "2025-12-17"]})
        # newest 2026-07-23 minus WATERMARK_GRACE_DAYS (2)
        self.assertEqual(scraper.compute_cutoff(df), "2026-07-21")

    def test_unparseable_dates_are_ignored_by_the_watermark(self):
        df = pd.DataFrame({"posted_date": ["", "not a date", "2026-02-24"]})
        self.assertEqual(scraper.compute_cutoff(df), "2026-02-22")

    def test_all_dates_unparseable_falls_back_to_no_cutoff(self):
        df = pd.DataFrame({"posted_date": ["", "not a date"]})
        self.assertIsNone(scraper.compute_cutoff(df))

    def test_within_window(self):
        self.assertTrue(scraper.within_window("2026-07-23", "2026-07-21"))
        self.assertTrue(scraper.within_window("2026-07-21", "2026-07-21"))
        self.assertFalse(scraper.within_window("2026-07-20", "2026-07-21"))

    def test_no_cutoff_keeps_everything(self):
        self.assertTrue(scraper.within_window("2020-01-01", None))

    def test_undated_vacancy_is_kept_not_dropped(self):
        self.assertTrue(scraper.within_window("", "2026-07-21"))


class TestHtmlHelpers(unittest.TestCase):
    def test_bullets_are_preserved_as_separators(self):
        html = "<ul><li>First duty</li><li>Second duty</li></ul>"
        self.assertEqual(scraper.strip_html(html), "First duty • Second duty")

    def test_entities_are_decoded(self):
        self.assertEqual(scraper.strip_html("Business &amp; Health"),
                         "Business & Health")

    def test_empty_markup_yields_empty_string(self):
        self.assertEqual(scraper.strip_html("<div><br /></div>"), "")
        self.assertEqual(scraper.strip_html(""), "")


class TestResultTableParsing(unittest.TestCase):
    """A trimmed-down copy of the real OAF result markup."""

    PAGE = (
        '<span id="JobSearchTable:region1:0">'
        '<a id="JobSearchTable:JobName:0">IRC42618</a></span>'
        '<input type="hidden" id=JobSearchTable:hiddenUrlVACVWPP:0 '
        'value=/OA_HTML/OA.jsp?region=/oracle/apps/irc/popup/webui/'
        'VacDetailsPPRN&ppVacancyId=42618&isPopupRegion=Y>'
        '<span id="JobSearchTable:JobTitle:0">Midwife</span>'
        '<span id="JobSearchTable:OrganizationName:0">Operations</span>'
        '<span id="JobSearchTable:ProfessionalArea:0">Nursing</span>'
        '<span id="JobSearchTable:LocationResult:0">Doha,QA</span>'
        '<span id="JobSearchTable:DatePostedResult:0">24-Feb-2026</span>'
        '<span id="JobSearchTable:FullTime:0">Full Time</span>'
        '<option selected value="1,10">1-10 of 15</option>'
        '<option value="11,5">11-15 of 15</option>'
    )

    def test_row_fields(self):
        rows = scraper.parse_result_rows(self.PAGE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            "job_id": "IRC42618",
            "vacancy_id": "42618",
            "raw_title": "Midwife",
            "organization": "Operations",
            "professional_area": "Nursing",
            "location_raw": "Doha,QA",
            "posted_raw": "24-Feb-2026",
            "employment_status": "Full Time",
        })

    def test_total_from_record_set_navigator(self):
        self.assertEqual(scraper.parse_total(self.PAGE), 15)

    def test_total_absent_when_there_is_no_navigator(self):
        self.assertEqual(scraper.parse_total("<html></html>"), 0)

    def test_end_to_end_row_build(self):
        row = scraper.listing_to_rich_row(scraper.parse_result_rows(self.PAGE)[0])
        self.assertEqual(row["title"], "Midwife")
        # taxonomy fields are left blank here; apply_classification() stamps them
        self.assertEqual(row["category"], "")
        self.assertEqual(row["category_original"], "Nursing")
        self.assertEqual(row["city"], "Doha")
        self.assertEqual(row["country"], "Qatar")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["posted_date"], "2026-02-24")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["salary_min_monthly"], "")
        self.assertFalse(row["needs_review"])
        self.assertEqual(
            row["job_url"],
            "https://careers.phcc.gov.qa/OA_HTML/OA.jsp"
            "?OAFunc=IRC_VIS_VAC_DISPLAY&p_svid=42618&p_spid=0")


class TestDetailParsing(unittest.TestCase):
    """Structure copied from a real IRC_VIS_VAC_DISPLAY page."""

    PAGE = (
        '<tr id="DepartmentDescription__xc_"><td><span class="OraPromptText">'
        'Department Description</span></td>'
        '<td><span id="DepartmentDescription" class="xhd"></span></td></tr>'
        '<tr><td></td><td align="left"></td></tr>'
        '<tr id="IrcBriefDescription__xc_">'
        '<td><span id="IrcBriefDescription" class="xhd"></span></td></tr>'
        '<tr><td align="left"><div>Midwives focuses on a well model.</div></td></tr>'
        '<tr id="DetailedDescription__xc_">'
        '<td><span id="DetailedDescription" class="xhd"></span></td></tr>'
        '<tr><td align="left"><ul><li>Provides midwifery care.</li>'
        '<li>Provides postnatal care.</li></ul></td></tr>'
        '<tr id="JobRequirements__xc_">'
        '<td><span id="JobRequirements" class="xhd"></span></td></tr>'
        '<tr><td align="left"><ul><li>Bachelor’s degree in Midwifery.</li>'
        '<li>At least 5 years’ experience in a Maternity setting.</li>'
        '</ul></td></tr>'
        '<tr id="AdditionalDetails__xc_">'
        '<td><span id="AdditionalDetails" class="xhd"></span></td></tr>'
        '<tr><td align="left"></td></tr>'
        '<span id="OASH__1034834">Skill</span>'
    )

    def test_sections_are_split_correctly(self):
        sections = scraper.parse_detail_sections(self.PAGE)
        self.assertEqual(sections["IrcBriefDescription"],
                         "Midwives focuses on a well model.")
        self.assertEqual(sections["DetailedDescription"],
                         "Provides midwifery care. • Provides postnatal care.")
        self.assertIn("Bachelor", sections["JobRequirements"])
        # blank sections are omitted, not stored as empty strings
        self.assertNotIn("DepartmentDescription", sections)
        self.assertNotIn("AdditionalDetails", sections)

    def test_skills_table_is_not_swallowed_into_the_last_section(self):
        sections = scraper.parse_detail_sections(self.PAGE)
        self.assertNotIn("Skill", " ".join(sections.values()))

    def test_description_and_experience_are_applied_to_the_row(self):
        row = scraper.listing_to_rich_row(
            scraper.parse_result_rows(TestResultTableParsing.PAGE)[0])
        row = scraper.apply_detail(row, scraper.parse_detail_sections(self.PAGE))
        self.assertIn("Midwives focuses on a well model.", row["description"])
        self.assertIn("Requirements: Bachelor", row["description"])
        self.assertEqual(row["experience_min_years"], "5")

    def test_vacancy_with_no_description_stays_empty(self):
        # IRC40133 / IRC42621 / IRC40565 really do have all five sections blank
        row = scraper.listing_to_rich_row(
            scraper.parse_result_rows(TestResultTableParsing.PAGE)[0])
        row = scraper.apply_detail(row, {})
        self.assertEqual(row["description"], "")
        self.assertEqual(row["experience_min_years"], "")


class TestClubRow(unittest.TestCase):
    @staticmethod
    def _in_scope_row():
        row = scraper.listing_to_rich_row(
            scraper.parse_result_rows(TestResultTableParsing.PAGE)[0])
        row["title"] = "Pharmacovigilance Officer"
        row["description"] = "Case processing. Requirements: MPH preferred."
        scraper.apply_classification(row)
        return row

    def test_club_schema_and_values(self):
        club = scraper.rich_row_to_club_row(self._in_scope_row())
        self.assertEqual(list(club), scraper.CLUB_COLUMNS)
        self.assertEqual(club["country_name"], "Qatar")
        self.assertEqual(club["country_code"], "QA")
        self.assertEqual(club["country_dial_code"], "+974")
        self.assertEqual(club["city_name"], "Doha")
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Pharmacovigilance")
        self.assertIn("qualification", club)
        # retired fleet-wide
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)

    def test_salary_columns_stay_blank(self):
        row = scraper.listing_to_rich_row(
            scraper.parse_result_rows(TestResultTableParsing.PAGE)[0])
        club = scraper.rich_row_to_club_row(row)
        for field in ("min_salary", "max_salary", "salary_period",
                      "salary_currency"):
            self.assertEqual(club[field], "", field)

    def test_nan_values_do_not_leak_into_the_club_csv(self):
        # pandas turns empty CSV cells into NaN on reload
        row = scraper.listing_to_rich_row(
            scraper.parse_result_rows(TestResultTableParsing.PAGE)[0])
        row["experience_min_years"] = float("nan")
        row["description"] = float("nan")
        club = scraper.rich_row_to_club_row(row)
        self.assertEqual(club["min_experience"], "")
        self.assertEqual(club["description"], "")


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0], "-v"], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
