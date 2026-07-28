#!/usr/bin/env python3
"""Unit tests for the moh.gov.sa scraper's parsers and filters.

Every worked example is copied verbatim from a live www.moh.gov.sa page
(announcement bodies, the Work For Us tables, the announcements listing markup).

Run with plain `python test_filters.py`.
"""

import unittest
from datetime import date

import scraper as s


# ---------------------------------------------------------------------------
# Hijri -> Gregorian
# ---------------------------------------------------------------------------

class TestHijriConversion(unittest.TestCase):
    """The tabular Islamic calendar tracks the Ministry's Umm al-Qura dates to
    ±1 day; these are the dates its own announcements print."""

    def assertWithinADay(self, got, expected_iso):
        self.assertIsNotNone(got)
        delta = abs((got - date.fromisoformat(expected_iso)).days)
        self.assertLessEqual(delta, 1,
                             "{} vs {} differ by {} days".format(
                                 got, expected_iso, delta))

    def test_announcement_1445_06_01(self):
        # ads-2023-12-14-001: "applications will be opened on Thursday
        # 01/06/1445 AH" — published the same day, 14 December 2023.
        self.assertWithinADay(s.hijri_to_gregorian(1445, 6, 1), "2023-12-14")

    def test_announcement_1444_10_10(self):
        # ads-2023-04-27-001: "from Sunday 10/10/1444 AH"
        self.assertWithinADay(s.hijri_to_gregorian(1444, 10, 10), "2023-04-30")

    def test_announcement_1444_06_03(self):
        # ads-2022-12-27-001: "starting from Tuesday, 03/06/1444H"
        self.assertWithinADay(s.hijri_to_gregorian(1444, 6, 3), "2022-12-27")

    def test_work_for_us_1445_04_20(self):
        # Work For Us: "Dentistry Vacancies … 20-4-1445H"
        self.assertWithinADay(s.hijri_to_gregorian(1445, 4, 20), "2023-11-04")

    def test_rejects_out_of_range(self):
        self.assertIsNone(s.hijri_to_gregorian(2023, 10, 4))   # Gregorian year
        self.assertIsNone(s.hijri_to_gregorian(1444, 13, 1))   # month 13
        self.assertIsNone(s.hijri_to_gregorian(1444, 6, 31))   # day 31


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

class TestDateParsing(unittest.TestCase):

    def test_page_date(self):
        self.assertEqual(s.parse_page_date("04 October 2023"), "2023-10-04")
        self.assertEqual(s.parse_page_date("10 June 2026"), "2026-06-10")
        self.assertEqual(s.parse_page_date(" 27 December 2022 "), "2022-12-27")

    def test_page_date_unparseable(self):
        self.assertEqual(s.parse_page_date(""), "")
        self.assertEqual(s.parse_page_date("Reading times"), "")
        self.assertEqual(s.parse_page_date("31 Smarch 2023"), "")

    def test_slug_date(self):
        self.assertEqual(s.parse_slug_date("ads-2023-10-04-001.aspx"),
                         "2023-10-04")
        self.assertEqual(s.parse_slug_date("Ads-2025-12-10-001.aspx"),
                         "2025-12-10")
        self.assertEqual(s.parse_slug_date("news-2025-06-26-001.aspx"),
                         "2025-06-26")

    def test_slug_date_typos_are_ignored(self):
        # real slug with a truncated year — must not blow up or invent a date
        self.assertEqual(s.parse_slug_date("ads-201-07-13-001.aspx"), "")
        self.assertEqual(s.parse_slug_date("employmentagreement.aspx"), "")

    def test_slug_date_invalid_day(self):
        self.assertEqual(s.parse_slug_date("ads-2023-02-31-001.aspx"), "")


