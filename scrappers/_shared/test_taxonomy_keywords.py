#!/usr/bin/env python3
"""Sanity checks for taxonomy_keywords.classify_subcategory."""

from taxonomy_keywords import classify_subcategory, SUBCATEGORIES


def test_title_match_wins():
    r = classify_subcategory(title="Senior Clinical Data Manager")
    assert r["sub_category"] == "Clinical Data Management"
    assert r["basis"] == "title"
    assert r["category"] == "Non Clinical"


def test_garbage_title_rescued_by_strong_skills():
    r = classify_subcategory(
        title="Urgent Opening - MNC Life Sciences",
        skills="Argus, MedDRA, ICSR case processing, E2B")
    assert r["sub_category"] == "Pharmacovigilance"
    assert r["basis"] == "skills"
    assert r["strong_hits"] >= 2


def test_one_strong_keyword_is_not_enough():
    r = classify_subcategory(title="Data Analyst", skills="MedDRA, Excel")
    assert r["sub_category"] == ""


def test_negative_keyword_vetoes():
    r = classify_subcategory(
        title="Medical Billing Executive",
        skills="ICD-10-CM, CPT, revenue cycle")
    assert r["sub_category"] == ""
    assert r["vetoed_by"]


def test_me_engineer_does_not_hit_monitoring_evaluation():
    r = classify_subcategory(title="M&E Engineer - Construction",
                             skills="HVAC, electrical")
    assert r["sub_category"] == ""


def test_public_health_subcategory_split():
    r = classify_subcategory(title="District Epidemiologist - IDSP")
    assert r["category"] == "Public Health"
    assert r["sub_category"] == "Epidemiology"


def test_disease_programs_strong_cluster():
    r = classify_subcategory(
        title="Consultant",
        skills="NTEP, Nikshay, DOTS implementation")
    assert r["sub_category"] == "Disease Programs"


def test_short_token_only_matches_in_title():
    # "TMF" inside skills text alone must not classify (title-only token).
    r = classify_subcategory(title="Executive", skills="TMF")
    assert r["sub_category"] == ""
    r = classify_subcategory(title="TMF Specialist")
    assert r["sub_category"] == "TMF"


def test_dietitian_is_public_health_nutrition():
    # User decision 2026-08-25: clinical/hospital dieticians are in scope
    # under Public Health -> Public Health Nutrition. The veto that used to
    # keep Nutrition strictly programmatic was retired with it.
    for title in ("Clinical Dietitian", "Hospital Dietician", "Dietitian"):
        r = classify_subcategory(title=title,
                                 skills="nutrition, diet counselling")
        assert r["sub_category"] == "Public Health Nutrition", title
        assert r["category"] == "Public Health"
    # A sports dietician is still a fitness role, not public health.
    assert classify_subcategory(title="Sports Dietitian")["sub_category"] == ""


def test_clinical_coding_officer_is_medical_coding():
    # Commonwealth/Gulf title; two genuine PHCC vacancies were dropped by
    # the old pattern, which only knew "clinical coder".
    r = classify_subcategory(title="Clinical Coding Officer")
    assert r["sub_category"] == "Medical Coding"


def test_meddra_tiebreak_prefers_title():
    # MedDRA appears in both PV and Medical Reviewer strong lists; a title
    # match must settle it before skills counting starts.
    r = classify_subcategory(title="Medical Reviewer",
                             skills="MedDRA, causality assessment")
    assert r["sub_category"] == "Medical Reviewer"
    assert r["basis"] == "title"


def test_every_subcategory_title_regex_compiles_and_hits_itself():
    # Each sub-category name (or a canonical title) should be reachable.
    canonical = {
        "Clinical Data Management": "Clinical Data Manager",
        "Clinical Research": "Clinical Research Associate",
        "Medical Writer": "Medical Writer",
        "TMF": "TMF Specialist",
        "Medical Coding": "Medical Coder",
        "Pharmacovigilance": "Pharmacovigilance Associate",
        "Regulatory Affairs": "Regulatory Affairs Executive",
        "Medical Reviewer": "Medical Reviewer",
        "MSL": "Medical Science Liaison",
        "HEOR": "HEOR Analyst",
        "Epidemiology": "Epidemiologist",
        "Public Health Program Management": "Program Officer",
        "Monitoring & Evaluation": "M&E Officer",
        "Community Health": "Community Health Officer",
        "Health Promotion & Education": "Health Educator",
        "Disease Programs": "Immunization Officer",
        "Public Health Nutrition": "Public Health Nutritionist",
        "Infection Prevention & Control": "Infection Control Officer",
        "Health Informatics & Data": "Health Data Analyst",
        "Public Health Research": "Field Investigator",
    }
    assert set(canonical) == set(SUBCATEGORIES)
    for expected, title in canonical.items():
        got = classify_subcategory(title=title)["sub_category"]
        assert got == expected, "%r -> %r, wanted %r" % (title, got, expected)
