"""Tests for classification.classify_job — the fleet-wide two-level gate."""

import unittest

import classification as C


class TestClassifyJob(unittest.TestCase):
    def test_pharmacovigilance_title(self):
        v = C.classify_job("Pharmacovigilance Associate", "", "")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["category"], "Non Clinical")
        self.assertEqual(v["sub_category"], "Pharmacovigilance")
        self.assertEqual(v["role_family"], "Pharmacovigilance")

    def test_epidemiologist_is_public_health(self):
        v = C.classify_job("Epidemiologist", "disease surveillance", "")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["category"], "Public Health")
        self.assertEqual(v["sub_category"], "Epidemiology")

    def test_ngo_monitoring_titles_are_public_health(self):
        # 2026-08-25: the comma forms are the standard NGO title format and
        # were all being dropped. Found via publichealthcareer, but the gap
        # suppressed Public Health yield fleet-wide.
        for title in ("Monitoring, Evaluation and Learning Trainee",
                      "Monitoring, Evaluation, Accountability and Learning "
                      "Officer"):
            v = C.classify_job(title)
            self.assertTrue(v["in_scope"], title)
            self.assertEqual(v["category"], "Public Health", title)
            self.assertEqual(v["sub_category"], "Monitoring & Evaluation",
                             title)

    def test_meal_acronym_does_not_admit_catering(self):
        # MEAL = Monitoring, Evaluation, Accountability and Learning. The
        # acronym must still work, but "Meal Coordinator" is food service
        # and used to be admitted as Public Health.
        for title in ("MEAL Officer", "MEAL Manager"):
            self.assertTrue(C.classify_job(title)["in_scope"], title)
        for title in ("Meal Coordinator", "Meal Service Coordinator"):
            self.assertFalse(C.classify_job(title)["in_scope"], title)

    def test_staff_nurse_is_vetoed(self):
        v = C.classify_job("Staff Nurse - ICU", "", "")
        self.assertFalse(v["in_scope"])
        self.assertTrue(v["vetoed_by"])

    def test_software_engineer_out_of_scope(self):
        v = C.classify_job("Senior Software Engineer", "java, spring", "")
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["category"], "")

    def test_generic_title_admitted_via_skills(self):
        v = C.classify_job(
            "Senior Executive",
            "pharmacovigilance, argus safety, case processing, ICSR",
            "")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["category"], "Non Clinical")

    def test_out_of_scope_profession_flagged_not_dropped(self):
        v = C.classify_job(
            "Business Development Manager - Regulatory Affairs", "", "")
        if v["in_scope"]:
            self.assertTrue(v["needs_review"])

    def test_medical_billing_vetoed(self):
        v = C.classify_job("Medical Billing Executive", "AR calling", "")
        self.assertFalse(v["in_scope"])
        self.assertTrue(v["vetoed_by"])

    def test_club_columns_contract(self):
        self.assertIn("category", C.CLUB_COLUMNS)
        self.assertIn("sub_category", C.CLUB_COLUMNS)
        self.assertIn("role_family", C.CLUB_COLUMNS)
        self.assertIn("qualification", C.CLUB_COLUMNS)
        self.assertNotIn("is_active", C.CLUB_COLUMNS)
        self.assertNotIn("expires_at", C.CLUB_COLUMNS)
        self.assertEqual(len(C.CLUB_COLUMNS), 23)

    def test_category_values_are_top_level_only(self):
        for title in ("Clinical Research Associate", "Medical Coder",
                      "Regulatory Affairs Executive", "Community Health Officer"):
            v = C.classify_job(title, "", "")
            if v["in_scope"]:
                self.assertIn(v["category"], C.CATEGORIES, title)


class TestGenericResearchGate(unittest.TestCase):
    """2026-08-27 rulings: a bare academic research title is not evidence of
    clinical research, and a blank Public Health sub_category must be
    flagged, never shipped silently."""

    def test_non_health_academic_ra_dropped(self):
        # Was admitted as Public Health Research / high confidence.
        v = C.classify_job(
            "Research Associate in Islamic Art (Fixed Term)", "",
            "The Faculty of Arts invites applications for research on "
            "medieval manuscripts.")
        self.assertFalse(v["in_scope"])

    def test_bench_rnd_ra_dropped(self):
        v = C.classify_job(
            "Senior Research Associate - Formulation Development", "",
            "Walk-in for synthesis R&D, HPLC, formulation development.")
        self.assertFalse(v["in_scope"])

    def test_ra_with_clinical_corroboration_kept(self):
        v = C.classify_job(
            "Research Associate", "",
            "Join our CRO to support clinical study start-up and site "
            "monitoring per GCP.")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["sub_category"], "Clinical Research")
        self.assertFalse(v["needs_review"])

    def test_generic_ra_with_health_title_kept_and_flagged(self):
        # Real docthub row: empty description, health context only in the
        # title. Kept + flagged rather than dropped (master-spec §2).
        v = C.classify_job(
            "Research Coordinator Jobs in VN Allergy & Asthma Research "
            "Centre, Chennai", "", "")
        self.assertTrue(v["in_scope"])
        self.assertTrue(v["needs_review"])

    def test_blank_ph_subcategory_flagged(self):
        # In scope as Public Health, but no sub-category qualifies: the row
        # ships with sub_category blank AND needs_review True. (Real
        # devnetjobs row — "Global Health" with no role word after it.)
        v = C.classify_job(
            "Director, Global Health and Development Team", "",
            "Lead the global health and development team's strategy.")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["category"], "Public Health")
        self.assertEqual(v["sub_category"], "")
        self.assertTrue(v["needs_review"])

    def test_fold_in_titles_get_sub_categories(self):
        # 2026-08-27 SHARED-03: fold-in vocabulary (keywords_for_jobs.md
        # rule 3) implemented on the splitter side — these shipped blank
        # before. Real titles from the stores.
        cases = [
            ("WASH Specialist (NO-3), Fixed Term",
             "Public Health Program Management"),
            ("Child Health Specialist (NO-3), FT",
             "Public Health Program Management"),
            ("Immunization Specialist, NO-3, FT, Lome, Togo",
             "Disease Programs"),
            ("Community Health Associate (FTC)", "Community Health"),
            ("Nutrition Specialist", "Public Health Nutrition"),
            ("Country MEAL Manager", "Monitoring & Evaluation"),
            ("M&E Coordinator", "Monitoring & Evaluation"),
            ("Health Information Management (HIM) Clerk",
             "Health Informatics & Data"),
        ]
        for title, want in cases:
            v = C.classify_job(title, "", "")
            self.assertTrue(v["in_scope"], title)
            self.assertEqual(v["sub_category"], want, title)
            self.assertFalse(v["needs_review"], title)

    def test_meal_coordinator_is_still_food_service(self):
        # The family gate deliberately rejects "Meal Coordinator"; the new
        # MEAL titles in the M&E splitter must not resurrect it.
        v = C.classify_job("Meal Coordinator", "", "")
        self.assertFalse(v["in_scope"])

    def test_ph_research_still_reachable_via_strong_keywords(self):
        # The family gate needs a PH signal in title/skills (as it always
        # has); the sub-category then comes from the strong-keyword tier,
        # which the title-tier demotion did not touch.
        v = C.classify_job(
            "Public Health Research Associate",
            "household survey, SurveyCTO, enumerator training",
            "NFHS-style household survey data collection, focus group "
            "discussion facilitation.")
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["sub_category"], "Public Health Research")


if __name__ == "__main__":
    unittest.main()
