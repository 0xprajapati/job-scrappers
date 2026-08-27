#!/usr/bin/env python3
"""Unit tests for the nhsjobs scraper's parsers, veto gate and cutoff logic.

Run with plain:  python test_filters.py
Worked examples are trimmed from real adverts on jobs.nhs.uk (Aug 2026):
E9847-26-0265 (Public Health Practitioner, Spectrum Community Health CIC,
Band 7) and C9188-26-0738 (Public Health Midwife, University Hospital
Southampton) — both taken verbatim from the live markup, including the
nested-<p> and mobile/desktop-duplicate quirks the parsers exist to absorb.
"""

import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# CLUB_COLUMNS is re-exported by scraper, which puts _shared on sys.path.
from scraper import (
    CLUB_COLUMNS,
    INITIAL_WINDOW_DAYS,
    STAFF_GROUPS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_row,
    card_only_row,
    classify_company_type,
    compute_cutoff,
    listing_veto,
    map_job_type,
    parse_description,
    parse_detail,
    parse_human_date,
    parse_result_count,
    parse_salary,
    parse_search_cards,
    rich_row_to_club_row,
    search_url,
)

# Real results-page shape: two cards, nested <li> inside each card (which is
# why cards are split on the panel tag, not matched with .*?</li>).
SEARCH_HTML = """
<h1 class="nhsuk-heading-l">9,307 jobs found</h1>
<ul class="nhsuk-list search-results">
  <li class="nhsuk-list-panel search-result nhsuk-u-padding-3" data-test="search-result">
    <div class="nhsuk-grid-row"><div class="nhsuk-grid-column-two-thirds">
      <h2 class="nhsuk-heading-m" id="job-title-1">
        <a href="/candidate/jobadvert/E9847-26-0265?keyword=&amp;language=en"
           data-test="search-result-job-title">
          Public Health Practitioner
        </a>
      </h2>
    </div></div>
    <div class="nhsuk-u-margin-bottom-4" data-test="search-result-location">
      <h3 class="nhsuk-u-font-weight-bold">
        Spectrum Community Health CIC
        <div class="location-font-size">
          Wakefield WF1 5RH
        </div>
    </div>
    <div class="nhsuk-grid-row"><ul class="ad-job-info nhsuk-list">
      <li data-test="search-result-salary" class="marginBt">
        Salary: <strong>&pound;49,387 to &pound;56,515 a year</strong>
      </li>
      <li data-test="search-result-publicationDate">
        Date posted: <strong>11 August 2026</strong>
      </li>
      <li data-test="search-result-closingDate">
        Closing date: <strong>1 September 2026</strong>
      </li>
      <li data-test="search-result-jobType" class="marginBt">
        Contract type: <strong>Permanent</strong>
      </li>
      <li data-test="search-result-workingPattern" class="marginBt">
        Working pattern: <strong>Flexible working, Full time, Job-share, Part time</strong>
      </li>
    </ul></div>
  </li>
  <li class="nhsuk-list-panel search-result nhsuk-u-padding-3" data-test="search-result">
    <div class="nhsuk-grid-row"><div class="nhsuk-grid-column-two-thirds">
      <h2 class="nhsuk-heading-m" id="job-title-2">
        <a href="/candidate/jobadvert/C9184-26-1273?keyword=&amp;language=en"
           data-test="search-result-job-title">
          Staff Nurse - Acute Medical Unit
        </a>
      </h2>
    </div></div>
    <div class="nhsuk-u-margin-bottom-4" data-test="search-result-location">
      <h3 class="nhsuk-u-font-weight-bold">
        Barts Health NHS Trust
        <div class="location-font-size">
          London E1 1FR
        </div>
    </div>
    <div class="nhsuk-grid-row"><ul class="ad-job-info nhsuk-list">
      <li data-test="search-result-salary" class="marginBt">
        Salary: <strong>Depends on experience</strong>
      </li>
      <li data-test="search-result-publicationDate">
        Date posted: <strong>27 August 2026</strong>
      </li>
      <li data-test="search-result-jobType" class="marginBt">
        Contract type: <strong>Permanent</strong>
      </li>
      <li data-test="search-result-workingPattern" class="marginBt">
        Working pattern: <strong>Part time</strong>
      </li>
    </ul></div>
  </li>
</ul>
"""

