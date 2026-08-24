#!/usr/bin/env python3
"""Unit tests for the gulftalent scraper's pure parsing/filter functions.

Run with plain `python test_filters.py` (no pytest needed). Worked examples
are trimmed verbatim from real www.gulftalent.com pages observed on
2026-07-27 (Kuwait job 584995 and UAE jobs 613092 / 614844).
"""

import unittest
from datetime import date

import pandas as pd

from scraper import (
    build_row, city_from, classify_category, classify_company_type,
    compute_cutoff, country_meta, job_type_from, parse_about_company,
    parse_base_salary, parse_detail_attributes, parse_experience,
    parse_job_posting, parse_last_page, parse_listing_date,
    parse_listing_rows, parse_reference, parse_salary_text,
    rich_row_to_club_row, within_window,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Kuwait healthcare listing, the single live row (company is bare text and
# there is no logo cell — the common shape for non-advertising employers).
KUWAIT_LISTING = """
<table><tbody>
    <tr class="content-visibility-auto">
        <td class="col-sm-21 text-muted text-overflow">
            <p class="text-base title text-body space-top-tiny space-bottom-none text-overflow">
                <a class="ga-job-impression ga-job-click text-base title text-title text-overflow text-strong
                    "
                        data-ga-label="584995"
                        data-ga-dimension-three="kuwait"
                        target="_blank"
                        popover-html-unsafe="Commercial Manager"
                        href="/kuwait/jobs/commercial-manager-584995" >
                    Commercial Manager
                </a>
                <span class="gt-icon-box"></span>
            </p>
            Eva Pharma
        </td>
        <td class="text-overflow col-sm-6">
            <a class="text-base text-regular text-secondary-hover" href="/kuwait/jobs">
                <span title="Kuwait">
                    Kuwait
                </span>
            </a>
        </td>
        <td class="col-sm-4">
            13 May
        </td>
        <td class="text-center col-sm-5">
        </td>
    </tr>
</tbody></table>
"""

# UAE listing row: linked company, city span "Dubai, UAE", easy-apply icon
# and a logo — plus the pager that gives the page count.
UAE_LISTING = """
<table><tbody>
    <tr class="content-visibility-auto">
        <td class="col-sm-21 text-muted text-overflow">
            <p class="text-base title text-body space-top-tiny space-bottom-none text-overflow">
                <a class="ga-job-impression ga-job-click text-base title text-title text-overflow text-strong
                    "
                        data-ga-label="613092"
                        data-ga-dimension-three="uae"
                        target="_blank"
                        href="/uae/jobs/senior-receptionist-healthcare-filipino-613092" >
                    Senior Receptionist (Healthcare) - Filipino
                </a>
                <span class="gt-icon-box">
                    <img class="icon-easy-apply" src="/encore/img/send-check.svg" alt='Easy Apply' />
                </span>
            </p>
            <a class="text-base text-muted text-secondary-hover"
                    target="_blank"
                    href="/companies/tanizze-careers">
                Tanizze
            </a>
        </td>
        <td class="text-overflow col-sm-6">
            <a class="text-base text-regular text-secondary-hover" href="/uae/jobs/city/dubai">
                <span title="Dubai, UAE">
                    Dubai
                </span>
            </a>
        </td>
        <td class="col-sm-4">
            23 Jul
        </td>
        <td class="text-center col-sm-5">
            <a href="/companies/tanizze-careers" target="_blank">
                <img src="https://www.gulftalent.com/images1/logos/listing/SE224-14278_logo.png"
                        alt="Tanizze careers &amp; jobs" width="60" height="40" />
            </a>
        </td>
    </tr>
</tbody></table>
<ul class="pagination">
    <li class="jumper"><a href="/uae/jobs/industry/healthcare/2" data-cy="pagination-next-btn">Next</a></li>
    <li class="jumper "><a href="/uae/jobs/industry/healthcare/56"
                           data-cy="pagination-last-btn">Last</a></li>
</ul>
"""

# Kuwait detail page: JSON-LD without baseSalary + the attribute grid with
# Salary "Not Specified".
KUWAIT_DETAIL = """
<span class="text-supermuted">Ref: PP000-65347</span>
<script type="application/ld+json">
{
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "datePosted": "2026-05-13T00:00:00+00:00",
    "description": "<p><h4>Description</h4>\\n<p>Join EVA Pharma, a leading pharmaceutical company.</p>\\n<h4>Requirements</h4>\\n<li>Bachelor's Degree in Pharmacy, Medicine, or any relevant scientific field.</li>\\n<li>Minimum 7 years of experience within the Kuwait pharmaceutical market.</li></p>",
    "employmentType": ["FULL_TIME"],
    "hiringOrganization": {"@type": "Organization", "name": "Eva Pharma", "sameAs": "Eva Pharma"},
    "identifier": {"@type": "PropertyValue", "name": "Eva Pharma", "value": "584995"},
    "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
        "addressLocality": "Kuwait", "addressRegion": "Kuwait",
        "addressCountry": "Kuwait", "streetAddress": "Kuwait", "postalCode": ""}},
    "title": "Commercial Manager",
    "industry": "Healthcare, Pharmaceuticals &amp; Medical Services",
    "validThrough": "2026-08-11T00:00:00+00:00"
}
</script>
<div class="col-sm-8"><div class="space-bottom-xs">
    <span style="color: #6c757d">Job Type</span><br>
    <span>Full Time</span>
</div></div>
<div class="col-sm-8"><div class="space-bottom-xs">
    <span style="color: #6c757d">Job Location</span><br>
    <span>Kuwait</span>
</div></div>
<div class="col-sm-8"><div class="space-bottom-xs">
    <span style="color: #6c757d">Nationality</span><br>
    <span>Any Nationality</span>
</div></div>
<div class="col-sm-8"><div class="space-bottom-xs">
    <span style="color: #6c757d">Salary</span><br>
    <span>Not Specified</span>
</div></div>
<div class="col-sm-8"><div class="space-bottom-xs">
    <span style="color: #6c757d">Job Function</span><br>
    <span>Sales - Retail</span>
</div></div>
<div class="col-sm-8"><div class="space-bottom-xs">
    <span style="color: #6c757d">Company Industry</span><br>
    <span>Healthcare, Pharmaceuticals &amp; Medical Services</span>
</div></div>
<h4 class="header-ribbon">About the Company</h4>
<p><p>EVA Pharma is a leading pharmaceutical company operating across 40 markets.</p>
</p>
"""

# UAE detail page fragment with a real AED baseSalary block.
UAE_SALARY_LD = """
<script type="application/ld+json">
{
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "baseSalary": {"@type": "MonetaryAmount", "currency": "AED",
        "value": {"@type": "QuantitativeValue", "minValue": 7000,
                  "maxValue": 8000, "unitText": "MONTH"}},
    "datePosted": "2026-07-23T00:00:00+00:00",
    "description": "<p>We are seeking a Senior Receptionist with 2-3 years of experience in healthcare.</p>",
    "employmentType": ["FULL_TIME"],
    "hiringOrganization": {"@type": "Organization", "name": "Tanizze",
        "logo": "https://www.gulftalent.com/images1/logos/listing/SE224-14278_logo.png"},
    "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
        "addressLocality": "Dubai", "addressRegion": "Dubai",
        "addressCountry": "UAE", "streetAddress": "Al Wasl"}},
    "title": "Senior Receptionist (Healthcare) - Filipino",
    "industry": "Healthcare, Pharmaceuticals &amp; Medical Services",
    "validThrough": "2026-10-21T00:00:00+00:00"
}
</script>
"""


class TestListingParsing(unittest.TestCase):

    def test_kuwait_row(self):
        rows = parse_listing_rows(KUWAIT_LISTING)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["job_id"], "584995")
        self.assertEqual(row["country_slug"], "kuwait")
        self.assertEqual(row["title"], "Commercial Manager")
        self.assertEqual(row["job_url"],
                         "https://www.gulftalent.com/kuwait/jobs/commercial-manager-584995")
        self.assertEqual(row["company"], "Eva Pharma")     # bare text, unlinked
        self.assertEqual(row["company_url"], "")
        self.assertEqual(row["city"], "Kuwait")
        self.assertEqual(row["listing_date_raw"], "13 May")
        self.assertFalse(row["easy_apply"])
        self.assertEqual(row["company_logo"], "")

    def test_uae_row(self):
        row = parse_listing_rows(UAE_LISTING)[0]
        self.assertEqual(row["job_id"], "613092")
        self.assertEqual(row["company"], "Tanizze")
        self.assertEqual(row["company_url"],
                         "https://www.gulftalent.com/companies/tanizze-careers")
        self.assertEqual(row["city"], "Dubai")
        self.assertEqual(row["city_full"], "Dubai, UAE")
        self.assertTrue(row["easy_apply"])
        self.assertTrue(row["company_logo"].endswith("SE224-14278_logo.png"))

    def test_empty_logo_cell_does_not_borrow_a_later_image(self):
        # The real Kuwait page has app-promo <img> tags further down; an empty
        # logo cell must stay empty rather than swallow the next image.
        page = KUWAIT_LISTING + '<img src="/images/mediumAppAndroid.png" />'
        self.assertEqual(parse_listing_rows(page)[0]["company_logo"], "")

    def test_no_rows_is_empty_not_error(self):
        self.assertEqual(parse_listing_rows("<html>no jobs</html>"), [])
        self.assertEqual(parse_listing_rows(None), [])

    def test_last_page(self):
        self.assertEqual(parse_last_page(UAE_LISTING), 56)
        self.assertEqual(parse_last_page(KUWAIT_LISTING), 1)   # no pager
        self.assertEqual(parse_last_page(None), 1)


class TestListingDate(unittest.TestCase):

    def test_year_inferred_from_today(self):
        today = date(2026, 7, 27)
        self.assertEqual(parse_listing_date("13 May", today), "2026-05-13")
        self.assertEqual(parse_listing_date("27 Jul", today), "2026-07-27")

    def test_future_month_rolls_back_a_year(self):
        # A "1 Dec" row seen in July belongs to the previous December.
        self.assertEqual(parse_listing_date("1 Dec", date(2026, 7, 27)),
                         "2025-12-01")

    def test_explicit_year_and_garbage(self):
        self.assertEqual(parse_listing_date("9 Apr 2025", date(2026, 7, 27)),
                         "2025-04-09")
        self.assertEqual(parse_listing_date("", date(2026, 7, 27)), "")
        self.assertEqual(parse_listing_date("soon", date(2026, 7, 27)), "")
        self.assertEqual(parse_listing_date("31 Feb", date(2026, 7, 27)), "")


class TestDetailParsing(unittest.TestCase):

    def test_job_posting_json_ld(self):
        posting = parse_job_posting(KUWAIT_DETAIL)
        self.assertEqual(posting["title"], "Commercial Manager")
        self.assertEqual(posting["datePosted"][:10], "2026-05-13")
        self.assertEqual(posting["validThrough"][:10], "2026-08-11")
        self.assertEqual(parse_job_posting("<html></html>"), {})

    def test_attribute_grid(self):
        attributes = parse_detail_attributes(KUWAIT_DETAIL)
        self.assertEqual(attributes["Job Type"], "Full Time")
        self.assertEqual(attributes["Salary"], "Not Specified")
        self.assertEqual(attributes["Job Function"], "Sales - Retail")
        self.assertEqual(attributes["Company Industry"],
                         "Healthcare, Pharmaceuticals & Medical Services")

    def test_about_and_reference(self):
        self.assertIn("EVA Pharma is a leading pharmaceutical company",
                      parse_about_company(KUWAIT_DETAIL))
        self.assertEqual(parse_reference(KUWAIT_DETAIL), "PP000-65347")
        self.assertEqual(parse_about_company("<html></html>"), "")


class TestSalary(unittest.TestCase):

    def test_undisclosed_is_never_invented(self):
        self.assertEqual(parse_salary_text("Not Specified"), {})
        self.assertEqual(parse_salary_text(""), {})
        self.assertEqual(parse_base_salary(parse_job_posting(KUWAIT_DETAIL)), {})

    def test_aed_range_from_grid(self):
        salary = parse_salary_text("7000 - 8000 AED")
        self.assertEqual(salary["salary_min_monthly"], 7000)
        self.assertEqual(salary["salary_max_monthly"], 8000)
        self.assertEqual(salary["salary_currency"], "AED")
        self.assertEqual(salary["salary_period_original"], "per_month")
        self.assertEqual(salary["salary_raw"], "7000 - 8000 AED")

    def test_symbol_and_thousands_separators(self):
        salary = parse_salary_text("$4,000 - $5,000")
        self.assertEqual((salary["salary_min_monthly"],
                          salary["salary_max_monthly"]), (4000, 5000))
        self.assertEqual(salary["salary_currency"], "USD")

    def test_single_amount(self):
        salary = parse_salary_text("KWD 1,500")
        self.assertEqual((salary["salary_min_monthly"],
                          salary["salary_max_monthly"]), (1500, 1500))
        self.assertEqual(salary["salary_currency"], "KWD")

    def test_base_salary_json_ld(self):
        salary = parse_base_salary(parse_job_posting(UAE_SALARY_LD))
        self.assertEqual(salary["salary_min_monthly"], 7000)
        self.assertEqual(salary["salary_max_monthly"], 8000)
        self.assertEqual(salary["salary_currency"], "AED")
        self.assertEqual(salary["salary_period_original"], "per_month")

    def test_annual_amounts_normalised_to_monthly(self):
        posting = {"baseSalary": {"currency": "USD", "value": {
            "minValue": 120000, "maxValue": 180000, "unitText": "YEAR"}}}
        salary = parse_base_salary(posting)
        self.assertEqual((salary["salary_min_monthly"],
                          salary["salary_max_monthly"]), (10000, 15000))


class TestExperience(unittest.TestCase):

    def test_minimum_years(self):
        raw, low, high = parse_experience(
            "Minimum 7 years of experience within the Kuwait pharmaceutical market.")
        self.assertEqual((low, high), (7, ""))
        self.assertIn("7 years", raw)

    def test_range(self):
        _, low, high = parse_experience(
            "We are seeking a Senior Receptionist with 2-3 years of experience "
            "in healthcare.")
        self.assertEqual((low, high), (2, 3))

    def test_absent(self):
        self.assertEqual(parse_experience("No numbers here."), ("", "", ""))
        self.assertEqual(parse_experience(""), ("", "", ""))


class TestClassification(unittest.TestCase):

    def test_clinical_titles(self):
        self.assertEqual(classify_category("Registered Home Care Nurse")[0], "nurses")
        self.assertEqual(classify_category("Consultant Dermatologist")[0], "doctors")
        self.assertEqual(classify_category("Aesthetic General Practitioner")[0],
                         "doctors")
        self.assertEqual(classify_category("Clinical Pharmacist")[0], "pharmacists")

    def test_healthcare_job_function_is_not_flagged(self):
        category, needs_review = classify_category(
            "Senior Receptionist (Healthcare) - Filipino", "Healthcare", "Tanizze")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(needs_review)

    def test_commercial_role_at_a_pharma_company_is_kept(self):
        category, needs_review = classify_category(
            "Commercial Manager", "Sales - Retail", "Eva Pharma",
            "Join EVA Pharma, a leading pharmaceutical company.")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(needs_review)   # "Pharma" in the employer name

    def test_signal_free_title_is_flagged_not_dropped(self):
        category, needs_review = classify_category(
            "Finance Manager", "Accounting & Audit", "Confidential", "")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)

    def test_company_type(self):
        self.assertEqual(classify_company_type("Eva Pharma", "Commercial Manager",
                                               "Sales - Retail"), "pharma")
        self.assertEqual(classify_company_type("Tanizze", "Senior Receptionist",
                                               "Healthcare"), "hospital")
        # An agency's clinical mandate is a hospital role, not pharma.
        self.assertEqual(classify_company_type("TalentGrade",
                                               "Consultant Dermatologist",
                                               "Healthcare"), "hospital")
        self.assertEqual(classify_company_type(
            "MENA Recruit", "Senior Legal Counsel (Commercial & Pharmaceutical)",
            "Legal"), "pharma")

    def test_job_type_maps_into_the_club_enum(self):
        self.assertEqual(job_type_from("Full Time"), "full_time")
        self.assertEqual(job_type_from("Part Time"), "part_time")
        self.assertEqual(job_type_from("Temporary"), "full_time")   # no enum value
        self.assertEqual(job_type_from("", ["PART_TIME"]), "part_time")
        self.assertEqual(job_type_from("", ""), "full_time")

    def test_country_and_city(self):
        self.assertEqual(country_meta("kuwait"), ("Kuwait", "KW", "+965"))
        self.assertEqual(country_meta("uae"),
                         ("United Arab Emirates", "AE", "+971"))
        self.assertEqual(country_meta("", "Kuwait"), ("Kuwait", "KW", "+965"))

    def test_city_drops_country_wide_placeholders(self):
        self.assertEqual(city_from("Kuwait", "Kuwait", "Kuwait", "KW", "kuwait"), "")
        # JSON-LD says "UAE" for country-wide UAE postings.
        self.assertEqual(
            city_from("UAE", "UAE", "United Arab Emirates", "AE", "uae"), "")
        self.assertEqual(
            city_from("Dubai", "Dubai", "United Arab Emirates", "AE", "uae"),
            "Dubai")

    def test_city_prefers_the_listing_facet_over_a_street_locality(self):
        self.assertEqual(
            city_from("Dubai", "Zabeel 2 - Zabeel - Dubai",
                      "United Arab Emirates", "AE", "uae"), "Dubai")
        # …but a street-level locality is better than nothing.
        self.assertEqual(
            city_from("", "Zabeel 2 - Zabeel - Dubai",
                      "United Arab Emirates", "AE", "uae"),
            "Zabeel 2 - Zabeel - Dubai")


