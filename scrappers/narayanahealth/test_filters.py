#!/usr/bin/env python3
"""Unit tests for the narayanahealth scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real listings on jobs.narayanahealth.org
(July 2026).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_detail,
    classify_category,
    compute_cutoff,
    job_id_from_url,
    listing_to_rich_row,
    parse_detail_page,
    parse_listing_date,
    parse_location,
    parse_meta_date,
    parse_search_page,
    rich_row_to_club_row,
    strip_html,
)

# Trimmed real search-row markup (Junior Executive, Jaipur, 24 Jul 2026).
SEARCH_HTML = """
<span class="paginationLabel">Results <b>1 &ndash; 10</b> of <b>116</b></span>
<table><tbody>
<tr class="data-row">
  <td class="colTitle" headers="hdrTitle">
    <span class="jobTitle hidden-phone">
      <a href="/NH-India/job/Jaipur-Junior-Executive-RJ-302033/58185744/" class="jobTitle-link">Junior Executive</a>
    </span>
    <div class="jobdetail-phone visible-phone">
      <span class="jobTitle visible-phone">
        <a class="jobTitle-link" href="/NH-India/job/Jaipur-Junior-Executive-RJ-302033/58185744/">Junior Executive</a>
      </span>
      <span class="jobLocation visible-phone">
        <span class="jobLocation">
            Jaipur, RJ, IN, 302033
        </span></span>
      <span class="jobDate visible-phone">24 Jul 2026</span>
    </div>
  </td>
  <td class="colLocation hidden-phone"><span class="jobLocation">
      Jaipur, RJ, IN, 302033
  </span></td>
  <td class="colDate hidden-phone"><span class="jobDate">24 Jul 2026
  </span></td>
  <td class="colFacility hidden-phone"><span class="jobFacility">14998</span></td>
