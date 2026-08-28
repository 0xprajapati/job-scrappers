#!/usr/bin/env python3
"""Unit tests for the roche scraper (careers.roche.com, Phenom widgets).

Fixture: fixtures/widgets_sample.json — six real cards captured from the
live `POST /widgets` refineSearch response on 2026-08-28 (a Seoul field
medical partner with an all-Korean ml_skills list, a UK medical manager
carrying the misleading bare "business development" ml_skills tag, a US
regulatory affairs manager, an out-of-scope bioinformatics scientist from
the bench-heavy R&D facet, a "China's Mainland" medical manager, and a
Japanese-language Tokyo regulatory card with a re-dated postedDate).

The shared classification engine is covered by _shared/test_classification.py;
here only the WIRING is tested (an in-scope card keeps the right
category/sub_category, an out-of-scope card is dropped, the ml_skills
sanitization rescues the falsely-vetoed card, non-English JDs get flagged).

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
        row = scraper.job_to_rich_row(card("Field Medical Partner"))
        self.assertEqual(row["job_id"], "202608-121963")   # dash KEPT
        self.assertEqual(row["company"], "Roche")
        self.assertEqual(row["site_category"], "Medical Affairs")
        self.assertEqual(row["city"], "Seoul")
        self.assertEqual(row["posted_date"], "2026-08-28")
        self.assertEqual(row["date_created"], "2026-08-27")
        # detail URL keeps the jobId dash (the dashless URL is HTTP 410)
        self.assertEqual(row["job_url"],
                         "https://careers.roche.com/global/en/job/202608-121963")
        self.assertTrue(row["description"])          # teaser until detail merge

    def test_redated_card_keeps_honest_date_created(self):
        # Phenom re-dating trap: this Tokyo card says "posted 2026-07-23"
        # but was created 2026-04-14 — both dates are exported.
        row = scraper.job_to_rich_row(card("Regulatory Affairs Specialist"))
        self.assertEqual(row["posted_date"], "2026-07-23")
        self.assertEqual(row["date_created"], "2026-04-14")

    def test_country_iso_mapping(self):
        row = scraper.job_to_rich_row(card("Field Medical Partner"))
        self.assertEqual((row["country"], row["country_code"],
                          row["country_dial_code"]),
                         ("Korea, Republic of", "KR", "+82"))
        # Roche's spelling for China — 45 of the 162 slice cards.
        row = scraper.job_to_rich_row(card("Country Medical Manager"))
        self.assertEqual((row["country"], row["country_code"],
                          row["country_dial_code"]),
                         ("China's Mainland", "CN", "+86"))

    def test_extended_country_map(self):
        self.assertEqual(scraper.country_meta("Türkiye"),
                         ("Türkiye", "TR", "+90"))
        self.assertEqual(scraper.country_meta("Saudi Arabia"),
                         ("Saudi Arabia", "SA", "+966"))
        self.assertEqual(scraper.country_meta("Bosnia and Herzegovina"),
                         ("Bosnia and Herzegovina", "BA", "+387"))

    def test_unknown_country_never_guessed(self):
        self.assertEqual(scraper.country_meta("Atlantis"),
                         ("Atlantis", "", ""))

    def test_no_remote_marker_on_this_board(self):
        # Roche cards publish no remoteType and no "Remote ..." locations
        # (checked across the whole slice 2026-08-28) — every fixture card
        # is on-site; the generic signals stay wired for the future.
        for job in fixture_jobs():
            self.assertFalse(scraper.job_to_rich_row(job)["remote"])
        self.assertTrue(scraper.is_remote_card(
            {"remoteType": "Remote", "location": "Basel"}))
        self.assertTrue(scraper.is_remote_card(
            {"location": "Remote Switzerland"}))


# ----------------------------------------------------------------------------
# Salary — grounded extraction from description text
# ----------------------------------------------------------------------------

class TestSalary(unittest.TestCase):
    def test_expected_salary_range_phrasing(self):
        parsed = scraper.parse_salary_from_description(
            "The expected salary range for this position based on the "
            "primary location of Indiana is $95,000 to $130,000 annually.")
        self.assertEqual(parsed["salary_min"], "95000")
        self.assertEqual(parsed["salary_max"], "130000")
        self.assertEqual(parsed["salary_period"], "per_annum")
        self.assertEqual(parsed["salary_currency"], "USD")

    def test_pay_range_colon_style(self):
        parsed = scraper.parse_salary_from_description(
            "Pay Range: $90,000-$120,000 USD Benefits: ...")
        self.assertEqual((parsed["salary_min"], parsed["salary_max"]),
                         ("90000", "120000"))

    def test_hourly_rate_stays_raw_only(self):
        parsed = scraper.parse_salary_from_description(
            "Pay Range: $25.00 - $30.00 per hour depending on experience.")
        self.assertIn("salary_raw", parsed)
        self.assertNotIn("salary_min", parsed)

    def test_no_statement_returns_empty(self):
        self.assertEqual(scraper.parse_salary_from_description(
            "Manage budgets of $2,000,000 across programs."), {})
        self.assertEqual(scraper.parse_salary_from_description(""), {})


# ----------------------------------------------------------------------------
# Dates / incremental window
# ----------------------------------------------------------------------------

class TestDatesAndCutoff(unittest.TestCase):
    def test_parse_iso_date(self):
        self.assertEqual(scraper.parse_iso_date("2026-08-28T00:00:00.000+0000"),
                         "2026-08-28")
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
# Facet-scoped payload
# ----------------------------------------------------------------------------

class TestFacetPayload(unittest.TestCase):
    def test_facet_strings_are_the_probe_verified_ones(self):
        self.assertEqual(scraper.CATEGORY_FACETS,
                         ["Medical Affairs", "Regulatory Affairs",
                          "Research & Development"])

    def test_selected_fields_present_when_scoped(self):
        payload = scraper._search_payload(0, 50, scraper.CATEGORY_FACETS)
        self.assertEqual(payload["selected_fields"],
                         {"category": scraper.CATEGORY_FACETS})
        self.assertEqual(payload["lang"], "en_global")
        self.assertEqual(payload["country"], "global")

    def test_selected_fields_empty_on_unfiltered_fallback(self):
        payload = scraper._search_payload(100, 50, None)
        self.assertEqual(payload["selected_fields"], {})
        self.assertEqual(payload["from"], 100)


# ----------------------------------------------------------------------------
# Classification wiring (the engine itself is tested in _shared)
# ----------------------------------------------------------------------------

class TestClassificationWiring(unittest.TestCase):
    def test_korean_field_medical_card_kept(self):
        # Kept despite the all-Korean ml_skills list — the title carries it.
        row = scraper.job_to_rich_row(card("Field Medical Partner"))
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "MSL")

    def test_regulatory_card_kept(self):
        row = scraper.job_to_rich_row(card("Companion Diagnostics"))
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["sub_category"], "Regulatory Affairs")

    def test_bench_rd_card_dropped(self):
        # The R&D facet is bench/IT-heavy — the classifier drops most of it.
        row = scraper.job_to_rich_row(card("Bioinformatics Scientist"))
        self.assertFalse(scraper.apply_classification(row))

    def test_bare_bd_tag_sanitized_but_kept_raw(self):
        # The bare ml_skills tag "business development" must not veto an
        # in-scope medical manager — but the raw tag stays in the rich CSV
        # column.  Measured 2026-08-28: 4 real keeps in the 162-card slice
        # were vetoed solely by this tag, this card among them.
        job = card("Medical Manager Cardiac")
        self.assertIn("business development",
                      [s.lower() for s in job["ml_skills"]])
        row = scraper.job_to_rich_row(job)
        self.assertIn("business development", row["ml_skills"].lower())
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")

    def test_skills_for_classifier_strips_substring_matches(self):
        joined = scraper.skills_for_classifier(
            ["medical communication", "Business Development",
             "business development support", "meddra coding"])
        self.assertNotIn("business development", joined.lower())
        self.assertIn("medical communication", joined)
        self.assertIn("meddra coding", joined)


# ----------------------------------------------------------------------------
# Non-English flagging (global board: kept + flagged)
# ----------------------------------------------------------------------------

class TestNonEnglish(unittest.TestCase):
    def test_korean_jd_flagged(self):
        ko = card("Field Medical Partner")["descriptionTeaser"] * 4
        self.assertTrue(scraper.is_non_english(ko))

    def test_japanese_jd_flagged(self):
        jp = card("Regulatory Affairs Specialist")["descriptionTeaser"] * 4
        self.assertTrue(scraper.is_non_english(jp))

    def test_english_jd_not_flagged(self):
        en = ("We are looking for a Regulatory Affairs Manager to oversee "
              "regulatory strategy and submissions for companion "
              "diagnostics. You will work with the health authorities and "
              "internal teams to secure and maintain approvals.") * 2
        self.assertFalse(scraper.is_non_english(en))

    def test_short_text_never_flagged(self):
        self.assertFalse(scraper.is_non_english("짧은 텍스트"))

    def test_kept_korean_card_lands_in_needs_review(self):
        row = scraper.job_to_rich_row(card("Field Medical Partner"))
        row["description"] = (row["description"] + " ") * 4   # full-JD length
        self.assertTrue(scraper.apply_classification(row))    # kept (MSL)
        self.assertTrue(row["non_english"])
        self.assertTrue(row["needs_review"])                  # ...and flagged


# ----------------------------------------------------------------------------
# Detail page parsing / merge
# ----------------------------------------------------------------------------

_DETAIL_HTML = """<html><script>
phApp.ddo = {"jobDetail": {"data": {"job": {
  "description": "<p><b>The Position</b></p><p>Oversee regulatory submissions with a minimum of 4 years of experience. PharmD preferred.</p><h2>The expected salary range for this position is $95,000 to $130,000.</h2>"}}}};
