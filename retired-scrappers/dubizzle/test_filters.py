#!/usr/bin/env python3
"""Unit tests for the dubizzle scraper's pure parsing/filter functions.

Run with plain `python test_filters.py` (no pytest needed). Worked examples
come from real dubai.dubizzle.com/jobs/medical-healthcare/ listings observed
on 2026-07-24.
"""

import unittest
from datetime import date

import pandas as pd

from scraper import (
    build_row, classify_category, classify_company_type, compute_cutoff,
    details_map, epoch_to_local_date, extract_listing_payload, job_type_from,
    parse_experience, parse_salary_bucket, rich_row_to_club_row,
)

# A trimmed real hit (Assistant Nurse / Midwife, posted 2026-07-16).
REAL_HIT = {
    "created_at": 1784191612,
    "name": {"en": "Assistant Nurse / Midwife", "ar": "Assistant Nurse / Midwife"},
    "uuid": "9c9536264c6f49a1800dea2f7179bc6b",
    "id": 100015472,
    "absolute_url": {"en": "https://dubai.dubizzle.com/jobs/medical-healthcare/"
                           "nurse-healthcare-assistant/2026/7/16/"
                           "assistant-nurse-midwife-2-364---"
                           "9c9536264c6f49a1800dea2f7179bc6b/"},
    "category": {"slug": ["medical-healthcare", "nurse-healthcare-assistant"],
                 "en": ["Medical / Healthcare", "Nurse / Healthcare Assistant"]},
    "location_list": {"en": ["UAE", "Dubai"]},
    "details_v2": {
        "primary": [
            {"label": {"en": "Monthly Salary"}, "slug": "salary",
             "value": {"en": "4,000 - 5,999"}},
            {"label": {"en": "Remote Job"}, "slug": "remote_job",
             "value": {"en": "No"}},
            {"label": {"en": "Employment Type"}, "slug": "required_commitment",
             "value": {"en": "Full Time"}},
            {"label": {"en": "Minimum Work Experience"},
             "slug": "required_work_experience", "value": {"en": "1-2 Years"}},
            {"label": {"en": "Benefits"}, "slug": "benefits",
             "value": {"en": "As per UAE law"}},
        ],
        "secondary": [
            {"label": {"en": "Minimum Education Level"},
             "slug": "required_education_level",
             "value": {"en": "High-School / Secondary"}},
            {"label": {"en": "Industry"}, "slug": "industry",
             "value": {"en": "Medical & Healthcare"}},
        ],
        "tertiary": [
            {"label": {"en": "Company Name"}, "slug": "company_name",
             "value": {"en": "Confidential"}},
            {"label": {"en": "Hide Company Name"}, "slug": "hide_company_name",
             "value": {"en": "True"}},
            {"label": {"en": "Gender"}, "slug": "gender",
             "value": {"en": "Female"}},
        ],
    },
}


class TestSalaryBucket(unittest.TestCase):
    def test_real_bucket(self):
        self.assertEqual(parse_salary_bucket("4,000 - 5,999"),
                         ("AED 4,000 - 5,999 per month", 4000, 5999))

    def test_low_bucket(self):
        self.assertEqual(parse_salary_bucket("2,000 - 3,999"),
                         ("AED 2,000 - 3,999 per month", 2000, 3999))

    def test_open_bucket_single_number(self):
        raw, lo, hi = parse_salary_bucket("30,000+")
        self.assertEqual((lo, hi), (30000, 30000))
        self.assertIn("30,000", raw)

    def test_missing_and_text_only(self):
        for value in ("", None, "Negotiable", "Unpaid"):
            self.assertEqual(parse_salary_bucket(value),
                             ("Not Disclosed", "", ""))

    def test_reversed_range_swapped(self):
        _, lo, hi = parse_salary_bucket("5,999 - 4,000")
        self.assertEqual((lo, hi), (4000, 5999))


class TestExperience(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_experience("1-2 Years"), (1, 2))
        self.assertEqual(parse_experience("0-1 Years"), (0, 1))

    def test_plus(self):
        self.assertEqual(parse_experience("5+ Years"), (5, ""))

    def test_missing(self):
        self.assertEqual(parse_experience(""), ("", ""))
        self.assertEqual(parse_experience(None), ("", ""))