</tr>
</tbody></table>
"""

# Trimmed real detail-page markup (same job).
DETAIL_HTML = """
<div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting">
<meta itemprop="datePosted" content="Fri Jul 24 00:00:00 UTC 2026">
<meta itemprop="validThrough" content="Fri Jul 31 18:30:00 UTC 2026">
<meta itemprop="hiringOrganization" content="Narayana Hrudayalaya Limited">
<span itemprop="description" data-careersite-propertyid="description">
<span class="jobdescription"><ul>
<li><span>Supervision of floor that have been allotted to them.</span></li>
<li><span>Attending to all Departmental Meeting.</span></li>
</ul></span></span>
<p class="job-location"><span class="jobmarkets"></span></p>
<div class="applylink pull-right"><a href="/talentcommunity/apply/58185744/">Apply now</a></div>
</div>
"""


class TestSearchParsing(unittest.TestCase):
    def test_parse_search_page(self):
        rows, total = parse_search_page(SEARCH_HTML)
        self.assertEqual(total, 116)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["title"], "Junior Executive")
        self.assertEqual(row["href"],
                         "/NH-India/job/Jaipur-Junior-Executive-RJ-302033/58185744/")
        self.assertEqual(row["location"], "Jaipur, RJ, IN, 302033")
        self.assertEqual(row["posted_raw"], "24 Jul 2026")
        self.assertEqual(row["requisition_id"], "14998")

    def test_job_id_from_url(self):
        self.assertEqual(job_id_from_url(
            "/NH-India/job/Jaipur-Junior-Executive-RJ-302033/58185744/"),
            "58185744")
        self.assertEqual(job_id_from_url(
            "https://jobs.narayanahealth.org/NH-India/job/X/56457144"),
            "56457144")
        self.assertEqual(job_id_from_url("/no/id/here/"), "")


class TestDates(unittest.TestCase):
    def test_listing_date(self):
        self.assertEqual(parse_listing_date("24 Jul 2026"), "2026-07-24")
        self.assertEqual(parse_listing_date("1 Jan 2026"), "2026-01-01")
        self.assertEqual(parse_listing_date(""), "")
        self.assertEqual(parse_listing_date("garbage"), "")

    def test_meta_date(self):
        self.assertEqual(parse_meta_date("Fri Jul 24 00:00:00 UTC 2026"),
                         "2026-07-24")
        self.assertEqual(parse_meta_date(""), "")


class TestLocation(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(parse_location("Jaipur, RJ, IN, 302033"),
                         ("Jaipur", "RJ", "IN", "302033"))

    def test_city_equals_state(self):
        # Real listing: New Delhi twice
        self.assertEqual(parse_location("New Delhi, New Delhi, IN, 110096"),
                         ("New Delhi", "New Delhi", "IN", "110096"))

    def test_partial(self):
        self.assertEqual(parse_location("Bangalore"),
                         ("Bangalore", "", "", ""))
        self.assertEqual(parse_location(""), ("", "", "", ""))

    def test_country_only(self):
        # Real listings ("Pharmacist" 56194244) show location as just "IN"
        self.assertEqual(parse_location("IN"), ("", "", "IN", ""))
        row = listing_to_rich_row({
            "href": "/NH-India/job/Pharmacist/56194244/", "title": "Pharmacist",
            "location": "IN", "posted_raw": "20 Jul 2026",
            "requisition_id": ""})
        self.assertEqual(row["city"], "")
        self.assertEqual(rich_row_to_club_row(row)["city_name"], "India")


class TestClassifier(unittest.TestCase):
    """Titles from real listings on the site."""

    def test_staff_nurse(self):
        category, review = classify_category("Staff Nurse")
        self.assertEqual(category, "nurses")
        self.assertFalse(review)

    def test_senior_staff_nurse(self):
        category, _ = classify_category("Senior Staff Nurse")
        self.assertEqual(category, "nurses")

    def test_senior_registrar_is_doctor(self):
        category, review = classify_category("Senior Registrar")
        self.assertEqual(category, "doctors")
        self.assertFalse(review)

    def test_consultant_finance_not_doctor(self):
        category, _ = classify_category("Consultant - Finance")
        self.assertEqual(category, "non_clinical")

    def test_pharmacist(self):
        category, review = classify_category("Pharmacist")
        self.assertEqual(category, "pharmacists")
        self.assertFalse(review)

    def test_technician_non_clinical_no_review(self):
        # Real listing "Technician" (Bangalore) — healthcare signal word
        category, review = classify_category("Technician")
        self.assertEqual(category, "non_clinical")
        self.assertFalse(review)

    def test_junior_executive_flagged(self):
        # Real listing "Junior Executive" — no healthcare word at all
        category, review = classify_category("Junior Executive")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(review)


class TestRowBuilding(unittest.TestCase):
    LISTING = {
        "href": "/NH-India/job/Jaipur-Junior-Executive-RJ-302033/58185744/",
        "title": "Junior Executive",
        "location": "Jaipur, RJ, IN, 302033",
        "posted_raw": "24 Jul 2026",
        "requisition_id": "14998",
    }

    def test_rich_row(self):
        row = listing_to_rich_row(self.LISTING)
        self.assertEqual(row["job_id"], "58185744")
        self.assertEqual(row["requisition_id"], "14998")
        self.assertEqual(row["city"], "Jaipur")
        self.assertEqual(row["state"], "RJ")
        self.assertEqual(row["country"], "India")
        self.assertEqual(row["country_dial_code"], "+91")
        self.assertEqual(row["posted_date"], "2026-07-24")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["job_url"],
                         "https://jobs.narayanahealth.org/NH-India/job/"
                         "Jaipur-Junior-Executive-RJ-302033/58185744/")
        self.assertTrue(row["needs_review"])

    def test_apply_detail_and_club_row(self):
        row = listing_to_rich_row(self.LISTING)
        extras = parse_detail_page(DETAIL_HTML)
        self.assertEqual(extras["posted_date"], "2026-07-24")
        self.assertEqual(extras["valid_through"], "2026-07-31")
        self.assertEqual(extras["company"], "Narayana Hrudayalaya Limited")
        self.assertIn("Supervision of floor", extras["description"])
        self.assertNotIn("Apply now", extras["description"])
        self.assertNotIn("<", extras["description"])

        apply_detail(row, extras)
        club = rich_row_to_club_row(row)
        self.assertEqual(club["company_name"], "Narayana Hrudayalaya Limited")
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(club["posted_at"], "2026-07-24")
        self.assertEqual(club["expires_at"], "2026-07-31")
        self.assertEqual(club["city_name"], "Jaipur")
        self.assertEqual(club["min_salary"], "")   # never disclosed
        self.assertEqual(club["salary_currency"], "")
        self.assertEqual(club["is_active"], "true")
        self.assertEqual(club["application_url"], row["job_url"])

    def test_strip_html(self):
        self.assertEqual(strip_html("<p>a&amp;b</p><br>c"), "a&b c")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark(self):
        df = pd.DataFrame({"posted_date": ["2026-07-10", "2026-07-20", ""]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 7, 20) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_all_dates_bad_falls_back(self):
        df = pd.DataFrame({"posted_date": ["", "not-a-date"]})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
