#!/usr/bin/env python3
"""Shared role-family scoring for the HealthCareers clinical-research scope.

ONE definition of the eleven in-scope role families, imported by every
re-scoped scraper (shine_roles, pharmarecruiter_roles, docthub_roles,
gulftalent_roles). Copy-pasting eleven regexes into four files guarantees they
drift the first time one is tuned.

    Public Health · Clinical Data Management · Clinical Research ·
    Medical Writer · TMF · Medical Coding · Pharmacovigilance ·
    Regulatory Affairs · Medical Reviewer · MSL · HEOR

These strings are the club CSV's `category` values verbatim (repo README,
"Allowed enum values"), so a scraper never has to translate.

Why scoring instead of a yes/no title match
-------------------------------------------
The himalayas scraper gates on the job title alone. That is precise but leaves
yield on the table: in a 307-job reference set built from Naukri, only 9 rows
matched on title alone, while 106 (35%) had NO title match and surfaced purely
through the skills tags or the description.

Matching those extra fields naively would flood the results with boilerplate
("...supports our clinical research division..."), so each field carries a
different weight and a job must clear a threshold:

    title       5   the employer's own name for the role — strongest signal
    skills      2   curated tag lists ("pharmacovigilance,drug safety,gcp")
    description 1   free text, weakest and easiest to trip

A distinct keyword scores once per field, so one family term repeated twenty
times in a description cannot manufacture a match. A single title hit (5)
clears the bar on its own; description text needs three distinct family terms.

Every scraper records `role_family` (the winner), `all_families`,
`family_scores`, `matched_in` and `family_confidence`, so a questionable row
can always be traced back to why it was admitted.
"""

import re

# ---------------------------------------------------------------------------
# Scoring configuration
# ---------------------------------------------------------------------------

FIELD_WEIGHTS = {"title": 5, "skills": 2, "description": 1}

MIN_SCORE_KEEP = 3      # any title hit, or 2 skill terms, or 3 description terms
HIGH_CONFIDENCE = 5     # a title hit, or a skill + description cluster
MEDIUM_CONFIDENCE = 3

# Per-family override of MIN_SCORE_KEEP. Public Health vocabulary is far
# more diffuse than the other families' ("child health", "nutrition",
# "infection control" appear as routine duties across all of healthcare),
# so score-3 PH admissions were mostly junk in the 2026-08-24 shine
# backfill: phlebotomists tagged "infection control", analyst spam listing
# "epidemiology" in keyword dumps. PH therefore demands a title hit or at
# least two distinct skill terms.
FAMILY_MIN_SCORE = {"Public Health": 5}

# Families that must show evidence in the title or the skill tags —
# description text alone never qualifies, whatever it scores. Every
# description-only PH admission in the 2026-08-24 shine backfill was junk
# (a printing RFQ, telesales, an HR operations role scoring 5 on scattered
# boilerplate mentions).
FAMILY_REQUIRE_TITLE_OR_SKILLS = {"Public Health"}

# Only the first N characters of a description are scored. Job descriptions end
# in EEO statements, benefits blurbs and company boilerplate that mention
# "clinical research" for unrelated roles.
DESCRIPTION_SCAN_CHARS = 4_000

# ---------------------------------------------------------------------------
# The eleven families
# ---------------------------------------------------------------------------
#
# Order is precedence for ties only — the score decides the winner first.
# Specific families precede broad ones, so an exact tie between Medical Writer
# and Clinical Research resolves to Medical Writer.