class TestApplicationWindow(unittest.TestCase):

    def test_gregorian_window(self):
        body = ("The Ministry stated that applications will be available for "
                "graduates of this specialty, starting from Thursday, "
                "05/10/2023 until Saturday, 14/10/2023, for a period of ten "
                "days through the Ministry of Health's employment portal.")
        opens, closes, raw = s.parse_application_window(body)
        self.assertEqual(opens, "2023-10-05")
        self.assertEqual(closes, "2023-10-14")
        self.assertIn("05/10/2023", raw)

    def test_hijri_window(self):
        body = ("the application will be available for graduates of these "
                "specializations from Sunday 10/10/1444 AH to Thursday "
                "21/10/1444 AH through the Recruitment Portal")
        opens, closes, raw = s.parse_application_window(body)
        self.assertEqual(opens[:4], "2023")
        self.assertTrue(opens < closes)
        self.assertIn("1444", raw)

    def test_hijri_h_suffix_window(self):
        body = ("applications will be available starting from Tuesday, "
                "03/06/1444H, to Saturday, 07/06/1444H, for a period of five "
                "days")
        opens, closes, _ = s.parse_application_window(body)
        self.assertEqual(opens[:7], "2022-12")
        self.assertEqual(closes[:7], "2022-12")
        self.assertTrue(opens < closes)

    def test_single_open_date_leaves_close_empty(self):
        body = ("applications will be opened on Thursday 01/06/1445 AH, "
                "through the Ministry of Health's employment website")
        opens, closes, _ = s.parse_application_window(body)
        self.assertEqual(opens[:4], "2023")
        self.assertEqual(closes, "")

    def test_no_dates(self):
        self.assertEqual(s.parse_application_window("Requirements: a valid "
                                                    "professional registration "
                                                    "card."),
                         ("", "", ""))

    def test_out_of_order_dates_are_swapped(self):
        # the Dentistry Vacancies plan row prints 20-4-1445H … 29-3-1445H
        opens, closes, _ = s.parse_application_window(
            "period from 20/4/1445H to 29/3/1445H")
        self.assertTrue(opens < closes)


# ---------------------------------------------------------------------------
# Announcement classification (job vs tender/consultation)
# ---------------------------------------------------------------------------

class TestAnnouncementClassification(unittest.TestCase):

    JOB_TITLES = [
        "MOH Announces Resident Dentist Jobs for Bachelor Degree Holders",
        "MOH Announces Cardiac Perfusion Technician Jobs for Diploma Holders",
        "MOH Announces Vacancies for Deputy Doctor and Dental Consultants",
        "Ministry of Health Announces Vacancies for IT Specialties",
        "MOH Announces Vacant Jobs for Non Physician Specialist Jobs",
        "MOH: Extension of Application Period for Dentist jobs",
        "MOH Opens Registration For Seasonal Staff During Hajj 1444",
    ]

    NON_JOB_TITLES = [
        "MoH Launches E-Consultation on Sehhaty Medical Report Services",
        "MOH Announces Qualified Companies List for the Digital Twin Project",
        "MOH Postpones RCM Activation Tender in Primary Healthcare Centers",
        "MOH Announces Prequalification Results for Medical Imaging Archiving- PII",
        "\"Ministry of Health\" Announces Business and Procurement Plan 2025",
        "MoH Announces Outstanding Ministerial Checks Pending Collection",
        "Health Ministry Announces HealthThon 3 Results to Boost Pilgrim Care",
        "Showcase Your Brand at the \"Live Well\" Zone - GHE Riyadh 2025",
    ]

    def test_job_titles(self):
        for title in self.JOB_TITLES:
            is_job, _ = s.classify_announcement(title)
            self.assertTrue(is_job, title)

    def test_non_job_titles(self):
        for title in self.NON_JOB_TITLES:
            is_job, _ = s.classify_announcement(title)
            self.assertFalse(is_job, title)

    def test_deny_beats_body_language(self):
        # a tender page that happens to mention the employment portal
        is_job, _ = s.classify_announcement(
            "MOH Announces Qualified Companies List for the Digital Twin Project",
            "Companies may apply through the employment portal of the Ministry.")
        self.assertFalse(is_job)

    def test_job_recognised_from_body_is_flagged(self):
        is_job, review = s.classify_announcement(
            "MOH Announces Update to Rehab Prosthetics and Speech Therapy Fields",
            "The Ministry announces the availability of receiving employment "
            "application for holders of a bachelor's degree.")
        self.assertTrue(is_job)
        self.assertTrue(review)

    def test_training_registration_is_kept_and_flagged(self):
        is_job, review = s.classify_announcement(
            "MOH Opens Registration for Patient Care Technician Training Program",
            "The Ministry opens registration for the training program for "
            "patient care technicians.")
        self.assertTrue(is_job)
        self.assertTrue(review)

    def test_plain_vacancy_is_not_flagged(self):
        is_job, review = s.classify_announcement(
            "MOH Announces Resident Dentist Jobs for Bachelor Degree Holders",
            "The Ministry of Health (MOH) announces the start of receiving "
            "employment applications for holders of a bachelor's degree in "
            "(dentistry).")
        self.assertTrue(is_job)
        self.assertFalse(review)


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

