#!/usr/bin/env python3
"""Unit tests for the michaelpage scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real listings on michaelpage.co.in/jobs/healthcare
(July 2026).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    build_row,
    classify_category,
    classify_company_type,
    compute_cutoff,
    job_type_from,
    location_meta,
    parse_base_salary,
    parse_job_posting,
    parse_listing_cards,
    rich_row_to_club_row,
)

# Trimmed real card markup (ED - Healthcare, JN-102025-6852902).
CARD_HTML = """
<li class="views-row"><div about="/job-detail/ed-healthcare-pharma/ref/jn-102025-6852902"
 class="job-tile search-job-tile"><div class="job-title " id="5732376"><h3>
<a href="/job-detail/ed-healthcare-pharma/ref/jn-102025-6852902" rel="bookmark"
 id="job-5732376" >ED - Healthcare</a></h3></div><div class="job-properties">
<div class="job-location"><i class="fal fa-map-marker-alt" aria-hidden="true"></i>
 India</div><div class="job-contract-type"><i class="far fa-clock"
 aria-hidden="true"></i> Permanent</div></div><div class="job-summary">
<div class="job_advert__job-summary-text"><p>This position leads value creation
 and transformation initiatives across multiple healthcare and pharma portfolio
 companies.</p></div></div><div class="bullet_points">
<div class="job_advert__job-desc-bullet-points"><ul><li>Leading Private Equity
 Firm</li><li>Value Creation role with PE</li></ul></div></div></div></li>
