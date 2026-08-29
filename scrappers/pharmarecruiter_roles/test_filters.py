#!/usr/bin/env python3
"""Unit tests for the pharmarecruiter.in role-targeted scraper.

Run with plain:  python test_filters.py
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    apply_classification,
    classify_company_type,
    classify_job_type,
    clean_company,
    clean_title,
    compute_cutoff,
    extract_labeled_fields,
    normalize_city,
    parse_experience,
    parse_location,
    parse_salary,
    post_to_rich_row,
    rich_row_to_club_row,
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    RICH_COLUMNS,
    SEARCH_TERMS,
    PAGE_SIZE,
    _NEWSY_TITLE_RE,
)


class TestExtractLabeledFields(unittest.TestCase):
    # Real shape from post 39698 (Accuprec Research Labs, 2026-07-24).
    HTML = """
    <h2>Job Details</h2>
    <ul class="wp-block-list">
      <li><strong>Company Name</strong>: Accuprec Research Labs Pvt Ltd</li>
      <li><strong>Experience</strong>: 0-7 Years (varies by role)</li>
      <li><strong>Qualification</strong>: M.Pharm, M.Sc, BE, B.Tech</li>
      <li><strong>Location</strong>: Ahmedabad, India</li>
      <li><strong>Work Type</strong>: Full-time, On-site</li>
    </ul>
    <ul><li><strong>Location</strong>: Some venue address later on</li></ul>
    """

    def test_extracts_all_fields(self):
        fields = extract_labeled_fields(self.HTML)
        self.assertEqual(fields["company"], "Accuprec Research Labs Pvt Ltd")
        self.assertEqual(fields["experience"], "0-7 Years (varies by role)")
        self.assertEqual(fields["location"], "Ahmedabad, India")
        self.assertEqual(fields["work_type"], "Full-time, On-site")

    def test_first_value_wins(self):
        # Job Details list comes first; a later venue list must not override.
        fields = extract_labeled_fields(self.HTML)
        self.assertEqual(fields["location"], "Ahmedabad, India")

    def test_organization_label_maps_to_company(self):
        # Real shape from post 39709 (UPSC/CDSCO government posting).
        html = "<ul><li><strong>Organization</strong>: CDSCO</li></ul>"
        self.assertEqual(extract_labeled_fields(html)["company"], "CDSCO")

    def test_ignores_unlabeled_bullets(self):
        html = "<ul><li>Just a sentence with no colon</li></ul>"
        self.assertEqual(extract_labeled_fields(html), {})


class TestCleanTitle(unittest.TestCase):
    """FIX 2026-08-27 (PHARMARECRUITER-02): SEO headlines -> job titles.

    All examples are real stored headlines from the 2026-08-27 audit.
    """

    def test_pipe_cut_and_jobs_in_tail(self):
        self.assertEqual(
            clean_title("Medical Writer Jobs in Delhi | "
                        "Insignia Clinical Research Careers"),
            "Medical Writer")

    def test_multi_city_jobs_in_tail(self):
        self.assertEqual(
            clean_title("Health Economics Intern Jobs in Bangalore & Gurugram "
                        "| IQVIA Pharma Careers"),
            "Health Economics Intern")

    def test_jobs_location_tail_without_in(self):
        self.assertEqual(
            clean_title("Senior PV Scientist & Team Lead Case Processing "
                        "Jobs Mumbai Noida"),
            "Senior PV Scientist & Team Lead Case Processing")

    def test_walk_in_drive_prefix(self):
        self.assertEqual(
            clean_title("Piramal Pharma Walk-In Drive 2026 – Senior Research "
                        "Associate Formulation Development Jobs in Ahmedabad"),
            "Senior Research Associate Formulation Development")

    def test_company_hiring_prefix(self):
        self.assertEqual(
            clean_title("Cactus Life Sciences Hiring 2026: Senior Medical "
                        "Writer – Medical Information Jobs (Remote – India)"),
            "Senior Medical Writer – Medical Information")

    def test_company_jobs_year_colon_prefix(self):
        self.assertEqual(
            clean_title("Novo Nordisk Pharma Jobs 2026: "
                        "Central Monitor Hiring in Bangalore"),
            "Central Monitor")

    def test_trailing_careers_year_suffix(self):
        self.assertEqual(
            clean_title("TMF Specialist Jobs in Chennai | "
                        "ICON Clinical Research Careers 2026"),
            "TMF Specialist")

    def test_recruitment_year_location_tail(self):
        self.assertEqual(
            clean_title("GSK Junior Programmer Recruitment 2026 – Bengaluru"),
            "GSK Junior Programmer")

    def test_short_result_falls_back_to_pipe_cut_only(self):
        # "Intern" alone is under the ~10-char floor; keep the less
        # aggressively cleaned pipe cut instead
        self.assertEqual(
            clean_title("Intern Jobs in Chennai & Bangalore | "
                        "ICON Clinical Research Internship 2026"),
            "Intern Jobs in Chennai & Bangalore")

    def test_never_leaves_a_pipe(self):
        self.assertNotIn("|", clean_title(
            "Quality Assurance Coordinator Jobs at Clario | "
            "Remote Pharma QA Jobs in India | Apply Online"))

    def test_plain_title_is_untouched(self):
        self.assertEqual(
            clean_title("Safety Science Specialist Job Opening at Fortrea"),
            "Safety Science Specialist Job Opening at Fortrea")

    def test_jobs_at_company_is_not_a_location_tail(self):
        # "Jobs at <Company>" keeps the employer context; only
        # "Jobs [in] <locations>" tails are stripped
        self.assertEqual(
            clean_title("Clinical Research Associate (CRA) Jobs at "
                        "medONE Pharma Solutions – Gurgaon"),
            "Clinical Research Associate (CRA) Jobs at "
            "medONE Pharma Solutions – Gurgaon")


class TestCleanCompany(unittest.TestCase):
    """FIX 2026-08-27 (PHARMARECRUITER-01): a failed company extraction is
    EMPTY, never the SEO page title. Examples are the audit's real rows."""

    def test_seo_page_title_with_pipe_is_rejected(self):
        self.assertEqual(clean_company(
            "Senior PV Scientist Jobs in Mumbai & Noida | "
            "Pharmacovigilance Careers in India"), "")

    def test_over_60_chars_is_rejected(self):
        self.assertEqual(clean_company(
            "Senior PV Scientist & Team Lead Case Processing "
            "Jobs Mumbai Noida"), "")

    def test_jobs_in_phrase_is_rejected(self):
        self.assertEqual(
            clean_company("Medical Writer Jobs in Delhi"), "")

    def test_real_company_passes(self):
        self.assertEqual(clean_company("Accuprec Research Labs Pvt Ltd"),
                         "Accuprec Research Labs Pvt Ltd")

    def test_empty_stays_empty(self):
        self.assertEqual(clean_company(""), "")
        self.assertEqual(clean_company(None), "")

    def test_club_row_never_falls_back_to_title(self):
        # the old rich_row_to_club_row fell back to the post title when
        # company was empty — that is exactly the PHARMARECRUITER-01 leak
        club = rich_row_to_club_row({"title": "Senior PV Scientist",
                                     "company": ""})
        self.assertEqual(club["company_name"], "")

    def test_raw_columns_are_in_rich_schema(self):
        # added 2026-08-27, rightmost so pre-existing rows load cleanly
        self.assertEqual(RICH_COLUMNS[-2:], ["title_raw", "location_raw"])


