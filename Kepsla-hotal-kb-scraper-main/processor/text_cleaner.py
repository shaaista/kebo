"""Clean and prepare raw extracted text for LLM processing.

Takes messy HTML-extracted content and produces clean, readable text
suitable for feeding into GPT-4o for KB structuring.
"""

import html
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# â”€â”€ Regex Patterns â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Zero-width and invisible characters (BOM, zero-width space/joiner/non-joiner, etc.)
_INVISIBLE_RE = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\ufeff\u2060\u2061\u2062\u2063\u2064"
    r"\u00ad\u034f\u061c\u180e\u2800\u3164\uffa0]"
)

# HTML entities that weren't decoded during extraction
_HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")

_MOJIBAKE_MARKERS = ("Ã", "â€™", "â€", "â€“", "â€”", "â‚", "Â", "Ă")
_MOJIBAKE_REPLACEMENTS = {
    "Ã©": "é",
    "Ã¨": "è",
    "Ãª": "ê",
    "Ã«": "ë",
    "Ã¡": "á",
    "Ã ": "à",
    "Ã­": "í",
    "Ã³": "ó",
    "Ã´": "ô",
    "Ã¶": "ö",
    "Ãº": "ú",
    "Ã¼": "ü",
    "Ã±": "ñ",
    "Ã§": "ç",
    "Ã×": "×",
    "\u00c3\u0097": "×",
    "\u0102\u0097": "×",
    "Ă—": "×",
    "â€“": "–",
    "â€”": "—",
    "â€˜": "‘",
    "â€™": "’",
    "â€œ": "“",
    "\u00e2\u20ac\u009d": "”",
    "\u00e2\u20ac\ufffd": "”",
    "\u00e2\u201a\u00b9": "₹",
    "Â ": " ",
    "Â": "",
}
# Cookie/privacy consent banner patterns (line-level matching, no DOTALL)
_COOKIE_PATTERNS = re.compile(
    r"(?:^|\n)[^\n]*(?:"
    r"we\s+use\s+cookies|"
    r"this\s+(?:site|website)\s+uses?\s+cookies|"
    r"cookie\s+(?:policy|preferences?|settings?|consent)|"
    r"accept\s+(?:all\s+)?cookies|"
    r"reject\s+(?:all\s+)?cookies|"
    r"manage\s+(?:cookie|privacy)\s+(?:preferences?|settings?)|"
    r"by\s+continuing\s+to\s+(?:use|browse)\s+this\s+(?:site|website)|"
    r"privacy\s+(?:policy|notice|banner)|"
    r"data\s+protection\s+(?:policy|notice)"
    r")[^\n]*(?:\n|$)",
    re.IGNORECASE,
)

# Newsletter / subscription boilerplate (line-level matching, no DOTALL)
_NEWSLETTER_PATTERNS = re.compile(
    r"(?:^|\n)[^\n]*(?:"
    r"subscribe\s+to\s+(?:our\s+)?(?:newsletter|mailing\s+list|updates)|"
    r"sign\s+up\s+for\s+(?:our\s+)?(?:newsletter|emails?|updates)|"
    r"enter\s+your\s+email\s+(?:to\s+)?(?:subscribe|sign\s+up|receive)|"
    r"get\s+(?:the\s+)?latest\s+(?:news|offers?|deals?|updates?)\s+(?:in\s+your|delivered)|"
    r"join\s+our\s+(?:mailing\s+list|newsletter)|"
    r"unsubscribe\s+(?:at\s+)?any\s+time"
    r")[^\n]*(?:\n|$)",
    re.IGNORECASE,
)

# Social media share buttons text
_SOCIAL_SHARE_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"share\s+(?:on|via|to)\s+(?:facebook|twitter|linkedin|pinterest|whatsapp|instagram|reddit)|"
    r"tweet\s+this|"
    r"tweet\b|"
    r"pin\s+it|"
    r"share\s+this\s+(?:page|article|post)|"
    r"follow\s+us\s+(?:on|@)|"
    r"like\s+us\s+on\s+facebook|"
    r"connect\s+with\s+us"
    r")\s*(?:\n|$)",
    re.IGNORECASE,
)

# Inline social media button text (may appear mid-line, e.g. "Share on Facebook Tweet Pin it")
_SOCIAL_INLINE_RE = re.compile(
    r"\b(?:"
    r"share\s+(?:on|via)\s+(?:facebook|twitter|linkedin|pinterest|whatsapp|instagram|reddit)|"
    r"tweet\s+this|"
    r"pin\s+it|"
    r"share\s+this\s+(?:page|article|post)"
    r")\b",
    re.IGNORECASE,
)

# Copyright / legal boilerplate
_COPYRIGHT_RE = re.compile(
    r"(?:^|\n)[^\n]*(?:"
    r"(?:\u00a9|copyright|\(c\))\s*\d{4}|"
    r"all\s+rights?\s+reserved|"
    r"terms\s+(?:of\s+(?:use|service)|and\s+conditions)|"
    r"privacy\s+policy\s*\|?\s*terms|"
    r"designed\s+(?:and\s+developed\s+)?by\s+|"
    r"powered\s+by\s+|"
    r"website\s+(?:designed|developed|built)\s+by"
    r")[^\n]*(?:\n|$)",
    re.IGNORECASE,
)

