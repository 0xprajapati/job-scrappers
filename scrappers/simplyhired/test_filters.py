#!/usr/bin/env python3
"""Unit tests for the simplyhired scraper's parsers and filters.

Run with plain:  python test_filters.py
Worked examples come from real __NEXT_DATA__ payloads captured on 2026-07-12.
"""

import unittest
from datetime import date

import pandas as pd

from simplyhired_scraper import (
    apply_classification,
    classify_company_type,
    compute_cutoff,
    epoch_ms_to_date,
    extract_page_props,
    job_to_rich_row,
    map_job_type,
    parse_city,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
)


class TestExtractPageProps(unittest.TestCase):
    def test_extracts_page_props(self):
        html = ('<html><script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"pageProps":{"resultCount":42,"jobs":[]}}}'
                '</script></html>')
        self.assertEqual(extract_page_props(html),
                         {"resultCount": 42, "jobs": []})

    def test_missing_or_bad_payload(self):
        self.assertIsNone(extract_page_props("<html>nope</html>"))
        self.assertIsNone(extract_page_props(
            '<script id="__NEXT_DATA__" type="application/json">{bad'
            '</script>'))


class TestParseCity(unittest.TestCase):
    def test_area_city_state(self):
        # real listing: "Saibaba Colony, Coimbatore, Tamil Nadu"
        self.assertEqual(parse_city("Saibaba Colony, Coimbatore, Tamil Nadu"),
                         "Coimbatore")

    def test_city_state(self):
        self.assertEqual(parse_city("Bengaluru, Karnataka"), "Bengaluru")
        self.assertEqual(parse_city("Delhi, Delhi"), "Delhi")

    def test_bare_region(self):
        self.assertEqual(parse_city("Goa"), "Goa")

    def test_empty(self):
        self.assertEqual(parse_city(""), "")
        self.assertEqual(parse_city(None), "")


class TestEpochMsToDate(unittest.TestCase):
    def test_real_listing_epoch_ms(self):
        # dateOnIndeed from a live listing (July 2026)
        self.assertEqual(epoch_ms_to_date(1783514221439), "2026-07-08")

    def test_garbage(self):
        self.assertEqual(epoch_ms_to_date(None), "")
        self.assertEqual(epoch_ms_to_date("recently"), "")


class TestParseSalary(unittest.TestCase):
    def test_monthly_range(self):
        # real: "₹18,000 - ₹25,000 a month"
        self.assertEqual(parse_salary("₹18,000 - ₹25,000 a month"),
                         ("₹18,000 - ₹25,000 a month", "18000", "25000",
                          "per_month"))

    def test_from_yearly_lakh_grouping(self):
        # real: "From ₹18,00,000 a year" (Indian digit grouping)
        self.assertEqual(parse_salary("From ₹18,00,000 a year"),
                         ("From ₹18,00,000 a year", "1800000", "1800000",
                          "per_annum"))

    def test_decimal_amounts(self):
        # real: "₹75,000.86 - ₹1,00,000.85 a month"
        raw = "₹75,000.86 - ₹1,00,000.85 a month"
        self.assertEqual(parse_salary(raw),
                         (raw, "75001", "100001", "per_month"))

    def test_hourly_kept_raw_but_not_normalized(self):
        raw = "Up to ₹350 an hour"
        self.assertEqual(parse_salary(raw), (raw, "", "", ""))

    def test_missing(self):
        self.assertEqual(parse_salary(None), ("Not Disclosed", "", "", ""))
        self.assertEqual(parse_salary(""), ("Not Disclosed", "", "", ""))


class TestMapJobType(unittest.TestCase):
    def test_full_time_wins_over_part_time(self):
        # real listings carry both: ["Part-time", "Full-time"]
        self.assertEqual(map_job_type(["Part-time", "Full-time"]), "full_time")

    def test_pure_part_time(self):
        self.assertEqual(map_job_type(["Part-time"]), "part_time")

    def test_remote_and_hybrid_hints(self):
        self.assertEqual(map_job_type(["Full-time"], ["Remote"]), "remote")
        self.assertEqual(map_job_type(["Full-time"], [], ["Hybrid work"]),
                         "hybrid")

    def test_defaults(self):
        self.assertEqual(map_job_type([]), "full_time")
        self.assertEqual(map_job_type(["Internship"]), "part_time")


class TestClassifyCategory(unittest.TestCase):
    """Wiring into the shared taxonomy — the engine itself is covered by
    ../_shared/test_classification.py."""

    def _row(self, title, qualifications="", description=""):
        return {"title": title, "qualifications": qualifications,
                "description": description}

    def test_in_scope_roles_are_stamped(self):
        for title, cat, sub in (
                ("Clinical Research Coordinator", "Non Clinical", "Clinical Research"),
                ("Medical Coder", "Non Clinical", "Medical Coding"),
                ("Clinical Data Manager", "Non Clinical", "Clinical Data Management"),
                ("Pharmacovigilance Associate", "Non Clinical", "Pharmacovigilance"),
                ("Public Health Nutritionist", "Public Health", "Public Health Nutrition")):
            row = self._row(title)
            self.assertTrue(apply_classification(row), title)
            self.assertEqual(row["category"], cat, title)
            self.assertEqual(row["sub_category"], sub, title)

    def test_bedside_and_admin_titles_are_dropped(self):
        """The 31 keyword walks are a recall device: "healthcare" pulls in
        bedside and back-office jobs, and the classifier is what removes
        them. They are dropped, not relabelled."""
        for title in ("bhms doctor", "Duty Doctor", "Staff Nurse",
                      "GNM Nursing", "Pharmacist", "Pharmacy Incharge",
                      "Medical Billing Executive", "Dialysis Technician",
                      "Wellness Evangelist"):
            row = self._row(title)
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_qualifications_reach_classifier_as_skills(self):
        """The site's qualifications bullets are the curated `skills`
        signal; pipe-joined in the rich CSV, split before scoring."""
        row = self._row("Associate", "Pharmacovigilance|Drug Safety")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["sub_category"], "Pharmacovigilance")
        self.assertEqual(row["matched_in"], "skills")


