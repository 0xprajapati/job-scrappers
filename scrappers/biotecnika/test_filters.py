#!/usr/bin/env python3
"""Unit tests for the biotecnika scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py   (no network)

Worked examples are trimmed from real www.biotecnika.org posts (Aug 2026), keeping
the exact markup shapes: the `<li><strong>Label:</strong> value</li>` "Job
Details" bullets and the two-column "Particulars | Details" table.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    EDITORIAL_SLUGS,
    INITIAL_WINDOW_DAYS,
    JOBS_SLUG,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    city_from_description,
    classify_company_type,
    classify_job_type,
    clean_title,
    company_from_title,
    compute_cutoff,
    extract_labeled_fields,
    has_job_fields,
    is_newsy_title,
    parse_deadline,
    parse_experience,
    parse_location,
    parse_salary,
    post_to_rich_row,
    resolve_editorial_ids,
    rich_row_to_club_row,
    trim_company_name,
)

# Real bullet shape (GE Healthcare GET post, trimmed).
BULLET_HTML = """
<p><strong>Job Details:</strong></p>
<ul>
<li><strong>Job Title:</strong> Clinical Data Coordinator</li>
<li><strong>Company:</strong> ICON plc</li>
<li><strong>Location:</strong> Bengaluru, Karnataka, India, 560066</li>
<li><strong>Experience:</strong> 2-5 Years</li>
<li><strong>Qualification:</strong> B.Sc / M.Sc Life Sciences</li>
<li><strong>Job Type:</strong> Hybrid</li>
<li><strong>Salary:</strong> Rs. 67,000/- p.m.</li>
<li><strong>Last Date to Apply:</strong> 14 Sep 2026</li>
</ul>
<p>Support clinical data management activities across trials.</p>
"""

# Real two-column table shape (IISER project position, trimmed).
TABLE_HTML = """
<p><strong>Job Details:</strong></p>
<table><tbody>
<tr><td>Particulars</td><td>Details</td></tr>
<tr><td>Position</td><td>Project Associate-I</td></tr>
<tr><td>Institute</td><td>IISER Bhopal</td></tr>
<tr><td>Essential Qualification</td><td>M.Sc. in Chemistry with 60% marks</td></tr>
<tr><td>Salary</td><td>&#8377;37,000 per month + HRA</td></tr>
<tr><td>Age Limit</td><td>Preferably below 28 years</td></tr>
</tbody></table>
"""

EDITORIAL_POST_HTML = "<p>A round-up of this week's sector news.</p>"


def make_post(post_id, title, content, categories, when="2026-08-26T09:00:00"):
    return {"id": post_id, "date": when, "link": "https://x/y",
            "title": {"rendered": title}, "content": {"rendered": content},
            "categories": categories}


class TestLabelledFields(unittest.TestCase):
    def test_bullets(self):
        f = extract_labeled_fields(BULLET_HTML)
        self.assertEqual(f["position"], "Clinical Data Coordinator")
        self.assertEqual(f["company"], "ICON plc")
        self.assertEqual(f["location"], "Bengaluru, Karnataka, India, 560066")
        self.assertEqual(f["experience"], "2-5 Years")
        self.assertEqual(f["job_type"], "Hybrid")
        self.assertEqual(f["salary"], "Rs. 67,000/- p.m.")
        self.assertEqual(f["deadline"], "14 Sep 2026")

    def test_two_column_table(self):
        f = extract_labeled_fields(TABLE_HTML)
        self.assertEqual(f["position"], "Project Associate-I")
        self.assertEqual(f["company"], "IISER Bhopal")     # "Institute" label
        self.assertIn("M.Sc. in Chemistry", f["qualification"])
        self.assertEqual(f["salary"], "₹37,000 per month + HRA")

    def test_age_limit_is_not_experience(self):
        # "Age Limit: Preferably below 28 years" must not become 28 years'
        # experience — the label map never routes it to `experience`.
        self.assertNotIn("experience", extract_labeled_fields(TABLE_HTML))

    def test_has_job_fields(self):
        self.assertTrue(has_job_fields(BULLET_HTML))
        self.assertTrue(has_job_fields(TABLE_HTML))
        self.assertFalse(has_job_fields(EDITORIAL_POST_HTML))


class TestCleanTitle(unittest.TestCase):
    """BIOTECNIKA-01: the stored/exported title loses the SEO wrapper.

    All worked examples are real stored post titles (Aug 2026)."""

    def test_pipe_cut_keeps_the_left_part(self):
        self.assertEqual(
            clean_title("Research Associate Job at IISER | "
                        "Earn Upto ₹71,920/Month | Apply Now"),
            "Research Associate Job at IISER")
        self.assertEqual(
            clean_title("Entry level Hybrid Clinical Data Coordinator Job "
                        "at ICON | Apply Online Now"),
            "Entry level Hybrid Clinical Data Coordinator Job at ICON")
        self.assertEqual(
            clean_title("CDM Jobs in Bengaluru at ICON | Life Sciences Eligible"),
            "CDM Jobs in Bengaluru at ICON")

    def test_short_left_part_is_not_kept(self):
        # a left part under ~10 chars would be a stub — use the next segment
        self.assertEqual(clean_title("QC | Officer Vacancy at Cipla"),
                         "Officer Vacancy at Cipla")

    def test_trailing_dash_marketing_clauses(self):
        self.assertEqual(
            clean_title("ExxonMobil Careers: Biology Jobs in Regulatory "
                        "Affairs – Apply Now"),
            "ExxonMobil Careers: Biology Jobs in Regulatory Affairs")
        self.assertEqual(
            clean_title("Research Associate Jobs at BRIC-NIPGR – Apply Online Now"),
            "Research Associate Jobs at BRIC-NIPGR")
        self.assertEqual(
            clean_title("Remote Clinical Research Jobs at Thermo Fisher – "
                        "Don’t Miss This Opportunity!!"),
            "Remote Clinical Research Jobs at Thermo Fisher")

    def test_salary_bait_segment_is_skipped(self):
        # the marketing left part yields to the real title in segment two
        self.assertEqual(
            clean_title("Earn ₹54,200/Month at IIT | Research Associate-I Jobs 2026"),
            "Research Associate-I Jobs")

    def test_leading_salary_clause_is_stripped(self):
        self.assertEqual(
            clean_title("Earn Upto Rs. 1,01,400/Month with Research Scientist "
                        "Job at ACTREC | Applications Open Now!"),
            "Research Scientist Job at ACTREC")

    def test_trailing_salary_clause_is_stripped(self):
        self.assertEqual(
            clean_title("Research Associate Jobs at IISER with Rs. 58,000/- "
                        "Per Month Pay | Life Sciences Can Apply"),
            "Research Associate Jobs at IISER")

    def test_trailing_year_is_stripped(self):
        self.assertEqual(
            clean_title("Fortrea Careers 2026 | Clinical Data Jobs for Life Sciences"),
            "Fortrea Careers")
        self.assertEqual(
            clean_title("Lupin Research Associate Jobs 2026 | Biochemistry & "
                        "Biotechnology Careers"),
            "Lupin Research Associate Jobs")

    def test_mid_title_year_survives(self):
        # only a TRAILING year is marketing decoration
        self.assertEqual(
            clean_title("ICAR Careers 2026: Research Associate Jobs and "
                        "Fellowship Vacancy"),
            "ICAR Careers 2026: Research Associate Jobs and Fellowship Vacancy")

    def test_pure_marketing_head_and_tail(self):
        self.assertEqual(
            clean_title("Novo Nordisk is Hiring!! Apply Now for Life Sciences Jobs"),
            "Novo Nordisk")

    def test_junk_first_segment_yields_to_the_real_one(self):
        self.assertEqual(
            clean_title("Hybrid Opportunity | IQVIA Hiring Medical Writer | "
                        "Life Sciences Jobs"),
            "IQVIA Hiring Medical Writer")

    def test_whitespace_collapse(self):
        self.assertEqual(clean_title("Clinical   Research\t Jobs at   Amgen"),
                         "Clinical Research Jobs at Amgen")

    def test_clean_titles_pass_through_and_are_idempotent(self):
        for t in ("Clinical Data Coordinator Job at ICON plc",
                  "Regulatory Affairs Jobs at Biocon",
                  "Thermo Fisher Careers: CDM Jobs in Bangalore"):
            self.assertEqual(clean_title(t), t)
            self.assertEqual(clean_title(clean_title(t)), clean_title(t))

    def test_empty_title(self):
        self.assertEqual(clean_title(""), "")


class TestCompanyFromTitle(unittest.TestCase):
    def test_jobs_at_pattern(self):
        self.assertEqual(
            company_from_title("Research Associate Jobs at Lupin | Jobs in Pune"),
            "Lupin")

    def test_job_at_pattern(self):
        self.assertEqual(
            company_from_title("Entry level Clinical Data Coordinator Job at ICON"),
            "ICON")

    def test_hiring_pattern(self):
        self.assertEqual(
            company_from_title("Sun Pharma Hiring Chemistry Graduates | CRA Role"),
            "Sun Pharma")

    def test_city_between_jobs_and_company(self):
        # "in <City> at <Company>" — the rightmost `at` is the employer
        self.assertEqual(
            company_from_title("CDM Jobs in Bengaluru at ICON | Life Sciences Eligible"),
            "ICON")

    def test_city_after_company(self):
        self.assertEqual(
            company_from_title("Clinical Trial Jobs at ICON in Bengaluru | Eligible"),
            "ICON")
        self.assertEqual(
            company_from_title("Clinical Research Associate Jobs at Dr. Reddy's "
                               "Laboratories in Hyderabad"),
            "Dr. Reddy's Laboratories")

    def test_leading_company_beats_a_trailing_city(self):
        # "Lupin Hiring ... at Pune" — the trailing `at` is a city, so the
        # leading "<Company> Hiring" shape must win
        self.assertEqual(
            company_from_title("Lupin Hiring Research Associates at Pune"), "Lupin")

    def test_salary_tail_is_not_a_company(self):
        self.assertEqual(
            company_from_title("Research Associate Jobs at IISER with Rs. 58,000/- "
                               "Per Month Pay"),
            "IISER")

    def test_unparseable_returns_blank(self):
        # never invents a company — blank instead, which flags needs_review
        self.assertEqual(company_from_title("Walk-In Interview This Friday"), "")
        self.assertEqual(company_from_title(""), "")
        # an amount is never an employer
        self.assertEqual(company_from_title("Earn up to Rs 75,400 at 2026 rates"), "")


class TestLocation(unittest.TestCase):
    def test_city_state_country_with_pincode(self):
        self.assertEqual(parse_location("Bengaluru, Karnataka, India, 560066"),
                         ("Bengaluru", "Karnataka", "India", "IN", "+91"))

    def test_bare_city_defaults_to_india(self):
        self.assertEqual(parse_location("Hyderabad"),
                         ("Hyderabad", "", "India", "IN", "+91"))

    def test_foreign_country(self):
        self.assertEqual(parse_location("Basel, Switzerland"),
                         ("Basel", "", "Switzerland", "CH", "+41"))

    def test_state_only_is_not_a_city(self):
        self.assertEqual(parse_location("Kerala"),
                         ("", "Kerala", "India", "IN", "+91"))

    def test_placeholder_locations(self):
        for raw in ("Pan India", "Multiple Locations", "Not specified", ""):
            self.assertEqual(parse_location(raw)[:2], ("", ""))


class TestCityFromDescription(unittest.TestCase):
    """BIOTECNIKA-02: body-text fallback when no Location bullet exists.

    Worked examples are flattened body text from real stored posts."""

    def test_labelled_city_before_the_next_label(self):
        self.assertEqual(
            city_from_description(
                "Job Details: Company: ICON plc Location: Bangalore, Bengaluru "
                "Reference Number: JR154827 Department: Clinical Data"),
            ("Bangalore", ""))
        self.assertEqual(
            city_from_description(
                "Job Title: Biostatistician I Locations: Bangalore "
                "Time Type: Full time"),
            ("Bangalore", ""))

    def test_bare_india_stays_country_only(self):
        self.assertEqual(
            city_from_description(
                "Position: Pharmacovigilance Specialist Company: ProPharma "
                "Location: India Job Type: Full-time"),
            ("", ""))

    def test_remote_india_stays_empty(self):
        self.assertEqual(
            city_from_description(
                "Job Type: Full-Time Work Mode: Remote Location: India "
                "Job ID: R0000043704"),
            ("", ""))

    def test_country_first_ordering(self):
        # "Primary Location: India, Hyderabad" — the city is the useful part
        self.assertEqual(
            city_from_description(
                "Job Title: Medical Writer I Primary Location: India, Hyderabad "
                "Additional Locations: India, Bengaluru; India, Remote"),
            ("Hyderabad", ""))

    def test_dash_separator_and_about_boilerplate(self):
        self.assertEqual(
            city_from_description(
                "Department – Data Standards and Integration Location – "
                "Bangalore About the Company Novo Nordisk is a global company"),
            ("Bangalore", ""))

    def test_full_street_address(self):
        self.assertEqual(
            city_from_description(
                "Job Schedule: Full time Locations: Ground Floor, Unit 1, "
                "Block E, Helios Business Park., Bangalore, 560103, IN"),
            ("Bangalore", ""))

    def test_city_state_pair_outside_the_whitelist(self):
        self.assertEqual(
            city_from_description(
                "Location: Bavla, Gujarat, IN About the Company: Dishman "
                "Carbogen is a globally recognized company"),
            ("Bavla", "Gujarat"))

    def test_delhi_files_as_a_state_like_parse_location(self):
        self.assertEqual(
            city_from_description("Work Location: New Delhi Job Type: Full Time"),
            ("", "New Delhi"))

    def test_unlabelled_city_mentions_are_ignored(self):
        # no "Location:" label — a passing mention is never extracted
        self.assertEqual(
            city_from_description(
                "Parexel operates in various locations, including Hyderabad, "
                "Bengaluru, and remote settings across the country."),
            ("", ""))
        self.assertEqual(
            city_from_description(
                "Fortrea Careers is hiring Safety Writers for its Mumbai and "
                "Pune locations."),
            ("", ""))

    def test_empty_description(self):
        self.assertEqual(city_from_description(""), ("", ""))


class TestTrimCompanyName(unittest.TestCase):
    """BIOTECNIKA-03: club company_name over 60 chars sheds trailing
    parenthetical qualifiers."""

    def test_long_name_loses_the_trailing_parenthetical(self):
        self.assertEqual(
            trim_company_name("A1 Facility and Property Managers Pvt. Ltd. "
                              "(Outsourced Contractor for Manpower Services)"),
            "A1 Facility and Property Managers Pvt. Ltd.")

    def test_short_name_keeps_its_parenthetical(self):
        self.assertEqual(trim_company_name("ICON plc (Ireland)"),
                         "ICON plc (Ireland)")

    def test_non_trailing_parenthetical_is_never_cut(self):
        # the parenthetical is mid-name — trimming it would corrupt the name
        name = "Indian Institute of Science Education and Research (IISER) Pune"
        self.assertEqual(trim_company_name(name), name)

    def test_empty(self):
        self.assertEqual(trim_company_name(""), "")


class TestExperience(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_experience("2-5 Years"), (2, 5))

    def test_fresher(self):
        self.assertEqual(parse_experience("Fresher Only"), (0, 0))

    def test_minimum(self):
        self.assertEqual(parse_experience("Minimum 3 years"), (3, ""))

    def test_parenthetical_year_is_not_experience(self):
        self.assertEqual(parse_experience("Freshers (2026 pass-outs) to 8 Years"),
                         (0, 8))

    def test_unstated(self):
        self.assertEqual(parse_experience(""), ("", ""))
        self.assertEqual(parse_experience("As per norms"), ("", ""))


class TestSalary(unittest.TestCase):
    def test_monthly_stipend(self):
        s = parse_salary("Rs. 67,000/- p.m.")
        self.assertEqual((s["salary_min"], s["salary_max"]), (67000, 67000))
        self.assertEqual(s["salary_period"], "per_month")
        self.assertEqual(s["salary_currency"], "INR")

    def test_lpa_range(self):
        s = parse_salary("₹3.5 - 5 LPA")
        self.assertEqual((s["salary_min"], s["salary_max"]), (350000, 500000))
        self.assertEqual(s["salary_period"], "per_annum")

    def test_no_amount_keeps_raw_only(self):
        s = parse_salary("As per institute norms")
        self.assertEqual(s, {"salary_raw": "As per institute norms"})

    def test_empty_is_not_disclosed(self):
        self.assertEqual(parse_salary(""), {})

    def test_unconverted_foreign_currency_keeps_raw_only(self):
        self.assertEqual(set(parse_salary("EUR 45,000 per year")), {"salary_raw"})


class TestDeadline(unittest.TestCase):
    def test_named_month(self):
        self.assertEqual(parse_deadline("14 Sep 2026"), "2026-09-14")

    def test_numeric_and_iso(self):
        self.assertEqual(parse_deadline("04/09/2026"), "2026-09-04")
        self.assertEqual(parse_deadline("2026-09-04"), "2026-09-04")

    def test_unparseable(self):
        self.assertEqual(parse_deadline("Until filled"), "")
        self.assertEqual(parse_deadline(""), "")


class TestNewsDetection(unittest.TestCase):
    def test_article_titles_flagged(self):
        for t in ("Top 10 Pharma Companies in India",
                  "How To Crack a CSIR NET Interview",
                  "Chemistry Salary in India: A Complete Guide",
                  "CSIR NET Exam Result Declared"):
            self.assertTrue(is_newsy_title(t), t)

    def test_real_vacancies_not_flagged(self):
        for t in ("Pharmacovigilance Job at ProPharma | Apply Online Now",
                  "Clinical Research Jobs at LabCorp | Apply Now"):
            self.assertFalse(is_newsy_title(t), t)


class TestClassifier(unittest.TestCase):
    """The scraper never assigns a category — it copies classify_job's verdict."""

    def test_in_scope_row_gets_the_shared_verdict(self):
        row = {"title": "Pharmacovigilance Associate",
               "site_categories": "jobs; pharma-jobs",
               "description": "Process ICSRs and author safety narratives "
                              "for marketed products; signal detection support.",
               "needs_review": False}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Pharmacovigilance")
        self.assertTrue(row["role_family"])

    def test_out_of_scope_row_is_dropped(self):
        row = {"title": "Production Chemist at a Paints Factory",
               "site_categories": "jobs; chemistry-jobs",
               "description": "Operate the reactor, monitor batch yields "
                              "and maintain plant production records.",
               "needs_review": False}
        self.assertFalse(apply_classification(row))

    def test_local_needs_review_flag_survives_classification(self):
        row = {"title": "Clinical Data Manager", "site_categories": "jobs",
               "description": "Clinical data management, EDC and CDISC SDTM "
                              "dataset preparation for oncology trials.",
               "needs_review": True}          # e.g. company could not be parsed
        self.assertTrue(apply_classification(row))
        self.assertTrue(row["needs_review"])


