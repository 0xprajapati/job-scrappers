#!/usr/bin/env python3
"""Unit tests for the devnetjobs (global) scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real postings on devnetjobs.org (Aug 2026):
job_id 313685 (Psychologist, CARE Egypt Foundation — carries the site's
stray trailing `}` and the "Apply by" span mislabeled lblPostedDate),
313584 (Lead Economist — a worldwide-remote posting with no jobLocation),
313554 (Head of Mission — the multi-country "SO, KE" addressCountry),
313565 (an "(RFP500586)" reference number that must NOT read as an RFP).
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
    parse_country_codes,
    parse_experience_years,
    parse_job_posting,
    parse_sectors,
    parse_sitemap_ids,
    rich_row_to_club_row,
    sectors_for_classifier,
)

# Trimmed real sitemap.aspx shape — every <lastmod> is the generation date,
# and ~27 static pages share the file with the job urls.
SITEMAP_XML = """<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://devnetjobs.org/</loc>
    <lastmod>2026-08-27</lastmod></url>
  <url><loc>https://devnetjobs.org/standard_jobs.aspx</loc>
    <lastmod>2026-08-27</lastmod></url>
  <url><loc>https://devnetjobs.org/jobdescription.aspx?job_id=313685</loc>
    <lastmod>2026-08-27</lastmod></url>
  <url><loc>https://devnetjobs.org/jobdescription.aspx?job_id=313554</loc>
    <lastmod>2026-08-27</lastmod></url>
  <url><loc>https://devnetjobs.org/jobdescription.aspx?job_id=290143</loc>
    <lastmod>2026-08-27</lastmod></url>