class TestPostToRichRow(unittest.TestCase):
    """Integration: SEO title/company handling inside the row builder."""

    POST = {
        "id": 99001,
        "date": "2026-08-27T09:00:00",
        "link": "https://pharmarecruiter.in/senior-pv-scientist/",
        "title": {"rendered": "Senior PV Scientist Jobs in Mumbai &amp; Noida "
                              "| Pharmacovigilance Careers in India"},
        "content": {"rendered": (
            "<h2>Job Details</h2><ul>"
            "<li><strong>Company Name</strong>: Senior PV Scientist Jobs in "
            "Mumbai &amp; Noida | Pharmacovigilance Careers in India</li>"
            "<li><strong>Location</strong>: India – Mumbai; India – Noida</li>"
            "</ul>")},
        "categories": [1],
    }

    def test_seo_leak_is_contained(self):
        row = post_to_rich_row(self.POST, {1: "jobs"})
        self.assertEqual(row["title"], "Senior PV Scientist")
        self.assertEqual(row["title_raw"],
                         "Senior PV Scientist Jobs in Mumbai & Noida "
                         "| Pharmacovigilance Careers in India")
        self.assertEqual(row["company"], "")       # guard, not the headline
        self.assertTrue(row["needs_review"])       # missing company flags it
        self.assertEqual(row["city"], "Mumbai")    # first site only
        self.assertEqual(row["location_raw"],
                         "India – Mumbai; India – Noida")


