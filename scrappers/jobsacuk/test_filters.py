#!/usr/bin/env python3
"""Unit tests for the jobsacuk scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real adverts on jobs.ac.uk (Aug 2026): DSS400
(Associate Lecturer in Public Health, Birkbeck), DSO886 (Postdoctoral
Research Associate in Public Health Nutrition, Liverpool), DSK858
(Lecturer / Senior Lecturer in Public Health Management, Chengdu) and
DRH240 (a PhD studentship sharing the board).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    HEALTH_SCOPE_RE,
    INITIAL_WINDOW_DAYS,
    PREFILTER_MARGIN_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_row,
    classify_company_type,
    compute_cutoff,
    is_bare_generic_research_title,
    is_studentship,
    map_job_type,
    parse_card_date,
    parse_detail_table,
    parse_experience_years,
    parse_job_posting,
    parse_salary,
    parse_search_cards,
    parse_sectors,
    parse_type_role,
    prefilter_cutoff,
    resolve_country,
    rich_row_to_club_row,
    should_skip_listing,
)

# Trimmed real search-page shape (sortOrder=1). Note the date-placed stamp
# carries a day and a month but NO year.
SEARCH_HTML = """
<div id="job-listings">
<div class="j-search-result__result ie-border-left" data-advert-id="1085983">
    <div class="j-search-result__text">
        <a href="/job/DSS400/associate-lecturer-in-public-health">
            Associate Lecturer in Public Health
        </a>
        <div class="j-search-result__department">School of Natural Sciences</div>
        <div class="j-search-result__employer">
            <b>Birkbeck, University of London</b>
        </div>
        <div>Location: London</div>
        <div class="j-search-result__info"><strong>Salary: </strong>
            &pound;69.68 per hour. See advert text for details.</div>
        <div><strong>Date Placed: </strong>25 Aug</div>
    </div>
    <div class="j-search-result__date-logos">
        <div class="j-search-result__date">
            <span class="j-search-result__date-span">Closes</span>
            <span class="j-search-result__date-span">20 Sep</span>
        </div>
    </div>
</div>
<div class="j-search-result__result ie-border-left" data-advert-id="1084112">
    <div class="j-search-result__text">
        <a href="/job/DRH240/fully-funded-phd-studentship-recycled-aluminium-alloys">
            Fully Funded PhD Studentship: Recycled Aluminium Alloys
        </a>
        <div class="j-search-result__employer"><b>University of Brunel</b></div>
        <div>Location: Uxbridge</div>
        <div><strong>Date Placed: </strong>04 Jul</div>
    </div>
</div>
</div>
"""

# Real detail-page shape (DSO886, trimmed): a complete JobPosting JSON-LD
# block, the visible advert-details table, and the Advert information block
# whose Type / Role widget is a *disabled button* on job adverts.
DETAIL_HTML = """
<script type="application/ld+json">{"@context": "https://schema.org",
"@type": "JobPosting",
"title": "Postdoctoral Research Associate in Public Health Nutrition",
"description": "<p>We are seeking a Postdoctoral Research Associate to join
the Department of Public Health, Policy &amp; Systems.</p><p>You will lead
quantitative analysis of household survey data on dietary intake and food
insecurity, contribute to focus group discussion fieldwork, and prepare
manuscripts.</p><p>Applicants must have at least 3 years of research
experience and a PhD in nutrition, epidemiology or public health.</p>",
"datePosted": "2026-08-12T00:00:00+00:00",
"validThrough": "2026-09-27T00:00:00+00:00",
"employmentType": "Full Time,Fixed-Term/Contract",
"hiringOrganization": {"@type": "Organization",
"name": "University of Liverpool",
"logo": "https://www.jobs.ac.uk/images/employer-logos/medium/42.gif",
"department": {"@type": "Organization",
"name": "Public Health, Policy and Systems"}},
"jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress",
"addressLocality": "Liverpool", "addressRegion": "England",
"addressCountry": "United Kingdom"}}]}</script>
<div class="j-advert-details__container row-5">
<table>
<tr><th class="j-advert-details__table-header">Location:</th>
    <td>Liverpool</td></tr>
<tr><th class="j-advert-details__table-header">Salary:</th>
    <td>&pound;39,906 to &pound;46,049 per annum</td></tr>
<tr><th class="j-advert-details__table-header">Hours:</th>
    <td>Full Time</td></tr>
<tr><th class="j-advert-details__table-header">Contract Type:</th>
    <td>Fixed-Term/Contract</td></tr>