ROLE_FAMILIES = [
    ("TMF",
     r"\btmf\b|trial master file|etmf"),

    ("HEOR",
     r"\bheor\b|health econom\w*|outcomes research|\bhta\b|market access|"
     r"payer (?:evidence|value|strateg\w*)|real[- ]world evidence|\brwe\b|"
     r"health technology assessment|pricing (?:and|&) reimbursement"),

    ("Medical Reviewer",
     r"medical review\w*|medical monitor\w*|physician reviewer|"
     r"clinical reviewer|\bmro\b"),

    ("MSL",
     r"medical science liaison|\bmsl\b|medical affairs|scientific affairs|"
     r"medical advisor|field medical|medical information"),

    ("Pharmacovigilance",
     r"pharmacovigilance|\bpv\b|drug safety|\bgvp\b|adverse event|"
     r"safety (?:physician|scientist|surveillance|domain|database)|"
     r"aggregate report\w*|signal detection|case processing|"
     r"\bpsur\b|\bpbrer\b|\bicsr\b|argus|\bmeddra\b"),

    ("Medical Coding",
     r"medical cod\w*|clinical coding|\bcoder\b|"
     r"coding (?:specialist|auditor|analyst|manager|officer|"
     r"quality|compliance|validation|audit)|\bcpc\b|\bccs\b|icd-?10|"
     r"risk adjustment|\bhcc\b|profee|\bdrg\b|charge capture|whodrug"),

    ("Medical Writer",
     r"medical writ\w*|scientific writ\w*|regulatory writ\w*|medical editor|"
     r"scientific editor|medical communications?|"
     r"publications? (?:manager|lead|specialist|associate|director)|"
     r"\bcsr\b writing|clinical study report"),

    ("Regulatory Affairs",
     # NOT bare "regulatory compliance" — in US listings that is usually
     # revenue-cycle compliance, a different job entirely.
     r"regulatory affairs?|regulatory (?:strateg\w*|submission\w*|operation\w*|"
     r"intelligence|specialist|associate|manager|director|lead|scientist|"
     r"labeling|labelling|publishing|dossier)|\bctd\b|\bectd\b|\bnda\b|"
     r"\bmaa\b|\banda\b|drug regulatory"),

    ("Clinical Data Management",
     # "CDM" alone is ambiguous — in revenue-cycle listings it is Charge
     # Description Master.
     r"clinical data|clinical database|clinical programm\w*|data manage\w*|"
     # "rave" (Medidata Rave) needs boundaries: bare it matches inside
     # "travel" and "Paravet" (found live on devnetjobsindia 2026-08-26).
     r"data steward|\bedc\b|\bcdisc\b|\bsdtm\b|\badam\b|medidata|\brave\b|"
     r"\bcdm\b(?=.*(?:clinical|trial|study|edc))"),

    ("Public Health",
     # Covers the ten PH sub-categories (taxonomy 2026-08-24): Epidemiology,
     # Program Management, Monitoring & Evaluation, Community Health, Health
     # Promotion & Education, Disease Programs, Nutrition, Infection
     # Prevention & Control, Health Informatics & Data, PH Research — plus
     # the fold-ins (MCH, WASH, environmental health, policy, HSS).
     # Health economics stays out: it always tags HEOR, never Public Health.
     # Deliberately absent: bare "M&E" (Media & Entertainment), bare "MCH"
     # (M.Ch. surgery degree), bare "IPC" (Indian Penal Code), bare "WASH"
     # (the verb) — each is admitted only in a role-shaped phrase.
     r"public health|epidemiolog\w*|population health|"
     r"community health|community medicine|\bchw\b|"
     r"asha (?:worker|supervisor|coordinator|facilitator)|anganwadi|"
     r"global health|health promotion|health educat\w*|\bsbcc\b|"
     r"behaviou?r(?:al)? change communication|"
     r"disease surveillance|outbreak (?:investigation|response|preparedness)|"
     r"health polic\w*|(?<!occupational )health program\w*|"
     r"health systems? strengthening|"
     # "Monitoring, Evaluation and Learning" / "Monitoring, Evaluation,
     # Accountability and Learning" (MEAL) are the standard NGO title forms;
     # the old `monitoring (?:and|&) evaluation` missed every comma variant.
     r"monitoring,? (?:and |& )?evaluation|"
     r"m&e (?:officer|manager|coordinator|specialist|associate|lead|director)|"
     # MEAL = Monitoring, Evaluation, Accountability and Learning. NOT
     # "coordinator": "Meal Coordinator" is food service, and it was being
     # admitted as Public Health. The spelled-out form is covered by the
     # monitoring/evaluation alternative above.
     r"\bmeal (?:officer|manager|specialist|advisor)\b|"
     r"tuberculosis|\bntep\b|\brntcp\b|hiv/aids|\bhiv\b|malaria|leprosy|"
     r"immuni[sz]ation|vaccinat\w*|"
     r"(?<!animal )(?<!poultry )(?<!cattle )(?<!sports )nutritionist|"
     r"(?<!animal )(?<!poultry )(?<!cattle )(?<!sports )dieti[ct]ian|"
     r"(?:public health|community) nutrition|"
     r"(?<!animal )(?<!poultry )(?<!cattle )(?<!feed )"
     r"nutrition (?:officer|specialist|program\w*|assistant|educator)|"
     r"poshan|"
     r"infection (?:prevention|control)|"
     r"health informatics|health information (?:management|system\w*)|"
     r"\bhmis\b|dhis-?2|health data|"
     r"health systems? research|implementation (?:research|science)|"
     r"operational research|"
     r"maternal (?:and |& )?child health|maternal health|child health|"
     r"\brmnch\w*|"
     r"wash (?:officer|specialist|coordinator|engineer|program\w*)|"
     r"water,? sanitation (?:and|&) hygiene|"
     # EHS ("Environmental Health and Safety" / "... , Safety") is a
     # workplace-safety occupation, not public health
     r"environmental health(?!\s*(?:,|and|&)?\s*safety)|\bmph\b"),

    ("Clinical Research",
     r"clinical research|clinical trial\w*|clinical stud\w*|clinical operations|"
     r"clinical monitor\w*|clinical development|clinical project|"
     r"clinical scientist|\bcra\b|\bgcp\b|\bich\b|"
     r"(?:study|trial) (?:manager|lead|coordinator|director|start[- ]?up|"
     r"specialist|associate)|principal investigator|"
     r"site (?:management|contracts|activation)|\bcro\b|"
     r"research (?:associate|coordinator|nurse|physician)"),
]

