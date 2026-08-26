#!/usr/bin/env python3
"""Unit tests for the impactpool.org scraper's parsers, classifier wiring
and location split.

Run with plain `python test_filters.py` (no pytest needed).

Every example marked "real" was taken verbatim from
https://www.impactpool.org/search?per_page=100 or a /jobs/<id> detail page
pulled on 2026-08-26. The classification engines are covered by _shared's
own tests; here only the wiring is asserted (master spec: an in-scope role
gets the right category/sub_category, an out-of-scope title is dropped).
"""

import unittest

from impactpool_scraper import (
    build_row,
    classify_company_type,
    classify_impactpool_job,
    extract_main_content,
    parse_cards,
    parse_deadline,
    parse_detail,
    rich_row_to_club_row,
    split_location,
    strip_html,
    truncate_description,
)

# Real card, trimmed to the parsed markup (UNICEF Health Specialist,
# /jobs/1231881, search page 1 of 2026-08-26). The second card exercises a
# promoted-card "extra" badge and a grade-only meta row.
CARD_HTML = """
<div class='job'>
<div class='extra'>
</div>
<a data-turbo-frame="_top" href="/jobs/1231881"><img alt="" src="x.svg" />
<div class='ip-typography' style='color: #1C1B16' type='cardTitle'>Health Specialist (Adolescent Health), NO-3, Fixed Term Position, Pretoria, South Africa #00137966</div>
<div class='ip-layout' gap='1' wrap='wrap'>
<div class='ip-typography' style='color: #1C1B16;' type='bodyEmphasis'>
UNICEF - United Nations Children&#8217;s Fund
<img alt="" src="/assets/ellipse.svg" />
</div>
<div class='ip-layout' gap='1'>
<div class='ip-typography' style='color: #63625B;' type='bodyEmphasis'>Pretoria</div>
<img alt="" src="/assets/ellipse.svg" />
<div class='ip-typography' style='color: #75736C;' type='bodyEmphasis'>NO-C, National Professional Officer - Locally recruited position - Mid level</div>
</div>
</div>
</a></div>

<div class='job'>
<div class='extra'>
<span badge-size='medium' badge-type='success' class='ip-badge'>
<span class='ip-badge-text'>New</span>
</span>
</div>
<a data-turbo-frame="_top" href="/jobs/1233480">
<div class='ip-typography' style='color: #1C1B16' type='cardTitle'>UN Women - Resource Mobilisation Consultant (Home-based)</div>
<div class='ip-layout' gap='1' wrap='wrap'>
<div class='ip-typography' style='color: #1C1B16;' type='bodyEmphasis'>
UN WOMEN - United Nations Entity for Gender Equality and the Empowerment of Women
<img alt="" src="/assets/ellipse.svg" />
</div>
<div class='ip-layout' gap='1'>
<div class='ip-typography' style='color: #63625B;' type='bodyEmphasis'>Remote | Home Based - May require travel</div>
<img alt="" src="/assets/ellipse.svg" />
<div class='ip-typography' style='color: #75736C;' type='bodyEmphasis'>Senior</div>
</div>
</div>
</a></div>
"""

# Real detail-page fragments (/jobs/1231881, 2026-08-26), trimmed.
DETAIL_HTML = """
<div class='ip-typography' style='color: #538F3E;' type='bodyEmphasis'>Application deadline: August 31, 2026 (5 days)</div>
<div class='summary'>
<div class='ip-layout' direction='column' gap='0.5'>
<div class='ip-typography' type='bodyEmphasis'>Summary by Impactpool</div>
<div class='ip-typography' type='body'>
The Adolescent Health Specialist at UNICEF will provide strategic leadership
and technical expertise to enhance adolescent health and well-being.
</div>
</div>
<div class='ip-layout' direction='column' gap='0.5'>
<div class='ip-typography' type='bodyEmphasis'>Candidate Requirements:</div>
<div class='ip-typography' type='body'>
<ul>
<li>Advanced university degree in relevant field</li>
<li>At least 5 years of experience in health and partnerships</li>
</ul>
</div>
</div>
</div>
<div class='main-content'>
<p>As the Adolescent Health Specialist, you will provide strategic
leadership in advancing adolescent health.</p>
<div class='nested'><p>This includes sexual and reproductive health,
mental health, and HIV prevention programming.</p></div>
</div>
<div class='sidebar ip-layout'>
<p>Recruiting? Post a job.</p>
</div>
"""


