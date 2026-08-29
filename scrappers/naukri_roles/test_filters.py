#!/usr/bin/env python3
"""Unit tests for the naukri scraper's parsers/classifiers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples are taken verbatim from real captured cards.
"""

import unittest

import pandas as pd

import naukri_scraper as ns


class TestSalary(unittest.TestCase):
    def _card(self, label, min_s=None, max_s=None, hide=False, cur="INR"):
        detail = {"minimumSalary": min_s, "maximumSalary": max_s,
                  "hideSalary": hide, "currency": cur}
        return {"placeholders": [{"type": "salary", "label": label}],
                "salaryDetail": detail, "currency": cur}

    def test_full_annual_range(self):
        # "45-60 Lacs PA" -> full integers from salaryDetail, per_annum
        raw, lo, hi, cur, per = ns.parse_salary(
            self._card("45-60 Lacs PA", 4500000, 6000000))
        self.assertEqual(raw, "45-60 Lacs PA")
        self.assertEqual((lo, hi, cur, per), ("4500000", "6000000", "INR", "per_annum"))

    def test_small_lacs_range(self):
        raw, lo, hi, cur, per = ns.parse_salary(
            self._card("3.5-4 Lacs PA", 350000, 400000))
        self.assertEqual((lo, hi, per), ("350000", "400000", "per_annum"))

    def test_cr_range(self):
        raw, lo, hi, cur, per = ns.parse_salary(
            self._card("55 Lacs-1 Cr PA", 5500000, 10000000))
        self.assertEqual((lo, hi), ("5500000", "10000000"))

    def test_hidden_salary(self):
        raw, lo, hi, cur, per = ns.parse_salary(
            self._card("Not disclosed", hide=True))
        self.assertEqual((lo, hi, cur, per), ("", "", "", ""))
        self.assertEqual(raw, "Not disclosed")

    def test_missing_detail(self):
        job = {"placeholders": [], "salaryDetail": {}}
        raw, lo, hi, cur, per = ns.parse_salary(job)
        self.assertEqual(raw, "Not Disclosed")
        self.assertEqual((lo, hi), ("", ""))

    def test_min_only(self):
        raw, lo, hi, cur, per = ns.parse_salary(
            self._card("30 Lacs PA", 3000000, None))
        self.assertEqual((lo, hi), ("3000000", "3000000"))

    def test_reversed_range_is_sorted(self):
        raw, lo, hi, cur, per = ns.parse_salary(
            self._card("weird", 6000000, 4500000))
        self.assertEqual((lo, hi), ("4500000", "6000000"))


class TestSharedClassifierWiring(unittest.TestCase):
    """job_to_rich_row() runs the shared two-level classifier and returns
    None for an out-of-scope card (the engine itself is covered by
    _shared/test_classification.py)."""

    @staticmethod
    def card(title, tags="", jd=""):
        return {"jobId": "1", "title": title, "tagsAndSkills": tags,
                "jobDescription": jd, "placeholders": [], "salaryDetail": {}}

    def test_in_scope_role_gets_taxonomy_labels(self):
        row = ns.job_to_rich_row(self.card(
            "We are Hiring For HCC Certified medical coders",
            "medical coding,ICD-10,CPC",
            "<p>Assign ICD-10-CM codes for HCC risk adjustment.</p>"))
        self.assertIsNotNone(row)
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Coding")
        self.assertEqual(row["role_family"], "Medical Coding")
        # all three signals were passed through and all three matched
        self.assertEqual(row["matched_in"], "title|skills|description")

    def test_second_in_scope_family(self):
        row = ns.job_to_rich_row(self.card(
            "Pharmacovigilance Associate", "argus,ICSR,MedDRA",
            "Case processing and MedDRA coding of adverse events."))
        self.assertIsNotNone(row)
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")

    def test_clinical_titles_are_dropped(self):
        for t in ["Neurologist For Varanasi, Uttar Pradesh", "Cardiologist",
                  "Staff Nurse", "GNM Nursing Incharge",
                  "Hospital Pharmacist", "Physiotherapist",
                  "Front Desk Receptionist"]:
            self.assertIsNone(ns.job_to_rich_row(self.card(t)), t)

    def test_skills_are_a_signal_not_a_decision(self):
        # A bedside title stays out of scope even with in-scope-looking tags.
        self.assertIsNone(ns.job_to_rich_row(self.card(
            "ICU Staff Nurse", "clinical research,GCP")))


