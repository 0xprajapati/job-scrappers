#!/usr/bin/env python3
"""Unit tests for the seha.ae scraper's parsers, classifier and cutoff logic.

Every example below is copied from a real SEHA requisition on the Oracle
candidate portal. Run with plain `python test_filters.py`.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    apply_classification,
    build_description,
    compute_cutoff,
    expand_facility,
    map_job_type,
    parse_city,
    parse_min_experience_years,
    rich_row_to_club_row,
    split_title,
    strip_html,
    within_window,
    CLUB_COLUMNS,
    DEFAULT_CITY,
    COMPANY_NAME,
)


class TestSplitTitle(unittest.TestCase):
    """A trailing parenthetical is a facility OR a role qualifier — only the
    facility is peeled off."""

    def test_facility_parenthetical_is_extracted(self):
        self.assertEqual(split_title("Sonographer (Tawam Fertility Center)"),
                         ("Sonographer", "Tawam Fertility Center"))
        self.assertEqual(split_title("Staff Midwife (Corniche Hospital)"),
                         ("Staff Midwife", "Corniche Hospital"))
        self.assertEqual(split_title("Consultant Dermatology (STMC)"),
                         ("Consultant Dermatology",
                          "Sheikh Tahnoon Bin Mohammad Medical City"))
        self.assertEqual(split_title("Neuro Physiologist (SKMC)"),
                         ("Neuro Physiologist", "Sheikh Khalifa Medical City"))
        self.assertEqual(
            split_title("Specialist Physician ENT (Al Dhafra Region)"),
            ("Specialist Physician ENT", "Al Dhafra Region"))

    def test_qualifier_parenthetical_is_kept_in_title(self):
        self.assertEqual(split_title("Embryologist (IVF)"),
                         ("Embryologist (IVF)", ""))
        self.assertEqual(split_title("Consultant Neurology (Arabic Speaker)"),
                         ("Consultant Neurology (Arabic Speaker)", ""))
        self.assertEqual(split_title("Practical Nurse (Radiology)"),
                         ("Practical Nurse (Radiology)", ""))
        self.assertEqual(
            split_title("Obstetrician and Gynecologist Consultant (High Risk)"),
            ("Obstetrician and Gynecologist Consultant (High Risk)", ""))

    def test_facility_followed_by_trailing_qualifier(self):
        # "(SKMC )" carries a stray space, and "- UAE National" trails it
        self.assertEqual(
            split_title("Pharmacist - Inpatient Pharmacy (SKMC ) - UAE National"),
            ("Pharmacist - Inpatient Pharmacy - UAE National",
             "Sheikh Khalifa Medical City"))

    def test_acronym_tail_collapses_to_one_canonical_name(self):
        # HR work-location strings repeat the acronym after the full name;
        # both spellings must land on the same club company_name
        self.assertEqual(expand_facility("SKMC"), "Sheikh Khalifa Medical City")
        self.assertEqual(expand_facility("Sheikh Khalifa Medical City - SKMC"),
                         "Sheikh Khalifa Medical City")
        self.assertEqual(expand_facility("AL Corniche - ACH"),
                         "Al Corniche Hospital")
        self.assertEqual(expand_facility("Tawam - TWM"), "Tawam Hospital")

    def test_unknown_facility_passes_through(self):
        self.assertEqual(expand_facility("Al Rahba Hospital"), "Al Rahba Hospital")
        self.assertEqual(expand_facility("Neima healthcare center - XYZ"),
                         "Neima healthcare center - XYZ")

    def test_no_parenthetical(self):
        self.assertEqual(split_title("Staff Nurse - ICU"),
                         ("Staff Nurse - ICU", ""))
        self.assertEqual(split_title("Consultant Physician"),
                         ("Consultant Physician", ""))

    def test_hyphen_typo_is_normalised(self):
        self.assertEqual(
            split_title("Specialist Physician- Cardiology (SEHA Clinics)"),
            ("Specialist Physician - Cardiology", "SEHA Clinics"))


class TestExperienceParser(unittest.TestCase):
    """SEHA states requirements in free text; the LOWEST stated bar wins."""

    def test_seha_house_style_nlt(self):
        self.assertEqual(parse_min_experience_years(
            "NLT 2 years post graduate experience in ultrasound in an acute "
            "care hospital"), "2")

    def test_tiered_requirements_take_the_minimum(self):
        self.assertEqual(parse_min_experience_years(
            "Tier 2: NLT 2 years of clinical experience post qualification. "
            "Tier 3: NLT 3 years clinical experience as Specialist post "
            "qualification."), "2")
        self.assertEqual(parse_min_experience_years(
            "Tier 1: NLT 2 years relevant experience as a consultant or "
            "Tier 2: NLT 8 years full-time employment as a Consultant"), "2")

    def test_minimum_of_phrasing(self):
        self.assertEqual(parse_min_experience_years(
            "Fellowship or additional certification in Pediatric Orthopedic "
            "Minimum of 5 years of post-specialization experience"), "5")

    def test_at_least_and_plus_phrasings(self):
        self.assertEqual(parse_min_experience_years(
            "at least 3 years of relevant experience"), "3")
        self.assertEqual(parse_min_experience_years(
            "7+ years experience in a tertiary hospital"), "7")
        self.assertEqual(parse_min_experience_years(
            "3 - 5 years of experience required"), "3")

    def test_no_requirement_returns_blank(self):
        self.assertEqual(parse_min_experience_years(
            "Responsible to independently perform all sonography imagery "
            "procedures and report to supervisor as needed."), "")
        self.assertEqual(parse_min_experience_years(""), "")
        self.assertEqual(parse_min_experience_years(None), "")

    def test_unrelated_year_numbers_are_ignored(self):
        # neither a requirement cue nor adjacent to "experience"
        self.assertEqual(parse_min_experience_years(
            "The hospital has served the community for 25 years."), "")
        self.assertEqual(parse_min_experience_years(
            "Renewable 2 year contract with annual leave."), "")

    def test_implausible_values_are_dropped(self):
        self.assertEqual(parse_min_experience_years(
            "minimum 99 years of experience"), "")


class TestClassificationWiring(unittest.TestCase):
    """The engine itself is tested in _shared/test_classification.py; these
    only prove this scraper wires its fields into it correctly."""

    def _row(self, title, category_original="", job_function="",
             description=""):
        return {"title": title, "category_original": category_original,
                "job_function": job_function, "description": description}

    def test_in_scope_role_gets_category_and_sub_category(self):
        row = self._row("Clinical Research Coordinator", "Administration")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")
        self.assertTrue(row["matched_in"])

    def test_public_health_role(self):
        row = self._row("Infection Control Practitioner", "Nursing")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_clinical_titles_are_dropped(self):
        for title, facet in (("Specialist Physician - Cardiology", "Medical"),
                             ("Staff Midwife", "Nursing"),
                             ("Charge Nurse - ICU", "Nursing"),
                             ("Sonographer", "Allied Health"),
                             ("Pharmacist - Inpatient Pharmacy", "Pharmacy")):
            row = self._row(title, facet)
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_hospital_corporate_role_without_a_taxonomy_family_is_dropped(self):
        # generic head-office roles are not in the two-level taxonomy
        row = self._row("Manager - Performance & Business Excellence",
                        "Administration")
        self.assertFalse(apply_classification(row))

    def test_oracle_facet_cannot_admit_a_job(self):
        # the facet is a signal for the shared classifier, never a decider
        row = self._row("Cast Technician", "Administration", "Administration")
        self.assertFalse(apply_classification(row))


class TestJobTypeAndCity(unittest.TestCase):
    def test_job_type(self):
        self.assertEqual(map_job_type("Full time"), "full_time")
        self.assertEqual(map_job_type("Part time"), "part_time")
        self.assertEqual(map_job_type(None), "full_time")

    def test_city(self):
        self.assertEqual(parse_city("Al Ain, United Arab Emirates"), "Al Ain")
        self.assertEqual(parse_city("Abu Dhabi, United Arab Emirates"), "Abu Dhabi")
        self.assertEqual(parse_city("Al Dhafra, United Arab Emirates"), "Al Dhafra")
        # some requisitions carry only the country
        self.assertEqual(parse_city("United Arab Emirates"), DEFAULT_CITY)
        self.assertEqual(parse_city(""), DEFAULT_CITY)


class TestDescription(unittest.TestCase):
    def test_list_items_are_separated(self):
        html = "<ul><li>Maintaining inventory</li><li>Reporting shortages</li></ul>"
        self.assertEqual(strip_html(html),
                         "Maintaining inventory • Reporting shortages")

    def test_entities_and_nbsp_are_decoded(self):
        self.assertEqual(strip_html("<p>Doppler, obstetrics &amp; abdominal&nbsp;</p>"),
                         "Doppler, obstetrics & abdominal")

    def test_sections_are_concatenated_in_portal_order(self):
        detail = {
            "ExternalDescriptionStr": "<p>Perform sonography procedures.</p>",
            "ExternalResponsibilitiesStr": "<ul><li>Maintain inventory</li></ul>",
            "ExternalQualificationsStr": "<p>NLT 2 years experience</p>",
        }
        self.assertEqual(
            build_description(detail),
            "Perform sonography procedures. Responsibilities: Maintain inventory "
            "Qualifications: NLT 2 years experience")

    def test_empty_sections_are_skipped(self):
        self.assertEqual(build_description({"ExternalDescriptionStr": ""}), "")


class TestCutoff(unittest.TestCase):
    """First run keeps every open requisition; later runs use the watermark."""

    def test_first_run_has_no_cutoff(self):
        self.assertIsNone(compute_cutoff(None))
        self.assertIsNone(compute_cutoff(pd.DataFrame(columns=["posted_date"])))

    def test_watermark_is_newest_posted_date_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-10", "2026-07-27", "2026-06-01"]})
        self.assertEqual(compute_cutoff(df), "2026-07-25")  # 27th - 2 days grace

    def test_unparseable_dates_do_not_break_the_watermark(self):
        df = pd.DataFrame({"posted_date": ["not-a-date", "2026-07-27"]})
        self.assertEqual(compute_cutoff(df), "2026-07-25")

    def test_within_window(self):
        self.assertTrue(within_window("2024-06-24", None))   # no cutoff = keep all
        self.assertTrue(within_window("2026-07-25", "2026-07-25"))  # boundary kept
        self.assertTrue(within_window("2026-07-27", "2026-07-25"))
        self.assertFalse(within_window("2026-07-24", "2026-07-25"))
        self.assertFalse(within_window("", "2026-07-25"))

    def test_recent_row_survives_a_freshly_computed_watermark(self):
        today = date.today()
        df = pd.DataFrame({"posted_date": [today.isoformat()]})
        cutoff = compute_cutoff(df)
        self.assertTrue(within_window((today - timedelta(days=1)).isoformat(), cutoff))
        self.assertFalse(within_window((today - timedelta(days=5)).isoformat(), cutoff))


class TestClubRow(unittest.TestCase):
    def test_schema_and_no_invented_salary(self):
        row = {"title": "Clinical Research Coordinator", "city": "Abu Dhabi",
               "facility": "Corniche Hospital", "category": "Non Clinical",
               "sub_category": "Clinical Research", "education": "Bachelors",
               "job_type": "full_time", "posted_date": "2026-07-27",
               "experience_min_years": "2", "job_url": "https://example.test/job/1"}
        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertNotIn("is_active", club)
        self.assertEqual(club["company_name"], "Corniche Hospital")
        self.assertEqual(club["min_experience"], "2")
        self.assertEqual(club["max_experience"], "")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        # structured StudyLevel wins over description extraction
        self.assertEqual(club["qualification"], "Bachelors")
        for field in ("min_salary", "max_salary", "salary_period", "salary_currency"):
            self.assertEqual(club[field], "")

    def test_qualification_falls_back_to_description(self):
        club = rich_row_to_club_row(
            {"title": "Epidemiologist",
             "description": "Requires an MPH and field experience."})
        self.assertIn("MPH", club["qualification"])

    def test_group_name_used_when_no_facility_parsed(self):
        club = rich_row_to_club_row({"title": "Clinical Data Manager"})
        self.assertEqual(club["company_name"], COMPANY_NAME)
        self.assertEqual(club["city_name"], DEFAULT_CITY)
        self.assertEqual(club["category"], "")

    def test_nan_values_from_csv_reload_become_blank(self):
        club = rich_row_to_club_row(
            {"title": "Sonographer", "facility": float("nan"),
             "experience_min_years": float("nan")})
        self.assertEqual(club["company_name"], COMPANY_NAME)
        self.assertEqual(club["min_experience"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
