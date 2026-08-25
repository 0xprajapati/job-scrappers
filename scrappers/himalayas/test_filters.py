#!/usr/bin/env python3
"""Unit tests for the himalayas.app scraper's parsers, classifier and cutoff.

Run with plain `python test_filters.py` (no pytest needed).

Every example marked "real listing" was taken verbatim from a 1,560-job
sample of https://himalayas.app/jobs/api pulled on 2026-07-29.
"""

import sys
import unittest
from datetime import date

import pandas as pd

from himalayas_scraper import (
    DESCRIPTION_MAX_CHARS,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    classify_company_type,
    classify_feed_job,
    clean_value,
    compute_cutoff,
    epoch_to_date,
    extract_qualification,
    job_id_from_guid,
    parse_listish,
    parse_salary,
    rich_row_to_club_row,
    strip_html,
    truncate_description,
)


class TestParseListish(unittest.TestCase):
    """The feed ships lists as Python-repr STRINGS, not JSON arrays."""

    def test_repr_string_single(self):
        # real listing: Galileo, Inc. — Nurse Practitioner
        self.assertEqual(parse_listish("['United States']"), ["United States"])

    def test_repr_string_multi(self):
        # real listing: Tia — Nurse Practitioner / Physician Assistant
        self.assertEqual(
            parse_listish("['India', 'United Kingdom', 'United States']"),
            ["India", "United Kingdom", "United States"])

    def test_empty_forms(self):
        for value in ("[]", "None", "", None):
            self.assertEqual(parse_listish(value), [], repr(value))

    def test_real_list_passthrough(self):
        self.assertEqual(parse_listish(["Healthcare"]), ["Healthcare"])

    def test_ints_become_strings(self):
        # real listing: timezoneRestrictions = "[1]"
        self.assertEqual(parse_listish("[1]"), ["1"])

    def test_malformed_never_raises(self):
        self.assertEqual(parse_listish("['unterminated"), [])


class TestCleanValue(unittest.TestCase):
    """Missing values arrive as the literal string "None"."""

    def test_string_none_is_empty(self):
        self.assertEqual(clean_value("None"), "")

    def test_real_value_kept(self):
        self.assertEqual(clean_value("  Full Time  "), "Full Time")

    def test_actual_none(self):
        self.assertEqual(clean_value(None), "")


class TestEpochToDate(unittest.TestCase):
    def test_real_pubdate(self):
        # real listing: pubDate 1785311944 -> 2026-07-29 07:59 UTC
        self.assertEqual(epoch_to_date(1785311944), "2026-07-29")

    def test_string_epoch(self):
        self.assertEqual(epoch_to_date("1785311944"), "2026-07-29")

    def test_expiry(self):
        # real listing: expiryDate 1790495944
        self.assertEqual(epoch_to_date(1790495944), "2026-09-27")

    def test_missing_forms(self):
        for value in ("None", "", None, 0, "abc"):
            self.assertEqual(epoch_to_date(value), "", repr(value))


class TestJobId(unittest.TestCase):
    def test_trailing_numeric_id(self):
        guid = ("https://himalayas.app/companies/bjak/jobs/"
                "android-software-engineer-ai-neobank-app-5243463048")
        self.assertEqual(job_id_from_guid(guid), "5243463048")

    def test_no_numeric_id_falls_back_to_company_and_slug(self):
        guid = "https://himalayas.app/companies/acme/jobs/staff-nurse"
        self.assertEqual(job_id_from_guid(guid), "acme/staff-nurse")

    def test_same_slug_at_two_companies_does_not_collide(self):
        a = job_id_from_guid("https://himalayas.app/companies/acme/jobs/registered-nurse")
        b = job_id_from_guid("https://himalayas.app/companies/globex/jobs/registered-nurse")
        self.assertNotEqual(a, b)

    def test_trailing_slash(self):
        guid = "https://himalayas.app/companies/acme/jobs/nurse-123456/"
        self.assertEqual(job_id_from_guid(guid), "123456")

    def test_empty(self):
        self.assertEqual(job_id_from_guid(None), "")

    def test_short_number_is_not_an_id(self):
        # "...-2024" is a year in the slug, not a 6+ digit job id
        guid = "https://himalayas.app/companies/acme/jobs/nurse-cohort-2024"
        self.assertEqual(job_id_from_guid(guid), "acme/nurse-cohort-2024")


