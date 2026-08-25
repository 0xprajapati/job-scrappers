#!/usr/bin/env python3
"""Unit tests for the hziegler.com scraper's parsers, classifier and cutoff.

All fixtures are verbatim fragments from real hziegler.com pages (captured
2026-07-27). Run with plain `python test_filters.py`.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

import scraper


# ---------------------------------------------------------------------------
# Fixtures (trimmed but structurally identical to the live pages)
# ---------------------------------------------------------------------------

INDEX_HTML = """
<div class="jobDir">
  <h2 id="jobs_nursing">NURSING</h2>
  <h3>Nurse Manager/Head Nurse</h3>
  <ul class='jobList'>
    <li><a href="/jobs/nurse-manager-oncology--king-faisal-riyadh.html">Nurse Manager - Oncology - Riyadh, Saudi Arabia</a></li>
  </ul>
  <h3>RN - Psychiatry Nurse</h3>
  <ul class='jobList'>
    <li><a href="/jobs/rn-addictions-detox--abu-dhabi.html">RN - Addictions/Detox - Abu Dhabi, UAE</a></li>
  </ul>
  <h2 id="jobs_allied-health-clinical-services">ALLIED HEALTH &amp; CLINICAL SERVICES</h2>
  <ul class='jobList'>
    <li><a href="/jobs/physical-therapist--riyadh.html">Physical Therapist - Riyadh, Saudi Arabia</a></li>
  </ul>
  <h2 id="jobs_physicians">PHYSICIANS</h2>
  <ul class='jobList'>
    <li><a href="/jobs/psychologist--abu-dhabi.html">Psychologist - Abu Dhabi, UAE</a></li>
    <li><a href="/jobs/family-physician--canada.html">Family Physician - Ontario, Canada</a></li>
  </ul>
</div>
"""

DETAIL_HTML = """
<script type="application/ld+json">
{
  "@context": "http://schema.org/",
  "@type": "JobPosting",
  "datePosted": "2022-07-12",
  "description": "<h1>Requirements:</h1>\\n<ul>\\n<li>Current nursing (RN) license</li>\\n<li>A minimum of <strong>six years</strong> recent experience</li></ul>",
  "employmentType": "FULL_TIME",
  "hiringOrganization": {"@type": "Organization", "name": "Helen Ziegler and Associates"},
  "identifier": {"@type": "PropertyValue", "value": "/jobs/nurse-manager-oncology--king-faisal-riyadh"},
  "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
     "addressCountry": "Saudi Arabia", "addressLocality": "Riyadh, Saudi Arabia",
     "addressRegion": "Riyadh, Saudi Arabia"}},
  "title": "Nurse Manager - Oncology"
}
</script>
<td class='column left-column'>
  <div><div class="markdown markdown-body"><p>The <a href="/employers/king-faisal-riyadh.html">KFSH&amp;RC</a> is a modern, 1,300+ bed JCI-accredited academic medical facility.</p>
  <!--This modern 1,000+ bed academic centre holds both JCI and Magnet accreditations.--></div></div>
  <div class="embedContainer"><div class="markdown markdown-body"><h1>Requirements:</h1>
  <ul><li>Current nursing (RN) license</li>
  <li>A minimum of <strong>six years</strong> recent experience as a Registered Nurse</li></ul>
  <h1>Benefits:</h1><ul><li>Tax-free income</li></ul></div></div>
  <h3 id="applySection">Apply to this Job</h3>
</td>
<td class='column right-column'>
  <div class="featureBox briefBox" ><h1><a href="/employers/king-faisal-riyadh.html">King Faisal Specialist Hospital, Riyadh</a></h1><p>The King Faisal Specialist Hospital &amp; Research Centre, Riyadh, with 1,300+ beds, is the most high acuity tertiary-care centre in the entire Arabian Peninsula.</p><a href="/employers/king-faisal-riyadh.html"> more</a></div>
  <div class="featureBox briefBox" ><h1><a href="/locations/riyadh.html">Riyadh, Saudi Arabia</a></h1><p>The capital city.</p></div>
