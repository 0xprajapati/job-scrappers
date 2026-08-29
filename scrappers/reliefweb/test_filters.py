#!/usr/bin/env python3
"""Unit tests for the reliefweb.int scraper's parsers, classifier wiring and
cutoff.

Run with plain `python test_filters.py` (no pytest needed).

Every example marked "real feed item" was taken verbatim from
https://reliefweb.int/jobs/rss.xml (unfiltered + Health-theme facet) pulled
on 2026-08-26. The classification engines are covered by _shared's own
tests; here only the wiring is asserted (master spec: an in-scope role gets
the right category/sub_category, an out-of-scope title is dropped).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from reliefweb_scraper import (
    classify_company_type,
    classify_feed_job,
    closing_date_from_description,
    compute_cutoff,
    job_id_from_link,
    normalize_country,
    parse_closing_date,
    parse_feed,
    parse_rfc822_date,
    rich_row_to_club_row,
    split_categories,
    strip_html,
    truncate_description,
    WINDOW_DAYS,
)


class TestJobIdFromLink(unittest.TestCase):
    def test_real_link(self):
        # real feed item: Network Coordinator Kenya
        self.assertEqual(
            job_id_from_link(
                "https://reliefweb.int/job/4227017/network-coordinator-kenya"),
            "4227017")

    def test_no_id(self):
        self.assertEqual(job_id_from_link("https://reliefweb.int/jobs"), "")
        self.assertEqual(job_id_from_link(""), "")
        self.assertEqual(job_id_from_link(None), "")


class TestDates(unittest.TestCase):
    def test_real_pubdate(self):
        # real feed item pubDate
        self.assertEqual(parse_rfc822_date("Wed, 26 Aug 2026 09:44:22 +0000"),
                         "2026-08-26")

    def test_bad_pubdate(self):
        self.assertEqual(parse_rfc822_date("not a date"), "")
        self.assertEqual(parse_rfc822_date(""), "")

    def test_real_closing_date(self):
        # real feed format: "<div class='date closing'>Closing date: 8 Sep 2026"
        self.assertEqual(parse_closing_date("8 Sep 2026"), "2026-09-08")
        self.assertEqual(parse_closing_date("1 Oct 2026"), "2026-10-01")

    def test_closing_date_from_description(self):
        desc = ('<div class="tag country">Country: Kenya</div>'
                '<div class="tag source">Organization: X</div>'
                '<div class="date closing">Closing date: 8 Sep 2026</div>'
                '<p>Body</p>')
        self.assertEqual(closing_date_from_description(desc), "2026-09-08")

    def test_closing_date_missing(self):
        # real behavior: ~half the feed items carry no closing-date div
        self.assertEqual(closing_date_from_description("<p>Body only</p>"), "")


class TestSplitCategories(unittest.TestCase):
    """The <category> elements are an unlabeled mix; they are told apart via
    the river's closed vocabularies + the <author> organization."""

    def test_real_item(self):
        # real feed item: Network Coordinator Kenya (Mensen met een Missie)
        meta = split_categories(
            ["Kenya", "Mensen met een Missie", "Program/Project Management",
             "Job", "Peacekeeping and Peacebuilding"],
            organization="Mensen met een Missie")
        self.assertEqual(meta["countries"], ["Kenya"])
        self.assertEqual(meta["career_categories"],
                         ["Program/Project Management"])
        self.assertEqual(meta["job_type"], "Job")
        self.assertEqual(meta["themes"], ["Peacekeeping and Peacebuilding"])

    def test_real_consultancy_multi_theme(self):
        # real feed item: Solidar Suisse Venezuela consultancy
        meta = split_categories(
            ["Venezuela (Bolivarian Republic of)", "Solidar Suisse",
             "Program/Project Management", "Consultancy",
             "Water Sanitation Hygiene"],
            organization="Solidar Suisse")
        self.assertEqual(meta["countries"],
                         ["Venezuela (Bolivarian Republic of)"])
        self.assertEqual(meta["job_type"], "Consultancy")
        self.assertEqual(meta["themes"], ["Water Sanitation Hygiene"])

    def test_multi_country(self):
        meta = split_categories(
            ["Kenya", "Uganda", "Save the Children", "Health", "Job"],
            organization="Save the Children")
        self.assertEqual(meta["countries"], ["Kenya", "Uganda"])
        self.assertEqual(meta["themes"], ["Health"])

    def test_empty(self):
        meta = split_categories([], organization="")
        self.assertEqual(meta, {"countries": [], "career_categories": [],
                                "job_type": "", "themes": []})


class TestNormalizeCountry(unittest.TestCase):
    def test_formal_un_names(self):
        self.assertEqual(normalize_country("Venezuela (Bolivarian Republic of)"),
                         "Venezuela")
        self.assertEqual(normalize_country("Viet Nam"), "Vietnam")
        self.assertEqual(normalize_country("occupied Palestinian territory"),
                         "Palestine")

    def test_plain_name_passthrough(self):
        self.assertEqual(normalize_country("Kenya"), "Kenya")
        self.assertEqual(normalize_country("  Mali "), "Mali")


