#!/usr/bin/env python3
"""Unit tests for the pharmatutor scraper's parsers and cutoff logic.

Run with plain:  python test_filters.py
Worked examples come from real postings on pharmatutor.org (Aug 2026):
the CMHO Surguja pharmacist notice, the Johnson & Johnson scientific
writer post, and the Amneal walk-in drive.
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
    company_from_title,
    compute_cutoff,
    extract_labeled_fields,
    parse_body_html,
    parse_end_date,
    parse_experience,
    parse_listing_cards,
    parse_location,
    parse_rss_items,
    parse_salary,
    parse_tags,
    parse_url_month,
    rich_row_to_club_row,
    url_month_before_cutoff,
)

# Trimmed real vacancies term-page markup (teasers inside the
# infinite-scroll wrapper; the sidebar outside it repeats the newest
# posts and must not leak in).
LISTING_HTML = """
<div data-drupal-views-infinite-scroll-content-wrapper
     class="views-infinite-scroll-content-wrapper clearfix"><div class="item-list">
 <ul>
  <li class="view-list-item">
   <div data-history-node-id="58828" class="node node--view-mode-teaser">
    <div class="post-thumbnail">
     <a href="/content/august-2026/job-for-pharmacist-at-cmho-surguja">
      <div class="field field--name-field-image"><img src="/x.png"/></div></a>
    </div>
    <h2 class="post-title">
     <a href="/content/august-2026/job-for-pharmacist-at-cmho-surguja">Job
     for Pharmacist at CMHO Surguja</a></h2>
   </div>
  </li>
  <li class="view-list-item">
   <div data-history-node-id="58712" class="node node--view-mode-teaser">
    <h2 class="post-title">
     <a href="/content/july-2026/some-older-post">Older Post</a></h2>
   </div>
  </li>
 </ul>
</div></div>
<ul class="js-pager__items pager">
 <li><a href="?page=1">Next</a></li>
</ul>
<div class="sidebar">
 <a href="/content/august-2026/sidebar-latest-post">Sidebar Latest</a>
</div>
"""

# Trimmed real detail page: the sidebar reuses the body-field class and the
# inlined CSS contains the literal strings "field--name-body" and
# ".post-tags" — the parsers must scope to the <article> element.
DETAIL_HTML = """
<style>.post-tags a{color:red}.field--name-body p{margin:0}</style>
<div class="sidebar">
 <div class="field field--name-body field__item">Main navigation Home</div>
</div>
<script type="application/ld+json">{"@context": "https://schema.org",
"@graph": [{"@type": "Article",
"headline": "Wanted Scientific Writing and Reporting Scientist at Johnson &amp; Johnson | Freshers may apply",
"datePublished": "2026-08-25T17:29:17+05:30",
"description": "The CPP SWR Scientist is responsible for writing documents."}]}
</script>
<article class="node node--type-article">
 <div class="post-content"><div class="node__content clearfix">
  <p>Johnson &amp; Johnson, we believe health is everything.</p>
  <p><em><strong>Post :</strong></em> Scientific Writing and Reporting
  Scientist</p>
  <ins class="adsbygoogle" data-ad-client="ca-pub-1">ad unit</ins>
  <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
  <p>Authors, coordinates, and facilitates timely reviews of Phase I
  clinical and regulatory documents such as protocols and CSRs.</p>
  <p><em><strong>Additional Information</strong></em><br>
  <strong>Experience :</strong> 12+ years<br>
  <strong>Qualification :</strong> Masters, PhD, MD<br>
  <strong>Location : </strong>Hyderabad / India<br>
  <strong>End Date : </strong>30th September 2026</p>
  <blockquote><p>Scientific Writing and Reporting Scientist :
  <a href="https://careers.example/apply">Apply Online</a></p></blockquote>
  <p>See All &nbsp;<a href="/x">B.Pharm Alerts</a>
  <a href="/y">M.Pharm Alerts</a><br>
  <a href="/z">Subscribe to Pharmatutor Job Alerts by Email</a></p>
 </div></div>
 <div class="post-tags clearfix"><div class="field field--name-field-tags">
  <div class="field__label">Tags</div><div class="field__items">
  <div class="field__item"><a href="/taxonomy/term/1754" hreflang="en">vacancies</a></div>
  <div class="field__item"><a href="/taxonomy/term/1929" hreflang="en">Scientist</a></div>
  <div class="field__item"><a href="/taxonomy/term/2515" hreflang="en">Johnson &amp; Johnson</a></div>
  <div class="field__item"><a href="/taxonomy/term/3341" hreflang="en">Company Jobs</a></div>
 </div></div></div>
