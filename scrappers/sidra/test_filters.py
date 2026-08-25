#!/usr/bin/env python3
"""Unit tests for the Sidra Medicine scraper's parsers, classifier and cutoff.

Every example below is copied from a real Sidra requisition on the Oracle
candidate portal. Run with plain `python test_filters.py`.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    apply_classification,
    build_description,
    compute_cutoff,
    is_nationals_only,
    is_talent_pool,
    map_job_type,
    normalise_title,
    parse_city,
    parse_experience_years,
    rich_row_to_club_row,
    strip_html,
    within_window,
    CLUB_COLUMNS,
    COMPANY_NAME,
    DEFAULT_CITY,
)


class TestTitle(unittest.TestCase):
    """Sidra mixes hyphens and en dashes; parentheticals are role qualifiers,
    never a facility (one hospital)."""

    def test_dash_variants_are_normalised(self):
        self.assertEqual(normalise_title("Manager – Ethics and Compliance"),
                         "Manager - Ethics and Compliance")
        self.assertEqual(normalise_title("Psychologist – Developmental Pediatrics (PhD)"),
                         "Psychologist - Developmental Pediatrics (PhD)")
        self.assertEqual(normalise_title("Specialist- Anesthesiology"),
                         "Specialist - Anesthesiology")

    def test_intra_word_hyphens_survive(self):
        self.assertEqual(
            normalise_title("Technologist - Cardiovascular (Non-Invasive)"),
            "Technologist - Cardiovascular (Non-Invasive)")
        self.assertEqual(normalise_title("Specialist - Sub-Acute Care"),
                         "Specialist - Sub-Acute Care")

    def test_qualifier_parenthetical_stays_in_the_title(self):
        for raw in ("Technologist - Cardiovascular (EP)",
                    "Clinical Nurse (Women's Inpatient Services Div)",
                    "Clinical Nurse Leader (Operating Room)",
                    "Physician - Child and Adolescent Psychiatry (Arabic Speaker)"):
            self.assertEqual(normalise_title(raw), raw)

    def test_plain_titles_pass_through(self):
        self.assertEqual(normalise_title("Sonographer"), "Sonographer")
        self.assertEqual(normalise_title("  Internal   Auditor "), "Internal Auditor")

    def test_qatarization_markers_are_detected(self):
        self.assertTrue(is_nationals_only("Engineer - Specialty Systems (Nationals only)"))
        self.assertTrue(is_nationals_only("Supervisor - Financial Counselling (Qatarized)"))
        self.assertFalse(is_nationals_only("Technologist II - Ultrasound"))
        self.assertFalse(is_nationals_only(""))


class TestExperienceParser(unittest.TestCase):
    """Requirements live in the qualifications table under "Experience";
    the LOWEST stated bar is the entry requirement."""

    def test_house_style_plus_years(self):
        qualifications = (
            "QUALIFICATIONS, EXPERIENCE AND SKILLS – SELECTION CRITERIA • "
            "ESSENTIAL • Education • BSc (Hons.) clinical physiology or "
            "equivalent (RCVT) • Experience • 5+ years as a senior Cardiac "
            "Technologist or RCVT • 2+ Years specialized experience in cardiac "
            "catheterization laboratory • Certification and Licensure • "
            "Registered as Clinical Physiologist")
        self.assertEqual(parse_experience_years(qualifications), ("2", ""))

    def test_education_years_outside_the_experience_block_are_ignored(self):
        # "Diploma in Practical Nursing (2 years)" sits under Education
        qualifications = (
            "• ESSENTIAL • Education • Diploma in Practical Nursing (2 years) "
            "after high school or Equivalent • Experience • 5+ years of work "
            "experience as a Licensed Practical Nurse • Certification and "
            "Licensure • Basic Life Support (BLS)")
        self.assertEqual(parse_experience_years(qualifications), ("5", ""))

    def test_minimum_of_phrasing(self):
        qualifications = (
            "QUALIFICATIONS & EXPERIENCE • Education • MD, MBBS degree (or "
            "equivalent) • Experience • A minimum of 1 year of post-board "
            "certification in Pediatric Anesthesia •")
        self.assertEqual(parse_experience_years(qualifications), ("1", ""))

    def test_explicit_range_fills_max(self):
        self.assertEqual(parse_experience_years(
            "• Experience • 3 - 5 years of clinical experience in a tertiary "
            "paediatric centre •"), ("3", "5"))

    def test_falls_back_to_cued_matches_without_an_experience_section(self):
        self.assertEqual(parse_experience_years(
            "Minimum of 8 years of relevant auditing experience"), ("8", ""))
        self.assertEqual(parse_experience_years(
            "at least 3 years of experience in a paediatric setting"), ("3", ""))
        self.assertEqual(parse_experience_years(
            "7+ years experience in a tertiary hospital"), ("7", ""))

    def test_uncued_year_numbers_outside_a_section_are_ignored(self):
        self.assertEqual(parse_experience_years(
            "Renewable 2 year contract with annual leave."), ("", ""))
        self.assertEqual(parse_experience_years(
            "Sidra Medicine opened its doors 8 years ago."), ("", ""))

    def test_no_requirement_returns_blanks(self):
        self.assertEqual(parse_experience_years(""), ("", ""))
        self.assertEqual(parse_experience_years(None), ("", ""))
        self.assertEqual(parse_experience_years(
            "Provides specialist technical expertise to the Cardiology service."),
            ("", ""))

    def test_implausible_values_are_dropped(self):
        self.assertEqual(parse_experience_years(
            "minimum of 99 years of experience"), ("", ""))


class TestClassificationWiring(unittest.TestCase):
    """The engine itself is tested in _shared/test_classification.py; these
    only prove this scraper wires its fields into it correctly."""

    def _row(self, title, category_original="", requisition_type="",
             job_function="", description="", talent_pool=False):
        return {"title": title, "category_original": category_original,
                "requisition_type": requisition_type,
                "job_function": job_function, "description": description,
                "talent_pool": talent_pool}

    def test_in_scope_role_gets_category_and_sub_category(self):
        row = self._row("Specialist - Clinical Data Management",
                        "Enabling Function: Admin", "Corporate")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Data Management")
        self.assertEqual(row["role_family"], "Clinical Data Management")
        self.assertFalse(row["needs_review"])

    def test_public_health_role(self):
        row = self._row("Specialist - Infection Prevention and Control",
                        "Nursing", "Nursing")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_clinical_titles_are_dropped(self):
        for title, facet, req in (
                ("Physician - Hematopathology", "Physician", "Physicians"),
                ("Licensed Practical Nurse (LPN)", "Nursing", "Nursing"),
                ("Sonographer", "Allied Health", "Allied Health"),
                ("Clinical Nurse Leader (Operating Room)", "Nursing", "Nursing")):
            row = self._row(title, facet, req)
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_oracle_facet_cannot_admit_a_job(self):
        # "Enabling Function: …" used to map straight to a category; now it is
        # only a signal, and a corporate title with no taxonomy family drops
        row = self._row("Internal Auditor", "Enabling Function: Admin",
                        "Corporate")
        self.assertFalse(apply_classification(row))

    def test_in_scope_talent_pool_row_is_kept_and_flagged(self):
        row = self._row("Clinical Research Coordinator",
                        "Join Our Talent Pool - Future opportunities",
                        "Campaigns", talent_pool=True)
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertTrue(row["needs_review"])

    def test_talent_pool_detection(self):
        self.assertTrue(is_talent_pool(
            "Join Our Talent Pool - Future opportunities", "Campaigns"))
        self.assertTrue(is_talent_pool("", "Campaigns"))
        self.assertFalse(is_talent_pool("Nursing", "Nursing"))


class TestJobTypeAndCity(unittest.TestCase):
    def test_job_type(self):
        self.assertEqual(map_job_type("Full time"), "full_time")
        self.assertEqual(map_job_type("Part time"), "part_time")
        self.assertEqual(map_job_type(None), "full_time")

    def test_arabic_schedule_labels(self):
        # the bilingual portal returns the Arabic label on some requisitions
        self.assertEqual(map_job_type("كل الوقت"), "full_time")
        self.assertEqual(map_job_type("بعض الوقت"), "part_time")

    def test_city_defaults_to_doha(self):
        # every Sidra requisition carries only the country
        self.assertEqual(parse_city("Qatar"), DEFAULT_CITY)
        self.assertEqual(parse_city(""), DEFAULT_CITY)

    def test_specific_work_location_wins(self):
        self.assertEqual(parse_city("Qatar", "Al Rayyan, Qatar"), "Al Rayyan")
        self.assertEqual(parse_city("Doha, Qatar"), "Doha")


class TestDescription(unittest.TestCase):
    def test_list_items_are_separated(self):
        html = ("<ul><li>Plans clinical education</li>"
                "<li>Provides technical expertise</li></ul>")
        self.assertEqual(strip_html(html),
                         "Plans clinical education • Provides technical expertise")

    def test_qualification_table_cells_are_separated(self):
        html = ("<table><tr><td>Experience</td>"
                "<td>5+ years as a senior Cardiac Technologist</td></tr></table>")
        self.assertEqual(strip_html(html),
                         "Experience • 5+ years as a senior Cardiac Technologist")

    def test_entities_and_nbsp_are_decoded(self):
        self.assertEqual(
            strip_html("<p>Invasive &amp; Non Invasive Cardiology&nbsp;</p>"),
            "Invasive & Non Invasive Cardiology")

    def test_sections_are_concatenated_in_portal_order(self):
        detail = {
            "ExternalDescriptionStr": "<p>Performs cardiac catheterization.</p>",
            "ExternalResponsibilitiesStr": "<ul><li>Sets up equipment</li></ul>",
            "ExternalQualificationsStr": "<p>5+ years experience</p>",
        }
        self.assertEqual(
            build_description(detail),
            "Performs cardiac catheterization. Responsibilities: Sets up equipment "
            "Qualifications: 5+ years experience")

    def test_empty_sections_are_skipped(self):
        # the talent-pool campaign requisition carries no description at all
        self.assertEqual(build_description({"ExternalDescriptionStr": ""}), "")


class TestCutoff(unittest.TestCase):
    """First run keeps every open requisition; later runs use the watermark."""

    def test_first_run_has_no_cutoff(self):
        self.assertIsNone(compute_cutoff(None))
        self.assertIsNone(compute_cutoff(pd.DataFrame(columns=["posted_date"])))

    def test_watermark_is_newest_posted_date_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-20", "2026-07-26", "2026-06-11"]})
        self.assertEqual(compute_cutoff(df), "2026-07-24")  # 26th - 2 days grace

    def test_unparseable_dates_do_not_break_the_watermark(self):
        df = pd.DataFrame({"posted_date": ["not-a-date", "2026-07-26"]})
        self.assertEqual(compute_cutoff(df), "2026-07-24")

    def test_within_window(self):
        self.assertTrue(within_window("2026-06-11", None))   # no cutoff = keep all
        self.assertTrue(within_window("2026-07-24", "2026-07-24"))  # boundary kept
        self.assertTrue(within_window("2026-07-26", "2026-07-24"))
        self.assertFalse(within_window("2026-07-23", "2026-07-24"))
        self.assertFalse(within_window("", "2026-07-24"))

    def test_recent_row_survives_a_freshly_computed_watermark(self):
        today = date.today()
        df = pd.DataFrame({"posted_date": [today.isoformat()]})
        cutoff = compute_cutoff(df)
        self.assertTrue(within_window((today - timedelta(days=1)).isoformat(), cutoff))
        self.assertFalse(within_window((today - timedelta(days=5)).isoformat(), cutoff))


class TestClubRow(unittest.TestCase):
    def test_schema_and_no_invented_salary(self):
        row = {"title": "Specialist - Clinical Data Management", "city": "Doha",
               "category": "Non Clinical", "sub_category": "Clinical Data Management",
               "job_type": "full_time", "education": "Bachelor's Degree",
               "posted_date": "2026-07-20", "expires_at": "2026-08-05",
               "experience_min_years": "2", "experience_max_years": "",
               "job_url": "https://example.test/job/3891"}
        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)
        self.assertEqual(club["company_name"], COMPANY_NAME)
        self.assertEqual(club["country_name"], "Qatar")
        self.assertEqual(club["country_code"], "QA")
        self.assertEqual(club["min_experience"], "2")
        self.assertEqual(club["max_experience"], "")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Data Management")
        # structured StudyLevel wins over description extraction
        self.assertEqual(club["qualification"], "Bachelor's Degree")
        for field in ("min_salary", "max_salary", "salary_period", "salary_currency"):
            self.assertEqual(club[field], "")

    def test_qualification_falls_back_to_description(self):
        club = rich_row_to_club_row(
            {"title": "Medical Writer",
             "description": "Requires an MPH or equivalent postgraduate degree."})
        self.assertIn("MPH", club["qualification"])

    def test_defaults_when_fields_are_missing(self):
        club = rich_row_to_club_row({"title": "Clinical Research Coordinator"})
        self.assertEqual(club["city_name"], DEFAULT_CITY)
        self.assertEqual(club["category"], "")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["qualification"], "")

    def test_nan_values_from_csv_reload_become_blank(self):
        club = rich_row_to_club_row(
            {"title": "Medical Coder", "experience_min_years": float("nan"),
             "education": float("nan"), "description": float("nan")})
        self.assertEqual(club["min_experience"], "")
        self.assertEqual(club["qualification"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
