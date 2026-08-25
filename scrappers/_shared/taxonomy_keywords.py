#!/usr/bin/env python3
"""Machine-readable form of Jobs_keywords/keywords_for_jobs.md — sub-category assignment.

`role_families.py` answers "is this job in scope, and which family?". This
module answers the finer question "which sub-category?", using the taxonomy
adopted 2026-08-24: two categories (Non Clinical, Public Health), ten
sub-categories each. For Non Clinical jobs the family and the sub-category are
the same name, so the main value here is splitting the single Public Health
family into its ten sub-categories — run `classify_subcategory` on rows that
`role_families.classify` already admitted.

Matching tiers (Jobs_keywords/keywords_for_jobs.md):

    1. negative keywords  a hit in title or skills vetoes everything
    2. title match        classifies directly — high precision
    3. strong skill kws   2+ distinct hits in skills/description classify
    4. weak skill kws     tie-breaks only, never match alone

Short ambiguous tokens (TMF, IPC, CHO, MSL, CTA, CPC...) appear only in the
title regexes, never in the skill-keyword lists, so they can only match as
whole words in a title.
"""

import re

MIN_STRONG_HITS = 2
DESCRIPTION_SCAN_CHARS = 4_000


def _phrase_re(phrase):
    """One keyword phrase -> regex fragment. Spaces match whitespace/hyphens,
    and boundaries are alphanumeric lookarounds so 'ART' never matches inside
    'particular' but 'E2B'/'ICD-10-CM' still work."""
    parts = [re.escape(w) for w in phrase.split()]
    return r"(?<![A-Za-z0-9])" + r"[\s\-]+".join(parts) + r"(?![A-Za-z0-9])"


def _compile_phrases(phrases):
    return re.compile("|".join(_phrase_re(p) for p in phrases), re.IGNORECASE)


# ---------------------------------------------------------------------------
# The taxonomy. Dict order is tie-break precedence (Non Clinical before
# Public Health, so "Clinical Data Manager" resolves to CDM before the PH
# informatics bucket can claim "data").
# ---------------------------------------------------------------------------

