"""Canonical two-level classifier — the ONLY categorization scrapers may use.

Adopted 2026-08-25, replacing the retired profession enum
(doctors/nurses/pharmacists/non_clinical) fleet-wide.  The taxonomy
(Jobs_keywords/keywords_for_jobs.md, mirrored in taxonomy_keywords.py):

    category      "Non Clinical" | "Public Health"
    sub_category  one of 20 names — the ten Non Clinical role families
                  (Clinical Data Management, Clinical Research, Medical
                  Writer, TMF, Medical Coding, Pharmacovigilance,
                  Regulatory Affairs, Medical Reviewer, MSL, HEOR) or the
                  ten Public Health splits (Epidemiology, Public Health
                  Program Management, Monitoring & Evaluation, Community
                  Health, Health Promotion & Education, Disease Programs,
                  Public Health Nutrition, Infection Prevention & Control,
                  Health Informatics & Data, Public Health Research)

This module composes the two existing engines:
  * role_families.py     — weighted in-scope gate (title x5 / skills x2 /
                           description x1) across the eleven families
  * taxonomy_keywords.py — negative-keyword veto + finer sub-category split

Contract for every scraper:
  * call classify_job(title, skills, description) for every candidate job;
  * a result with in_scope False is DROPPED (count it as
    excluded_out_of_scope; never export it);
  * an in-scope result fills `category` and `sub_category` in both the rich
    CSV and the club CSV; `role_family` and the score-trace fields belong in
    the rich CSV so any admission stays auditable;
  * no scraper defines its own category regexes or enum values.
"""

import role_families as RF
import taxonomy_keywords as TK

CATEGORIES = ("Non Clinical", "Public Health")
SUBCATEGORY_NAMES = TK.SUBCATEGORY_NAMES

# Grounded credential extraction (MBBS, PharmD, MPH, CPC, ...) — re-exported
# so scrapers import everything classification-related from one module.
extract_qualification = RF.extract_qualification

# The one club CSV contract (jobs_csv/<DD-MM-YYYY>/<site>.csv), 22 columns.
# `category` holds "Non Clinical" | "Public Health"; `sub_category` holds the
# finer split.  The old is_active/expires_at columns are retired.
CLUB_COLUMNS = [
    "country_name", "country_code", "country_dial_code", "city_name",
    "company_name", "company_type", "company_logo", "company_about",
    "title", "description", "job_type", "category", "sub_category",
    "application_url", "posted_at", "min_experience", "max_experience",
    "qualification", "min_salary", "max_salary", "salary_period",
    "salary_currency",
]


def _out_of_scope(vetoed_by=""):
    return {
        "in_scope": False, "category": "", "sub_category": "",
        "sub_category_basis": "", "role_family": "", "needs_review": False,
        "vetoed_by": vetoed_by, "all_families": "", "family_scores": "",
        "family_confidence": "", "matched_in": "",
    }


def classify_job(title, skills="", description=""):
    """Classify one job into the two-level taxonomy.

    Returns a dict:
        in_scope            bool — False means DROP the job
        category            "Non Clinical" | "Public Health" | ""
        sub_category        winning sub-category ("" only for a Public
                            Health job the finer split could not place)
        sub_category_basis  "title" | "skills" | "family" | ""
        role_family         the winning role family (rich-CSV trace)
        needs_review        bool — in-scope but the title looks like a
                            different profession; keep AND flag
        vetoed_by           negative keyword that blocked the row, or ""
        all_families / family_scores / family_confidence / matched_in
                            score trace from role_families (rich CSV)
    """
    tax = TK.classify_subcategory(title, skills, description)
    if tax["vetoed_by"]:
        return _out_of_scope(vetoed_by=tax["vetoed_by"])

    verdict = RF.classify(title=title, skills=skills, description=description)
    family = verdict["family"]
    if not family:
        return _out_of_scope()

    sub_category, basis = tax["sub_category"], tax["basis"]
    if sub_category:
        category = tax["category"]
    elif family == "Public Health":
        # Public Health's single family is coarser than its ten
        # sub-categories, so there is no honest fallback: leave blank.
        category = "Public Health"
    else:
        # The ten Non Clinical family names ARE their sub-categories.
        category, sub_category, basis = "Non Clinical", family, "family"

    return {
        "in_scope": True,
        "category": category,
        "sub_category": sub_category,
        "sub_category_basis": basis,
        "role_family": family,
        "needs_review": bool(verdict.get("needs_review")),
        "vetoed_by": "",
        "all_families": verdict.get("all_families", ""),
        "family_scores": verdict.get("family_scores", ""),
        "family_confidence": verdict.get("confidence", ""),
        "matched_in": verdict.get("matched_in", ""),
    }