<tr><th class="j-advert-details__table-header">Placed On:</th>
    <td>12th August 2026</td></tr>
<tr><th class="j-advert-details__table-header">Closes:</th>
    <td>27th September 2026</td></tr>
<tr><th class="j-advert-details__table-header">Job Ref:</th>
    <td>114995</td></tr>
</table>
</div>
<h5>Advert information</h5>
<div>
    <p><b>Type / Role:</b></p>
    <form method="GET" action="/search/academic-or-research">
        <div class="j-form-input ie-11-width">
            <input class="j-form-input__disabled-cat" type="button"
                   value="Academic or Research">
        </div>
    </form>
</div>
<div>
    <p><b>Subject Area(s):</b></p>
    <form method="GET" action="/search/">
        <div class="j-form-input ie-11-width">
            <input name="academicDisciplineFacet[0]" value="health-and-medical" type="hidden">
            <input class="parent-category" type="submit" value="Health &amp; Medical">
        </div>
    </form>
    <form method="GET" action="/search/">
        <div class="j-form-input ie-11-width">
            <input name="academicDisciplineFacet[0]" value="health-and-medical" type="hidden">
            <input name="subDisciplineFacet[0]" value="nutrition" type="hidden">
            <input class="" type="submit" value="Nutrition">
        </div>
    </form>
</div>
<div>
    <p><b>Location(s):</b></p>
    <div class="j-form-input ie-11-width">
        <input class="j-form-input__disabled-cat" type="button" value="Liverpool">
    </div>
</div>
<h5>Job tools</h5>
<form method="GET" action="/search/employer/university-of-liverpool">
    <input type="submit" value="View Employer Profile">
</form>
"""

# A PhD studentship: the Type / Role widget is a live submit here, so only
# the enclosing form's action slug identifies it.
STUDENTSHIP_HTML = """
<h5>Advert information</h5>
<div>
    <p><b>Type / Role:</b></p>
    <form method="GET" action="/search/phds">
        <div class="j-form-input ie-11-width">
            <input type="submit" value="PhDs">
        </div>
    </form>
</div>
<div>
    <p><b>Subject Area(s):</b></p>
    <form method="GET" action="/search/">
        <input name="academicDisciplineFacet[0]" value="physical-and-environmental-sciences" type="hidden">
        <input type="submit" value="Physical &amp; Environmental Sciences">
    </form>
    <form method="GET" action="/search/">
        <input name="academicDisciplineFacet[0]" value="physical-and-environmental-sciences" type="hidden">
        <input name="subDisciplineFacet[0]" value="materials-science" type="hidden">
        <input type="submit" value="Materials Science">
    </form>
