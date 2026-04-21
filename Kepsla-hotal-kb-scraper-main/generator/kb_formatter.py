"""Convert structured KB text into multiple output formats (.txt, .json, .md).

Takes the final KB text produced by processor/llm_structurer.py and formats it
for different consumption scenarios:
  - .txt  : Direct upload to kebo-main's config/knowledge_base/ folder
  - .json : Structured data for programmatic access and API responses
  - .md   : Human-readable review format with table of contents

The === SECTION === headers in the source text are the primary structural markers
that enable kebo-main's RAG chunker to split content effectively.
"""

import json
import logging
import re
from datetime import datetime, timezone

from config.hotel_kb_schema import KB_SECTIONS

logger = logging.getLogger(__name__)

_GENERATOR_VERSION = "Hotel KB Scraper v1.0"


# ── Section Parsing Helpers ──────────────────────────────────────────────────


def parse_sections(kb_text: str) -> list[dict]:
    """Parse KB text into a list of section dicts.

    Splits the raw KB text on ``=== SECTION NAME ===`` header patterns and
    maps each title back to its canonical id from KB_SECTIONS.

    Args:
        kb_text: The full KB text with ``=== ... ===`` section headers.

    Returns:
        List of dicts, each with keys:
            - ``title`` (str): The section title as it appeared in the text.
            - ``content`` (str): Everything between this header and the next.
            - ``id`` (str): The matching KB_SECTIONS id, or a slug of the title.
    """
    if not kb_text or not kb_text.strip():
        return []

    # Build a lookup: normalised title -> section id
    title_to_id: dict[str, str] = {}
    for section in KB_SECTIONS:
        normalised = section["title"].strip().lower()
        title_to_id[normalised] = section["id"]

    # Split on === SECTION NAME === pattern
    # The regex captures the title inside the === markers
    section_pattern = re.compile(r"^===\s*(.+?)\s*===$", re.MULTILINE)

    matches = list(section_pattern.finditer(kb_text))

    if not matches:
        # No section headers found — return entire text as one section
        logger.warning("No === SECTION === headers found in KB text")
        return [
            {
                "title": "Full Content",
                "content": kb_text.strip(),
                "id": "full_content",
            }
        ]

    sections: list[dict] = []

    for i, match in enumerate(matches):
        title = match.group(1).strip()

        # Determine content boundaries
        content_start = match.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(kb_text)
        content = kb_text[content_start:content_end].strip()

        # Match title to KB_SECTIONS id
        normalised_title = title.lower().strip()
        section_id = title_to_id.get(normalised_title)

        if section_id is None:
            # Fuzzy match: check if any known title is contained in this title or vice versa
            for known_title, known_id in title_to_id.items():
                if known_title in normalised_title or normalised_title in known_title:
                    section_id = known_id
                    break

        if section_id is None:
            # Generate a slug from the title
            section_id = re.sub(r"[^a-z0-9]+", "_", normalised_title).strip("_")

        sections.append(
            {
                "title": title,
                "content": content,
                "id": section_id,
            }
        )

    logger.debug("Parsed %d sections from KB text", len(sections))
    return sections


def extract_facts(section_content: str) -> list[str]:
    """Extract individual facts from a section's content.

    A "fact" is any informational line:
      - Lines starting with ``- `` (bullet points)
      - Lines matching ``Key: Value`` patterns
      - Lines under ``## Sub-header`` blocks (the sub-header itself is kept)

    Empty lines, whitespace-only lines, and pure separator lines are filtered out.

    Args:
        section_content: The text content of a single KB section.

    Returns:
        List of fact strings, trimmed of leading/trailing whitespace.
    """
    if not section_content or not section_content.strip():
        return []

    facts: list[str] = []
    key_value_pattern = re.compile(r"^[A-Za-z][A-Za-z\s/&\-()]+:\s*.+")

    for line in section_content.split("\n"):
        stripped = line.strip()

        # Skip empty or separator lines
        if not stripped:
            continue
        if re.match(r"^[-=_*]{3,}$", stripped):
            continue

        # Bullet points
        if stripped.startswith("- "):
            fact = stripped[2:].strip()
            if fact:
                facts.append(fact)
            continue

        # Bullet points with * marker
        if stripped.startswith("* "):
            fact = stripped[2:].strip()
            if fact:
                facts.append(fact)
            continue

        # Sub-headers (## ...) — keep as facts for context
        if stripped.startswith("## "):
            facts.append(stripped)
            continue

        # Key: Value lines
        if key_value_pattern.match(stripped):
            facts.append(stripped)
            continue

    return facts