"""

# Real JobPosting JSON-LD shape (JN-042026-6994543 has disclosed salary).
POSTING = {
    "@type": "JobPosting",
    "title": "ED - Healthcare Pharma",
    "datePosted": "2026-03-22",
    "employmentType": "FULL_TIME",
    "industry": "Healthcare",
    "description": "<p><strong>About Our Client</strong></p><p>A leading "
                   "global investment firm.</p>",
    "baseSalary": {"@type": "MonetaryAmount", "currency": "INR",
                   "value": {"@type": "QuantitativeValue",
                             "minValue": "8000000", "maxValue": "10000000",
                             "unitText": "YEAR"}},
    "jobLocation": {"@type": "Place",
                    "address": {"@type": "PostalAddress",
                                "addressLocality": "Bangalore Urban",
                                "addressRegion": "Karnataka",
                                "addressCountry": "IN"}},
}


class TestCardParser(unittest.TestCase):
    def test_real_card(self):
        cards = parse_listing_cards(CARD_HTML)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["job_id"], "JN-102025-6852902")
        self.assertEqual(card["internal_id"], "5732376")
        self.assertEqual(card["title"], "ED - Healthcare")
        self.assertEqual(card["job_url"],
                         "https://www.michaelpage.co.in/job-detail/"
                         "ed-healthcare-pharma/ref/jn-102025-6852902")
        self.assertEqual(card["location"], "India")
        self.assertEqual(card["contract_type"], "Permanent")
        self.assertIn("value creation", card["summary"])
        self.assertEqual(card["bullets"],
                         ["Leading Private Equity Firm",
                          "Value Creation role with PE"])

    def test_empty_page(self):
        self.assertEqual(parse_listing_cards("<html>no jobs</html>"), [])
        self.assertEqual(parse_listing_cards(""), [])


class TestJsonLd(unittest.TestCase):
    def test_control_chars_tolerated(self):
        # The site's JSON-LD embeds raw newlines inside strings.
        html = ('<script type="application/ld+json">{"@type": "JobPosting",'
                '"title": "X", "description": "line1\nline2"}</script>')
        posting = parse_job_posting(html)
        self.assertEqual(posting.get("title"), "X")

    def test_non_jobposting_skipped(self):
        html = ('<script type="application/ld+json">{"@type": "Organization"}'
                '</script>')
        self.assertEqual(parse_job_posting(html), {})


class TestSalaryParser(unittest.TestCase):
    def test_disclosed_annual_inr(self):
        parsed = parse_base_salary(POSTING)
        self.assertEqual(parsed["salary_min"], 8000000)
        self.assertEqual(parsed["salary_max"], 10000000)
        self.assertEqual(parsed["salary_period"], "per_annum")
        self.assertEqual(parsed["salary_currency"], "INR")
        self.assertEqual(parsed["salary_raw"],
                         "INR 8,000,000 - 10,000,000 per year")

    def test_confidential_is_empty(self):
        # Most mandates: currency "", minValue "", maxValue "", unit "YEAR".
        posting = {"baseSalary": {"currency": "", "value":
                   {"minValue": "", "maxValue": "", "unitText": "YEAR"}}}
        self.assertEqual(parse_base_salary(posting), {})
        self.assertEqual(parse_base_salary({}), {})

    def test_swapped_and_single(self):
        posting = {"baseSalary": {"currency": "INR", "value":
                   {"minValue": "500000", "maxValue": "", "unitText": "YEAR"}}}
        parsed = parse_base_salary(posting)
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         (500000, 500000))


class TestClassifier(unittest.TestCase):
    def test_sales_head_healthcare_industry(self):
        category, review = classify_category(
            "Head of Sales (Ayurveda Healthcare)", "Healthcare", "")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(review)  # healthcare word present

    def test_medical_affairs_is_doctor(self):
        category, _ = classify_category("Director - Medical Affairs",
                                        "Healthcare", "")
        self.assertEqual(category, "doctors")

    def test_no_signal_flagged(self):
        category, review = classify_category(
            "Chief Financial Officer", "Banking & Financial Services",
            "Leads finance for a large corporate.")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(review)

    def test_company_type(self):
        self.assertEqual(classify_company_type(
            "GM Sales", "Healthcare / Pharmaceutical", ""), "pharma")
        self.assertEqual(classify_company_type(
            "COO - Hospital Chain", "Healthcare", "Runs hospital ops"),
            "hospital")


class TestLocationAndType(unittest.TestCase):
    def test_city(self):
        self.assertEqual(location_meta("Coimbatore"),
                         ("Coimbatore", "India", "IN", "+91"))

    def test_country_placeholders(self):
        for loc in ("India", "International", ""):
            self.assertEqual(location_meta(loc), ("", "India", "IN", "+91"))

    def test_job_type(self):
        self.assertEqual(job_type_from("Permanent", "FULL_TIME"), "full_time")
        self.assertEqual(job_type_from("Temporary", "FULL_TIME"), "contract")


class TestRowBuilding(unittest.TestCase):
    def test_row_and_club_row(self):
        card = parse_listing_cards(CARD_HTML)[0]
        row = build_row(card, POSTING)
        self.assertEqual(row["job_id"], "JN-102025-6852902")
        self.assertEqual(row["posted_date"], "2026-03-22")
        self.assertEqual(row["city"], "Bangalore Urban")
        self.assertEqual(row["state"], "Karnataka")
        self.assertEqual(row["company"], "Michael Page")
        self.assertEqual(row["salary_min"], 8000000)
        self.assertIn("About Our Client", row["description"])

        club = rich_row_to_club_row(row)
        self.assertEqual(club["company_name"], "Michael Page")
        self.assertEqual(club["min_salary"], "8000000")
        self.assertEqual(club["salary_period"], "per_annum")
        self.assertEqual(club["posted_at"], "2026-03-22")
        self.assertEqual(club["company_about"],
                         "Leading Private Equity Firm; Value Creation role with PE")

    def test_row_without_posting(self):
        # Detail JSON-LD missing: card data still yields a valid row.
        card = parse_listing_cards(CARD_HTML)[0]
        row = build_row(card, {})
        self.assertEqual(row["posted_date"], "")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertIn("value creation", row["description"])


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-15", ""]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 7, 15) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