class TestParseSalary(unittest.TestCase):
    def test_empty_returns_empty_dict(self):
        self.assertEqual(parse_salary(""), {})
        self.assertEqual(parse_salary(None), {})

    def test_no_numbers_keeps_raw_only(self):
        result = parse_salary("Best in Industry")
        self.assertEqual(result["salary_raw"], "Best in Industry")
        self.assertNotIn("salary_min", result)

    def test_lpa_range(self):
        result = parse_salary("₹3.5 – 5 LPA")
        self.assertEqual(result["salary_min"], 350_000)
        self.assertEqual(result["salary_max"], 500_000)
        self.assertEqual(result["salary_period"], "per_annum")
        self.assertEqual(result["salary_currency"], "INR")

    def test_monthly_amount(self):
        result = parse_salary("Rs. 25,000 per month")
        self.assertEqual(result["salary_min"], 25_000)
        self.assertEqual(result["salary_max"], 25_000)
        self.assertEqual(result["salary_period"], "per_month")

    def test_k_suffix_range(self):
        result = parse_salary("15k-20k")
        self.assertEqual(result["salary_min"], 15_000)
        self.assertEqual(result["salary_max"], 20_000)
        self.assertEqual(result["salary_period"], "per_month")

    def test_indian_grouping_per_annum(self):
        result = parse_salary("4,50,000 P.A.")
        self.assertEqual(result["salary_min"], 450_000)
        self.assertEqual(result["salary_period"], "per_annum")

    def test_bare_large_amount_reads_per_annum(self):
        self.assertEqual(parse_salary("450000")["salary_period"], "per_annum")

    def test_bare_small_amount_reads_per_month(self):
        self.assertEqual(parse_salary("30,000")["salary_period"], "per_month")

    def test_swapped_range_is_ordered(self):
        result = parse_salary("5 - 3 LPA")
        self.assertEqual(result["salary_min"], 300_000)
        self.assertEqual(result["salary_max"], 500_000)

    def test_foreign_currency_keeps_raw_only(self):
        result = parse_salary("AED 5,000 per month")
        self.assertIn("salary_raw", result)
        self.assertNotIn("salary_min", result)


class TestParseExperience(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_experience("0-7 Years (varies by role)"), (0, 7))

    def test_fresher_only(self):
        self.assertEqual(parse_experience("Fresher Only"), (0, 0))

    def test_freshers_to_n_years(self):
        # parenthetical year "(2026 pass-outs)" must not be read as experience
        self.assertEqual(
            parse_experience("Freshers (2026 pass-outs) to 8 Years"), (0, 8))

    def test_min_years(self):
        self.assertEqual(parse_experience("Min 3 years"), (3, ""))

    def test_plus_years(self):
        self.assertEqual(parse_experience("5+ years"), (5, ""))

    def test_empty(self):
        self.assertEqual(parse_experience(""), ("", ""))

    def test_unparseable(self):
        self.assertEqual(parse_experience("As per role"), ("", ""))


class TestApplyClassification(unittest.TestCase):
    """Wiring tests for the shared two-level classifier.

    The engine itself is covered by _shared/test_classification.py; these
    only assert that this scraper feeds it the right signals and stamps
    the verdict onto the rich row.
    """

    @staticmethod
    def _row(title, site_categories="", description="", needs_review=False):
        return {"title": title, "site_categories": site_categories,
                "description": description, "needs_review": needs_review}

    def test_in_scope_role_is_kept_and_labelled(self):
        row = self._row("Senior Medical Writer - Regulatory Documents", "jobs")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Writer")
        self.assertEqual(row["role_family"], "Medical Writer")

    def test_public_health_role(self):
        row = self._row("Epidemiologist - District Surveillance Unit", "jobs")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Epidemiology")

    def test_bedside_title_is_dropped(self):
        row = self._row("Staff Nurse Openings in Mumbai", "jobs")
        self.assertFalse(apply_classification(row))

    def test_search_term_does_not_rescue_an_out_of_scope_post(self):
        # SEARCH_TERMS is crawl-side only: a production walk-in surfaced by
        # the "clinical research" full-text query is still dropped.
        row = self._row(
            "Walk-in Interview: Production, QC, ADL & R&D Jobs at Ami Lifesciences",
            "jobs; production-jobs; qc-jobs",
            "Clinical research department is not involved.")
        self.assertFalse(apply_classification(row))

    def test_local_needs_review_flag_survives(self):
        row = self._row("Clinical Data Manager", "jobs", needs_review=True)
        self.assertTrue(apply_classification(row))
        self.assertTrue(row["needs_review"])


