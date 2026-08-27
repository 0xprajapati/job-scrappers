#!/usr/bin/env python3
"""Scrape UNDP / UNCDF / UNV vacancies from the Oracle HCM Candidate
Experience REST API behind jobs.undp.org.

Data source
-----------
jobs.undp.org's "Jobs" tab is a ColdFusion shell (`cj_view_jobs.cfm`) whose
every card links OUT to an Oracle HCM Candidate Experience requisition at

    https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/
        CX_1/requisitions/job/<id>

The CFM shell is not scraped, and must not be: **jobs.undp.org/robots.txt is
`User-Agent: * / Disallow: /`** — the whole host is off limits. The Oracle
host is a different origin, serves no robots.txt at all (HTTP 404 => no
restrictions), and is where the data actually lives, so the compliant path
and the cheap path are the same path (probed 2026-08-27).

Oracle CE exposes its public candidate API unauthenticated, no key, no
cookie, no signed parameter:

  discovery (ONE request, every open requisition):
    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
        &finder=findReqs;siteNumber=CX_1,limit=500,sortBy=POSTING_DATES_DESC

  detail (one request per requisition):
    GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="<id>",siteNumber=CX_1

`finder` needs literal `;` and `,`, so URLs are built as strings rather than
through a params dict (requests would percent-encode the separators and
Oracle answers 400).

No browser capture, no HTML parsing, no anti-bot: a 7-request burst with no
delay returned 7x HTTP 200 (~0.9 s each). The board is small and global —
107 open requisitions across 57 countries on 2026-08-27 — so the whole
listing arrives in one response and `TotalJobsCount` confirms nothing was
paged off (limit=200 and limit=500 both return all 107).

Verified quirks
---------------
* The listing's `PostedDate` is exactly the date half of the detail's
  `ExternalPostedStartDate` (checked across sampled requisitions), so the
  time window is decided BEFORE any detail fetch — unlike devnetjobsindia,
  no detail request is ever spent on an out-of-window posting. seen_old.csv
  is therefore a record of what was skipped, not a fetch-saving skip list.
* The listing's own `PostingEndDate` is null on every row; the real closing
  date exists only on the detail as `ExternalPostedEndDate` -> valid_through.
* Oracle's taxonomy columns are dead on this tenant: `JobFamily`,
  `JobFunction`, `Category`, `ContractType` and `WorkerType` are null on all
  107 rows, and `skills` is `[]` on every detail. The curated topic signal
  lives in `requisitionFlexFields` instead, as `Practice Area`
  ("Health", "Governance", "Nature, Climate and Energy", ...). That is this
  board's analogue of devnetjobsindia's Relevant Sectors: kept raw in the
  `sectors` column and passed to classify_job as `skills`.
* `requisitionFlexFields` also carries `Agency` — the real employer is UNDP,
  UNCDF or UNV, not always UNDP — plus Grade, Bureau, Vacancy Type, Contract
  Duration and `Education & Work Experience`. The flex education string
  ("Master's Degree - 2 year(s) experience OR Bachelor's Degree - 4 year(s)
  experience") is a cleaner experience source than the prose and is parsed
  first, falling back to the description. Grade/Bureau/Vacancy Type are NOT
  stored: the rich CSV keeps the fleet's exact column contract.
* Every description is wrapped in UNDP's standard legal furniture — a
  "Tiered Approach" tier-eligibility preamble (~4-6k chars) and an
  "Equal opportunity / Sexual harassment / Scam alert" epilogue (~2k chars) —
  identical across postings and pure noise for both the classifier and the
  JD-rewrite pipeline. strip_boilerplate() cuts to the first real content
  heading and drops the epilogue, and refuses to cut when that would leave
  less than MIN_DESCRIPTION_CHARS (the internship template opens straight
  into BACKGROUND with a diversity paragraph and keeps some residue).
* Some country-office vacancies are posted wholly in French or Spanish, and
  the API exposes no English variant of a requisition — each is a
  single-language document. Those rows are KEPT (never dropped) but a cheap
  FR/ES stopword heuristic (looks_non_english) forces needs_review True so
  no untranslated description is silently published (UNDP-01).
* `PrimaryLocationCountry` is ISO alpha-2 except for two UNDP pseudo-codes:
  `U2` = "Home Based" (exported job_type "remote") and `U1` = "Multiple".
  Neither is a country, so country_name/code/dial stay blank rather than
  being guessed.
* The site's second tab, `cj_view_consultancies.cfm`, is NOT this source:
  those 191 rows are Individual-Contractor tenders on
  procurement-notices.undp.org, bid through the UNDP Quantum supplier
  portal, a separate system with a separate schema. Out of scope here; a
  candidate for its own scraper.
* UNDP publishes no salary anywhere -> blank salary columns, never invented.

Classification (shared taxonomy)
--------------------------------
Fetch wide, filter tight: every open requisition is enumerated — no Practice
Area facet, no health keyword filter — and the keep/drop and labeling
decision belongs entirely to the ONE shared classifier,
_shared/classification.classify_job(title, skills, description):

* skills      = the Practice Area flex field
* description = the boilerplate-stripped description text

in_scope False -> DROPPED, counted excluded_out_of_scope, full row appended
to out-of-scope.csv. in_scope True fills category/sub_category/role_family
and the score-trace columns; needs_review True keeps the row AND appends it
to needs_review.csv. This scraper never assigns a category itself.

Outputs
-------
* undp_jobs.csv        — rich cumulative store (dedup key: job_id).
* ../../jobs_csv/<DD-MM-YYYY>/undp.csv — the same jobs in CLUB_COLUMNS.
* seen_old_ids.csv     — out-of-window ids and their dates.
* out-of-scope.csv     — dropped rows + their skip list (reversible).
* needs_review.csv     — kept but flagged.

Time window: first run keeps requisitions posted in the last
INITIAL_WINDOW_DAYS (14); later runs keep only those newer than the newest
stored posted_date minus WATERMARK_GRACE_DAYS of overlap.

Run `python scraper.py --help` for options.
"""

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# The one shared classifier (see ../../instructions/taxonomy-migration-spec.md).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "_shared"))
from classification import classify_job, extract_qualification, CLUB_COLUMNS

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SITE = "undp"

