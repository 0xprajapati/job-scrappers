#!/usr/bin/env python3
"""Unit tests for the fortrea scraper (careers.fortrea.com, Phenom widgets).

Fixture: fixtures/widgets_sample.json — six real cards captured from the
live `POST /widgets` refineSearch response on 2026-08-27 (a remote-US CRA
posting, a Korea PV associate, a China TMF reviewer with no city, a clinical
project manager carrying the misleading bare "business development"
ml_skills tag, an out-of-scope HR director, and a bedside lab technician).

The shared classification engine is covered by _shared/test_classification.py;
here only the WIRING is tested (an in-scope card keeps the right
category/sub_category, an out-of-scope card is dropped, the ml_skills
sanitization rescues the falsely-vetoed card).

Run:  ../../.venv/bin/python -m pytest test_filters.py -q
  or: ../../.venv/bin/python test_filters.py
"""

import json
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import scraper

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "widgets_sample.json"


def fixture_jobs():
    with open(FIXTURE) as fh:
        return json.load(fh)["refineSearch"]["data"]["jobs"]


def card(title_fragment):
    for job in fixture_jobs():
        if title_fragment in job["title"]:
            return job
    raise KeyError(title_fragment)


# ----------------------------------------------------------------------------
# Card parsing (real captured cards)
# ----------------------------------------------------------------------------

class TestCardParsing(unittest.TestCase):
    def test_basic_fields_from_real_card(self):
        row = scraper.job_to_rich_row(card("PSS Associate I"))
        self.assertEqual(row["job_id"], "264649")
        self.assertEqual(row["job_seq_no"], "FOMFORUS264649EXTERNALENUS")
        self.assertEqual(row["title"], "PSS Associate I (1 year contract)")
        self.assertEqual(row["company"], "Fortrea")
        self.assertEqual(row["site_category"], "Clinical")
        self.assertEqual(row["posted_date"], "2026-08-27")
        self.assertEqual(row["date_created"], "2026-08-11")
        self.assertEqual(row["job_url"],
                         "https://careers.fortrea.com/us/en/job/264649")
        self.assertTrue(row["description"])          # teaser until detail merge

    def test_country_iso_mapping(self):
        row = scraper.job_to_rich_row(card("PSS Associate I"))
        self.assertEqual((row["country"], row["country_code"],
                          row["country_dial_code"]),
                         ("Korea, Republic of", "KR", "+82"))
        row = scraper.job_to_rich_row(card("TMF Reviewer III"))
        self.assertEqual((row["country_code"], row["country_dial_code"]),
                         ("CN", "+86"))
        row = scraper.job_to_rich_row(card("Global Human Resources"))
        self.assertEqual((row["country_code"], row["country_dial_code"]),
                         ("US", "+1"))

    def test_unknown_country_never_guessed(self):
        name, code, dial = scraper.country_meta("Atlantis")
        self.assertEqual((name, code, dial), ("Atlantis", "", ""))

    def test_remote_detected_from_location_not_city(self):
        # Remote US postings keep the Durham HQ placeholder city; the card
        # signal is location == "Remote United States".
        row = scraper.job_to_rich_row(card("CRA II & Sr. CRA"))
        self.assertTrue(row["remote"])
        self.assertEqual(row["city"], "Durham")      # raw source value kept
        onsite = scraper.job_to_rich_row(card("PRN Lab Technician"))
        self.assertFalse(onsite["remote"])

    def test_no_city_card_keeps_state(self):
        row = scraper.job_to_rich_row(card("TMF Reviewer III"))
        self.assertEqual(row["city"], "")
        self.assertEqual(row["state"], "Shanghai")


# ----------------------------------------------------------------------------
# Salary — grounded extraction from description text (worked live examples)
# ----------------------------------------------------------------------------