</script></html>"""


class TestDetail(unittest.TestCase):
    def test_parse_job_detail(self):
        detail = scraper.parse_job_detail(_DETAIL_HTML)
        self.assertIn("Oversee regulatory submissions", detail["description"])
        self.assertEqual(scraper.parse_job_detail("<html>no ddo</html>"), {})

    def test_merge_detail(self):
        row = scraper.job_to_rich_row(card("Companion Diagnostics"))
        ok = scraper.merge_detail(row, scraper.parse_job_detail(_DETAIL_HTML))
        self.assertTrue(ok)
        self.assertNotIn("<p>", row["description"])   # HTML stripped
        self.assertFalse(row["remote"])               # no remote field -> on-site
        self.assertEqual(row["experience_min_years"], "4")
        self.assertEqual(row["salary_min"], "95000")
        self.assertEqual(row["salary_currency"], "USD")

    def test_merge_detail_empty(self):
        row = scraper.job_to_rich_row(card("Field Medical Partner"))
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

    def test_onsite_row(self):
        club = scraper.rich_row_to_club_row(
            self._kept_row("Companion Diagnostics"))
        self.assertEqual(club["city_name"], "Indianapolis")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["company_name"], "Roche")
        self.assertEqual(club["company_type"], "pharma")
        self.assertEqual(club["posted_at"], "2026-08-27")
        self.assertEqual(
            club["application_url"],
            "https://careers.roche.com/global/en/job/202607-119375")

    def test_china_mainland_row(self):
        club = scraper.rich_row_to_club_row(
            self._kept_row("Country Medical Manager"))
        self.assertEqual(club["country_name"], "China's Mainland")
        self.assertEqual(club["country_code"], "CN")
        self.assertEqual(club["city_name"], "Shanghai")

    def test_exact_club_columns(self):
        club = scraper.rich_row_to_club_row(
            self._kept_row("Field Medical Partner"))
        self.assertEqual(list(club.keys()), scraper.CLUB_COLUMNS)

    def test_salary_only_when_usd_or_inr(self):
        row = self._kept_row("Companion Diagnostics")
        row.update({"salary_min": "100000", "salary_max": "120000",
                    "salary_period": "per_annum", "salary_currency": "CHF"})
        club = scraper.rich_row_to_club_row(row)
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")

    def test_job_type_mapping(self):
        self.assertEqual(scraper.map_job_type("Full time", False), "full_time")
        self.assertEqual(scraper.map_job_type("Part time", False), "part_time")
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