# The Oracle HCM Candidate Experience tenant that jobs.undp.org links into.
CE_HOST = "https://estm.fa.em2.oraclecloud.com"
ROBOTS_URL = CE_HOST + "/robots.txt"
REST_BASE = CE_HOST + "/hcmRestApi/resources/latest"
SITE_NUMBER = "CX_1"

# jobs.undp.org is Disallow: / for every user agent. It is listed here only
# so assert_no_disallowed_host() can prove no request is ever aimed at it.
DISALLOWED_HOSTS = ("jobs.undp.org",)

LIST_URL = (
    REST_BASE + "/recruitingCEJobRequisitions"
    "?onlyData=true"
    "&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
    "&finder=findReqs;siteNumber=" + SITE_NUMBER +
    ",limit={limit},sortBy=POSTING_DATES_DESC"
)
DETAIL_URL = (
    REST_BASE + "/recruitingCEJobRequisitionDetails"
    "?expand=all&onlyData=true"
    "&finder=ById;Id=%22{job_id}%22,siteNumber=" + SITE_NUMBER
)
JOB_URL_TEMPLATE = (
    CE_HOST + "/hcmUI/CandidateExperience/en/sites/" + SITE_NUMBER +
    "/requisitions/job/{}"
)

# One page holds the whole board (107 open requisitions on 2026-08-27);
# 500 leaves a wide margin and TotalJobsCount is asserted against it.
LIST_PAGE_LIMIT = 500

USER_AGENT = (
    "HealthCareersJobScraper/1.0 (+https://github.com/0xprajapati/job-scrappers)"
)

# UN vacancies stay open ~2 weeks and trickle in; a 14-day first window
# matches the rest of the development-sector fleet. No salary filter
# (master spec): salaries captured, never filtered on — this board has none.
INITIAL_WINDOW_DAYS = 14
WATERMARK_GRACE_DAYS = 2

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 3.0
DESCRIPTION_MAX_CHARS = 20_000
# A boilerplate cut that would leave less than this is refused as a
# mis-detection and the uncut text is kept.
MIN_DESCRIPTION_CHARS = 400
# How far back strip_boilerplate() will look for a sentence end when an
# epilogue cut lands mid-sentence.
_DANGLING_CLAUSE_CHARS = 200

SCRAPER_DIR = Path(__file__).resolve().parent
RICH_CSV = str(SCRAPER_DIR / "undp_jobs.csv")
SEEN_OLD_CSV = str(SCRAPER_DIR / "seen_old_ids.csv")
OUT_OF_SCOPE_CSV = str(SCRAPER_DIR / "out-of-scope.csv")
NEEDS_REVIEW_CSV = str(SCRAPER_DIR / "needs_review.csv")
CLUB_CSV_DIR = Path(__file__).resolve().parents[2] / "jobs_csv"

RICH_COLUMNS = [
    "source", "job_id", "title", "company", "city", "state", "country",
    "sectors", "is_rfp", "category", "sub_category", "role_family",
    "all_families", "family_scores", "family_confidence", "matched_in",
    "needs_review", "experience_min_years", "posted_date", "valid_through",
    "description", "job_url", "scraped_at",
]

log = logging.getLogger("undp_scraper")

