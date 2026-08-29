#!/usr/bin/env python3
"""Unit tests for the ngobox scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real postings on ngobox.org (Aug 2026):
job_id 109408 (District Program Manager, Piramal Swasthya — Public Health),
109394 (Accounts & Administrative Assistant — out of scope, "No Deadline").
"""

import unittest
from datetime import date, timedelta

import pandas as pd

from scraper import (
    INITIAL_WINDOW_DAYS,
    WATERMARK_GRACE_DAYS,
    apply_classification,
    build_row,
    classify_company_type,
    compute_cutoff,
    parse_description,
    parse_detail,
    parse_experience_years,
    parse_listing_links,
    parse_location,
    parse_site_date,
    rich_row_to_club_row,
)

# Trimmed real job_listing.php card shape — the href is relative and may
# hold HTML entities; featured cards repeat the same id across pages.
LISTING_HTML = """
<p class="p_blue"><a target="_blank" href="job-detail_District-Program-Manager-Piramal-Swasthya_109408" class="card-title">District Program Manager</a></p>
<p class="p_blue"><a target="_blank" href="job-detail_Data-Analyst-&amp;-Insights-CSRBOX_109418" class="card-title">Data Analyst</a></p>
<div data-url="http://ngobox.org/job-detail_District-Program-Manager-Piramal-Swasthya_109408"></div>
<a href="job-detail_District-Program-Manager-Piramal-Swasthya_109408">again</a>
"""

# Trimmed real detail page (job_id 109408): fields sit on stable anchors,
# the posted date exists ONLY inside <title>, and the description runs from
# the row_section div to the commented-out ad script.
DETAIL_HTML = """<html><head>
<title>District Program Manager-Piramal Swasthya-25 Aug . 2026-NGO jobs in India, Jobs in NGO in India, Grants for NGOs in India, India CSR, CSR Projects in India</title>
</head><body>
<h1 class="card-header">District Program Manager</h1>
<h4 class="card-title" style="font-size:18px"><strong>Organization: </strong>Piramal Swasthya</h4>
<h2 class="card-text" ><strong>Apply By: </strong> 25 Sep  2026</h2>
<p class="card-text2"><strong>Location: </strong>
(Odisha)
</p>
<div class="row row_section font_chance12">
<p><strong>Job Title: District Program Manager</strong></p>
<p><strong>Location:</strong> Mayurbhanj District, Odisha</p>
<p>Public health programme management for the district health society:
maternal and child health, disease surveillance support and community
health worker coordination.</p>
<p><strong>Essential Qualifications</strong></p>
<p>Full-Time Master Degree &ndash; MSW, MPH/MHA, MRD or MBA</p>
<p>Minimum 4 years of experience in public health programmes.</p>
</div>
<div class="row row_section font_chance12">How to apply</div>
<p>Candidates may share their resume at HR.PSL@piramalswasthya.org</p>
<!---<script async src="//pagead2.googlesyndication.com/pagead/js"></script>-->
<div id="footer">footer</div>
</body></html>"""


class TestListingParser(unittest.TestCase):
    def test_links_deduped_and_absolute(self):
        links = parse_listing_links(LISTING_HTML)
        self.assertEqual([i for i, _ in links], ["109408", "109418"])
        self.assertEqual(links[0][1],
                         "https://ngobox.org/job-detail_District-Program-"
                         "Manager-Piramal-Swasthya_109408")
        # &amp; in the raw href is unescaped
        self.assertIn("&-Insights", links[1][1])

    def test_empty(self):
        self.assertEqual(parse_listing_links(""), [])
        self.assertEqual(parse_listing_links("<html></html>"), [])