class TestSearchTerms(unittest.TestCase):
    def test_terms_are_lowercase_and_unique(self):
        self.assertEqual(len(SEARCH_TERMS), len(set(SEARCH_TERMS)))
        for term in SEARCH_TERMS:
            self.assertEqual(term, term.lower().strip())

    def test_every_role_family_has_a_query(self):
        joined = " | ".join(SEARCH_TERMS)
        for needle in ("clinical data management", "clinical research",
                       "medical writ", "trial master file", "medical coding",
                       "pharmacovigilance", "regulatory affairs",
                       "medical monitor", "medical science liaison",
                       "health economics", "public health",
                       # added 2026-08-25 — the family had no query at all
                       "medical review"):
            self.assertIn(needle, joined)

    def test_every_public_health_subcategory_has_a_query(self):
        # 2026-08-25 widening: the list used to carry only "public health"
        # and "epidemiology" for all ten PH sub-categories.
        joined = " | ".join(SEARCH_TERMS)
        for needle in ("epidemiolog",                 # Epidemiology
                       "public health",               # PH Program Management
                       "monitoring and evaluation",   # M&E
                       "community health",            # Community Health
                       "health promotion",            # Health Promotion & Ed
                       "tuberculosis",                # Disease Programs
                       "nutrition",                   # PH Nutrition
                       "infection control",           # IPC
                       "health informatics",          # Health Informatics
                       "implementation research"):    # PH Research
            self.assertIn(needle, joined)

    def test_short_queries_are_all_spot_checked(self):
        # WordPress search is a substring LIKE, so a short query can match
        # inside unrelated words. Every query under 5 characters must be one
        # whose top results were checked live (2026-08-25) and found real.
        # Rejected there: heor (matches "theory"), hmis, bare hiv (matches
        # "archive"), and the bare acronyms cra/msl/tmf/cpc/edc/argus.
        # sdtm -> statistical programmers; icsr -> PV case processing.
        VERIFIED_SHORT = {"sdtm", "icsr"}
        for term in SEARCH_TERMS:
            if len(term) < 5:
                self.assertIn(
                    term, VERIFIED_SHORT,
                    "%r is a short query that has not been spot-checked "
                    "against its live top results" % term)


class TestTermCursor(unittest.TestCase):
    """The 2026-08-25 crawl fix: an exhausted term must not end the run."""

    def _walk(self, pages_per_term):
        """Replay the main loop's cursor logic over a fake API."""
        term_idx, page, walked = 0, 1, []
        while term_idx < len(SEARCH_TERMS):
            term = SEARCH_TERMS[term_idx]
            n = pages_per_term.get(term, 1)
            posts = list(range(PAGE_SIZE)) if page < n else []
            page_all_old = page >= n
            walked.append((term, page))
            page += 1
            if not posts:
                term_idx, page = term_idx + 1, 1
                continue
            if page_all_old or len(posts) < PAGE_SIZE:
                term_idx, page = term_idx + 1, 1
                continue
        return walked

    def test_all_terms_are_walked_when_each_stops_early(self):
        # every term's first page is all-old — the old `break` stopped here
        walked = self._walk({})
        self.assertEqual([t for t, _ in walked], list(SEARCH_TERMS))

    def test_deep_term_does_not_starve_the_rest(self):
        deep = {SEARCH_TERMS[0]: 4}
        walked = self._walk(deep)
        self.assertEqual([p for t, p in walked if t == SEARCH_TERMS[0]],
                         [1, 2, 3, 4])
        self.assertEqual(sorted(set(t for t, _ in walked)),
                         sorted(set(SEARCH_TERMS)))