</article>
"""

JOB_ID = ("august-2026/wanted-scientific-writing-and-reporting-scientist-"
          "at-johnson-and-johnson")


class TestListingParser(unittest.TestCase):
    def test_cards(self):
        # title anchors only, deduped against the image anchors, and the
        # sidebar outside the infinite-scroll wrapper must not leak in
        self.assertEqual(parse_listing_cards(LISTING_HTML), [
            ("august-2026/job-for-pharmacist-at-cmho-surguja",
             "Job for Pharmacist at CMHO Surguja"),
            ("july-2026/some-older-post", "Older Post"),
        ])

    def test_empty(self):
        self.assertEqual(parse_listing_cards(""), [])
        self.assertEqual(parse_listing_cards("<div>no cards</div>"), [])


class TestRssParser(unittest.TestCase):
    def test_items(self):
        xml = """<rss><channel>
        <item><title>Job for Pharmacist at CMHO Surguja</title>
        <link>https://www.pharmatutor.org/content/august-2026/job-for-pharmacist-at-cmho-surguja</link>
        <pubDate>Tue, 25 Aug 2026 11:59:17 +0000</pubDate></item>
        <item><title>Non-content page</title>
        <link>https://www.pharmatutor.org/articles</link></item>
        </channel></rss>"""
        self.assertEqual(parse_rss_items(xml), [
            ("august-2026/job-for-pharmacist-at-cmho-surguja",
             "Job for Pharmacist at CMHO Surguja")])

    def test_empty(self):
        self.assertEqual(parse_rss_items(""), [])


class TestUrlMonth(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_url_month("august-2026/some-slug"), (2026, 8))
        self.assertIsNone(parse_url_month("weird-path/some-slug"))

    def test_before_cutoff(self):
        # any day of July < any August cutoff; same month is never old
        self.assertTrue(url_month_before_cutoff("july-2026/x", "2026-08-12"))
        self.assertFalse(url_month_before_cutoff("august-2026/x", "2026-08-12"))
        self.assertTrue(url_month_before_cutoff("august-2026/x", "2026-09-01"))
        # unknown URL shapes are never judged old without a fetch
        self.assertFalse(url_month_before_cutoff("weird-path/x", "2026-08-12"))


class TestDetailParser(unittest.TestCase):
    def test_body_scoped_to_article(self):
        body = parse_body_html(DETAIL_HTML)
        self.assertIn("health is everything", body)
        # sidebar body-field clone and inlined CSS must not leak in
        self.assertNotIn("Main navigation", body)
        # ad units and their scripts are removed
        self.assertNotIn("adsbygoogle", body)
        # the pasted alerts footer is cut, the apply blockquote kept
        self.assertIn("Apply Online", body)
        self.assertNotIn("B.Pharm Alerts", body)

    def test_tags(self):
        self.assertEqual(parse_tags(DETAIL_HTML),
                         "vacancies; Scientist; Johnson & Johnson; Company Jobs")

    def test_labeled_fields(self):
        fields = extract_labeled_fields(parse_body_html(DETAIL_HTML))
        self.assertEqual(fields["position"],
                         "Scientific Writing and Reporting Scientist")
        self.assertEqual(fields["experience"], "12+ years")
        self.assertEqual(fields["qualification"], "Masters, PhD, MD")
        self.assertEqual(fields["location"], "Hyderabad / India")
        self.assertEqual(fields["end_date"], "30th September 2026")

    def test_end_date(self):
        self.assertEqual(parse_end_date("30th September 2026"), "2026-09-30")
        self.assertEqual(parse_end_date("September 8, 2026"), "2026-09-08")
        self.assertEqual(parse_end_date("as soon as possible"), "")
        self.assertEqual(parse_end_date(""), "")

    def test_salary(self):
        # the govt-notice shape
        s = parse_salary("Rs 16,500/- pm")
        self.assertEqual((s["salary_min"], s["salary_max"], s["salary_period"],
                          s["salary_currency"]),
                         (16500, 16500, "per_month", "INR"))
        s = parse_salary("3.5 - 5 LPA")
        self.assertEqual((s["salary_min"], s["salary_max"], s["salary_period"]),
                         (350000, 500000, "per_annum"))
        # no usable amount -> raw only, never invented
        self.assertEqual(parse_salary("As per company norms"),
                         {"salary_raw": "As per company norms"})
        self.assertEqual(parse_salary(""), {})

    def test_experience(self):
        self.assertEqual(parse_experience("12+ years"), (12, ""))
        self.assertEqual(parse_experience("1–7 years"), (1, 7))
        self.assertEqual(parse_experience("Fresher Only"), (0, 0))
        self.assertEqual(parse_experience(""), ("", ""))

    def test_company_from_title(self):
        # the "| Freshers may apply" suffix must not leak into the company
        self.assertEqual(
            company_from_title("Wanted Scientific Writing and Reporting "
                               "Scientist at Johnson & Johnson | Freshers "
                               "may apply"),
            "Johnson & Johnson")
        self.assertEqual(company_from_title("Job for Pharmacist at CMHO Surguja"),
                         "CMHO Surguja")
        self.assertEqual(
            company_from_title("SD College of Pharmacy and Vocational Studies "
                               "Invites Applications for post of Professor"),
            "SD College of Pharmacy and Vocational Studies")
        # the company-prefix verb shape ("<employer> Hiring/looking for ...")
        for title, company in (
                ("Sanofi Hiring Central Clinical Research Associate", "Sanofi"),
                ("Xogene looking for CTT Specialist", "Xogene"),
                ("Sandoz Require Regulatory Affairs Specialist", "Sandoz"),
                ("Macleods Walk in Drive for Research Associate", "Macleods"),
                ("MPMMCC Interview for Pharma candidates", "MPMMCC")):
            self.assertEqual(company_from_title(title), company)
        # generic lead words are role phrases, not employers
        self.assertEqual(company_from_title("Urgent Hiring for CRA"), "")
        self.assertEqual(company_from_title("Career for Scientific Officer"), "")

    def test_location(self):
        self.assertEqual(parse_location("Hyderabad / India"),
                         ("Hyderabad", "", "India", "IN", "+91"))
        self.assertEqual(parse_location("Ahmedabad, Gujarat"),
                         ("Ahmedabad", "Gujarat", "India", "IN", "+91"))
        # govt notices have no Location run — the state arrives as a tag
        self.assertEqual(
            parse_location("", tags="vacancies; Pharmacist; Chhattisgarh"),
            ("", "Chhattisgarh", "India", "IN", "+91"))
        self.assertEqual(parse_location("Dubai / UAE"),
                         ("Dubai", "", "UAE", "AE", "+971"))


class TestClassifier(unittest.TestCase):
    """Wiring into the shared taxonomy — the engine itself is covered by
    ../_shared/test_classification.py."""

    def _row(self, title, tags="", description="", company="x"):
        return {"title": title, "tags": tags, "description": description,
                "company": company, "needs_review": False}

    def test_medical_writer_kept(self):
        row = self._row(
            "Wanted Scientific Writing and Reporting Scientist at Johnson "
            "& Johnson",
            "vacancies; Pharma Jobs; Scientist; Company Jobs; R&D",
            "Responsible for writing Phase I clinical and regulatory "
            "documents such as protocols and CSRs; medical writing and "
            "QC of clinical study reports.")
        self.assertTrue(apply_classification(row))
        self.assertEqual(row["category"], "Non Clinical")
        self.assertEqual(row["sub_category"], "Medical Writer")

    def test_board_majority_dropped(self):
        """Most of the board is out of scope: dispensing pharmacists,
        manufacturing drives and fellowships are dropped, not relabelled."""
        for title, tags in (
                ("Job for Pharmacist at CMHO Surguja",
                 "vacancies; Pharmacist; D.Pharm; Government Jobs"),
                ("Walk In Drive for Pharma and Science candidates in "
                 "Manufacturing (Production) at Amneal Pharma",
                 "vacancies; Production; walk in jobs"),
                ("Pharma Fellowship at BRIC-National Centre for Cell Science",
                 "vacancies; Fellowship")):
            row = self._row(title, tags)
            self.assertFalse(apply_classification(row), title)
            self.assertEqual(row["category"], "")

    def test_missing_company_flagged(self):
        row = self._row(
            "Wanted Medical Writer",
            "vacancies",
            "Medical writing of clinical study reports and protocols.",
            company="")
        row["needs_review"] = True   # build_row sets this when company is ""
        if apply_classification(row):
            self.assertTrue(row["needs_review"])

    def test_company_type(self):
        self.assertEqual(classify_company_type("CMHO Surguja",
                                               "Job for Pharmacist at CMHO"),
                         "hospital")
        self.assertEqual(classify_company_type("Johnson & Johnson",
                                               "Wanted Scientist"),
                         "pharma")


class TestRowBuilding(unittest.TestCase):
    def test_row_and_club_row(self):
        row = build_row(JOB_ID, "card title", DETAIL_HTML)
        self.assertEqual(row["job_id"], JOB_ID)
        # JSON-LD headline beats the card title
        self.assertTrue(row["title"].startswith("Wanted Scientific"))
        self.assertEqual(row["posted_date"], "2026-08-25")
        self.assertEqual(row["end_date"], "2026-09-30")
        self.assertEqual(row["company"], "Johnson & Johnson")
        self.assertEqual(row["city"], "Hyderabad")
        self.assertEqual(row["min_experience"], 12)
        self.assertEqual(row["qualification"], "Masters, PhD, MD")
        self.assertFalse(row["needs_review"])
        self.assertNotIn("<", row["description"])   # HTML stripped

        club = rich_row_to_club_row(row)
        self.assertEqual(club["country_name"], "India")
        self.assertEqual(club["city_name"], "Hyderabad")
        self.assertEqual(club["posted_at"], "2026-08-25")
        self.assertEqual(club["min_experience"], "12")
        self.assertEqual(club["min_salary"], "")    # not disclosed
        self.assertEqual(club["application_url"],
                         "https://www.pharmatutor.org/content/" + JOB_ID)

    def test_no_location_falls_back_to_state_then_india(self):
        row = build_row("august-2026/x", "t", "<article><div class="
                        '"post-content">body text here</div></article>')
        self.assertEqual(rich_row_to_club_row(row)["city_name"], "India")


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