class TestRowBuilding(unittest.TestCase):
    CATEGORY_MAP = {1: JOBS_SLUG, 2: "featured", 3: "clinical-research"}

    def test_rich_row_from_a_bullet_post(self):
        post = make_post(4242, "Clinical Data Coordinator Job at ICON",
                         BULLET_HTML, [1, 2, 3])
        row = post_to_rich_row(post, self.CATEGORY_MAP)
        self.assertEqual(row["job_id"], "4242")
        self.assertEqual(row["company"], "ICON plc")
        self.assertEqual(row["city"], "Bengaluru")
        self.assertEqual(row["state"], "Karnataka")
        self.assertEqual(row["posted_date"], "2026-08-26")
        self.assertEqual(row["valid_through"], "2026-09-14")
        self.assertEqual(row["job_type"], "hybrid")
        self.assertEqual(row["salary_min"], 67000)
        self.assertTrue(row["in_jobs_category"])
        self.assertFalse(row["needs_review"])
        # the site's curated signal is kept raw and never decides anything
        self.assertEqual(row["site_categories"],
                         "; ".join([JOBS_SLUG, "featured", "clinical-research"]))
        # taxonomy fields stay blank until apply_classification stamps them
        self.assertEqual(row["category"], "")

    def test_company_falls_back_to_the_title(self):
        post = make_post(7, "Research Associate Jobs at Lupin | Jobs in Pune",
                         TABLE_HTML.replace("<td>Institute</td><td>IISER Bhopal</td>", ""),
                         [1])
        self.assertEqual(post_to_rich_row(post, self.CATEGORY_MAP)["company"],
                         "Lupin")

    def test_title_is_cleaned_and_the_raw_headline_is_kept(self):
        post = make_post(21, "Research Associate Job at IISER | "
                             "Earn Upto ₹71,920/Month | Apply Now",
                         BULLET_HTML, [1])
        row = post_to_rich_row(post, self.CATEGORY_MAP)
        self.assertEqual(row["title"], "Research Associate Job at IISER")
        self.assertEqual(row["title_raw"],
                         "Research Associate Job at IISER | "
                         "Earn Upto ₹71,920/Month | Apply Now")

    def test_company_and_job_type_parse_the_raw_headline(self):
        # the cleaned title ("Research Associate-I Jobs") has neither the
        # "at IIT" clause nor the Hybrid marker — both live in the raw one
        html = BULLET_HTML.replace(
            "<li><strong>Company:</strong> ICON plc</li>", "").replace(
            "<li><strong>Job Type:</strong> Hybrid</li>", "")
        post = make_post(22, "Earn ₹54,200/Month at IIT | "
                             "Research Associate-I Jobs 2026", html, [1])
        row = post_to_rich_row(post, self.CATEGORY_MAP)
        self.assertEqual(row["title"], "Research Associate-I Jobs")
        self.assertEqual(row["company"], "IIT")

        post = make_post(23, "Hybrid Opportunity | IQVIA Hiring Medical Writer",
                         BULLET_HTML.replace(
                             "<li><strong>Job Type:</strong> Hybrid</li>", ""),
                         [1])
        self.assertEqual(post_to_rich_row(post, self.CATEGORY_MAP)["job_type"],
                         "hybrid")

    def test_city_falls_back_to_the_body_when_no_location_bullet(self):
        html = BULLET_HTML.replace(
            "<li><strong>Location:</strong> Bengaluru, Karnataka, India, "
            "560066</li>", "") + (
            "<p>Job Title: Clinical Data Coordinator Location: Bangalore, "
            "Bengaluru Reference Number: JR154827</p>")
        post = make_post(24, "Clinical Data Coordinator Job at ICON", html, [1])
        row = post_to_rich_row(post, self.CATEGORY_MAP)
        self.assertEqual(row["city"], "Bangalore")

    def test_labelled_location_bullet_beats_the_body(self):
        html = BULLET_HTML + "<p>Location: Hyderabad Job ID: 1</p>"
        post = make_post(25, "Clinical Data Coordinator Job at ICON", html, [1])
        self.assertEqual(post_to_rich_row(post, self.CATEGORY_MAP)["city"],
                         "Bengaluru")

    def test_post_outside_jobs_category_without_job_fields_is_flagged(self):
        post = make_post(9, "Sector Weekly Round-Up", EDITORIAL_POST_HTML, [2])
        row = post_to_rich_row(post, self.CATEGORY_MAP)
        self.assertFalse(row["in_jobs_category"])
        self.assertTrue(row["needs_review"])

    def test_vacancy_outside_jobs_category_is_not_flagged_for_that_alone(self):
        # JRF/fellowship vacancies live outside `jobs` but publish real
        # fields — they are exactly what the wide crawl exists to catch.
        post = make_post(11, "JRF Vacancy at IISER | Apply Now", BULLET_HTML, [2])
        row = post_to_rich_row(post, self.CATEGORY_MAP)
        self.assertFalse(row["in_jobs_category"])
        self.assertFalse(row["needs_review"])


