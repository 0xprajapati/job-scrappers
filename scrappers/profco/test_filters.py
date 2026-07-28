#!/usr/bin/env python3
"""Unit tests for the profco scraper's parsers — run with
`python test_filters.py`.

Every example below is copied from a real profco.com fragment (job ids are
noted where it helps).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    WATERMARK_GRACE_DAYS, classify_category, classify_company_type,
    clean_text, compute_cutoff, job_type_from, normalize_city, parse_amount,
    parse_contract_type, parse_detail, parse_experience_years, parse_job_links,
    parse_options, parse_salary, resolve_country, rich_row_to_club_row,
    strip_html, within_window,
)


class TestListingParsing(unittest.TestCase):
    FRAGMENT = (
        '<ul class="joblist">\n'
        '<li><a href="index.php?job_id=15904" title="SAUDI ARABIA Registered '
        'Nurse &ndash; Emergency Department (ED) | Qassim" '
        'onclick="loadContent({ job_id: 15904 }); return false;">SAUDI ARABIA '
        'Registered Nurse</a></li>'
        '<li><a href="index.php?job_id=15847" title="Mental Health Nursing '
        'Jobs in Sydney ( SID 482 sponsorship)" '
        'onclick="loadContent({ job_id: 15847 }); return false;">Mental Health'
        '</a></li></ul>')

    def test_ids_titles_and_urls(self):
        jobs = parse_job_links(self.FRAGMENT)
        self.assertEqual([job["job_id"] for job in jobs], ["15904", "15847"])
        self.assertEqual(jobs[0]["job_url"],
                         "https://www.profco.com/index.php?job_id=15904")
        self.assertTrue(jobs[0]["listing_title"].startswith("SAUDI ARABIA"))

    def test_html_entities_unescaped_in_titles(self):
        self.assertIn("–", parse_job_links(self.FRAGMENT)[0]["listing_title"])

    def test_duplicate_links_collapse(self):
        doubled = self.FRAGMENT + self.FRAGMENT
        self.assertEqual(len(parse_job_links(doubled)), 2)

    def test_empty_result_set(self):
        self.assertEqual(parse_job_links('<ul class="joblist"></ul>'), [])
        self.assertEqual(parse_job_links(None), [])

    def test_speciality_select_drops_placeholder(self):
        select = ('<select name="jobspeciality_id"><option value="0">'
                  '-- Select Speciality --</option><option value="77">'
                  'Emergency</option><option value="150">Theatres/OR/Peri op'
                  '</option></select>')
        self.assertEqual(parse_options(select),
                         [("77", "Emergency"), ("150", "Theatres/OR/Peri op")])


class TestDetailParsing(unittest.TestCase):
    # Trimmed from getContent for job 15904.
    FRAGMENT = (
        '<div id="jobsWide"><table><tr><td></td></tr><tr><td>'
        '<h1>#15904 Registered Nurse &ndash; Emergency Department (ED) | '
        'Qassim</h1></td></tr></table>'
        '<table id="jobTable" class="job">'
        '<tr><th>Category</th><td>Nursing and Midwifery</td></tr>'
        '<tr><th>Speciality</th><td>Emergency</td></tr>'
        '<tr><th>Location</th><td>Qassim</td></tr>'
        '<tr><th>Salary min</th><td>On Application SAR</td></tr>'
        '<tr><th>Salary max</th><td>On Application SAR</td></tr>'
        '<tr><th>Hospital</th><td>Newly commissioned hospital<br />300 beds'
        '</td></tr>'
        '<tr><th>Description</th><td></td></tr>'
        '<tr><th>Requirements </th><td>Bachelor&#39;s Degree in Nursing<br />'
        'Minimum of two (2) years of current clinical nursing experience'
        '</td></tr></table></div>')

    def setUp(self):
        self.detail = parse_detail(self.FRAGMENT)

    def test_title_without_id_prefix(self):
        self.assertEqual(self.detail["title"],
                         "Registered Nurse – Emergency Department (ED) | Qassim")

    def test_fields_keyed_lowercase_and_trimmed(self):
        self.assertEqual(self.detail["category"], "Nursing and Midwifery")
        self.assertEqual(self.detail["salary min"], "On Application SAR")
        self.assertIn("requirements", self.detail)   # source label has a space

    def test_html_values_flatten(self):
        self.assertEqual(strip_html(self.detail["hospital"]),
                         "Newly commissioned hospital 300 beds")

    def test_missing_fragment_is_safe(self):
        self.assertEqual(parse_detail("")["title"], "")


class TestSalary(unittest.TestCase):
    def test_on_application_is_not_disclosed(self):
        salary = parse_salary("On Application SAR", "On Application SAR")
        self.assertEqual(salary["salary_raw"], "Not Disclosed")
        self.assertEqual(salary["salary_min"], "")
        self.assertEqual(salary["salary_max"], "")
        self.assertEqual(salary["salary_currency"], "")

    def test_australian_range(self):
        # job 15847: Sydney mental-health nursing
        salary = parse_salary("86434 AUD", "103979 AUD")
        self.assertEqual(salary["salary_min"], 86434)
        self.assertEqual(salary["salary_max"], 103979)
        self.assertEqual(salary["salary_currency"], "AUD")
        self.assertEqual(salary["salary_period"], "per_annum")
        self.assertEqual(salary["salary_raw"], "AUD 86,434 - 103,979")

    def test_period_is_inferred_not_sourced(self):
        self.assertEqual(parse_salary("86434 AUD", "103979 AUD")
                         ["salary_period_original"], "")

    def test_small_figures_read_as_monthly(self):
        self.assertEqual(parse_salary("9000 SAR", "12000 SAR")
                         ["salary_period"], "per_month")

    def test_single_amount_becomes_min_and_max(self):
        salary = parse_salary("74317 AUD", "")
        self.assertEqual((salary["salary_min"], salary["salary_max"]),
                         (74317, 74317))
        self.assertEqual(salary["salary_raw"], "AUD 74,317")

    def test_reversed_bounds_are_ordered(self):
        salary = parse_salary("103979 AUD", "86434 AUD")
        self.assertEqual((salary["salary_min"], salary["salary_max"]),
                         (86434, 103979))

    def test_thousand_separators(self):
        self.assertEqual(parse_amount("1,200 GBP"), 1200)

    def test_zero_is_not_a_salary(self):
        self.assertIsNone(parse_amount("0 SAR"))
        self.assertEqual(parse_salary("0 SAR", "0 SAR")["salary_raw"],
                         "Not Disclosed")


class TestLocation(unittest.TestCase):
    def test_known_typo_fixed(self):
        self.assertEqual(normalize_city("Riaydh"), "Riyadh")

    def test_same_city_spelled_two_ways_collapses(self):
        # the board carries both "Al Madinah" and "Medina"
        self.assertEqual(normalize_city("Medina"), "Al Madinah")
        self.assertEqual(normalize_city("Al Madinah"), "Al Madinah")

    def test_prefix_and_suffix_forms(self):
        self.assertEqual(normalize_city("Al Qassim"), "Qassim")
        self.assertEqual(normalize_city("Riyadh,"), "Riyadh")
        self.assertEqual(normalize_city("Sydney Eastern Surburbs"), "Sydney")

    def test_unknown_city_kept_verbatim(self):
        self.assertEqual(normalize_city(" Various States "), "Various States")

    def test_country_from_currency(self):
        self.assertEqual(resolve_country("SAR", "Riyadh", ""), "Saudi Arabia")
        self.assertEqual(resolve_country("AUD", "Sydney", ""), "Australia")
        self.assertEqual(resolve_country("GBP", "London", ""),
                         "United Kingdom")

    def test_usd_falls_through_to_city(self):
        # Saudi posts are quoted in SAR *or* USD (e.g. job 15872).
        self.assertEqual(resolve_country("USD", "Riyadh", ""), "Saudi Arabia")

    def test_title_prefix_is_last_resort(self):
        self.assertEqual(
            resolve_country("USD", "Somewhere else",
                            "SAUDI ARABIA | Vascular Access Co-ordinator"),
            "Saudi Arabia")

    def test_unresolvable_country_is_blank(self):
        self.assertEqual(resolve_country("USD", "Somewhere else", "Nurse"), "")


class TestClassifier(unittest.TestCase):
    def test_site_category_wins_for_clinical_buckets(self):
        self.assertEqual(
            classify_category("Nursing and Midwifery",
                              "Nurse Manager – Medical | Qassim"),
            ("nurses", False))
        self.assertEqual(
            classify_category("Medical Doctor", "Consultant - Gastroenterology"),
            ("doctors", False))
        self.assertEqual(classify_category("Pharmacist", "Clinical Pharmacist"),
                         ("pharmacists", False))

    def test_allied_health_is_non_clinical_but_not_flagged(self):
        self.assertEqual(
            classify_category("Allied Health Professionals",
                              "Laboratory Technologist - Transfusion"),
            ("non_clinical", False))

    def test_generic_category_classified_from_title(self):
        self.assertEqual(classify_category("Other", "Registered Nurse - ICU"),
                         ("nurses", False))
        self.assertEqual(classify_category("Other", "Consultant Radiologist"),
                         ("doctors", False))

    def test_unclassifiable_title_is_kept_and_flagged(self):
        category, needs_review = classify_category("Other", "Project Manager")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)          # kept, never dropped (§2)

    def test_healthcare_signal_clears_the_flag(self):
        category, needs_review = classify_category(
            "Administration - Management", "Hospital Facilities Coordinator")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(needs_review)

    def test_company_type_defaults_to_hospital(self):
        self.assertEqual(
            classify_company_type("Registered Nurse - ICU",
                                  "Nursing and Midwifery", "Tertiary hospital"),
            "hospital")
        self.assertEqual(
            classify_company_type("Clinical Research Associate", "Other", ""),
            "pharma")


class TestJobTypeAndExperience(unittest.TestCase):
    def test_default_is_full_time(self):
        self.assertEqual(job_type_from("Registered Nurse - Surgical Ward"),
                         "full_time")

    def test_part_time_detected(self):
        self.assertEqual(job_type_from("Part-time Midwife"), "part_time")

    def test_locum_contract_kept_separately(self):
        # club enum has no "contract" value, so job_type stays full_time
        title = "Locum 90-day contract_Registered Nurse - BMT"
        self.assertEqual(job_type_from(title), "full_time")
        self.assertEqual(parse_contract_type(title),
                         "locum/short-term contract")

    def test_day_contract_without_the_word_locum(self):
        self.assertEqual(
            parse_contract_type("Registered Nurse - 90 day contract"),
            "90-day contract")

    def test_permanent_role_has_no_contract_type(self):
        self.assertEqual(parse_contract_type("Nurse Manager – Medical"), "")

    def test_experience_spelled_and_digit_forms(self):
        self.assertEqual(parse_experience_years(
            "Minimum of two (2) years of current clinical nursing experience"),
            "2")
        self.assertEqual(parse_experience_years(
            "A minimum of 3+ years full-time post-graduate experience in the "
            "UK or Ireland."), "3")

    def test_smallest_requirement_wins(self):
        self.assertEqual(parse_experience_years(
            "5 years total, of which 2 years in ICU"), "2")

    def test_visa_numbers_are_not_experience(self):
        self.assertEqual(parse_experience_years(
            "Sponsorship under the TSS (subclass 482) visa"), "")

    def test_absurd_figures_ignored(self):
        self.assertEqual(parse_experience_years("Hospital founded 100 years "
                                                "ago"), "")

    def test_no_requirements_text(self):
        self.assertEqual(parse_experience_years("", ""), "")


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        cutoff = compute_cutoff(None)
        self.assertEqual(cutoff,
                         (date.today() - timedelta(days=30)).isoformat())

    def test_watermark_with_grace(self):
        newest = date.today() - timedelta(days=3)
        df = pd.DataFrame({"posted_date": [
            (newest - timedelta(days=5)).isoformat(), newest.isoformat()]})
        self.assertEqual(
            compute_cutoff(df),
            (newest - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_unparseable_dates_fall_back(self):
        df = pd.DataFrame({"posted_date": ["", "not a date"]})
        self.assertEqual(compute_cutoff(df),
                         (date.today() - timedelta(days=30)).isoformat())

    def test_first_seen_dates_are_always_in_window(self):
        # posted_date = first-seen date, so today's finds always pass.
        self.assertTrue(within_window(date.today().isoformat(),
                                      compute_cutoff(None)))

    def test_older_than_cutoff_excluded(self):
        old = (date.today() - timedelta(days=90)).isoformat()
        self.assertFalse(within_window(old, compute_cutoff(None)))

    def test_missing_date_excluded(self):
        self.assertFalse(within_window("", compute_cutoff(None)))


class TestClubRow(unittest.TestCase):
    BASE = {
        "job_id": "15847", "title": "Mental Health Nursing Jobs in Sydney",
        "company": "Profco (Professional Connections)",
        "company_about": "NSW Health hospitals", "city": "Sydney",
        "country": "Australia", "country_code": "AU",
        "country_dial_code": "+61", "category": "nurses",
        "company_type": "hospital", "job_type": "full_time",
        "description": "Our client hospitals are in Sydney.",
        "requirements": "3+ years experience", "benefits": "482 sponsorship",
        "min_experience_years": "3", "posted_date": "2026-07-27",
        "job_url": "https://www.profco.com/index.php?job_id=15847",
        "salary_min": 86434, "salary_max": 103979,
        "salary_period": "per_annum", "salary_currency": "AUD",
    }

    def test_aud_salary_not_exported(self):
        # club schema allows INR/USD only — amounts are never converted
        row = rich_row_to_club_row(self.BASE)
        self.assertEqual(row["min_salary"], "")
        self.assertEqual(row["salary_currency"], "")
        self.assertEqual(row["salary_period"], "")

    def test_usd_salary_exported(self):
        row = rich_row_to_club_row(dict(self.BASE, salary_currency="USD"))
        self.assertEqual(row["min_salary"], "86434")
        self.assertEqual(row["max_salary"], "103979")
        self.assertEqual(row["salary_currency"], "USD")

    def test_description_merges_requirements_and_benefits(self):
        description = rich_row_to_club_row(self.BASE)["description"]
        self.assertIn("Our client hospitals", description)
        self.assertIn("Requirements: 3+ years experience", description)
        self.assertIn("Benefits: 482 sponsorship", description)

    def test_required_columns_present(self):
        row = rich_row_to_club_row(self.BASE)
        for column in ("country_name", "city_name", "company_name", "title",
                       "application_url", "posted_at"):
            self.assertTrue(row[column], column)

    def test_closed_listing_marked_inactive(self):
        self.assertEqual(rich_row_to_club_row(self.BASE, {"15847"})["is_active"],
                         "true")
        self.assertEqual(rich_row_to_club_row(self.BASE, {"999"})["is_active"],
                         "false")

    def test_unknown_active_set_keeps_everything_active(self):
        self.assertEqual(rich_row_to_club_row(self.BASE, None)["is_active"],
                         "true")


class TestTextCleaning(unittest.TestCase):
    def test_strip_html_turns_breaks_into_spaces(self):
        self.assertEqual(
            strip_html("Bachelor&#39;s Degree<br />Current licensure"),
            "Bachelor's Degree Current licensure")

    def test_clean_text_collapses_whitespace(self):
        self.assertEqual(clean_text("  Registered   Nurse \n ICU "),
                         "Registered Nurse ICU")

    def test_none_safe(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(clean_text(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