</div>
<h5>Job tools</h5>
"""


class TestSearchCards(unittest.TestCase):
    def test_cards_carry_ref_title_employer_and_date(self):
        cards = parse_search_cards(SEARCH_HTML, today=date(2026, 8, 27))
        self.assertEqual([c["job_id"] for c in cards], ["DSS400", "DRH240"])
        self.assertEqual(cards[0]["title"], "Associate Lecturer in Public Health")
        self.assertEqual(cards[0]["company"], "Birkbeck, University of London")
        self.assertEqual(cards[0]["advert_id"], "1085983")
        self.assertEqual(cards[0]["slug"],
                         "associate-lecturer-in-public-health")
        self.assertEqual(cards[0]["card_date"], "2026-08-25")
        self.assertEqual(cards[1]["card_date"], "2026-07-04")

    def test_no_cards_on_an_empty_page(self):
        self.assertEqual(parse_search_cards("<div id='job-listings'></div>"), [])

    def test_cards_carry_the_whole_card_text_for_the_scoping_gate(self):
        cards = parse_search_cards(SEARCH_HTML, today=date(2026, 8, 27))
        # title + department + employer + location + salary, markup-free
        self.assertIn("Associate Lecturer in Public Health", cards[0]["card_text"])
        self.assertIn("School of Natural Sciences", cards[0]["card_text"])
        self.assertIn("Birkbeck, University of London", cards[0]["card_text"])
        self.assertNotIn("<", cards[0]["card_text"])


class TestListingScopingGate(unittest.TestCase):
    """The listing-stage request saver (JOBSACUK-01).

    Skips a detail fetch ONLY for a bare generic research title with no
    health-scope term anywhere on the card; the shared classifier stays the
    final authority for everything fetched.
    """

    def test_bare_generic_titles_are_recognised(self):
        for title in ("Research Associate", "Research Assistant",
                      "Senior Research Fellow", "Postdoctoral Research Associate",
                      "Research Associate (Fixed Term)", "Research Fellows x2",
                      "research assistant 0.5 FTE"):
            self.assertTrue(is_bare_generic_research_title(title), title)

    def test_titles_naming_any_discipline_are_not_bare(self):
        for title in ("Research Associate in Chemistry",
                      "Research Fellow - Quantum Computing",
                      "Postdoctoral Research Associate in Public Health Nutrition",
                      "Lecturer in Medieval History",
                      "Marketing Research Associate"):
            self.assertFalse(is_bare_generic_research_title(title), title)

    def test_health_scope_terms_match_broadly(self):
        for text in ("Faculty of Medicine", "School of Nursing",
                     "pharmacology", "epidemiological modelling",
                     "biomedical sciences", "Life Sciences",
                     "population health", "veterinary school",
                     "biostatistician", "Institute of Neuroscience"):
            self.assertTrue(HEALTH_SCOPE_RE.search(text), text)
        self.assertFalse(HEALTH_SCOPE_RE.search("Department of Physics"))
        self.assertFalse(HEALTH_SCOPE_RE.search("School of Engineering"))

    def test_bare_title_with_no_health_signal_is_skipped(self):
        self.assertTrue(should_skip_listing(
            "Research Associate",
            "Research Associate Department of Physics University of Bristol "
            "Location: Bristol Salary: £38,000 Date Placed: 26 Aug"))

    def test_health_signal_anywhere_on_the_card_keeps_the_fetch(self):
        # A bare title, but the department names a health discipline.
        self.assertTrue(is_bare_generic_research_title("Research Associate"))
        self.assertFalse(should_skip_listing(
            "Research Associate",
            "Research Associate Institute of Population Health "
            "University of Liverpool Location: Liverpool"))
        # ...or the employer does.
        self.assertFalse(should_skip_listing(
            "Research Associate",
            "Research Associate London School of Hygiene & Tropical Medicine"))

    def test_informative_titles_are_never_gated_even_without_health_terms(self):
        # Out of scope, but the classifier decides that, not the gate.
        self.assertFalse(should_skip_listing(
            "Research Associate in Aluminium Alloys",
            "Research Associate in Aluminium Alloys University of Brunel"))
        self.assertFalse(should_skip_listing(
            "Lecturer in Accounting",
            "Lecturer in Accounting Business School"))

    def test_gate_never_skips_the_real_public_health_cards(self):
        for card in parse_search_cards(SEARCH_HTML, today=date(2026, 8, 27)):
            if "Public Health" in card["title"]:
                self.assertFalse(should_skip_listing(card["title"],
                                                     card["card_text"]))

    def test_missing_card_text_gates_on_the_title_alone(self):
        self.assertTrue(should_skip_listing("Research Associate", ""))
        self.assertFalse(should_skip_listing("Research Associate in Nursing", ""))


class TestCardDate(unittest.TestCase):
    """The board stamps cards with a day and month but no year."""

    def test_current_year_when_in_the_past(self):
        self.assertEqual(parse_card_date("25 Aug", today=date(2026, 8, 27)),
                         "2026-08-25")

    def test_rolls_back_a_year_rather_than_post_dating(self):
        # In January, a "20 Dec" card is last December's, not this year's.
        self.assertEqual(parse_card_date("20 Dec", today=date(2027, 1, 5)),
                         "2026-12-20")

    def test_today_and_yesterday_stay_in_the_current_year(self):
        self.assertEqual(parse_card_date("27 Aug", today=date(2026, 8, 27)),
                         "2026-08-27")

    def test_unparseable_dates_are_blank_not_guessed(self):
        # A blank date must never cause a date exclusion.
        self.assertEqual(parse_card_date("", today=date(2026, 8, 27)), "")
        self.assertEqual(parse_card_date("Today", today=date(2026, 8, 27)), "")


class TestDetailParsers(unittest.TestCase):
    def test_json_ld_job_posting_is_found(self):
        posting = parse_job_posting(DETAIL_HTML)
        self.assertEqual(posting["@type"], "JobPosting")
        self.assertEqual(posting["datePosted"], "2026-08-12T00:00:00+00:00")

    def test_no_json_ld_returns_empty(self):
        self.assertEqual(parse_job_posting("<html><body>nope</body></html>"), {})

    def test_advert_details_table(self):
        table = parse_detail_table(DETAIL_HTML)
        self.assertEqual(table["location"], "Liverpool")
        self.assertEqual(table["hours"], "Full Time")
        self.assertEqual(table["contract type"], "Fixed-Term/Contract")
        self.assertEqual(table["job ref"], "114995")

    def test_type_role_read_from_the_form_action_not_the_widget(self):
        # Job adverts render a disabled <input type="button">…
        self.assertEqual(parse_type_role(DETAIL_HTML), "academic-or-research")
        # …studentships render a live submit; the action slug covers both.
        self.assertEqual(parse_type_role(STUDENTSHIP_HTML), "phds")

    def test_sectors_are_type_role_plus_subject_areas(self):
        self.assertEqual(
            parse_sectors(DETAIL_HTML),
            "Academic or Research; Health & Medical; Nutrition")

    def test_sectors_exclude_job_tools_buttons(self):
        # "View Employer Profile" sits outside the Advert information block
        # and its form carries no discipline facet — it must not leak in.
        self.assertNotIn("Employer Profile", parse_sectors(DETAIL_HTML))

    def test_studentship_sectors(self):
        self.assertEqual(
            parse_sectors(STUDENTSHIP_HTML),
            "PhDs; Physical & Environmental Sciences; Materials Science")


class TestSalary(unittest.TestCase):
    def test_annual_range(self):
        raw, lo, hi, cur, per = parse_salary("£39,906 to £46,049 per annum")
        self.assertEqual((lo, hi, cur, per),
                         ("39906", "46049", "GBP", "per_annum"))
        self.assertEqual(raw, "£39,906 to £46,049 per annum")

    def test_hourly_rate_keeps_its_pence(self):
        _, lo, hi, cur, per = parse_salary(
            "£69.68 per hour. See advert text for details.")
        self.assertEqual((lo, hi, cur, per), ("69.68", "69.68", "GBP", "per_hour"))

    def test_grade_numbers_are_not_pay(self):
        _, lo, hi, _, _ = parse_salary("Grade 7, £38,249 per annum")
        self.assertEqual((lo, hi), ("38249", "38249"))

    def test_undisclosed_salary_keeps_the_text_and_invents_nothing(self):
        raw, lo, hi, cur, per = parse_salary(
            "Competitive Package: Attractive salary aligned with experience")
        self.assertTrue(raw.startswith("Competitive"))
        self.assertEqual((lo, hi, cur, per), ("", "", "", ""))

    def test_blank_salary(self):
        self.assertEqual(parse_salary(""), ("", "", "", "", ""))


class TestStudentshipDetection(unittest.TestCase):
    def test_type_role_slug_marks_a_study_place(self):
        self.assertTrue(is_studentship("Doctoral Researcher: Air Quality", "phds"))
        self.assertTrue(is_studentship("Research Project", "masters"))

    def test_title_words_catch_studentships_filed_as_jobs(self):
        self.assertTrue(is_studentship("PhD Studentship in Epidemiology", ""))
        self.assertTrue(is_studentship(
            "Fully Funded Scholarship: Health Inequalities", ""))

    def test_real_jobs_are_not_studentships(self):
        self.assertFalse(is_studentship(
            "Postdoctoral Research Associate in Public Health Nutrition",
            "academic-or-research"))
        self.assertFalse(is_studentship(
            "Lecturer / Senior Lecturer in Public Health Management",
            "academic-or-research"))


class TestJobType(unittest.TestCase):
    def test_part_time(self):
        self.assertEqual(map_job_type("Part Time", "Fixed-Term/Contract",
                                      "London"), "part_time")

    def test_full_time_default(self):
        self.assertEqual(map_job_type("Full Time", "Permanent", "Liverpool"),
                         "full_time")

    def test_remote_and_hybrid_win_over_hours(self):
        self.assertEqual(map_job_type("Full Time", "Permanent",
                                      "Hybrid - Manchester"), "hybrid")
        self.assertEqual(map_job_type("Part Time", "Permanent", "Remote"),
                         "remote")


class TestExperience(unittest.TestCase):
    def test_grounded_extraction(self):
        self.assertEqual(parse_experience_years(
            "Applicants must have at least 3 years of research experience"), "3")
        self.assertEqual(parse_experience_years(
            "Minimum 5 years of postdoctoral experience"), "5")

    def test_nothing_is_inferred(self):
        self.assertEqual(parse_experience_years(
            "You will join a lively department in the heart of the city."), "")


class TestCountry(unittest.TestCase):
    def test_uk(self):
        self.assertEqual(resolve_country("United Kingdom"),
                         ("United Kingdom", "GB", "+44"))

    def test_overseas_advert(self):
        self.assertEqual(resolve_country("China"), ("China", "CN", "+86"))

    def test_unknown_country_keeps_its_name_and_invents_no_code(self):
        self.assertEqual(resolve_country("Ruritania"), ("Ruritania", "", ""))


class TestClassification(unittest.TestCase):
    """The scraper never assigns a category — classify_job does."""

    def _row(self):
        posting = parse_job_posting(DETAIL_HTML)
        return build_row("DSO886",
                         "postdoctoral-research-associate-in-public-health-nutrition",
                         posting, DETAIL_HTML)

    def test_public_health_postdoc_is_kept_and_placed(self):
        row = self._row()
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Public Health Research")
        self.assertEqual(row["role_family"], "Public Health")

    def test_out_of_scope_advert_is_dropped(self):
        row = self._row()
        row["title"] = "Lecturer in Medieval History"
        row["sectors"] = "Academic or Research; Humanities; History"
        row["description"] = ("Teaching on the BA History programme, "
                              "supervising dissertations on monastic charters.")
        self.assertFalse(apply_classification(row))
        self.assertEqual(row["category"], "")

    def test_an_in_scope_studentship_is_force_flagged_for_review(self):
        row = self._row()
        row["is_studentship"] = True
        self.assertTrue(apply_classification(row))
        self.assertTrue(row["needs_review"])


class TestRowBuilding(unittest.TestCase):
    def test_fields_come_from_json_ld_and_the_visible_table(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("DSO886", "postdoctoral-research-associate", posting,
                        DETAIL_HTML)
        self.assertEqual(row["title"],
                         "Postdoctoral Research Associate in Public Health Nutrition")
        self.assertEqual(row["company"], "University of Liverpool")
        self.assertEqual(row["department"], "Public Health, Policy and Systems")
        self.assertEqual(row["city"], "Liverpool")
        self.assertEqual(row["state"], "England")
        self.assertEqual(row["country"], "United Kingdom")
        self.assertEqual(row["posted_date"], "2026-08-12")
        self.assertEqual(row["valid_through"], "2026-09-27")
        self.assertEqual(row["experience_min_years"], "3")
        self.assertEqual(row["salary_currency"], "GBP")
        self.assertEqual(row["job_type"], "full_time")
        self.assertFalse(row["is_studentship"])
        self.assertEqual(
            row["job_url"],
            "https://www.jobs.ac.uk/job/DSO886/postdoctoral-research-associate")
        self.assertNotIn("<p>", row["description"])


class TestClubExport(unittest.TestCase):
    def test_gbp_salaries_stay_out_of_the_club_columns(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("DSO886", "slug", posting, DETAIL_HTML)
        apply_classification(row)
        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_name"], "United Kingdom")
        self.assertEqual(club["country_code"], "GB")
        self.assertEqual(club["country_dial_code"], "+44")
        self.assertEqual(club["city_name"], "Liverpool")
        self.assertEqual(club["category"], "Public Health")
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["max_salary"], "")
        self.assertEqual(club["salary_period"], "")
        self.assertEqual(club["salary_currency"], "")

    def test_city_falls_back_to_region_then_country(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("DSO886", "slug", posting, DETAIL_HTML)
        row["city"] = ""
        self.assertEqual(rich_row_to_club_row(row)["city_name"], "England")
        row["state"] = ""
        self.assertEqual(rich_row_to_club_row(row)["city_name"],
                         "United Kingdom")

    def test_universities_default_to_the_hospital_company_type(self):
        self.assertEqual(classify_company_type("University of Liverpool"),
                         "hospital")
        self.assertEqual(classify_company_type("AstraZeneca Pharma"), "pharma")


class TestCutoff(unittest.TestCase):
    def test_first_run_uses_the_initial_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_later_runs_use_the_watermark_minus_grace(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-20"]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 8, 20) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_since_overrides_everything(self):
        df = pd.DataFrame({"posted_date": ["2026-08-20"]})
        self.assertEqual(compute_cutoff(df, since="2026-01-01"), "2026-01-01")

    def test_card_prefilter_is_looser_than_the_real_cutoff(self):
        self.assertEqual(prefilter_cutoff("2026-08-13"),
                         (date(2026, 8, 13)
                          - timedelta(days=PREFILTER_MARGIN_DAYS)).isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