FAMILY_NAMES = [name for name, _ in ROLE_FAMILIES]
_FAMILY_RES = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in ROLE_FAMILIES]
_PRECEDENCE = {name: i for i, name in enumerate(FAMILY_NAMES)}

# Titles carrying an in-scope term inside a plainly different profession.
# These are KEPT but flagged — never silently dropped (master spec §2).
OUT_OF_SCOPE_TITLE = re.compile(
    r"(?:^|[^a-z])(?:"
    r"counsel|attorney|paralegal|"
    r"account (?:executive|manager)|sales (?:representative|rep|director|"
    r"manager|executive|specialist|officer)|business development|"
    r"recruiter|talent acquisition|"
    r"software (?:engineer|developer)|web developer|frontend|backend|"
    r"data engineer|devops"
    r")(?:[^a-z]|$)",
    re.IGNORECASE)


def _distinct_hits(pattern, text):
    """Number of DISTINCT matched substrings, so repetition can't inflate."""
    if not text:
        return 0
    return len({m.group(0).lower() for m in pattern.finditer(text)})


def score_families(title="", skills="", description=""):
    """Score every family across the three fields.

    `skills` may be a list or a delimited string (Naukri's tagsAndSkills,
    Shine's skills array, WordPress tags...). Returns
    {family: {"score": int, "fields": [...]}} for families that scored.
    """
    if isinstance(skills, (list, tuple, set)):
        skills = " , ".join(str(s) for s in skills)
    fields = {
        "title": title or "",
        "skills": skills or "",
        "description": (description or "")[:DESCRIPTION_SCAN_CHARS],
    }
    scored = {}
    for name, pattern in _FAMILY_RES:
        total, matched_in = 0, []
        for field, text in fields.items():
            hits = _distinct_hits(pattern, text)
            if hits:
                total += hits * FIELD_WEIGHTS[field]
                matched_in.append(field)
        if total:
            scored[name] = {"score": total, "fields": matched_in}
    return scored


def confidence_for(score):
    if score >= HIGH_CONFIDENCE:
        return "high"
    if score >= MEDIUM_CONFIDENCE:
        return "medium"
    return "low"