SUBCATEGORIES = {
    # ----------------------------- Non Clinical ---------------------------
    "Clinical Data Management": {
        "category": "Non Clinical",
        "titles": r"clinical data (?:manage\w*|coordinator|associate|analyst|"
                  r"specialist|reviewer)|clinical database programm\w*|"
                  r"clinical sas programm\w*|"
                  r"\bcdm\b(?=.*(?:clinical|trial|study|edc))",
        "strong": ["Medidata Rave", "Oracle Clinical", "Veeva CDMS", "Inform",
                   "EDC", "eCRF", "CRF design", "discrepancy management",
                   "query management", "data validation", "database lock",
                   "CDISC", "SDTM", "CDASH", "SAE reconciliation"],
        "weak": ["clinical trials", "data entry", "GCP", "data cleaning"],
    },
    "Clinical Research": {
        "category": "Non Clinical",
        "titles": r"clinical research (?:associate|coordinator|executive|"
                  r"manager|scientist)|\bcra\b|\bcrc\b|\bcta\b|"
                  r"clinical trial (?:associate|assistant|manager)|"
                  r"clinical study manager|clinical project manager|"
                  r"clinical operations|site management associate|"
                  r"study start[- ]?up|feasibility analyst|site coordinator",
        "strong": ["ICH-GCP", "site monitoring", "SDV",
                   "source data verification", "site initiation",
                   "site close-out", "CTMS", "protocol deviation",
                   "informed consent", "EC/IRB submission", "study start-up",
                   "patient recruitment", "monitoring visit report"],
        "weak": ["clinical trials", "pharma", "CRO", "protocol"],
    },
    "Medical Writer": {
        "category": "Non Clinical",
        "titles": r"medical writ\w*|scientific writer|regulatory writer|"
                  r"publication writer|medical communications writer|"
                  r"scientific content writer",
        "strong": ["clinical study report", "CSR writing", "protocol writing",
                   "investigator brochure", "ICF drafting", "manuscript",
                   "publication planning", "ICMJE", "CONSORT", "AMA style",
                   "CTD Module 2", "plain language summary"],
        "weak": ["medical writing", "scientific content", "editing",
                 "literature review"],
    },
    "TMF": {
        "category": "Non Clinical",
        "titles": r"\btmf\b|trial master file|\betmf\b|"
                  r"clinical documentation associate",
        "strong": ["eTMF", "Veeva Vault eTMF", "TMF Reference Model",
                   "DIA TMF", "essential documents", "document QC",
                   "inspection readiness", "TMF completeness",
                   "filing and archival"],
        "weak": ["documentation", "document management", "quality check"],
    },
    "Medical Coding": {
        "category": "Non Clinical",
        "titles": r"medical cod\w*|clinical cod(?:er|ing)|certified professional coder|"
                  r"\bcpc\b|coding (?:auditor|quality analyst|officer)|"
                  r"(?:hcc|ipdrg|ip-drg|ed|e ?& ?m|surgery|radiology) coder",
        "strong": ["ICD-10-CM", "CPT", "HCPCS", "CPC", "CCS", "CIC", "COC",
                   "HCC", "risk adjustment", "DRG", "IP-DRG", "E/M coding",
                   "3M encoder", "coding compliance", "AAPC", "AHIMA"],
        "weak": ["anatomy", "physiology", "medical terminology",
                 "healthcare documentation"],
    },
    "Pharmacovigilance": {
        "category": "Non Clinical",
        "titles": r"pharmacovigilance|\bpv (?:associate|executive|officer|"
                  r"specialist|scientist|quality)\b|drug safety|\bdsa\b|"
                  r"case processing associate|icsr associate|argus safety|"
                  r"signal detection analyst|literature surveillance|"
                  r"local safety officer",
        "strong": ["Argus", "ArisG", "LifeSphere", "Veeva Vault Safety",
                   "ICSR", "case processing", "MedDRA", "WHO-DD", "E2B",
                   "narrative writing", "PSUR", "PBRER", "DSUR", "PADER",
                   "SUSAR", "expedited reporting", "signal detection",
                   "causality assessment", "triage", "literature screening"],
        "weak": ["drug safety", "GVP", "adverse events", "safety reporting"],
    },
    "Regulatory Affairs": {
        "category": "Non Clinical",
        "titles": r"regulatory affairs?|drug regulatory|\bdra\b|"
                  r"regulatory (?:submission\w*|publisher|operation\w*|"
                  r"intelligence|labell?ing)",
        "strong": ["eCTD", "CTD", "dossier", "ANDA", "NDA", "IND", "MAA",
                   "DMF", "CDSCO", "USFDA submission", "EMA", "variations",
                   "renewals", "lifecycle management", "SmPC", "PIL", "USPI",
                   "labeling", "Veeva Vault RIM", "ROW markets",
                   "health authority queries"],
        "weak": ["regulatory", "submissions", "compliance", "pharma"],
    },
    "Medical Reviewer": {
        "category": "Non Clinical",
        "titles": r"medical review\w*|safety physician|medical monitor\w*|"
                  r"aggregate report reviewer|clinical reviewer",
        "strong": ["medical review", "ICSR review", "aggregate report review",
                   "causality assessment", "benefit-risk assessment",
                   "MedDRA coding review", "narrative review",
                   "medical monitoring", "safety physician"],
        "weak": ["medical assessment", "drug safety", "clinical judgment"],
    },
    "MSL": {
        "category": "Non Clinical",
        "titles": r"medical science liaison|\bmsl\b|medical advisor|"
                  r"field medical|medical affairs|scientific advisor",
        "strong": ["KOL engagement", "key opinion leader",
                   "scientific exchange", "advisory board", "medical affairs",
                   "insight generation", "CME programs",
                   "therapy area expertise", "field medical"],
        "weak": ["medical education", "stakeholder engagement",
                 "product launch"],
    },
    "HEOR": {
        "category": "Non Clinical",
        "titles": r"\bheor\b|health economist|outcomes research|market access|"
                  r"\brwe\b|\bhta\b|pricing (?:and|&) reimbursement|"
                  r"evidence synthesis|\bslr analyst\b|value (?:and|&) access",
        "strong": ["cost-effectiveness", "budget impact model", "Markov model",
                   "QALY", "HTA", "NICE", "payer evidence", "reimbursement",
                   "systematic literature review", "meta-analysis",
                   "network meta-analysis", "RWE", "RWD", "claims data",
                   "TreeAge", "value dossier", "AMCP dossier",
                   "global value dossier"],
        "weak": ["health economics", "market access", "evidence generation"],
    },
    # ----------------------------- Public Health --------------------------
    "Epidemiology": {
        "category": "Public Health",
        "titles": r"epidemiolog\w*|(?:disease )?surveillance officer",
        "strong": ["outbreak investigation", "IDSP", "IHIP",
                   "disease surveillance", "line list", "case definition",
                   "contact tracing", "incidence", "prevalence", "Epi Info",
                   "epidemiological analysis"],
        "weak": ["public health", "R", "STATA", "data analysis",
                 "infectious disease"],
    },
    "Public Health Program Management": {
        "category": "Public Health",
        "titles": r"program(?:me)? (?:officer|manager|coordinator)|"
                  r"project (?:officer|coordinator|manager)|"
                  r"technical (?:officer|advisor|specialist)|"
                  r"(?:state|district|block) program manager|"
                  r"health program consultant|implementation lead",
        "strong": ["NHM", "NRHM", "donor-funded", "USAID", "Global Fund",
                   "BMGF", "program implementation", "government liaison",
                   "sub-grantee management", "work plan", "health systems"],
        "weak": ["program management", "stakeholder management", "NGO",
                 "budgeting"],
    },
    "Monitoring & Evaluation": {
        "category": "Public Health",
        "titles": r"m ?& ?e (?:officer|specialist|manager)|\bmel officer\b|"
                  r"\bmerl\b|\bmis officer\b|evaluation specialist|"
                  r"impact assessment|monitoring,? (?:and |& )?evaluation",
        "strong": ["logframe", "results framework", "indicators",
                   "baseline endline", "DHIS2", "KoboToolbox", "ODK",
                   "data quality audit", "theory of change",
                   "third-party monitoring", "MEL"],
        "weak": ["monitoring", "evaluation", "reporting", "dashboards"],
    },
    "Community Health": {
        "category": "Public Health",
        "titles": r"community health (?:officer|worker)|\bcho\b|"
                  r"community mobili[sz]er|outreach (?:worker|coordinator)|"
                  r"asha (?:coordinator|facilitator)|"
                  r"community engagement officer|link worker|peer educator",
        "strong": ["ASHA", "VHSNC", "community mobilization", "grassroots",
                   "SHG", "panchayat", "door-to-door outreach",
                   "village health", "mobilisation"],
        "weak": ["fieldwork", "community", "outreach", "rural"],
    },
    "Health Promotion & Education": {
        "category": "Public Health",
        "titles": r"health educator|\biec officer\b|\bbcc officer\b|\bsbcc\b|"
                  r"health communication specialist|health promotion officer|"
                  r"awareness program(?:me)? coordinator",
        "strong": ["IEC", "BCC", "SBCC", "behaviour change communication",
                   "health literacy", "campaign design",
                   "IEC material development", "community radio",
                   "tobacco control"],
        "weak": ["communication", "awareness", "campaigns", "counselling"],
    },
    "Disease Programs": {
        "category": "Public Health",
        "titles": r"tb health visitor|\btbhv\b|senior treatment supervisor|"
                  r"senior tb lab supervisor|\bstls\b|\bntep\b|"
                  r"(?:tb|hiv) consultant|(?:ictc|art) counsell?or|"
                  r"\bnaco\b|\bsacs\b|malaria technical supervisor|\bvbd\b|"
                  r"immuni[sz]ation officer|routine immuni[sz]ation|"
                  r"cold chain|vaccine logistics",
        "strong": ["NTEP", "RNTCP", "Nikshay", "DOTS", "NACO", "SACS", "ICTC",
                   "ART", "PrEP", "NVBDCP", "IRS", "LLIN", "kala-azar",
                   "lymphatic filariasis", "UIP", "EPI", "eVIN", "cold chain",
                   "AEFI", "RI microplanning", "measles-rubella", "polio"],
        "weak": ["TB", "HIV", "malaria", "immunization", "vaccination",
                 "counselling"],
    },
    "Public Health Nutrition": {
        "category": "Public Health",
        "titles": r"nutrition (?:officer|consultant|coordinator)|"
                  r"(?:public health )?nutritionist|poshan|"
                  r"(?<!sports )dieti[ct]ian|"
                  r"nutrition program(?:me)? manager|nrc nutrition|"
                  r"sam program officer",
        "strong": ["POSHAN Abhiyaan", "ICDS", "anganwadi", "SAM", "MAM",
                   "CMAM", "IYCF", "micronutrient", "Anemia Mukt Bharat",
                   "growth monitoring", "take-home ration",
                   "food fortification"],
        "weak": ["nutrition", "diet counselling", "malnutrition"],
    },
    "Infection Prevention & Control": {
        "category": "Public Health",
        "titles": r"infection (?:prevention|control)|infection preventionist|"
                  r"\bipc (?:officer|specialist|coordinator|nurse)\b",
        "strong": ["HAI surveillance", "hand hygiene audit",
                   "CIC certification", "CBIC", "antimicrobial stewardship",
                   "bundle care audit", "CSSD",
                   "biomedical waste management", "NABH", "JCI",
                   "hospital outbreak management"],
        "weak": ["infection control", "sterilization", "hygiene", "audits"],
    },
    "Health Informatics & Data": {
        "category": "Public Health",
        "titles": r"health data analyst|\bhmis\b|health informatics|"
                  r"digital health|\bmhealth\b|gis analyst|health mis|"
                  r"\babdm\b",
        "strong": ["HMIS", "DHIS2", "ABDM", "Ayushman Bharat Digital Mission",
                   "health dashboards", "QGIS", "health data pipeline",
                   "telemedicine program", "facility reporting"],
        "weak": ["data analyst", "Power BI", "Tableau", "GIS", "SQL"],
    },
    "Public Health Research": {
        "category": "Public Health",
        "titles": r"field investigator|field research assistant|"
                  r"survey (?:coordinator|supervisor)|"
                  r"data collection supervisor|qualitative researcher|"
                  r"research fellow|project research scientist|enumerator|"
                  r"research (?:associate|assistant)",
        "strong": ["NFHS", "household survey", "CAPI", "SurveyCTO", "KoBo",
                   "REDCap", "focus group discussion", "FGD",
                   "in-depth interview", "IDI", "qualitative coding", "ICMR",
                   "ethics committee submission", "enumerator training"],
        "weak": ["research", "survey", "data collection", "interviews"],
    },
}