class TestCompanyType(unittest.TestCase):
    def test_pharma(self):
        self.assertEqual(ns.classify_company_type("Sun Pharma Laboratories"), "pharma")
        self.assertEqual(ns.classify_company_type("Acme Diagnostics"), "pharma")

    def test_hospital_default(self):
        self.assertEqual(ns.classify_company_type("Apollo Hospitals"), "hospital")
        self.assertEqual(ns.classify_company_type("Linking Jobs", "Multispeciality Hospitals"), "hospital")


class TestLocation(unittest.TestCase):
    def test_single_indian_city(self):
        self.assertEqual(ns.parse_location("Hyderabad"),
                         ("Hyderabad", "India", "IN", "+91"))

    def test_multi_city_takes_first(self):
        city, country, code, dial = ns.parse_location("Nashik, Pune, Mumbai (All Areas)")
        self.assertEqual((city, country), ("Nashik", "India"))

    def test_all_areas_stripped(self):
        city, *_ = ns.parse_location("Mumbai (All Areas)")
        self.assertEqual(city, "Mumbai")

    def test_bare_country_uses_country_as_city(self):
        # club requires a non-empty city_name; a country-only location reuses it
        self.assertEqual(ns.parse_location("Saudi Arabia"),
                         ("Saudi Arabia", "Saudi Arabia", "SA", "+966"))

    def test_empty_defaults_india(self):
        self.assertEqual(ns.parse_location(""), ("", "India", "IN", "+91"))


class TestDate(unittest.TestCase):
    def test_epoch_ms(self):
        # 1786189694385 ms -> 2026-08-08 (UTC), verbatim from a captured card
        self.assertEqual(ns.epoch_ms_to_date(1786189694385), "2026-08-08")

    def test_zero_and_none(self):
        self.assertEqual(ns.epoch_ms_to_date(0), "")
        self.assertEqual(ns.epoch_ms_to_date(None), "")
        self.assertEqual(ns.epoch_ms_to_date("garbage"), "")


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_window(self):
        from datetime import date
        cutoff = ns.compute_cutoff(None, today=date(2026, 8, 8))
        self.assertEqual(cutoff, "2026-08-06")  # 8 - INITIAL_WINDOW_DAYS

    def test_later_run_uses_watermark(self):
        df = pd.DataFrame({"posted_date": ["2026-08-05", "2026-08-08", "2026-08-07"]})
        # newest 2026-08-08 minus 2 grace days
        self.assertEqual(ns.compute_cutoff(df), "2026-08-06")


class TestSameRoleDedup(unittest.TestCase):
    """dedupe_same_role() collapses (title, company, city) near-duplicates
    WITHIN one ingest batch — job_id stays the cross-run dedup key."""

    @staticmethod
    def row(job_id, title, company, city, desc, ms=0):
        return {"job_id": job_id, "title": title, "company": company,
                "city": city, "description": desc, "_created_ms": ms}

    def test_longer_description_wins(self):
        rows = [
            self.row("1", "Clinical Data Svs Specialist", "IQVIA", "Kochi",
                     "short teaser"),
            self.row("2", "Clinical Data Svs Specialist", "IQVIA", "Kochi",
                     "a much longer, fuller description of the same role"),
        ]
        kept, skipped = ns.dedupe_same_role(rows)
        self.assertEqual([r["job_id"] for r in kept], ["2"])
        self.assertEqual(skipped, 1)

    def test_key_is_normalised(self):
        # lowercase + collapsed whitespace: these are the SAME role
        rows = [
            self.row("1", "Clinical  Data Specialist", "IQVIA ", "Kochi", "x"),
            self.row("2", "clinical data specialist", "iqvia", " KOCHI", "xx"),
        ]
        kept, skipped = ns.dedupe_same_role(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped, 1)

    def test_different_city_is_kept(self):
        # one role per city is a legitimate naukri posting pattern
        rows = [
            self.row("1", "Medical Coder", "Omega", "Chennai", "desc"),
            self.row("2", "Medical Coder", "Omega", "Trichy", "desc"),
        ]
        kept, skipped = ns.dedupe_same_role(rows)
        self.assertEqual(len(kept), 2)
        self.assertEqual(skipped, 0)

    def test_tie_broken_by_newer_created_date(self):
        rows = [
            self.row("old", "Pharmacovigilance Associate", "Accenture",
                     "Bengaluru", "same length!", ms=1756000000000),
            self.row("new", "Pharmacovigilance Associate", "Accenture",
                     "Bengaluru", "same length!", ms=1756100000000),
        ]
        kept, _ = ns.dedupe_same_role(rows)
        self.assertEqual(kept[0]["job_id"], "new")

    def test_transient_ms_key_is_stripped(self):
        kept, _ = ns.dedupe_same_role(
            [self.row("1", "Medical Coder", "Omega", "Chennai", "desc")])
        self.assertNotIn("_created_ms", kept[0])


