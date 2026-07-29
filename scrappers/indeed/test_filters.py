#!/usr/bin/env python3
"""Unit tests for indeed_scraper parsers/classifiers.

All worked examples come from real cards captured on 2026-07-29 from
https://in.indeed.com/jobs?q=...&l=Remote. Run with:

    python test_filters.py
"""

import unittest

from indeed_scraper import (
    classify_category,
    classify_company_type,
    compute_cutoff,
    epoch_ms_to_date,
    is_healthcare,
    is_remote,
    parse_salary,
    cards_from_html,
)


class TestSalaryParser(unittest.TestCase):
    # Isha Health Solutions "₹15,000 - ₹20,000 a month"
    def test_monthly_range(self):
        self.assertEqual(parse_salary(15000, 20000, "MONTHLY"),
                         ("INR 15000 - 20000 per month",
                          "15000", "20000", "INR", "per_month"))

    # IT Futurista "₹10,00,000 - ₹12,00,000 a year"
    def test_yearly_range(self):
        self.assertEqual(parse_salary(1000000, 1200000, "YEARLY"),
                         ("INR 1000000 - 1200000 per annum",
                          "1000000", "1200000", "INR", "per_annum"))

    # Nayan Tarit "From ₹21,000 a month" -> extractedSalary max == -1
    def test_open_ended_from(self):
        raw, lo, hi, cur, period = parse_salary(21000, -1, "MONTHLY")
        self.assertEqual((lo, hi, cur, period),
                         ("21000", "", "INR", "per_month"))
        self.assertIn("from 21000", raw)

    # Tricog Health "From ₹600 an hour"
    def test_hourly_not_club_exportable_period(self):
        _, lo, hi, cur, period = parse_salary(600, -1, "HOURLY")
        self.assertEqual(period, "per_hour")   # rich-only period

    # Nursing Mitr "₹13,238.09 - ₹34,688.12 a month" -> rounded ints
    def test_decimal_rounding(self):
        _, lo, hi, _, _ = parse_salary(13238.09, 34688.12, "MONTHLY")
        self.assertEqual((lo, hi), ("13238", "34688"))

    def test_no_salary(self):
        self.assertEqual(parse_salary("", "", ""),
                         ("Not Disclosed", "", "", "", ""))

    def test_swapped_range(self):
        _, lo, hi, _, _ = parse_salary(20000, 15000, "MONTHLY")
        self.assertEqual((lo, hi), ("15000", "20000"))


class TestHealthcareFilter(unittest.TestCase):
    def test_clinical_titles_kept(self):
        for title in [
            "Clinical Coordinator (Remote-Night Shift)",       # ZYLA Health
            "Assistant Nurse / Registered Nurse – Germany Opportunities",
            "Consulting Pediatrician, Telemedicine",           # NeoKids Pro
            "Medical Coder",                                   # MedCoded
            "Pharmacist- (Prescription Digitization & Verification)",
            "Psychiatrist Job in India",                       # Mantra Care
            "Clinical Nutritionist / Dietician",               # ZYLA Health
            "Paediatric Physiotherapist",
            "RCM Specialist - Quality (US Healthcare - DME/HME)",
            "Prior Authorization Specialist Remote (IN)",      # Snapscale
        ]:
            keep, signal = is_healthcare(title)
            self.assertTrue(keep, title)
            self.assertEqual(signal, "title", title)

    def test_non_healthcare_titles_denied(self):
        for title in [
            "Admissions Counsellor",                # UniAthena (education)
            "Academic counselor (WFH )",            # Brightchamps
            "Visa counsellor",                      # Canam Consultants
            "Telecaller / Admission Counsellor",
            "Hindi Transcriber",                    # Lionbridge (AI data)
            "Telugu Audio Annotation Specialist",
            "Data Annotator- Marathi",
            "Hindi Voice Collection Contributors",
            "English voice over artist",
            "IGCSE A Level Psychology Tutor (Online)",
            "Legal Transcriptionist (Australian Accent) – Remote",
        ]:
            keep, _ = is_healthcare(title)
            self.assertFalse(keep, title)

    def test_ambiguous_title_kept_flagged(self):
        # Vaighai Agro "Assistant Manager" — junk card the nurse query
        # surfaced; spec: keep + flag, never silently drop.
        keep, signal = is_healthcare("Assistant Manager")
        self.assertTrue(keep)
        self.assertEqual(signal, "needs_review")

    # "Medical transcriptionist" itself is healthcare; only the generic
    # language-data gigs are denied.
    def test_medical_transcription_kept(self):
        keep, signal = is_healthcare(
            "Online Medical Transcription Faculty (Remote) – Kerala Candidates Only")
        self.assertTrue(keep)
        self.assertEqual(signal, "title")


