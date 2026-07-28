#!/usr/bin/env python3
"""Unit tests for the kfshrc.edu.sa scraper's parsers, classifier and cutoff.

Every example below is copied from a real KFSH&RC posting on
www.kfshrc.edu.sa/en/careers/jobs-listing. Run with plain
`python test_filters.py`.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    build_description,
    classify_category,
    clean_department,
    clean_text,
    compute_cutoff,
    map_job_type,
    parse_audience,
    parse_city,
    parse_experience_years,
    parse_ms_date,
    rich_row_to_club_row,
    strip_html,
    title_case,
    within_window,
    CLUB_COLUMNS,
    COMPANY_NAME,
    DEFAULT_CITY,
    WATERMARK_GRACE_DAYS,
)


class TestTitleCase(unittest.TestCase):
    """The feed stores ALL CAPS; the board wants sentence case, with the KFSH&RC
    grade numerals and clinical acronyms intact."""

    def test_real_titles(self):
        cases = {
            "STAFF NURSE I": "Staff Nurse I",
            "PHARMACIST I": "Pharmacist I",
            "POLYSOMNOGRAPHY TECHNOLOGIST II": "Polysomnography Technologist II",
            "CASE MANAGEMENT ASSISTANT II": "Case Management Assistant II",
            "PROFESSOR OF BIOMEDICAL SCIENCES": "Professor of Biomedical Sciences",
            "CONSULTANT, MEDICAL ONCOLOGY": "Consultant, Medical Oncology",
            "ASSISTANT CONSULTANT, PEDIATRIC HEMATOLOGY / ONCOLOGY":
                "Assistant Consultant, Pediatric Hematology / Oncology",
            "ASSOCIATE CONSULTANT, INTERNIST POLYCLINICS":
                "Associate Consultant, Internist Polyclinics",
            "HEAD, BILLING AND ACCOUNTS RECEIVABLE":
                "Head, Billing and Accounts Receivable",
            "LOCUM TENENS": "Locum Tenens",
        }
        for raw, expected in cases.items():
            self.assertEqual(title_case(raw), expected, raw)

    def test_acronyms_and_grades_stay_uppercase(self):
        self.assertEqual(title_case("STAFF NURSE II - ICU"),
                         "Staff Nurse II - ICU")
        self.assertEqual(title_case("TECHNOLOGIST, MRI"), "Technologist, MRI")
        self.assertEqual(title_case("SPECIALIST, IT SYSTEMS"),
                         "Specialist, IT Systems")

    def test_hyphenated_compound_capitalises_every_part(self):
        self.assertEqual(title_case("NON-MALIGNANT BLOOD DISORDERS"),
                         "Non-Malignant Blood Disorders")
        self.assertEqual(title_case("IN-PATIENT PHARMACIST"),
                         "In-Patient Pharmacist")

    def test_mixed_case_input_is_left_alone(self):
        """A CMS change to normal casing must not be re-mangled."""
        self.assertEqual(title_case("Staff Nurse I"), "Staff Nurse I")
        self.assertEqual(title_case("Consultant, ENT Surgery"),
                         "Consultant, ENT Surgery")


class TestCleanDepartment(unittest.TestCase):
    """`(Sec)`/`(Dpt)`/`(Unit)` markers and the trailing branch letter are
    internal bookkeeping, not part of the department name."""

    def test_real_departments(self):
        cases = {
            "CASE MANAGEMENT (Sec)-R": "Case Management",
            "INPATIENT PHARMACY (Sec)-J": "Inpatient Pharmacy",
            "BLOOD BANK NURSING (Sec)-M": "Blood Bank Nursing",
            "ADULT MEDICAL CRITICAL CARE (Unit)-R":
                "Adult Medical Critical Care",
            "NON-MALIGNANT BLOOD DISORDERS (Sec)-R":
                "Non-Malignant Blood Disorders",
            "NURSING RECRUITMENT, RETENTION & RECOGNITION (Sec)-R":
                "Nursing Recruitment, Retention & Recognition",
            "RESPIRATORY CARE THERAPISTS AND TECHNICIANS (Sec)-J":
                "Respiratory Care Therapists and Technicians",
        }
        for raw, expected in cases.items():
            self.assertEqual(clean_department(raw), expected, raw)

    def test_two_level_department_keeps_both_levels(self):
        self.assertEqual(
            clean_department("ADMIN (Sec)-R/BILLING & ACCOUNTS RECEIVABLE (Dpt)-R"),
            "Admin / Billing & Accounts Receivable")
        self.assertEqual(clean_department("ADMIN (Sec)/BIOMEDICAL SCIENCES (Dpt)"),
                         "Admin / Biomedical Sciences")

    def test_blank(self):
        self.assertEqual(clean_department(""), "")
        self.assertEqual(clean_department(None), "")


class TestParseMsDate(unittest.TestCase):
    """Microsoft-JSON epochs stored at midnight Riyadh time (UTC+3). Parsing in
    UTC would report every posting one day early."""

    def test_real_values(self):
        # 2026-07-25T21:00:00Z == 2026-07-26 00:00 in Riyadh
        self.assertEqual(parse_ms_date("/Date(1785013200000)/"), "2026-07-26")
        self.assertEqual(parse_ms_date("/Date(1785531600000)/"), "2026-08-01")
        self.assertEqual(parse_ms_date("/Date(1770498000000)/"), "2026-02-08")

    def test_iso_passthrough_and_garbage(self):
        self.assertEqual(parse_ms_date("2026-07-26T00:00:00"), "2026-07-26")
        self.assertEqual(parse_ms_date(""), "")
        self.assertEqual(parse_ms_date(None), "")
        self.assertEqual(parse_ms_date("soon"), "")


class TestParseExperienceYears(unittest.TestCase):
    """KFSH&RC writes "Two (2) years ... or four (4) years ..." — alternative
    qualification pathways, so the lowest figure is the entry bar and the
    highest the upper bound."""

    def test_single_figure_leaves_max_blank(self):
        self.assertEqual(
            parse_experience_years(
                "Two (2) years of training in specialty and subspecialty plus "
                "post-training experience required."),
            ("2", ""))
        self.assertEqual(
            parse_experience_years(
                "Six (6) years of relatedexperience post-doctoral degree is "
                "required"),
            ("6", ""))
        self.assertEqual(
            parse_experience_years(
                "Five (5) years of training inspecialty and subspecialty plus "
                "post-training experience is required."),
            ("5", ""))

    def test_two_pathways_give_a_range(self):
        self.assertEqual(
            parse_experience_years(
                "Two (2) years of relatedexperience with Master’s, or four (4) "
                "years withPharm.D./ Bachelor’s Degree is required."),
            ("2", "4"))
        self.assertEqual(
            parse_experience_years(
                "One (1) year of related experience with Bachelor’s Degree or "
                "three (3) years with IPA Diploma is required."),
            ("1", "3"))

    def test_repeated_figure_collapses_to_one(self):
        self.assertEqual(
            parse_experience_years(
                "Seven (7) Years of training in specialty or subspecialty plus "
                "postgraduate training experience must be equal to or exceed "
                "seven (7) years’ experience in the subspecialty."),
            ("7", ""))

    def test_lookback_and_ceiling_phrases_are_ignored(self):
        """"for the last four (4) years" is an appraisal window and
        "less than one (1) year" a programme ceiling — neither is a
        requirement."""
        self.assertEqual(
            parse_experience_years(
                "Grade 08: Two (2) years of related experience is required. "
                "Grade 09: Four (4) years as Polysomnography Technologist II at "
                "KFSH&RC, or (Grade 08) related experience is required. "
                "Average of annual performance appraisal at least (3.5) for the "
                "last four (4) years. Fifty (50) hours of cumulative continued "
                "medical education [CME]."),
            ("2", "4"))
        self.assertEqual(
            parse_experience_years(
                "Two (2) years of Nursing experience with Bachelor?s or four (4) "
                "years with Associate Degree/Diploma is required. No experience "
                "is required for Saudis with Bachelor?s Degree. Enrolment in New "
                "Graduate Development Program for staff with less than one (1) "
                "year of experience is required."),
            ("2", "4"))

    def test_sentences_not_about_experience_are_skipped(self):
        self.assertEqual(
            parse_experience_years("Contract renewable every two (2) years."),
            ("", ""))

    def test_nothing_stated(self):
        self.assertEqual(parse_experience_years(""), ("", ""))
        self.assertEqual(parse_experience_years(None), ("", ""))
        self.assertEqual(parse_experience_years("N/A."), ("", ""))

    def test_bare_digits_are_a_fallback(self):
        self.assertEqual(
            parse_experience_years("Minimum 3 years of related experience."),
            ("3", ""))

    def test_implausible_figures_are_dropped(self):
        self.assertEqual(
            parse_experience_years("(99) years of experience is required."),
            ("", ""))


class TestClassifyCategory(unittest.TestCase):
    """Everything KFSH&RC posts is healthcare-sector employment, so nothing is
    dropped — the classifier only picks the club bucket."""

    def test_pharmacy_and_nursing_win_first(self):
        self.assertEqual(
            classify_category("Pharmacist I", "Inpatient Pharmacy"),
            ("pharmacists", False))
        self.assertEqual(
            classify_category("Staff Nurse I",
                              "Nursing Recruitment and Retention"),
            ("nurses", False))
        self.assertEqual(
            classify_category("Staff Nurse I", "Blood Bank Nursing"),
            ("nurses", False))

    def test_medical_staff_ranks_are_doctors(self):
        for title, dept in [
                ("Assistant Consultant, Pediatric Hematology / Oncology",
                 "Non-Malignant Blood Disorders"),
                ("Assistant Consultant, Critical Care",
                 "Surgical Critical Care Medicine"),
                ("Associate Consultant, Internist Polyclinics",
                 "Family Medicine"),
                ("Consultant, Medical Oncology", "Medical Oncology"),
                ("Locum Tenens", "Locum Tenens"),
        ]:
            self.assertEqual(classify_category(title, dept), ("doctors", False),
                             title)

    def test_allied_health_is_non_clinical(self):
        self.assertEqual(
            classify_category("Polysomnography Technologist II",
                              "Respiratory Care Therapists and Technicians"),
            ("non_clinical", False))

    def test_allied_keywords_beat_the_specialist_rank(self):
        """"Specialist" is a medical grade at KFSH&RC, but an unmistakable
        allied discipline in the same title must still win."""
        self.assertEqual(
            classify_category("Specialist, Respiratory Therapy", ""),
            ("non_clinical", False))

    def test_corporate_titles_are_non_clinical(self):
        self.assertEqual(
            classify_category("Head, Billing and Accounts Receivable",
                              "Admin / Billing & Accounts Receivable"),
            ("non_clinical", False))
        self.assertEqual(
            classify_category("Professor of Biomedical Sciences",
                              "Admin / Biomedical Sciences"),
            ("non_clinical", False))
        self.assertEqual(
            classify_category("Case Management Assistant II", "Case Management"),
            ("non_clinical", False))

    def test_corporate_keywords_beat_the_consultant_rank(self):
        self.assertEqual(
            classify_category("Consultant, Information Technology", ""),
            ("non_clinical", False))

    def test_generic_support_role_is_non_clinical_unflagged(self):
        self.assertEqual(classify_category("Patient Care Assistant", ""),
                         ("non_clinical", False))

    def test_unplaceable_title_is_kept_and_flagged(self):
        """Master spec §2: never silently dropped."""
        category, needs_review = classify_category("Wataniya Programme", "")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)


class TestMapJobType(unittest.TestCase):
    def test_locum_tenens_is_a_contract_grade(self):
        self.assertEqual(map_job_type("LOCUM TENENS"), "contract")

    def test_establishment_posts_are_full_time(self):
        for title in ("STAFF NURSE I", "PHARMACIST I", "CONSULTANT, MEDICAL "
                      "ONCOLOGY", "HEAD, BILLING AND ACCOUNTS RECEIVABLE"):
            self.assertEqual(map_job_type(title), "full_time", title)

    def test_explicit_schedules(self):
        self.assertEqual(map_job_type("Staff Nurse (Part-Time)"), "part_time")
        self.assertEqual(map_job_type("Pharmacy Internship"), "internship")


class TestLocationFields(unittest.TestCase):
    def test_branch_is_the_city(self):
        self.assertEqual(parse_city("Riyadh"), "Riyadh")
        self.assertEqual(parse_city("Jeddah"), "Jeddah")
        self.assertEqual(parse_city("Madinah"), "Madinah")
        self.assertEqual(parse_city("Medina"), "Madinah")

    def test_missing_branch_falls_back_to_the_main_campus(self):
        self.assertEqual(parse_city(""), DEFAULT_CITY)
        self.assertEqual(parse_city(None), DEFAULT_CITY)

    def test_unknown_branch_is_kept_title_cased(self):
        self.assertEqual(parse_city("DAMMAM"), "Dammam")

    def test_location_field_is_really_the_audience(self):
        self.assertEqual(parse_audience("External Job"), "external")
        self.assertEqual(parse_audience("Internal Job"), "internal")
        self.assertEqual(parse_audience(""), "")


class TestHtmlHandling(unittest.TestCase):
    def test_strip_html_keeps_list_boundaries(self):
        self.assertEqual(
            strip_html("<p><span>Assists the Consultants</span></p>"
                       "<ul><li>Ward rounds</li><li>On-call cover</li></ul>"),
            "Assists the Consultants • Ward rounds • On-call cover")

    def test_mojibake_possessive_is_repaired(self):
        self.assertEqual(clean_text("Bachelor?s Degree in Nursing"),
                         "Bachelor’s Degree in Nursing")

    def test_genuine_question_mark_survives(self):
        self.assertEqual(clean_text("Ready to join us?"), "Ready to join us?")

    def test_build_description_orders_and_labels_sections(self):
        description = build_description({
            "summary": "<span>Assists in providing medical care.</span>",
            "duties": "<p>Ward rounds.</p>",
            "education": "<span>Graduation from a medical school.</span>",
            "experience": "<span>Two (2) years of training.</span>",
            "otherrequirements": "<span>N/A. </span>",
        })
        self.assertEqual(
            description,
            "Assists in providing medical care. Duties: Ward rounds. "
            "Education: Graduation from a medical school. "
            "Experience: Two (2) years of training.")

    def test_build_description_tolerates_the_empty_postings(self):
        """Three of the 24 live postings carry title/branch/dates only."""
        self.assertEqual(build_description({"id": "joben157868"}), "")


class TestCutoff(unittest.TestCase):
    """First run keeps every open posting; later runs use the watermark."""

    def test_first_run_keeps_everything(self):
        self.assertIsNone(compute_cutoff(None))
        self.assertIsNone(compute_cutoff(pd.DataFrame(columns=["posted_date"])))

    def test_watermark_is_newest_stored_date_minus_grace(self):
        newest = date(2026, 7, 26)
        df = pd.DataFrame({"posted_date": ["2026-02-08", "2026-07-26",
                                           "2026-07-20"]})
        expected = (newest - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_unparseable_dates_do_not_break_the_watermark(self):
        df = pd.DataFrame({"posted_date": ["", "not a date", "2026-07-26"]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 7, 26) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_within_window(self):
        self.assertTrue(within_window("2026-01-01", None))
        self.assertTrue(within_window("2026-07-26", "2026-07-24"))
        self.assertTrue(within_window("2026-07-24", "2026-07-24"))
        self.assertFalse(within_window("2026-07-23", "2026-07-24"))
        self.assertFalse(within_window("", "2026-07-24"))


class TestClubRow(unittest.TestCase):
    def test_schema_and_mapping(self):
        rich = {
            "source": "kfshrc",
            "job_id": "joben159120",
            "title": "Pharmacist I",
            "company": COMPANY_NAME,
            "city": "Jeddah",
            "country": "Saudi Arabia",
            "job_type": "full_time",
            "category": "pharmacists",
            "experience_min_years": "2",
            "experience_max_years": "4",
            "posted_date": "2026-07-21",
            "expires_at": "2026-07-27",
            "description": "Provides pharmaceutical care.",
            "job_url": "https://www.kfshrc.edu.sa/en/careers/jobs-listing/"
                       "job?id=joben159120",
        }
        club = rich_row_to_club_row(rich, today=date(2026, 7, 22))
        self.assertEqual(list(club.keys()), CLUB_COLUMNS)
        self.assertEqual(club["country_name"], "Saudi Arabia")
        self.assertEqual(club["country_code"], "SA")
        self.assertEqual(club["country_dial_code"], "+966")
        self.assertEqual(club["city_name"], "Jeddah")
        self.assertEqual(club["company_name"], COMPANY_NAME)
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(club["category"], "pharmacists")
        self.assertEqual(club["min_experience"], "2")
        self.assertEqual(club["max_experience"], "4")
        self.assertEqual(club["is_active"], "true")

    def test_no_salary_is_ever_invented(self):
        club = rich_row_to_club_row({"title": "Staff Nurse I",
                                     "salary_raw": "Not Disclosed"})
        for field in ("min_salary", "max_salary", "salary_period",
                      "salary_currency"):
            self.assertEqual(club[field], "")

    def test_expired_posting_is_marked_inactive(self):
        club = rich_row_to_club_row({"title": "Staff Nurse I",
                                     "expires_at": "2026-07-20"},
                                    today=date(2026, 7, 22))
        self.assertEqual(club["is_active"], "false")

    def test_missing_expiry_stays_active(self):
        club = rich_row_to_club_row({"title": "Staff Nurse I"},
                                    today=date(2026, 7, 22))
        self.assertEqual(club["is_active"], "true")
        self.assertEqual(club["expires_at"], "")

    def test_defaults_when_the_source_is_silent(self):
        club = rich_row_to_club_row({"title": "Staff Nurse I"})
        self.assertEqual(club["company_name"], COMPANY_NAME)
        self.assertEqual(club["city_name"], DEFAULT_CITY)
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["category"], "non_clinical")

    def test_nan_values_from_csv_reload_become_blank(self):
        club = rich_row_to_club_row({"title": "Staff Nurse I",
                                     "city": float("nan"),
                                     "experience_min_years": float("nan")})
        self.assertEqual(club["city_name"], DEFAULT_CITY)
        self.assertEqual(club["min_experience"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
