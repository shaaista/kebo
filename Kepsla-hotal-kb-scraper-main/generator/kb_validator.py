"""Validate generated KB documents for completeness and quality.

Checks the structured KB text against the expected KB_SECTIONS schema,
counts facts, and produces a detailed validation report with warnings
and quality scoring.

Validation criteria:
  - At least 3 sections with content required for validity
  - Property overview should exist (warning if missing)
  - Contact info should have phone or email (warning if missing)
  - No section should be just 1 line (likely incomplete)
  - Total KB should be at least 1000 characters
  - No excessive repetition of phrases
"""

import logging
import re
from collections import Counter

from config.hotel_kb_schema import KB_SECTIONS
from generator.kb_formatter import extract_facts, parse_sections

logger = logging.getLogger(__name__)

# Minimum thresholds
_MIN_SECTIONS_FOR_VALID = 3
_MIN_TOTAL_CHARS = 1000
_MIN_LINES_PER_SECTION = 2
_REPETITION_THRESHOLD = 5  # same phrase appearing this many times is suspicious


# ── Main Validation ──────────────────────────────────────────────────────────


def validate_kb(kb_text: str, source_url: str) -> dict:
    """Validate a KB text document and produce a detailed report.

    Checks section presence, fact counts, content thresholds, and quality
    indicators to determine whether the KB is ready for production use.

    Args:
        kb_text: The full KB text with ``=== ... ===`` section headers.
        source_url: The website URL that was scraped (for context in warnings).

    Returns:
        Validation report dict with keys:
            - ``is_valid`` (bool): Whether the KB passes minimum quality bar.
            - ``completeness_score`` (float): 0.0-1.0 ratio of sections found.
            - ``sections_found`` (list[str]): IDs of sections with content.
            - ``sections_missing`` (list[str]): IDs of expected sections not found.
            - ``total_facts`` (int): Total extracted fact count.
            - ``total_chars`` (int): Total character count of KB text.
            - ``warnings`` (list[str]): Human-readable warning messages.
            - ``quality_score`` (float): 0.0-1.0 overall quality assessment.
    """
    warnings: list[str] = []
    total_chars = len(kb_text) if kb_text else 0

    # Handle empty input
    if not kb_text or not kb_text.strip():
        return {
            "is_valid": False,
            "completeness_score": 0.0,
            "sections_found": [],
            "sections_missing": [s["id"] for s in KB_SECTIONS],
            "total_facts": 0,
            "total_chars": 0,
            "warnings": ["KB text is empty"],
            "quality_score": 0.0,
        }

    # ── Parse sections ───────────────────────────────────────────────────
    parsed = parse_sections(kb_text)
    expected_ids = {s["id"] for s in KB_SECTIONS}
    expected_titles = {s["id"]: s["title"] for s in KB_SECTIONS}

    # Determine which expected sections are present with actual content
    sections_found: list[str] = []
    sections_with_content: dict[str, str] = {}

    for section in parsed:
        sid = section["id"]
        content = section["content"].strip()
        if sid in expected_ids and content:
            sections_found.append(sid)
            sections_with_content[sid] = content

    sections_missing = [sid for sid in expected_ids if sid not in sections_found]

    # ── Completeness score ───────────────────────────────────────────────
    completeness_score = round(
        len(sections_found) / len(expected_ids), 2
    ) if expected_ids else 0.0

    # ── Fact counting ────────────────────────────────────────────────────
    total_facts = 0
    for section in parsed:
        facts = extract_facts(section["content"])
        total_facts += len(facts)

    # ── Warnings: section-level checks ───────────────────────────────────
    # Property overview check
    if "property_overview" not in sections_found:
        warnings.append("Property Overview section is missing")

    # Contact info check
    if "contact_info" in sections_found:
        contact_content = sections_with_content.get("contact_info", "").lower()
        has_phone = bool(re.search(r"phone|tel|call|\+\d", contact_content))
        has_email = bool(re.search(r"email|@", contact_content))
        if not has_phone and not has_email:
            warnings.append("Contact Information section has no phone numbers or email addresses")
    else:
        warnings.append("Contact Information section is missing")

    # Check for thin sections (only 1 line of content)
    for section in parsed:
        if section["id"] not in expected_ids:
            continue
        content_lines = [
            line for line in section["content"].split("\n")
            if line.strip()
        ]
        if 0 < len(content_lines) < _MIN_LINES_PER_SECTION:
            title = expected_titles.get(section["id"], section["title"])
            warnings.append(
                f"{title} section has only {len(content_lines)} line(s) - likely incomplete"
            )

    # Report missing sections
    for sid in sections_missing:
        title = expected_titles.get(sid, sid)
        warnings.append(f"{title} section is empty or not found")

    # ── Warnings: document-level checks ──────────────────────────────────
    if total_chars < _MIN_TOTAL_CHARS:
        warnings.append(
            f"KB is very short ({total_chars} chars) - expected at least {_MIN_TOTAL_CHARS}"
        )

    if total_facts == 0:
        warnings.append("No structured facts (bullet points or key-value pairs) found")

    # Check for excessive repetition
    repetition_warnings = _check_repetition(kb_text)
    warnings.extend(repetition_warnings)

    # ── Validity determination ───────────────────────────────────────────
    is_valid = (
        len(sections_found) >= _MIN_SECTIONS_FOR_VALID
        and total_chars >= _MIN_TOTAL_CHARS
    )

    # ── Quality score ────────────────────────────────────────────────────
    quality_score = _calculate_quality_score(
        completeness_score=completeness_score,
        total_facts=total_facts,
        total_chars=total_chars,
        sections_found=sections_found,
        warnings=warnings,
    )

    report = {
        "is_valid": is_valid,
        "completeness_score": completeness_score,
        "sections_found": sorted(sections_found),
        "sections_missing": sorted(sections_missing),
        "total_facts": total_facts,
        "total_chars": total_chars,
        "warnings": warnings,
        "quality_score": quality_score,
    }

    logger.info(
        "KB validation: valid=%s, completeness=%.2f, quality=%.2f, facts=%d, warnings=%d",
        is_valid,
        completeness_score,
        quality_score,
        total_facts,
        len(warnings),
    )

    return report


