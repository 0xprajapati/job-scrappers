#!/usr/bin/env python3
"""Unit tests for the freshersworld scraper's parsers and filters.

Run with plain:  python test_filters.py
Worked examples come from real listings captured on 2026-08-08.
"""

import unittest
from datetime import date

import pandas as pd

from freshersworld_scraper import (
    CLUB_COLUMNS,
    apply_classification,
    build_row,
    classify_company_type,
    humanize_fw_categories,
    compute_cutoff,
    parse_ago_days,
    parse_card_salary,
    parse_cards,
    parse_experience_years,
    parse_iso_date,
    parse_jsonld_salary,
    rich_row_to_club_row,
    strip_html,
    title_from_seo_title,
)


class CardSalaryTests(unittest.TestCase):
    """Real card strings: the salary line on the listing page."""

    def test_monthly_range(self):
        # Physiotherapist, HireInfinity Consulting LLP (job 2921820)
        self.assertEqual(
            parse_card_salary("100000 - 150000 Monthly"),
            ("100000 - 150000 Monthly", "100000", "150000", "Monthly"))

    def test_single_monthly_value(self):
        self.assertEqual(parse_card_salary("15000 Monthly"),
                         ("15000 Monthly", "15000", "15000", "Monthly"))

    def test_yearly_is_normalized_to_monthly(self):
        raw, lo, hi, period = parse_card_salary("240000 - 360000 Yearly")
        self.assertEqual((lo, hi, period), ("20000", "30000", "Yearly"))
        self.assertEqual(raw, "240000 - 360000 Yearly")

    def test_reversed_range_is_reordered(self):
        _, lo, hi, _ = parse_card_salary("150000 - 100000 Monthly")
        self.assertEqual((lo, hi), ("100000", "150000"))

    def test_qualification_text_is_not_a_salary(self):
        # The card's qualification line shares the CSS class of the salary
        # line; content like "BPT" or "B.Pharm, M.Pharm" must not parse.
        self.assertEqual(parse_card_salary("BPT"),
                         ("Not Disclosed", "", "", ""))
        self.assertEqual(parse_card_salary("B.Pharm, M.Pharm"),
                         ("Not Disclosed", "", "", ""))

    def test_missing_salary(self):
        self.assertEqual(parse_card_salary(""), ("Not Disclosed", "", "", ""))
        self.assertEqual(parse_card_salary(None), ("Not Disclosed", "", "", ""))


class JsonLdSalaryTests(unittest.TestCase):
    """baseSalary from the detail page's JobPosting JSON-LD."""

    def test_monthly_range(self):
        # Job 2921820's actual baseSalary block
        base = {"@type": "MonetaryAmount", "currency": "INR",
                "value": {"@type": "QuantitativeValue", "minValue": 100000,
                          "maxValue": 150000, "unitText": "Month",
                          "value": 150000}}
        self.assertEqual(parse_jsonld_salary(base),
                         ("INR 100000 - 150000 Month",
                          "100000", "150000", "Month"))

    def test_yearly_normalizes_but_keeps_raw(self):
        base = {"currency": "INR",
                "value": {"minValue": 300000, "maxValue": 600000,
                          "unitText": "Year"}}
        raw, lo, hi, period = parse_jsonld_salary(base)
        self.assertEqual(raw, "INR 300000 - 600000 Year")
        self.assertEqual((lo, hi, period), ("25000", "50000", "Year"))

    def test_zero_and_missing_amounts_are_not_disclosed(self):
        self.assertEqual(parse_jsonld_salary({"currency": "INR", "value": {}}),
                         ("Not Disclosed", "", "", ""))
        self.assertEqual(
            parse_jsonld_salary({"value": {"minValue": 0, "maxValue": 0}}),
            ("Not Disclosed", "", "", ""))
        self.assertEqual(parse_jsonld_salary(None),
                         ("Not Disclosed", "", "", ""))


class AgoTests(unittest.TestCase):
    def test_real_card_values(self):
        self.assertEqual(parse_ago_days("3 days ago"), 3)
        self.assertEqual(parse_ago_days("29 days ago"), 29)
        self.assertEqual(parse_ago_days("1 months ago"), 30)
        self.assertEqual(parse_ago_days("5 hours ago"), 0)

    def test_unrecognised_returns_none(self):
        # None => caller must treat the job as in-window, never exclude.
        self.assertIsNone(parse_ago_days(""))
        self.assertIsNone(parse_ago_days("Posted recently"))
        self.assertIsNone(parse_ago_days(None))