class TestCategoryClassifier(unittest.TestCase):
    def test_nurses(self):
        self.assertEqual(
            classify_category("Assistant Nurse / Registered Nurse – Germany Opportunities"),
            ("nurses", False))

    def test_doctors(self):
        for title in ["General Practitioner",
                      "Consulting Pediatrician, Telemedicine",
                      "Diabetologist",
                      "Consultant Dermatologist (Teleconsultation & AI Validation)",
                      "Medical Reviewer(MBBS/MD)",
                      "Psychiatrist"]:
            self.assertEqual(classify_category(title)[0], "doctors", title)

    def test_pharmacists(self):
        for title in ["Pharmacist- (Prescription Digitization & Verification)",
                      "PHARMACY TUTOR", "Clinical Pharmacologist"]:
            self.assertEqual(classify_category(title)[0], "pharmacists", title)

    def test_non_clinical(self):
        for title in ["Medical Coder", "Healthcare Content Editor",
                      "Prior Authorization Specialist Remote (IN)",
                      "Part Time Clinical Pschyologist",   # sic, real card
                      "Patient Care Assistant"]:
            self.assertEqual(classify_category(title)[0], "non_clinical", title)


class TestRemoteGate(unittest.TestCase):
    def test_remote_model(self):
        self.assertTrue(is_remote({"rw": "REMOTE_ALWAYS", "loc": "Remote"}))

    # "Remote in Bengaluru" cards: employer city + REMOTE_ALWAYS
    def test_remote_with_city(self):
        self.assertTrue(is_remote({"rw": "REMOTE_ALWAYS",
                                   "loc": "Bengaluru, Karnataka"}))

    # Nursing Mitr: empty model but formattedLocation == "Remote"
    def test_remote_by_location(self):
        self.assertTrue(is_remote({"rw": "", "loc": "Remote"}))

    # DISHHA Intensivist card: loc "India", no remote model -> padding row
    def test_not_remote_excluded(self):
        self.assertFalse(is_remote({"rw": "", "loc": "India"}))


class TestDates(unittest.TestCase):
    def test_epoch_ms(self):
        # Snapscale "Just posted" card captured 2026-07-29
        self.assertEqual(epoch_ms_to_date(1785214800000), "2026-07-28")
        # Isha "9 days ago" card
        self.assertEqual(epoch_ms_to_date(1784523600000), "2026-07-20")

    def test_bad_epoch(self):
        self.assertEqual(epoch_ms_to_date(""), "")
        self.assertEqual(epoch_ms_to_date(None), "")
        self.assertEqual(epoch_ms_to_date(-5), "")

    def test_cutoff_first_run(self):
        from datetime import date
        self.assertEqual(compute_cutoff(None, 30, today=date(2026, 7, 29)),
                         "2026-06-29")

    def test_cutoff_watermark(self):
        import pandas as pd
        df = pd.DataFrame({"posted_date": ["2026-07-20", "2026-07-28"]})
        self.assertEqual(compute_cutoff(df, 30), "2026-07-26")


class TestCompanyType(unittest.TestCase):
    def test_pharma(self):
        for name in ["Advanz Pharma", "Soterius", "IQVIA", "Parexel",
                     "Catalyst Clinical Research, LLC"]:
            self.assertEqual(classify_company_type(name), "pharma", name)

    def test_hospital_default(self):
        self.assertEqual(classify_company_type("ZYLA Health"), "hospital")


class TestHtmlExtraction(unittest.TestCase):
    def test_mosaic_extraction(self):
        html = ('<html><script>window.mosaic.providerData'
                '["mosaic-provider-jobcards"] = {"metaData":'
                '{"mosaicProviderJobCardsModel":{"results":[{"jobkey":"abc123",'
                '"title":"Medical Coder","company":"MedCoded",'
                '"formattedLocation":"Remote","pubDate":1783573200000,'
                '"remoteWorkModel":{"type":"REMOTE_ALWAYS"},'
                '"snippet":"<b>Assign</b> ICD-10 codes"}]}}};</script></html>')
        cards = cards_from_html(html, "test.html")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["k"], "abc123")
        self.assertEqual(cards[0]["sn"], "Assign ICD-10 codes")
        self.assertEqual(cards[0]["rw"], "REMOTE_ALWAYS")

    def test_no_mosaic(self):
        self.assertEqual(cards_from_html("<html>403</html>", "x"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