class TestCutoff(unittest.TestCase):
    def test_first_run_matches_source_filter(self):
        self.assertEqual(INITIAL_WINDOW_DAYS, 15)
        self.assertEqual(compute_cutoff(None, today=date(2026, 7, 12)),
                         "2026-06-27")

    def test_watermark_minus_grace(self):
        existing = pd.DataFrame({"posted_date": ["2026-07-05", "2026-07-10"]})
        self.assertEqual(WATERMARK_GRACE_DAYS, 2)
        self.assertEqual(compute_cutoff(existing), "2026-07-08")


class TestRowBuilding(unittest.TestCase):
    LISTING_JOB = {  # trimmed real listing payload
        "jobKey": "sfOKFX5pCK6jHnNhq_zBPvg_I6Lfvj2Ni4zfMvX5mloaZxQgtfk8nQ",
        "title": "bhms doctor",
        "company": "MODERN HOMEOPATHY PVT LTD",
        "location": "Goa",
        "salaryInfo": "₹18,000 - ₹25,000 a month",
        "dateOnIndeed": 1782287450027,
        "jobTypes": ["Full-time"],
        "remoteAttributes": [],
        "snippet": "Provide guidance on healthy lifestyle habits…",
        "sponsored": True,
        "botUrl": "/job/sfOKFX5pCK6jHnNhq_zBPvg_I6Lfvj2Ni4zfMvX5mloaZxQgtfk8nQ",
    }
    DETAIL = {  # trimmed real /job/<key> pageProps
        "jobDescriptionHtml": "<p>Provide guidance on <b>healthy</b> lifestyle "
                              "habits, nutrition, and preventive care.</p>",
        "formattedLocation": "Panaji, Goa",
        "state": "GA",
        "jobTypes": ["Full-time"],
        "workSettings": [],
        "compensation": "₹18,000 - ₹25,000 a month",
        "datePublished": 1782287450027,
        "employerName": "MODERN HOMEOPATHY PVT LTD",
        "employerSquareLogoUrl": None,
        "qualifications": ["Bachelor's degree", "Patient care"],
        "benefits": ["Paid time off"],
    }

    def test_rich_row_listing_only(self):
        row = job_to_rich_row(self.LISTING_JOB, None)
        self.assertEqual(row["job_id"], self.LISTING_JOB["jobKey"])
        self.assertEqual(row["city"], "Goa")
        # the row builder no longer classifies; apply_classification stamps
        # the taxonomy fields in the main loop
        self.assertEqual(row["category"], "")
        self.assertEqual(row["sub_category"], "")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["salary_min"], "18000")
        self.assertEqual(row["salary_period"], "per_month")
        self.assertEqual(row["posted_date"], "2026-06-24")
        self.assertTrue(row["sponsored"])
        self.assertIn("healthy lifestyle", row["description"])
        self.assertEqual(row["job_url"],
                         "https://www.simplyhired.co.in/job/" + row["job_id"])

    def test_rich_row_with_detail_prefers_detail_fields(self):
        row = job_to_rich_row(self.LISTING_JOB, self.DETAIL)
        self.assertEqual(row["city"], "Panaji")
        self.assertEqual(row["state"], "GA")
        self.assertIn("preventive care", row["description"])
        self.assertNotIn("<p>", row["description"])
        self.assertEqual(row["qualifications"],
                         "Bachelor's degree|Patient care")

    def test_club_row_exports_inr_salary(self):
        rich = job_to_rich_row(self.LISTING_JOB, self.DETAIL)
        rich["category"] = "Non Clinical"
        rich["sub_category"] = "Clinical Research"
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["country_dial_code"], "+91")
        self.assertEqual(club["min_salary"], "18000")
        self.assertEqual(club["max_salary"], "25000")
        self.assertEqual(club["salary_period"], "per_month")
        self.assertEqual(club["salary_currency"], "INR")
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Research")
        # the source's own qualifications bullets win over extraction
        self.assertEqual(club["qualification"], "Bachelor's degree")
        # retired columns are gone from the club contract
        self.assertNotIn("is_active", club)
        self.assertNotIn("expires_at", club)
        self.assertEqual(club["posted_at"], "2026-06-24")

    def test_club_row_undisclosed_salary_stays_empty(self):
        job = dict(self.LISTING_JOB, salaryInfo=None)
        rich = job_to_rich_row(job, None)
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")


class TestCompanyType(unittest.TestCase):
    def test_defaults_hospital(self):
        self.assertEqual(classify_company_type("Multispeciality Hospital"),
                         "hospital")

    def test_pharma(self):
        self.assertEqual(classify_company_type("Sun Pharma Distributors"),
                         "pharma")
        self.assertEqual(classify_company_type("Metropolis Diagnostics"),
                         "pharma")


class TestStripHtml(unittest.TestCase):
    def test_tags_and_entities(self):
        self.assertEqual(
            strip_html("<p>Nurses &amp; Doctors</p><ul><li>BLS</li></ul>"),
            "Nurses & Doctors BLS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