# ISO alpha-2 -> (country name, dial code) for the UN duty-station universe,
# inverted from the table already vetted in ../impactpool/ and extended with
# the three duty stations UNDP posts in that it did not cover. A code that is
# not here exports blank name/dial — never guessed.
COUNTRY_BY_CODE = {
    "AE": ("United Arab Emirates", "+971"), "AF": ("Afghanistan", "+93"),
    "AL": ("Albania", "+355"), "AM": ("Armenia", "+374"),
    "AO": ("Angola", "+244"), "AR": ("Argentina", "+54"),
    "AT": ("Austria", "+43"), "AU": ("Australia", "+61"),
    "AZ": ("Azerbaijan", "+994"), "BA": ("Bosnia and Herzegovina", "+387"),
    "BB": ("Barbados", "+1"), "BD": ("Bangladesh", "+880"),
    "BE": ("Belgium", "+32"), "BF": ("Burkina Faso", "+226"),
    "BG": ("Bulgaria", "+359"), "BI": ("Burundi", "+257"),
    "BJ": ("Benin", "+229"), "BO": ("Bolivia", "+591"),
    "BR": ("Brazil", "+55"), "BW": ("Botswana", "+267"),
    "CA": ("Canada", "+1"),
    "CD": ("Democratic Republic of the Congo", "+243"),
    "CF": ("Central African Republic", "+236"), "CG": ("Congo", "+242"),
    "CH": ("Switzerland", "+41"), "CI": ("Côte d'Ivoire", "+225"),
    "CL": ("Chile", "+56"), "CM": ("Cameroon", "+237"),
    "CN": ("China", "+86"), "CO": ("Colombia", "+57"),
    "CR": ("Costa Rica", "+506"), "CU": ("Cuba", "+53"),
    "CV": ("Cabo Verde", "+238"), "CY": ("Cyprus", "+357"),
    "CZ": ("Czechia", "+420"), "DE": ("Germany", "+49"),
    "DJ": ("Djibouti", "+253"), "DK": ("Denmark", "+45"),
    "DO": ("Dominican Republic", "+1"), "DZ": ("Algeria", "+213"),
    "EC": ("Ecuador", "+593"), "EE": ("Estonia", "+372"),
    "EG": ("Egypt", "+20"), "ER": ("Eritrea", "+291"), "ES": ("Spain", "+34"),
    "ET": ("Ethiopia", "+251"), "FI": ("Finland", "+358"),
    "FJ": ("Fiji", "+679"), "FR": ("France", "+33"), "GA": ("Gabon", "+241"),
    "GB": ("United Kingdom", "+44"), "GE": ("Georgia", "+995"),
    "GH": ("Ghana", "+233"), "GM": ("Gambia", "+220"),
    "GN": ("Guinea", "+224"), "GR": ("Greece", "+30"),
    "GT": ("Guatemala", "+502"), "GW": ("Guinea-Bissau", "+245"),
    "HN": ("Honduras", "+504"), "HR": ("Croatia", "+385"),
    "HT": ("Haiti", "+509"), "HU": ("Hungary", "+36"),
    "ID": ("Indonesia", "+62"), "IE": ("Ireland", "+353"),
    "IL": ("Israel", "+972"), "IN": ("India", "+91"), "IQ": ("Iraq", "+964"),
    "IR": ("Iran", "+98"), "IT": ("Italy", "+39"), "JM": ("Jamaica", "+1"),
    "JO": ("Jordan", "+962"), "JP": ("Japan", "+81"), "KE": ("Kenya", "+254"),
    "KG": ("Kyrgyzstan", "+996"), "KH": ("Cambodia", "+855"),
    "KI": ("Kiribati", "+686"), "KR": ("South Korea", "+82"),
    "KW": ("Kuwait", "+965"), "KZ": ("Kazakhstan", "+7"),
    "LA": ("Laos", "+856"), "LB": ("Lebanon", "+961"),
    "LK": ("Sri Lanka", "+94"), "LR": ("Liberia", "+231"),
    "LS": ("Lesotho", "+266"), "LT": ("Lithuania", "+370"),
    "LU": ("Luxembourg", "+352"), "LV": ("Latvia", "+371"),
    "LY": ("Libya", "+218"), "MA": ("Morocco", "+212"),
    "MD": ("Moldova", "+373"), "ME": ("Montenegro", "+382"),
    "MG": ("Madagascar", "+261"), "MK": ("North Macedonia", "+389"),
    "ML": ("Mali", "+223"), "MM": ("Myanmar", "+95"),
    "MN": ("Mongolia", "+976"), "MR": ("Mauritania", "+222"),
    "MT": ("Malta", "+356"), "MW": ("Malawi", "+265"),
    "MX": ("Mexico", "+52"), "MY": ("Malaysia", "+60"),
    "MZ": ("Mozambique", "+258"), "NA": ("Namibia", "+264"),
    "NE": ("Niger", "+227"), "NG": ("Nigeria", "+234"),
    "NI": ("Nicaragua", "+505"), "NL": ("Netherlands", "+31"),
    "NO": ("Norway", "+47"), "NP": ("Nepal", "+977"),
    "NZ": ("New Zealand", "+64"), "OM": ("Oman", "+968"),
    "PA": ("Panama", "+507"), "PE": ("Peru", "+51"),
    "PG": ("Papua New Guinea", "+675"), "PH": ("Philippines", "+63"),
    "PK": ("Pakistan", "+92"), "PL": ("Poland", "+48"),
    "PS": ("Palestine", "+970"), "PT": ("Portugal", "+351"),
    "PY": ("Paraguay", "+595"), "QA": ("Qatar", "+974"),
    "RO": ("Romania", "+40"), "RS": ("Serbia", "+381"),
    "RU": ("Russia", "+7"), "RW": ("Rwanda", "+250"),
    "SA": ("Saudi Arabia", "+966"), "SB": ("Solomon Islands", "+677"),
    "SD": ("Sudan", "+249"), "SE": ("Sweden", "+46"),
    "SG": ("Singapore", "+65"), "SI": ("Slovenia", "+386"),
    "SK": ("Slovakia", "+421"), "SL": ("Sierra Leone", "+232"),
    "SN": ("Senegal", "+221"), "SO": ("Somalia", "+252"),
    "SS": ("South Sudan", "+211"), "SV": ("El Salvador", "+503"),
    "SY": ("Syria", "+963"), "SZ": ("Eswatini", "+268"),
    "TD": ("Chad", "+235"), "TG": ("Togo", "+228"), "TH": ("Thailand", "+66"),
    "TJ": ("Tajikistan", "+992"), "TL": ("Timor-Leste", "+670"),
    "TN": ("Tunisia", "+216"), "TR": ("Turkey", "+90"),
    "TZ": ("Tanzania", "+255"), "UA": ("Ukraine", "+380"),
    "UG": ("Uganda", "+256"), "US": ("United States", "+1"),
    "UY": ("Uruguay", "+598"), "UZ": ("Uzbekistan", "+998"),
    "VE": ("Venezuela", "+58"), "VN": ("Vietnam", "+84"),
    "VU": ("Vanuatu", "+678"), "YE": ("Yemen", "+967"),
    "ZA": ("South Africa", "+27"), "ZM": ("Zambia", "+260"),
    "ZW": ("Zimbabwe", "+263"),
}

# UNDP's non-geographic pseudo "countries": these are not ISO codes and must
# never be resolved to a country.
PSEUDO_COUNTRY_CODES = {"U1": "Multiple", "U2": "Home Based"}