class TestClubExport(unittest.TestCase):
    def test_club_row_carries_the_taxonomy_verdict(self):
        post = make_post(1, "Clinical Data Coordinator Job at ICON",
                         BULLET_HTML, [1])
        row = post_to_rich_row(post, {1: JOBS_SLUG})
        apply_classification(row)
        club = rich_row_to_club_row(row)
        self.assertEqual(club["category"], row["category"])
        self.assertEqual(club["sub_category"], row["sub_category"])
        self.assertEqual(club["role_family"], row["role_family"])
        self.assertEqual(club["min_salary"], "67000")
        self.assertEqual(club["salary_currency"], "INR")
        self.assertEqual(club["posted_at"], "2026-08-26")

    def test_club_title_is_the_cleaned_form(self):
        post = make_post(3, "Pharmacovigilance Job at ProPharma | Apply Online Now",
                         BULLET_HTML, [1])
        club = rich_row_to_club_row(post_to_rich_row(post, {1: JOBS_SLUG}))
        self.assertEqual(club["title"], "Pharmacovigilance Job at ProPharma")

    def test_club_title_recleans_a_stored_seo_headline(self):
        # store rows written before the title fix still export clean
        club = rich_row_to_club_row(
            {"title": "Clinical Research Job at IQVIA | Apply Now"})
        self.assertEqual(club["title"], "Clinical Research Job at IQVIA")

    def test_club_company_name_is_trimmed_but_rich_company_is_not(self):
        long_name = ("A1 Facility and Property Managers Pvt. Ltd. "
                     "(Outsourced Contractor for Manpower Services)")
        club = rich_row_to_club_row({"title": "Lab Technician Job", "company": long_name})
        self.assertEqual(club["company_name"],
                         "A1 Facility and Property Managers Pvt. Ltd.")

    def test_undisclosed_salary_leaves_club_columns_blank(self):
        post = make_post(2, "Clinical Research Associate Job at IQVIA",
                         BULLET_HTML.replace(
                             "<li><strong>Salary:</strong> Rs. 67,000/- p.m.</li>", ""),
                         [1])
        club = rich_row_to_club_row(post_to_rich_row(post, {1: JOBS_SLUG}))
        for col in ("min_salary", "max_salary", "salary_period", "salary_currency"):
            self.assertEqual(club[col], "")