class TestParseSalary(unittest.TestCase):
    """Master spec §3: capture, never filter, never invent."""

    def test_usd_annual_range(self):
        self.assertEqual(
            parse_salary(120000, 150000, "USD", "annual"),
            ("USD 120000 - 150000 per annum", "120000", "150000",
             "USD", "per_annum"))

    def test_undisclosed_is_not_disclosed(self):
        # real listing shape: all three fields are the STRING "None"
        self.assertEqual(parse_salary("None", "None", "None", "annual"),
                         ("Not Disclosed", "", "", "", ""))

    def test_single_sided_min_only(self):
        raw, lo, hi, cur, per = parse_salary(90000, "None", "USD", "annual")
        self.assertEqual((lo, hi), ("90000", "90000"))
        self.assertEqual((cur, per), ("USD", "per_annum"))

    def test_hourly_period_mapped(self):
        _, lo, hi, cur, per = parse_salary(55, 75, "USD", "hourly")
        self.assertEqual((lo, hi, cur, per), ("55", "75", "USD", "per_hour"))

    def test_monthly_period_mapped(self):
        _, _, _, _, per = parse_salary(4000, 6000, "USD", "monthly")
        self.assertEqual(per, "per_month")

    def test_inverted_range_is_swapped(self):
        _, lo, hi, _, _ = parse_salary(150000, 120000, "USD", "annual")
        self.assertEqual((lo, hi), ("120000", "150000"))

    def test_floats_rounded_to_int(self):
        _, lo, hi, _, _ = parse_salary(99999.6, 120000.2, "USD", "annual")
        self.assertEqual((lo, hi), ("100000", "120000"))

    def test_non_club_currency_still_captured(self):
        # CAD can't go in the club CSV, but it must survive in the rich CSV
        raw, lo, _, cur, _ = parse_salary(80000, 95000, "CAD", "annual")
        self.assertEqual((lo, cur), ("80000", "CAD"))
        self.assertIn("CAD", raw)

    def test_never_invents_a_salary(self):
        raw, lo, hi, cur, per = parse_salary(None, None, None, None)
        self.assertEqual((raw, lo, hi, cur, per),
                         ("Not Disclosed", "", "", "", ""))


class TestSharedClassifierWiring(unittest.TestCase):
    """classify_feed_job routes everything through _shared/classification.

    Only the WIRING is tested here (signals in, verdict fields out); the
    engine itself is covered by _shared/test_classification.py.
    """

    def test_in_scope_role_gets_two_level_category(self):
        v = classify_feed_job("Senior Medical Writer")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["category"], "Non Clinical")
        self.assertEqual(v["sub_category"], "Medical Writer")
        self.assertEqual(v["role_family"], "Medical Writer")

    def test_public_health_split(self):
        v = classify_feed_job("Population Health Program Coordinator")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["category"], "Public Health")
        self.assertEqual(v["sub_category"], "Public Health Program Management")

    def test_out_of_scope_title_is_dropped(self):
        v = classify_feed_job("Android Software Engineer - AI Neobank App")
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["category"], "")

    def test_vetoed_title_is_dropped(self):
        v = classify_feed_job(
            "Senior Medical Billing Specialist", ["medical-billing"],
            "ICD-10 coding review, CPT charge capture, coding compliance.")
        self.assertFalse(v["in_scope"])
        self.assertTrue(v["vetoed_by"])

    def test_slugs_alone_cannot_admit(self):
        # feed slugs are auto-tagged and loose: passed only as the weighted
        # skills signal, they must not admit a clinical title on their own
        v = classify_feed_job("Registered Dietitian", ["Clinical-Research"])
        self.assertFalse(v["in_scope"])

    def test_slugs_are_flattened_to_scorer_text(self):
        # hyphenated slugs must still count as a skills signal
        v = classify_feed_job("Senior Director, DSPV",
                              ["drug-safety", "pharmacovigilance"],
                              "ICSR case processing and signal detection.")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["role_family"], "Pharmacovigilance")


class TestCompanyType(unittest.TestCase):
    def test_pharma_company(self):
        self.assertEqual(classify_company_type("Lambda Therapeutic Research"),
                         "pharma")

    def test_biotech_company(self):
        self.assertEqual(classify_company_type("Acme Biotech Labs"), "pharma")

    def test_default_hospital(self):
        # real listing: OptiMindHealth
        self.assertEqual(classify_company_type("OptiMindHealth"), "hospital")