class TestClassifier(unittest.TestCase):
    def test_nurse_titles(self):
        for title in ("Assistant Nurse / Midwife", "Nurse with DHA License",
                      "ICU Nurse", "Registered Nurse", "Home Caregiver"):
            category, review = classify_category(title)
            self.assertEqual(category, "nurses", title)
            self.assertFalse(review, title)

    def test_pharmacist_and_doctor(self):
        self.assertEqual(classify_category("Assistant Pharmacist")[0],
                         "pharmacists")
        self.assertEqual(classify_category("General Practitioner Doctor")[0],
                         "doctors")

    def test_non_clinical_with_signal_not_flagged(self):
        # Real listing: healthcare-adjacent but non-clinical title.
        category, review = classify_category(
            "Medical Receptionist", "other medical attendants")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(review)

    def test_no_signal_flagged_but_kept(self):
        category, review = classify_category("Office Assistant", "", "")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(review)

    def test_company_type(self):
        self.assertEqual(classify_company_type("Gulf Pharma FZE", "", ""),
                         "pharma")
        self.assertEqual(classify_company_type("Confidential",
                                               "Medical & Healthcare",
                                               "Nurse"), "hospital")


class TestJobType(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(job_type_from("Full Time", "No"), "full_time")
        self.assertEqual(job_type_from("Part Time", "No"), "part_time")
        self.assertEqual(job_type_from("Full Time", "Yes"), "remote")
        self.assertEqual(job_type_from("", ""), "full_time")


class TestDates(unittest.TestCase):
    def test_epoch_to_dubai_date(self):
        # 1784191612 = 2026-07-16 in Asia/Dubai (matches the URL /2026/7/16/).
        self.assertEqual(epoch_to_local_date(1784191612), "2026-07-16")

    def test_bad_epochs(self):
        self.assertEqual(epoch_to_local_date(None), "")
        self.assertEqual(epoch_to_local_date("x"), "")
        self.assertEqual(epoch_to_local_date(0), "")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        self.assertEqual(compute_cutoff(None, today=date(2026, 7, 24)),
                         "2026-06-24")

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-10", "2026-07-16", ""]})
        self.assertEqual(compute_cutoff(df), "2026-07-14")

    def test_empty_existing_falls_back(self):
        df = pd.DataFrame({"posted_date": ["", ""]})
        self.assertEqual(compute_cutoff(df, today=date(2026, 7, 24)),
                         "2026-06-24")


class TestRowBuilding(unittest.TestCase):
    def test_details_map_flattens_tiers(self):
        d = details_map(REAL_HIT)
        self.assertEqual(d["salary"], "4,000 - 5,999")
        self.assertEqual(d["industry"], "Medical & Healthcare")
        self.assertEqual(d["gender"], "Female")

    def test_build_row_real_hit(self):
        row = build_row(REAL_HIT)
        self.assertEqual(row["job_id"], "9c9536264c6f49a1800dea2f7179bc6b")
        self.assertEqual(row["title"], "Assistant Nurse / Midwife")
        self.assertEqual(row["posted_date"], "2026-07-16")
        self.assertEqual(row["city"], "Dubai")
        self.assertEqual(row["salary_min_monthly"], 4000)
        self.assertEqual(row["salary_currency"], "AED")
        self.assertEqual(row["category"], "nurses")
        self.assertFalse(row["needs_review"])
        self.assertEqual(row["company"], "Confidential")

    def test_club_row_conversion(self):
        club = rich_row_to_club_row(build_row(REAL_HIT))
        self.assertEqual(club["country_code"], "AE")
        self.assertEqual(club["city_name"], "Dubai")
        self.assertEqual(club["category"], "nurses")
        self.assertEqual(club["posted_at"], "2026-07-16")
        self.assertEqual(club["min_experience"], "1")
        self.assertEqual(club["max_experience"], "2")
        # AED can't be expressed in the club enum -> salary stays blank.
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")
        self.assertTrue(club["description"])

    def test_extract_listing_payload(self):
        nd = {"props": {"pageProps": {"reduxWrapperActionsGIPP": [
            {"type": "app/initApp", "payload": {}},
            {"type": "listings/fetchListingDataForQuery/fulfilled",
             "payload": {"hits": [REAL_HIT],
                         "pagination": {"page": 0, "totalPages": 1}}},
        ]}}}
        payload = extract_listing_payload(nd)
        self.assertEqual(len(payload["hits"]), 1)
        self.assertIsNone(extract_listing_payload({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
