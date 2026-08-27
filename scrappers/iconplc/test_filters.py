#!/usr/bin/env python3
"""Unit tests for the iconplc scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples are trimmed from real detail pages captured 2026-08-27:
jid 49011 (Senior TMF Specialist, India/Chennai — JR146301),
jid 51472 (Senior Clinical Systems Specialist (IRT), Regional United
States (PRA) — JR152608), jid 44634 (Senior Medical Writer, Japan/Tokyo).
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_row,
    compute_cutoff,
    job_type_from_employment,
    parse_base_salary,
    parse_experience_years,
    parse_job_posting,
    parse_locality,
    parse_sitemap_jobs,
    rich_row_to_club_row,
)

# Trimmed real vacanciessitemap.xml shape. <lastmod> is a modification
# stamp, not the posting date (see the scraper docstring) — ignored.
SITEMAP_XML = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://careers.iconplc.com/job/clinical-research-associate-in-japan-tokyo-jid-43682</loc>
    <lastmod>2026-07-15T00:01:23Z</lastmod>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://careers.iconplc.com/job/senior-tmf-specialist-in-india-chennai-jid-49011</loc>
    <lastmod>2026-08-25T16:01:20Z</lastmod>
  </url>
  <url>
    <loc>https://careers.iconplc.com/job/senior-clinical-systems-specialist-irt-in-regional-united-states-pra-jid-51472</loc>
    <lastmod>2026-08-04T13:34:39Z</lastmod>
  </url>
</urlset>"""

# Trimmed real detail-page JSON-LD (jid 49011). The live block parses
# clean; the parser must also survive Attrax growing the devnetjobsindia
# trailing-`}` bug, so that variant is tested separately below.
DETAIL_HTML = """
<script type="application/ld+json">{"@context": "https://schema.org/",
"@type": "JobPosting",
"title": "Senior TMF Specialist",
"description": "<p>Senior TMF Specialist - India, Chennai - Office Based -
5+ years of TMF experience; must include 2+ years of experience in periodic
file review with co-dependency checks.</p><p>ICON plc is a world-leading
healthcare intelligence and clinical research organization.</p><p><p>We are
currently seeking a Senior TMF Specialist to join our diverse and dynamic
team. Maintain and oversee the Trial Master File (TMF) ensuring inspection
readiness and compliance with ICH-GCP.</p></p>",
"identifier": {"@type": "PropertyValue", "name": "ICON", "value": "JR146301"},
"datePosted": "2026-03-11T14:02:49+00:00",
"validThrough": "2027-03-11T14:02:49+00:00",
"employmentType": ["Permanent"],
"industry": ["Regulatory Affairs"],
"hiringOrganization": {"@type": "Organization", "name": "ICON",
"sameAs": "https://careers.iconplc.com", "url": "https://careers.iconplc.com"},
"jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress",
"addressLocality": "India, Chennai"}}],
"url": "https://careers.iconplc.com/job/senior-tmf-specialist-in-india-chennai-jid-49011"}</script>
"""

# jid 51472, trimmed: a US regional (home-based) posting.
REGIONAL_POSTING = {
    "@type": "JobPosting",
    "title": "Senior Clinical Systems Specialist(IRT)",
    "description": "<p>Senior Clinical Systems Specialist (IRT)- Remote USA"
                   "</p><p>Leading the implementation and optimization of "
                   "clinical systems for efficient trial execution. Managing "
                   "third party IRT vendors. Writing UAT test scripts.</p>",
    "identifier": {"@type": "PropertyValue", "name": "ICON",
                   "value": "JR152608"},
    "datePosted": "2026-06-16T18:01:19+00:00",
    "validThrough": "2027-06-16T18:01:19+00:00",
    "employmentType": ["Permanent"],
    "industry": ["Clinical Data Management"],
    "hiringOrganization": {"@type": "Organization", "name": "ICON"},
    "jobLocation": [{"@type": "Place", "address": {
        "@type": "PostalAddress",
        "addressLocality": "Regional United States (PRA)"}}],
    "url": "https://careers.iconplc.com/job/senior-clinical-systems-"
           "specialist-irt-in-regional-united-states-pra-jid-51472",
}


class TestSitemapParser(unittest.TestCase):
    def test_urls_and_jids_newest_first(self):
        jobs = parse_sitemap_jobs(SITEMAP_XML)
        self.assertEqual([jid for _, jid in jobs],
                         ["51472", "49011", "43682"])
        self.assertEqual(
            jobs[1][0],
            "https://careers.iconplc.com/job/senior-tmf-specialist-in-india-"
            "chennai-jid-49011")

    def test_empty(self):
        self.assertEqual(parse_sitemap_jobs("<urlset></urlset>"), [])
        self.assertEqual(parse_sitemap_jobs(""), [])


