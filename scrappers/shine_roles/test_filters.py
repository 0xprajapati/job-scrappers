#!/usr/bin/env python3
"""Unit tests for the role-scoped shine.com scraper's parsers and filters.

Run with plain `python test_filters.py` (no pytest needed). Worked examples
come from real listings captured on 2026-08-08. Family-scoring logic itself
lives in ../_shared/classification.py and is tested there
(../_shared/test_classification.py); these tests cover the shine-specific
wiring (jJT/jKwd/jJD -> classify_job) and the parsers.
"""

import unittest

from shine_scraper import (
    SEARCH_QUERIES,
    apply_classification,
    classify_company_type,
    club_salary,
    compute_cutoff,
    decode_job_type,
    page_url,
    parse_date,
    parse_experience,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    truncate_description,
)

import pandas as pd
from datetime import date


class TestParseSalary(unittest.TestCase):
    # Real strings seen on the healthcare query, 2026-08-08.

    def test_plain_lakh_range(self):
        raw, lo, hi, period = parse_salary("Rs 2.0  - 3.5 Lakh/Yr")
        self.assertEqual(raw, "Rs 2.0  - 3.5 Lakh/Yr")
        self.assertEqual(lo, "16667")     # 200,000 / 12
        self.assertEqual(hi, "29167")     # 350,000 / 12
        self.assertEqual(period, "Yr")

    def test_integer_lakhs(self):
        _, lo, hi, period = parse_salary("Rs 12  - 24 Lakh/Yr")
        self.assertEqual((lo, hi, period), ("100000", "200000", "Yr"))

    def test_mixed_absolute_and_lakh(self):
        # "50,000" has a comma -> absolute rupees; "2.5" -> lakhs.
        _, lo, hi, period = parse_salary("< Rs 50,000  - 2.5 Lakh/Yr")
        self.assertEqual((lo, hi, period), ("4167", "20833", "Yr"))

    def test_hidden(self):
        self.assertEqual(parse_salary("[Salary Hidden]"),
                         ("Not Disclosed", "", "", ""))

    def test_empty_and_none(self):
        self.assertEqual(parse_salary(""), ("Not Disclosed", "", "", ""))
        self.assertEqual(parse_salary(None), ("Not Disclosed", "", "", ""))

    def test_monthly(self):
        _, lo, hi, period = parse_salary("Rs 25,000 - 35,000 /Month")
        self.assertEqual((lo, hi, period), ("25000", "35000", "Mo"))

    def test_no_period_captures_raw_only(self):
        raw, lo, hi, period = parse_salary("Rs 5 Lakh")
        self.assertEqual(raw, "Rs 5 Lakh")
        self.assertEqual((lo, hi), ("", ""))   # never invent a period

    def test_never_invents(self):
        raw, lo, hi, _ = parse_salary("Negotiable")
        self.assertEqual((lo, hi), ("", ""))


class TestClubSalary(unittest.TestCase):
    def test_yearly_full_figures(self):
        lo, hi, period, currency = club_salary("Rs 4.0  - 4.5 Lakh/Yr")
        self.assertEqual((lo, hi), ("400000", "450000"))
        self.assertEqual((period, currency), ("per_annum", "INR"))

    def test_hidden_is_empty(self):
        self.assertEqual(club_salary("Not Disclosed"), ("", "", "", ""))
        self.assertEqual(club_salary("[Salary Hidden]"), ("", "", "", ""))


class TestParseExperience(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_experience("1 to 5 Yrs"), ("1", "5"))

    def test_single(self):
        self.assertEqual(parse_experience("0 Yrs"), ("0", "0"))

    def test_singular_unit(self):
        self.assertEqual(parse_experience("0 to 1 Yr"), ("0", "1"))

    def test_empty(self):
        self.assertEqual(parse_experience(""), ("", ""))
        self.assertEqual(parse_experience(None), ("", ""))


def _row(title, keywords="", description=""):
    return {"title": title, "keywords": keywords, "description": description}