class TestParseCards(unittest.TestCase):
    def test_real_cards(self):
        cards = parse_cards(CARD_HTML)
        self.assertEqual(len(cards), 2)

        unicef = cards[0]
        self.assertEqual(unicef["job_id"], "1231881")
        self.assertTrue(unicef["title"].startswith(
            "Health Specialist (Adolescent Health)"))
        # entity + whitespace collapsed; the trailing <img> never leaks in
        self.assertEqual(unicef["company"],
                         "UNICEF - United Nations Children’s Fund")
        self.assertEqual(unicef["locations"], "Pretoria")
        self.assertTrue(unicef["grade"].startswith("NO-C"))

        unwomen = cards[1]
        self.assertEqual(unwomen["job_id"], "1233480")
        self.assertEqual(unwomen["locations"],
                         "Remote | Home Based - May require travel")
        self.assertEqual(unwomen["grade"], "Senior")

    def test_no_cards(self):
        self.assertEqual(parse_cards(""), [])
        self.assertEqual(parse_cards("<html><body>maintenance</body></html>"),
                         [])


class TestSplitLocation(unittest.TestCase):
    def test_city_only_known_duty_station(self):
        # real card: UNICEF Pretoria — country derived from the duty-station
        # table, never guessed
        self.assertEqual(split_location("Pretoria"),
                         ("Pretoria", "South Africa", False))

    def test_city_pipe_country(self):
        # real card: DW correspondents "Islamabad | Pakistan"
        self.assertEqual(split_location("Islamabad | Pakistan"),
                         ("Islamabad", "Pakistan", False))

    def test_remote_multi_country(self):
        # real card: Financial Innovation for Impact Lead Economist
        city, country, remote = split_location(
            "Remote | Nigeria | Indonesia | Kenya | Ethiopia | Uganda")
        self.assertTrue(remote)
        self.assertEqual(city, "")
        self.assertEqual(country, "Nigeria")   # first listed

    def test_home_based(self):
        # real card: UN Women consultant
        city, country, remote = split_location(
            "Remote | Home Based - May require travel")
        self.assertTrue(remote)
        self.assertEqual((city, country), ("", ""))

    def test_sector_shorthand_alias(self):
        # real card: CTG training officer in "CAR"
        self.assertEqual(split_location("CAR"),
                         ("", "Central African Republic", False))

    def test_unknown_city_stays_countryless(self):
        self.assertEqual(split_location("Tarawa"), ("Tarawa", "", False))

    def test_empty(self):
        self.assertEqual(split_location(""), ("", "", False))


class TestParseDeadline(unittest.TestCase):
    def test_real_format(self):
        # real detail line: "Application deadline: August 31, 2026 (5 days)"
        self.assertEqual(parse_deadline("August 31, 2026"), "2026-08-31")

    def test_bad_input(self):
        self.assertEqual(parse_deadline("31/08/2026"), "")
        self.assertEqual(parse_deadline(""), "")


class TestParseDetail(unittest.TestCase):
    def test_real_fragments(self):
        detail = parse_detail(DETAIL_HTML)
        self.assertEqual(detail["closing_date"], "2026-08-31")
        self.assertIn("strategic leadership in advancing adolescent health",
                      detail["description"])
        # depth-balanced extraction keeps nested-div content...
        self.assertIn("HIV prevention programming", detail["description"])
        # ...and stops before the sidebar
        self.assertNotIn("Post a job", detail["description"])
        self.assertIn("Advanced university degree", detail["ip_summary"])
        self.assertIn("well-being", detail["ip_summary"])

    def test_missing_everything(self):
        detail = parse_detail("<html><body></body></html>")
        self.assertEqual(detail, {"closing_date": "", "description": "",
                                  "ip_summary": ""})


class TestExtractMainContent(unittest.TestCase):
    def test_depth_balance(self):
        html = ("<div class='main-content'><div><p>a</p><div>b</div></div>"
                "</div><div class='sidebar'>c</div>")
        seg = extract_main_content(html)
        self.assertIn("b", seg)
        self.assertNotIn("sidebar", seg)

    def test_absent(self):
        self.assertEqual(extract_main_content("<div>x</div>"), "")


class TestStripHtml(unittest.TestCase):
    def test_entities_unescaped(self):
        self.assertEqual(strip_html("<p>M&amp;E officer</p>"), "M&E officer")

    def test_tags_removed(self):
        self.assertEqual(strip_html("<ul><li>MPH</li><li>MD</li></ul>"),
                         "MPH MD")