class TestCompanyType(unittest.TestCase):
    def test_enum_values_only(self):
        for company, title in (("ICON plc", "CRA job"), ("Apollo Hospitals", "x"),
                               ("", ""), ("IISER Bhopal", "Project Associate")):
            self.assertIn(classify_company_type(company, title),
                          ("hospital", "pharma"))

    def test_hospital_beats_default(self):
        self.assertEqual(classify_company_type("Apollo Hospitals", ""), "hospital")

    def test_clinical_in_the_title_does_not_make_a_hospital(self):
        # regression: bare `clinic` matched "Clinical", typing every CRO as
        # a hospital. The title never decides when a company is known.
        for cro in ("ICON plc", "IQVIA", "Medpace", "Velocity Clinical Research"):
            self.assertEqual(
                classify_company_type(cro, "Clinical Data Coordinator Job"),
                "pharma", cro)

    def test_title_used_only_when_company_is_unknown(self):
        self.assertEqual(classify_company_type("", "Nurse at a district hospital"),
                         "hospital")


class TestJobType(unittest.TestCase):
    def test_values(self):
        self.assertEqual(classify_job_type("Hybrid", ""), "hybrid")
        self.assertEqual(classify_job_type("", "Remote Clinical Research Jobs"),
                         "remote")
        self.assertEqual(classify_job_type("", "Graduate Engineer Trainee"),
                         "internship")
        self.assertEqual(classify_job_type("", "Clinical Data Manager"), "full_time")


class TestEditorialResolution(unittest.TestCase):
    def test_slugs_resolve_to_ids(self):
        cat_map = {100 + i: s for i, s in enumerate(EDITORIAL_SLUGS)}
        cat_map[1] = JOBS_SLUG
        self.assertEqual(sorted(resolve_editorial_ids(cat_map)),
                         sorted(range(100, 100 + len(EDITORIAL_SLUGS))))

    def test_a_missing_editorial_slug_aborts(self):
        # a renamed editorial term must never silently widen the crawl back
        # into the news feed
        with self.assertRaises(SystemExit):
            resolve_editorial_ids({1: JOBS_SLUG})


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_the_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_the_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-20"]})
        self.assertEqual(compute_cutoff(df), "2026-08-18")

    def test_since_overrides(self):
        df = pd.DataFrame({"posted_date": ["2026-08-20"]})
        self.assertEqual(compute_cutoff(df, since="2026-01-01"), "2026-01-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
