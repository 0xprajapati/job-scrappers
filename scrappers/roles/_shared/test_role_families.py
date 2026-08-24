#!/usr/bin/env python3
"""Tests for the shared role-family scorer. Run: python test_role_families.py"""

import sys
import unittest

from role_families import (
    FAMILY_NAMES, FIELD_WEIGHTS, MIN_SCORE_KEEP, OUT_OF_SCOPE_TITLE,
    classify, confidence_for, score_families,
)


class TestFamilySet(unittest.TestCase):
    EXPECTED = {
        "Public Health", "Clinical Data Management", "Clinical Research",
        "Medical Writer", "TMF", "Medical Coding", "Pharmacovigilance",
        "Regulatory Affairs", "Medical Reviewer", "MSL", "HEOR",
    }

    def test_exactly_the_eleven(self):
        self.assertEqual(set(FAMILY_NAMES), self.EXPECTED)

    def test_no_duplicates(self):
        self.assertEqual(len(FAMILY_NAMES), len(set(FAMILY_NAMES)))

    def test_every_family_reachable_from_a_title(self):
        samples = {
            "TMF": "Associate Director, TMF Operations Lead",
            "HEOR": "Senior Director, HEOR & Evidence Strategy",
            "MSL": "Medical Science Liaison - Metabolism",
            "Pharmacovigilance": "Executive Director, Pharmacovigilance (PV)",
            "Medical Coding": "Medical Coding Analyst - Trichy",
            "Medical Writer": "Senior Medical Writer",
            "Regulatory Affairs": "Senior Associate, Regulatory Affairs (US)",
            "Medical Reviewer": "Medical Monitor (Gastroenterology)",
            "Clinical Data Management": "Senior Clinical Data Manager",
            "Public Health": "Population Health Program Coordinator",
            "Clinical Research": "Senior Clinical Research Associate",
        }
        self.assertEqual(set(samples), self.EXPECTED)
        for family, title in samples.items():
            self.assertEqual(classify(title=title)["family"], family, title)


class TestFieldWeighting(unittest.TestCase):
    """A title hit alone must qualify; description text must not, on its own."""

    def test_title_hit_alone_is_high_confidence(self):
        v = classify(title="Senior Medical Writer")
        self.assertEqual(v["family"], "Medical Writer")
        self.assertEqual(v["confidence"], "high")
        self.assertEqual(v["matched_in"], "title")

    def test_single_description_mention_is_not_enough(self):
        # the boilerplate case: an unrelated role name-dropping the domain
        v = classify(title="Office Administrator",
                     description="Supports our clinical research division.")
        self.assertEqual(v["family"], "")

    def test_three_distinct_description_terms_qualify(self):
        v = classify(title="Programme Officer",
                     description="Oversees clinical trials, GCP compliance "
                                 "and site management across studies.")
        self.assertEqual(v["family"], "Clinical Research")
        self.assertEqual(v["matched_in"], "description")

    def test_skills_only_match_qualifies(self):
        # 35% of the reference set had no title hit at all
        v = classify(title="Senior Executive",
                     skills="pharmacovigilance,drug safety,argus")
        self.assertEqual(v["family"], "Pharmacovigilance")
        self.assertIn("skills", v["matched_in"])

    def test_repetition_cannot_inflate_a_score(self):
        spam = classify(title="Analyst",
                        description="clinical research " * 50)
        once = classify(title="Analyst", description="clinical research")
        self.assertEqual(spam["score"], once["score"])

    def test_weights_are_ordered_title_skills_description(self):
        self.assertGreater(FIELD_WEIGHTS["title"], FIELD_WEIGHTS["skills"])
        self.assertGreater(FIELD_WEIGHTS["skills"], FIELD_WEIGHTS["description"])

    def test_description_is_capped_so_footers_do_not_score(self):
        far = "x" * 6000 + " clinical trials GCP site management"
        self.assertEqual(classify(title="Analyst", description=far)["family"], "")


class TestMultiLabel(unittest.TestCase):
    def test_highest_score_wins(self):
        v = classify(title="Medical Writer",
                     skills="clinical research,clinical trials")
        self.assertEqual(v["family"], "Medical Writer")
        self.assertIn("Clinical Research", v["all_families"])

    def test_family_scores_are_reported(self):
        v = classify(title="Medical Writer", skills="clinical research,gcp")
        self.assertIn("Medical Writer=", v["family_scores"])
        self.assertIn(";", v["family_scores"])

    def test_tie_breaks_to_the_more_specific_family(self):
        # TMF precedes Clinical Research, so an exact tie resolves to TMF
        v = classify(title="TMF and Clinical Trial Lead")
        self.assertEqual(v["family"], "TMF")


class TestBoundaries(unittest.TestCase):
    """Real false positives that shaped the patterns."""

    def test_charge_description_master_is_not_cdm(self):
        self.assertNotEqual(
            classify(title="Revenue Integrity & CDM Operations Manager")["family"],
            "Clinical Data Management")

    def test_medical_stop_loss_is_flagged(self):
        v = classify(title="Regional Account Manager, Medical Stop Loss (MSL)")
        self.assertTrue(v["needs_review"])

    def test_legal_counsel_is_flagged_not_dropped(self):
        v = classify(title="Senior Counsel, Global Commercial Legal - Market Access")
        self.assertEqual(v["family"], "HEOR")     # kept
        self.assertTrue(v["needs_review"])        # but doubtful

    def test_software_engineer_at_a_cro_is_flagged(self):
        v = classify(title="Senior Software Engineer",
                     skills="clinical research,clinical trials,gcp")
        self.assertTrue(v["needs_review"])

    def test_out_of_scope_regex_ignores_plain_titles(self):
        self.assertFalse(OUT_OF_SCOPE_TITLE.search("Clinical Data Manager"))


class TestConfidence(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(confidence_for(9), "high")
        self.assertEqual(confidence_for(5), "high")
        self.assertEqual(confidence_for(3), "medium")
        self.assertEqual(confidence_for(1), "low")

    def test_kept_rows_are_never_low_confidence(self):
        # MIN_SCORE_KEEP and the medium threshold must not disagree
        self.assertGreaterEqual(MIN_SCORE_KEEP, 3)


class TestSkillsInputForms(unittest.TestCase):
    def test_list_and_string_are_equivalent(self):
        a = classify(title="X", skills=["pharmacovigilance", "drug safety"])
        b = classify(title="X", skills="pharmacovigilance,drug safety")
        self.assertEqual(a["family"], b["family"])
        self.assertEqual(a["score"], b["score"])

    def test_empty_inputs_are_out_of_scope(self):
        self.assertEqual(classify()["family"], "")
        self.assertEqual(classify(title=None, skills=None,
                                  description=None)["family"], "")


if __name__ == "__main__":
    r = unittest.main(argv=[sys.argv[0], "-q"], exit=False).result
    sys.exit(0 if r.wasSuccessful() else 1)