class TestClassificationWiring(unittest.TestCase):
    """The shine record -> classification.classify_job wiring."""

    def test_title_hit_gets_two_level_taxonomy(self):
        row = _row("Senior Clinical Research Associate")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertEqual(row["role_family"], "Clinical Research")
        self.assertEqual(row["family_confidence"], "high")

    def test_keywords_rescue_a_generic_title(self):
        # Common shine pattern: CRO posts "Senior Executive" and names the
        # domain only in the keyword tags (jKwd is passed as `skills`).
        row = _row("Senior Executive", "pharmacovigilance,drug safety,argus")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["role_family"], "Pharmacovigilance")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")

    def test_description_only_admission(self):
        # The scraper hands the HTML-stripped jJD to the classifier.
        row = _row("Officer", "", "Runs clinical trials under GCP with full "
                                  "site management responsibility.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["role_family"], "Clinical Research")

    def test_out_of_scope_record_is_dropped(self):
        row = _row("Java Developer - Hospital Chain")
        self.assertFalse(apply_classification(row))
        self.assertEqual(row["category"], "")

    def test_bedside_role_is_dropped(self):
        row = _row("Staff Nurse - ICU", "icu, ward")
        self.assertFalse(apply_classification(row))

    def test_sales_title_is_kept_but_flagged(self):
        row = _row("Sales Manager - Medical Affairs")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["role_family"], "MSL")
        self.assertEqual(row["needs_review"], "true")


class TestPublicHealthCoverage(unittest.TestCase):
    """PH expanded 2026-08-24 to the ten-sub-category taxonomy."""

    PH_TITLES = [
        "Field Epidemiologist",
        "Public Health Programme Manager",
        "M&E Officer - Health Projects",
        "Community Health Worker Supervisor",
        "Health Education Specialist",
        "District Tuberculosis Coordinator (NTEP)",
        "Public Health Nutritionist",
        "Infection Prevention and Control Nurse",
        "HMIS / DHIS2 Data Analyst",
        "Health Systems Research Associate",
    ]

    def test_each_sub_category_lands_in_public_health(self):
        for title in self.PH_TITLES:
            row = _row(title)
            self.assertTrue(apply_classification(row), title)
            self.assertEqual(row["category"], "Public Health", title)
            self.assertEqual(row["role_family"], "Public Health", title)

    def test_ph_slugs_present(self):
        # The data-driven slug set (probed live 2026-08-24). M&E and
        # Community Health have no dedicated slug — shine's matching turns
        # those into tens of thousands of noise results with zero PH yield;
        # the classifier still catches strays from the other queries.
        for slug in ("public-health", "epidemiology", "epidemiologist",
                     "disease-surveillance", "health-promotion",
                     "tuberculosis", "hiv", "immunization", "vaccination",
                     "public-health-nutrition", "infection-control",
                     "health-informatics", "public-health-research"):
            self.assertIn(slug, SEARCH_QUERIES, slug)

    def test_search_slugs_are_unique(self):
        self.assertEqual(len(SEARCH_QUERIES), len(set(SEARCH_QUERIES)))


class TestCompanyType(unittest.TestCase):
    def test_pharma(self):
        self.assertEqual(classify_company_type("Sun Pharma Industries"),
                         "pharma")
        self.assertEqual(classify_company_type("Metropolis Diagnostics"),
                         "pharma")

    def test_hospital_default(self):
        self.assertEqual(classify_company_type("Apollo Hospitals"), "hospital")
        self.assertEqual(classify_company_type(""), "hospital")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        # INITIAL_WINDOW_DAYS is 2 in the roles variant
        self.assertEqual(
            compute_cutoff(None, today=date(2026, 8, 8)), "2026-08-06")

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-06"]})
        self.assertEqual(compute_cutoff(df), "2026-08-04")

    def test_since_override(self):
        self.assertEqual(compute_cutoff(None, since="2026-07-01"), "2026-07-01")

    def test_empty_dates_fall_back_to_window(self):
        df = pd.DataFrame({"posted_date": ["", ""]})
        self.assertEqual(
            compute_cutoff(df, today=date(2026, 8, 8)), "2026-08-06")