# Real detail-page shape (trimmed). Note the two site quirks the parsers are
# built around: the prose blocks are <p id="..."> wrappers with nested <p>
# and no reliable closing tag, and the Details block plus the person
# specification are each rendered TWICE (show-mobile / hide-mobile), so
# several ids appear more than once.
DETAIL_HTML = """
<span id="employer_name">Spectrum Community Health CIC</span>
<h1 class="nhsuk-heading-xl" id="heading">Public Health Practitioner</h1>
<p id="closing_date"><strong>The closing date is 01 September 2026</strong></p>
<h3 class="nhsuk-heading-xs">Job summary</h3>
<p id="job_overview"><p>Leading the development of a whole-prison public
health and recovery-oriented approach.</p>
<p>Embedding health promotion, social prescribing and peer support.</p>
<h3 class="nhsuk-heading-xs">Main duties of the job</h3>
<p id="job_description">What you'll be doing
<p> Develop, implement and review multi-year public health and health
promotion plans across the North East prison cluster.</p>
<p> Use population health data and epidemiology to identify priority
groups and design targeted interventions.</p>
<h3 class="nhsuk-heading-xs">About us</h3>
<p id="about_organisation"><p>BE THE DIFFERENCE IN HEALTHCARE. Access to
NHS Pension. Up to 33 days annual leave.</p></p>
<div class="show-mobile">
  <h2 class="nhsuk-u-visually-hidden">Details</h2>
  <h3 class="nhsuk-heading-s" id="date_posted_heading">Date posted</h3>
  <p id="date_posted">11 August 2026</p>
  <h3 class="nhsuk-heading-s">Pay scheme</h3>
  <p id="payscheme-type">Agenda for change</p>
  <h3 class="nhsuk-heading-s">Band</h3>
  <p id="payscheme-band">Band 7</p>
  <h3 class="nhsuk-heading-s">Salary</h3>
  <p id="range_salary">&pound;49,387 to &pound;56,515 a year
    per annum, pro rata for part time</p>
  <h3 class="nhsuk-heading-s" id="contract_type_heading">Contract</h3>
  <p id="contract_type">Permanent</p>
  <h3 class="nhsuk-heading-s" id="reference_number_heading">Reference number</h3>
  <p id="trac-job-reference">847-RM-26-V945</p>
  <h3 id="employer_location_heading" class="nhsuk-heading-s">Job locations</h3>
  <p id="employer_address_line_1">Navigation Walk</p>
  <p id="employer_town">Wakefield</p>
  <p id="employer_county"></p>
  <p id="employer_postcode">WF1 5RH</p>
  <p id="employer_country">United Kingdom</p>
</div>
<div class="hide-mobile">
  <h2 class="nhsuk-heading-l">Job description</h2>
  <h3 class="nhsuk-heading-xs">Job responsibilities</h3>
  <p id="job_description_large" style="white-space: pre-line">About you
  <p>Significant experience of public health work in complex or
  underserved communities.</p>
  <h2 class="nhsuk-heading-l">Person Specification</h2>
  <h3 id="skill_category_1">application form</h3>
  <h4>Essential</h4>
  <ul class="nhsuk-list">
    <li id="essential_skill_1_criteria_1">Knowledge of a specialist public
      health field acquired through a relevant degree</li>
    <li id="essential_skill_1_criteria_2">Experience of health protection
      and health promotion programmes</li>
  </ul>
  <h4>Desirable</h4>
  <ul class="nhsuk-list">
    <li id="desirable_skill_1_criteria_1">Public health qualification
      (MPH, UKPHR practitioner registration)</li>
  </ul>
</div>
<div class="show-mobile">
  <h2 class="nhsuk-heading-l">Person Specification</h2>
  <h3 id="skill_category_1">application form</h3>
  <ul class="nhsuk-list">
    <li id="essential_skill_1_criteria_1">Knowledge of a specialist public
      health field acquired through a relevant degree</li>
  </ul>
</div>
<h3 id="employer_website_heading">Employer's website</h3>
<a id="employer_website_url_link" href="https://spectrum-cic.org.uk/">
  https://spectrum-cic.org.uk/</a>
"""


