#!/usr/bin/env python3
"""Unit tests for the pharmarecruiter.in scraper's parsers.

Run with plain:  python test_filters.py
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    apply_classification,
    classify_company_type,
    classify_job_type,
    compute_cutoff,
    extract_labeled_fields,
    parse_experience,
    parse_location,
    parse_salary,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    _NEWSY_TITLE_RE,
)


class TestExtractLabeledFields(unittest.TestCase):
    # Real shape from post 39698 (Accuprec Research Labs, 2026-07-24).
    HTML = """
    <h2>Job Details</h2>
    <ul class="wp-block-list">
      <li><strong>Company Name</strong>: Accuprec Research Labs Pvt Ltd</li>
      <li><strong>Experience</strong>: 0-7 Years (varies by role)</li>
      <li><strong>Qualification</strong>: M.Pharm, M.Sc, BE, B.Tech</li>
      <li><strong>Location</strong>: Ahmedabad, India</li>
      <li><strong>Work Type</strong>: Full-time, On-site</li>
    </ul>
    <ul><li><strong>Location</strong>: Some venue address later on</li></ul>
    """

    def test_extracts_all_fields(self):
        fields = extract_labeled_fields(self.HTML)
        self.assertEqual(fields["company"], "Accuprec Research Labs Pvt Ltd")
        self.assertEqual(fields["experience"], "0-7 Years (varies by role)")
        self.assertEqual(fields["location"], "Ahmedabad, India")
        self.assertEqual(fields["work_type"], "Full-time, On-site")

    def test_first_value_wins(self):
        # Job Details list comes first; a later venue list must not override.
        fields = extract_labeled_fields(self.HTML)
        self.assertEqual(fields["location"], "Ahmedabad, India")

    def test_organization_label_maps_to_company(self):
        # Real shape from post 39709 (UPSC/CDSCO government posting).
        html = "<ul><li><strong>Organization</strong>: CDSCO</li></ul>"
        self.assertEqual(extract_labeled_fields(html)["company"], "CDSCO")

    def test_ignores_unlabeled_bullets(self):
        html = "<ul><li>Just a sentence with no colon</li></ul>"
        self.assertEqual(extract_labeled_fields(html), {})


class TestParseSalary(unittest.TestCase):
    def test_empty_returns_empty_dict(self):
        self.assertEqual(parse_salary(""), {})
        self.assertEqual(parse_salary(None), {})

    def test_no_numbers_keeps_raw_only(self):
        result = parse_salary("Best in Industry")
        self.assertEqual(result["salary_raw"], "Best in Industry")
        self.assertNotIn("salary_min", result)

    def test_lpa_range(self):
        result = parse_salary("₹3.5 – 5 LPA")
        self.assertEqual(result["salary_min"], 350_000)
        self.assertEqual(result["salary_max"], 500_000)
        self.assertEqual(result["salary_period"], "per_annum")
        self.assertEqual(result["salary_currency"], "INR")

    def test_monthly_amount(self):
        result = parse_salary("Rs. 25,000 per month")
        self.assertEqual(result["salary_min"], 25_000)
        self.assertEqual(result["salary_max"], 25_000)
        self.assertEqual(result["salary_period"], "per_month")

    def test_k_suffix_range(self):
        result = parse_salary("15k-20k")
        self.assertEqual(result["salary_min"], 15_000)
        self.assertEqual(result["salary_max"], 20_000)
        self.assertEqual(result["salary_period"], "per_month")

    def test_indian_grouping_per_annum(self):
        result = parse_salary("4,50,000 P.A.")
        self.assertEqual(result["salary_min"], 450_000)
        self.assertEqual(result["salary_period"], "per_annum")

    def test_bare_large_amount_reads_per_annum(self):
        self.assertEqual(parse_salary("450000")["salary_period"], "per_annum")

    def test_bare_small_amount_reads_per_month(self):
        self.assertEqual(parse_salary("30,000")["salary_period"], "per_month")

    def test_swapped_range_is_ordered(self):
        result = parse_salary("5 - 3 LPA")
        self.assertEqual(result["salary_min"], 300_000)
        self.assertEqual(result["salary_max"], 500_000)

    def test_foreign_currency_keeps_raw_only(self):
        result = parse_salary("AED 5,000 per month")
        self.assertIn("salary_raw", result)
        self.assertNotIn("salary_min", result)


class TestParseExperience(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_experience("0-7 Years (varies by role)"), (0, 7))

    def test_fresher_only(self):
        self.assertEqual(parse_experience("Fresher Only"), (0, 0))

    def test_freshers_to_n_years(self):
        # parenthetical year "(2026 pass-outs)" must not be read as experience
        self.assertEqual(
            parse_experience("Freshers (2026 pass-outs) to 8 Years"), (0, 8))

    def test_min_years(self):
        self.assertEqual(parse_experience("Min 3 years"), (3, ""))

    def test_plus_years(self):
        self.assertEqual(parse_experience("5+ years"), (5, ""))

    def test_empty(self):
        self.assertEqual(parse_experience(""), ("", ""))

    def test_unparseable(self):
        self.assertEqual(parse_experience("As per role"), ("", ""))


class TestApplyClassification(unittest.TestCase):
    """Wiring tests for the shared two-level classifier.

    The engine itself is covered by _shared/test_classification.py; these
    only assert that this scraper feeds it the right signals and stamps
    the verdict onto the rich row.
    """

    @staticmethod
    def _row(title, site_categories="", description="", needs_review=False):
        return {"title": title, "site_categories": site_categories,
                "description": description, "needs_review": needs_review}

    def test_in_scope_role_is_kept_and_labelled(self):
        row = self._row("Regulatory Affairs Executive - Dossier Submission",
                        "jobs; regulatory-jobs")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Regulatory Affairs")
        self.assertEqual(row["role_family"], "Regulatory Affairs")

    def test_public_health_role(self):
        row = self._row("Epidemiologist - District Surveillance Unit", "jobs")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Epidemiology")

    def test_bedside_title_is_dropped(self):
        row = self._row("Staff Nurse Openings in Mumbai", "jobs")
        self.assertFalse(apply_classification(row))

    def test_production_walkin_is_out_of_scope(self):
        # Manufacturing / QC / R&D is not one of the eleven families.
        row = self._row(
            "Walk-in Interview: Production, QC, ADL & R&D Jobs at Ami Lifesciences",
            "jobs; production-jobs; qc-jobs")
        self.assertFalse(apply_classification(row))

    def test_local_needs_review_flag_survives(self):
        row = self._row("Clinical Data Manager", "jobs", needs_review=True)
        self.assertTrue(apply_classification(row))
        self.assertTrue(row["needs_review"])


class TestClassifiers(unittest.TestCase):
    def test_newsy_title_is_junk_detection_only(self):
        # feeds needs_review; it is not a category decider
        self.assertTrue(_NEWSY_TITLE_RE.search("Top 10 Pharma Companies in India"))
        self.assertFalse(_NEWSY_TITLE_RE.search("Clinical Data Manager"))

    def test_company_type_defaults_to_pharma(self):
        self.assertEqual(classify_company_type("Macleods Pharmaceuticals", ""),
                         "pharma")

    def test_hospital_company(self):
        self.assertEqual(classify_company_type("Apollo Hospital", ""), "hospital")

    def test_job_type_mapping(self):
        self.assertEqual(classify_job_type("Full-time, On-site"), "full_time")
        self.assertEqual(classify_job_type("Remote / Work From Home"), "remote")
        self.assertEqual(classify_job_type("Hybrid"), "hybrid")
        self.assertEqual(classify_job_type("Part-time"), "part_time")
        self.assertEqual(classify_job_type(""), "full_time")


class TestParseLocation(unittest.TestCase):
    def test_city_india(self):
        self.assertEqual(parse_location("Ahmedabad, India"),
                         ("Ahmedabad", "India", "IN", "+91"))

    def test_city_state(self):
        city, country, code, dial = parse_location(
            "Karakhadi & Ankleshwar, Gujarat")
        self.assertEqual(city, "Karakhadi & Ankleshwar")
        self.assertEqual(code, "IN")

    def test_abroad(self):
        self.assertEqual(parse_location("Dubai, UAE"),
                         ("Dubai", "UAE", "AE", "+971"))

    def test_not_specified(self):
        self.assertEqual(parse_location("Not specified (India-based)"),
                         ("", "India", "IN", "+91"))

    def test_slash_alternatives_first_wins(self):
        city, _, code, _ = parse_location(
            "New Delhi (Interviews) / Various locations across India (Posting)")
        self.assertEqual(city, "New Delhi")
        self.assertEqual(code, "IN")

    def test_empty(self):
        self.assertEqual(parse_location(""), ("", "India", "IN", "+91"))


class TestComputeCutoff(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-20", "bogus"]})
        expected = (date(2026, 7, 20) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_all_bogus_dates_fall_back_to_initial_window(self):
        df = pd.DataFrame({"posted_date": ["bogus", ""]})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
