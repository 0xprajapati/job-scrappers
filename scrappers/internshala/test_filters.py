#!/usr/bin/env python3
"""Unit tests for the internshala scraper's parsers (plain `python test_filters.py`)."""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (ago_to_date, build_row, classify_category,
                     classify_company_type, compute_cutoff, parse_card_salary,
                     parse_experience, parse_job_posting_ld, parse_ld_salary,
                     parse_listing_cards, rich_row_to_club_row, strip_html,
                     url_is_clean, INITIAL_WINDOW_DAYS, WATERMARK_GRACE_DAYS)

TODAY = date(2026, 7, 24)


class TestAgoToDate(unittest.TestCase):
    """Age strings taken verbatim from live listing cards."""

    def test_today_variants(self):
        for text in ("Today", "Just now", "Few hours ago"):
            self.assertEqual(ago_to_date(text, TODAY), "2026-07-24")

    def test_days_ago(self):
        self.assertEqual(ago_to_date("1 day ago", TODAY), "2026-07-23")
        self.assertEqual(ago_to_date("4 days ago", TODAY), "2026-07-20")

    def test_weeks_ago(self):
        self.assertEqual(ago_to_date("1 week ago", TODAY), "2026-07-17")
        self.assertEqual(ago_to_date("3 weeks ago", TODAY), "2026-07-03")

    def test_month_ago(self):
        self.assertEqual(ago_to_date("1 month ago", TODAY), "2026-06-24")

    def test_unparseable(self):
        self.assertEqual(ago_to_date("", TODAY), "")
        self.assertEqual(ago_to_date("someday", TODAY), "")


class TestParseCardSalary(unittest.TestCase):
    """Salary strings taken verbatim from live listing cards (annual CTC)."""

    def test_range(self):
        out = parse_card_salary("₹ 2,00,000 - 3,00,000")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (200000, 300000))
        self.assertEqual(out["salary_period"], "per_annum")
        self.assertEqual(out["salary_currency"], "INR")

    def test_single_amount(self):
        out = parse_card_salary("₹ 3,00,000")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (300000, 300000))

    def test_competitive_keeps_raw_only(self):
        out = parse_card_salary("Competitive salary")
        self.assertEqual(out["salary_raw"], "Competitive salary")
        self.assertNotIn("salary_min", out)

    def test_foreign_currency_keeps_raw_only(self):
        out = parse_card_salary("£ 2,00,000 - 3,00,000")
        self.assertNotIn("salary_min", out)
        self.assertIn("£", out["salary_raw"])

    def test_empty(self):
        self.assertEqual(parse_card_salary(""), {})

    def test_reversed_range_swapped(self):
        out = parse_card_salary("₹ 3,00,000 - 2,00,000")
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (200000, 300000))


class TestParseLdSalary(unittest.TestCase):
    """baseSalary shapes taken from live detail-page JSON-LD."""

    def test_year_range(self):
        out = parse_ld_salary({"@type": "MonetaryAmount", "currency": "INR",
                               "value": {"@type": "QuantitativeValue",
                                         "minValue": 340000, "maxValue": 480000,
                                         "unitText": "YEAR"}})
        self.assertEqual((out["salary_min"], out["salary_max"]),
                         (340000, 480000))
        self.assertEqual(out["salary_period"], "per_annum")
        self.assertEqual(out["salary_currency"], "INR")

    def test_month_unit(self):
        out = parse_ld_salary({"currency": "INR",
                               "value": {"minValue": 20000, "maxValue": 30000,
                                         "unitText": "MONTH"}})
        self.assertEqual(out["salary_period"], "per_month")

    def test_missing_values(self):
        self.assertEqual(parse_ld_salary(None), {})
        self.assertEqual(parse_ld_salary({}), {})
        self.assertEqual(parse_ld_salary({"currency": "INR", "value": {}}), {})

    def test_foreign_currency_raw_only(self):
        out = parse_ld_salary({"currency": "AED",
                               "value": {"minValue": 5000, "maxValue": 8000,
                                         "unitText": "MONTH"}})
        self.assertNotIn("salary_min", out)
        self.assertIn("AED", out["salary_raw"])