class TestSearchPage(unittest.TestCase):
    def setUp(self):
        self.cards = parse_search_cards(SEARCH_HTML)

    def test_result_count(self):
        self.assertEqual(parse_result_count(SEARCH_HTML), 9307)
        self.assertIsNone(parse_result_count("<p>no results here</p>"))

    def test_cards_survive_nested_li(self):
        # Each card contains its own <li> list; a .*?</li> match would cut
        # every card off at the first nested item.
        self.assertEqual(len(self.cards), 2)

    def test_card_fields(self):
        card = self.cards[0]
        self.assertEqual(card["job_id"], "E9847-26-0265")
        self.assertEqual(card["title"], "Public Health Practitioner")
        self.assertEqual(card["company"], "Spectrum Community Health CIC")
        self.assertEqual(card["location"], "Wakefield WF1 5RH")
        self.assertEqual(card["posted_date"], "2026-08-11")
        self.assertEqual(card["closing_date"], "2026-09-01")
        self.assertEqual(card["contract_type"], "Permanent")

    def test_card_labels_are_stripped(self):
        # "Salary: £49,387..." must not keep the "Salary:" label.
        self.assertEqual(self.cards[0]["salary_raw"],
                         "£49,387 to £56,515 a year")

    def test_missing_card_field_is_blank_not_an_error(self):
        # The second advert has no closing date on the card.
        self.assertEqual(self.cards[1]["closing_date"], "")

    def test_search_url_is_unfaceted_and_newest_first(self):
        url = search_url()
        self.assertIn("keyword=", url)
        self.assertIn("sort=publicationDateDesc", url)
        self.assertNotIn("staffGroup", url)
        self.assertIn("staffGroup=ADMINISTRATIVE_AND_CLERICAL",
                      search_url("ADMINISTRATIVE_AND_CLERICAL", 3))
        self.assertIn("page=3", search_url("ADMINISTRATIVE_AND_CLERICAL", 3))


class TestDetailPage(unittest.TestCase):
    def setUp(self):
        self.detail = parse_detail(DETAIL_HTML)

    def test_structured_fields(self):
        self.assertEqual(self.detail["title"], "Public Health Practitioner")
        self.assertEqual(self.detail["company"], "Spectrum Community Health CIC")
        self.assertEqual(self.detail["posted_date"], "2026-08-11")
        self.assertEqual(self.detail["closing_date"], "2026-09-01")
        self.assertEqual(self.detail["pay_scheme"], "Agenda for change")
        self.assertEqual(self.detail["band"], "Band 7")
        self.assertEqual(self.detail["city"], "Wakefield")
        self.assertEqual(self.detail["postcode"], "WF1 5RH")
        self.assertEqual(self.detail["country"], "United Kingdom")
        self.assertEqual(self.detail["employer_website"],
                         "https://spectrum-cic.org.uk/")

    def test_description_survives_nested_p_tags(self):
        # The block wrapper's own </p> is unreliable, so a naive
        # id="..."(.*?)</p> match keeps only the first inner paragraph.
        description = self.detail["description"]
        self.assertIn("whole-prison public health", description)
        self.assertIn("Embedding health promotion", description)

    def test_description_spans_every_prose_block(self):
        description = self.detail["description"]
        self.assertIn("population health data and epidemiology", description)   # duties
        self.assertIn("Significant experience of public health", description)   # responsibilities
        self.assertIn("UKPHR practitioner registration", description)           # person spec

    def test_boilerplate_about_us_is_not_in_the_description(self):
        # about_organisation describes the trust, not the role — feeding it
        # to the classifier would score every advert on employer blurb.
        self.assertNotIn("BE THE DIFFERENCE", self.detail["description"])
        self.assertNotIn("annual leave", self.detail["description"])

    def test_duplicate_mobile_desktop_blocks_are_read_once(self):
        description = parse_description(DETAIL_HTML)
        self.assertEqual(description.count("acquired through a relevant degree"), 1)


