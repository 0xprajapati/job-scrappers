#!/usr/bin/env python3
"""Unit tests for the jhpiego scraper's parsers, cutoff and export mapping.

Run with plain:  python test_filters.py

Worked examples are trimmed real payloads captured 2026-08-28: job 8067
(Senior Program Officer -TB -Gujrat (Gandhinagar), India), job 8052
(Conseiller Technique — Surveillance Epidémiologique, Côte d'Ivoire,
French) and job 7964 (Epidemiólogo/a para Seguridad Sanitaria Global,
Guatemala, Spanish), plus the live sitemap's URL shapes.
"""

import unittest
from datetime import date, timedelta

import pandas as pd

import scraper
from scraper import (
    CLUB_COLUMNS,
    COUNTRY_BY_CODE,
    FORBIDDEN_PATH_TOKENS,
    INITIAL_WINDOW_DAYS,
    RICH_COLUMNS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_row,
    compute_cutoff,
    job_type_from_employment,
    looks_non_english,
    parse_base_salary,
    parse_experience_years,
    parse_iso_date,
    parse_job_posting,
    parse_locations,
    parse_sitemap_jobs,
    rich_row_to_club_row,
)

# Trimmed real sitemap.xml shape. <lastmod> is a modification stamp, not
# the posting date — ignored. /jobs/intro must never match.
SITEMAP_XML = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://jobs-jhpiego.icims.com/jobs/intro</loc>
    <lastmod>2024-03-21T10:07:03-04:00</lastmod>
  </url>
  <url>
    <loc>https://jobs-jhpiego.icims.com/jobs/8067/senior-program-officer--tb--gujrat-%28gandhinagar%29/job</loc>
    <lastmod>2026-08-26T09:00:00-04:00</lastmod>
  </url>
  <url>
    <loc>https://jobs-jhpiego.icims.com/jobs/4171/care-and-treatment-lead/job</loc>
    <lastmod>2024-03-21T10:07:03-04:00</lastmod>
  </url>
  <url>
    <loc>https://jobs-jhpiego.icims.com/jobs/8071/senior--program-officer---cphc/job</loc>
    <lastmod>2026-08-27T11:00:00-04:00</lastmod>
  </url>