# country name -> (ISO alpha-2, dial code), for the club export, which only
# has the stored country name to work from.
CODE_BY_COUNTRY = {name: (code, dial)
                   for code, (name, dial) in COUNTRY_BY_CODE.items()}

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text):
    return _WS_RE.sub(" ", html_lib.unescape(str(text or ""))).strip()


def strip_html(markup):
    markup = re.sub(r"</?(p|br|li|ul|ol|div|h\d|tr|table)[^>]*>", " ",
                    markup or "", flags=re.IGNORECASE)
    return clean_text(_TAG_RE.sub(" ", markup))


# ---- description boilerplate -----------------------------------------------

# UNDP wraps every vacancy in the same legal furniture. The preamble is the
# tier-eligibility block; the real posting starts at the first of these
# headings.
_CONTENT_START_RES = [
    # English
    re.compile(r"\bProject Description\b", re.IGNORECASE),
    re.compile(r"\bPosition Summary\b", re.IGNORECASE),
    re.compile(r"\bScope of Work\b", re.IGNORECASE),
    re.compile(r"\bDuties and Responsibilities\b", re.IGNORECASE),
    re.compile(r"\bBackground\b", re.IGNORECASE),
    # French — UNDP posts country-office vacancies in FR and ES as well,
    # with the same furniture translated (PNUD/FENU/VNU).
    re.compile(r"\bContexte\b", re.IGNORECASE),
    re.compile(r"\bHistorique\b", re.IGNORECASE),
    re.compile(r"\bDescription du projet\b", re.IGNORECASE),
    re.compile(r"\bFonctions et responsabilit", re.IGNORECASE),
    re.compile(r"\bPort[ée]e des travaux\b", re.IGNORECASE),
    # Spanish
    re.compile(r"\bAntecedentes\b", re.IGNORECASE),
    re.compile(r"\bDescripci[óo]n del (?:proyecto|puesto)\b", re.IGNORECASE),
    re.compile(r"\bFunciones y responsabilidades\b", re.IGNORECASE),
    re.compile(r"\b[ÁA]mbito de trabajo\b", re.IGNORECASE),
    re.compile(r"\bAlcance del trabajo\b", re.IGNORECASE),
]
# The preamble is only cut when the text actually opens with it, and only
# when a content heading follows inside this many characters.
_PREAMBLE_RES = [
    re.compile(r"^\s*Tiered?\s+(?:Approach|Descri?ption)", re.IGNORECASE),
    re.compile(r"^\s*Tier\s+Decription", re.IGNORECASE),
    re.compile(r"^\s*Approche\s+Tiers", re.IGNORECASE),
    re.compile(r"^\s*Aproximaci[óo]n\s+por\s+Niveles", re.IGNORECASE),
    re.compile(r"^\s*Enfoque\s+por\s+niveles", re.IGNORECASE),
]
_PREAMBLE_SEARCH_CHARS = 9_000
# The tier preamble is never shorter than this, so a content heading found
# before it is a prose match, not the real start of the posting.
_PREAMBLE_MIN_CHARS = 200

# Everything from the first of these to the end is the standard footer.
_EPILOGUE_RES = [
    # English
    re.compile(r"\bEqual opportunity\b", re.IGNORECASE),
    re.compile(r"\bSexual harassment, exploitation, and abuse of authority\b",
               re.IGNORECASE),
    re.compile(r"\bRight to select multiple candidates\b", re.IGNORECASE),
    re.compile(r"\bUse of AI by candidates\b", re.IGNORECASE),
    re.compile(r"\bScam (?:alert|warning)\b", re.IGNORECASE),
    re.compile(r"\bUNDP is an equal opportunity\b", re.IGNORECASE),
    re.compile(r"\bThe United Nations does not charge\b", re.IGNORECASE),
    re.compile(r"\bUNDP does not charge a fee\b", re.IGNORECASE),
    # French
    re.compile(r"\b[ÉE]galit[ée] des chances\b", re.IGNORECASE),
    re.compile(r"\bHarc[èe]lement sexuel\b", re.IGNORECASE),
    re.compile(r"\bDroit de s[ée]lectionner plusieurs candidat", re.IGNORECASE),
    re.compile(r"\bAlerte (?:[àa] l'escroquerie|aux escroqueries)\b",
               re.IGNORECASE),
    re.compile(r"\bLe PNUD ne facture aucun", re.IGNORECASE),
    re.compile(r"\bUtilisation de l'IA par les candidat", re.IGNORECASE),
    # Spanish
    re.compile(r"\bIgualdad de oportunidades\b", re.IGNORECASE),
    re.compile(r"\bAcoso sexual, explotaci[óo]n y abuso de autoridad\b",
               re.IGNORECASE),
    re.compile(r"\bDerecho a seleccionar (?:a )?(?:m[áa]s de un|varios)",
               re.IGNORECASE),
    re.compile(r"\bAlerta de estafa\b", re.IGNORECASE),
    re.compile(r"\bEl PNUD no cobra\b", re.IGNORECASE),
    re.compile(r"\bUso de IA por parte de los candidatos\b", re.IGNORECASE),
    re.compile(r"\bPSEAH?\b[:\s]", re.IGNORECASE),
]


def _first_match(regexes, text, start=0):
    """Lowest match position among `regexes` at/after `start`, or -1."""
    positions = [m.start() for m in (r.search(text, start) for r in regexes)
                 if m]
    return min(positions) if positions else -1