class TestCategoryClassifier(unittest.TestCase):

    def test_doctors(self):
        for title in ("MOH Announces Vacancies for Deputy Doctor and Dental "
                      "Consultants",
                      "MOH Announces Resident Dentist Jobs for Bachelor Degree "
                      "Holders",
                      "Physicians & Nursing"):
            self.assertEqual(s.classify_category(title)[0], "doctors", title)

    def test_nurses(self):
        self.assertEqual(s.classify_category("Nursing Vacancies")[0], "nurses")
        self.assertEqual(s.classify_category("MOH Announces Midwifery Jobs")[0],
                         "nurses")

    def test_pharmacists(self):
        self.assertEqual(
            s.classify_category("MOH Announces Pharmacist Jobs for Bachelor "
                                "Holders")[0], "pharmacists")

    def test_pharmacy_wins_over_doctor_wording(self):
        # "Consultant" alone would read as a doctor; the specialty decides
        self.assertEqual(
            s.classify_category("MOH Announces Consultant Jobs",
                                "Clinical Pharmacy")[0], "pharmacists")

    def test_allied_health_is_non_clinical_without_review(self):
        for title, specialty in (
                ("MOH Announces Cardiac Perfusion Technician Jobs", ""),
                ("MOH Announces Non-Physician Specialist Jobs",
                 "Prosthetics; Physiotherapy; Occupational Therapy"),
                ("Respiratory Therapy and Prosthetics", "")):
            category, review = s.classify_category(title, specialty)
            self.assertEqual(category, "non_clinical", title)
            self.assertFalse(review, title)

    def test_corporate_posts_are_non_clinical(self):
        for title in ("Cyber Security", "Documents & Archives",
                      "Ministry of Health Announces Vacancies for IT "
                      "Specialties"):
            category, review = s.classify_category(title)
            self.assertEqual(category, "non_clinical", title)
            self.assertFalse(review, title)

    def test_unknown_title_is_flagged(self):
        category, review = s.classify_category("MOH Announces WA'ED Track")
        self.assertEqual(category, "non_clinical")
        self.assertTrue(review)

    def test_technologist_is_not_a_doctor(self):
        self.assertEqual(s.classify_category("Medical Technologist Jobs")[0],
                         "non_clinical")


# ---------------------------------------------------------------------------
# Body field extraction
# ---------------------------------------------------------------------------

class TestBodyFields(unittest.TestCase):

    NON_PHYSICIAN_BODY = (
        "The Ministry of Health announces the availability of receiving job "
        "applications for bachelor's degree holders in the following "
        "specializations: (Prosthetics - Physiotherapy - Occupational Therapy "
        "- Speech Therapy) For the non-physician specialist category.")

    def test_specialty_list(self):
        self.assertEqual(
            s.parse_specialties(self.NON_PHYSICIAN_BODY),
            "Prosthetics; Physiotherapy; Occupational Therapy; Speech Therapy")

    def test_specialty_single(self):
        body = ("announces the start of receiving employment applications for "
                "holders of a bachelor's degree in (dentistry).")
        self.assertEqual(s.parse_specialties(body), "dentistry")

    def test_specialty_from_specialty_phrase(self):
        body = ("job applications for holders of diploma qualification in the "
                "specialty of (cardiac perfusion technician).")
        self.assertEqual(s.parse_specialties(body), "cardiac perfusion technician")

    def test_specialty_absent(self):
        self.assertEqual(s.parse_specialties(
            "Requirements: a valid professional registration card."), "")

    def test_specialty_ignores_dated_parentheses(self):
        body = "Applications open on Thursday (01/06/1445 AH) for all regions."
        self.assertEqual(s.parse_specialties(body), "")

    def test_qualification(self):
        self.assertEqual(s.parse_qualification("", self.NON_PHYSICIAN_BODY),
                         "Bachelor's degree")
        self.assertEqual(
            s.parse_qualification("MOH Announces Cardiac Perfusion Technician "
                                  "Jobs for Diploma Holders", ""), "Diploma")
        self.assertEqual(s.parse_qualification("Nursing Vacancies", ""), "")

    def test_region(self):
        self.assertEqual(s.parse_region("posts across the Makkah region"),
                         "Makkah")
        self.assertEqual(s.parse_region("vacancies Kingdom-wide"), "")

    def test_job_type_defaults_to_full_time(self):
        self.assertEqual(s.parse_job_type("Nursing Vacancies", ""), "full_time")
        self.assertEqual(
            s.parse_job_type("Consultant post", "This is a part-time post."),
            "part_time")