class TestStripHtml(unittest.TestCase):
    def test_tags_removed_and_entities_decoded(self):
        html = "<h3><strong>About KIRA</strong></h3><p>Money &amp; more.</p>"
        self.assertEqual(strip_html(html), "About KIRA Money & more.")

    def test_empty(self):
        self.assertEqual(strip_html(None), "")


class TestTruncateDescription(unittest.TestCase):
    """A real 9,868-char description must survive intact."""

    def test_cap_is_above_the_real_maximum(self):
        # live sample: max 9,868 plain-text chars, median ~3,859
        self.assertGreaterEqual(DESCRIPTION_MAX_CHARS, 10_000)

    def test_typical_description_is_untouched(self):
        text = "word " * 1000          # 5,000 chars — was truncated at 3,000
        self.assertEqual(truncate_description(text), text)

    def test_longest_observed_description_is_untouched(self):
        text = "x" * 9868
        self.assertEqual(truncate_description(text), text)

    def test_over_limit_is_marked(self):
        out = truncate_description("word " * 100, limit=50)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 51)

    def test_truncation_respects_word_boundary(self):
        out = truncate_description("alpha beta gamma delta", limit=14)
        self.assertEqual(out, "alpha beta…")   # not "alpha beta gam…"

    def test_no_word_boundary_still_truncates(self):
        # a single unbroken token must not collapse to just "…"
        out = truncate_description("x" * 100, limit=10)
        self.assertEqual(out, "x" * 10 + "…")

    def test_empty(self):
        self.assertEqual(truncate_description(None), "")


class TestCutoff(unittest.TestCase):
    """Master spec §4."""

    def test_initial_window_is_two_days(self):
        # narrowed from the spec's default of 7 — see the constant's comment
        self.assertEqual(INITIAL_WINDOW_DAYS, 2)

    def test_first_run_uses_initial_window(self):
        cutoff = compute_cutoff(None, today=date(2026, 7, 29))
        self.assertEqual(cutoff, "2026-07-27")   # 2 days back

    def test_empty_csv_uses_initial_window(self):
        df = pd.DataFrame({"posted_date": []})
        self.assertEqual(compute_cutoff(df, today=date(2026, 7, 29)),
                         "2026-07-27")

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-20", "2026-07-25",
                                           "2026-07-22"]})
        # newest (07-25) minus WATERMARK_GRACE_DAYS
        self.assertEqual(compute_cutoff(df), "2026-07-23")
        self.assertEqual(WATERMARK_GRACE_DAYS, 2)

    def test_since_overrides_the_watermark(self):
        # a re-run must be able to re-walk the full window despite a
        # populated CSV, or drift-missed jobs can never be recovered
        df = pd.DataFrame({"posted_date": ["2026-07-28", "2026-07-29"]})
        self.assertEqual(compute_cutoff(df, since="2026-07-22"), "2026-07-22")

    def test_since_overrides_initial_window_too(self):
        self.assertEqual(
            compute_cutoff(None, today=date(2026, 7, 29), since="2026-07-01"),
            "2026-07-01")

    def test_unparseable_dates_ignored(self):
        df = pd.DataFrame({"posted_date": ["not-a-date", "2026-07-25"]})
        self.assertEqual(compute_cutoff(df), "2026-07-23")