class TestClassifiers(unittest.TestCase):
    def test_newsy_title_is_junk_detection_only(self):
        # feeds needs_review; it is not a category decider
        self.assertTrue(_NEWSY_TITLE_RE.search("Top 10 Pharma Companies in India"))
        self.assertFalse(_NEWSY_TITLE_RE.search("Clinical Data Manager"))

    def test_company_type_defaults_to_pharma(self):
        self.assertEqual(classify_company_type("Macleods Pharmaceuticals", ""),
                         "pharma")

    def test_hospital_company(self):
        self.assertEqual(classify_company_type("Apollo Hospital", ""), "hospital")

    def test_job_type_mapping(self):
        self.assertEqual(classify_job_type("Full-time, On-site"), "full_time")
        self.assertEqual(classify_job_type("Remote / Work From Home"), "remote")
        self.assertEqual(classify_job_type("Hybrid"), "hybrid")
        self.assertEqual(classify_job_type("Part-time"), "part_time")
        self.assertEqual(classify_job_type(""), "full_time")


class TestParseLocation(unittest.TestCase):
    def test_city_india(self):
        self.assertEqual(parse_location("Ahmedabad, India"),
                         ("Ahmedabad", "India", "IN", "+91"))

    def test_city_state(self):
        city, country, code, dial = parse_location(
            "Karakhadi & Ankleshwar, Gujarat")
        self.assertEqual(city, "Karakhadi & Ankleshwar")
        self.assertEqual(code, "IN")

    def test_abroad(self):
        self.assertEqual(parse_location("Dubai, UAE"),
                         ("Dubai", "UAE", "AE", "+971"))

    def test_not_specified(self):
        self.assertEqual(parse_location("Not specified (India-based)"),
                         ("", "India", "IN", "+91"))

    def test_slash_alternatives_first_wins(self):
        city, _, code, _ = parse_location(
            "New Delhi (Interviews) / Various locations across India (Posting)")
        self.assertEqual(city, "New Delhi")
        self.assertEqual(code, "IN")

    def test_empty(self):
        self.assertEqual(parse_location(""), ("", "India", "IN", "+91"))

    # -- FIX 2026-08-27 (PHARMARECRUITER-03): multi-location strings --------

    def test_semicolon_list_with_india_prefixes(self):
        # real stored value from the audit
        city, country, code, _ = parse_location(
            "India – Hyderabad; India – Bengaluru; India – Bengaluru-Remote")
        self.assertEqual(city, "Hyderabad")
        self.assertEqual((country, code), ("India", "IN"))

    def test_semicolon_list_plain(self):
        self.assertEqual(parse_location("Gurugram ; Kochi")[0], "Gurugram")

    def test_india_dash_remote_is_empty_city(self):
        self.assertEqual(parse_location("India – Remote")[0], "")

    def test_remote_alone_is_empty_city(self):
        self.assertEqual(parse_location("Remote"),
                         ("", "India", "IN", "+91"))

    def test_bare_india_is_empty_city(self):
        self.assertEqual(parse_location("India"),
                         ("", "India", "IN", "+91"))


class TestNormalizeCity(unittest.TestCase):
    """normalize_city() alone — the piece the 2026-08-27 store migration
    ran over the city column of the 201 legacy rows."""

    def test_first_city_of_semicolon_list(self):
        self.assertEqual(
            normalize_city("India – Hyderabad; India – Bengaluru; "
                           "India – Bengaluru-Remote"),
            "Hyderabad")

    def test_slash_list(self):
        self.assertEqual(normalize_city("Mumbai / Pune"), "Mumbai")

    def test_india_prefix_stripped(self):
        self.assertEqual(normalize_city("India – Trivandrum"), "Trivandrum")

    def test_india_space_remote(self):
        self.assertEqual(normalize_city("India Remote"), "")

    def test_keeps_real_city(self):
        self.assertEqual(normalize_city("Ahmedabad"), "Ahmedabad")
        # "Indianapolis"-style names must survive the India-prefix strip
        self.assertEqual(normalize_city("Indiana"), "Indiana")

    def test_empty(self):
        self.assertEqual(normalize_city(""), "")


class TestComputeCutoff(unittest.TestCase):
    def test_first_run_uses_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-07-01", "2026-07-20", "bogus"]})
        expected = (date(2026, 7, 20) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_all_bogus_dates_fall_back_to_initial_window(self):
        df = pd.DataFrame({"posted_date": ["bogus", ""]})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