# ── Quality Scoring ──────────────────────────────────────────────────────────


def _calculate_quality_score(
    completeness_score: float,
    total_facts: int,
    total_chars: int,
    sections_found: list[str],
    warnings: list[str],
) -> float:
    """Compute an overall quality score from 0.0 to 1.0.

    Weighted components:
      - Completeness (40%): ratio of sections found
      - Fact density (25%): more facts = higher quality, capped at 200
      - Content volume (20%): more content = higher quality, capped at 30000 chars
      - Warning penalty (15%): fewer warnings = higher quality

    Args:
        completeness_score: Section completeness ratio.
        total_facts: Total fact count.
        total_chars: Total character count.
        sections_found: List of found section IDs.
        warnings: List of warning messages.

    Returns:
        Quality score between 0.0 and 1.0, rounded to 2 decimal places.
    """
    # Component 1: Completeness (40%)
    comp_score = completeness_score

    # Component 2: Fact density (25%) — cap at 200 facts for max score
    fact_score = min(total_facts / 200.0, 1.0) if total_facts > 0 else 0.0

    # Component 3: Content volume (20%) — cap at 30000 chars for max score
    volume_score = min(total_chars / 30000.0, 1.0) if total_chars > 0 else 0.0

    # Component 4: Warning penalty (15%) — each warning reduces score
    max_warnings = 15  # beyond this, the penalty is maxed out
    warning_count = min(len(warnings), max_warnings)
    warning_score = 1.0 - (warning_count / max_warnings)

    # Bonus: property_overview presence is critical
    has_overview = "property_overview" in sections_found

    # Weighted combination
    quality = (
        comp_score * 0.40
        + fact_score * 0.25
        + volume_score * 0.20
        + warning_score * 0.15
    )

    # Small bonus for having property overview (essential section)
    if has_overview:
        quality = min(quality + 0.05, 1.0)

    return round(max(0.0, min(quality, 1.0)), 2)


def _check_repetition(kb_text: str) -> list[str]:
    """Check for excessive phrase repetition in the KB text.

    Looks for phrases (3+ words) that appear suspiciously often, which
    may indicate the LLM produced repetitive or hallucinated content.

    Args:
        kb_text: The full KB text.

    Returns:
        List of warning strings about repeated phrases.
    """
    warnings: list[str] = []

    # Tokenise into sentences/lines
    lines = [line.strip() for line in kb_text.split("\n") if line.strip()]

    # Count exact duplicate lines (excluding headers and bullet markers)
    line_counts = Counter()
    for line in lines:
        # Skip section headers and very short lines
        if line.startswith("===") or line.startswith("##") or len(line) < 20:
            continue
        normalised = line.lower().strip("- *")
        line_counts[normalised] += 1

    for line_text, count in line_counts.most_common(5):
        if count >= _REPETITION_THRESHOLD:
            preview = line_text[:80] + "..." if len(line_text) > 80 else line_text
            warnings.append(
                f"Excessive repetition detected ({count}x): \"{preview}\""
            )

    return warnings


# ── Human-Readable Summary ───────────────────────────────────────────────────


def generate_validation_summary(report: dict) -> str:
    """Generate a concise human-readable summary of the validation report.

    Args:
        report: The validation report dict from ``validate_kb()``.

    Returns:
        A single-line or short multi-line summary string.
    """
    total_expected = len(KB_SECTIONS)
    sections_found_count = len(report.get("sections_found", []))
    total_facts = report.get("total_facts", 0)
    quality_score = report.get("quality_score", 0.0)
    is_valid = report.get("is_valid", False)
    warnings = report.get("warnings", [])

    # Quality label
    if quality_score >= 0.8:
        quality_label = "Excellent"
    elif quality_score >= 0.6:
        quality_label = "Good"
    elif quality_score >= 0.4:
        quality_label = "Fair"
    elif quality_score >= 0.2:
        quality_label = "Poor"
    else:
        quality_label = "Very Poor"

    status = "VALID" if is_valid else "INVALID"

    summary_parts = [
        f"KB Validation [{status}]: "
        f"{sections_found_count}/{total_expected} sections complete, "
        f"{total_facts} facts extracted, "
        f"Quality: {quality_label} ({quality_score:.0%})",
    ]

    if warnings:
        summary_parts.append(f"  Warnings ({len(warnings)}):")
        # Show up to 5 most important warnings
        for warning in warnings[:5]:
            summary_parts.append(f"    - {warning}")
        if len(warnings) > 5:
            summary_parts.append(f"    ... and {len(warnings) - 5} more")

    return "\n".join(summary_parts)