# Cross-cutting veto list — a hit in title or skills blocks classification.
NEGATIVE_KEYWORDS = [
    "medical billing", "AR caller", "AR calling", "revenue cycle", "RCM",
    "claims adjudication",                       # Medical Coding lookalikes
    "M&E engineer", "MEP", "mechanical & electrical",
    "mechanical and electrical",                 # Monitoring & Evaluation
    "business development", "BD executive", "telecalling", "telesales",
    "sales executive", "sales representative",   # MSL / Clinical Research
    "Indian Penal Code",                         # IPC
    "rheumatoid arthritis",                      # RA
    "staff nurse", "lab technician", "phlebotomist",  # clinical roles
]

CATEGORY_OF = {name: spec["category"] for name, spec in SUBCATEGORIES.items()}
SUBCATEGORY_NAMES = list(SUBCATEGORIES)
_PRECEDENCE = {name: i for i, name in enumerate(SUBCATEGORY_NAMES)}

_TITLE_RES = {name: re.compile(spec["titles"], re.IGNORECASE)
              for name, spec in SUBCATEGORIES.items()}
_STRONG_RES = {name: _compile_phrases(spec["strong"])
               for name, spec in SUBCATEGORIES.items()}
_WEAK_RES = {name: _compile_phrases(spec["weak"])
             for name, spec in SUBCATEGORIES.items()}
