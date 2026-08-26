#!/usr/bin/env python3
"""Unit tests for the devnetjobsindia scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real postings on devnetjobsindia.org (Aug 2026):
job_id 302532 (Azim Premji Foundation), 302709 (HIV/AIDS TI project),
302836 (an RFP sharing the job_id space).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_row,
    classify_company_type,
    compute_cutoff,
    is_rfp_title,
    parse_experience_years,
    parse_job_posting,
    parse_sectors,
    parse_sitemap_ids,
    rich_row_to_club_row,
)

# Trimmed real sitemap.aspx shape — every <lastmod> is the generation date.
SITEMAP_XML = """<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://devnetjobsindia.org/</loc>
    <lastmod>2026-08-26</lastmod></url>
  <url><loc>https://devnetjobsindia.org/standard_jobs.aspx</loc>
    <lastmod>2026-08-26</lastmod></url>
  <url><loc>https://devnetjobsindia.org/jobdescription.aspx?job_id=302532</loc>
    <lastmod>2026-08-26</lastmod></url>
  <url><loc>https://devnetjobsindia.org/jobdescription.aspx?job_id=302836</loc>
    <lastmod>2026-08-26</lastmod></url>
  <url><loc>https://devnetjobsindia.org/jobdescription.aspx?job_id=298281</loc>
    <lastmod>2026-08-26</lastmod></url>