class TestDetailParser(unittest.TestCase):
    def test_jobposting_parsed(self):
        posting = parse_job_posting(DETAIL_HTML)
        self.assertEqual(posting.get("title"), "Senior TMF Specialist")
        self.assertEqual(posting.get("industry"), ["Regulatory Affairs"])

    def test_trailing_brace_tolerated(self):
        # Defensive JSON-LD parsing: raw_decode must survive the stray `}`
        # bug seen on other boards' JSON-LD (devnetjobsindia).
        html = DETAIL_HTML.replace("jid-49011\"}</script>",
                                   "jid-49011\"}}</script>")
        self.assertEqual(parse_job_posting(html).get("title"),
                         "Senior TMF Specialist")

    def test_non_jobposting_skipped(self):
        html = ('<script type="application/ld+json">{"@type": "Organization"}'
                '</script>')
        self.assertEqual(parse_job_posting(html), {})


class TestLocality(unittest.TestCase):
    """Every shape is a real addressLocality from the 2026-08-27 sitemap."""

    def test_country_city(self):
        loc = parse_locality("India, Chennai")
        self.assertEqual((loc["country"], loc["country_code"], loc["city"]),
                         ("India", "IN", "Chennai"))
        self.assertFalse(loc["is_remote"])

    def test_country_only(self):
        loc = parse_locality("United States of America")
        self.assertEqual((loc["country"], loc["country_code"], loc["city"]),
                         ("United States of America", "US", ""))

    def test_bare_us_state(self):
        loc = parse_locality("California")
        self.assertEqual((loc["country_code"], loc["city"]),
                         ("US", "California"))

    def test_bare_georgia_is_the_us_state(self):
        # The country always arrives as "Georgia, Tbilisi".
        self.assertEqual(parse_locality("Georgia")["country_code"], "US")
        loc = parse_locality("Georgia, Tbilisi")
        self.assertEqual((loc["country_code"], loc["city"]),
                         ("GE", "Tbilisi"))

    def test_us_city_state_abbrev(self):
        loc = parse_locality("Chicago, IL")
        self.assertEqual((loc["country_code"], loc["city"]),
                         ("US", "Chicago"))

    def test_paren_site_tag_stripped(self):
        loc = parse_locality("US, Blue Bell (ICON)")
        self.assertEqual((loc["country_code"], loc["city"]),
                         ("US", "Blue Bell"))
        loc = parse_locality("Singapore, Singapore (Labs)")
        self.assertEqual((loc["country_code"], loc["city"]),
                         ("SG", "Singapore"))

    def test_regional_is_remote(self):
        loc = parse_locality("Regional United States (PRA)")
        self.assertTrue(loc["is_remote"])
        self.assertEqual(loc["country_code"], "US")
        loc = parse_locality("Regional Great Britain & Northern Ireland")
        self.assertTrue(loc["is_remote"])
        self.assertEqual(loc["country_code"], "GB")
        # jid 53154's real form: the paren strip leaves bare "Great Britain"
        loc = parse_locality("Regional Great Britain (Northern Ireland)")
        self.assertTrue(loc["is_remote"])
        self.assertEqual(loc["country_code"], "GB")

    def test_abbreviations_and_variants(self):
        self.assertEqual(parse_locality("UK, Reading")["country_code"], "GB")
        self.assertEqual(parse_locality("Korea, Seoul")["country_code"], "KR")
        self.assertEqual(parse_locality("Hong Kong, Hong Kong")["country_code"],
                         "HK")

    def test_unknown_token_never_guessed(self):
        loc = parse_locality("Atlantis")
        self.assertEqual((loc["country"], loc["country_code"], loc["city"]),
                         ("", "", "Atlantis"))

    def test_empty(self):
        loc = parse_locality("")
        self.assertEqual((loc["country"], loc["city"]), ("", ""))