class TestClubMapping(unittest.TestCase):
    """The 22-column HealthCareers.club contract."""

    BASE = {
        "title": "Nurse Practitioner", "company": "Galileo, Inc.",
        "company_type": "hospital", "company_logo": "https://cdn/x.png",
        "country": "United States", "category": "Non Clinical",
        "sub_category": "Clinical Research", "role_family": "Clinical Research",
        "description": "Provide virtual primary care.",
        "job_url": "https://himalayas.app/companies/galileo/jobs/np-123456",
        "posted_date": "2026-07-29", "expires_date": "2026-09-27",
        "salary_min": "120000", "salary_max": "150000",
        "salary_currency": "USD", "salary_period": "per_annum",
    }

    def test_country_lookup(self):
        row = rich_row_to_club_row(self.BASE)
        self.assertEqual((row["country_name"], row["country_code"],
                          row["country_dial_code"]),
                         ("United States", "US", "+1"))

    def test_india_lookup(self):
        row = rich_row_to_club_row(dict(self.BASE, country="India"))
        self.assertEqual((row["country_code"], row["country_dial_code"]),
                         ("IN", "+91"))

    def test_unknown_country_leaves_codes_empty(self):
        row = rich_row_to_club_row(dict(self.BASE, country="Atlantis"))
        self.assertEqual((row["country_name"], row["country_code"]),
                         ("Atlantis", ""))

    def test_job_type_is_always_remote(self):
        self.assertEqual(rich_row_to_club_row(self.BASE)["job_type"], "remote")

    def test_city_is_remote(self):
        self.assertEqual(rich_row_to_club_row(self.BASE)["city_name"], "Remote")

    def test_usd_salary_exported(self):
        row = rich_row_to_club_row(self.BASE)
        self.assertEqual((row["min_salary"], row["max_salary"],
                          row["salary_period"], row["salary_currency"]),
                         ("120000", "150000", "per_annum", "USD"))

    def test_cad_salary_not_exported(self):
        # club salary_currency enum is INR/USD only — don't misdeclare CAD
        row = rich_row_to_club_row(dict(self.BASE, salary_currency="CAD"))
        self.assertEqual((row["min_salary"], row["salary_currency"]), ("", ""))

    def test_hourly_salary_not_exported(self):
        # club salary_period enum has no per_hour
        row = rich_row_to_club_row(dict(self.BASE, salary_period="per_hour"))
        self.assertEqual((row["min_salary"], row["salary_period"]), ("", ""))

    def test_undisclosed_salary_stays_empty(self):
        row = rich_row_to_club_row(dict(self.BASE, salary_min="",
                                        salary_max="", salary_currency="",
                                        salary_period=""))
        self.assertEqual((row["min_salary"], row["max_salary"]), ("", ""))

    def test_experience_never_invented(self):
        row = rich_row_to_club_row(self.BASE)
        self.assertEqual((row["min_experience"], row["max_experience"]), ("", ""))

    def test_enum_values_are_valid(self):
        row = rich_row_to_club_row(self.BASE)
        self.assertIn(row["company_type"], ("hospital", "pharma"))
        self.assertIn(row["job_type"], ("full_time", "part_time", "remote",
                                        "hybrid"))
        self.assertIn(row["category"], ("Non Clinical", "Public Health"))

    def test_club_columns_match_the_current_contract(self):
        # The 22-column contract is imported from _shared/classification —
        # never hand-copied — and carries category + sub_category but no
        # is_active / expires_at.
        from himalayas_scraper import CLUB_COLUMNS
        self.assertIn("qualification", CLUB_COLUMNS)
        self.assertIn("sub_category", CLUB_COLUMNS)
        self.assertNotIn("is_active", CLUB_COLUMNS)
        self.assertNotIn("expires_at", CLUB_COLUMNS)
        self.assertEqual(len(CLUB_COLUMNS), 22)
        self.assertEqual(sorted(rich_row_to_club_row(self.BASE)),
                         sorted(CLUB_COLUMNS))

    def test_nan_scrubbed(self):
        row = rich_row_to_club_row(dict(self.BASE, company_logo=float("nan")))
        self.assertEqual(row["company_logo"], "")


class TestExtractQualification(unittest.TestCase):
    """Grounded extraction only — never inferred from the title."""

    def test_pulls_explicit_credentials(self):
        out = extract_qualification(
            "Requires a PharmD or B.Pharm and 5 years of pharmacovigilance work.")
        self.assertIn("PharmD", out)
        self.assertIn("B.Pharm", out)

    def test_degree_phrases(self):
        self.assertIn("Bachelor's degree",
                      extract_qualification("A Bachelor's degree is required."))

    def test_life_sciences(self):
        self.assertIn("Life Sciences",
                      extract_qualification("Degree in life sciences preferred."))

    def test_empty_when_nothing_stated(self):
        self.assertEqual(
            extract_qualification("You will manage timelines and stakeholders."), "")

    def test_no_description(self):
        self.assertEqual(extract_qualification(""), "")
        self.assertEqual(extract_qualification(None), "")

    def test_maryland_is_not_a_medical_degree(self):
        # "MD" as a US state abbreviation must not become a qualification
        self.assertEqual(extract_qualification("Remote role based in Bethesda, MD."), "")

    def test_do_the_verb_is_not_a_degree(self):
        self.assertEqual(extract_qualification("You will do great work here."), "")

    def test_md_with_periods_is_recognised(self):
        self.assertIn("MD", extract_qualification("An M.D. is required."))

    def test_capped(self):
        text = ("MBBS PharmD PhD MPH DVM BSN MSN MSc BSc MBA RN RAC CCRA "
                "CCRP RHIA RHIT CPC CCS")
        self.assertLessEqual(len(extract_qualification(text).split(", ")), 8)


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0], "-v"], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