class TestDateParsing(unittest.TestCase):
    def test_human_dates(self):
        self.assertEqual(parse_human_date("27 August 2026"), "2026-08-27")
        self.assertEqual(parse_human_date("01 September 2026"), "2026-09-01")
        self.assertEqual(parse_human_date(
            "The closing date is 1 March 2027"), "2027-03-01")

    def test_unparseable_dates_are_blank(self):
        self.assertEqual(parse_human_date(""), "")
        self.assertEqual(parse_human_date("Ongoing"), "")
        self.assertEqual(parse_human_date("27 Augusto 2026"), "")


class TestSalary(unittest.TestCase):
    def test_annual_range(self):
        self.assertEqual(parse_salary("£49,387 to £56,515 a year"),
                         ("49387", "56515", "GBP", "per_annum"))

    def test_hourly_single_value(self):
        self.assertEqual(parse_salary("£12.72 an hour"),
                         ("13", "13", "GBP", "per_hour"))

    def test_unpriced_adverts_invent_nothing(self):
        self.assertEqual(parse_salary("Depends on experience"),
                         ("", "", "", ""))
        self.assertEqual(parse_salary("Negotiable"), ("", "", "", ""))
        self.assertEqual(parse_salary(""), ("", "", "", ""))


class TestJobType(unittest.TestCase):
    def test_part_time_only(self):
        self.assertEqual(map_job_type("Part time"), "part_time")

    def test_full_and_part_time_is_full_time(self):
        self.assertEqual(
            map_job_type("Flexible working, Full time, Job-share, Part time"),
            "full_time")

    def test_remote_and_hybrid(self):
        self.assertEqual(map_job_type("Full time, Home working"), "remote")
        self.assertEqual(map_job_type("Full time, Hybrid working"), "hybrid")

    def test_blank_defaults_to_full_time(self):
        self.assertEqual(map_job_type(""), "full_time")


class TestListingVeto(unittest.TestCase):
    """The pre-fetch skip must be the shared classifier's own veto, and
    nothing more — it must never drop a row a description could rescue."""

    def test_clinical_titles_are_vetoed_without_a_detail_fetch(self):
        self.assertEqual(
            listing_veto("Staff Nurse - Acute Medical Unit",
                         "Nursing and Midwifery Registered").lower(),
            "staff nurse")

    def test_veto_is_identical_with_and_without_a_description(self):
        # The proof the short-circuit rests on: classify_subcategory runs
        # the negative-keyword veto over title + skills ONLY, so no
        # description can change the verdict for a vetoed row.
        title, group = "Staff Nurse - Acute Medical Unit", "Nursing and Midwifery Registered"
        long_description = ("Epidemiology, health protection, immunisation "
                            "programme, surveillance, outbreak management, "
                            "public health intelligence.") * 20
        self.assertFalse(apply_classification(
            {"title": title, "staff_group": group,
             "description": long_description}))

    def test_unscored_titles_are_NOT_vetoed(self):
        # A vague admin title scores nothing on the card, but its
        # description might — it must still earn a detail fetch.
        self.assertEqual(listing_veto("Programme Support Officer",
                                      "Administrative and Clerical"), "")
        self.assertEqual(listing_veto("Band 6 Practitioner",
                                      "Additional Clinical Services"), "")


