#!/usr/bin/env python3
"""Unit tests for the gsk scraper (careers.gsk.com, Phenom widgets).

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
        row = scraper.job_to_rich_row(card("Medical Science Liaison"))
        self.assertEqual(row["job_id"], "446196")
        self.assertEqual(row["company"], "GSK")
        self.assertEqual(row["site_category"], "Medical and Clinical")
        self.assertEqual(row["city"], "Pittsburgh")
        self.assertEqual(row["posted_date"], "2026-08-27")
        self.assertEqual(row["date_created"], "2026-08-24")
        self.assertEqual(row["job_url"],
                         "https://jobs.gsk.com/gb/en/job/446196")
        self.assertTrue(row["description"])          # teaser until detail merge

    def test_redated_card_keeps_honest_date_created(self):
        # Phenom re-dating trap: this Melbourne-area card says "posted
        # 2026-08-28" but was created 2026-05-12 — both dates exported.
        row = scraper.job_to_rich_row(card("Health Economist"))
        self.assertEqual(row["posted_date"], "2026-08-28")
        self.assertEqual(row["date_created"], "2026-05-12")

    def test_country_iso_mapping(self):
        row = scraper.job_to_rich_row(card("Medical Science Liaison"))
        self.assertEqual((row["country"], row["country_code"],
                          row["country_dial_code"]),
                         ("United States of America", "US", "+1"))
        row = scraper.job_to_rich_row(card("Respiratory Emerging Therapeutics"))
        self.assertEqual((row["country"], row["country_code"],
                          row["country_dial_code"]),
                         ("Belgium", "BE", "+32"))

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

    def test_remote_type_mapping_gsk(self):
        # GSK stamps remoteType on every card: exact "Remote" -> remote,
        # "Hybrid (Remote & On-site)" -> hybrid (keeps its city),
        # "Field worker" -> plain on-site (territory MSLs keep base city).
        remote = scraper.job_to_rich_row(
            card("Patient Centered Outcomes"))
        self.assertEqual(remote["remote"], True)
        hybrid = scraper.job_to_rich_row(card("ICSR Management Expert"))
        self.assertEqual((hybrid["remote"], hybrid["hybrid"]),
                         (False, True))
        field = scraper.job_to_rich_row(card("Medical Science Liaison"))
        self.assertEqual((field["remote"], field["hybrid"]),
                         (False, False))
        self.assertEqual(scraper.map_job_type("Full time", False, True),
                         "hybrid")


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
                         ["Medical and Clinical",
                          "Epidemiology and Health Outcomes",
                          "Regulatory"])

    def test_selected_fields_present_when_scoped(self):
        payload = scraper._search_payload(0, 50, scraper.CATEGORY_FACETS)
        self.assertEqual(payload["selected_fields"],
                         {"category": scraper.CATEGORY_FACETS})
        self.assertEqual(payload["lang"], "en_gb")
        self.assertEqual(payload["country"], "gb")

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
        row = scraper.job_to_rich_row(card("Medical Science Liaison"))
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "MSL")

    def test_regulatory_card_kept(self):
        row = scraper.job_to_rich_row(card("Global Regulatory Affairs Lead"))
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["sub_category"], "Regulatory Affairs")

    def test_heor_card_kept(self):
        row = scraper.job_to_rich_row(card("Lead / Senior Health Economist"))
        self.assertTrue(scraper.apply_classification(row))
        self.assertEqual(row["sub_category"], "HEOR")

    def test_bench_rd_card_dropped(self):
        # Synthetic bench card — GSK's slice omits bench roles, so none of
        # the captured fixtures can exercise the drop path.
        job = dict(card("Lead / Senior Health Economist"))
        job["title"] = "Senior Scientist, Protein Purification"
        job["ml_skills"] = ["chromatography", "protein purification"]
        job["descriptionTeaser"] = ("Run downstream purification unit "
                                    "operations for biologics drug substance.")
        row = scraper.job_to_rich_row(job)
        self.assertFalse(scraper.apply_classification(row))

    def test_bare_bd_tag_sanitized_but_kept_raw(self):
        # The bare ml_skills tag "business development" must not veto an
        # in-scope card — but the raw tag stays in the rich CSV column.
        # Measured 2026-08-28: 3 real GSK keeps were vetoed solely by this
        # tag (via "business development support" — hence substring match).
        job = dict(card("Respiratory Emerging Therapeutics"))
        job["ml_skills"] = list(job["ml_skills"]) + [
            "business development support"]
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
    def test_japanese_jd_flagged(self):
        jp = card("ICSR Management Expert")["descriptionTeaser"] * 4
        self.assertTrue(scraper.is_non_english(jp))

    def test_english_jd_not_flagged(self):
        en = ("We are looking for a Regulatory Affairs Manager to oversee "
              "regulatory strategy and submissions for companion "
              "diagnostics. You will work with the health authorities and "
              "internal teams to secure and maintain approvals.") * 2
        self.assertFalse(scraper.is_non_english(en))

    def test_short_text_never_flagged(self):
        self.assertFalse(scraper.is_non_english("짧은 텍스트"))

    def test_kept_japanese_card_lands_in_needs_review(self):
        # ICSR = Pharmacovigilance keep; Japanese JD -> flagged, never dropped.
        row = scraper.job_to_rich_row(card("ICSR Management Expert"))
        row["description"] = (row["description"] + " ") * 4   # full-JD length
        self.assertTrue(scraper.apply_classification(row))
        self.assertTrue(row["non_english"])
        self.assertTrue(row["needs_review"])


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
        row = scraper.job_to_rich_row(card("Global Congress Strategy Director"))
        ok = scraper.merge_detail(row, scraper.parse_job_detail(_DETAIL_HTML))
        self.assertTrue(ok)
        self.assertNotIn("<p>", row["description"])   # HTML stripped
        self.assertFalse(row["remote"])               # no remote field -> on-site
        self.assertEqual(row["experience_min_years"], "4")
        self.assertEqual(row["salary_min"], "95000")
        self.assertEqual(row["salary_currency"], "USD")

    def test_merge_detail_empty(self):
        row = scraper.job_to_rich_row(card("Medical Science Liaison"))
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

    def test_field_worker_row(self):
        club = scraper.rich_row_to_club_row(
            self._kept_row("Medical Science Liaison"))
        self.assertEqual(club["city_name"], "Pittsburgh")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["company_name"], "GSK")
        self.assertEqual(club["company_type"], "pharma")
        self.assertEqual(club["posted_at"], "2026-08-27")
        self.assertEqual(club["application_url"],
                         "https://jobs.gsk.com/gb/en/job/446196")

    def test_hybrid_row(self):
        club = scraper.rich_row_to_club_row(
            self._kept_row("Health Economist"))
        self.assertEqual(club["city_name"], "Abbotsford")
        self.assertEqual(club["job_type"], "hybrid")

    def test_japan_row(self):
        club = scraper.rich_row_to_club_row(
            self._kept_row("ICSR Management Expert"))
        self.assertEqual(club["country_name"], "Japan")
        self.assertEqual(club["country_code"], "JP")
        self.assertEqual(club["city_name"], "Akasaka")

    def test_exact_club_columns(self):
        club = scraper.rich_row_to_club_row(
            self._kept_row("Medical Science Liaison"))
        self.assertEqual(list(club.keys()), scraper.CLUB_COLUMNS)

    def test_salary_only_when_usd_or_inr(self):
        row = self._kept_row("Health Economist")
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