class TestExperienceParser(unittest.TestCase):

    def test_range(self):
        raw, low, high = s.parse_experience(
            "Applicants must have 2 - 5 years of experience in the field.")
        self.assertEqual((low, high), ("2", "5"))
        self.assertIn("experience", raw)

    def test_single_minimum(self):
        _, low, high = s.parse_experience(
            "A minimum of 3 years experience is required.")
        self.assertEqual((low, high), ("3", ""))

    def test_application_period_is_not_experience(self):
        # "for a period of ten days" / Hijri years must never become years of
        # experience
        self.assertEqual(
            s.parse_experience(
                "starting from Tuesday, 03/06/1444H, to Saturday, 07/06/1444H, "
                "for a period of five days"),
            ("", "", ""))

    def test_unstated(self):
        self.assertEqual(s.parse_experience(
            "A detailed GOSI certificate, and a copy of the employment "
            "certificate, if any."), ("", "", ""))


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

ANNOUNCEMENT_HTML = """
<html><head><title>
    MOH Announcements
    -
    MOH Announces Resident Dentist Jobs for Bachelor Degree Holders
</title></head><body>
<span class="news_date" style="display:none;"><span id="pageDate">
<span id="ctl00_PlaceHolderMain_ctl04_lblDate">04 October 2023</span>
</span></span>
<div class="newscontent">
<div id="ctl00_PlaceHolderMain_ctl02_label" style='display:none'>Page Content</div>
<div class="ms-rtestate-field"><p>&#8203;&#8203;The Ministry of Health (MOH)
announces the start of receiving employment applications for holders of a
bachelor&#8217;s degree in (dentistry). The Ministry stated that applications
will be available for graduates of this specialty, starting from Thursday,
05/10/2023 until Saturday, 14/10/2023, for a period of ten days through the
Ministry of Health&#8217;s employment portal, to apply for this job
<a href="https://erp.moh.gov.sa/OA_HTML/IrcVisitor.jsp?L=AR">click here</a>.</p>
<script type="text/javascript">$(function(){ var html_content=""; });</script>
</div></div>
<div class="ms-hide">Last Update : 24 November 2025</div>
</body></html>
"""


class TestAnnouncementParsing(unittest.TestCase):

    def setUp(self):
        self.parsed = s.parse_announcement(
            ANNOUNCEMENT_HTML,
            "https://www.moh.gov.sa/en/ministry/mediacenter/ads/pages/"
            "ads-2023-10-04-001.aspx")

    def test_title_strips_section_prefix(self):
        self.assertEqual(
            self.parsed["title"],
            "MOH Announces Resident Dentist Jobs for Bachelor Degree Holders")

    def test_date_comes_from_page_not_last_update(self):
        self.assertEqual(self.parsed["posted_date"], "2023-10-04")

    def test_body_is_plain_text_without_scripts(self):
        body = self.parsed["body"]
        self.assertTrue(body.startswith("The Ministry of Health (MOH)"))
        self.assertNotIn("html_content", body)
        self.assertNotIn("<", body)
        self.assertNotIn("Last Update", body)

    def test_apply_link(self):
        self.assertEqual(self.parsed["apply_url"],
                         "https://erp.moh.gov.sa/OA_HTML/IrcVisitor.jsp?L=AR")

    def test_falls_back_to_listing_metadata(self):
        parsed = s.parse_announcement(
            "<html><head><title>MOH Announcements</title></head><body></body>"
            "</html>",
            "https://www.moh.gov.sa/en/ministry/mediacenter/ads/pages/"
            "ads-2022-11-30-001.aspx",
            fallback_title="MOH Announces Vacant Jobs for Non Physician "
                           "Specialist Jobs",
            fallback_date="2022-11-30")
        self.assertEqual(parsed["title"],
                         "MOH Announces Vacant Jobs for Non Physician "
                         "Specialist Jobs")
        self.assertEqual(parsed["posted_date"], "2022-11-30")

    def test_row_from_parsed_announcement(self):
        candidate = {"slug": "ads-2023-10-04-001.aspx",
                     "url": "https://www.moh.gov.sa/en/ministry/mediacenter/"
                            "ads/pages/ads-2023-10-04-001.aspx"}
        row = s.build_rich_row(candidate, self.parsed, needs_review=False)
        self.assertEqual(row["job_id"], "ads-2023-10-04-001")
        self.assertEqual(row["category"], "doctors")
        self.assertEqual(row["specialties"], "dentistry")
        self.assertEqual(row["qualification"], "Bachelor's degree")
        self.assertEqual(row["application_opens"], "2023-10-05")
        self.assertEqual(row["application_closes"], "2023-10-14")
        self.assertEqual(row["salary_raw"], "Not Disclosed")
        self.assertEqual(row["salary_min_monthly"], "")
        self.assertEqual(row["is_active"], "false")      # window closed in 2023