class TestSalary(unittest.TestCase):
    def test_annual_usd_range(self):
        # Live example from job 264553 (Senior Programmer Analyst, US remote)
        parsed = scraper.parse_salary_from_description(
            "Office work environment. Pay Range: $90,000-$120,000 USD "
            "Benefits: All job offers will be based on...")
        self.assertEqual(parsed["salary_min"], "90000")
        self.assertEqual(parsed["salary_max"], "120000")
        self.assertEqual(parsed["salary_period"], "per_annum")
        self.assertEqual(parsed["salary_currency"], "USD")
        self.assertIn("Pay Range", parsed["salary_raw"])

    def test_range_with_to_and_spaces(self):
        parsed = scraper.parse_salary_from_description(
            "The pay range for this role is $80,000 to $95,000 annually.")
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         ("80000", "95000"))

    def test_hourly_rate_stays_raw_only(self):
        parsed = scraper.parse_salary_from_description(
            "Pay Range: $25.00 - $30.00 per hour depending on experience.")
        self.assertIn("salary_raw", parsed)
        self.assertNotIn("salary_min", parsed)

    def test_no_statement_returns_empty(self):
        self.assertEqual(scraper.parse_salary_from_description(
            "Manage budgets of $2,000,000 across programs."), {})
        self.assertEqual(scraper.parse_salary_from_description(""), {})

    def test_reversed_range_normalized(self):
        parsed = scraper.parse_salary_from_description(
            "Salary range $120,000 - $90,000")
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         ("90000", "120000"))


# ----------------------------------------------------------------------------
# Dates / incremental window
# ----------------------------------------------------------------------------