class TestClassify(unittest.TestCase):
    def test_nurse(self):
        cat, review = classify_category("Staff Nurse", ["nurse-jobs"])
        self.assertEqual(cat, "nurses")
        self.assertFalse(review)

    def test_pharmacist(self):
        cat, _ = classify_category("Pharmacist cum Store Manager",
                                   ["pharmacist-jobs"])
        self.assertEqual(cat, "pharmacists")

    def test_doctor(self):
        for title in ("Resident Doctor", "General Physician",
                      "Dermatologist", "Medical Officer"):
            cat, _ = classify_category(title, ["medicine-jobs"])
            self.assertEqual(cat, "doctors", title)

    def test_non_clinical_core_category_not_flagged(self):
        cat, review = classify_category("Trainee Underwriter",
                                        ["medicine-jobs"])
        self.assertEqual(cat, "non_clinical")
        self.assertFalse(review)  # core healthcare category -> trusted

    def test_ambiguous_category_without_keyword_flagged(self):
        cat, review = classify_category("Research Assistant",
                                        ["psychology-jobs"])
        self.assertEqual(cat, "non_clinical")
        self.assertTrue(review)

    def test_ambiguous_category_with_keyword_not_flagged(self):
        _, review = classify_category("Clinical Psychologist",
                                      ["psychology-jobs"])
        self.assertFalse(review)

    def test_mixed_categories_not_flagged(self):
        _, review = classify_category("Research Assistant",
                                      ["psychology-jobs", "medicine-jobs"])
        self.assertFalse(review)

    def test_junk_title_flagged(self):
        _, review = classify_category("Test job do not apply", ["nurse-jobs"])
        self.assertTrue(review)

    def test_company_type(self):
        self.assertEqual(classify_company_type("Apollo Hospitals"), "hospital")
        self.assertEqual(classify_company_type("Sun Pharma Ltd"), "pharma")
        self.assertEqual(classify_company_type("Acme Corp", "Pharmaceutical"),
                         "pharma")


class TestParseExperience(unittest.TestCase):
    """Strings taken verbatim from live listing cards."""

    def test_no_experience(self):
        self.assertEqual(parse_experience("No experience required"), ("0", ""))

    def test_years(self):
        self.assertEqual(parse_experience("1 year(s)"), ("1", ""))
        self.assertEqual(parse_experience("3 year(s)"), ("3", ""))

    def test_range(self):
        self.assertEqual(parse_experience("1-3 years"), ("1", "3"))

    def test_empty(self):
        self.assertEqual(parse_experience(""), ("", ""))


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        expected = (TODAY - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None, TODAY), expected)

    def test_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-20", ""]})
        expected = (date(2026, 7, 20)
                    - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df, TODAY), expected)

    def test_all_bad_dates_falls_back(self):
        df = pd.DataFrame({"posted_date": ["", "not-a-date"]})
        expected = (TODAY - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df, TODAY), expected)


class TestRobotsGuard(unittest.TestCase):
    def test_query_strings_and_commas_blocked(self):
        self.assertFalse(url_is_clean("https://internshala.com/jobs/?p=2"))
        self.assertFalse(url_is_clean("https://internshala.com/jobs/a,b/"))
        self.assertTrue(url_is_clean("https://internshala.com/jobs/page-2/"))


CARD_HTML = """
<div class="container-fluid individual_internship logged_out_jd_summary "
 id="individual_internship_3218759" internshipId="3218759" employment_type="job"
 data-href='/job/detail/fresher-growth-manager-job-at-the-agency-source178471'>
  <h2 class="job-internship-name">
    <a class="job-title-href" id="job_title"
       href="/job/detail/fresher-growth-manager-job-at-the-agency-source178471"
       target="_blank">Growth Manager</a>
  </h2>
  <p class="company-name">
      The Agency Source
  </p>
  <p class="row-1-item  locations">
    <i class="ic-16-map-pin"></i>
    <span><a>Delhi</a>, <a>South</a></span>
  </p>
  <div class="row-1-item">
    <i class="ic-16-money"></i>
    <span class="desktop">
        ₹ 3,00,000
    </span>
  </div>
  <div class="row-1-item">
    <i class="ic-16-briefcase"></i> <span>No experience required</span>
  </div>
  <div class="status-success"><i class="ic-16-reschedule"></i><span>1 day ago</span></div>
</div>
"""