</td>
"""

CONFIDENTIAL_RIGHT_COL = """
<td class='column right-column'>
  <div class="featureBox briefBox" ><h1><a href="/employers/abu-dhabi.html">Abu Dhabi</a></h1>
  <a href="/employers/abu-dhabi.html"> more</a></div>
</td>
"""


class TestIndexParsing(unittest.TestCase):
    def setUp(self):
        self.cards = scraper.parse_index(INDEX_HTML)
        self.by_id = {c["job_id"]: c for c in self.cards}

    def test_finds_every_card_in_every_section(self):
        self.assertEqual(len(self.cards), 5)
        self.assertEqual(
            sorted({c["site_section"] for c in self.cards}),
            ["allied-health-clinical-services", "nursing", "physicians"])

    def test_job_id_is_the_url_slug(self):
        card = self.by_id["rn-addictions-detox--abu-dhabi"]
        self.assertEqual(card["job_url"],
                         "https://www.hziegler.com/jobs/rn-addictions-detox--abu-dhabi.html")

    def test_role_group_tracks_the_preceding_h3(self):
        self.assertEqual(self.by_id["nurse-manager-oncology--king-faisal-riyadh"]
                         ["role_group"], "Nurse Manager/Head Nurse")
        self.assertEqual(self.by_id["rn-addictions-detox--abu-dhabi"]
                         ["role_group"], "RN - Psychiatry Nurse")
        # Sections without <h3> groups leave role_group empty, not stale.
        self.assertEqual(self.by_id["physical-therapist--riyadh"]["role_group"], "")

    def test_label_location_suffix(self):
        self.assertEqual(self.by_id["family-physician--canada"]["label_location"],
                         "Ontario, Canada")

    def test_no_cards_on_a_page_without_the_directory(self):
        self.assertEqual(scraper.parse_index("<html><body>no jobs</body></html>"), [])


class TestDetailParsing(unittest.TestCase):
    def setUp(self):
        self.posting = scraper.parse_job_posting(DETAIL_HTML)

    def test_json_ld_fields(self):
        self.assertEqual(self.posting["title"], "Nurse Manager - Oncology")
        self.assertEqual(self.posting["datePosted"], "2022-07-12")
        self.assertEqual(self.posting["employmentType"], "FULL_TIME")

    def test_missing_json_ld_is_empty_not_an_error(self):
        self.assertEqual(scraper.parse_job_posting("<html></html>"), {})
        self.assertEqual(scraper.parse_job_posting(
            '<script type="application/ld+json">{not json</script>'), {})

    def test_description_includes_the_intro_the_json_ld_omits(self):
        text = scraper.parse_page_description(DETAIL_HTML)
        self.assertIn("JCI-accredited academic medical facility", text)
        self.assertIn("Requirements:", text)
        self.assertIn("Tax-free income", text)
        self.assertNotIn("<li>", text)
        self.assertNotIn("Magnet", text)          # HTML comment dropped
        self.assertNotIn("Apply to this Job", text)

    def test_employer_box(self):
        name, about, url = scraper.parse_employer(DETAIL_HTML)
        self.assertEqual(name, "King Faisal Specialist Hospital, Riyadh")
        self.assertIn("tertiary-care centre", about)
        self.assertFalse(about.endswith("more"))   # trailing link text dropped
        self.assertEqual(url, "https://www.hziegler.com/employers/king-faisal-riyadh.html")

    def test_employer_blurb_keeps_the_word_more_inside_a_sentence(self):
        html = ("<td class='column right-column'><div class=\"featureBox briefBox\" >"
                "<h1><a href=\"/employers/x.html\">X Hospital</a></h1>"
                "<p>With more than 1,000 beds.</p><a href=\"/employers/x.html\">"
                " more</a></div>")
        self.assertEqual(scraper.parse_employer(html)[1],
                         "With more than 1,000 beds.")


class TestCompanyResolution(unittest.TestCase):
    def test_named_hospital_kept(self):
        self.assertEqual(
            scraper.resolve_company("King Faisal Specialist Hospital, Riyadh",
                                    "Riyadh", "Saudi Arabia"),
            ("King Faisal Specialist Hospital, Riyadh", False))

    def test_location_named_employer_is_confidential(self):
        name, confidential = scraper.parse_employer(CONFIDENTIAL_RIGHT_COL)[0], None
        company, confidential = scraper.resolve_company(
            name, "Abu Dhabi", "United Arab Emirates")
        self.assertEqual(company, scraper.CONFIDENTIAL_COMPANY)
        self.assertTrue(confidential)

    def test_explicit_confidential_label(self):
        self.assertEqual(
            scraper.resolve_company("Canada (Confidential)", "", "Canada"),
            (scraper.CONFIDENTIAL_COMPANY, True))

    def test_missing_employer_box(self):
        self.assertEqual(scraper.resolve_company("", "Riyadh", "Saudi Arabia"),
                         (scraper.CONFIDENTIAL_COMPANY, True))


class TestLocation(unittest.TestCase):
    def test_city_country(self):
        self.assertEqual(scraper.location_meta("Riyadh, Saudi Arabia", "Saudi Arabia"),
                         ("Riyadh", "", "Saudi Arabia", "SA", "+966"))

    def test_city_state_country(self):
        self.assertEqual(scraper.location_meta("Barrie, Ontario, Canada", "Canada"),
                         ("Barrie", "Ontario", "Canada", "CA", "+1"))

    def test_country_abbreviation_in_locality(self):
        self.assertEqual(scraper.location_meta("Abu Dhabi, UAE", "United Arab Emirates"),
                         ("Abu Dhabi", "", "United Arab Emirates", "AE", "+971"))

    def test_empty_locality_falls_back_to_the_index_label(self):
        self.assertEqual(
            scraper.location_meta("", "Canada", label_location="Ontario, Canada"),
            ("Ontario", "", "Canada", "CA", "+1"))

    def test_country_only_leaves_city_empty(self):
        self.assertEqual(scraper.location_meta("Canada", "Canada"),
                         ("", "", "Canada", "CA", "+1"))


class TestClassifier(unittest.TestCase):
    def test_nursing_titles(self):
        for title in ("RN - Adult Cardiac Surgical ICU (CSICU)",
                      "Nurse Manager - Oncology",
                      "Nurse Practitioner - Genomics/Precision Medicine",
                      "Nursing Education Coordinator - Oncology"):
            self.assertEqual(scraper.classify_category(title, "nursing")[0],
                             "nurses", title)

    def test_physician_titles(self):
        for title in ("Chairman Pediatrics", "Consultant Anesthesiologist",
                      "Family Medicine Physicians", "Family Physician"):
            self.assertEqual(scraper.classify_category(title, "physicians")[0],
                             "doctors", title)

    def test_allied_titles_are_non_clinical_even_inside_the_physicians_section(self):
        # HZA files "Psychologist" under PHYSICIANS; the title must win, or the
        # broad "-ologist" doctor rule would mislabel it.
        self.assertEqual(scraper.classify_category("Psychologist", "physicians")[0],
                         "non_clinical")
        self.assertEqual(
            scraper.classify_category("Physical Therapist",
                                      "allied-health-clinical-services")[0],
            "non_clinical")

    def test_pharmacist(self):
        self.assertEqual(
            scraper.classify_category("Clinical Pharmacist",
                                      "allied-health-clinical-services")[0],
            "pharmacists")

    def test_section_is_the_fallback_for_unplaceable_titles(self):
        self.assertEqual(scraper.classify_category("Unit Coordinator", "nursing"),
                         ("nurses", False))

    def test_titles_with_no_healthcare_signal_are_kept_and_flagged(self):
        category, needs_review = scraper.classify_category("Account Executive", "")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(needs_review)

    def test_healthcare_titles_are_not_flagged(self):
        self.assertFalse(scraper.classify_category("RN - NICU", "nursing")[1])

    def test_company_type(self):
        self.assertEqual(
            scraper.classify_company_type("King Faisal Specialist Hospital, Riyadh"),
            "hospital")
        self.assertEqual(scraper.classify_company_type("Confidential Pharma Client"),
                         "pharma")


class TestExperienceParser(unittest.TestCase):
    def test_word_numbers(self):
        self.assertEqual(scraper.parse_min_experience(
            "Must have a minimum of two years current experience as an RN"), "2")
        self.assertEqual(scraper.parse_min_experience(
            "Minimum three years experience as a registered, licensed PT"), "3")

    def test_digits_and_first_match_wins(self):
        text = ("A minimum of 10 years of experience as a Consultant after board "
                "certification, which must include a minimum of four years of "
                "current administrative experience")
        self.assertEqual(scraper.parse_min_experience(text), "10")

    def test_absent_requirement_stays_empty(self):
        self.assertEqual(scraper.parse_min_experience(
            "If Canadian: CCFP or eligible. If from the USA: license and ABFM"), "")
        self.assertEqual(scraper.parse_min_experience(""), "")

    def test_implausible_values_rejected(self):
        self.assertEqual(scraper.parse_min_experience("minimum of 99 years"), "")


class TestJobType(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(scraper.job_type_from("FULL_TIME"), "full_time")
        self.assertEqual(scraper.job_type_from("PART_TIME"), "part_time")
        self.assertEqual(scraper.job_type_from("CONTRACTOR"), "contract")
        self.assertEqual(scraper.job_type_from(""), "full_time")


class TestCutoff(unittest.TestCase):
    def test_first_run_keeps_every_open_job(self):
        self.assertEqual(scraper.compute_cutoff(None), "")

    def test_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-06-29", "2022-07-12", ""]})
        expected = (date(2026, 6, 29)
                    - timedelta(days=scraper.WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(scraper.compute_cutoff(df), expected)

    def test_unparseable_dates_fall_back_to_the_first_run_rule(self):
        self.assertEqual(
            scraper.compute_cutoff(pd.DataFrame({"posted_date": ["", "n/a"]})), "")


class TestRowBuilding(unittest.TestCase):
    def setUp(self):
        card = {c["job_id"]: c for c in scraper.parse_index(INDEX_HTML)}[
            "nurse-manager-oncology--king-faisal-riyadh"]
        self.row = scraper.build_row(card, scraper.parse_job_posting(DETAIL_HTML),
                                     DETAIL_HTML)

    def test_core_fields(self):
        self.assertEqual(self.row["job_id"],
                         "nurse-manager-oncology--king-faisal-riyadh")
        self.assertEqual(self.row["title"], "Nurse Manager - Oncology")
        self.assertEqual(self.row["company"],
                         "King Faisal Specialist Hospital, Riyadh")
        self.assertEqual(self.row["posted_date"], "2022-07-12")
        self.assertEqual(self.row["category"], "nurses")
        self.assertEqual(self.row["city"], "Riyadh")
        self.assertEqual(self.row["country_code"], "SA")
        self.assertEqual(self.row["min_experience"], "6")

    def test_salary_is_recorded_as_undisclosed_never_invented(self):
        self.assertEqual(self.row["salary_raw"], "Not Disclosed")
        for field in ("salary_min", "salary_max", "salary_period",
                      "salary_currency"):
            self.assertEqual(self.row[field], "")

    def test_club_row_shape(self):
        club = scraper.rich_row_to_club_row(self.row)
        self.assertEqual(list(club), scraper.CLUB_COLUMNS)
        self.assertEqual(club["country_name"], "Saudi Arabia")
        self.assertEqual(club["category"], "nurses")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["is_active"], "true")
        self.assertEqual(club["min_salary"], "")
        self.assertIn(club["company_type"], ("hospital", "pharma"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
