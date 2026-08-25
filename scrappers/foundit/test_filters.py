#!/usr/bin/env python3
"""Unit tests for the foundit scraper's parsers and filters.

Run with plain:  python test_filters.py
Worked examples come from real cards in captures/ (see README.md).
"""

import unittest
from datetime import date

from foundit_scraper import (
    card_signals,
    classify_company_type,
    compute_cutoff,
    decode_job_type,
    epoch_ms_to_ist_date,
    experience_years,
    job_to_rich_row,
    parse_salary,
    rich_row_to_club_row,
    scope_card,
    strip_html,
    truncate_description,
)

import pandas as pd


class TestSalary(unittest.TestCase):
    """Real worked examples from the 08-08-2026 capture."""

    def test_yearly_range_with_monthly_precomputed(self):
        # "Medical Billing - Team Lead" (Vee Healthtek): 4.5-6 LPA
        raw, lo, hi, period, cur, hidden = parse_salary(
            {"currency": "INR", "absoluteValue": 450000,
             "absoluteMonthlyValue": 37500},
            {"currency": "INR", "absoluteValue": 600000,
             "absoluteMonthlyValue": 50000},
            True, "INR")
        self.assertEqual(raw, "INR 450000-600000 P.A.")
        self.assertEqual((lo, hi), ("37500", "50000"))
        self.assertEqual(period, "Yr")
        self.assertEqual(cur, "INR")
        self.assertEqual(hidden, "true")   # payload value, UI hides it

    def test_yearly_without_monthly_divides_by_12(self):
        raw, lo, hi, period, _, _ = parse_salary(
            {"absoluteValue": 250000}, {"absoluteValue": 550000},
            False, "INR")
        self.assertEqual(raw, "INR 250000-550000 P.A.")
        self.assertEqual((lo, hi), ("20833", "45833"))
        self.assertEqual(period, "Yr")

    def test_monthly_only(self):
        raw, lo, hi, period, _, _ = parse_salary(
            {"absoluteMonthlyValue": 20000}, {"absoluteMonthlyValue": 25000},
            False, "INR")
        self.assertEqual(raw, "INR 20000-25000 P.M.")
        self.assertEqual((lo, hi), ("20000", "25000"))
        self.assertEqual(period, "Mo")

    def test_open_range_uses_known_end(self):
        raw, lo, hi, _, _, _ = parse_salary(
            {"absoluteValue": 300000}, {"absoluteValue": 0}, False, "INR")
        self.assertEqual(raw, "INR 300000-300000 P.A.")
        self.assertEqual((lo, hi), ("25000", "25000"))

    def test_all_zero_is_not_disclosed_never_invented(self):
        raw, lo, hi, period, cur, hidden = parse_salary(
            {"currency": "INR", "absoluteValue": 0, "absoluteMonthlyValue": 0},
            {"currency": "INR", "absoluteValue": 0, "absoluteMonthlyValue": 0},
            False, "INR")
        self.assertEqual((raw, lo, hi, period, cur), ("Not Disclosed", "", "", "", ""))
        self.assertEqual(hidden, "false")

    def test_missing_objects(self):
        raw, lo, hi, _, _, _ = parse_salary(None, None, False, None)
        self.assertEqual((raw, lo, hi), ("Not Disclosed", "", ""))


class TestScopeGate(unittest.TestCase):
    """Wiring of the shared classify_job (2026-08-25 taxonomy migration).

    Classifier behaviour is tested in _shared/test_classification.py; here
    we check that foundit wires it correctly — `category` is the top-level
    taxonomy value, the family lands in `role_family`, and out-of-scope /
    vetoed cards come back in_scope=False (dropped by the main loop).
    """

    def _row(self, **card):
        title, skills_str, description = card_signals(card)
        verdict = scope_card(title, skills_str, description)
        self.assertTrue(verdict["in_scope"], "expected in scope: %r" % card)
        return job_to_rich_row({"jobId": 1, "postedAt": 1786124151000,
                                **card}, verdict)

    def test_in_scope_nonclinical_family(self):
        row = self._row(title="Pharmacovigilance Associate")
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")
        self.assertEqual(row["role_family"], "Pharmacovigilance")
        self.assertIn("title", row["matched_in"])

    def test_public_health_in_scope(self):
        row = self._row(title="Epidemiologist")
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Epidemiology")
        self.assertEqual(row["role_family"], "Public Health")

    def test_skills_rescue_when_title_is_generic(self):
        verdict = scope_card(
            "Senior Executive", "Argus, MedDRA, ICSR, case processing", "")
        self.assertTrue(verdict["in_scope"])
        self.assertEqual(verdict["category"], "Non Clinical")
        self.assertEqual(verdict["role_family"], "Pharmacovigilance")
        self.assertEqual(verdict["sub_category"], "Pharmacovigilance")

    def test_taxonomy_veto_drops_billing_lookalike(self):
        # "Medical Billing" scores on 'coding'/'clinical' vocabulary but the
        # negative-keyword veto must exclude it.
        verdict = scope_card("Medical Billing - Team Lead", "", "")
        self.assertFalse(verdict["in_scope"])
        self.assertTrue(verdict["vetoed_by"])

    def test_out_of_scope_clinical_role(self):
        self.assertFalse(scope_card("Consultant Cardiologist", "", "")["in_scope"])

    def test_out_of_scope_staff_nurse(self):
        self.assertFalse(scope_card("Staff Nurse - ICU", "", "")["in_scope"])

    def test_company_type(self):
        self.assertEqual(classify_company_type("Syneos Health CRO"), "pharma")
        self.assertEqual(classify_company_type("Apollo Hospitals"), "hospital")