class TestStripHtml(unittest.TestCase):
    def test_metadata_divs_removed(self):
        """The tagged metadata divs must never pollute the description the
        classifier scores — 'Country: Kenya Organization: ...' is not body."""
        desc = ('<div class="tag country">Country: Kenya</div>'
                '<div class="tag source">Organization: Acme NGO</div>'
                '<div class="date closing">Closing date: 8 Sep 2026</div>'
                '<p>Lead the community health programme.</p>')
        text = strip_html(desc)
        self.assertEqual(text, "Lead the community health programme.")
        self.assertNotIn("Closing date", text)

    def test_entities_unescaped(self):
        self.assertEqual(strip_html("<p>M&amp;E officer</p>"), "M&E officer")


class TestTruncateDescription(unittest.TestCase):
    def test_short_untouched(self):
        self.assertEqual(truncate_description("short", 100), "short")

    def test_word_boundary_and_marker(self):
        out = truncate_description("alpha beta gamma delta", 12)
        self.assertEqual(out, "alpha beta…")


class TestParseFeed(unittest.TestCase):
    XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>ReliefWeb - Jobs</title>
<item>
  <title>Programme Manager - Community Health</title>
  <link>https://reliefweb.int/job/4226938/programme-manager-community-health</link>
  <author>Acme NGO</author>
  <pubDate>Tue, 25 Aug 2026 11:53:03 +0000</pubDate>
  <category>Kenya</category>
  <category>Acme NGO</category>
  <category>Program/Project Management</category>
  <category>Job</category>
  <category>Health</category>
  <description>&lt;div class="tag country"&gt;Country: Kenya&lt;/div&gt;&lt;p&gt;Body&lt;/p&gt;</description>
</item>
</channel></rss>"""

    def test_parse(self):
        items = parse_feed(self.XML)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "Programme Manager - Community Health")
        self.assertEqual(job_id_from_link(item["link"]), "4226938")
        self.assertEqual(item["author"], "Acme NGO")
        self.assertEqual(len(item["categories"]), 5)

    def test_malformed_xml(self):
        self.assertEqual(parse_feed("<rss><unclosed"), [])


class TestClassifierWiring(unittest.TestCase):
    """Only wiring — the engines have their own tests in _shared."""

    def test_in_scope_gets_right_labels(self):
        # real feed item (Health-theme facet): Programme Manager - Community
        # Health
        verdict = classify_feed_job(
            "Programme Manager - Community Health",
            career_categories=["Program/Project Management"],
            themes=["Health"])
        self.assertTrue(verdict["in_scope"])
        self.assertEqual(verdict["category"], "Public Health")
        self.assertEqual(verdict["sub_category"],
                         "Public Health Program Management")

    def test_out_of_scope_dropped(self):
        # real feed item: humanitarian HR role — never exported
        verdict = classify_feed_job(
            "People & Culture Partner",
            career_categories=["Human Resources"], themes=[])
        self.assertFalse(verdict["in_scope"])

    def test_theme_alone_cannot_admit(self):
        """A facet tag is recall, not a decision: a Health-themed job whose
        title/body show a different profession must not pass on the tag."""
        verdict = classify_feed_job(
            "Explosive Ordnance Threat Mitigation Training Officer",
            career_categories=[], themes=["Health"])
        self.assertFalse(verdict["in_scope"])


class TestCompanyType(unittest.TestCase):
    def test_default_hospital(self):
        self.assertEqual(classify_company_type("Danish Refugee Council"),
                         "hospital")

    def test_pharma_lookalike(self):
        self.assertEqual(classify_company_type("Acme Diagnostics Ltd"),
                         "pharma")


class TestComputeCutoff(unittest.TestCase):
    """A rolling window every run, NOT a stored-max watermark — a facet feed
    that was transiently empty reaches back weeks once warm, and a watermark
    would refuse everything it then serves."""

    def test_rolling_window(self):
        cutoff = compute_cutoff(today=date(2026, 8, 26))
        self.assertEqual(cutoff, (date(2026, 8, 26) -
                                  timedelta(days=WINDOW_DAYS)).isoformat())

    def test_since_overrides(self):
        self.assertEqual(compute_cutoff(since="2026-07-01"), "2026-07-01")


class TestClubRow(unittest.TestCase):
    RICH = {
        "country": "Kenya", "company": "Acme NGO", "company_type": "hospital",
        "title": "Epidemiologist", "description": "MPH required. Lead surveillance.",
        "category": "Public Health", "sub_category": "Epidemiology",
        "role_family": "Public Health",
        "job_url": "https://reliefweb.int/job/1/epidemiologist",
        "posted_date": "2026-08-26",
    }

    def test_mapping(self):
        club = rich_row_to_club_row(self.RICH)
        self.assertEqual(club["country_name"], "Kenya")
        self.assertEqual(club["country_code"], "KE")
        self.assertEqual(club["country_dial_code"], "+254")
        self.assertEqual(club["city_name"], "")          # never invented
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["category"], "Public Health")
        self.assertEqual(club["sub_category"], "Epidemiology")
        # feed has no salary data — columns stay empty, never invented
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")
        self.assertIn("MPH", club["qualification"])

    def test_unknown_country_exports_without_codes(self):
        rich = dict(self.RICH, country="World")
        club = rich_row_to_club_row(rich)
        self.assertEqual(club["country_name"], "World")
        self.assertEqual(club["country_code"], "")
        self.assertEqual(club["country_dial_code"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