</urlset>"""

# Real JSON-LD shape, INCLUDING the site's stray trailing `}` bug, wrapped
# the way the detail page embeds it. Note the sector spans carry ASP.NET's
# full ctl00_ control ids on this board (the India board strips them), and
# the visible date span is "Apply by", not the posted date.
DETAIL_HTML = """
<script type="application/ld+json">{"@context": "https://schema.org",
"@type": "JobPosting","title": "Community Health Outreach Officer",
"description": "<div><b>Requirements:</b></div><ul><li>Minimum 4 years of
experience in community health programming.</li><li>Applicants must be in
the age group 25-40 years.</li></ul><p>Community health outreach, HIV
counselling and testing linkage, and health promotion for key populations
in Aswan.</p>",
"datePosted": "2026-08-26","validThrough": "2026-09-03",
"hiringOrganization": {"@type": "Organization",
"name": "CARE Egypt Foundation (CEF)"},
"jobLocation": {"@type": "Place","address": {"@type": "PostalAddress",
"addressLocality": "Aswan","addressCountry": "EG"}},
"identifier": {"@type": "PropertyValue","name": "DevNetJobs",
"value": "313685"},
"url": "https://devnetjobs.org/jobdescription.aspx?job_id=313685"}}</script>
<p class="jd-sectorContent"><span id="ctl00_ContentPlaceHolder1_JD1_lblSector1">Health, Doctors, Nurses, HIV/AIDS</span></p>
<p class="jd-sectorContent"><span id="ctl00_ContentPlaceHolder1_JD1_lblSector2">Social, Education, Gender, Youth, Child</span></p>
<p class="jd-sectorContent"><span id="ctl00_ContentPlaceHolder1_JD1_lblSector3"></span></p>
<p class="m-0">Apply by: <span id="ctl00_ContentPlaceHolder1_JD1_lblPostedDate">03 Sep 2026</span></p>
"""


class TestSitemapParser(unittest.TestCase):
    def test_ids_newest_first(self):
        self.assertEqual(parse_sitemap_ids(SITEMAP_XML),
                         ["313685", "313554", "290143"])

    def test_static_pages_ignored(self):
        self.assertNotIn("standard_jobs", "".join(parse_sitemap_ids(SITEMAP_XML)))

    def test_empty(self):
        self.assertEqual(parse_sitemap_ids("<urlset></urlset>"), [])
        self.assertEqual(parse_sitemap_ids(""), [])


class TestDetailParser(unittest.TestCase):
    def test_trailing_brace_tolerated(self):
        # json.loads raises "Extra data" on the site's stray `}` —
        # raw_decode must not.
        posting = parse_job_posting(DETAIL_HTML)
        self.assertEqual(posting.get("title"),
                         "Community Health Outreach Officer")
        self.assertEqual(posting.get("datePosted"), "2026-08-26")

    def test_non_jobposting_skipped(self):
        html = ('<script type="application/ld+json">{"@type": "Organization"}'
                '</script>')
        self.assertEqual(parse_job_posting(html), {})

    def test_sectors_with_ctl00_prefix(self):
        # This board emits the full ASP.NET control id; empty slots drop out.
        self.assertEqual(
            parse_sectors(DETAIL_HTML),
            "Health, Doctors, Nurses, HIV/AIDS; "
            "Social, Education, Gender, Youth, Child")

    def test_sectors_without_prefix_still_read(self):
        html = ('<span id="ContentPlaceHolder1_JD1_lblSector1">Health, '
                'Doctors, Nurses, HIV/AIDS</span>')
        self.assertEqual(parse_sectors(html), "Health, Doctors, Nurses, HIV/AIDS")

    def test_sectors_optional(self):
        # 26 of 60 sampled global postings carry no sector tags at all.
        self.assertEqual(parse_sectors("<html><body>no tags</body></html>"), "")

    def test_country_codes(self):
        self.assertEqual(parse_country_codes("EG"), ["EG"])
        self.assertEqual(parse_country_codes("SO, KE"), ["SO", "KE"])
        self.assertEqual(parse_country_codes(None), [])
        self.assertEqual(parse_country_codes(""), [])

    def test_experience_minimum_not_age_range(self):
        # "Minimum 4 years of experience" is read; "age group 25-40 years"
        # must never be.
        self.assertEqual(parse_experience_years(
            "Applicants must be in the age group 25-40 years. Minimum 4 "
            "years of experience in community health programming."), "4")
        self.assertEqual(parse_experience_years(
            "Applicants must be in the age group 25-40 years."), "")
        self.assertEqual(parse_experience_years(
            "3+ years of relevant experience in M&E."), "3")
        self.assertEqual(parse_experience_years(""), "")

    def test_rfp_titles(self):
        for title in ("RFP - Empanelment of implementation partners",
                      "Expression of Interest: Baseline Survey",
                      "Call for Proposals — HIV prevention"):
            self.assertTrue(is_rfp_title(title), title)
        self.assertFalse(is_rfp_title("Community Health Outreach Officer"))

    def test_rfp_reference_number_is_not_an_rfp(self):
        # Real title 313565 — the reference number must not trip the flag.
        self.assertFalse(is_rfp_title(
            "Associate, Administrative & Project Support (RFP500586)"))

    def test_procurement_shapes_that_dodge_the_rfp_vocabulary(self):
        # Real titles 313288 and 312798 — tenders whose titles use none of
        # the RFP/EOI/tender words.
        self.assertTrue(is_rfp_title(
            "Calling for Data Collection Teams for Fowash Project "
            "Endline Survey"))
        self.assertTrue(is_rfp_title(
            "Software Development, Update, and Maintenance Support Services "
            "for Infectious Disease Surveillance and Early Warning System"))

    def test_calling_for_applications_is_not_a_tender(self):
        # The narrow patterns must not swallow ordinary job ads.
        for title in ("Calling for applications: Programme Officer",
                      "Calling for expressions of talent — Nurse Educator",
                      "Health Services Coordinator",
                      "Support Services Officer"):
            self.assertFalse(is_rfp_title(title), title)


class TestSectorsForClassifier(unittest.TestCase):
    """The funding-sector tag is dropped before classification, never from
    the stored row (see sectors_for_classifier)."""

    FUNDRAISING = "Fundraising, Business Development, Grants Writer"

    def test_funding_tag_dropped(self):
        self.assertEqual(
            sectors_for_classifier(
                "Capacity Building, Training, Advocacy; " + self.FUNDRAISING +
                "; Health, Doctors, Nurses, HIV/AIDS"),
            "Capacity Building, Training, Advocacy; "
            "Health, Doctors, Nurses, HIV/AIDS")

    def test_only_that_tag_is_dropped(self):
        # The other 15 tags in the board's closed vocabulary pass through.
        keep = ("Health, Doctors, Nurses, HIV/AIDS; "
                "Monitoring, Evaluation, Policy, Research, Analysis")
        self.assertEqual(sectors_for_classifier(keep), keep)

    def test_sole_tag_leaves_empty_skills(self):
        self.assertEqual(sectors_for_classifier(self.FUNDRAISING), "")

    def test_empty_and_missing(self):
        self.assertEqual(sectors_for_classifier(""), "")
        self.assertEqual(sectors_for_classifier(None), "")

    def test_health_role_survives_the_funding_tag(self):
        """The whole point: a health role tagged into a fundraising unit is
        no longer vetoed by the taxonomy's "Business Development" keyword."""
        row = {"title": "Monitoring, Evaluation and Learning Officer",
               "sectors": "Health, Doctors, Nurses, HIV/AIDS; " +
                          self.FUNDRAISING,
               "description": "Monitoring and evaluation of community "
                              "health and HIV programme indicators.",
               "is_rfp": False}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        # ...and the raw tag is still on the row that gets stored.
        self.assertIn("Business Development", row["sectors"])