# ── TXT Format ───────────────────────────────────────────────────────────────


def format_txt(kb_text: str, property_name: str, source_url: str) -> str:
    """Format KB text for direct upload to kebo-main's knowledge base folder.

    Adds a metadata header block and preserves the === SECTION === structure
    so that kebo-main's RAG chunker can split effectively.

    Args:
        kb_text: The raw structured KB text from llm_structurer.
        property_name: Name of the hotel property.
        source_url: The website URL that was scraped.

    Returns:
        Formatted .txt content string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    header = (
        "================================================================\n"
        "HOTEL KNOWLEDGE BASE\n"
        f"Property: {property_name}\n"
        f"Source: {source_url}\n"
        f"Generated: {now}\n"
        f"Generator: {_GENERATOR_VERSION}\n"
        "================================================================"
    )

    return f"{header}\n\n{kb_text.strip()}\n"


# ── JSON Format ──────────────────────────────────────────────────────────────


def format_json(kb_text: str, property_name: str, source_url: str) -> str:
    """Parse KB text into structured JSON.

    Produces a JSON document with sections, individual facts per section,
    and metadata including completeness scoring.

    Args:
        kb_text: The raw structured KB text from llm_structurer.
        property_name: Name of the hotel property.
        source_url: The website URL that was scraped.

    Returns:
        JSON string (pretty-printed with indent=2).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Parse sections from the KB text
    parsed = parse_sections(kb_text)

    # Build structured section objects
    sections_json: list[dict] = []
    total_facts = 0

    for section in parsed:
        facts = extract_facts(section["content"])
        total_facts += len(facts)
        sections_json.append(
            {
                "id": section["id"],
                "title": section["title"],
                "content": section["content"],
                "facts": facts,
            }
        )

    # Calculate completeness: sections with actual content / total expected sections
    expected_section_ids = {s["id"] for s in KB_SECTIONS}
    found_section_ids = {
        s["id"] for s in sections_json
        if s["content"].strip() and s["id"] in expected_section_ids
    }
    total_expected = len(expected_section_ids)
    completeness_score = round(len(found_section_ids) / total_expected, 2) if total_expected > 0 else 0.0

    document = {
        "property_name": property_name,
        "source_url": source_url,
        "generated_at": now,
        "generator": _GENERATOR_VERSION,
        "sections": sections_json,
        "metadata": {
            "total_sections": len(sections_json),
            "total_facts": total_facts,
            "completeness_score": completeness_score,
        },
    }

    return json.dumps(document, indent=2, ensure_ascii=False)


# ── Markdown Format ──────────────────────────────────────────────────────────


def format_md(kb_text: str, property_name: str, source_url: str) -> str:
    """Convert KB text to a human-readable Markdown document.

    Transforms:
      - ``=== SECTION NAME ===`` headers into ``# Section Name``
      - Preserves ``## Sub-header`` blocks as-is
      - Adds a table of contents at the top
      - Adds a metadata footer

    Args:
        kb_text: The raw structured KB text from llm_structurer.
        property_name: Name of the hotel property.
        source_url: The website URL that was scraped.

    Returns:
        Formatted Markdown string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    parsed = parse_sections(kb_text)

    # Build table of contents
    toc_lines: list[str] = []
    for i, section in enumerate(parsed, start=1):
        # Create an anchor-friendly slug
        anchor = re.sub(r"[^a-z0-9\s-]", "", section["title"].lower())
        anchor = re.sub(r"\s+", "-", anchor).strip("-")
        toc_lines.append(f"{i}. [{section['title']}](#{anchor})")

    toc = "\n".join(toc_lines)

    # Build section content
    body_parts: list[str] = []
    for section in parsed:
        body_parts.append(f"# {section['title']}\n")

        content = section["content"]
        # The content may already contain ## sub-headers, which is fine.
        # We just need to make sure we don't double-up heading markers.
        body_parts.append(content)
        body_parts.append("")  # blank line between sections

    body = "\n\n".join(body_parts)

    # Assemble the full document
    md_parts = [
        f"# {property_name} - Hotel Knowledge Base\n",
        f"> **Source:** {source_url}  ",
        f"> **Generated:** {now}  ",
        f"> **Generator:** {_GENERATOR_VERSION}\n",
        "---\n",
        "## Table of Contents\n",
        toc,
        "\n---\n",
        body,
        "\n---\n",
        "*This knowledge base was automatically generated by the Hotel KB Scraper.*  ",
        f"*Source: {source_url}*  ",
        f"*Generated: {now}*",
    ]

    return "\n".join(md_parts) + "\n"