</urlset>"""

# Real JSON-LD shape, INCLUDING the site's stray trailing `}` bug, wrapped
# the way the detail page embeds it (job_id 302709, trimmed).
DETAIL_HTML = """
<script type="application/ld+json">{"@context": "https://schema.org",
"@type": "JobPosting","title": "Community Liaison Health/Outreach Worker",
"description": "<div><b>Eligibility:</b></div><ul><li>Minimum 4 years of
experience of working in a civil society organization.</li><li>Must be in
the age group 25-40 years.</li></ul><p>HIV/AIDS targeted intervention
programme: community outreach, counselling and testing linkage for key
populations.</p>",
"datePosted": "2026-08-25","validThrough": "2026-09-04",
"hiringOrganization": {"@type": "Organization",
"name": "Banaras Network for Positive People Living  with HIV/AIDS Society"},
"jobLocation": {"@type": "Place","address": {"@type": "PostalAddress",
"addressLocality": "Sonbhadra","addressRegion": "Uttar Pradesh",
"addressCountry": "IN"}},
"identifier": {"@type": "PropertyValue","name": "DevNetJobsIndia",
"value": "302709"},
"url": "https://devnetjobsindia.org/jobdescription.aspx?job_id=302709"}}</script>
<p class="jd-sectorContent"><span id="ContentPlaceHolder1_JD1_lblSector1">Health, Doctors, Nurses, HIV/AIDS, Nutrition</span></p>
<p class="jd-sectorContent"><span id="ContentPlaceHolder1_JD1_lblSector2">Social, Gender, Education, Youth, Child</span></p>
<p class="jd-sectorContent"><span id="ContentPlaceHolder1_JD1_lblSector3"></span></p>
"""


class TestSitemapParser(unittest.TestCase):
    def test_ids_newest_first(self):
        self.assertEqual(parse_sitemap_ids(SITEMAP_XML),
                         ["302836", "302532", "298281"])

    def test_empty(self):
        self.assertEqual(parse_sitemap_ids("<urlset></urlset>"), [])
        self.assertEqual(parse_sitemap_ids(""), [])


class TestDetailParser(unittest.TestCase):
    def test_trailing_brace_tolerated(self):
        # json.loads raises "Extra data" on the site's stray `}` —
        # raw_decode must not.
        posting = parse_job_posting(DETAIL_HTML)
        self.assertEqual(posting.get("title"),
                         "Community Liaison Health/Outreach Worker")
        self.assertEqual(posting.get("datePosted"), "2026-08-25")

    def test_non_jobposting_skipped(self):
        html = ('<script type="application/ld+json">{"@type": "Organization"}'
                '</script>')
        self.assertEqual(parse_job_posting(html), {})

    def test_sectors(self):
        self.assertEqual(
            parse_sectors(DETAIL_HTML),
            "Health, Doctors, Nurses, HIV/AIDS, Nutrition; "
            "Social, Gender, Education, Youth, Child")

    def test_experience_minimum_not_age_range(self):
        # "Minimum 4 years of experience" is read; "age group 25-40 years"
        # must never be.
        self.assertEqual(parse_experience_years(
            "Must be in the age group 25-40 years. Minimum 4 years of "
            "experience of working in a civil society organization."), "4")
        self.assertEqual(parse_experience_years(
            "Must be in the age group 25-40 years."), "")
        self.assertEqual(parse_experience_years(
            "3+ years of relevant experience in M&E."), "3")
        self.assertEqual(parse_experience_years(""), "")

    def test_rfp_titles(self):
        for title in ("RFP - Empanelment of development & implementation "
                      "partners for CSR projects",
                      "Expression of Interest: Baseline Survey",
                      "Call for Proposals — HIV prevention"):
            self.assertTrue(is_rfp_title(title), title)
        self.assertFalse(is_rfp_title("Community Liaison Health/Outreach Worker"))


class TestClassifier(unittest.TestCase):
    """Wiring into the shared taxonomy — the engine itself is covered by
    ../_shared/test_classification.py."""

    def _row(self, title, sectors="", description="", is_rfp=False):
        return {"title": title, "sectors": sectors,
                "description": description, "is_rfp": is_rfp}

    def test_public_health_role_kept(self):
        row = self._row(
            "District Epidemiologist",
            "Health, Doctors, Nurses, HIV/AIDS, Nutrition",
            "Disease surveillance and outbreak investigation for the "
            "district health society; epidemiology and public health "
            "reporting.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Epidemiology")

    def test_ngo_admin_role_dropped(self):
        """NGO-board supply is mostly out of scope: admin, livelihoods,
        education roles are dropped, not relabelled."""
        for title in ("Accounts & Admin Officer",
                      "Community Mobilization & Mushroom Farming Officer",
                      "Fundraising Manager"):
            row = self._row(title, "Administration, HR, Management, "
                                   "Accounting/Finance")
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_in_scope_rfp_is_flagged(self):
        row = self._row(
            "RFP - Endline Evaluation of HIV Targeted Intervention Programme",
            "Health, Doctors, Nurses, HIV/AIDS, Nutrition",
            "Request for proposals to conduct the endline evaluation, "
            "monitoring and evaluation of the HIV prevention programme.",
            is_rfp=True)
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_company_type(self):
        self.assertEqual(classify_company_type("Azim Premji Foundation"),
                         "hospital")
        self.assertEqual(classify_company_type("Clinical Research Labs Pvt Ltd"),
                         "pharma")


class TestRowBuilding(unittest.TestCase):
    def test_row_and_club_row(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("302709", posting, parse_sectors(DETAIL_HTML))
        self.assertEqual(row["job_id"], "302709")
        self.assertEqual(row["posted_date"], "2026-08-25")
        self.assertEqual(row["valid_through"], "2026-09-04")
        self.assertEqual(row["city"], "Sonbhadra")
        self.assertEqual(row["state"], "Uttar Pradesh")
        self.assertEqual(row["experience_min_years"], "4")
        self.assertFalse(row["is_rfp"])
        self.assertIn("community outreach", row["description"])
        self.assertNotIn("<", row["description"])  # HTML stripped

        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["city_name"], "Sonbhadra")
        self.assertEqual(club["posted_at"], "2026-08-25")
        self.assertEqual(club["min_experience"], "4")
        self.assertEqual(club["min_salary"], "")     # board publishes none
        self.assertEqual(club["application_url"],
                         "https://devnetjobsindia.org/jobdescription.aspx"
                         "?job_id=302709")

    def test_nationwide_region_is_not_a_state(self):
        # RFP-style rows: addressRegion is the literal "India".
        posting = {"@type": "JobPosting", "title": "RFP - Empanelment of partners",
                   "datePosted": "2026-08-26",
                   "jobLocation": {"address": {"addressRegion": "India",
                                               "addressCountry": "IN"}}}
        row = build_row("302836", posting, "")
        self.assertEqual(row["state"], "")
        self.assertTrue(row["is_rfp"])
        self.assertEqual(rich_row_to_club_row(row)["city_name"], "India")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-19", ""]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 8, 19) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_since_override(self):
        self.assertEqual(compute_cutoff(None, since="2026-07-01"), "2026-07-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