class TestClassifier(unittest.TestCase):
    """Wiring into the shared taxonomy — the engine itself is covered by
    ../_shared/test_classification.py."""

    def _row(self, title, sectors="", description="", is_rfp=False):
        return {"title": title, "sectors": sectors,
                "description": description, "is_rfp": is_rfp}

    def test_public_health_role_kept(self):
        row = self._row(
            "Senior Health Specialist, Maternal and Newborn Health",
            "Health, Doctors, Nurses, HIV/AIDS",
            "Technical leadership on maternal and newborn health "
            "programmes: community health systems, immunisation and "
            "public health service delivery in country offices.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")

    def test_ingo_admin_role_dropped(self):
        """Global-board supply is mostly out of scope: admin, fundraising,
        logistics and IT roles are dropped, not relabelled."""
        for title in ("Procurement and Travel Manager",
                      "Manager, Major Giving East",
                      "IT Support"):
            row = self._row(title, "Administration, Management, "
                                   "Finance/Accounting, Procurement")
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_in_scope_rfp_is_flagged(self):
        row = self._row(
            "RFP - Endline Evaluation of HIV Prevention Programme",
            "Health, Doctors, Nurses, HIV/AIDS",
            "Request for proposals to conduct the endline evaluation, "
            "monitoring and evaluation of the HIV prevention programme.",
            is_rfp=True)
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_company_type(self):
        self.assertEqual(classify_company_type("CARE Egypt Foundation (CEF)"),
                         "hospital")
        self.assertEqual(classify_company_type("Clinical Research Labs Ltd"),
                         "pharma")