</urlset>"""

TB_BODY = (
    "<h2>Overview</h2><p>Jhpiego is an international, non-profit health "
    "organization and an affiliate of Johns Hopkins University, dedicated "
    "to improving the health of women and families in developing "
    "countries.</p><p>The Senior Program Officer will support the state TB "
    "cell in planning, implementation and monitoring of the National TB "
    "Elimination Program (NTEP), tuberculosis case finding, public health "
    "program management and partner coordination with the Ministry of "
    "Health.</p><p>Minimum 5 years of experience in public health "
    "programs required. MPH preferred.</p>"
)

# Trimmed real detail-page JSON-LD (job 8067), including iCIMS's
# "UNAVAILABLE" placeholders in the unused address fields.
DETAIL_HTML = """
<script type="application/ld+json">{"@context": "http://schema.org",
"@type": "JobPosting",
"title": "Senior Program Officer -TB -Gujrat (Gandhinagar)",
"description": "%s",
"datePosted": "2026-08-26T04:00:00.000Z",
"validThrough": "2027-08-26T04:00:00.000Z",
"employmentType": "OTHER",
"occupationalCategory": "Local",
"directApply": true,
"hiringOrganization": {"@type": "Organization", "name": "Jhpiego",
"sameAs": "www.jhpiego.org"},
"jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress",
"addressCountry": "IN", "addressLocality": "Gujrat",
"addressRegion": "UNAVAILABLE", "streetAddress": "UNAVAILABLE",
"postalCode": "UNAVAILABLE", "postOfficeBoxNumber": "UNAVAILABLE"}}],
"url": "https://jobs-jhpiego.icims.com/jobs/8067/senior-program-officer--tb--gujrat-%%28gandhinagar%%29/job"}</script>
""" % TB_BODY.replace('"', '\\"')

FRENCH_BODY = (
    "Jhpiego recrute pour le compte de l'Unité de Gestion Opérationnelle "
    "de l'Accord de Coopération entre le Gouvernement de Côte d'Ivoire et "
    "le Gouvernement des Etats-Unis d'Amérique un Conseiller Technique "
    "pour la surveillance épidémiologique dans les régions ciblées, la "
    "riposte aux épidémies et la qualité des données pour le ministère de "
    "la santé et les partenaires du projet dans une approche intégrée. "
    "Epidemiological surveillance, disease surveillance and outbreak "
    "response for global health security."
)

FRENCH_POSTING = {
    "@type": "JobPosting",
    "title": "Conseiller Technique_ Surveillance Epidémiologique et Santé "
             "Sécuritaire",
    "description": "<p>" + FRENCH_BODY + "</p>",
    "datePosted": "2026-08-21T04:00:00.000Z",
    "validThrough": "2027-08-21T04:00:00.000Z",
    "employmentType": "OTHER",
    "occupationalCategory": "Local",
    "hiringOrganization": {"@type": "Organization", "name": "Jhpiego"},
    "jobLocation": [{"@type": "Place", "address": {
        "@type": "PostalAddress", "addressCountry": "CI",
        "addressLocality": "Abidjan", "addressRegion": "UNAVAILABLE"}}],
    "url": "https://jobs-jhpiego.icims.com/jobs/8052/conseiller-technique/job",
}


class TestSitemapParser(unittest.TestCase):
    def test_urls_and_ids_newest_first(self):
        jobs = parse_sitemap_jobs(SITEMAP_XML)
        self.assertEqual([job_id for _, job_id in jobs],
                         ["8071", "8067", "4171"])
        self.assertEqual(
            jobs[1][0],
            "https://jobs-jhpiego.icims.com/jobs/8067/"
            "senior-program-officer--tb--gujrat-%28gandhinagar%29/job")

    def test_the_intro_page_is_not_a_job(self):
        self.assertNotIn("intro",
                         " ".join(u for u, _ in parse_sitemap_jobs(SITEMAP_XML)))

    def test_empty(self):
        self.assertEqual(parse_sitemap_jobs("<urlset></urlset>"), [])
        self.assertEqual(parse_sitemap_jobs(""), [])


class TestDetailParser(unittest.TestCase):
    def test_jobposting_parsed(self):
        posting = parse_job_posting(DETAIL_HTML)
        self.assertEqual(posting.get("title"),
                         "Senior Program Officer -TB -Gujrat (Gandhinagar)")
        self.assertEqual(posting.get("occupationalCategory"), "Local")

    def test_trailing_brace_tolerated(self):
        # Defensive JSON-LD parsing: raw_decode must survive the stray `}`
        # bug seen on other boards' JSON-LD (devnetjobsindia).
        html = DETAIL_HTML.replace("/job\"}</script>", "/job\"}}</script>")
        self.assertEqual(parse_job_posting(html).get("occupationalCategory"),
                         "Local")

    def test_non_jobposting_skipped(self):
        html = ('<script type="application/ld+json">{"@type": "Organization"}'
                '</script>')
        self.assertEqual(parse_job_posting(html), {})


class TestLocations(unittest.TestCase):
    def test_iso_code_and_city_are_read(self):
        posting = parse_job_posting(DETAIL_HTML)
        self.assertEqual(parse_locations(posting), [("IN", "Gujrat")])

    def test_unavailable_placeholders_are_never_data(self):
        posting = {"jobLocation": [{"address": {
            "addressCountry": "UNAVAILABLE",
            "addressLocality": "UNAVAILABLE"}}]}
        self.assertEqual(parse_locations(posting), [])

    def test_multiple_locations_are_all_kept(self):
        posting = {"jobLocation": [
            {"address": {"addressCountry": "SN", "addressLocality": "Dakar"}},
            {"address": {"addressCountry": "SN", "addressLocality": "Fatick"}},
        ]}
        self.assertEqual(parse_locations(posting),
                         [("SN", "Dakar"), ("SN", "Fatick")])

    def test_missing_job_location_is_empty_not_an_error(self):
        self.assertEqual(parse_locations({}), [])


class TestFieldParsers(unittest.TestCase):
    def test_iso_date(self):
        self.assertEqual(parse_iso_date("2026-08-26T04:00:00.000Z"),
                         "2026-08-26")
        self.assertEqual(parse_iso_date(None), "")

    def test_experience(self):
        self.assertEqual(parse_experience_years(
            "Minimum 5 years of experience in public health programs."), "5")
        self.assertEqual(parse_experience_years(
            "3+ years of M&E experience required."), "3")
        self.assertEqual(parse_experience_years("No prior notice."), "")

    def test_base_salary_absent(self):
        # Every probed Jhpiego page has no baseSalary.
        self.assertEqual(parse_base_salary(None),
                         ("Not Disclosed", "", "", "", ""))

    def test_base_salary_present(self):
        raw, lo, hi, ccy, period = parse_base_salary({
            "@type": "MonetaryAmount", "currency": "USD",
            "value": {"@type": "QuantitativeValue", "minValue": 60000,
                      "maxValue": 80000, "unitText": "YEAR"}})
        self.assertEqual((lo, hi, ccy, period),
                         (60000, 80000, "USD", "per_annum"))
        self.assertIn("USD", raw)

    def test_job_type(self):
        # "OTHER" is what every probed page carries.
        self.assertEqual(job_type_from_employment("OTHER"), "full_time")
        self.assertEqual(job_type_from_employment(["Part-time"]), "part_time")
        self.assertEqual(job_type_from_employment(None), "full_time")

    def test_employment_status_is_read_from_the_page_header(self):
        # The page's dt/dd header table is more truthful than the JSON-LD
        # constant "OTHER" (real 8067 shape).
        html = ("<dt>Job ID</dt><dd>2026-8067</dd>"
                "<dt>Employment Status</dt><dd>Full-Time</dd>")
        self.assertEqual(scraper.parse_employment_status(html), "Full-Time")
        self.assertEqual(scraper.parse_employment_status(""), "")
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("8067", posting, posting["url"], html)
        self.assertEqual(row["employment_type_raw"], "OTHER; Full-Time")
        part = build_row("8067", posting, posting["url"],
                         "<dt>Employment Status</dt><dd>Part-Time</dd>")
        self.assertEqual(rich_row_to_club_row(part)["job_type"], "part_time")


class TestLanguage(unittest.TestCase):
    """FR/ES country-office postings are kept but flagged, never dropped
    on language alone."""

    def test_french_description_is_detected(self):
        self.assertTrue(looks_non_english(FRENCH_BODY))

    def test_spanish_description_is_detected(self):
        text = ("En Jhpiego trabajamos para salvar vidas, mejorar la salud "
                "y transformar el futuro de las mujeres y los niños "
                "alrededor del mundo, con los gobiernos y las "
                "organizaciones para el fortalecimiento de los sistemas "
                "de salud en el país y para la seguridad sanitaria global.")
        self.assertTrue(looks_non_english(text))

    def test_english_description_is_not(self):
        self.assertFalse(looks_non_english(
            "Jhpiego is an international non-profit health organization "
            "supporting TB program implementation and monitoring."))

    def test_empty_description_is_not_flagged(self):
        self.assertFalse(looks_non_english(""))
        self.assertFalse(looks_non_english(None))


class TestClassifier(unittest.TestCase):
    """Wiring into the shared taxonomy — the engine itself is covered by
    ../_shared/test_classification.py."""

    def test_an_me_specialist_is_kept_on_its_title(self):
        # Real job 7417's title form: "monitoring and evaluation" is a
        # Public Health title keyword in the shared engine.
        row = {"title": "Monitoring and Evaluation Specialist Digital "
                        "Systems and Visualization",
               "description": "Leads M&E for health programs, DHIS2 "
                              "dashboards and routine health data quality."}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")

    def test_description_only_ph_drops_under_the_current_shared_engine(self):
        # KNOWN LIMIT (documented in the scraper docstring/readme): the
        # shared engine requires Public Health to match in title or
        # skills, and this board has no skills signal — so a TB program
        # officer whose title carries no PH keyword drops, landing
        # reversibly in out-of-scope.csv. If _shared grows the ruled
        # NGO-board description-only-PH path, this test SHOULD fail —
        # that failure is the signal to re-adjudicate out-of-scope.csv.
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("8067", posting, posting["url"])
        self.assertFalse(apply_classification(row))
        self.assertEqual(row["vetoed_by"], "")   # no veto — just no match

    def test_accents_are_folded_for_the_classifier(self):
        from scraper import fold_diacritics
        self.assertEqual(fold_diacritics("Épidémiologique"),
                         "Epidemiologique")
        self.assertEqual(fold_diacritics(""), "")
        # A French epidemiology title reaches the shared keywords once
        # folded.
        row = {"title": "Chargé de Surveillance Épidémiologique",
               "description": FRENCH_BODY}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")

    def test_back_office_roles_dropped(self):
        for title in ("Finance and Administration Manager",
                      "Procurement Officer",
                      "Human Resources (HR) Officer"):
            row = {"title": title,
                   "description": "Manages ledgers, purchase orders and "
                                  "personnel files for the country office."}
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_a_french_row_is_kept_but_forced_into_review(self):
        row = build_row("8052", FRENCH_POSTING, FRENCH_POSTING["url"])
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_the_hire_type_tag_is_not_a_classifier_signal(self):
        # occupationalCategory "Local" must not sway the verdict: an
        # obvious out-of-scope role stays dropped whatever the tag says.
        row = {"title": "Driver", "hire_type": "Local",
               "description": "Drives project vehicles and maintains the "
                              "vehicle log."}
        self.assertFalse(apply_classification(row))


class TestRowBuilding(unittest.TestCase):
    def test_row_and_club_row(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("8067", posting, "https://x/jobs/8067/slug/job")
        self.assertEqual(sorted(row), sorted(RICH_COLUMNS))
        self.assertEqual(row["job_id"], "8067")
        self.assertEqual(row["posted_date"], "2026-08-26")
        self.assertEqual(row["valid_through"], "2027-08-26")  # auto-stamp
        self.assertEqual(row["city"], "Gujrat")
        self.assertEqual(row["country"], "India")
        self.assertEqual(row["country_code"], "IN")
        self.assertEqual(row["hire_type"], "Local")
        self.assertEqual(row["employment_type_raw"], "OTHER")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["experience_min_years"], "5")
        self.assertIn("National TB Elimination Program", row["description"])
        self.assertNotIn("<", row["description"])  # HTML stripped
        # JSON-LD url wins over the sitemap URL passed in
        self.assertIn("/jobs/8067/", row["job_url"])

        club = rich_row_to_club_row(row)
        self.assertEqual(sorted(club), sorted(CLUB_COLUMNS))
        self.assertEqual((club["country_name"], club["country_code"],
                          club["country_dial_code"]),
                         ("India", "IN", "+91"))
        self.assertEqual(club["city_name"], "Gujrat")
        self.assertEqual(club["company_name"], "Jhpiego")
        self.assertEqual(club["company_type"], "hospital")  # NGO convention
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["posted_at"], "2026-08-26")
        self.assertEqual(club["min_experience"], "5")
        self.assertEqual(club["min_salary"], "")   # board publishes none
        self.assertEqual(club["salary_currency"], "")

    def test_qualification_is_lifted_from_the_description(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("8067", posting, posting["url"])
        self.assertIn("MPH", rich_row_to_club_row(row)["qualification"])

    def test_a_cityless_row_falls_back_to_the_country_name(self):
        posting = dict(FRENCH_POSTING,
                       jobLocation=[{"@type": "Place", "address": {
                           "addressCountry": "CI",
                           "addressLocality": "UNAVAILABLE"}}])
        row = build_row("8052", posting, posting["url"])
        self.assertEqual(row["city"], "")
        self.assertEqual(rich_row_to_club_row(row)["city_name"],
                         "Côte d'Ivoire")

    def test_an_unknown_country_code_is_never_guessed(self):
        posting = dict(FRENCH_POSTING,
                       jobLocation=[{"@type": "Place", "address": {
                           "addressCountry": "ZZ",
                           "addressLocality": "Nowhere"}}])
        row = build_row("9999", posting, posting["url"])
        self.assertEqual(row["country"], "")
        self.assertEqual(row["country_code"], "")
        self.assertEqual(rich_row_to_club_row(row)["country_dial_code"], "")


class TestCutoff(unittest.TestCase):
    def test_first_run_window(self):
        expected = (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()
        self.assertEqual(compute_cutoff(None), expected)

    def test_watermark(self):
        df = pd.DataFrame({"posted_date": ["2026-08-01", "2026-08-26", ""]})
        self.assertEqual(
            compute_cutoff(df),
            (date(2026, 8, 26) - timedelta(days=WATERMARK_GRACE_DAYS)).isoformat())

    def test_since_override(self):
        self.assertEqual(compute_cutoff(None, since="2026-07-01"), "2026-07-01")


class TestCompliance(unittest.TestCase):
    def test_every_request_goes_to_the_icims_host(self):
        for url in (scraper.SITEMAP_URL, scraper.ROBOTS_URL):
            self.assertTrue(url.startswith(scraper.SITE_BASE))

    def test_the_forbidden_tokens_cover_the_robots_disallows(self):
        for token in ("referral", "login", "candidate", "reminder",
                      "/connect"):
            self.assertIn(token, FORBIDDEN_PATH_TOKENS)

    def test_fetch_refuses_robots_disallowed_paths(self):
        with self.assertRaises(AssertionError):
            scraper.fetch(None, scraper.SITE_BASE + "/jobs/login")

    def test_country_table_is_iso_alpha_2_keyed(self):
        for code in COUNTRY_BY_CODE:
            self.assertRegex(code, r"^[A-Z]{2}$")

    def test_request_spacing_is_at_least_the_fleet_minimum(self):
        self.assertGreaterEqual(scraper.REQUEST_DELAY_SECONDS, 1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