LISTING_HTML = """
<ul class="news_arch">
<li><a href="https://www.moh.gov.sa/en/Ministry/MediaCenter/Ads/Pages/ads-2026-06-10-001.aspx">
<img alt="MoH Launches E-Consultation on Sehhaty Medical Report Services" src="/x.png" />
<span class="date">10 June 2026</span>
<span class="ntitle">MoH Launches E-Consultation on Sehhaty Medical Report Services</span>
<span class="clr" /></a></li>
<li><a href="https://www.moh.gov.sa/en/Ministry/MediaCenter/Ads/Pages/Ads-2025-12-10-001.aspx">
<img alt="x" src="/x.png" /><span class="date">10 December 2025</span>
<span class="ntitle">MOH Announces Nursing Vacancies</span><span class="clr" /></a></li>
</ul>
"""


class TestListingParsing(unittest.TestCase):

    def test_listing_items(self):
        items = s._LISTING_ITEM_RE.findall(LISTING_HTML)
        self.assertEqual(len(items), 2)
        url, date_text, title = items[1]
        self.assertTrue(url.endswith("Ads-2025-12-10-001.aspx"))
        self.assertEqual(s.parse_page_date(date_text), "2025-12-10")
        self.assertEqual(s.clean_text(title), "MOH Announces Nursing Vacancies")

    def test_merge_prefers_listing_metadata(self):
        sitemap = [{"url": "https://www.moh.gov.sa/en/ministry/mediacenter/ads/"
                           "pages/ads-2025-12-10-001.aspx",
                    "slug": "ads-2025-12-10-001.aspx",
                    "slug_date": "2025-12-10", "lastmod": "2026-01-01",
                    "listing_title": "", "listing_date": ""}]
        listing = [{"url": "https://www.moh.gov.sa/en/Ministry/MediaCenter/Ads/"
                           "Pages/Ads-2025-12-10-001.aspx",
                    "slug": "Ads-2025-12-10-001.aspx",
                    "slug_date": "2025-12-10", "lastmod": "",
                    "listing_title": "MOH Announces Nursing Vacancies",
                    "listing_date": "2025-12-10"}]
        merged = s.merge_candidates(sitemap, listing)
        self.assertEqual(len(merged), 1)                 # case-insensitive slug
        self.assertEqual(merged[0]["listing_title"],
                         "MOH Announces Nursing Vacancies")


# ---------------------------------------------------------------------------
# Work For Us recruitment plan
# ---------------------------------------------------------------------------

WORK_FOR_US_HTML = """
<table><tr><th>&#8203;Announcement</th><th>&#8203;Start Date</th>
<th>&#8203;End Date</th><th>&#8203;End Date</th></tr>
<tr><td>&#8203;Physicians &amp; Nursing&#8203;</td><td>&#8203;Since Early This Year&#8203;</td>
<td>&#8203;Until the End of the Year</td><td>Still Running</td></tr></table>
<table><tr><th>&#8203;Announcement</th><th>&#8203;Start Date</th>
<th>&#8203;End Date</th><th>&#8203;End Date</th></tr>
<tr><td>&#8203;Dental Assistant&#8203;</td><td>&#8203;14-8-1443H</td>
<td>&#8203;28-8-1443H</td><td>&#8203;Expired</td></tr>
<tr><td>&#8203;Cyber Security</td><td>&#8203;13-6-1443H</td>
<td>&#8203;24-6-1443H</td><td>&#8203;Expired</td></tr>
<tr><td></td><td></td><td></td><td></td></tr></table>
"""