# Navigation-like repeated patterns
_NAV_BOILERPLATE_RE = re.compile(
    r"(?:^|\n)\s*(?:home|about\s+us|contact\s+us|sitemap|faq|careers?|press|"
    r"media|blog|news|login|sign\s+in|my\s+account|book\s+now)\s*\n",
    re.IGNORECASE,
)


# â”€â”€ Core Cleaning Functions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _remove_invisible_chars(text: str) -> str:
    """Remove zero-width characters, BOM markers, and other invisible unicode."""
    return _INVISIBLE_RE.sub("", text)


def _decode_html_entities(text: str) -> str:
    """Decode any remaining HTML entities that weren't handled during extraction."""
    if not _HTML_ENTITY_RE.search(text):
        return text
    try:
        return html.unescape(text)
    except Exception:
        return text


def _repair_mojibake(text: str) -> str:
    """Repair common UTF-8/Windows-1252 mojibake sequences when detected."""
    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text

    def _score(value: str) -> int:
        return sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)

    best = text
    best_score = _score(text)

    for source_encoding in ("latin-1", "cp1252"):
        try:
            candidate = text.encode(source_encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue

        candidate_score = _score(candidate)
        if candidate_score < best_score:
            best = candidate
            best_score = candidate_score

    for broken, fixed in _MOJIBAKE_REPLACEMENTS.items():
        best = best.replace(broken, fixed)

    return best


def _normalize_whitespace(text: str) -> str:
    """Collapse excessive whitespace while preserving paragraph structure."""
    # Multiple spaces/tabs within a line -> single space
    text = re.sub(r"[^\S\n]+", " ", text)
    # Strip trailing whitespace on each line
    text = re.sub(r" +\n", "\n", text)
    # Strip leading whitespace on each line (but not indentation in lists)
    text = re.sub(r"\n +(?=[^\s\-\*\d])", "\n", text)
    # 3+ consecutive newlines -> 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _remove_cookie_banners(text: str) -> str:
    """Remove cookie consent and privacy banner text."""
    return _COOKIE_PATTERNS.sub("\n", text)


def _remove_newsletter_boilerplate(text: str) -> str:
    """Remove subscribe/newsletter prompts."""
    return _NEWSLETTER_PATTERNS.sub("\n", text)


def _remove_social_share(text: str) -> str:
    """Remove social media share button text (both standalone lines and inline)."""
    text = _SOCIAL_SHARE_RE.sub("\n", text)
    text = _SOCIAL_INLINE_RE.sub("", text)
    return text


# â”€â”€ Public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def clean_text(text: str) -> str:
    """Master cleaning function: transform raw extracted text into clean, LLM-ready content.

    Applies all cleaning steps in order:
      1. Remove invisible/zero-width characters and BOM
      2. Decode remaining HTML entities
      3. Normalize unicode (NFKC)
      4. Remove cookie/privacy banners
      5. Remove newsletter/subscription boilerplate
      6. Remove social share button text
      7. Normalize whitespace

    Args:
        text: Raw text extracted from a web page.

    Returns:
        Cleaned text suitable for LLM processing.
    """
    if not text or not text.strip():
        return ""

    try:
        # Step 1: Remove invisible characters
        text = _remove_invisible_chars(text)

        # Step 2: Decode HTML entities
        text = _decode_html_entities(text)

        # Step 3: Repair common encoding artifacts before further normalization
        text = _repair_mojibake(text)

        # Step 4: Normalize unicode (NFKC converts compatibility characters)
        text = unicodedata.normalize("NFKC", text)

        # Step 5: Remove cookie/privacy banners
        text = _remove_cookie_banners(text)

        # Step 6: Remove newsletter boilerplate
        text = _remove_newsletter_boilerplate(text)

        # Step 7: Remove social share text
        text = _remove_social_share(text)

        # Step 8: Normalize whitespace
        text = _normalize_whitespace(text)

        return text

    except Exception as exc:
        logger.warning("Text cleaning failed, returning stripped original: %s", exc)
        return text.strip()


def truncate_to_chars(text: str, max_chars: int) -> str:
    """Smart truncation that doesn't cut mid-sentence.

    Finds the last sentence boundary (. ! ?) before the limit
    and truncates there. If no sentence boundary found within the
    last 20% of the text, falls back to word boundary.

    Args:
        text: Text to truncate.
        max_chars: Maximum character count.

    Returns:
        Truncated text ending at a natural boundary.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    # Look for the last sentence-ending punctuation before the limit
    truncated = text[:max_chars]

    # Search for the last sentence boundary in the truncated portion
    # Look in the last 20% of the truncated text for a sentence end
    search_start = max(0, int(max_chars * 0.8))
    search_region = truncated[search_start:]

    # Find the last sentence-ending punctuation followed by a space or end
    sentence_end = None
    for match in re.finditer(r'[.!?](?:\s|$)', search_region):
        sentence_end = search_start + match.start() + 1  # include the punctuation

    if sentence_end is not None:
        return text[:sentence_end].strip()

    # Fallback: truncate at the last word boundary
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        return text[:last_space].strip()

    # Last resort: hard truncate
    return truncated.strip()


def merge_texts(texts: list[str], separator: str = "\n\n---\n\n") -> str:
    """Merge multiple page texts with separators.

    Filters out empty texts before merging.

    Args:
        texts: List of text strings from different pages.
        separator: String to place between texts.

    Returns:
        Single merged text string.
    """
    if not texts:
        return ""

    # Filter out empty/whitespace-only entries
    non_empty = [t.strip() for t in texts if t and t.strip()]
    if not non_empty:
        return ""

    return separator.join(non_empty)


def remove_boilerplate(text: str) -> str:
    """Remove common website boilerplate content.

    Targets copyright notices, "All rights reserved", navigation
    fragments, and other repeated header/footer content.

    Args:
        text: Text that may contain boilerplate.

    Returns:
        Text with boilerplate removed.
    """
    if not text:
        return ""

    try:
        # Remove copyright lines (apply twice to catch adjacent boilerplate lines
        # where removing one reveals the next at a line boundary)
        text = _COPYRIGHT_RE.sub("\n", text)
        text = _COPYRIGHT_RE.sub("\n", text)

        # Remove navigation-like fragments (isolated menu items)
        text = _NAV_BOILERPLATE_RE.sub("\n", text)

        # Remove "Back to top" / "Scroll to top" links
        text = re.sub(
            r"(?:^|\n)\s*(?:back\s+to\s+top|scroll\s+to\s+top|go\s+to\s+top)\s*(?:\n|$)",
            "\n",
            text,
            flags=re.IGNORECASE,
        )

        # Remove "Read more" / "Learn more" standalone lines
        text = re.sub(
            r"(?:^|\n)\s*(?:read\s+more|learn\s+more|view\s+more|see\s+more|show\s+more|load\s+more)\s*\.?\s*(?:\n|$)",
            "\n",
            text,
            flags=re.IGNORECASE,
        )

        # Remove breadcrumb-like lines (Home > About > Contact)
        text = re.sub(
            r"(?:^|\n)\s*(?:home\s*[>\/\|]\s*){1,}.*?(?:\n|$)",
            "\n",
            text,
            flags=re.IGNORECASE,
        )

        # Normalize resulting whitespace
        text = _normalize_whitespace(text)

        return text

    except Exception as exc:
        logger.warning("Boilerplate removal failed: %s", exc)
        return text.strip()


def deduplicate_content(texts: list[str]) -> list[str]:
    """Remove near-duplicate page texts from a list.

    Uses sentence-level overlap to detect duplicates. If two texts
    share more than 92% of their sentences, the shorter one is dropped.

    Args:
        texts: List of text strings, one per page.

    Returns:
        Deduplicated list preserving original order.
    """
    if not texts or len(texts) <= 1:
        return list(texts) if texts else []

    def _extract_sentences(text: str) -> set[str]:
        """Split text into a set of normalized sentences for comparison."""
        if not text:
            return set()
        # Split on sentence boundaries
        raw_sentences = re.split(r'[.!?]+\s+|\n+', text)
        # Normalize: lowercase, strip, remove very short fragments
        sentences = set()
        for s in raw_sentences:
            normalized = s.strip().lower()
            # Only consider sentences with enough substance (>20 chars)
            if len(normalized) > 20:
                sentences.add(normalized)
        return sentences

    def _similarity(set_a: set[str], set_b: set[str]) -> float:
        """Compute true Jaccard similarity between two sentence sets.

        Uses union as the denominator so two pages that each have unique
        sentences (room pages with the same amenities list but different
        descriptions) are NOT incorrectly flagged as duplicates.
        """
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union if union > 0 else 0.0

    try:
        # Pre-compute sentence sets for each text
        sentence_sets = [_extract_sentences(t) for t in texts]

        # Track which indices to keep
        keep = [True] * len(texts)

        for i in range(len(texts)):
            if not keep[i]:
                continue
            for j in range(i + 1, len(texts)):
                if not keep[j]:
                    continue
                sim = _similarity(sentence_sets[i], sentence_sets[j])
                if sim > 0.92:
                    # Drop the shorter text (less content)
                    if len(texts[i]) >= len(texts[j]):
                        keep[j] = False
                        logger.debug(
                            "Dropping duplicate text at index %d (%.0f%% overlap with index %d)",
                            j, sim * 100, i,
                        )
                    else:
                        keep[i] = False
                        logger.debug(
                            "Dropping duplicate text at index %d (%.0f%% overlap with index %d)",
                            i, sim * 100, j,
                        )
                        break  # i is dropped, no need to compare further

        result = [texts[i] for i in range(len(texts)) if keep[i]]

        dropped = len(texts) - len(result)
        if dropped > 0:
            logger.info("Deduplication removed %d near-duplicate texts out of %d", dropped, len(texts))

        return result

    except Exception as exc:
        logger.warning("Deduplication failed, returning originals: %s", exc)
        return list(texts)