_NEGATIVE_RE = _compile_phrases(NEGATIVE_KEYWORDS)


def _distinct_hits(pattern, text):
    if not text:
        return 0
    return len({m.group(0).lower() for m in pattern.finditer(text)})


def classify_subcategory(title="", skills="", description=""):
    """Assign a sub-category to one job.

    Returns a dict:
        category      "Non Clinical" | "Public Health" | ""
        sub_category  winning sub-category, or "" when nothing qualified
        basis         "title" | "skills" | ""
        strong_hits   distinct strong-keyword hits for the winner (skills tier)
        vetoed_by     the negative keyword that blocked the row, or ""
    """
    if isinstance(skills, (list, tuple, set)):
        skills = " , ".join(str(s) for s in skills)
    title = title or ""
    skills = skills or ""
    description = (description or "")[:DESCRIPTION_SCAN_CHARS]
    empty = {"category": "", "sub_category": "", "basis": "",
             "strong_hits": 0, "vetoed_by": ""}

    veto = _NEGATIVE_RE.search(title + " " + skills)
    if veto:
        return dict(empty, vetoed_by=veto.group(0))

    # Tier 2: title match classifies directly.
    title_hits = {name: _distinct_hits(rx, title)
                  for name, rx in _TITLE_RES.items()}
    title_hits = {k: v for k, v in title_hits.items() if v}
    if title_hits:
        winner = min(title_hits, key=lambda k: (-title_hits[k], _PRECEDENCE[k]))
        return {"category": CATEGORY_OF[winner], "sub_category": winner,
                "basis": "title", "strong_hits": 0, "vetoed_by": ""}

    # Tier 3: 2+ distinct strong skill keywords; weak keywords tie-break.
    haystack = skills + " \n " + description
    strong = {name: _distinct_hits(rx, haystack)
              for name, rx in _STRONG_RES.items()}
    candidates = {k: v for k, v in strong.items() if v >= MIN_STRONG_HITS}
    if not candidates:
        return empty
    weak = {name: _distinct_hits(_WEAK_RES[name], haystack)
            for name in candidates}
    winner = min(candidates,
                 key=lambda k: (-candidates[k], -weak[k], _PRECEDENCE[k]))
    return {"category": CATEGORY_OF[winner], "sub_category": winner,
            "basis": "skills", "strong_hits": candidates[winner],
            "vetoed_by": ""}