class TestWorkForUs(unittest.TestCase):

    def setUp(self):
        self.rows = s.parse_work_for_us(WORK_FOR_US_HTML)

    def test_skips_headers_and_empty_rows(self):
        self.assertEqual(len(self.rows), 3)
        self.assertEqual(self.rows[0]["announcement"], "Physicians & Nursing")

    def test_running_row_has_no_dates(self):
        running = self.rows[0]
        self.assertEqual(running["opens"], "")
        self.assertEqual(running["closes"], "")
        self.assertEqual(running["status"], "Still Running")

    def test_hijri_plan_dates(self):
        dental = self.rows[1]
        self.assertEqual(dental["announcement"], "Dental Assistant")
        self.assertEqual(dental["opens"][:4], "2022")    # 14-8-1443H
        self.assertTrue(dental["opens"] < dental["closes"])

    def test_running_row_is_active_and_categorised(self):
        row = s.build_plan_row(self.rows[0], first_seen="2026-07-27")
        self.assertEqual(row["is_active"], "true")
        self.assertEqual(row["category"], "doctors")     # Physicians & Nursing
        self.assertEqual(row["posted_date"], "2026-07-27")
        self.assertEqual(row["specialties"], "Physicians; Nursing")
        self.assertEqual(row["announcement_type"], "recruitment_plan")
        self.assertEqual(row["salary_raw"], "Not Disclosed")

    def test_expired_row_is_inactive(self):
        row = s.build_plan_row(self.rows[2], first_seen="2026-07-27")
        self.assertEqual(row["is_active"], "false")
        self.assertEqual(row["category"], "non_clinical")   # Cyber Security
        self.assertTrue(row["job_id"].startswith("workforus-2022-"))

    def test_job_ids_are_stable_and_unique(self):
        ids = {s.build_plan_row(r, "2026-07-27")["job_id"] for r in self.rows}
        self.assertEqual(len(ids), 3)
        again = s.build_plan_row(self.rows[1], "2026-07-27")["job_id"]
        self.assertIn(again, ids)


# ---------------------------------------------------------------------------
# Activity / time window
# ---------------------------------------------------------------------------

class TestIsActive(unittest.TestCase):

    TODAY = date(2026, 7, 27)

    def test_open_window(self):
        self.assertTrue(s.compute_is_active("2026-08-10", "2026-07-20",
                                            today=self.TODAY))

    def test_closed_window(self):
        self.assertFalse(s.compute_is_active("2023-10-14", "2023-10-04",
                                             today=self.TODAY))

    def test_still_running_beats_dates(self):
        self.assertTrue(s.compute_is_active("", "2019-01-01", "Still Running",
                                            today=self.TODAY))

    def test_expired_status_beats_dates(self):
        self.assertFalse(s.compute_is_active("2030-01-01", "2026-07-20",
                                             "Expired", today=self.TODAY))

    def test_no_window_uses_assumed_open_days(self):
        self.assertTrue(s.compute_is_active("", "2026-07-20", today=self.TODAY))
        self.assertFalse(s.compute_is_active("", "2026-01-01", today=self.TODAY))

    def test_nothing_known_is_inactive(self):
        self.assertFalse(s.compute_is_active("", "", today=self.TODAY))