class TaxonomyWiringTests(unittest.TestCase):
    """The keep/drop + labeling decision belongs to _shared/classification.

    Only the wiring is tested here — the engine itself is covered by
    _shared/test_classification.py.
    """

    def test_in_scope_role_is_kept_and_labelled(self):
        row = {"title": "Pharmacovigilance Associate",
               "fw_categories": "pharma-job-vacancies",
               "description": "Process ICSRs and author safety narratives."}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")
        self.assertEqual(row["role_family"], "Pharmacovigilance")
        self.assertTrue(row["family_scores"])

    def test_category_slug_rescues_a_generic_title(self):
        # The whole point of the regulatory-affairs crawl slug: real RA
        # openings are advertised as plain "Executive" / "Manager".
        row = {"title": "Executive",
               "fw_categories": "regulatory-affairs-job-vacancies",
               "description": "Prepare and file regulatory dossiers and "
                              "submissions with CDSCO for drug registration."}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Regulatory Affairs")
        # The slug alone (skills x2) is not enough — the description carries it.
        self.assertIn("skills", row["matched_in"])

    def test_bedside_title_is_dropped(self):
        row = {"title": "Staff Nurse",
               "fw_categories": "health-care-job-vacancies",
               "description": "Ward duty, patient care."}
        self.assertFalse(apply_classification(row))
        self.assertEqual(row["category"], "")
        self.assertEqual(row["sub_category"], "")

    def test_non_healthcare_noise_is_dropped(self):
        row = {"title": "Zumba Instructor",
               "fw_categories": "health-care-job-vacancies",
               "description": "Lead group fitness classes."}
        self.assertFalse(apply_classification(row))

    def test_humanize_fw_categories(self):
        self.assertEqual(
            humanize_fw_categories(
                "pharma-job-vacancies; regulatory-affairs-job-vacancies"),
            "pharma, regulatory affairs")
        self.assertEqual(humanize_fw_categories(""), "")


class ClassifierTests(unittest.TestCase):

    def test_company_type(self):
        self.assertEqual(classify_company_type("Sun Pharma Ltd"), "pharma")
        self.assertEqual(classify_company_type("Apollo Hospitals"), "hospital")


class CutoffTests(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        self.assertEqual(compute_cutoff(None, today=date(2026, 8, 8)),
                         "2026-08-01")

    def test_later_runs_use_watermark_minus_grace(self):
        existing = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-05"]})
        self.assertEqual(compute_cutoff(existing), "2026-08-03")

    def test_since_overrides_everything(self):
        existing = pd.DataFrame({"posted_date": ["2026-08-05"]})
        self.assertEqual(compute_cutoff(existing, since="2026-07-01"),
                         "2026-07-01")

    def test_unparseable_dates_fall_back_to_initial_window(self):
        existing = pd.DataFrame({"posted_date": ["", "garbage"]})
        self.assertEqual(compute_cutoff(existing, today=date(2026, 8, 8)),
                         "2026-08-01")


class ParsingHelperTests(unittest.TestCase):
    def test_iso_date(self):
        # datePosted exactly as served by the site (IST offset)
        self.assertEqual(parse_iso_date("2026-07-10T00:00:00+05:30"),
                         "2026-07-10")
        self.assertEqual(parse_iso_date(""), "")
        self.assertEqual(parse_iso_date("soon"), "")

    def test_title_from_seo_title(self):
        self.assertEqual(
            title_from_seo_title("Physiotherapist Jobs Opening in "
                                 "HireInfinity Consulting LLP at "
                                 "Bannerghatta Road, Bangalore"),
            "Physiotherapist")
        self.assertEqual(title_from_seo_title("Plain Title"), "Plain Title")

    def test_experience_years(self):
        self.assertEqual(
            parse_experience_years({"monthsOfExperience": 1}), "0")
        self.assertEqual(
            parse_experience_years({"monthsOfExperience": 24}), "2")
        self.assertEqual(parse_experience_years(None, "0 Years"), "0")
        self.assertEqual(parse_experience_years(None, ""), "")

    def test_strip_html(self):
        self.assertEqual(
            strip_html("<p>Provide <b>care</b> &amp; support</p>"),
            "Provide care & support")


_CARD_HTML = '''
<div class="col-md-12 col-lg-12 col-xs-12 padding-none job-container jobs-on-hover top_space" job_id="2921820" job_display_url="https://www.freshersworld.com/jobs/physiotherapist-jobs-opening-2921820" id="all-jobs-append">
  <div class="job-new-title"><span class="wrap-title seo_title">Physiotherapist Jobs Opening in HireInfinity Consulting LLP at Bannerghatta Road, Bangalore<span class="title_less">Less</span></span></div>
  <h3 class="latest-jobs-title font-16 margin-none inline-block company-name">HireInfinity Consulting LLP</h3>
  <span class="job-location display-block modal-open job-details-span"><a href='/jobs-in-bangalore/9999016065'>Bangalore</a></span>
  <span class="experience job-details-span" style="x">0 Years</span>
  <span class="qualifications display-block modal-open pull-left job-details-span" style="x">100000 - 150000 Monthly</span>
  <span class="qualifications display-block modal-open pull-left job-details-span" style="x"><span class='elig_pos'>BPT</span></span>
  <div class="text-ago"><span class="job_posted_on">Posted: </span><span class="ago-text">29 days ago</span></div>
</div>
'''


class CardParsingTests(unittest.TestCase):
    def test_real_card_extraction(self):
        cards = list(parse_cards(_CARD_HTML))
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["job_id"], "2921820")
        self.assertEqual(card["company"], "HireInfinity Consulting LLP")
        self.assertEqual(card["location"], "Bangalore")
        self.assertEqual(card["experience_raw"], "0 Years")
        self.assertEqual(card["salary_text"], "100000 - 150000 Monthly")
        self.assertEqual(card["qualification"], "BPT")
        self.assertEqual(card["ago_text"], "29 days ago")


