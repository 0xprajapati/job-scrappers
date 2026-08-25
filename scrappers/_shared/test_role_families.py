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


class TestPublicHealthSubCategories(unittest.TestCase):
    """Every PH sub-category (taxonomy 2026-08-24) must be reachable."""

    TITLES = {
        "Epidemiology": "Field Epidemiologist - Disease Surveillance",
        "Program Management": "Public Health Programme Manager",
        "Monitoring & Evaluation": "M&E Officer - Health Projects",
        "Community Health": "Community Health Worker Supervisor",
        "Health Promotion & Education": "Health Education Specialist",
        "Disease Programs": "District Tuberculosis Coordinator (NTEP)",
        "Nutrition": "Public Health Nutritionist",
        "IPC": "Infection Prevention and Control Nurse",
        "Health Informatics & Data": "HMIS / DHIS2 Data Analyst",
        "PH Research": "Health Systems Research Associate",
        "MCH fold-in": "Maternal and Child Health Consultant",
        "WASH fold-in": "WASH Officer",
    }

    def test_each_sub_category_title_lands_in_public_health(self):
        for sub, title in self.TITLES.items():
            self.assertEqual(classify(title=title)["family"],
                             "Public Health", "{}: {}".format(sub, title))

    def test_monitoring_and_evaluation_spelled_out(self):
        v = classify(title="Manager - Monitoring and Evaluation")
        self.assertEqual(v["family"], "Public Health")

    def test_health_economics_is_heor_not_public_health(self):
        # taxonomy fold-in rule: never double-tag health economics as PH
        v = classify(title="Health Economics and Market Access Manager")
        self.assertEqual(v["family"], "HEOR")

    def test_vaccine_cra_stays_clinical_research(self):
        # "vaccinat*" not "vaccin*": vaccine-product trial roles are CR
        v = classify(title="Clinical Research Associate - Vaccine Trials")
        self.assertEqual(v["family"], "Clinical Research")

    def test_surgery_degree_mch_is_not_public_health(self):
        self.assertEqual(
            classify(title="Consultant Urologist (M.Ch)")["family"], "")

    def test_media_and_entertainment_is_not_public_health(self):
        self.assertEqual(
            classify(title="Account Director, Media & Entertainment (M&E)"
                     )["family"], "")

    def test_washing_machine_sales_is_not_public_health(self):
        self.assertEqual(
            classify(title="Territory Manager - Washing Machines")["family"],
            "")

    def test_poultry_nutrition_is_not_public_health(self):
        # real shine card from the 2026-08-24 backfill
        self.assertEqual(
            classify(title="Poultry Nutrition Specialist")["family"], "")
        self.assertEqual(
            classify(title="Animal Nutritionist")["family"], "")

    def test_ph_needs_more_than_one_skill_tag(self):
        # FAMILY_MIN_SCORE: a phlebotomist tagged "infection control"
        # (skill 2 + description 1 = 3) must NOT become Public Health
        v = classify(title="Sitting Phlebotomist",
                     skills="phlebotomy, blood collection, infection control",
                     description="Maintains infection control protocols.")
        self.assertNotEqual(v["family"], "Public Health")

    def test_ph_title_hit_still_qualifies(self):
        self.assertEqual(classify(title="Epidemiologist")["family"],
                         "Public Health")

    def test_ph_skill_cluster_plus_description_qualifies(self):
        # the real "Health Officer (Coimbatore)" card: 2 skill terms (4)
        # + description mention (1) clears the PH bar of 5
        v = classify(title="Health Officer",
                     skills="public health, health promotion, data collection",
                     description="District-level public health programmes.")
        self.assertEqual(v["family"], "Public Health")

    def test_ph_two_skill_terms_alone_do_not_qualify(self):
        # score 4 < FAMILY_MIN_SCORE["Public Health"]
        v = classify(title="Analyst",
                     skills="epidemiology, health data")
        self.assertEqual(v["family"], "")

    def test_ehs_is_not_public_health(self):
        # real shine cards: EHS is workplace safety, not public health
        for title in ("Remote Environmental Health and Safety Coordinator",
                      "Senior Environmental Health & Safety Specialist"):
            self.assertNotEqual(classify(title=title)["family"],
                                "Public Health", title)
        # bare "environmental health" (no safety) still counts
        self.assertEqual(
            classify(title="Environmental Health Officer")["family"],
            "Public Health")

    def test_occupational_health_programs_is_not_ph(self):
        v = classify(title="Occupational Health Physician",
                     skills="Occupational Health Programs, Compliance")
        self.assertNotEqual(v["family"], "Public Health")

    def test_ph_description_only_never_qualifies(self):
        # an HR role scored 5 on scattered description boilerplate
        v = classify(title="Associate Director, HR Operations",
                     description="Supports public health programmes, "
                                 "community health drives, immunization "
                                 "camps, nutrition officer coordination and "
                                 "health promotion events.")
        self.assertEqual(v["family"], "")

    def test_other_families_keep_the_lower_bar(self):
        # PV: one skill tag + one description mention (score 3) still keeps
        v = classify(title="Senior Executive",
                     skills="pharmacovigilance",
                     description="Handles pharmacovigilance case intake.")
        self.assertEqual(v["family"], "Pharmacovigilance")


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


class TestUserDecisions20260825(unittest.TestCase):
    """Four classifier calls the user made on 2026-08-25."""

    def test_fire_and_safety_officer_is_not_pharmacovigilance(self):
        # manipalhospitals false positive: workplace safety, not drug safety.
        self.assertEqual(
            classify(title="Fire and Safety Officer")["family"], "")

    def test_real_drug_safety_titles_still_match(self):
        for title in ("Drug Safety Officer", "Pharmacovigilance Officer",
                      "Safety Physician"):
            self.assertEqual(classify(title=title)["family"],
                             "Pharmacovigilance", title)

    def test_payer_side_utilization_review_is_out(self):
        # ~24 himalayas rows; in scope only when the job also reads as MSL.
        for title in ("Utilization Review Nurse",
                      "Utilization Management Coordinator",
                      "Disability Peer Reviewer"):
            self.assertEqual(classify(title=title)["family"], "", title)
        self.assertEqual(
            classify(title="Medical Science Liaison, Utilization Management"
                     )["family"], "MSL")

    def test_clinical_coding_officer_is_medical_coding(self):
        # Commonwealth/Gulf title; two genuine PHCC vacancies were dropped.
        for title in ("Clinical Coding Officer", "Coding Officer",
                      "Clinical Coding Specialist"):
            self.assertEqual(classify(title=title)["family"],
                             "Medical Coding", title)

    def test_dieticians_are_public_health(self):
        for title in ("Clinical Dietitian", "Hospital Dietician",
                      "Clinical Nutritionist"):
            self.assertEqual(classify(title=title)["family"],
                             "Public Health", title)
        # the animal/sports guards still hold
        for title in ("Animal Dietitian", "Sports Dietitian"):
            self.assertEqual(classify(title=title)["family"], "", title)


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