class TestTimeWindow(unittest.TestCase):

    def test_first_run_keeps_everything(self):
        self.assertIsNone(compute_cutoff(None))
        self.assertIsNone(compute_cutoff(pd.DataFrame(columns=["posted_date"])))
        self.assertTrue(within_window("2025-01-01", None))

    def test_watermark_with_grace(self):
        existing = pd.DataFrame({"posted_date": ["2026-07-20", "2026-07-25"]})
        self.assertEqual(compute_cutoff(existing), "2026-07-23")

    def test_within_window(self):
        self.assertTrue(within_window("2026-07-23", "2026-07-23"))
        self.assertFalse(within_window("2026-07-22", "2026-07-23"))
        self.assertFalse(within_window("", "2026-07-23"))


class TestRowBuilding(unittest.TestCase):

    def _kuwait_row(self):
        listing_row = parse_listing_rows(KUWAIT_LISTING)[0]
        listing_row["reference"] = parse_reference(KUWAIT_DETAIL)
        return build_row(listing_row, parse_job_posting(KUWAIT_DETAIL),
                         parse_detail_attributes(KUWAIT_DETAIL),
                         parse_about_company(KUWAIT_DETAIL), "kuwait")

    def test_rich_row(self):
        row = self._kuwait_row()
        self.assertEqual(row["job_id"], "584995")
        self.assertEqual(row["reference"], "PP000-65347")
        self.assertEqual(row["company"], "Eva Pharma")
        self.assertEqual(row["country"], "Kuwait")
        self.assertEqual(row["country_code"], "KW")
        self.assertEqual(row["country_dial_code"], "+965")
        self.assertEqual(row["city"], "")            # locality repeats country
        self.assertEqual(row["posted_date"], "2026-05-13")
        self.assertEqual(row["expires_at"], "2026-08-11")
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual(row["job_type_original"], "Full Time")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["salary_min_monthly"], "")
        self.assertEqual(row["category"], "non_clinical")
        self.assertEqual(row["company_type"], "pharma")
        self.assertFalse(row["needs_review"])
        self.assertEqual(row["experience_min_years"], 7)
        self.assertIn("Join EVA Pharma", row["description"])
        self.assertNotIn("<", row["description"])

    def test_club_row(self):
        club = rich_row_to_club_row(self._kuwait_row())
        self.assertEqual(club["country_name"], "Kuwait")
        self.assertEqual(club["country_code"], "KW")
        self.assertEqual(club["country_dial_code"], "+965")
        # Country-wide posting: the country stands in for the missing city.
        self.assertEqual(club["city_name"], "Kuwait")
        self.assertEqual(club["company_name"], "Eva Pharma")
        self.assertEqual(club["category"], "non_clinical")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["posted_at"], "2026-05-13")
        self.assertEqual(club["min_experience"], "7")
        self.assertEqual(club["is_active"], "true")
        self.assertEqual(club["expires_at"], "2026-08-11")
        self.assertEqual(sorted(club), sorted([
            "country_name", "country_code", "country_dial_code", "city_name",
            "company_name", "company_type", "company_logo", "company_about",
            "title", "description", "job_type", "category", "application_url",
            "posted_at", "min_experience", "max_experience", "min_salary",
            "max_salary", "salary_period", "salary_currency", "is_active",
            "expires_at"]))

    def test_gulf_currency_stays_out_of_the_club_csv(self):
        listing_row = parse_listing_rows(UAE_LISTING)[0]
        row = build_row(listing_row, parse_job_posting(UAE_SALARY_LD), {}, "", "uae")
        self.assertEqual(row["salary_min_monthly"], 7000)
        self.assertEqual(row["salary_currency"], "AED")
        club = rich_row_to_club_row(row)
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["max_salary"], "")
        self.assertEqual(club["salary_currency"], "")
        self.assertEqual(club["salary_period"], "")

    def test_usd_salary_reaches_the_club_csv(self):
        posting = {"baseSalary": {"currency": "USD", "value": {
            "minValue": 5000, "maxValue": 6000, "unitText": "MONTH"}}}
        row = build_row({"job_id": "1", "title": "Consultant Cardiologist",
                         "country_slug": "kuwait"}, posting)
        club = rich_row_to_club_row(row)
        self.assertEqual((club["min_salary"], club["max_salary"]),
                         ("5000", "6000"))
        self.assertEqual(club["salary_currency"], "USD")
        self.assertEqual(club["salary_period"], "per_month")

    def test_build_row_survives_a_detail_page_with_no_structured_data(self):
        listing_row = parse_listing_rows(KUWAIT_LISTING)[0]
        row = build_row(listing_row, {}, {}, "", "kuwait")
        self.assertEqual(row["title"], "Commercial Manager")
        self.assertEqual(row["posted_date"],
                         parse_listing_date("13 May"))   # listing-date fallback
        self.assertEqual(row["salary_raw"], "Not Disclosed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
