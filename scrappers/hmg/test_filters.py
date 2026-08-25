#!/usr/bin/env python3
"""Unit tests for the hmg.com (Dr. Sulaiman Al Habib) scraper's parsers.

Worked examples are taken verbatim from the Elevatus API payload of
talents.hmg.com / the group's other career portals.

Run with plain `python test_filters.py`.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    CLUB_COLUMNS,
    apply_classification,
    build_description,
    classification_skills,
    compute_cutoff,
    country_meta,
    location_label,
    map_job_type,
    normalize_city,
    parse_experience,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    within_window,
)


class TestSalary(unittest.TestCase):
    """Master spec §3: capture, never filter, never invent."""

    def test_hmg_never_publishes_pay(self):
        # every posting on every HMG portal carries this exact payload
        self.assertEqual(parse_salary({"min": 0, "max": 0}),
                         ("Not Disclosed", "", "", "", ""))

    def test_missing_salary_object(self):
        self.assertEqual(parse_salary(None),
                         ("Not Disclosed", "", "", "", ""))

    def test_range_is_captured_if_ever_published(self):
        raw, lo, hi, period, currency = parse_salary({"min": 12000,
                                                      "max": 18000})
        self.assertEqual(raw, "SAR 12,000 - 18,000 per month")
        self.assertEqual((lo, hi), ("12000", "18000"))
        self.assertEqual((period, currency), ("per_month", "SAR"))

    def test_single_bound(self):
        raw, lo, hi, period, _ = parse_salary({"min": 9000, "max": 0})
        self.assertEqual(raw, "SAR 9,000 per month")
        self.assertEqual((lo, hi, period), ("9000", "", "per_month"))

    def test_reversed_bounds_are_swapped(self):
        _, lo, hi, _, _ = parse_salary({"min": 20000, "max": 15000})
        self.assertEqual((lo, hi), ("15000", "20000"))

    def test_junk_values_are_not_invented(self):
        self.assertEqual(parse_salary({"min": None, "max": "n/a"}),
                         ("Not Disclosed", "", "", "", ""))


class TestExperience(unittest.TestCase):
    def test_single_value_is_a_minimum(self):
        # "Consultant Endocrinologist": years_of_experience [3]
        self.assertEqual(parse_experience([3]), ("3", ""))

    def test_zero_years_is_kept(self):
        # "Tamheer Program - Patient Services": fresh graduates, [0]
        self.assertEqual(parse_experience([0]), ("0", ""))

    def test_two_values_make_a_range(self):
        self.assertEqual(parse_experience([2, 5]), ("2", "5"))

    def test_empty_and_junk(self):
        self.assertEqual(parse_experience([]), ("", ""))
        self.assertEqual(parse_experience(None), ("", ""))
        self.assertEqual(parse_experience(["", None]), ("", ""))


class TestClassificationWiring(unittest.TestCase):
    """The scraper delegates every keep/drop + label decision to
    _shared/classification.py; these tests check the wiring only (the engine
    itself is covered by _shared/test_classification.py)."""

    @staticmethod
    def _row(title, ats_category="", major="", industry="", career_level="",
             skills="", description=""):
        return {"title": title, "category_original": ats_category,
                "major": major, "industry": industry,
                "career_level": career_level, "skills": skills,
                "description": description}

    def test_skills_signal_joins_the_ats_facets(self):
        row = self._row("Medical Coder", "Administration",
                        major="Health Information", industry="Healthcare",
                        career_level="Mid Level")
        self.assertEqual(
            classification_skills(row),
            "Administration Mid Level Healthcare Health Information")

    def test_generic_competency_tags_are_kept_out_of_the_signal(self):
        # HMG fills `skills` from a corporate competency framework; feeding
        # it in admitted radiologists/secretaries as Clinical Data Management
        row = self._row("Senior Specialist Radiologist", "Physicians",
                        major="Physicians", industry="Hospital & Health Care",
                        career_level="Mid - Level",
                        skills="data management & record keeping; "
                               "data gathering & assessment")
        self.assertNotIn("data management", classification_skills(row))
        self.assertFalse(apply_classification(row))

    def test_in_scope_role_is_labelled(self):
        row = self._row("Medical Coder", "Administration")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")
        self.assertEqual(row["role_family"], "Medical Coding")

    def test_public_health_role_is_labelled(self):
        row = self._row("Infection Control Coordinator", "Administration")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_ats_category_cannot_admit_a_clinical_role(self):
        # the ATS files real clinical roles under the literal "Default";
        # neither the title nor the facet may keep them in scope now
        for title, ats in (("Registered Nurse", "Default"),
                           ("Consultant Endocrinologist", "Physicians"),
                           ("Pharmacist - 3", "Pharmacy"),
                           ("Ultrasound Technologist", "Paramedical")):
            row = self._row(title, ats)
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_support_services_roles_are_dropped(self):
        # WRASS / Cloud Solutions postings with no in-scope signal
        for title in ("Graphic Designer", "AC Technician", "Housekeeper",
                      "Senior Developer"):
            self.assertFalse(
                apply_classification(self._row(title, "Administration")), title)

    def test_club_row_uses_shared_columns(self):
        row = self._row("Clinical Research Coordinator", "Default",
                        description="Coordinates ethics submissions.")
        row["education"] = "Bachelor's Degree"
        apply_classification(row)
        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        self.assertEqual(club["qualification"], "Bachelor's Degree")
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)


class TestJobType(unittest.TestCase):
    def test_permanent_is_full_time(self):
        self.assertEqual(map_job_type(["Permanent"]), "full_time")

    def test_blank_defaults_to_full_time(self):
        self.assertEqual(map_job_type([]), "full_time")
        self.assertEqual(map_job_type(None), "full_time")

    def test_other_enum_values(self):
        self.assertEqual(map_job_type(["Part Time"]), "part_time")
        self.assertEqual(map_job_type(["Remote"]), "remote")
        self.assertEqual(map_job_type(["Hybrid"]), "hybrid")


class TestLocation(unittest.TestCase):
    def test_facility_label_city(self):
        # real location objects from the API
        self.assertEqual(normalize_city("Jeddah - Al Mohammdiya"), "Jeddah")
        self.assertEqual(normalize_city("Sewedi - Riyadh"), "Riyadh")
        self.assertEqual(normalize_city("Al Sahafah - Riyadh"), "Riyadh")
        self.assertEqual(normalize_city("Kharj"), "Kharj")
        self.assertEqual(normalize_city("Qassim"), "Qassim")

    def test_country_label_is_not_a_city(self):
        self.assertEqual(normalize_city("Saudi Arabia"), "Riyadh")

    def test_fallback_city_then_default(self):
        self.assertEqual(normalize_city("", "Riyadh"), "Riyadh")
        self.assertEqual(normalize_city("", ""), "Riyadh")

    def test_unknown_label_is_kept_verbatim(self):
        self.assertEqual(normalize_city("Wadi Al Dawasir"), "Wadi Al Dawasir")

    def test_location_label_shapes(self):
        self.assertEqual(
            location_label({"name": {"en": "Khobar", "ar": "الخبر"}}), "Khobar")
        self.assertEqual(location_label({"city": "Riyadh", "country": "Saudi "
                                                                     "Arabia"}),
                         "Riyadh")
        self.assertEqual(location_label({"city": None, "country": None}), "")
        self.assertEqual(location_label(None), "")

    def test_country_meta(self):
        self.assertEqual(country_meta({"country": "Saudi Arabia"}),
                         ("Saudi Arabia", "SA", "+966"))
        # missing country: HMG's postings are Saudi unless stated otherwise
        self.assertEqual(country_meta({"country": None}),
                         ("Saudi Arabia", "SA", "+966"))
        self.assertEqual(country_meta({"country": "United Arab Emirates"}),
                         ("United Arab Emirates", "AE", "+971"))


class TestDescription(unittest.TestCase):
    def test_tags_become_spaces(self):
        html = ("<p>Manage the assigned clinics.</p><ul><li>Admit the "
                "patients.</li></ul>")
        self.assertEqual(strip_html(html),
                         "Manage the assigned clinics. Admit the patients.")

    def test_entities_are_unescaped(self):
        self.assertEqual(strip_html("<p>diagnosis&nbsp;&amp; treatment</p>"),
                         "diagnosis & treatment")

    def test_requirements_are_appended_with_a_label(self):
        text = build_description("<p>Deliver care.</p>",
                                 "<p>Bachelor of Nursing.</p>")
        self.assertEqual(text, "Deliver care. Requirements: Bachelor of "
                               "Nursing.")

    def test_missing_requirements(self):
        self.assertEqual(build_description("<p>Deliver care.</p>", None),
                         "Deliver care.")


class TestCutoff(unittest.TestCase):
    """Master spec §4 — first run keeps every open posting, later runs use the
    watermark with a 2-day grace overlap."""

    def test_first_run_keeps_everything(self):
        self.assertIsNone(compute_cutoff(None))
        self.assertIsNone(compute_cutoff(pd.DataFrame(columns=["posted_date"])))

    def test_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-20", "2026-07-27",
                                           "2026-06-01"]})
        self.assertEqual(compute_cutoff(df), "2026-07-25")

    def test_unparseable_dates_fall_back_to_first_run(self):
        df = pd.DataFrame({"posted_date": ["", None]})
        self.assertIsNone(compute_cutoff(df))

    def test_window_boundaries(self):
        self.assertTrue(within_window("2026-07-25", "2026-07-25"))
        self.assertTrue(within_window("2026-07-26", "2026-07-25"))
        self.assertFalse(within_window("2026-07-24", "2026-07-25"))

    def test_missing_posted_date_is_kept(self):
        # 3 corporate postings have posted_at: null — dedup adds them once
        self.assertTrue(within_window("", "2026-07-25"))

    def test_no_cutoff_keeps_everything(self):
        old = (date.today() - timedelta(days=900)).isoformat()
        self.assertTrue(within_window(old, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