class TestDatesAndCutoff(unittest.TestCase):
    def test_parse_iso_date(self):
        self.assertEqual(scraper.parse_iso_date("2026-08-27T00:00:00.000+0000"),
                         "2026-08-27")
        self.assertEqual(scraper.parse_iso_date(""), "")
        self.assertEqual(scraper.parse_iso_date(None), "")

    def test_first_run_window(self):
        expected = (date.today()
                    - timedelta(days=scraper.INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(scraper.compute_cutoff(None), expected)

    def test_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-20", "2026-08-25", ""]})
        self.assertEqual(scraper.compute_cutoff(df), "2026-08-23")

    def test_since_override(self):
        self.assertEqual(scraper.compute_cutoff(None, since="2026-01-01"),
                         "2026-01-01")


# ----------------------------------------------------------------------------
# Classification wiring (the engine itself is tested in _shared)
# ----------------------------------------------------------------------------

class TestClassificationWiring(unittest.TestCase):
    def test_in_scope_cra_card_kept_with_right_labels(self):
        row = scraper.job_to_rich_row(card("CRA II & Sr. CRA"))
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")
        self.assertTrue(row["role_family"])

    def test_out_of_scope_hr_card_dropped(self):
        row = scraper.job_to_rich_row(card("Global Human Resources"))
        self.assertFalse(scraper.apply_classification(row))

    def test_bedside_lab_technician_dropped(self):
        row = scraper.job_to_rich_row(card("PRN Lab Technician"))
        self.assertFalse(scraper.apply_classification(row))

    def test_bd_tag_sanitized_but_kept_raw(self):
        # The bare ml_skills tag "business development" (Phenom auto-tag for
        # JDs that merely liaise with BD) must not veto an in-scope clinical
        # PM role — but the raw tag stays in the rich CSV column.
        job = card("Senior Clinical Project Manager")
        self.assertIn("business development",
                      [s.lower() for s in job["ml_skills"]])
        row = scraper.job_to_rich_row(job)
        self.assertIn("business development", row["ml_skills"])
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")

    def test_skills_for_classifier_strips_only_blocklisted(self):
        joined = scraper.skills_for_classifier(
            ["adverse event reporting", "Business Development", "meddra coding"])
        self.assertNotIn("Business Development", joined)
        self.assertIn("meddra coding", joined)


# ----------------------------------------------------------------------------
# Detail page parsing / merge
# ----------------------------------------------------------------------------

_DETAIL_HTML = """<html><script>
phApp.ddo = {"jobDetail": {"data": {"job": {
  "description": "<p><b>Job Overview:</b></p><p>Monitor clinical trials with a minimum of 3 years of experience. MBBS preferred.</p><h2>Pay Range: $95,000-$110,000 USD</h2>",
  "remote": "Remote"}}}};
</script></html>"""


class TestDetail(unittest.TestCase):
    def test_parse_job_detail(self):
        detail = scraper.parse_job_detail(_DETAIL_HTML)
        self.assertEqual(detail["remote"], "Remote")
        self.assertIn("Monitor clinical trials", detail["description"])
        self.assertEqual(scraper.parse_job_detail("<html>no ddo</html>"), {})

    def test_merge_detail(self):
        row = scraper.job_to_rich_row(card("PRN Lab Technician"))
        self.assertFalse(row["remote"])
        ok = scraper.merge_detail(row, scraper.parse_job_detail(_DETAIL_HTML))
        self.assertTrue(ok)
        self.assertNotIn("<p>", row["description"])   # HTML stripped
        self.assertTrue(row["remote"])                # detail remote flag wins
        self.assertEqual(row["experience_min_years"], "3")
        self.assertEqual(row["salary_min"], "95000")
        self.assertEqual(row["salary_currency"], "USD")

    def test_merge_detail_empty(self):
        row = scraper.job_to_rich_row(card("PSS Associate I"))
        before = row["description"]
        self.assertFalse(scraper.merge_detail(row, {}))
        self.assertEqual(row["description"], before)


# ----------------------------------------------------------------------------
# Club-schema mapping
# ----------------------------------------------------------------------------

class TestClubRow(unittest.TestCase):
    def _kept_row(self, fragment):
        row = scraper.job_to_rich_row(card(fragment))
        scraper.apply_classification(row)
        return row

    def test_remote_row_city_and_job_type(self):
        club = scraper.rich_row_to_club_row(self._kept_row("CRA II & Sr. CRA"))
        self.assertEqual(club["city_name"], "Remote")
        self.assertEqual(club["job_type"], "remote")
        self.assertEqual(club["country_code"], "US")

    def test_onsite_row(self):
        club = scraper.rich_row_to_club_row(self._kept_row("PSS Associate I"))
        self.assertEqual(club["city_name"], "Seoul")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["company_name"], "Fortrea")
        self.assertEqual(club["company_type"], "pharma")
        self.assertEqual(club["posted_at"], "2026-08-27")
        self.assertEqual(club["application_url"],
                         "https://careers.fortrea.com/us/en/job/264649")

    def test_no_city_falls_back_to_state(self):
        club = scraper.rich_row_to_club_row(self._kept_row("TMF Reviewer III"))
        self.assertEqual(club["city_name"], "Shanghai")

    def test_exact_club_columns(self):
        club = scraper.rich_row_to_club_row(self._kept_row("PSS Associate I"))
        self.assertEqual(list(club.keys()), scraper.CLUB_COLUMNS)

    def test_salary_only_when_usd_or_inr(self):
        row = self._kept_row("PSS Associate I")
        row.update({"salary_min": "100000", "salary_max": "120000",
                    "salary_period": "per_annum", "salary_currency": "EUR"})
        club = scraper.rich_row_to_club_row(row)
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")

    def test_job_type_mapping(self):
        self.assertEqual(scraper.map_job_type("Full time", False), "full_time")
        self.assertEqual(scraper.map_job_type("Part time", False), "part_time")
        self.assertEqual(scraper.map_job_type("Casual", False), "part_time")
        self.assertEqual(scraper.map_job_type("Full time", True), "remote")
        self.assertEqual(scraper.map_job_type("", False), "full_time")


# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

class TestTextHelpers(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(
            scraper.strip_html("<p>Hello <b>world</b></p><ul><li>x</li></ul>"),
            "Hello world x")

    def test_clean_text(self):
        self.assertEqual(scraper.clean_text("  a\n\tb &amp; c  "), "a b & c")
        self.assertEqual(scraper.clean_text(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