class TestRowBuilding(unittest.TestCase):
    def test_row_and_club_row(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("313685", posting, parse_sectors(DETAIL_HTML))
        self.assertEqual(row["job_id"], "313685")
        self.assertEqual(row["posted_date"], "2026-08-26")
        self.assertEqual(row["valid_through"], "2026-09-03")
        self.assertEqual(row["city"], "Aswan")
        self.assertEqual(row["country"], "EG")
        self.assertEqual(row["experience_min_years"], "4")
        self.assertFalse(row["is_rfp"])
        self.assertFalse(row["remote"])
        self.assertIn("community health outreach", row["description"].lower())
        self.assertNotIn("<", row["description"])  # HTML stripped

        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_name"], "Egypt")
        self.assertEqual(club["country_code"], "EG")
        self.assertEqual(club["country_dial_code"], "+20")
        self.assertEqual(club["city_name"], "Aswan")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["posted_at"], "2026-08-26")
        self.assertEqual(club["min_experience"], "4")
        self.assertEqual(club["min_salary"], "")     # board publishes none
        self.assertEqual(club["application_url"],
                         "https://devnetjobs.org/jobdescription.aspx"
                         "?job_id=313685")

    def test_multi_country_takes_duty_station_first(self):
        posting = {"@type": "JobPosting", "title": "Head of Mission",
                   "datePosted": "2026-08-25",
                   "jobLocation": {"address": {"addressLocality": "Mogadishu",
                                               "addressCountry": "SO, KE"}}}
        row = build_row("313554", posting, "")
        self.assertEqual(row["country"], "SO")
        self.assertEqual(row["country_codes"], "SO, KE")
        self.assertEqual(rich_row_to_club_row(row)["country_name"], "Somalia")

    def test_worldwide_remote_without_job_location(self):
        posting = {"@type": "JobPosting", "title": "Lead Economist",
                   "datePosted": "2026-08-26",
                   "jobLocationType": "TELECOMMUTE",
                   "applicantLocationRequirements": {"@type": "Country",
                                                     "name": "Worldwide"}}
        row = build_row("313584", posting, "")
        self.assertTrue(row["remote"])
        self.assertEqual(row["country"], "")
        self.assertEqual(row["city"], "Worldwide")
        club = rich_row_to_club_row(row)
        self.assertEqual(club["job_type"], "remote")
        # Fleet (himalayas) convention for remote-only rows: the region name
        # goes to country_name, city_name says "Remote", and the country
        # columns are never all blank. No ISO code exists for "Worldwide",
        # so code/dial stay empty rather than being invented.
        self.assertEqual(club["city_name"], "Remote")
        self.assertEqual(club["country_name"], "Worldwide")
        self.assertEqual(club["country_code"], "")
        self.assertEqual(club["country_dial_code"], "")

    def test_remote_posting_with_remote_as_its_locality(self):
        # DEVNETJOBS-02: the real 312899 shape ("Director, Global Health and
        # Development Team", Rethink Priorities) — TELECOMMUTE with
        # addressLocality "Remote" and no addressCountry shipped with
        # country_name/code/dial ALL empty. It must export as
        # Worldwide/Remote instead.
        posting = {"@type": "JobPosting",
                   "title": "Director, Global Health and Development Team",
                   "datePosted": "2026-08-14",
                   "jobLocationType": "TELECOMMUTE",
                   "jobLocation": {"address": {"addressLocality": "Remote"}}}
        row = build_row("312899", posting, "")
        self.assertTrue(row["remote"])
        self.assertEqual(row["country"], "")
        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_name"], "Worldwide")
        self.assertEqual(club["country_code"], "")
        self.assertEqual(club["country_dial_code"], "")
        self.assertEqual(club["city_name"], "Remote")
        self.assertEqual(club["job_type"], "remote")

    def test_remote_within_one_country_keeps_that_country(self):
        # Remote but country-restricted: the duty-station country wins,
        # "Worldwide" is only for rows with no country at all.
        posting = {"@type": "JobPosting", "title": "Health Data Analyst",
                   "datePosted": "2026-08-26",
                   "jobLocationType": "TELECOMMUTE",
                   "jobLocation": {"address": {"addressLocality": "Remote",
                                               "addressCountry": "KE"}}}
        club = rich_row_to_club_row(build_row("313700", posting, ""))
        self.assertEqual(club["country_name"], "Kenya")
        self.assertEqual(club["country_code"], "KE")
        self.assertEqual(club["country_dial_code"], "+254")
        self.assertEqual(club["city_name"], "Remote")
        self.assertEqual(club["job_type"], "remote")

    def test_unknown_country_code_is_not_guessed(self):
        posting = {"@type": "JobPosting", "title": "Programme Officer",
                   "datePosted": "2026-08-26",
                   "jobLocation": {"address": {"addressCountry": "ZZ"}}}
        club = rich_row_to_club_row(build_row("1", posting, ""))
        self.assertEqual(club["country_code"], "ZZ")
        self.assertEqual(club["country_name"], "")
        self.assertEqual(club["country_dial_code"], "")

    def test_club_row_survives_csv_roundtrip_booleans(self):
        # The rich CSV stores every column as a string, so `remote` comes
        # back as "True"/"False", not a bool.
        club = rich_row_to_club_row({"title": "Lead Economist", "remote": "True",
                                     "city": "", "country": ""})
        self.assertEqual(club["job_type"], "remote")
        self.assertEqual(club["city_name"], "Remote")
        self.assertEqual(club["country_name"], "Worldwide")


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