class TestJdTooShort(unittest.TestCase):
    """The rich-CSV trace flag for card-teaser descriptions (NAUKRI-01)."""

    def test_under_threshold_flagged(self):
        self.assertEqual(ns.flag_jd_too_short("x" * 199), "True")
        self.assertEqual(ns.flag_jd_too_short(""), "True")
        self.assertEqual(ns.flag_jd_too_short(None), "True")

    def test_at_and_over_threshold_blank(self):
        self.assertEqual(ns.flag_jd_too_short("x" * 200), "")
        self.assertEqual(ns.flag_jd_too_short("x" * 5000), "")

    def test_flag_is_never_a_club_column(self):
        self.assertNotIn("jd_too_short", ns.CLUB_COLUMNS)
        # rightmost rich (trace) column
        self.assertEqual(ns.RICH_COLUMNS[-1], "jd_too_short")


class TestCaptureDate(unittest.TestCase):
    """capture_file_date() reads DD-MM-YYYY from the filename, falling back
    to the file's mtime (NAUKRI-02 stale-capture warning)."""

    def test_filename_date_wins(self):
        from datetime import date
        self.assertEqual(ns.capture_file_date("captures/24-08-2026.json"),
                         date(2026, 8, 24))

    def test_bad_name_falls_back_to_mtime(self):
        import os
        import tempfile
        from datetime import date
        with tempfile.NamedTemporaryFile(suffix="undated.json") as fh:
            mtime = 1756000000  # 2026-08-24 UTC (local date may differ by 1)
            os.utime(fh.name, (mtime, mtime))
            self.assertEqual(ns.capture_file_date(fh.name),
                             date.fromtimestamp(mtime))

    def test_missing_file_returns_none(self):
        self.assertIsNone(ns.capture_file_date("captures/no-such-file.json"))


class TestClubMapping(unittest.TestCase):
    def test_inr_annual_exported(self):
        row = {"salary_currency": "INR", "salary_period": "per_annum",
               "salary_min": "4500000", "salary_max": "6000000",
               "country": "India", "country_code": "IN",
               "country_dial_code": "+91", "city": "Pune",
               "company": "X", "title": "Cardiologist", "job_url": "u",
               "posted_date": "2026-08-08", "category": "Non Clinical",
               "sub_category": "Clinical Research",
               "description": "Site monitoring to ICH-GCP. B.Pharm preferred.",
               "company_type": "pharma"}
        club = ns.rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(ns.CLUB_COLUMNS))
        self.assertEqual(club["min_salary"], "4500000")
        self.assertEqual(club["salary_currency"], "INR")
        self.assertEqual(club["salary_period"], "per_annum")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        # qualification is grounded extraction, never inferred
        self.assertEqual(club["qualification"], "B.Pharm")

    def test_undisclosed_salary_blank(self):
        row = {"salary_currency": "", "salary_period": "", "salary_min": "",
               "salary_max": "", "country": "India"}
        club = ns.rich_row_to_club_row(row)
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")
        # required defaults still present
        self.assertEqual(club["country_name"], "India")
        # is_active / expires_at are retired from the club contract
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)


if __name__ == "__main__":
    unittest.main(verbosity=2)