class TestSmallParsers(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(parse_date("2026-07-27T14:02:15"), "2026-07-27")
        self.assertEqual(parse_date(None), "")
        self.assertEqual(parse_date("soon"), "")

    def test_page_url(self):
        self.assertEqual(page_url("public-health", 1),
                         "https://www.shine.com/job-search/public-health-jobs?sort=1")
        self.assertEqual(page_url("epidemiology", 3),
                         "https://www.shine.com/job-search/epidemiology-jobs-3?sort=1")

    def test_page_url_with_extra_param(self):
        self.assertEqual(
            page_url("healthcare?ind=13", 2),
            "https://www.shine.com/job-search/healthcare-jobs-2?sort=1&ind=13")

    def test_page_url_industry_browse(self):
        # The bare "jobs" slug is the browse-all listing; it must not become
        # "jobs-jobs", and page N paginates as jobs-N (verified 2026-08-24).
        self.assertEqual(page_url("jobs?ind=63", 1),
                         "https://www.shine.com/job-search/jobs?sort=1&ind=63")
        self.assertEqual(page_url("jobs?ind=63", 2),
                         "https://www.shine.com/job-search/jobs-2?sort=1&ind=63")

    def test_strip_html(self):
        self.assertEqual(
            strip_html("<p><strong>Staff Nurse</strong></p>\r\n<p>ICU &amp; "
                       "ER</p>"),
            "Staff Nurse ICU & ER")

    def test_truncate_marks_cut(self):
        text = "word " * 100
        cut = truncate_description(text.strip(), limit=50)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(len(cut), 51)

    def test_decode_job_type(self):
        self.assertEqual(decode_job_type({"jTypeC": 1, "jEType": 1}),
                         ("Full time", "Regular", "full_time"))
        self.assertEqual(decode_job_type({"jTypeC": 2, "jEType": 1}),
                         ("Part time", "Regular", "part_time"))
        self.assertEqual(decode_job_type({"jTypeC": 1, "jEType": 4}),
                         ("Full time", "Work from home", "remote"))


class TestClubRow(unittest.TestCase):
    def test_full_mapping(self):
        rich = {
            "location": "Noida, Delhi", "company": "IQVIA",
            "company_type": "pharma", "title": "Epidemiologist",
            "description": "MPH required. Disease surveillance role.",
            "job_type": "Full time", "employment_type": "Regular",
            "category": "Public Health",
            "sub_category": "Epidemiology",
            "role_family": "Epidemiologist",
            "job_url": "https://www.shine.com/jobs/epidemiologist/x/1",
            "posted_date": "2026-08-07", "experience_min_years": "1",
            "experience_max_years": "5",
            "salary_raw": "Rs 2.0  - 3.5 Lakh/Yr",
        }
        row = rich_row_to_club_row(rich)
        self.assertEqual(row["country_code"], "IN")
        self.assertEqual(row["city_name"], "Noida")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Epidemiology")
        self.assertEqual(row["role_family"], "Epidemiologist")
        self.assertNotIn("is_active", row)
        self.assertNotIn("expires_at", row)
        self.assertEqual(row["qualification"], "MPH")
        self.assertEqual((row["min_salary"], row["max_salary"]),
                         ("200000", "350000"))
        self.assertEqual(row["salary_period"], "per_annum")
        self.assertEqual(row["salary_currency"], "INR")
        self.assertEqual(row["posted_at"], "2026-08-07")

    def test_city_falls_back_to_all_india(self):
        row = rich_row_to_club_row({"location": "", "salary_raw": ""})
        self.assertEqual(row["city_name"], "All India")


if __name__ == "__main__":
    unittest.main(verbosity=1)
