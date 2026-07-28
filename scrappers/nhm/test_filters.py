#!/usr/bin/env python3
"""Unit tests for the nhm.gov.in notice scraper. Run: python test_filters.py"""

import unittest

from scraper import (classify_category, extract_links, is_recruitment_notice,
                     make_job_id, notice_to_rich_row, rich_row_to_club_row)


class TestRecruitmentClassifier(unittest.TestCase):
    """Real-ish titles seen on NHM/state portals vs. the guideline noise the
    central portal actually publishes."""

    def test_accepts_hiring_notices(self):
        for title in [
            "Vacancy for Consultant (Public Health) under NPMU",
            "Advertisement for the post of Senior Consultant",
            "Applications are invited for Staff Nurse positions",
            "Walk-in interview for Medical Officers under NHM",
            "Engagement of Consultants at NHSRC",
            "Hiring of Data Entry Operator on contractual basis",
            "Recruitment of Community Health Officers 2026",
        ]:
            self.assertTrue(is_recruitment_notice(title, "https://nhm.gov.in/x.pdf"),
                            title)

    def test_rejects_guidelines_and_reports(self):
        for title in [
            "Revised Operational Guidelines for Mobile Medical Units (MMU) - 2026",
            "Training Modules for Medical Officers under NP-NCD",
            "National Family Health Survey (NFHS-6), 2023-24",
            "RBSK 2.0 Operational Guidelines, 2026-English",
            "ASHA Incentives July 2025",
            "17th Common Review Mission",
            "Quarterly NHM MIS Report (status as on 31.12.2025)",
            "Recruitment Rules operational guidelines for States",  # doc, not ad
        ]:
            self.assertFalse(is_recruitment_notice(title, "https://nhm.gov.in/x.pdf"),
                             title)

    def test_strong_phrase_beats_deny_word(self):
        # "walk-in"/"applications invited" wins even if a deny word appears
        self.assertTrue(is_recruitment_notice(
            "Walk-in interview under NP-NCD training programme",
            "https://nhm.gov.in/x.pdf"))

    def test_matches_on_filename_when_label_is_generic(self):
        self.assertTrue(is_recruitment_notice(
            "Click here", "https://nhm.gov.in/pdf/vacancy-consultant-2026.pdf"))


class TestCategoryClassifier(unittest.TestCase):
    def test_clinical_categories(self):
        self.assertEqual(classify_category("Recruitment of Staff Nurse"), "nurses")
        self.assertEqual(classify_category("Vacancy for Pharmacist"), "pharmacists")
        self.assertEqual(classify_category("Walk-in for Medical Officer"), "doctors")

    def test_default_non_clinical(self):
        self.assertEqual(classify_category("Engagement of Consultant (M&E)"),
                         "non_clinical")


class TestLinkExtraction(unittest.TestCase):
    HTML = ('<div><a href="/pdf/Vacancy-Consultant.pdf" target="_blank">'
            '<b>Vacancy</b> for Consultant</a>'
            '<a href="#top">top</a>'
            '<a href="index1.php?lang=1&amp;sublinkid=9">Guidelines</a></div>')

    def test_extracts_absolute_urls_and_clean_labels(self):
        links = extract_links(self.HTML, "https://nhm.gov.in/")
        self.assertEqual(links[0],
                         ("https://nhm.gov.in/pdf/Vacancy-Consultant.pdf",
                          "Vacancy for Consultant"))

    def test_skips_fragment_links(self):
        links = extract_links(self.HTML, "https://nhm.gov.in/")
        self.assertNotIn("#", " ".join(u for u, _ in links))


class TestRows(unittest.TestCase):
    def test_job_id_is_stable_url_path(self):
        self.assertEqual(make_job_id("https://nhm.gov.in/pdf/Vac.pdf"), "pdf/Vac.pdf")
        self.assertEqual(make_job_id("https://nhm.gov.in/index1.php?lang=1&lid=2"),
                         "index1.php?lang=1&lid=2")

    def test_rich_row_never_invents_salary(self):
        row = notice_to_rich_row("https://nhm.gov.in/pdf/Vac.pdf",
                                 "Vacancy for Staff Nurse")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["salary_min"], "")
        self.assertTrue(row["needs_review"])
        self.assertEqual(row["category"], "nurses")

    def test_club_row_contract(self):
        row = notice_to_rich_row("https://nhm.gov.in/pdf/Vac.pdf",
                                 "Engagement of Consultant")
        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_code"], "IN")
        self.assertEqual(club["application_url"], "https://nhm.gov.in/pdf/Vac.pdf")
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["is_active"], "true")


if __name__ == "__main__":
    unittest.main(verbosity=2)