LD_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Growth Manager",
 "description":"<p>About the job:</p>Key responsibilities: <br />1. Do things.",
 "industry":"Healthcare","employmentType":"FULL_TIME",
 "jobLocation":[{"@type":"Place","address":{"@type":"PostalAddress",
   "addressCountry":"IN","addressLocality":"Mumbai","addressRegion":"Maharashtra"}}],
 "hiringOrganization":{"@type":"Organization","name":"The Agency Source"},
 "datePosted":"2026-07-14","validThrough":"2026-08-13 23:59:59",
 "baseSalary":{"@type":"MonetaryAmount","currency":"INR",
   "value":{"@type":"QuantitativeValue","minValue":340000,"maxValue":480000,
            "unitText":"YEAR"}}}
</script>
"""


class TestCardAndRowBuilding(unittest.TestCase):
    def test_parse_listing_card(self):
        cards = parse_listing_cards(CARD_HTML)
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["job_id"], "3218759")
        self.assertEqual(c["title"], "Growth Manager")
        self.assertEqual(c["company"], "The Agency Source")
        self.assertEqual(c["locations"], ["Delhi", "South"])
        self.assertEqual(c["salary_card"], "₹ 3,00,000")
        self.assertEqual(c["experience_raw"], "No experience required")
        self.assertEqual(c["ago"], "1 day ago")
        self.assertTrue(c["job_url"].endswith("source178471"))

    def test_parse_job_posting_ld(self):
        ld = parse_job_posting_ld(LD_HTML)
        self.assertEqual(ld.get("datePosted"), "2026-07-14")

    def test_build_row_merges_detail_over_card(self):
        card = parse_listing_cards(CARD_HTML)[0]
        detail = parse_job_posting_ld(LD_HTML)
        row = build_row(card, {"medicine-jobs"}, detail, TODAY)
        self.assertEqual(row["posted_date"], "2026-07-14")   # exact LD date
        self.assertEqual(row["posted_date_is_estimate"], False)
        self.assertEqual(row["city"], "Mumbai")              # LD wins
        self.assertEqual(row["salary_min"], 340000)          # LD salary wins
        self.assertEqual(row["valid_through"], "2026-08-13")
        self.assertEqual(row["job_type"], "full_time")
        self.assertIn("Do things", row["description"])

    def test_build_row_card_only_fallback(self):
        card = parse_listing_cards(CARD_HTML)[0]
        row = build_row(card, {"medicine-jobs"}, {}, TODAY)
        self.assertEqual(row["posted_date"], "2026-07-23")   # from "1 day ago"
        self.assertEqual(row["posted_date_is_estimate"], True)
        self.assertEqual(row["city"], "Delhi")
        self.assertEqual(row["salary_min"], 300000)          # card CTC
        self.assertEqual(row["salary_period"], "per_annum")

    def test_club_row_contract(self):
        card = parse_listing_cards(CARD_HTML)[0]
        detail = parse_job_posting_ld(LD_HTML)
        club = rich_row_to_club_row(build_row(card, {"medicine-jobs"}, detail, TODAY))
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["city_name"], "Mumbai")
        self.assertEqual(club["posted_at"], "2026-07-14")
        self.assertEqual(club["min_salary"], "340000")
        self.assertEqual(club["salary_period"], "per_annum")
        self.assertEqual(club["min_experience"], "0")
        self.assertEqual(club["expires_at"], "2026-08-13")
        self.assertEqual(club["is_active"], "true")


class TestStripHtml(unittest.TestCase):
    def test_br_and_tags(self):
        out = strip_html("<p>About:</p>Line 1<br />\nLine 2 &amp; more")
        self.assertIn("Line 1", out)
        self.assertIn("& more", out)
        self.assertNotIn("<", out)


if __name__ == "__main__":
    unittest.main()