class RowBuildingTests(unittest.TestCase):
    CARD = {"job_id": "2921820",
            "job_url": "https://www.freshersworld.com/jobs/x-2921820",
            "seo_title": "Physiotherapist Jobs Opening in HireInfinity "
                         "Consulting LLP at Bannerghatta Road, Bangalore",
            "company": "HireInfinity Consulting LLP",
            "location": "Bangalore", "experience_raw": "0 Years",
            "salary_text": "100000 - 150000 Monthly",
            "qualification": "BPT", "ago_text": "29 days ago"}

    POSTING = {"@type": "JobPosting",
               "datePosted": "2026-07-10T00:00:00+05:30",
               "validThrough": "2026-09-08T00:00:00+05:30",
               "title": "Physiotherapist",
               "description": "<p>Provide physiotherapy assessment.</p>",
               "employmentType": "FULL_TIME", "qualifications": "BPT",
               "experienceRequirements": {"monthsOfExperience": 1},
               "hiringOrganization": {"@type": "Organization",
                                      "name": "HireInfinity Consulting LLP"},
               "baseSalary": {"currency": "INR",
                              "value": {"minValue": 100000,
                                        "maxValue": 150000,
                                        "unitText": "Month"}},
               "jobLocation": [{"@type": "Place",
                                "address": {"addressLocality": "Bangalore",
                                            "addressRegion": "Karnataka",
                                            "addressCountry": "IN"}}]}

    def test_row_from_card_plus_jsonld(self):
        row = build_row(self.CARD, self.POSTING,
                        {"health-care-job-vacancies"})
        self.assertEqual(row["title"], "Physiotherapist")
        self.assertEqual(row["posted_date"], "2026-07-10")
        self.assertEqual(row["valid_through"], "2026-09-08")
        self.assertEqual(row["salary_min_monthly"], "100000")
        self.assertEqual(row["city"], "Bangalore")
        self.assertEqual(row["state"], "Karnataka")
        self.assertEqual(row["country"], "IN")
        self.assertEqual(row["experience_min_years"], "0")
        self.assertEqual(row["description"],
                         "Provide physiotherapy assessment.")

    def test_row_without_jsonld_falls_back_to_card(self):
        row = build_row(self.CARD, None, {"health-care-job-vacancies"},
                        today=date(2026, 8, 8))
        self.assertEqual(row["title"], "Physiotherapist")
        # 29 days before 2026-08-08
        self.assertEqual(row["posted_date"], "2026-07-10")
        self.assertEqual(row["salary_raw"], "100000 - 150000 Monthly")
        self.assertEqual(row["city"], "Bangalore")
        self.assertEqual(row["qualification"], "BPT")

    def test_club_row_mapping(self):
        club = rich_row_to_club_row(
            build_row(self.CARD, self.POSTING, {"health-care-job-vacancies"}))
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["country_dial_code"], "+91")
        self.assertEqual(club["city_name"], "Bangalore")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["min_salary"], "100000")
        self.assertEqual(club["max_salary"], "150000")
        self.assertEqual(club["salary_period"], "per_month")
        self.assertEqual(club["salary_currency"], "INR")
        self.assertEqual(club["posted_at"], "2026-07-10")
        self.assertEqual(club["qualification"], "BPT")
        # The shared 23-column contract — is_active/expires_at are retired.
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))

    def test_club_row_carries_the_stamped_taxonomy(self):
        row = build_row(self.CARD, self.POSTING, {"pharma-job-vacancies"})
        row["title"] = "Clinical Data Manager"
        self.assertTrue(apply_classification(row))
        club = rich_row_to_club_row(row)
        self.assertEqual(club["category"], "Non Clinical")
        self.assertEqual(club["sub_category"], "Clinical Data Management")

    def test_club_row_yearly_salary_restores_annual_amounts(self):
        # Dermatologist (job 2936474): INR 100000 - 500000 Year. The monthly
        # normalization rounds (100000/12 -> 8333), so the club export must
        # re-read the ORIGINAL amounts from salary_raw, not multiply back.
        row = build_row(self.CARD, self.POSTING, set())
        row["salary_raw"] = "INR 100000 - 500000 Year"
        row["salary_min_monthly"], row["salary_max_monthly"] = "8333", "41667"
        row["salary_period_original"] = "Year"
        club = rich_row_to_club_row(row)
        self.assertEqual(club["min_salary"], "100000")
        self.assertEqual(club["max_salary"], "500000")
        self.assertEqual(club["salary_period"], "per_annum")


if __name__ == "__main__":
    unittest.main(verbosity=2)