class TestClubRow(unittest.TestCase):
    def test_club_row_carries_family_and_sub_category(self):
        club = rich_row_to_club_row({
            "title": "Regulatory Affairs Manager", "location": "Pune",
            "category": "Regulatory Affairs",
            "sub_category": "Regulatory Affairs",
            "description": "MBBS preferred; eCTD submissions."})
        self.assertEqual(club["category"], "Regulatory Affairs")
        self.assertEqual(club["sub_category"], "Regulatory Affairs")
        self.assertEqual(club["qualification"], "MBBS")

    def test_club_row_has_no_legacy_columns(self):
        club = rich_row_to_club_row({"title": "x", "location": "Pune",
                                     "category": "HEOR"})
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)


class TestDatesAndCutoff(unittest.TestCase):
    def test_epoch_ms_to_ist_date(self):
        # real card postedAt: 2026-08-07 17:35:51 UTC = 23:05:51 IST
        self.assertEqual(epoch_ms_to_ist_date(1786124151000), "2026-08-07")

    def test_ist_rollover_differs_from_utc(self):
        # 2026-08-07 20:00 UTC is already 2026-08-08 01:30 IST — the IST
        # calendar day must win (foundit is an India board).
        self.assertEqual(epoch_ms_to_ist_date(1786132800000), "2026-08-08")

    def test_bad_epoch(self):
        self.assertEqual(epoch_ms_to_ist_date(None), "")
        self.assertEqual(epoch_ms_to_ist_date("x"), "")
        self.assertEqual(epoch_ms_to_ist_date(0), "")

    def test_first_run_window(self):
        cutoff = compute_cutoff(None, today=date(2026, 8, 8))
        self.assertEqual(cutoff, "2026-08-01")   # INITIAL_WINDOW_DAYS = 7

    def test_watermark_with_grace(self):
        existing = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-06"]})
        self.assertEqual(compute_cutoff(existing), "2026-08-04")   # -2 grace

    def test_since_override(self):
        self.assertEqual(compute_cutoff(None, since="2026-07-01"), "2026-07-01")


class TestMisc(unittest.TestCase):
    def test_decode_job_type(self):
        job_type, employment, club = decode_job_type(
            ["Permanent Job", "Jobs for Women"], ["Full time"])
        self.assertEqual(job_type, "Permanent Job, Jobs for Women")
        self.assertEqual(employment, "Full time")
        self.assertEqual(club, "full_time")
        self.assertEqual(decode_job_type([], ["Part time"])[2], "part_time")
        self.assertEqual(decode_job_type(["Work From Home"], [])[2], "remote")

    def test_experience_years(self):
        self.assertEqual(experience_years({"years": 5}), "5")
        self.assertEqual(experience_years(3), "3")
        self.assertEqual(experience_years(None), "")
        self.assertEqual(experience_years({"years": None}), "")

    def test_strip_html(self):
        self.assertEqual(
            strip_html("<strong>Who We Are<br/></strong>Nurses &amp; carers"),
            "Who We Are Nurses & carers")

    def test_truncate_description(self):
        text = "word " * 6000
        cut = truncate_description(text.strip())
        self.assertLessEqual(len(cut), 20_001)
        self.assertTrue(cut.endswith("…"))

    def test_club_row_yearly_salary_roundtrip(self):
        row = {"job_type": "Permanent Job", "employment_type": "Full time",
               "location": "Hyderabad / Secunderabad, Telangana",
               "salary_min_monthly": "37500", "salary_max_monthly": "50000",
               "salary_period_original": "Yr",
               "salary_currency_original": "INR",
               "company": "Vee Healthtek", "company_type": "hospital",
               "title": "Medical Coder", "description": "d",
               "category": "Medical Coding", "sub_category": "Medical Coding",
               "job_url": "https://www.foundit.in/job/x-1",
               "posted_date": "2026-08-05", "experience_min_years": "1",
               "experience_max_years": "5"}
        club = rich_row_to_club_row(row)
        self.assertEqual(club["min_salary"], "450000")
        self.assertEqual(club["max_salary"], "600000")
        self.assertEqual(club["salary_period"], "per_annum")
        self.assertEqual(club["salary_currency"], "INR")
        self.assertEqual(club["city_name"], "Hyderabad / Secunderabad")
        self.assertEqual(club["job_type"], "full_time")

    def test_club_row_no_salary(self):
        club = rich_row_to_club_row({"title": "Clinical Research Associate",
                                     "location": "Pune",
                                     "category": "Clinical Research"})
        self.assertEqual((club["min_salary"], club["salary_period"],
                          club["salary_currency"]), ("", "", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