class TestFieldParsers(unittest.TestCase):
    def test_experience_with_intervening_words(self):
        # jid 49011's real phrasing: the domain word sits between
        # "years of" and "experience".
        self.assertEqual(parse_experience_years(
            "5+ years of TMF experience; must include 2+ years of "
            "experience in periodic file review."), "5")
        self.assertEqual(parse_experience_years(
            "Minimum 3 years in clinical monitoring."), "3")
        self.assertEqual(parse_experience_years(
            "2-4 years' experience with EDC systems."), "2")
        self.assertEqual(parse_experience_years("No prior notice."), "")
        self.assertEqual(parse_experience_years(""), "")

    def test_base_salary_absent(self):
        # Every probed ICON page has baseSalary null.
        self.assertEqual(parse_base_salary(None),
                         ("Not Disclosed", "", "", "", ""))

    def test_base_salary_present(self):
        raw, lo, hi, ccy, period = parse_base_salary({
            "@type": "MonetaryAmount", "currency": "USD",
            "value": {"@type": "QuantitativeValue", "minValue": 90000,
                      "maxValue": 120000, "unitText": "YEAR"}})
        self.assertEqual((lo, hi, ccy, period),
                         (90000, 120000, "USD", "per_annum"))
        self.assertIn("USD", raw)

    def test_job_type(self):
        self.assertEqual(job_type_from_employment(["Permanent"]), "full_time")
        self.assertEqual(job_type_from_employment(["Part-time"]), "part_time")
        self.assertEqual(job_type_from_employment(None), "full_time")


class TestClassifier(unittest.TestCase):
    """Wiring into the shared taxonomy — the engine itself is covered by
    ../_shared/test_classification.py."""

    def test_tmf_role_kept(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("49011", posting,
                        "https://careers.iconplc.com/job/x-jid-49011")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "TMF")

    def test_cra_role_kept(self):
        row = {"title": "Clinical Research Associate",
               "industry": "Clinical Operations",
               "description": "On-site monitoring of clinical trials, site "
                              "initiation and close-out visits, ICH-GCP."}
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Clinical Research")

    def test_back_office_roles_dropped(self):
        # ICON also posts finance/IT/facilities roles — out of scope.
        for title, industry in (("Financial Analyst II", "Finance"),
                                ("Payroll Specialist", "Human Resources"),
                                ("Facilities Coordinator", "Facilities")):
            row = {"title": title, "industry": industry,
                   "description": "Corporate support role at a CRO."}
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")


class TestRowBuilding(unittest.TestCase):
    def test_row_and_club_row(self):
        posting = parse_job_posting(DETAIL_HTML)
        row = build_row("49011", posting,
                        "https://careers.iconplc.com/job/x-jid-49011")
        self.assertEqual(row["job_id"], "49011")
        self.assertEqual(row["requisition_id"], "JR146301")
        self.assertEqual(row["posted_date"], "2026-03-11")
        self.assertEqual(row["valid_through"], "2027-03-11")
        self.assertEqual(row["city"], "Chennai")
        self.assertEqual(row["country_code"], "IN")
        self.assertEqual(row["industry"], "Regulatory Affairs")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["experience_min_years"], "5")
        self.assertIn("Trial Master File", row["description"])
        self.assertNotIn("<", row["description"])  # HTML stripped
        # JSON-LD url wins over the sitemap URL passed in
        self.assertIn("jid-49011", row["job_url"])

        club = rich_row_to_club_row(row)
        self.assertEqual((club["country_name"], club["country_code"],
                          club["country_dial_code"]),
                         ("India", "IN", "+91"))
        self.assertEqual(club["city_name"], "Chennai")
        self.assertEqual(club["company_name"], "ICON")
        self.assertEqual(club["company_type"], "pharma")
        self.assertEqual(club["job_type"], "full_time")
        self.assertEqual(club["posted_at"], "2026-03-11")
        self.assertEqual(club["min_experience"], "5")
        self.assertEqual(club["min_salary"], "")   # board publishes none
        self.assertEqual(club["salary_currency"], "")

    def test_regional_row_exports_city_remote(self):
        # himalayas convention: a home-based/regional row ships city
        # "Remote", country columns from the coverage country.
        row = build_row("51472", REGIONAL_POSTING,
                        REGIONAL_POSTING["url"])
        self.assertTrue(row["is_remote"])
        club = rich_row_to_club_row(row)
        self.assertEqual(club["city_name"], "Remote")
        self.assertEqual((club["country_name"], club["country_code"]),
                         ("United States of America", "US"))

    def test_is_remote_survives_csv_round_trip(self):
        # After a CSV reload the flag is the string "True"/"False".
        row = build_row("51472", REGIONAL_POSTING, REGIONAL_POSTING["url"])
        row["is_remote"] = str(row["is_remote"])
        self.assertEqual(rich_row_to_club_row(row)["city_name"], "Remote")
        row["is_remote"] = "False"
        self.assertNotEqual(rich_row_to_club_row(row)["city_name"], "Remote")

    def test_country_only_row_falls_back_to_country_name(self):
        posting = dict(REGIONAL_POSTING,
                       jobLocation=[{"@type": "Place", "address": {
                           "addressLocality": "United States of America"}}])
        row = build_row("51001", posting, posting["url"])
        self.assertFalse(row["is_remote"])
        self.assertEqual(rich_row_to_club_row(row)["city_name"],
                         "United States of America")


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
