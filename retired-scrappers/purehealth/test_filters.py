#!/usr/bin/env python3
"""Unit tests for the purehealth scraper's parsers — run with
`python test_filters.py`.

Every worked example (titles, ATS categories, description snippets) is copied
verbatim from live requisitions on PureHealth's Oracle Recruiting site
CX_6007.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    CLUB_COLUMNS, WATERMARK_GRACE_DAYS, apply_classification, clean_text,
    compute_cutoff, map_job_type, parse_city, parse_experience, parse_facility,
    rich_row_to_club_row, strip_html, within_window,
)


class TestClassificationWiring(unittest.TestCase):
    """The engine itself is tested in _shared/test_classification.py; these
    only prove this scraper wires its fields into it correctly."""

    def _row(self, title, category_original="", description=""):
        return {"title": title, "category_original": category_original,
                "description": description}

    def test_in_scope_role_gets_category_and_sub_category(self):
        row = self._row("Clinical Research Coordinator", "Administration")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")

    def test_public_health_role(self):
        row = self._row("Infection Prevention and Control Officer", "Nursing")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Infection Prevention & Control")

    def test_bedside_nurse_is_dropped(self):
        for title in ("Registered Nurse - CICU", "Cath Lab Staff Nurse",
                      "Assistant Nurse", "Midwife"):
            row = self._row(title, "Administration")
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_clinician_title_is_dropped(self):
        row = self._row("Consultant Physician", "Medical")
        self.assertFalse(apply_classification(row))

    def test_ats_category_cannot_admit_a_job(self):
        # the ATS files nurses under "Administration" — the facet is a signal
        # for the shared classifier, never a decider
        row = self._row("Patient Care Assistant (Non-Licensed)",
                        "Administration")
        self.assertFalse(apply_classification(row))


class TestParseExperience(unittest.TestCase):
    def test_range_with_en_dash(self):
        raw, lo, hi = parse_experience(
            "Experience: Minimum 2–3 years in cardiac critical care or "
            "cardiac ICU (UAE/GCC experience preferred)")
        self.assertEqual((lo, hi), ("2", "3"))
        self.assertIn("2–3 years", raw)

    def test_range_with_hyphen(self):
        _, lo, hi = parse_experience(
            "10-12 years of experience in healthcare industry")
        self.assertEqual((lo, hi), ("10", "12"))

    def test_spelled_out_number_with_digits(self):
        _, lo, hi = parse_experience(
            "Experience Minimum of two (2) years of recent clinical experience "
            "as a Registered Midwife in a hospital maternity unit")
        self.assertEqual((lo, hi), ("2", ""))

    def test_single_minimum(self):
        _, lo, hi = parse_experience(
            "Minimum of 2 years of recent experience in a Cardiac ICU (CICU)")
        self.assertEqual((lo, hi), ("2", ""))

    def test_plus_notation(self):
        _, lo, hi = parse_experience("At least 5+ years of experience required")
        self.assertEqual((lo, hi), ("5", ""))

    def test_reversed_range_is_normalised(self):
        _, lo, hi = parse_experience("Minimum 5 - 3 years of experience")
        self.assertEqual((lo, hi), ("3", "5"))

    def test_duration_without_experience_context_ignored(self):
        self.assertEqual(
            parse_experience("The DOH licence is valid for 2 years."),
            ("", "", ""))

    def test_qualitative_experience_left_empty(self):
        self.assertEqual(
            parse_experience("Clinical experience (preferably in a Mental "
                             "Health hospital)"),
            ("", "", ""))

    def test_empty_and_none_safe(self):
        self.assertEqual(parse_experience(""), ("", "", ""))
        self.assertEqual(parse_experience(None), ("", "", ""))


class TestJobType(unittest.TestCase):
    def test_full_time(self):
        self.assertEqual(map_job_type("Full time"), "full_time")

    def test_part_time(self):
        self.assertEqual(map_job_type("Part time"), "part_time")

    def test_blank_schedule_defaults_to_full_time(self):
        # requisition 2503 leaves JobSchedule unset
        self.assertEqual(map_job_type(None), "full_time")
        self.assertEqual(map_job_type(""), "full_time")


class TestLocations(unittest.TestCase):
    def test_town_from_work_location(self):
        self.assertEqual(
            parse_city([{"LocationName": "SEHA", "TownOrCity": "Abu Dhabi"}],
                       "United Arab Emirates"),
            "Abu Dhabi")

    def test_city_from_primary_location_string(self):
        self.assertEqual(parse_city([], "Abu Dhabi, United Arab Emirates"),
                         "Abu Dhabi")

    def test_country_only_falls_back_to_default_city(self):
        self.assertEqual(parse_city([], "United Arab Emirates"), "Abu Dhabi")

    def test_facility_kept_verbatim(self):
        self.assertEqual(
            parse_facility([{"LocationName": "Sheikh Shakhbout Medical City "
                                             "(SSMC)"}]),
            "Sheikh Shakhbout Medical City (SSMC)")

    def test_facility_missing(self):
        self.assertEqual(parse_facility([]), "")


class TestCutoff(unittest.TestCase):
    def test_no_csv_keeps_everything(self):
        self.assertIsNone(compute_cutoff(None))

    def test_empty_csv_keeps_everything(self):
        self.assertIsNone(compute_cutoff(pd.DataFrame(columns=["posted_date"])))

    def test_watermark_minus_grace(self):
        newest = date(2026, 7, 24)
        df = pd.DataFrame({"posted_date": ["2026-07-06", newest.isoformat()]})
        self.assertEqual(compute_cutoff(df),
                         (newest - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_within_window(self):
        self.assertTrue(within_window("2026-07-24", "2026-07-22"))
        self.assertTrue(within_window("2026-07-22", "2026-07-22"))  # inclusive
        self.assertFalse(within_window("2026-07-21", "2026-07-22"))
        self.assertFalse(within_window("", "2026-07-22"))

    def test_no_cutoff_keeps_undated_jobs(self):
        self.assertTrue(within_window("", None))


class TestTextCleaning(unittest.TestCase):
    def test_strip_html_separates_list_items(self):
        self.assertEqual(
            strip_html("Key Responsibilities <ul><li>Deliver safe care.</li>"
                       "<li>Monitor patients.</li></ul>"),
            "Key Responsibilities Deliver safe care. Monitor patients.")

    def test_entities_and_nbsp(self):
        self.assertEqual(strip_html("<p>Labour &amp;&nbsp;Delivery</p>"),
                         "Labour & Delivery")

    def test_none_safe(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(clean_text(None), "")


class TestClubRow(unittest.TestCase):
    def test_salary_columns_stay_empty(self):
        club = rich_row_to_club_row({
            "title": "Clinical Research Coordinator", "city": "Abu Dhabi",
            "country": "United Arab Emirates", "category": "Non Clinical",
            "sub_category": "Clinical Research",
            "job_type": "full_time", "posted_date": "2026-07-16",
            "expires_at": "2026-08-31", "description": "Run study visits",
            "job_url": "https://example.invalid/job/4582",
            "education": "Bachelors Degree",
            "experience_min_years": "2", "experience_max_years": "",
        })
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["max_salary"], "")
        self.assertEqual(club["salary_currency"], "")
        self.assertEqual(club["country_code"], "AE")
        self.assertEqual(club["company_type"], "hospital")
        self.assertNotIn("is_active", club)
        self.assertEqual(club["posted_at"], "2026-07-16")
        self.assertEqual(club["min_experience"], "2")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        # structured StudyLevel wins over description extraction
        self.assertEqual(club["qualification"], "Bachelors Degree")

    def test_qualification_falls_back_to_description(self):
        club = rich_row_to_club_row(
            {"title": "Medical Writer",
             "description": "Candidates must hold an MPH or equivalent."})
        self.assertIn("MPH", club["qualification"])

    def test_missing_fields_do_not_crash(self):
        club = rich_row_to_club_row({})
        self.assertEqual(club["country_name"], "United Arab Emirates")
        self.assertEqual(club["city_name"], "Abu Dhabi")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["category"], "")
        self.assertEqual(club["qualification"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