class TestCutoff(unittest.TestCase):

    TODAY = date(2026, 7, 27)

    def test_first_run_uses_initial_window(self):
        self.assertEqual(s.compute_cutoff(None, 365, today=self.TODAY),
                         "2025-07-27")

    def test_first_run_whole_archive(self):
        self.assertIsNone(s.compute_cutoff(None, None, today=self.TODAY))

    def test_watermark_with_grace(self):
        import pandas as pd
        existing = pd.DataFrame({"posted_date": ["2026-06-10", "2026-07-20",
                                                 "2023-10-04"]})
        self.assertEqual(s.compute_cutoff(existing, 365, today=self.TODAY),
                         "2026-07-18")                   # newest - 2 days

    def test_empty_csv_falls_back_to_initial_window(self):
        import pandas as pd
        existing = pd.DataFrame({"posted_date": []})
        self.assertEqual(s.compute_cutoff(existing, 365, today=self.TODAY),
                         "2025-07-27")

    def test_within_window(self):
        self.assertTrue(s.within_window("2026-07-20", "2026-07-18"))
        self.assertTrue(s.within_window("2026-07-18", "2026-07-18"))
        self.assertFalse(s.within_window("2026-07-17", "2026-07-18"))

    def test_unknown_date_is_kept_for_inspection(self):
        # slug without a parseable date must not be silently dropped
        self.assertTrue(s.within_window("", "2026-07-18"))

    def test_no_cutoff_keeps_everything(self):
        self.assertTrue(s.within_window("2011-04-30", None))


# ---------------------------------------------------------------------------
# Club export
# ---------------------------------------------------------------------------

class TestClubRow(unittest.TestCase):

    def setUp(self):
        parsed = s.parse_announcement(
            ANNOUNCEMENT_HTML,
            "https://www.moh.gov.sa/en/ministry/mediacenter/ads/pages/"
            "ads-2023-10-04-001.aspx")
        candidate = {"slug": "ads-2023-10-04-001.aspx",
                     "url": "https://www.moh.gov.sa/en/ministry/mediacenter/"
                            "ads/pages/ads-2023-10-04-001.aspx"}
        self.rich = s.build_rich_row(candidate, parsed, needs_review=False)
        self.club = s.rich_row_to_club_row(self.rich)

    def test_columns_match_contract(self):
        self.assertEqual(list(self.club.keys()), s.CLUB_COLUMNS)

    def test_enums(self):
        self.assertIn(self.club["company_type"], {"hospital", "pharma"})
        self.assertIn(self.club["job_type"],
                      {"full_time", "part_time", "remote", "hybrid"})
        self.assertIn(self.club["category"],
                      {"doctors", "nurses", "pharmacists", "non_clinical"})
        self.assertIn(self.club["salary_period"], {"per_annum", "per_month", ""})
        self.assertIn(self.club["salary_currency"], {"INR", "USD", ""})

    def test_country_and_city_fallback(self):
        self.assertEqual(self.club["country_name"], "Saudi Arabia")
        self.assertEqual(self.club["country_code"], "SA")
        self.assertEqual(self.club["country_dial_code"], "+966")
        self.assertEqual(self.club["city_name"], "Riyadh")

    def test_application_url_is_the_announcement_not_the_unreachable_erp(self):
        self.assertTrue(self.club["application_url"].startswith(
            "https://www.moh.gov.sa/"))
        self.assertEqual(self.rich["apply_url"],
                         "https://erp.moh.gov.sa/OA_HTML/IrcVisitor.jsp?L=AR")

    def test_salary_is_never_invented(self):
        self.assertEqual(self.club["min_salary"], "")
        self.assertEqual(self.club["max_salary"], "")
        self.assertEqual(self.club["salary_period"], "")

    def test_expiry_from_application_window(self):
        self.assertEqual(self.club["expires_at"], "2023-10-14")
        self.assertEqual(self.club["posted_at"], "2023-10-04")

    def test_region_becomes_city_when_stated(self):
        row = dict(self.rich, region="Makkah")
        self.assertEqual(s.rich_row_to_club_row(row)["city_name"], "Makkah")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

class TestTextHelpers(unittest.TestCase):

    def test_clean_text_strips_zero_width_and_entities(self):
        self.assertEqual(s.clean_text("&#8203;&#8203;Physicians &amp; Nursing&#8203;"),
                         "Physicians & Nursing")

    def test_html_to_text_drops_scripts(self):
        self.assertEqual(
            s.html_to_text("<p>Apply now</p><script>var x=1;</script>"),
            "Apply now")

    def test_html_to_text_separates_blocks(self):
        self.assertEqual(s.html_to_text("<li>policies.</li><li>Perform</li>"),
                         "policies. Perform")

    def test_slugify(self):
        self.assertEqual(s.slugify("Physicians & Nursing"), "physicians-nursing")
        self.assertEqual(s.slugify("Documents & Archives"), "documents-archives")


if __name__ == "__main__":
    unittest.main(verbosity=2)