class TestTruncateDescription(unittest.TestCase):
    def test_short_untouched(self):
        self.assertEqual(truncate_description("short", 100), "short")

    def test_word_boundary_and_marker(self):
        out = truncate_description("alpha beta gamma delta", 12)
        self.assertEqual(out, "alpha beta…")


class TestClassifierWiring(unittest.TestCase):
    """Only wiring — the engines have their own tests in _shared."""

    def test_in_scope_gets_right_labels(self):
        # real card (health search, 2026-08-26): Community Health Worker
        verdict = classify_impactpool_job("Community Health Worker")
        self.assertTrue(verdict["in_scope"])
        self.assertEqual(verdict["category"], "Public Health")
        self.assertEqual(verdict["sub_category"], "Community Health")

    def test_out_of_scope_dropped(self):
        # real card: MFO "Chief Fire Officer" — never exported
        verdict = classify_impactpool_job(
            "Chief Fire Officer",
            description="Lead the fire brigade of the multinational force, "
                        "manage fire safety inspections and drills.")
        self.assertFalse(verdict["in_scope"])

    def test_summary_feeds_the_description_signal(self):
        """The Impactpool summary card rides along in the text the classifier
        scores — visible as a description match in matched_in. (The shared
        engine is title-gated: a description can refine labels but never
        admit a job on its own.)"""
        verdict = classify_impactpool_job(
            "Community Health Worker",
            description="",
            ip_summary="Support the district epidemiology and immunization "
                       "programme with community outreach.")
        self.assertTrue(verdict["in_scope"])
        self.assertIn("description", verdict["matched_in"])


class TestCompanyType(unittest.TestCase):
    def test_default_hospital(self):
        self.assertEqual(classify_company_type(
            "UNICEF - United Nations Children's Fund"), "hospital")

    def test_pharma_lookalike(self):
        self.assertEqual(classify_company_type(
            "Medicines for Malaria Venture"), "pharma")


class TestBuildRow(unittest.TestCase):
    # real card (health search, 2026-08-26): UNICEF Child Health Specialist
    CARD = {"job_id": "1231882",
            "title": "Child Health Specialist (NO-3), FT, #137967, "
                     "Pretoria - South Africa, ESAR",
            "company": "UNICEF - United Nations Children's Fund",
            "locations": "Pretoria",
            "grade": "NO-C, National Professional Officer - Mid level"}
    DETAIL = {"closing_date": "2026-08-31",
              "description": "Advance child health programming. "
                             "Advanced university degree (MPH) required.",
              "ip_summary": "Strategic leadership for child health."}

    def _row(self):
        verdict = classify_impactpool_job(self.CARD["title"],
                                          self.DETAIL["description"],
                                          self.DETAIL["ip_summary"])
        return build_row(self.CARD, self.DETAIL, verdict)

    def test_rich_row(self):
        row = self._row()
        self.assertEqual(row["job_id"], "1231882")
        self.assertEqual(row["country"], "South Africa")
        self.assertEqual(row["city"], "Pretoria")
        self.assertEqual(row["closing_date"], "2026-08-31")
        # the site exposes no posted date — never invented
        self.assertEqual(row["posted_date"], "")
        self.assertEqual(row["job_url"],
                         "https://www.impactpool.org/jobs/1231882")

    def test_club_mapping(self):
        club = rich_row_to_club_row(self._row())
        self.assertEqual(club["country_name"], "South Africa")
        self.assertEqual(club["country_code"], "ZA")
        self.assertEqual(club["country_dial_code"], "+27")
        self.assertEqual(club["city_name"], "Pretoria")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["posted_at"], "")          # never invented
        self.assertIn(club["category"], ("Non Clinical", "Public Health"))
        # no salary anywhere on this source — columns stay empty
        self.assertEqual(club["min_salary"], "")
        self.assertEqual(club["salary_currency"], "")
        self.assertIn("mph", club["qualification"].lower())

    def test_remote_maps_to_remote_job_type(self):
        card = dict(self.CARD, locations="Remote | Home Based")
        verdict = classify_impactpool_job(card["title"],
                                          self.DETAIL["description"])
        club = rich_row_to_club_row(build_row(card, self.DETAIL, verdict))
        self.assertEqual(club["job_type"], "remote")
        self.assertEqual(club["country_name"], "")
        self.assertEqual(club["country_code"], "")

    def test_club_columns_match_the_contract(self):
        # imported from _shared/classification — never hand-copied
        from impactpool_scraper import CLUB_COLUMNS
        club = rich_row_to_club_row(self._row())
        self.assertEqual(list(club.keys()), CLUB_COLUMNS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