def strip_boilerplate(text):
    """Drop UNDP's tier preamble and legal epilogue from a description.

    Conservative on purpose: either cut is refused if it would leave less
    than MIN_DESCRIPTION_CHARS, so a template this does not recognise keeps
    its full text rather than losing the posting.
    """
    text = clean_text(text)
    if not text:
        return ""

    if any(r.match(text) for r in _PREAMBLE_RES):
        head = _first_match(_CONTENT_START_RES, text[:_PREAMBLE_SEARCH_CHARS],
                            _PREAMBLE_MIN_CHARS)
        if head > 0 and len(text) - head >= MIN_DESCRIPTION_CHARS:
            text = text[head:]

    tail = _first_match(_EPILOGUE_RES, text)
    if tail > 0 and tail >= MIN_DESCRIPTION_CHARS:
        text = text[:tail].rstrip()
        # Some footers open mid-sentence ("...comprometida con la Igualdad
        # de oportunidades"), so the cut can leave a dangling clause. Fall
        # back to the last sentence end when one is close behind.
        if text and text[-1] not in ".!?":
            stop = max(text.rfind(c) for c in ".!?")
            if stop >= MIN_DESCRIPTION_CHARS and len(text) - stop <= _DANGLING_CLAUSE_CHARS:
                text = text[:stop + 1]

    return text.strip()


# ---- listing ----------------------------------------------------------------

def parse_requisition_list(payload):
    """(requisitions, total_count) from a recruitingCEJobRequisitions body.

    Oracle wraps the search result — including the requisition array — in a
    single-element `items` list.
    """
    items = (payload or {}).get("items") or []
    if not items:
        return [], 0
    result = items[0] or {}
    return (result.get("requisitionList") or []), result.get("TotalJobsCount")


def parse_requisition_detail(payload):
    """The single requisition object of a ...RequisitionDetails body, or {}."""
    items = (payload or {}).get("items") or []
    return items[0] if items and isinstance(items[0], dict) else {}


def parse_flex_fields(detail):
    """`requisitionFlexFields` as a {Prompt: Value} dict."""
    out = {}
    for field in (detail.get("requisitionFlexFields") or []):
        if not isinstance(field, dict):
            continue
        prompt, value = clean_text(field.get("Prompt")), clean_text(field.get("Value"))
        if prompt and value:
            out[prompt] = value
    return out