class TestRowBuilding(unittest.TestCase):
    def setUp(self):
        self.card = parse_search_cards(SEARCH_HTML)[0]
        self.row = build_row(self.card, parse_detail(DETAIL_HTML),
                             "Additional Professional Scientific and Technical")

    def test_detail_wins_over_card_where_both_carry_a_field(self):
        self.assertEqual(self.row["city"], "Wakefield")          # detail
        self.assertEqual(self.row["postcode"], "WF1 5RH")        # detail only
        self.assertEqual(self.row["working_pattern"],            # card only
                         "Flexible working, Full time, Job-share, Part time")

    def test_salary_is_parsed_from_the_detail_string(self):
        self.assertEqual(self.row["salary_min"], "49387")
        self.assertEqual(self.row["salary_max"], "56515")
        self.assertEqual(self.row["salary_currency"], "GBP")
        self.assertEqual(self.row["salary_period"], "per_annum")

    def test_classification_is_stamped_by_the_shared_classifier(self):
        self.assertTrue(apply_classification(self.row))
        self.assertEqual(self.row["category"], "Public Health")
        self.assertEqual(self.row["role_family"], "Public Health")
        self.assertEqual(self.row["vetoed_by"], "")
        self.assertTrue(self.row["family_scores"])

    def test_card_only_row_needs_no_detail_page(self):
        veto_card = parse_search_cards(SEARCH_HTML)[1]
        row = card_only_row(veto_card, "Nursing and Midwifery Registered",
                            "staff nurse")
        self.assertEqual(row["job_id"], "C9184-26-1273")
        self.assertEqual(row["vetoed_by"], "staff nurse")
        self.assertEqual(row["city"], "London E1 1FR")   # falls back to the card
        self.assertEqual(row["description"], "")

    def test_club_row_shape_and_uk_constants(self):
        apply_classification(self.row)
        club = rich_row_to_club_row(self.row)
        self.assertEqual(club["country_name"], "United Kingdom")
        self.assertEqual(club["country_code"], "GB")
        self.assertEqual(club["country_dial_code"], "+44")
        self.assertEqual(club["city_name"], "Wakefield")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["application_url"],
                         "https://www.jobs.nhs.uk/candidate/jobadvert/E9847-26-0265")

    def test_gbp_salaries_are_not_exported_to_the_club_schema(self):
        # The club salary_currency enum admits INR/USD only; the verbatim
        # range stays in the rich CSV rather than being converted.
        club = rich_row_to_club_row(self.row)
        for column in ("min_salary", "max_salary",
                       "salary_period", "salary_currency"):
            self.assertEqual(club[column], "")
        self.assertTrue(self.row["salary_raw"])

    def test_experience_is_blank_not_inferred(self):
        club = rich_row_to_club_row(self.row)
        self.assertEqual(club["min_experience"], "")
        self.assertEqual(club["max_experience"], "")

    def test_company_type(self):
        self.assertEqual(classify_company_type("Barts Health NHS Trust"),
                         "hospital")
        self.assertEqual(classify_company_type("Spectrum Community Health CIC"),
                         "hospital")
        self.assertEqual(classify_company_type("NHS Blood and Transplant "
                                               "Diagnostic Laboratory"), "pharma")


class TestStaffGroups(unittest.TestCase):
    def test_all_nine_groups_are_configured(self):
        self.assertEqual(len(STAFF_GROUPS), 9)
        self.assertIn("NURSING_AND_MIDWIFERY_REGD", STAFF_GROUPS)
        self.assertTrue(all(STAFF_GROUPS.values()))


class TestCrawlWalk(unittest.TestCase):
    """iter_cards must interleave the groups and date-gate on the card."""

    def _run(self, cutoff, page_bodies):
        import scraper

        calls = []

        def fake_fetch(_session, url):
            calls.append(url)
            for key, body in page_bodies.items():
                if key in url:
                    return body
            return ""

        original, scraper.fetch = scraper.fetch, fake_fetch
        try:
            counters = {"scanned": 0, "listing_pages": 0, "excluded_old": 0}
            groups = {"ADMINISTRATIVE_AND_CLERICAL": "Administrative and Clerical",
                      "NURSING_AND_MIDWIFERY_REGD": "Nursing and Midwifery Registered"}
            yielded = list(scraper.iter_cards(None, groups, cutoff, counters))
        finally:
            scraper.fetch = original
        return yielded, counters, calls

    def test_groups_are_interleaved_page_by_page(self):
        bodies = {"ADMINISTRATIVE_AND_CLERICAL&page=1": SEARCH_HTML,
                  "NURSING_AND_MIDWIFERY_REGD&page=1": SEARCH_HTML}
        _, _, calls = self._run("2026-01-01", bodies)
        # Page 1 of BOTH groups must be read before page 2 of either.
        self.assertIn("ADMINISTRATIVE_AND_CLERICAL", calls[0])
        self.assertIn("NURSING_AND_MIDWIFERY_REGD", calls[1])

    def test_out_of_window_cards_are_dropped_before_any_detail_fetch(self):
        bodies = {"page=1": SEARCH_HTML}
        # Cutoff sits between the two adverts (11 Aug and 27 Aug).
        yielded, counters, calls = self._run("2026-08-20", bodies)
        self.assertEqual(counters["excluded_old"], 2)     # one per group
        self.assertEqual([c["job_id"] for c, _ in yielded],
                         ["C9184-26-1273", "C9184-26-1273"])
        self.assertTrue(all("/candidate/search/" in url for url in calls))

    def test_a_group_is_retired_when_a_short_page_ends_it(self):
        bodies = {"page=1": SEARCH_HTML}     # 2 cards < RESULTS_PER_PAGE
        _, counters, calls = self._run("2026-01-01", bodies)
        self.assertEqual(counters["listing_pages"], 2)    # one page per group
        self.assertEqual(len(calls), 2)


