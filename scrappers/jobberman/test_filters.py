#!/usr/bin/env python3
"""Unit tests for the jobberman scraper's parsers, classifier and cutoff.

Runnable with plain `python test_filters.py` (no network access needed).
Worked examples come from real listings observed on jobberman.com in
July 2026 (e.g. listing-1244751, "Medical Sales Representative").
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    classify_category,
    classify_company_type,
    compute_cutoff,
    INITIAL_WINDOW_DAYS,
    job_type_from,
    newest_possible_date,
    parse_base_salary,
    parse_experience_years,
    parse_graph,
    parse_listing_cards,
    rich_row_to_club_row,
    split_chips,
    WATERMARK_GRACE_DAYS,
)

# Trimmed real card from /jobs/healthcare (July 2026).
CARD_HTML = '''
<div class="..." data-cy="listing-cards-components" aria-labelledby="job-1244751-title">
  <a
      href="https://www.jobberman.com/listings/medical-sales-representative-qz9jek"
      class="block break-words"
      data-cy="listing-title-link"
      onclick="window.ga_push_event('select_item', {});"
      title="Medical Sales Representative"
  ><p class="text-lg font-medium break-words text-link-500">Medical Sales Representative</p></a>
  <p class="text-sm text-blue-700 text-loading-animate inline-block mt-3">
      Digitall  Healthcare Limited
  </p>
  <span class="mb-3 px-3 py-1 rounded bg-brand-secondary-100 mr-2 text-loading-hide text-gray-700">
      Lagos
  </span>
  <span class="mb-3 px-3 py-1 rounded bg-brand-secondary-100 mr-2 text-loading-hide text-gray-700">Full Time</span>
  <span class="mb-3 px-3 py-1 rounded bg-brand-secondary-100 mr-2 text-loading-hide text-gray-700">
      NGN <span class="mr-1">250,000 - 400,000</span>
  </span>
  <p class="text-sm text-gray-500 text-loading-animate inline-block">
      Sales
  </p>
  <p class="text-sm font-normal text-gray-700 text-loading-animate">4 days ago</p>
</div>
'''

# Trimmed real detail-page @graph (listing-1244751).
DETAIL_HTML = '''
<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@graph": [
  {
   "@type": "JobPosting",
   "@id": "https://www.jobberman.com/#/schema/JobPosting/listing-1244751",
   "title": "Medical Sales Representative",
   "description": "<div><b>Responsibilities:</b></div><ul><li>Achieve set company goals</li></ul>",
   "datePosted": "2026-07-20T00:00:00.000000Z",
   "industry": "Healthcare",
   "occupationalCategory": "Sales",
   "employmentType": "FULL_TIME",
   "validThrough": "2026-10-18T00:00:00.000000Z",
   "baseSalary": {"@type": "MonetaryAmount", "currency": "NGN",
                  "value": {"@type": "QuantitativeValue", "value": 250000,
                            "minValue": 250000, "maxValue": 400000,
                            "unitText": "MONTH"}},
   "qualifications": "Entry level",
   "experienceRequirements": {"@type": "OccupationalExperienceRequirements",
                              "monthsOfExperience": 12},
   "jobLocation": {"@id": "https://www.jobberman.com/#/schema/Place/location-1244751"},
   "hiringOrganization": {"@id": "https://www.jobberman.com/#/schema/Organization/agency-1244751"}
  },
  {
   "@type": "Organization",
   "@id": "https://www.jobberman.com/#/schema/Organization/agency-1244751",
   "name": "Digitall  Healthcare Limited"
  },
  {
   "@type": "Place",
   "@id": "https://www.jobberman.com/#/schema/Place/location-1244751",
   "address": {"@id": "https://www.jobberman.com/#/schema/PostalAddress/1244751"}
  },
  {
   "@type": "PostalAddress",
   "@id": "https://www.jobberman.com/#/schema/PostalAddress/1244751",
   "streetAddress": "Lagos",
   "addressLocality": "Nigeria",
   "addressRegion": "Marina",
   "addressCountry": "Nigeria"
  }
 ]
}
</script>
'''


class TestListingCards(unittest.TestCase):
    def test_real_card(self):
        cards = parse_listing_cards(CARD_HTML)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["job_id"], "medical-sales-representative-qz9jek")
        self.assertEqual(card["internal_id"], "1244751")
        self.assertEqual(card["title"], "Medical Sales Representative")
        self.assertEqual(card["company"], "Digitall Healthcare Limited")
        self.assertEqual(card["location"], "Lagos")
        self.assertEqual(card["chip_job_type"], "Full Time")
        self.assertEqual(card["chip_salary"], "NGN 250,000 - 400,000")
        self.assertEqual(card["function"], "Sales")
        self.assertEqual(card["age_text"], "4 days ago")

    def test_empty_page(self):
        self.assertEqual(parse_listing_cards(""), [])
        self.assertEqual(parse_listing_cards("<html><body>no jobs</body>"), [])

    def test_split_chips_confidential(self):
        location, job_type, salary = split_chips(
            ["Abuja", "Contract", "Confidential"])
        self.assertEqual((location, job_type, salary),
                         ("Abuja", "Contract", "Confidential"))


class TestDetailGraph(unittest.TestCase):
    def setUp(self):
        self.posting, self.by_id = parse_graph(DETAIL_HTML)

    def test_posting_found(self):
        self.assertEqual(self.posting.get("title"),
                         "Medical Sales Representative")
        self.assertEqual(self.posting.get("datePosted")[:10], "2026-07-20")

    def test_salary_real_example(self):
        salary = parse_base_salary(self.posting)
        self.assertEqual(salary["salary_min"], 250000)
        self.assertEqual(salary["salary_max"], 400000)
        self.assertEqual(salary["salary_period"], "per_month")
        self.assertEqual(salary["salary_currency"], "NGN")
        self.assertEqual(salary["salary_raw"], "NGN 250,000 - 400,000 per month")

    def test_salary_missing_never_invented(self):
        self.assertEqual(parse_base_salary({}), {})
        self.assertEqual(parse_base_salary({"baseSalary": {}}), {})
        self.assertEqual(parse_base_salary(
            {"baseSalary": {"currency": "NGN",
                            "value": {"minValue": "", "maxValue": ""}}}), {})

    def test_salary_swapped_bounds(self):
        salary = parse_base_salary(
            {"baseSalary": {"currency": "NGN",
                            "value": {"minValue": 400000, "maxValue": 250000,
                                      "unitText": "MONTH"}}})
        self.assertEqual((salary["salary_min"], salary["salary_max"]),
                         (250000, 400000))

    def test_experience_months_to_years(self):
        self.assertEqual(parse_experience_years(self.posting), "1")
        self.assertEqual(parse_experience_years({}), "")
        self.assertEqual(parse_experience_years(
            {"experienceRequirements": {"monthsOfExperience": 30}}), "2")

    def test_graph_reference_resolution(self):
        from scraper import address_from_graph, company_from_graph
        self.assertEqual(company_from_graph(self.posting, self.by_id),
                         "Digitall Healthcare Limited")
        self.assertEqual(address_from_graph(self.posting,
                                            self.by_id)["streetAddress"],
                         "Lagos")


class TestRelativeAges(unittest.TestCase):
    def test_units(self):
        today = date(2026, 7, 25)
        cases = {
            "Today": "2026-07-25",
            "Yesterday": "2026-07-24",
            "4 days ago": "2026-07-21",
            "1 week ago": "2026-07-18",
            "3 weeks ago": "2026-07-04",
            "1 month ago": "2026-06-27",   # optimistic: 28 days
            "2 hours ago": "2026-07-25",
        }
        for text, expected in cases.items():
            self.assertEqual(newest_possible_date(text, today), expected, text)

    def test_unknown_is_never_old(self):
        self.assertEqual(newest_possible_date("", date(2026, 7, 25)), "")
        self.assertEqual(newest_possible_date("some day", date(2026, 7, 25)), "")


class TestClassifier(unittest.TestCase):
    def test_clinical_titles(self):
        self.assertEqual(classify_category("Registered Nurse")[0], "nurses")
        self.assertEqual(classify_category("Pharmacist")[0], "pharmacists")
        self.assertEqual(classify_category("Superintendent Pharmacist")[0],
                         "pharmacists")
        self.assertEqual(classify_category("Medical Officer")[0], "doctors")
        self.assertEqual(classify_category("Optometrist")[0], "doctors")
        self.assertEqual(classify_category("Sonologist")[0], "doctors")

    def test_sector_roles_kept_not_flagged(self):
        # Real page-1 examples: healthcare signal in title or company.
        category, review = classify_category(
            "Medical Sales Representative", "Digitall Healthcare Limited",
            "Sales")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(review)
        category, review = classify_category(
            "Accountant", "Blue Chip Hospital Group", "Accounting")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(review)

    def test_no_signal_flagged_never_dropped(self):
        category, review = classify_category(
            "Technical Assistant - Programmatic", "Acme Ltd", "Admin")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(review)

    def test_company_type(self):
        self.assertEqual(
            classify_company_type("Sales Rep", "Emzor Pharmaceutical", ""),
            "pharma")
        self.assertEqual(
            classify_company_type("Nurse", "Lagoon Hospitals", ""),
            "hospital")


class TestJobType(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(job_type_from("Full Time", "FULL_TIME"), "full_time")
        self.assertEqual(job_type_from("Part Time", ""), "part_time")
        self.assertEqual(job_type_from("", "PART_TIME"), "part_time")
        self.assertEqual(job_type_from("Remote", ""), "remote")
        # Club enum has no contract/internship: falls back to full_time.
        self.assertEqual(job_type_from("Contract", "CONTRACTOR"), "full_time")
        self.assertEqual(job_type_from("Internship & Graduate", ""), "full_time")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today()
                    - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark_with_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-10", "2026-07-20", ""]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 7, 20)
             - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_all_dates_unparseable_falls_back(self):
        df = pd.DataFrame({"posted_date": ["", "n/a"]})
        expected = (date.today()
                    - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


class TestClubRow(unittest.TestCase):
    def test_ngn_salary_blank_in_club_csv(self):
        club = rich_row_to_club_row({
            "country": "Nigeria", "country_code": "NG",
            "country_dial_code": "+234", "city": "Lagos",
            "company": "Digitall Healthcare Limited",
            "company_type": "hospital", "title": "Medical Sales Representative",
            "description": "desc", "job_type": "full_time",
            "category": "non_clinical",
            "job_url": "https://www.jobberman.com/listings/x",
            "posted_date": "2026-07-20", "min_experience_years": "1",
            "salary_min": 250000, "salary_max": 400000,
            "salary_period": "per_month", "salary_currency": "NGN",
            "expires_at": "2026-10-18",
        })
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["max_salary"], "")
        self.assertEqual(club["salary_period"], "")
        self.assertEqual(club["salary_currency"], "")
        self.assertEqual(club["country_name"], "Nigeria")
        self.assertEqual(club["city_name"], "Lagos")
        self.assertEqual(club["min_experience"], "1")
        self.assertEqual(club["expires_at"], "2026-10-18")
        self.assertEqual(club["is_active"], "true")

    def test_city_falls_back_to_country(self):
        club = rich_row_to_club_row({"city": "", "state": "", "title": "X"})
        self.assertEqual(club["city_name"], "Nigeria")


if __name__ == "__main__":
    unittest.main(verbosity=2)