class TestDetailParser(unittest.TestCase):
    def test_fields(self):
        fields = parse_detail(DETAIL_HTML)
        self.assertEqual(fields["title"], "District Program Manager")
        self.assertEqual(fields["company"], "Piramal Swasthya")
        self.assertEqual(fields["city"], "")        # state-wide posting
        self.assertEqual(fields["state"], "Odisha")
        # posted date comes ONLY from the <title> tag
        self.assertEqual(fields["posted_date"], "2026-08-25")
        self.assertEqual(fields["valid_through"], "2026-09-25")
        self.assertIn("disease surveillance", fields["description"])
        self.assertIn("How to apply", fields["description"])
        self.assertNotIn("<", fields["description"])   # HTML stripped
        self.assertNotIn("pagead2", fields["description"])

    def test_no_deadline_is_blank(self):
        html = DETAIL_HTML.replace(" 25 Sep  2026", " No Deadline")
        self.assertEqual(parse_detail(html)["valid_through"], "")

    def test_description_missing_block(self):
        self.assertEqual(parse_description("<html><body></body></html>"), "")

    def test_site_dates(self):
        # the site sprinkles stray dots/spaces between month and year
        self.assertEqual(parse_site_date("25 Aug . 2026"), "2026-08-25")
        self.assertEqual(parse_site_date("30 Sep. 2026"), "2026-09-30")
        self.assertEqual(parse_site_date(" 25 Sep  2026"), "2026-09-25")
        self.assertEqual(parse_site_date("No Deadline"), "")
        self.assertEqual(parse_site_date("31 Feb 2026"), "")   # impossible
        self.assertEqual(parse_site_date(""), "")

    def test_locations(self):
        # no space before the paren is the site's usual shape
        self.assertEqual(parse_location("Mumbai(Maharashtra)"),
                         ("Mumbai", "Maharashtra"))
        self.assertEqual(parse_location("Hubli-Dharwad (Karnataka)"),
                         ("Hubli-Dharwad", "Karnataka"))
        self.assertEqual(parse_location("(Odisha)"), ("", "Odisha"))
        self.assertEqual(parse_location(""), ("", ""))

    def test_experience_minimum_not_age_range(self):
        self.assertEqual(parse_experience_years(
            "Must be in the age group 25-40 years. Minimum 4 years of "
            "experience in public health programmes."), "4")
        self.assertEqual(parse_experience_years(
            "Must be in the age group 25-40 years."), "")
        self.assertEqual(parse_experience_years(
            "2–4 years of relevant experience"), "2")  # en-dash range: minimum
        self.assertEqual(parse_experience_years(
            "3+ years of relevant experience in M&E."), "3")
        self.assertEqual(parse_experience_years(""), "")


class TestClassifier(unittest.TestCase):
    """Wiring into the shared taxonomy — the engine itself is covered by
    ../_shared/test_classification.py."""

    def _row(self, title, description=""):
        return {"title": title, "description": description}

    def test_public_health_role_kept(self):
        row = self._row(
            "District Epidemiologist",
            "Disease surveillance and outbreak investigation for the "
            "district health society; epidemiology and public health "
            "reporting.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Public Health")
        self.assertEqual(row["sub_category"], "Epidemiology")

    def test_ngo_admin_role_dropped(self):
        """NGO-board supply is mostly out of scope: CSR, admin, education
        and livelihood roles are dropped, not relabelled."""
        for title in ("Accounts & Administrative Assistant",
                      "Trainer – Solar Panel Installation Technician",
                      "Manager - Learning Strategy and Projects"):
            row = self._row(title)
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_company_type(self):
        self.assertEqual(classify_company_type("Piramal Swasthya"), "hospital")
        self.assertEqual(classify_company_type("Clinical Research Labs Pvt Ltd"),
                         "pharma")


class TestRowBuilding(unittest.TestCase):
    def test_row_and_club_row(self):
        url = ("https://ngobox.org/job-detail_District-Program-Manager-"
               "Piramal-Swasthya_109408")
        row = build_row("109408", url, DETAIL_HTML)
        self.assertEqual(row["job_id"], "109408")
        self.assertEqual(row["title"], "District Program Manager")
        self.assertEqual(row["company"], "Piramal Swasthya")
        self.assertEqual(row["posted_date"], "2026-08-25")
        self.assertEqual(row["valid_through"], "2026-09-25")
        self.assertEqual(row["experience_min_years"], "4")
        self.assertEqual(row["job_url"], url)

        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_name"], "India")
        # state-wide posting: no city, city_name falls back to the state
        self.assertEqual(club["city_name"], "Odisha")
        self.assertEqual(club["company_type"], "hospital")
        self.assertEqual(club["posted_at"], "2026-08-25")
        self.assertEqual(club["min_experience"], "4")
        self.assertEqual(club["min_salary"], "")     # board publishes none
        self.assertEqual(club["application_url"], url)

    def test_unparseable_detail_has_no_title(self):
        row = build_row("109999", "https://ngobox.org/x", "")
        self.assertEqual(row["title"], "")   # main() counts detail_failed


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