def parse_iso_date(text):
    """The date half of an Oracle timestamp ("2026-08-27T04:58:17+00:00")."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", clean_text(text))
    return match.group(1) if match else ""


def parse_location(primary_location, country_code):
    """(city, country_name) from "New Delhi, India" + its ISO alpha-2 code.

    The trailing country segment is dropped when it merely repeats the
    country the code already names; a single-segment string ("Barbados",
    "Home Based") stays as the city, which is how the club schema's
    city_name behaves elsewhere in the fleet.
    """
    location = clean_text(primary_location)
    code = clean_text(country_code).upper()
    country = "" if code in PSEUDO_COUNTRY_CODES else \
        COUNTRY_BY_CODE.get(code, ("", ""))[0]

    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) > 1 and country and parts[-1].lower() == country.lower():
        parts = parts[:-1]
    return ", ".join(parts), country


# ---- experience -------------------------------------------------------------

# The flex "Education & Work Experience" / "Other Criteria" strings state the
# bar directly: "Master's Degree - 2 year(s) experience OR Bachelor's Degree -
# 4 year(s) experience". Every alternative is a valid route in, so the
# minimum is the entry requirement.
_FLEX_YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+)?\s*year", re.IGNORECASE)

# Prose fallback, same grounded patterns as the rest of the fleet: never
# inferred, only lifted when the posting says it.
_EXPERIENCE_RES = [
    re.compile(r"(?:minimum|at\s?least)\s+(?:of\s+)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?",
               re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*years?['’s]*\s*(?:of\s+)?"
               r"(?:relevant\s+|work(?:ing)?\s+|professional\s+)?experience",
               re.IGNORECASE),
]


def parse_experience_years(flex_fields, description):
    """Stated minimum years of experience, or "" when the posting states none.

    The structured flex fields win over the prose; a posting that names only
    a degree ("Master's Degree") yields "" rather than 0.
    """
    for prompt in ("Education & Work Experience", "Other Criteria"):
        years = [int(y) for y in _FLEX_YEARS_RE.findall(flex_fields.get(prompt, ""))]
        years = [y for y in years if 0 < y <= 30]
        if years:
            return str(min(years))

    for regex in _EXPERIENCE_RES:
        match = regex.search(description or "")
        if match:
            years = int(match.group(1))
            if 0 < years <= 30:
                return str(years)
    return ""


# ---- language (UNDP-01) -----------------------------------------------------

# UNDP country offices post some vacancies wholly in French or Spanish
# ("Le Gouvernement de la République du Tchad..."). Oracle CE requisitions on
# this tenant are single-language documents: the /en/ site serves the one
# ExternalDescriptionStr the office wrote, and the API exposes no per-language
# variant of a requisition to prefer (the FR/ES boilerplate handling above
# exists precisely because those postings arrive untranslated). So a
# non-English row is KEPT — never dropped — but flagged needs_review so it is
# not silently published untranslated.
#
# Detection is a cheap stopword count: high-frequency FR/ES function words in
# the first LANGUAGE_SNIFF_CHARS of the description. English prose contains
# almost none of them as standalone words, while any real FR/ES paragraph
# hits the threshold within a few sentences.
_NON_ENGLISH_STOPWORDS = (" le ", " la ", " les ", " des ", " une ", " pour ",
                          " dans ", " el ", " los ", " para ", " con ")
NON_ENGLISH_STOPWORD_THRESHOLD = 8
LANGUAGE_SNIFF_CHARS = 1000


def looks_non_english(description):
    """True when the description opens in French/Spanish, not English."""
    head = " " + clean_text(description)[:LANGUAGE_SNIFF_CHARS].lower() + " "
    hits = sum(head.count(word) for word in _NON_ENGLISH_STOPWORDS)
    return hits >= NON_ENGLISH_STOPWORD_THRESHOLD


# Procurement notices are not jobs. UNDP's own tenders live on
# procurement-notices.undp.org and not in this API, so this is a guard
# against the odd one leaking into the requisition space rather than a
# routine occurrence — same title test and same kept-and-flagged handling as
# the rest of the fleet.
_RFP_TITLE_RE = re.compile(
    r"\b(rfp|rfq|eoi|tender|empanelment|request for proposals?|"
    r"expressions? of interest|call for proposals?)\b", re.IGNORECASE)


def is_rfp_title(title):
    return bool(_RFP_TITLE_RE.search(title or ""))


# ---- classification ---------------------------------------------------------

def apply_classification(row):
    """Stamp the shared two-level taxonomy onto a rich row.

    The Practice Area flex field is the curated `skills` signal; it stays in
    the rich CSV as a raw source column and never decides the category.
    Returns in_scope — False means DROP the row (excluded_out_of_scope).
    """
    verdict = classify_job(row.get("title", ""),
                           row.get("sectors", ""),
                           row.get("description", ""))
    row["category"] = verdict["category"]
    row["sub_category"] = verdict["sub_category"]
    row["role_family"] = verdict["role_family"]
    row["all_families"] = verdict["all_families"]
    row["family_scores"] = verdict["family_scores"]
    row["family_confidence"] = verdict["family_confidence"]
    row["matched_in"] = verdict["matched_in"]
    # An in-scope procurement notice is a tender about an in-scope
    # programme, not a job opening — always reviewed before export. A
    # non-English (FR/ES) description is likewise kept but reviewed rather
    # than silently published untranslated (UNDP-01).
    row["needs_review"] = (verdict["needs_review"] or bool(row.get("is_rfp"))
                           or looks_non_english(row.get("description", "")))
    return verdict["in_scope"]


# company_type (hospital|pharma) is a separate club field, NOT a category;
# UN agencies default to "hospital" (the fleet-wide convention for the club
# enum).
_PHARMA_COMPANY_RE = re.compile(
    r"pharma|therapeut|laborator|\blabs?\b|\bcro\b|biotech|bioscience|"
    r"life ?science|diagnostic|clinical research|medical devices?",
    re.IGNORECASE)


def classify_company_type(name):
    return "pharma" if _PHARMA_COMPANY_RE.search(name or "") else "hospital"


# ----------------------------------------------------------------------------
# Time window (master spec §4)
# ----------------------------------------------------------------------------

def compute_cutoff(existing_df, since=None):
    """First run: last INITIAL_WINDOW_DAYS. Later: watermark minus grace."""
    if since:
        return since
    if existing_df is not None and "posted_date" in existing_df.columns:
        dates = pd.to_datetime(existing_df["posted_date"], errors="coerce").dropna()
        if not dates.empty:
            return (dates.max() - pd.Timedelta(days=WATERMARK_GRACE_DAYS)).date().isoformat()
    return (date.today() - timedelta(days=INITIAL_WINDOW_DAYS)).isoformat()


# ----------------------------------------------------------------------------
# HTTP layer (master spec §7)
# ----------------------------------------------------------------------------

def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/json"})
    return session


def assert_no_disallowed_host():
    """jobs.undp.org is Disallow: / — prove nothing here points at it."""
    for url in (LIST_URL, DETAIL_URL, JOB_URL_TEMPLATE, ROBOTS_URL):
        for host in DISALLOWED_HOSTS:
            if host in url:
                sys.exit("{} is robots-disallowed but appears in {} — "
                         "aborting.".format(host, url))


def check_robots(session):
    """Verify the Oracle CE host allows the two REST paths.

    The host served no robots.txt when probed (HTTP 404), which means no
    restrictions; the check still runs so a later-added robots.txt stops the
    scraper instead of being ignored.
    """
    assert_no_disallowed_host()
    rp = urllib.robotparser.RobotFileParser(ROBOTS_URL)
    try:
        resp = session.get(ROBOTS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 404:
            log.info("No robots.txt on %s (HTTP 404) — crawling allowed", CE_HOST)
            rp.parse([])
        elif resp.status_code < 400:
            rp.parse(resp.text.splitlines())
        else:
            rp.parse([])
    except requests.RequestException as exc:
        log.warning("Could not fetch robots.txt (%s); assuming allowed", exc)
        return
    for url in (LIST_URL.format(limit=LIST_PAGE_LIMIT),
                DETAIL_URL.format(job_id="0")):
        if not rp.can_fetch(USER_AGENT, url):
            sys.exit("robots.txt disallows {} — aborting.".format(url))
    log.info("robots.txt check passed")


def fetch_json(session, url):
    """GET one JSON document with retries/backoff. Returns dict or None."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("Retry %d/%d for %s in %.0fs (%s)",
                        attempt, MAX_RETRIES, url, wait, last_error)
            time.sleep(wait)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = "HTTP {}".format(resp.status_code)
                continue
            if 400 <= resp.status_code < 500:  # permanent — don't retry
                log.warning("HTTP %d for %s — skipping", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except ValueError as exc:            # not JSON — a block page etc.
            log.warning("Non-JSON response from %s (%s)", url, exc)
            return None
        except requests.RequestException as exc:
            last_error = exc
    log.error("Giving up on %s after %d retries (%s)", url, MAX_RETRIES, last_error)
    return None


# ----------------------------------------------------------------------------
# Row building
# ----------------------------------------------------------------------------

def build_row(job_id, detail, posted_date):
    """Rich row from one requisition detail + the listing's posted date.

    posted_date comes from the listing because it is the same value as the
    detail's ExternalPostedStartDate and is what the cutoff was judged on;
    the detail's own value is the fallback.
    """
    flex = parse_flex_fields(detail)
    title = clean_text(detail.get("Title"))

    country_code = clean_text(detail.get("PrimaryLocationCountry")).upper()
    city, country_name = parse_location(detail.get("PrimaryLocation"), country_code)

    description = strip_boilerplate(
        strip_html(detail.get("ExternalDescriptionStr") or ""))[:DESCRIPTION_MAX_CHARS]

    return {
        "source": SITE,
        "job_id": str(job_id),
        # Agency is the real employer: UNDP, UNCDF or UNV.
        "company": flex.get("Agency") or "UNDP",
        "title": title,
        "city": city,
        # Oracle exposes no sub-national region on this tenant.
        "state": "",
        "country": country_name,
        # Practice Area is UNDP's own curated topic tag — the `skills` signal.
        "sectors": flex.get("Practice Area", ""),
        "is_rfp": is_rfp_title(title),
        # taxonomy fields are stamped by apply_classification()
        "category": "",
        "sub_category": "",
        "role_family": "",
        "all_families": "",
        "family_scores": "",
        "family_confidence": "",
        "matched_in": "",
        "needs_review": False,
        "experience_min_years": parse_experience_years(flex, description),
        "posted_date": (parse_iso_date(posted_date) or
                        parse_iso_date(detail.get("ExternalPostedStartDate"))),
        "valid_through": parse_iso_date(detail.get("ExternalPostedEndDate")),
        "description": description,
        "job_url": JOB_URL_TEMPLATE.format(job_id),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _blank(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def rich_row_to_club_row(r):
    """Map a rich row to the CLUB_COLUMNS contract.

    Everything is derived from stored rich columns so a full-store re-export
    reproduces the same rows: the ISO code and dial code come back from the
    country name, and the club job_type enum
    (full_time|part_time|remote|hybrid) carries the one thing it can say
    honestly about UNDP's contract modalities (PSA, Internship, Roster) — a
    Home Based duty station is "remote", everything else "full_time". UNDP
    publishes no salary, so those columns stay empty rather than invented.
    """
    country = _blank(r.get("country"))
    code, dial = CODE_BY_COUNTRY.get(country, ("", ""))
    city = _blank(r.get("city"))
    description = _blank(r.get("description"))
    return {
        "country_name": country,
        "country_code": code,
        "country_dial_code": dial,
        "city_name": city,
        "company_name": _blank(r.get("company")),
        "company_type": classify_company_type(_blank(r.get("company"))),
        "company_logo": "",
        "company_about": "",
        "title": _blank(r.get("title")),
        "description": description,
        "job_type": "remote" if city == PSEUDO_COUNTRY_CODES["U2"] else "full_time",
        "category": _blank(r.get("category")),
        "sub_category": _blank(r.get("sub_category")),
        "role_family": _blank(r.get("role_family")),
        "application_url": _blank(r.get("job_url")),
        "posted_at": _blank(r.get("posted_date")),
        "min_experience": _blank(r.get("experience_min_years")),
        "max_experience": "",
        # no structured qualification field on the board — grounded
        # extraction from the description only, never inferred
        "qualification": extract_qualification(description),
        # UNDP publishes no salary anywhere
        "min_salary": "",
        "max_salary": "",
        "salary_period": "",
        "salary_currency": "",
    }


# ----------------------------------------------------------------------------
# Stores
# ----------------------------------------------------------------------------

def load_existing(path):
    try:
        return pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        return None


def load_id_column(path):
    try:
        return set(pd.read_csv(path, dtype=str)["job_id"].dropna())
    except (FileNotFoundError, KeyError):
        return set()


def append_out_of_scope(rows, path=OUT_OF_SCOPE_CSV):
    """Append dropped rich rows, deduped on job_id. Moved, never discarded."""
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    try:
        df = pd.concat([pd.read_csv(path, dtype=str), df], ignore_index=True)
    except FileNotFoundError:
        pass
    df = df.fillna("").drop_duplicates(subset="job_id", keep="last")
    for col in RICH_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df[RICH_COLUMNS].to_csv(path, index=False)
    return len(df)


def write_club_csv(rich_df, run_date):
    rows = [rich_row_to_club_row(r) for _, r in rich_df.iterrows()]
    club_df = pd.DataFrame(rows, columns=CLUB_COLUMNS)
    out_dir = CLUB_CSV_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{}.csv".format(SITE)
    club_df.to_csv(target, index=False)
    return target, len(club_df)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape UNDP/UNCDF/UNV vacancies from the Oracle HCM "
                    "Candidate Experience API behind jobs.undp.org.")
    parser.add_argument("--output", default=RICH_CSV,
                        help="rich cumulative CSV path (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="stop after fetching N new detail records (test runs)")
    parser.add_argument("--run-date", default=date.today().strftime("%d-%m-%Y"),
                        help="jobs_csv/<DD-MM-YYYY>/ folder (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="override the watermark and keep every job "
                             "posted on/after this date")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    session = make_session()
    check_robots(session)

    existing_df = load_existing(args.output)
    known_ids = set(existing_df["job_id"].dropna()) if existing_df is not None else set()
    seen_old = load_id_column(SEEN_OLD_CSV)
    out_of_scope_ids = load_id_column(OUT_OF_SCOPE_CSV)
    cutoff = compute_cutoff(existing_df, since=args.since)
    log.info("Existing CSV has %d known jobs (%d known-old, %d known "
             "out-of-scope skipped); keeping jobs posted on/after %s%s",
             len(known_ids), len(seen_old), len(out_of_scope_ids), cutoff,
             " (--since override)" if args.since else "")

    payload = fetch_json(session, LIST_URL.format(limit=LIST_PAGE_LIMIT))
    if payload is None:
        sys.exit("Could not fetch the requisition list — aborting.")
    requisitions, total = parse_requisition_list(payload)
    log.info("Oracle CE site %s lists %d open requisitions (TotalJobsCount=%s)",
             SITE_NUMBER, len(requisitions), total)
    if not requisitions:
        sys.exit("Requisition list was empty — API shape changed? Aborting.")
    if total is not None and len(requisitions) < total:
        # One page is meant to hold the whole board; if it ever stops
        # doing so, say so loudly rather than silently under-collecting.
        log.warning("Only %d of %d requisitions returned at limit=%d — "
                    "RAISE LIST_PAGE_LIMIT, the board has outgrown one page",
                    len(requisitions), total, LIST_PAGE_LIMIT)

    counters = {"scanned": 0, "duplicates": 0, "skipped_old": 0,
                "skipped_out_of_scope": 0, "excluded_old": 0,
                "excluded_out_of_scope": 0, "needs_review": 0, "new": 0,
                "detail_failed": 0}
    new_rows, review_log, new_seen_old, dropped_rows = [], [], [], []
    fetched = 0

    for req in requisitions:              # POSTING_DATES_DESC = newest first
        counters["scanned"] += 1
        job_id = clean_text(req.get("Id"))
        if not job_id:
            continue
        if job_id in known_ids:
            counters["duplicates"] += 1
            continue
        if job_id in out_of_scope_ids:
            counters["skipped_out_of_scope"] += 1
            continue

        # The listing's PostedDate IS the detail's ExternalPostedStartDate,
        # so the window is decided before spending a detail request.
        posted_date = parse_iso_date(req.get("PostedDate"))
        if posted_date and posted_date < cutoff:
            if job_id in seen_old:
                counters["skipped_old"] += 1
            else:
                counters["excluded_old"] += 1
                seen_old.add(job_id)
                new_seen_old.append({"job_id": job_id,
                                     "posted_date": posted_date})
            continue

        if args.limit is not None and fetched >= args.limit:
            break

        payload = fetch_json(session, DETAIL_URL.format(job_id=job_id))
        fetched += 1
        detail = parse_requisition_detail(payload)
        if not detail:
            counters["detail_failed"] += 1
            log.warning("No requisition detail for job_id=%s", job_id)
            continue
        try:
            row = build_row(job_id, detail, posted_date)
        except Exception as exc:  # never let one job crash the run (spec §7)
            log.warning("Skipping malformed job %s: %s", job_id, exc)
            continue

        if not apply_classification(row):
            counters["excluded_out_of_scope"] += 1
            out_of_scope_ids.add(job_id)
            dropped_rows.append(row)
            continue

        if row["needs_review"]:
            counters["needs_review"] += 1
            review_log.append({"job_id": row["job_id"], "title": row["title"],
                               "company": row["company"],
                               "sectors": row["sectors"],
                               "is_rfp": row["is_rfp"]})
        known_ids.add(job_id)
        new_rows.append(row)
        counters["new"] += 1

    # ---- rich cumulative CSV (source of truth) ----
    if new_rows:
        new_df = pd.DataFrame(new_rows, dtype=str)
        combined = (pd.concat([existing_df, new_df], ignore_index=True)
                    if existing_df is not None else new_df)
        combined = combined.drop_duplicates(subset="job_id", keep="first")
        for col in RICH_COLUMNS:
            if col not in combined.columns:
                combined[col] = ""
        combined = combined[RICH_COLUMNS]
        combined.to_csv(args.output, index=False)
        log.info("Wrote %s (%d total rows)", args.output, len(combined))
    else:
        combined = existing_df if existing_df is not None else \
            pd.DataFrame(columns=RICH_COLUMNS)
        log.info("No new jobs; %s left unchanged", args.output)

    # ---- club-schema CSV (full-store dump, regenerated every run) ----
    if len(combined):
        target, n = write_club_csv(combined, args.run_date)
        log.info("Wrote %s (%d rows, HealthCareers.club schema)", target, n)

    # ---- sidecars ----
    if new_seen_old:
        old_df = pd.DataFrame(new_seen_old)
        try:
            old_df = pd.concat([pd.read_csv(SEEN_OLD_CSV, dtype=str), old_df],
                               ignore_index=True)
        except FileNotFoundError:
            pass
        old_df.drop_duplicates(subset="job_id").to_csv(SEEN_OLD_CSV, index=False)
    if dropped_rows:
        total_dropped = append_out_of_scope(dropped_rows)
        log.info("Moved %d rows to %s (%d total, kept for review)",
                 len(dropped_rows), OUT_OF_SCOPE_CSV, total_dropped)
    if review_log:
        pd.DataFrame(review_log).to_csv(NEEDS_REVIEW_CSV, index=False)
        log.info("Wrote %s (%d titles to review)", NEEDS_REVIEW_CSV,
                 len(review_log))

    print("\n===== Run summary =====")
    print("Requisitions scanned:         {:>6,}".format(counters["scanned"]))
    print("Excluded (out of scope):      {:>6,}  (shared classifier)".format(
        counters["excluded_out_of_scope"]))
    print("Skipped (known out of scope): {:>6,}".format(counters["skipped_out_of_scope"]))
    print("Excluded (older than {}): {:>2,}".format(cutoff, counters["excluded_old"]))
    print("Skipped (known old):          {:>6,}".format(counters["skipped_old"]))
    print("Flagged needs_review:         {:>6,}".format(counters["needs_review"]))
    print("New jobs added:               {:>6,}".format(counters["new"]))
    print("Duplicates skipped:           {:>6,}".format(counters["duplicates"]))
    print("Detail parse failures:        {:>6,}".format(counters["detail_failed"]))


if __name__ == "__main__":
    main()
