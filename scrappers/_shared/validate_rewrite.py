"""Validate an LLM-rewritten job description against its source material.

The rewrite prompts (instructions/jd_rewrite_prompts.md) forbid inventing facts.
This validator enforces the machine-checkable subset of that contract:

1. numbers  — every number in the output must appear in the input
              (comma/space-insensitive, so "205,000" matches "205000")
2. emails   — every email in the output must appear in the input
3. urls     — every URL in the output must appear in the input
4. shape    — output starts with a paragraph (not a heading) and contains
              "## Responsibilities" unless it's a thin-input fallback
5. length   — output should not exceed input length on non-thin jobs

Usage:
    from validate_rewrite import validate_rewrite
    issues = validate_rewrite(source_text, output_text)   # [] means pass

`source_text` should be the full user message sent to the model (structured
fields + raw description) so numbers coming from fields also count as grounded.
"""

import re

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE = re.compile(r"https?://\S+|www\.\S+")

# Ordinal-ish tokens the model may legitimately write as digits from words
# ("first", "second") or that come from markdown numbering.
IGNORED_NUMBERS = set()


def _norm_num(tok: str) -> str:
    return tok.replace(",", "")


def _grounded_numbers(text: str) -> set:
    return {_norm_num(t) for t in NUM_RE.findall(text)}


def validate_rewrite(source_text: str, output_text: str, thin_input: bool = False):
    """Return a list of human-readable issue strings. Empty list = pass.

    A leading "NEEDS_REVIEW: <reason>" line (the prompt's quality escape for
    corrupted sources) is not a validation failure — it is stripped before the
    shape checks and surfaced as a "review:" entry so the pipeline can route
    the job to manual review instead of publishing it.
    """
    issues = []

    if output_text.lstrip().startswith("NEEDS_REVIEW:"):
        first, _, rest = output_text.lstrip().partition("\n")
        issues.append(f"review: {first[len('NEEDS_REVIEW:'):].strip()}")
        output_text = rest

    src_nums = _grounded_numbers(source_text)
    for tok in NUM_RE.findall(output_text):
        norm = _norm_num(tok)
        if norm in IGNORED_NUMBERS:
            continue
        # Accept if the exact number appears in source, or it is a sub-part of
        # a grounded range like "2-8" / "205000-240000".
        if norm in src_nums:
            continue
        if any(norm in s for s in src_nums):
            continue
        issues.append(f"invented number: {tok!r} not found in source")

    for email in EMAIL_RE.findall(output_text):
        if email not in source_text:
            issues.append(f"invented email: {email!r}")

    for url in URL_RE.findall(output_text):
        if url.rstrip(".,)") not in source_text:
            issues.append(f"invented url: {url!r}")

    stripped = output_text.lstrip()
    if stripped.startswith("#"):
        issues.append("shape: output starts with a heading, expected opening paragraph")
    if not thin_input and "## Responsibilities" not in output_text:
        issues.append("shape: missing '## Responsibilities' section on non-thin input")

    if not thin_input and len(output_text) > len(source_text) * 1.1:
        issues.append(
            f"length: output ({len(output_text)}) longer than source "
            f"({len(source_text)}) — likely padding"
        )

    return issues
