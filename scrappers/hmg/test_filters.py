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
    build_description,
    classify_category,
    compute_cutoff,
    country_meta,
    location_label,
    map_job_type,
    normalize_city,
    parse_experience,
    parse_salary,
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


class TestCategory(unittest.TestCase):
    """Master spec §2 — the title wins, the ATS category is the fallback."""

    def test_title_beats_ats_default_category(self):
        # the ATS files these clinical roles under the literal "Default"
        self.assertEqual(classify_category("Registered Nurse", "Default"),
                         ("nurses", False))
        self.assertEqual(classify_category("Consultant IVF", "Default"),
                         ("doctors", False))

    def test_physician_titles(self):
        for title in ("Consultant Endocrinologist", "Senior Specialist OB/GYN",
                      "Consultant Plastic Surgeon", "Specialist PICU",
                      "Consultant Psychiatrist"):
            self.assertEqual(classify_category(title, "Physicians")[0],
                             "doctors", title)

    def test_dental_titles_from_the_ajaji_portal(self):
        for title in ("Restorative and Esthetic Dentist", "Endodontics",
                      "Oral Maxillofacial", "Pediatric Dentistry"):
            self.assertEqual(classify_category(title, "Doctor")[0], "doctors",
                             title)

    def test_nursing_titles(self):
        for title in ("Head Nurse", "Charge Nurse", "Nursing informatics",
                      "Assistant Nurse", "Dental Assistant"):
            category, review = classify_category(title, "Nursing")
            self.assertEqual(category, "nurses", title)
            self.assertFalse(review)

    def test_pharmacy_titles(self):
        self.assertEqual(classify_category("Pharmacist - 3", "Pharmacy"),
                         ("pharmacists", False))
        self.assertEqual(classify_category("Tamheer Program - Pharmacist",
                                           "Default"),
                         ("pharmacists", False))

    def test_allied_health_is_non_clinical_but_not_flagged(self):
        for title in ("Ultrasound Technologist", "Dialysis Technician",
                      "Echo Technician", "Cath Lab Radiographer",
                      "Laser Technician"):
            category, review = classify_category(title, "Paramedical")
            self.assertEqual(category, "non_clinical", title)
            self.assertFalse(review, title)

    def test_technologist_is_not_a_physician(self):
        # "-ologist" must not turn technologists/psychologists into doctors
        self.assertEqual(classify_category("Neurology Technologist",
                                           "Paramedical")[0], "non_clinical")

    def test_hospital_admin_is_kept_unflagged(self):
        for title in ("Medical Administrator",
                      "Tamheer Program - Patient Services"):
            category, review = classify_category(title, "Administration")
            self.assertEqual(category, "non_clinical", title)
            self.assertFalse(review, title)

    def test_unclassifiable_is_kept_and_flagged(self):
        # WRASS / Cloud Solutions support roles: kept, never dropped
        for title, ats in (("Graphic Designer", "Administration"),
                           ("AC Technician", "Administration"),
                           ("Housekeeper", "Administration"),
                           ("Senior Developer", "Administration"),
                           ("FMS Manager", "")):
            category, review = classify_category(title, ats)
            self.assertEqual(category, "non_clinical", title)
            self.assertTrue(review, title)

    def test_healthcare_signal_in_major_clears_the_flag(self):
        _, review = classify_category("Associate", "", "Emergency Medical "
                                                       "Services")
        self.assertFalse(review)


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
