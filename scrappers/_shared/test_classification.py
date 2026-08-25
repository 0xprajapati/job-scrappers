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
        self.assertIn("qualification", C.CLUB_COLUMNS)
        self.assertNotIn("is_active", C.CLUB_COLUMNS)
        self.assertNotIn("expires_at", C.CLUB_COLUMNS)
        self.assertEqual(len(C.CLUB_COLUMNS), 22)

    def test_category_values_are_top_level_only(self):
        for title in ("Clinical Research Associate", "Medical Coder",
                      "Regulatory Affairs Executive", "Community Health Officer"):
            v = C.classify_job(title, "", "")
            if v["in_scope"]:
                self.assertIn(v["category"], C.CATEGORIES, title)


if __name__ == "__main__":
    unittest.main()