def classify(title="", skills="", description=""):
    """Full verdict for one job.

    Returns a dict with:
        family        winning family, or "" when out of scope
        score         its score
        confidence    high | medium | low
        all_families  every family clearing MIN_SCORE_KEEP, "|"-joined
        family_scores "Clinical Research=8;Medical Writing=3"
        matched_in    "title|skills|description" for the winning family
        needs_review  True when the title looks like a different profession
    """
    scored = score_families(title, skills, description)
    keep = {k: v for k, v in scored.items()
            if v["score"] >= FAMILY_MIN_SCORE.get(k, MIN_SCORE_KEEP)
            and not (k in FAMILY_REQUIRE_TITLE_OR_SKILLS
                     and v["fields"] == ["description"])}
    if not keep:
        return {"family": "", "score": 0, "confidence": "", "all_families": "",
                "family_scores": "", "matched_in": "", "needs_review": False}

    # highest score wins; ties break on the precedence order above
    family = min(keep, key=lambda k: (-keep[k]["score"], _PRECEDENCE[k]))
    ordered = sorted(keep, key=lambda k: (-keep[k]["score"], _PRECEDENCE[k]))
    return {
        "family": family,
        "score": keep[family]["score"],
        "confidence": confidence_for(keep[family]["score"]),
        "all_families": "|".join(ordered),
        "family_scores": ";".join("{}={}".format(k, keep[k]["score"])
                                  for k in ordered),
        "matched_in": "|".join(keep[family]["fields"]),
        "needs_review": bool(OUT_OF_SCOPE_TITLE.search(title or "")),
    }


# ---------------------------------------------------------------------------
# Qualification extraction (shared with the club CSV's `qualification` column)
# ---------------------------------------------------------------------------

_QUALIFICATION_PATTERNS = [
    (r"\bMBBS\b", "MBBS"),
    (r"\bM\.D\.|\bMD\s*/\s*DO\b|\bMD\s+degree\b", "MD"),
    (r"\bPharm\.?\s?D\b", "PharmD"),
    (r"\bB\.?\s?Pharm\b", "B.Pharm"),
    (r"\bM\.?\s?Pharm\b", "M.Pharm"),
    (r"\bPh\.?\s?D\b", "PhD"),
    (r"\bMPH\b", "MPH"),
    (r"\bDVM\b", "DVM"),
    (r"\bBSN\b", "BSN"),
    (r"\bMSN\b", "MSN"),
    (r"\bM\.?Sc\b|\bMaster of Science\b", "MSc"),
    (r"\bB\.?Sc\b|\bBachelor of Science\b", "BSc"),
    (r"\bMBA\b", "MBA"),
    (r"\bRN\b|\bRegistered Nurse\b", "RN"),
    (r"\bRAC\b", "RAC"),
    (r"\bCCRA\b", "CCRA"),
    (r"\bCCRP\b", "CCRP"),
    (r"\bRHIA\b", "RHIA"),
    (r"\bRHIT\b", "RHIT"),
    (r"\bCPC\b", "CPC"),
    (r"\bCCS\b", "CCS"),
    (r"\bCDISC\b", "CDISC"),
    (r"\bBachelor'?s?\s+degree\b", "Bachelor's degree"),
    (r"\bMaster'?s?\s+degree\b", "Master's degree"),
    (r"\bDoctorate\b|\bDoctoral degree\b", "Doctorate"),
    (r"\blife sciences?\b", "Life Sciences"),
]
_QUALIFICATION_RES = [(re.compile(p, re.IGNORECASE), label)
                      for p, label in _QUALIFICATION_PATTERNS]
MAX_QUALIFICATIONS = 8

_QUALIFICATION_RES = [(re.compile(p, re.IGNORECASE), label)
                      for p, label in _QUALIFICATION_PATTERNS]


def extract_qualification(description):
    """Lift explicit credentials verbatim out of a description.

    Grounded extraction, not inference: a token is only emitted when it
    literally appears in the posting. Returns "" when the description names
    none — the club schema's `qualification` is optional and the master spec
    forbids inventing what the source did not state.
    """
    text = description or ""
    if not text:
        return ""
    found = []
    for regex, label in _QUALIFICATION_RES:
        if label not in found and regex.search(text):
            found.append(label)
        if len(found) >= MAX_QUALIFICATIONS:
            break
    return ", ".join(found)