class TestEndToEnd(unittest.TestCase):
    """One full main() run against fake fetches, writing to a temp dir.

    Proves the wiring the individual parser tests cannot: that an in-scope
    advert reaches the rich CSV and the club CSV, that a vetoed advert
    reaches out-of-scope.csv and NEVER the main CSV, and that the vetoed
    advert costs no detail request.
    """

    def setUp(self):
        import shutil
        import tempfile

        import scraper

        self.scraper = scraper
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.detail_fetches = []
        self._saved = {name: getattr(scraper, name) for name in
                       ("fetch", "check_robots", "assert_partition",
                        "OUT_OF_SCOPE_CSV", "NEEDS_REVIEW_CSV", "CLUB_CSV_DIR",
                        "REQUEST_DELAY_SECONDS")}

        def fake_fetch(_session, url):
            if "/jobadvert/" in url:
                self.detail_fetches.append(url)
                return DETAIL_HTML
            if "page=1" in url:
                return SEARCH_HTML
            return ""

        scraper.fetch = fake_fetch
        scraper.check_robots = lambda _session: None
        scraper.assert_partition = lambda _session: (2, {})
        scraper.OUT_OF_SCOPE_CSV = str(Path(self.tmp) / "out-of-scope.csv")
        scraper.NEEDS_REVIEW_CSV = str(Path(self.tmp) / "needs_review.csv")
        scraper.CLUB_CSV_DIR = Path(self.tmp) / "jobs_csv"
        scraper.REQUEST_DELAY_SECONDS = 0

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.scraper, name, value)

    def test_full_run(self):
        rich = str(Path(self.tmp) / "nhsjobs_jobs.csv")
        self.scraper.main([
            "--output", rich, "--since", "2026-01-01", "--run-date", "27-08-2026",
            "--staff-group", "ADMINISTRATIVE_AND_CLERICAL", "--no-partition-check",
        ])

        rich_df = pd.read_csv(rich, dtype=str).fillna("")
        self.assertEqual(list(rich_df.columns), self.scraper.RICH_COLUMNS)
        # The Public Health advert is kept; the Staff Nurse never appears.
        self.assertEqual(list(rich_df["job_id"]), ["E9847-26-0265"])
        self.assertEqual(rich_df.iloc[0]["category"], "Public Health")
        self.assertEqual(rich_df.iloc[0]["staff_group"], "Administrative and Clerical")

        dropped = pd.read_csv(self.scraper.OUT_OF_SCOPE_CSV, dtype=str).fillna("")
        self.assertEqual(list(dropped["job_id"]), ["C9184-26-1273"])
        self.assertEqual(dropped.iloc[0]["vetoed_by"].lower(), "staff nurse")

        # The vetoed advert cost no detail request.
        self.assertEqual(len(self.detail_fetches), 1)
        self.assertIn("E9847-26-0265", self.detail_fetches[0])

        club = pd.read_csv(
            Path(self.tmp) / "jobs_csv" / "27-08-2026" / "nhsjobs.csv",
            dtype=str).fillna("")
        self.assertEqual(list(club.columns), CLUB_COLUMNS)
        self.assertEqual(len(club), 1)
        self.assertEqual(club.iloc[0]["country_code"], "GB")
        self.assertEqual(club.iloc[0]["category"], "Public Health")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-19", ""]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 8, 19) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_since_override(self):
        self.assertEqual(compute_cutoff(None, since="2026-07-01"), "2026-07-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
