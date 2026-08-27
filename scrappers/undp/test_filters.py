#!/usr/bin/env python3
"""Unit tests for the UNDP scraper's parsers, cutoff and export mapping.

Run with plain:  python test_filters.py

Worked examples are trimmed real payloads from the Oracle HCM Candidate
Experience API behind jobs.undp.org (27 Aug 2026): the Jakarta health
governance analyst (36401), the Kathmandu knowledge management analyst
(36470) and the UNCDF home-based advisor (36499).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

import scraper
from scraper import (
    CLUB_COLUMNS,
    COUNTRY_BY_CODE,
    DISALLOWED_HOSTS,
    INITIAL_WINDOW_DAYS,
    MIN_DESCRIPTION_CHARS,
    RICH_COLUMNS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_row,
    classify_company_type,
    compute_cutoff,
    is_rfp_title,
    looks_non_english,
    parse_experience_years,
    parse_flex_fields,
    parse_iso_date,
    parse_location,
    parse_requisition_detail,
    parse_requisition_list,
    rich_row_to_club_row,
    strip_boilerplate,
    strip_html,
)

# The tier preamble and the legal epilogue that wrap every UNDP vacancy.
PREAMBLE = (
    "Tiered Approach In line with the commitment to safeguard capacity and "
    "support personnel already in the Organization, a majority of UNDP "
    "UNCDF/UNV vacancies are advertised using a tiered application process "
    "whereby: Tier 0 : UNDP/UNCDF/UNV IP staff holding permanent (PA) and "
    "fixed-term (FTA) appointments. Tier 1 : Other UNDP/UNCDF/UNV staff. "
    "Tier 2 : staff holding temporary appointments (TA). "
    "Tier 3 or no tier indicated : All other contract types. "
)
BODY = (
    "Project Description Nepal adopted a new Constitution in September 2015. "
    "Scope of Work The Knowledge Management Analyst will support the "
    "programme team with evidence generation and learning products across "
    "the federal and provincial tiers of government. " * 4
)
EPILOGUE = (
    "Equal opportunity UNDP is an equal opportunity and inclusive employer. "
    "Right to select multiple candidates UNDP reserves the right to select "
    "one or more candidates from this vacancy announcement. "
    "Scam alert UNDP does not charge a fee at any stage of its recruitment."
)

LIST_PAYLOAD = {
    "items": [{
        "TotalJobsCount": 107,
        "requisitionList": [
            {"Id": "36401", "Title": "Technical Analyst for Health Governance",
             "PostedDate": "2026-08-20", "PostingEndDate": None,
             "PrimaryLocation": "Jakarta Pusat, Indonesia",
             "PrimaryLocationCountry": "ID"},
            {"Id": "36499", "Title": "Foundations, Philanthropies Advisor",
             "PostedDate": "2026-08-26", "PostingEndDate": None,
             "PrimaryLocation": "Home Based", "PrimaryLocationCountry": "U2"},
        ],
    }]
}

DETAIL_PAYLOAD = {
    "items": [{
        "Id": "36470",
        "Title": "Knowledge Management Analyst",
        "PrimaryLocation": "Kathmandu, Nepal",
        "PrimaryLocationCountry": "NP",
        "ExternalPostedStartDate": "2026-08-26T11:09:37+00:00",
        "ExternalPostedEndDate": "2026-09-10T03:59:00+00:00",
        "ExternalDescriptionStr": "<div><p>" + PREAMBLE + BODY + EPILOGUE + "</p></div>",
        "skills": [],
        "requisitionFlexFields": [
            {"Prompt": "Agency", "Value": "UNDP"},
            {"Prompt": "Grade", "Value": "NPSA-8"},
            {"Prompt": "Practice Area", "Value": "Governance"},
            {"Prompt": "Education & Work Experience",
             "Value": "Master's Degree - 2 year(s) experience OR "
                      "Bachelor's Degree - 4 year(s) experience"},
        ],
    }]
}


class ListParsingTests(unittest.TestCase):
    def test_requisition_list_is_unwrapped_from_the_items_envelope(self):
        reqs, total = parse_requisition_list(LIST_PAYLOAD)
        self.assertEqual(total, 107)
        self.assertEqual([r["Id"] for r in reqs], ["36401", "36499"])

    def test_empty_payload_is_not_an_error(self):
        self.assertEqual(parse_requisition_list({}), ([], 0))
        self.assertEqual(parse_requisition_list({"items": []}), ([], 0))

    def test_detail_is_unwrapped_from_the_items_envelope(self):
        self.assertEqual(parse_requisition_detail(DETAIL_PAYLOAD)["Id"], "36470")
        self.assertEqual(parse_requisition_detail({"items": []}), {})

    def test_flex_fields_become_a_prompt_keyed_dict(self):
        flex = parse_flex_fields(parse_requisition_detail(DETAIL_PAYLOAD))
        self.assertEqual(flex["Agency"], "UNDP")
        self.assertEqual(flex["Practice Area"], "Governance")

    def test_flex_fields_tolerate_a_missing_block(self):
        self.assertEqual(parse_flex_fields({}), {})


class BoilerplateTests(unittest.TestCase):
    def test_tier_preamble_is_cut_at_the_first_content_heading(self):
        out = strip_boilerplate(strip_html(PREAMBLE + BODY))
        self.assertTrue(out.startswith("Project Description"))
        self.assertNotIn("Tier 0", out)

    def test_legal_epilogue_is_cut(self):
        out = strip_boilerplate(strip_html(BODY + EPILOGUE))
        self.assertNotIn("Scam alert", out)
        self.assertNotIn("equal opportunity", out.lower())
        self.assertIn("Knowledge Management Analyst", out)

    def test_both_ends_are_cut_together(self):
        full = strip_html(PREAMBLE + BODY + EPILOGUE)
        out = strip_boilerplate(full)
        self.assertLess(len(out), len(full))
        self.assertTrue(out.startswith("Project Description"))
        self.assertTrue(out.rstrip().endswith("government."))

    def test_a_cut_that_would_gut_the_posting_is_refused(self):
        # Epilogue marker inside a short posting: cutting leaves almost
        # nothing, so the uncut text is kept instead.
        short = "Scope of Work Equal opportunity statement applies."
        self.assertEqual(strip_boilerplate(short), short)

    def test_a_posting_without_boilerplate_is_untouched(self):
        plain = "Scope of Work " + "Support the programme team. " * 30
        self.assertEqual(strip_boilerplate(plain), plain.strip())

    def test_preamble_is_only_cut_when_the_text_opens_with_it(self):
        # "Tiered Approach" mentioned mid-posting is content, not furniture.
        text = "Scope of Work " + ("Design the roll-out. " * 30) + PREAMBLE
        self.assertTrue(strip_boilerplate(text).startswith("Scope of Work"))

    def test_french_preamble_and_epilogue_are_cut(self):
        # UNDP country offices post in FR with the same furniture translated.
        text = ("Approche Tiers Conformément à l'engagement pris de préserver "
                "les capacités et de soutenir le personnel déjà en poste au "
                "sein de l'Organisation, la majorité des postes vacants au "
                "PNUD, au FENU et au programme VNU sont publiés selon un "
                "processus à plusieurs niveaux. Niveau 0, Niveau 1, Niveau 2. "
                + "Contexte Le PNUD travaille avec les pays. " * 20 +
                "Alerte à l'escroquerie Le PNUD ne facture aucune redevance.")
        out = strip_boilerplate(text)
        self.assertTrue(out.startswith("Contexte"))
        self.assertNotIn("Niveau 0", out)
        self.assertNotIn("escroquerie", out)

    def test_spanish_preamble_and_epilogue_are_cut(self):
        text = ("Aproximación por Niveles (Tiered Approach) De acuerdo con el "
                "compromiso de salvaguardar capacidades y apoyar al personal "
                "ya vinculado a la Organización, la mayoría de las vacantes "
                "del PNUD, FNUDC y VNU se anuncian por niveles sucesivos. "
                + "Antecedentes El PNUD trabaja en 170 países. " * 20 +
                "Alerta de estafa El PNUD no cobra ninguna tarifa.")
        out = strip_boilerplate(text)
        self.assertTrue(out.startswith("Antecedentes"))
        self.assertNotIn("Alerta de estafa", out)

    def test_a_mid_sentence_epilogue_cut_falls_back_to_the_sentence_end(self):
        text = ("Antecedentes " + "El proyecto apoya la gestión pública. " * 20 +
                "La oficina del PNUD en Uruguay, comprometida con la "
                "Igualdad de oportunidades, invita a postular.")
        out = strip_boilerplate(text)
        self.assertTrue(out.endswith("."))
        self.assertNotIn("comprometida con la", out)

    def test_a_content_heading_inside_the_preamble_is_not_a_cut_point(self):
        # "Background" appearing in the first few words is prose, not the
        # start of the posting.
        text = ("Tiered Approach Background checks apply to every tier. "
                + "Scope of Work Deliver the programme. " * 30)
        self.assertTrue(strip_boilerplate(text).startswith("Scope of Work"))

    def test_empty_description_is_empty(self):
        self.assertEqual(strip_boilerplate(""), "")
        self.assertEqual(strip_boilerplate(None), "")


class LocationTests(unittest.TestCase):
    def test_trailing_country_segment_is_dropped(self):
        self.assertEqual(parse_location("Kathmandu, Nepal", "NP"),
                         ("Kathmandu", "Nepal"))

    def test_single_segment_stays_the_city(self):
        # Barbados posts the country itself as the duty station.
        self.assertEqual(parse_location("Barbados", "BB"),
                         ("Barbados", "Barbados"))

    def test_home_based_pseudo_code_resolves_to_no_country(self):
        self.assertEqual(parse_location("Home Based", "U2"), ("Home Based", ""))

    def test_multiple_pseudo_code_resolves_to_no_country(self):
        self.assertEqual(parse_location("Multiple", "U1"), ("Multiple", ""))

    def test_unknown_code_is_never_guessed(self):
        self.assertEqual(parse_location("Somewhere, Atlantis", "ZZ"),
                         ("Somewhere, Atlantis", ""))

    def test_multi_part_city_keeps_its_commas(self):
        self.assertEqual(parse_location("Jakarta Pusat, Java, Indonesia", "ID"),
                         ("Jakarta Pusat, Java", "Indonesia"))


class DateTests(unittest.TestCase):
    def test_oracle_timestamp_reduces_to_a_date(self):
        self.assertEqual(parse_iso_date("2026-08-27T04:58:17+00:00"),
                         "2026-08-27")

    def test_plain_date_passes_through(self):
        self.assertEqual(parse_iso_date("2026-08-27"), "2026-08-27")

    def test_missing_date_is_blank(self):
        self.assertEqual(parse_iso_date(None), "")
        self.assertEqual(parse_iso_date("not a date"), "")


class ExperienceTests(unittest.TestCase):
    def test_flex_alternatives_yield_the_lowest_entry_bar(self):
        flex = {"Education & Work Experience":
                "Master's Degree - 2 year(s) experience OR "
                "Bachelor's Degree - 4 year(s) experience"}
        self.assertEqual(parse_experience_years(flex, ""), "2")

    def test_other_criteria_is_used_when_education_field_has_no_years(self):
        flex = {"Education & Work Experience": "Master's  Degree",
                "Other Criteria": "Bachelor's degree - 2 years' experience"}
        self.assertEqual(parse_experience_years(flex, ""), "2")

    def test_degree_only_yields_nothing_rather_than_zero(self):
        self.assertEqual(parse_experience_years({"Education & Work Experience":
                                                 "Master's  Degree"}, ""), "")

    def test_prose_fallback_when_flex_is_silent(self):
        self.assertEqual(
            parse_experience_years({}, "A minimum of 7 years of experience."),
            "7")

    def test_nothing_stated_stays_blank(self):
        self.assertEqual(parse_experience_years({}, "A great opportunity."), "")

    def test_flex_wins_over_the_prose(self):
        flex = {"Education & Work Experience": "Bachelor's - 3 year(s) experience"}
        self.assertEqual(
            parse_experience_years(flex, "at least 15 years of experience"), "3")


class RfpTests(unittest.TestCase):
    def test_tender_titles_are_flagged(self):
        self.assertTrue(is_rfp_title("RFP for supply of cold chain equipment"))
        self.assertTrue(is_rfp_title("Expression of Interest - survey firms"))

    def test_ordinary_vacancies_are_not(self):
        self.assertFalse(is_rfp_title("Knowledge Management Analyst"))
        self.assertFalse(is_rfp_title("Health Programme Specialist"))


class CutoffTests(unittest.TestCase):
    def test_first_run_uses_the_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_the_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-10", "2026-08-20"]})
        expected = (date(2026, 8, 20) -
                    timedelta(days=WATERMARK_GRACE_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)

    def test_since_overrides_everything(self):
        df = pd.DataFrame({"posted_date": ["2026-08-20"]})
        self.assertEqual(compute_cutoff(df, since="2026-01-01"), "2026-01-01")

    def test_an_empty_store_falls_back_to_the_initial_window(self):
        df = pd.DataFrame({"posted_date": []})
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(df), expected)


class BuildRowTests(unittest.TestCase):
    def setUp(self):
        self.detail = parse_requisition_detail(DETAIL_PAYLOAD)
        self.row = build_row("36470", self.detail, "2026-08-26")

    def test_row_has_exactly_the_fleet_columns(self):
        self.assertEqual(sorted(self.row), sorted(RICH_COLUMNS))

    def test_agency_flex_field_is_the_employer(self):
        self.assertEqual(self.row["company"], "UNDP")

    def test_uncdf_postings_are_not_relabelled_undp(self):
        detail = dict(self.detail,
                      requisitionFlexFields=[{"Prompt": "Agency",
                                              "Value": "UNCDF"}])
        self.assertEqual(build_row("1", detail, "2026-08-26")["company"], "UNCDF")

    def test_practice_area_is_the_sectors_signal(self):
        self.assertEqual(self.row["sectors"], "Governance")

    def test_listing_date_is_preferred_and_detail_is_the_fallback(self):
        self.assertEqual(self.row["posted_date"], "2026-08-26")
        self.assertEqual(build_row("36470", self.detail, "")["posted_date"],
                         "2026-08-26")

    def test_closing_date_comes_from_the_detail(self):
        self.assertEqual(self.row["valid_through"], "2026-09-10")

    def test_description_is_boilerplate_free(self):
        self.assertNotIn("Tier 0", self.row["description"])
        self.assertNotIn("Scam alert", self.row["description"])
        self.assertGreater(len(self.row["description"]), MIN_DESCRIPTION_CHARS)

    def test_job_url_points_at_the_candidate_experience_requisition(self):
        self.assertTrue(self.row["job_url"].endswith("/requisitions/job/36470"))

    def test_experience_comes_from_the_flex_field(self):
        self.assertEqual(self.row["experience_min_years"], "2")


class ClassificationTests(unittest.TestCase):
    def _row(self, title, sectors="", description=""):
        return {"title": title, "sectors": sectors, "description": description,
                "is_rfp": is_rfp_title(title)}

    def test_the_scraper_never_invents_a_category(self):
        row = self._row("Epidemiologist", "Health")
        self.assertTrue(apply_classification(row))
        # whatever the verdict is, it came from the shared classifier
        self.assertTrue(row["category"])
        self.assertTrue(row["role_family"])

    def test_out_of_scope_rows_are_dropped(self):
        row = self._row("Driver", "Management")
        self.assertFalse(apply_classification(row))

    def test_an_in_scope_tender_is_kept_but_forced_into_review(self):
        row = self._row("RFP for Public Health Specialist services", "Health")
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_un_agencies_default_to_the_hospital_club_enum(self):
        self.assertEqual(classify_company_type("UNDP"), "hospital")
        self.assertEqual(classify_company_type("UNCDF"), "hospital")


# The opening of the real stored French row (36247, Chad M&E Associate) and
# an English counterpart of the same register (35276, Guinea-Bissau).
FRENCH_SNIPPET = (
    "Background Le Gouvernement de la République du Tchad, dans sa volonté "
    "de refonder l'action publique, a lancé une réforme ambitieuse pour le "
    "renforcement des capacités nationales dans les domaines de la "
    "planification et du suivi-évaluation. Le projet appuie les structures "
    "nationales pour une meilleure coordination des interventions dans le "
    "pays, avec un accent sur la santé publique et la gouvernance locale.")
ENGLISH_SNIPPET = (
    "Background The Anti-Corruption Project in Guinea-Bissau aims to "
    "strengthen national capacities for transparency and accountability. "
    "The Monitoring and Evaluation Analyst will support evidence "
    "generation, learning products and reporting across the programme "
    "portfolio, working closely with government counterparts.")


class LanguageHeuristicTests(unittest.TestCase):
    """UNDP-01: non-English rows are kept but flagged, never dropped."""

    def test_french_description_is_detected(self):
        self.assertTrue(looks_non_english(FRENCH_SNIPPET))

    def test_spanish_description_is_detected(self):
        self.assertTrue(looks_non_english(
            "Antecedentes El Gobierno, con el apoyo del PNUD, busca "
            "fortalecer las capacidades nacionales para la planificación y "
            "el seguimiento de los programas de salud pública en el país, "
            "con especial atención a los mecanismos de coordinación para "
            "los actores locales y con miras a mejorar los resultados para "
            "la población en el territorio."))

    def test_english_description_is_not(self):
        self.assertFalse(looks_non_english(ENGLISH_SNIPPET))

    def test_only_the_head_of_the_text_is_sniffed(self):
        # An English posting quoting some French far into the text is fine.
        self.assertFalse(looks_non_english(ENGLISH_SNIPPET * 5 +
                                           FRENCH_SNIPPET))

    def test_empty_description_is_not_non_english(self):
        self.assertFalse(looks_non_english(""))
        self.assertFalse(looks_non_english(None))

    def test_an_in_scope_french_row_is_kept_but_forced_into_review(self):
        row = {"title": "Monitoring and Evaluation Associate",
               "sectors": "Health", "description": FRENCH_SNIPPET,
               "is_rfp": False}
        apply_classification(row)
        self.assertTrue(row["needs_review"])

    def test_an_english_row_is_not_flagged_by_the_language_check(self):
        row = {"title": "Epidemiologist", "sectors": "Health",
               "description": "Leads outbreak surveillance and response, "
                              "analysing epidemiological data to guide "
                              "public health action.",
               "is_rfp": False}
        self.assertTrue(apply_classification(row))
        self.assertFalse(row["needs_review"])


class ClubExportTests(unittest.TestCase):
    def _club(self, **over):
        row = {"country": "Nepal", "city": "Kathmandu", "company": "UNDP",
               "title": "Health Programme Analyst", "description":
               "Requires a Master's degree in public health.",
               "category": "Public Health", "sub_category": "",
               "role_family": "Public Health", "job_url": "https://x/job/1",
               "posted_date": "2026-08-26", "experience_min_years": "2"}
        row.update(over)
        return rich_row_to_club_row(row)

    def test_club_row_has_exactly_the_club_columns(self):
        self.assertEqual(sorted(self._club()), sorted(CLUB_COLUMNS))

    def test_country_code_and_dial_code_come_back_from_the_country_name(self):
        club = self._club()
        self.assertEqual(club["country_code"], "NP")
        self.assertEqual(club["country_dial_code"], "+977")

    def test_home_based_postings_export_as_remote(self):
        club = self._club(country="", city="Home Based")
        self.assertEqual(club["job_type"], "remote")
        self.assertEqual(club["country_code"], "")
        self.assertEqual(club["country_dial_code"], "")

    def test_duty_station_postings_export_as_full_time(self):
        self.assertEqual(self._club()["job_type"], "full_time")

    def test_multiple_duty_stations_are_not_remote(self):
        self.assertEqual(self._club(country="", city="Multiple")["job_type"],
                         "full_time")

    def test_salary_is_never_invented(self):
        club = self._club()
        for col in ("min_salary", "max_salary", "salary_period",
                    "salary_currency"):
            self.assertEqual(club[col], "")

    def test_qualification_is_lifted_from_the_description(self):
        self.assertIn("Master", self._club()["qualification"])

    def test_an_unmapped_country_exports_blank_code_not_a_guess(self):
        club = self._club(country="Atlantis")
        self.assertEqual(club["country_name"], "Atlantis")
        self.assertEqual(club["country_code"], "")


class ComplianceTests(unittest.TestCase):
    def test_no_configured_url_touches_the_disallowed_host(self):
        # jobs.undp.org is robots.txt "Disallow: /" for every user agent.
        for url in (scraper.LIST_URL, scraper.DETAIL_URL,
                    scraper.JOB_URL_TEMPLATE, scraper.ROBOTS_URL):
            for host in DISALLOWED_HOSTS:
                self.assertNotIn(host, url)

    def test_every_request_goes_to_the_oracle_candidate_experience_host(self):
        for url in (scraper.LIST_URL, scraper.DETAIL_URL, scraper.ROBOTS_URL):
            self.assertTrue(url.startswith(scraper.CE_HOST))

    def test_country_table_is_iso_alpha_2_keyed(self):
        for code in COUNTRY_BY_CODE:
            self.assertRegex(code, r"^[A-Z]{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
