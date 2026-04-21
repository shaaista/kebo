"""Use OpenAI GPT-4o to convert raw extracted content into a structured hotel KB.

Takes cleaned, classified page content and produces a single comprehensive
knowledge base document via multi-step LLM processing:
  1. Clean and classify all extracted content
  2. Chunk content per section for the LLM context window
  3. Extract structured data from each chunk
  4. Merge and consolidate into the final KB
"""

import asyncio
import io
import json
import logging
import re
from typing import Any, Callable

import httpx
import openai
from openai import AsyncOpenAI
from pypdf import PdfReader

from config.hotel_kb_schema import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_PROMPT,
    KB_SECTIONS,
    MERGE_PROMPT,
)
from config.settings import settings
from processor.section_classifier import group_content_by_section
from processor.text_cleaner import (
    clean_text,
    deduplicate_content,
    merge_texts,
    remove_boilerplate,
    truncate_to_chars,
)

logger = logging.getLogger(__name__)
_SPECIAL_OFFERS_TITLE = next(
    (section["title"] for section in KB_SECTIONS if section["id"] == "special_offers"),
    "Special Offers & Packages",
)
_SPECIAL_OFFERS_HEADER = f"=== {_SPECIAL_OFFERS_TITLE} ==="
_OFFER_SIGNAL_RE = re.compile(
    r"\b(?:offer|offers|package|packages|deal|deals|promotion|promotions|"
    r"staycation|spacation|getaway|exclusive offer|special offer|"
    r"hotel[-\s]?exclusive[-\s]?deals?)\b",
    re.IGNORECASE,
)

# Type for the optional progress callback: (step_name, pct) -> None
ProgressCallback = Callable[[str, int], None] | None

# Retry configuration for rate limit handling
_MAX_RETRIES = 3
_RETRY_DELAYS = [2, 4, 8]  # exponential backoff in seconds
_ASSET_TEXT_CACHE: dict[str, str] = {}
_ASSET_TEXT_KEYWORDS = {
    "menu",
    "restaurant",
    "dining",
    "food",
    "beverage",
    "bar",
    "spa",
    "wellness",
    "policy",
    "policies",
    "banquet",
    "wedding",
    "offer",
    "offers",
    "package",
    "packages",
    "brochure",
}
_MAX_ASSET_TEXTS_PER_PAGE = 2
_MAX_ASSET_PDF_BYTES = 30 * 1024 * 1024
_MAX_ASSET_PDF_PAGES = 12
_MAX_ASSET_PDF_CHARS = 12_000
_MAX_OFFER_KB_INPUT = 30_000

_OFFER_EXTRACTION_SYSTEM_PROMPT = """You are extracting hotel offer-page information.

OUTPUT CONTRACT:
- Output ONLY one section using this exact header: === Special Offers & Packages ===
- Create one `## Offer Name` subsection for every distinct named offer, package, deal, or promotion.
- Preserve the exact offer/package names when the source states them.
- Under each offer, capture factual details such as inclusions, discounts, room upgrade notes, spa credits, dining credits, booking/stay periods, blackout notes, and offer terms when present.
- Use `Field: Value` lines for compact facts and bullets for inclusions/benefits.
- Do not collapse multiple named offers into one generic summary.
- If the reviewed offer pages do not contain reliable offer details, output:
  === Special Offers & Packages ===
  Status: Not found on reviewed pages.
- Do not invent prices, validity dates, or benefits.
"""

_OFFER_EXTRACTION_USER_PROMPT = """Extract only the special offers section from the following reviewed hotel offer-page content.

Property: {property_name}
Representative URL: {url}

--- OFFER PAGE CONTENT ---
{content}
--- END OFFER PAGE CONTENT ---
"""


# ── LLM Communication ───────────────────────────────────────────────────────


async def _call_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4000,
) -> str:
    """Make a single OpenAI chat completion call with retry logic.

    Handles rate limits with exponential backoff, timeouts, and
    general API errors.

    Args:
        system_prompt: System message setting the LLM's role.
        user_prompt: User message with the actual content/task.
        max_tokens: Maximum tokens in the response.

    Returns:
        The generated text from the LLM.

    Raises:
        RuntimeError: If all retries are exhausted or a non-retryable error occurs.
    """
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI API key is not configured (settings.openai_api_key is empty)")

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    last_error = None

    for attempt in range(_MAX_RETRIES):
        try:
            response = await client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=settings.llm_temperature,
            )

            # Extract the generated text
            choice = response.choices[0]
            result = choice.message.content or ""

            if not result.strip():
                logger.warning("LLM returned empty response on attempt %d", attempt + 1)
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                    continue
                return ""

            # Log token usage
            if response.usage:
                logger.debug(
                    "LLM call: model=%s, prompt_tokens=%d, completion_tokens=%d, total=%d",
                    settings.openai_model,
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                    response.usage.total_tokens,
                )

            return result.strip()

        except openai.RateLimitError as exc:
            last_error = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "Rate limited by OpenAI (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, _MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("Rate limit exceeded after %d retries", _MAX_RETRIES)

        except openai.APITimeoutError as exc:
            last_error = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "OpenAI request timed out (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, _MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("OpenAI timeout after %d retries", _MAX_RETRIES)

        except openai.APIConnectionError as exc:
            last_error = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "OpenAI connection error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, _MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("OpenAI connection failed after %d retries", _MAX_RETRIES)

        except openai.APIError as exc:
            last_error = exc
            # Don't retry on 4xx client errors (except 429 rate limit, handled above)
            status_code = getattr(exc, "status_code", None)
            if status_code and 400 <= status_code < 500 and status_code != 429:
                logger.error("OpenAI client error (not retryable): %s", exc)
                raise RuntimeError(f"OpenAI API client error: {exc}") from exc

            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "OpenAI API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, _MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("OpenAI API error after %d retries", _MAX_RETRIES)

        except Exception as exc:
            logger.error("Unexpected error calling OpenAI: %s", exc, exc_info=True)
            raise RuntimeError(f"Unexpected LLM error: {exc}") from exc

    raise RuntimeError(f"LLM call failed after {_MAX_RETRIES} retries: {last_error}")


def _extract_section_block(kb_text: str, section_title: str) -> str:
    """Return one `=== SECTION ===` block from KB text."""
    if not kb_text or not section_title:
        return ""

    header_re = re.compile(
        rf"^===\s*{re.escape(section_title)}\s*===$",
        re.MULTILINE,
    )
    match = header_re.search(kb_text)
    if not match:
        return ""

    next_match = re.search(r"^===\s*.+?\s*===$", kb_text[match.end():], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(kb_text)
    return kb_text[match.start():end].strip()


def _normalize_offer_section_text(offer_text: str) -> str:
    """Ensure offer-only output is wrapped as a single special-offers section."""
    if not offer_text or not offer_text.strip():
        return ""

    extracted = _extract_section_block(offer_text, _SPECIAL_OFFERS_TITLE)
    if extracted:
        return extracted

    return f"{_SPECIAL_OFFERS_HEADER}\n{offer_text.strip()}"


def _replace_or_append_section(kb_text: str, section_title: str, replacement_block: str) -> str:
    """Replace an existing section block, or append it if missing."""
    normalized_replacement = replacement_block.strip()
    if not normalized_replacement:
        return kb_text.strip()

    header_re = re.compile(
        rf"^===\s*{re.escape(section_title)}\s*===$",
        re.MULTILINE,
    )
    match = header_re.search(kb_text)
    if not match:
        base = kb_text.strip()
        if not base:
            return normalized_replacement
        return f"{base}\n\n{normalized_replacement}"

    next_match = re.search(r"^===\s*.+?\s*===$", kb_text[match.end():], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(kb_text)
    before = kb_text[:match.start()].rstrip()
    after = kb_text[end:].lstrip()

    merged = normalized_replacement
    if before:
        merged = f"{before}\n\n{merged}"
    if after:
        merged = f"{merged}\n\n{after}"
    return merged.strip()


def _offer_page_signal_text(page_data: dict[str, Any]) -> str:
    """Collect page identity signals used to detect offer pages."""
    meta = page_data.get("meta") or {}
    parts = [
        page_data.get("source_url") or page_data.get("url") or "",
        page_data.get("title") or "",
        page_data.get("manual_label") or "",
        page_data.get("manual_name") or "",
        meta.get("description") or "",
        meta.get("og:title") or "",
    ]
    return " ".join(part for part in parts if part)


def _page_looks_like_offer(page_data: dict[str, Any]) -> bool:
    """Return True when page identity strongly suggests offer content."""
    return bool(_OFFER_SIGNAL_RE.search(_offer_page_signal_text(page_data)))


def _dedupe_pages_by_source(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate pages while preserving their first occurrence."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []

    for page in pages:
        source = (
            page.get("source_url")
            or page.get("url")
            or page.get("title")
            or str(id(page))
        )
        if source in seen:
            continue
        seen.add(source)
        deduped.append(page)

    return deduped


def _collect_offer_pages(
    cleaned_pages: list[dict[str, Any]],
    section_groups: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Collect pages that likely contain property offer details."""
    offer_pages = list(section_groups.get("special_offers", []))
    offer_pages.extend(page for page in cleaned_pages if _page_looks_like_offer(page))
    return _dedupe_pages_by_source(offer_pages)


def _offer_section_richness(section_block: str) -> int:
    """Score how detailed an offers section is."""
    if not section_block or not section_block.strip():
        return 0

    richness = 0
    richness += section_block.count("## ") * 4
    richness += section_block.count("\n- ") * 2
    richness += section_block.count("Field: ")
    richness += min(len(section_block.splitlines()), 25)
    if "Status: Not found on reviewed pages." in section_block:
        richness -= 20
    return richness


async def _build_offer_kb_text(
    property_name: str,
    base_url: str,
    offer_pages: list[dict[str, Any]],
) -> str:
    """Generate a dedicated offers section from offer-specific pages."""
    offer_texts = [
        page.get("_cleaned_text", "")
        for page in offer_pages
        if page.get("_cleaned_text", "").strip()
    ]
    if not offer_texts:
        return ""

    merged_offer_text = merge_texts(offer_texts)
    merged_offer_text = truncate_to_chars(merged_offer_text, _MAX_OFFER_KB_INPUT)

    offer_prompt = _OFFER_EXTRACTION_USER_PROMPT.format(
        property_name=property_name,
        url=base_url,
        content=merged_offer_text,
    )

    offer_result = await _call_llm(
        system_prompt=_OFFER_EXTRACTION_SYSTEM_PROMPT,
        user_prompt=offer_prompt,
        max_tokens=settings.llm_max_tokens,
    )

    normalized = _normalize_offer_section_text(offer_result)
    if "Status: Not found on reviewed pages." in normalized:
        return ""
    return normalized


# ── Text Chunking ────────────────────────────────────────────────────────────


def chunk_text(text: str, max_chars: int = 25000) -> list[str]:
    """Split text into chunks without cutting mid-paragraph.

    Splitting strategy (in order of preference):
      1. Split on double-newline paragraph boundaries
      2. Split on single-newline boundaries
      3. Split on sentence boundaries (". ")

    Each chunk will be at most max_chars long.

    Args:
        text: Text to split.
        max_chars: Maximum characters per chunk.

    Returns:
        List of text chunks.
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []

    while text:
        if len(text) <= max_chars:
            chunks.append(text.strip())
            break

        # Try to find a split point within the max_chars window
        window = text[:max_chars]
        split_pos = None

        # Strategy 1: Split on paragraph boundary (double newline)
        last_para = window.rfind("\n\n")
        if last_para > max_chars * 0.3:  # don't split too early
            split_pos = last_para + 2  # include the double newline in current chunk conceptually

        # Strategy 2: Split on single newline
        if split_pos is None:
            last_newline = window.rfind("\n")
            if last_newline > max_chars * 0.3:
                split_pos = last_newline + 1

        # Strategy 3: Split on sentence boundary
        if split_pos is None:
            last_sentence = window.rfind(". ")
            if last_sentence > max_chars * 0.3:
                split_pos = last_sentence + 2  # after the ". "

        # Fallback: split on last space
        if split_pos is None:
            last_space = window.rfind(" ")
            if last_space > max_chars * 0.3:
                split_pos = last_space + 1

        # Absolute fallback: hard split
        if split_pos is None:
            split_pos = max_chars

        chunk = text[:split_pos].strip()
        if chunk:
            chunks.append(chunk)
        text = text[split_pos:].strip()

    return chunks


# ── Prompt Building ──────────────────────────────────────────────────────────


def build_sections_list() -> str:
    """Build a formatted string listing all KB sections for the LLM prompt.

    Format:
        1. Property Overview -- Hotel name, brand, star rating...
        2. Rooms & Suites -- All room types with: name, size...

    Returns:
        Numbered list of sections with descriptions.
    """
    lines: list[str] = []
    for i, section in enumerate(KB_SECTIONS, start=1):
        title = section.get("title", "")
        description = section.get("description", "")
        lines.append(f"{i}. {title} -- {description}")
    return "\n".join(lines)


def _humanize_structured_key(key: str) -> str:
    """Convert schema.org style keys into readable field names."""
    cleaned = (key or "").replace("@", "")
    cleaned = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", cleaned)
    return cleaned.replace("_", " ").strip().title()


def _compact_structured_text(value: Any) -> str:
    """Normalize whitespace for structured-data values."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _structured_address_text(value: dict[str, Any]) -> str:
    """Render a PostalAddress object as one readable line."""
    bits: list[str] = []
    for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
        text = _compact_structured_text(value.get(key, ""))
        if text:
            bits.append(text)
    return ", ".join(bits)


def _structured_rating_text(value: dict[str, Any], *, aggregate: bool) -> str:
    """Render schema rating objects into short readable text."""
    rating = _compact_structured_text(value.get("ratingValue", ""))
    best = _compact_structured_text(value.get("bestRating", ""))
    count = _compact_structured_text(value.get("reviewCount", ""))

    if aggregate and rating and count:
        suffix = f"/{best}" if best else ""
        return f"{rating}{suffix} from {count} reviews"
    if rating:
        return f"{rating}/{best}" if best else rating
    return ""


def _structured_list_names(values: list[Any]) -> str:
    """Join useful names/values from a schema list."""
    names: list[str] = []
    for item in values:
        if isinstance(item, dict):
            name = _compact_structured_text(item.get("name", ""))
            if not name and "value" in item:
                name = _compact_structured_text(item.get("value", ""))
            if name:
                names.append(name)
        elif isinstance(item, (str, int, float)):
            text = _compact_structured_text(item)
            if text:
                names.append(text)
    deduped = list(dict.fromkeys(names))
    return ", ".join(deduped[:12])


def _structured_fact_value(key: str, value: Any) -> str:
    """Render one schema.org field into compact text."""
    if value in (None, "", [], {}):
        return ""

    if isinstance(value, (str, int, float, bool)):
        return _compact_structured_text(value)

    if isinstance(value, dict):
        if key == "address":
            return _structured_address_text(value)
        if key == "geo":
            latitude = _compact_structured_text(value.get("latitude", ""))
            longitude = _compact_structured_text(value.get("longitude", ""))
            if latitude and longitude:
                return f"{latitude}, {longitude}"
            return latitude or longitude
        if key == "starRating":
            return _structured_rating_text(value, aggregate=False)
        if key == "aggregateRating":
            return _structured_rating_text(value, aggregate=True)
        if key == "floorSize":
            size = _compact_structured_text(value.get("value", ""))
            unit = _compact_structured_text(value.get("unitText", "") or value.get("unitCode", ""))
            return " ".join(part for part in (size, unit) if part)
        if key == "occupancy":
            maximum = _compact_structured_text(value.get("maxValue", "") or value.get("value", ""))
            minimum = _compact_structured_text(value.get("minValue", ""))
            if minimum and maximum:
                return f"{minimum}-{maximum}"
            return maximum or minimum
        for nested_key in ("name", "description", "value"):
            text = _compact_structured_text(value.get(nested_key, ""))
            if text:
                return text
        return ""

    if isinstance(value, list):
        return _structured_list_names(value)

    return ""


def _walk_structured_items(node: Any, collector: list[dict[str, Any]]) -> None:
    """Walk nested JSON-LD objects and collect dict nodes."""
    if isinstance(node, list):
        for item in node:
            _walk_structured_items(item, collector)
        return

    if not isinstance(node, dict):
        return

    collector.append(node)
    for nested_key in ("@graph", "mainEntity", "hasPart", "subjectOf", "about"):
        if nested_key in node:
            _walk_structured_items(node[nested_key], collector)


def _summarize_structured_data(structured: list[dict[str, Any]]) -> list[str]:
    """Convert JSON-LD objects into compact fact lines for the LLM."""
    items: list[dict[str, Any]] = []
    _walk_structured_items(structured, items)

    interesting_keys = (
        "name",
        "headline",
        "description",
        "telephone",
        "email",
        "priceRange",
        "currenciesAccepted",
        "checkinTime",
        "checkoutTime",
        "address",
        "geo",
        "starRating",
        "aggregateRating",
        "amenityFeature",
        "servesCuisine",
        "openingHours",
        "openingHoursSpecification",
        "floorSize",
        "occupancy",
        "bed",
        "petsAllowed",
        "smokingAllowed",
        "maximumAttendeeCapacity",
    )

    lines: list[str] = []
    seen: set[str] = set()

    for item in items:
        type_value = item.get("@type", "")
        if isinstance(type_value, list):
            type_label = ", ".join(_compact_structured_text(part) for part in type_value if part)
        else:
            type_label = _compact_structured_text(type_value) or "Thing"

        facts: list[str] = []
        for key in interesting_keys:
            rendered = _structured_fact_value(key, item.get(key))
            if rendered:
                facts.append(f"{_humanize_structured_key(key)}: {rendered}")

        if not facts:
            continue

        line = f"Structured Data ({type_label}): " + " | ".join(facts[:12])
        normalized = line.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(line)

    return lines[:20]


def _asset_search_text(asset: dict[str, Any]) -> str:
    """Build a lowercase search string from asset metadata."""
    parts = [
        str(asset.get("name") or "").strip(),
        str(asset.get("label") or "").strip(),
        str(asset.get("url") or "").strip(),
    ]
    return " ".join(part.lower() for part in parts if part)


def _should_extract_file_asset(asset: dict[str, Any]) -> bool:
    """Return True for selected PDF files that are likely to contain KB facts."""
    if str(asset.get("type") or "").strip().lower() != "file":
        return False

    search_text = _asset_search_text(asset)
    if ".pdf" not in search_text:
        return False

    return any(keyword in search_text for keyword in _ASSET_TEXT_KEYWORDS)


async def _extract_pdf_text_from_url(url: str) -> str:
    """Fetch a PDF and extract a bounded amount of text for KB enrichment."""
    if not url:
        return ""

    cached = _ASSET_TEXT_CACHE.get(url)
    if cached is not None:
        return cached

    text = ""

    try:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.content

        if not payload:
            logger.debug("Selected PDF asset returned an empty response: %s", url)
        elif len(payload) > _MAX_ASSET_PDF_BYTES:
            logger.warning("Skipping oversized PDF asset (%d bytes): %s", len(payload), url)
        else:
            reader = PdfReader(io.BytesIO(payload))
            page_chunks: list[str] = []
            used_chars = 0

            for page in reader.pages[:_MAX_ASSET_PDF_PAGES]:
                try:
                    extracted = page.extract_text() or ""
                except Exception as exc:
                    logger.debug("Failed to extract a PDF page from %s: %s", url, exc)
                    continue

                normalized = re.sub(r"\s+", " ", extracted).strip()
                if not normalized:
                    continue

                remaining = _MAX_ASSET_PDF_CHARS - used_chars
                if remaining <= 0:
                    break

                snippet = normalized[:remaining]
                page_chunks.append(snippet)
                used_chars += len(snippet)

            if page_chunks:
                text = truncate_to_chars(" ".join(page_chunks), _MAX_ASSET_PDF_CHARS).strip()

    except Exception as exc:
        logger.warning("Failed to extract selected PDF asset '%s': %s", url, exc)

    _ASSET_TEXT_CACHE[url] = text
    return text


async def _enrich_pages_with_asset_text(extracted_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach extracted text for selected PDF assets to page payloads."""
    enriched_pages: list[dict[str, Any]] = []

    for page_data in extracted_pages:
        selected_assets = page_data.get("_selected_assets") or []
        if not selected_assets:
            enriched_pages.append(page_data)
            continue

        asset_texts: list[dict[str, Any]] = []
        for asset in selected_assets:
            if len(asset_texts) >= _MAX_ASSET_TEXTS_PER_PAGE:
                break
            if not _should_extract_file_asset(asset):
                continue

            asset_text = await _extract_pdf_text_from_url(str(asset.get("url") or ""))
            if not asset_text:
                continue

            asset_texts.append(
                {
                    "type": asset.get("type") or "file",
                    "name": asset.get("name") or "",
                    "label": asset.get("label") or "",
                    "url": asset.get("url") or "",
                    "text": asset_text,
                }
            )

        if asset_texts:
            enriched_page = dict(page_data)
            enriched_page["_selected_asset_texts"] = asset_texts
            enriched_pages.append(enriched_page)
        else:
            enriched_pages.append(page_data)

    return enriched_pages


# ── Content Preparation ─────────────────────────────────────────────────────


def _prepare_page_text(page_data: dict) -> str:
    """Extract and combine all useful text from a single page's extracted content.

    Merges main_text, table data, contact info, structured data, and
    meta descriptions into a single clean text block.

    Args:
        page_data: A single extracted_content dict.

    Returns:
        Combined text ready for cleaning.
    """
    parts: list[str] = []

    manual_entity_name = page_data.get("manual_entity_name") or ""
    manual_name = page_data.get("manual_name") or ""
    manual_label = page_data.get("manual_label") or ""
    if manual_entity_name or manual_name or manual_label:
        curated_bits = []
        if manual_entity_name:
            curated_bits.append(f"Property Name: {manual_entity_name}")
        if manual_name:
            curated_bits.append(f"Curated Page Name: {manual_name}")
        if manual_label:
            curated_bits.append(f"Curated Topic Label: {manual_label}")
        parts.append("Reviewed Metadata:\n" + "\n".join(curated_bits))

    # Title
    title = page_data.get("title") or ""
    if title.strip():
        parts.append(f"Page Title: {title.strip()}")

    # Source URL
    source_url = page_data.get("source_url") or ""
    if source_url:
        parts.append(f"Source: {source_url}")

    # Main text content
    main_text = page_data.get("main_text") or ""
    if main_text.strip():
        parts.append(main_text.strip())

    # Table data — convert to readable text
    tables = page_data.get("tables") or []
    for table in tables:
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if rows:
            table_lines = []
            if headers:
                table_lines.append(" | ".join(headers))
                table_lines.append("-" * (len(" | ".join(headers))))
            for row in rows:
                table_lines.append(" | ".join(str(cell) for cell in row))
            parts.append("Table:\n" + "\n".join(table_lines))

    # Contact information
    contacts = page_data.get("contacts") or {}
    contact_parts: list[str] = []
    phones = contacts.get("phones") or []
    if phones:
        contact_parts.append(f"Phone(s): {', '.join(phones)}")
    emails = contacts.get("emails") or []
    if emails:
        contact_parts.append(f"Email(s): {', '.join(emails)}")
    address = contacts.get("address") or ""
    if address:
        contact_parts.append(f"Address: {address}")
    if contact_parts:
        parts.append("Contact Info:\n" + "\n".join(contact_parts))

    # Structured data (JSON-LD) — extract key facts
    structured = page_data.get("structured_data") or []
    for item in structured:
        if isinstance(item, dict):
            # Extract useful fields from schema.org types
            sd_type = item.get("@type", "")
            sd_name = item.get("name", "")
            sd_desc = item.get("description", "")
            if sd_name or sd_desc:
                sd_text = f"Structured Data ({sd_type}): {sd_name}"
                if sd_desc:
                    sd_text += f" — {sd_desc}"
                parts.append(sd_text)

    structured_lines = _summarize_structured_data(structured)
    if structured_lines:
        parts.append("Structured Data:\n" + "\n".join(f"- {line}" for line in structured_lines))

    # Meta description (often contains useful summary)
    meta = page_data.get("meta") or {}
    og_title = meta.get("og:title") or meta.get("twitter:title") or ""
    if og_title.strip():
        parts.append(f"Meta Title: {og_title.strip()}")

    meta_desc = meta.get("description") or ""
    if meta_desc.strip() and meta_desc.strip() not in (main_text or ""):
        parts.append(f"Meta Description: {meta_desc.strip()}")

    og_desc = meta.get("og:description") or meta.get("twitter:description") or ""
    if og_desc.strip() and og_desc.strip() not in {meta_desc.strip(), (main_text or "").strip()}:
        parts.append(f"Meta Summary: {og_desc.strip()}")

    canonical = meta.get("canonical") or meta.get("og:url") or ""
    if canonical.strip():
        parts.append(f"Canonical URL: {canonical.strip()}")

    site_name = meta.get("og:site_name") or ""
    if site_name.strip():
        parts.append(f"Site Name: {site_name.strip()}")

    image_notes: list[str] = []
    for image in (page_data.get("images") or [])[:12]:
        title_bits = [
            bit.strip()
            for bit in [image.get("title") or "", image.get("alt") or "", image.get("description") or ""]
            if bit and bit.strip()
        ]
        if not title_bits:
            continue
        image_notes.append(" | ".join(dict.fromkeys(title_bits)))
    if image_notes:
        parts.append("Image References:\n" + "\n".join(f"- {note}" for note in image_notes))

    selected_assets = page_data.get("_selected_assets") or []
    if selected_assets:
        asset_lines = []
        for asset in selected_assets:
            asset_type = (asset.get("type") or "asset").title()
            asset_name = asset.get("name") or asset.get("url") or "Asset"
            asset_label = asset.get("label") or "Label"
            line = f"{asset_type}: {asset_name} | Label: {asset_label}"
            asset_lines.append(line)
        parts.append("Selected Assets:\n" + "\n".join(asset_lines))

    selected_asset_texts = page_data.get("_selected_asset_texts") or []
    if selected_asset_texts:
        asset_extract_blocks = []
        for asset in selected_asset_texts:
            title_bits = [
                bit.strip()
                for bit in [asset.get("label") or "", asset.get("name") or ""]
                if bit and bit.strip()
            ]
            heading = " | ".join(dict.fromkeys(title_bits)) or (asset.get("url") or "Selected File")
            asset_text = (asset.get("text") or "").strip()
            if not asset_text:
                continue
            asset_extract_blocks.append(f"{heading}\n{asset_text}")
        if asset_extract_blocks:
            parts.append("Asset Text Extracts:\n" + "\n\n".join(asset_extract_blocks))

    return "\n\n".join(parts)


# ── Main Pipeline ────────────────────────────────────────────────────────────


async def structure_property_kb(
    property_data: dict,
    progress_callback: ProgressCallback = None,
) -> str:
    """Convert a property's raw extracted content into a structured hotel KB.

    Pipeline steps:
      1. Clean all extracted texts
      2. Classify pages into KB sections
      3. Merge relevant page texts per section
      4. Chunk merged text for LLM context window
      5. Call LLM to extract structured data per chunk
      6. Merge chunk extractions
      7. Final consolidation pass to produce complete KB

    Args:
        property_data: Dict with keys: name, pages, extracted_content.
        progress_callback: Optional callable(step_name: str, pct: int)
            for reporting progress.

    Returns:
        The final KB text string.

    Raises:
        RuntimeError: If LLM calls fail after retries.
        ValueError: If property_data is missing required fields.
    """
    property_name = property_data.get("name") or "Unknown Hotel"
    pages = property_data.get("pages") or []
    extracted_content = property_data.get("extracted_content") or []
    property_data["offer_kb_text"] = ""

    if not extracted_content:
        logger.warning("No extracted content for property '%s'", property_name)
        return ""

    logger.info(
        "Starting KB structuring for '%s' (%d pages)",
        property_name,
        len(extracted_content),
    )

    def _report(step: str, pct: int) -> None:
        """Safely call the progress callback."""
        if progress_callback:
            try:
                progress_callback(step, pct)
            except Exception as exc:
                logger.debug("Progress callback error: %s", exc)

    # Determine a representative URL for the property
    base_url = ""
    if pages:
        base_url = pages[0].get("url", "")
    if not base_url and extracted_content:
        base_url = extracted_content[0].get("source_url", "")

    _report("asset_enrichment", 3)
    extracted_content = await _enrich_pages_with_asset_text(extracted_content)
    asset_extract_count = sum(len(page.get("_selected_asset_texts") or []) for page in extracted_content)
    if asset_extract_count:
        logger.info(
            "Attached %d selected asset text extracts for '%s'",
            asset_extract_count,
            property_name,
        )

    # ── Step 1: Clean all texts ──────────────────────────────────────────
    _report("cleaning", 5)
    logger.info("Step 1: Cleaning extracted content for '%s'", property_name)

    cleaned_pages: list[dict] = []
    raw_texts: list[str] = []

    for page_data in extracted_content:
        try:
            # Prepare combined text from all page fields
            combined = _prepare_page_text(page_data)

            # Apply cleaning pipeline
            cleaned = clean_text(combined)
            cleaned = remove_boilerplate(cleaned)

            if cleaned.strip():
                # Create a cleaned version of the page data
                cleaned_page = dict(page_data)
                cleaned_page["_cleaned_text"] = cleaned
                cleaned_pages.append(cleaned_page)
                raw_texts.append(cleaned)

        except Exception as exc:
            logger.warning(
                "Failed to clean page '%s': %s",
                (page_data.get("title") or "unknown")[:60],
                exc,
            )

    if not cleaned_pages:
        logger.warning("All pages empty after cleaning for '%s'", property_name)
        return ""

    # Deduplicate near-identical pages
    deduped_texts = deduplicate_content(raw_texts)
    # Rebuild cleaned_pages to match deduplicated texts
    deduped_pages: list[dict] = []
    deduped_text_set = set(deduped_texts)
    for page in cleaned_pages:
        if page.get("_cleaned_text") in deduped_text_set:
            deduped_pages.append(page)
            # Remove from set so exact duplicates only keep first occurrence
            deduped_text_set.discard(page["_cleaned_text"])

    cleaned_pages = deduped_pages if deduped_pages else cleaned_pages

    logger.info(
        "Cleaning complete: %d pages → %d after dedup",
        len(extracted_content),
        len(cleaned_pages),
    )

    _report("cleaning", 10)

    # ── Step 2: Classify pages into sections ─────────────────────────────
    _report("classifying", 15)
    logger.info("Step 2: Classifying pages into KB sections")

    section_groups = group_content_by_section(cleaned_pages)

    logger.info(
        "Classification complete: %d sections found — %s",
        len(section_groups),
        list(section_groups.keys()),
    )

    _report("classifying", 20)
    offer_pages = _collect_offer_pages(cleaned_pages, section_groups)

    # ── Step 3-5: Process each section with LLM ─────────────────────────
    sections_list = build_sections_list()
    chunk_extractions: list[str] = []
    total_sections = len(section_groups) if section_groups else 1
    sections_done = 0

    for section_id, section_pages in section_groups.items():
        section_title = section_id  # fallback
        for s in KB_SECTIONS:
            if s["id"] == section_id:
                section_title = s["title"]
                break

        logger.info(
            "Processing section '%s' (%d pages)",
            section_title,
            len(section_pages),
        )

        # Step 3: Merge relevant page texts for this section
        section_texts = [
            p.get("_cleaned_text", "") for p in section_pages
            if p.get("_cleaned_text", "").strip()
        ]

        if not section_texts:
            sections_done += 1
            continue

        merged_text = merge_texts(section_texts)

        # Step 4: Chunk the merged text
        text_chunks = chunk_text(merged_text, max_chars=25000)

        logger.debug(
            "Section '%s': %d chars merged, %d chunks",
            section_title,
            len(merged_text),
            len(text_chunks),
        )

        # Step 5: Call LLM for each chunk
        for chunk_idx, chunk in enumerate(text_chunks):
            try:
                user_prompt = EXTRACTION_USER_PROMPT.format(
                    sections_list=sections_list,
                    url=base_url,
                    property_name=property_name,
                    content=chunk,
                )

                extraction = await _call_llm(
                    system_prompt=EXTRACTION_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    max_tokens=settings.llm_max_tokens,
                )

                if extraction.strip():
                    chunk_extractions.append(extraction)
                    logger.debug(
                        "Extracted %d chars from section '%s' chunk %d/%d",
                        len(extraction),
                        section_title,
                        chunk_idx + 1,
                        len(text_chunks),
                    )

            except Exception as exc:
                logger.error(
                    "LLM extraction failed for section '%s' chunk %d: %s",
                    section_title,
                    chunk_idx + 1,
                    exc,
                )
                # Continue with other chunks — partial KB is better than none

        sections_done += 1
        pct = 20 + int((sections_done / total_sections) * 50)
        _report("extracting", min(pct, 70))

    if not chunk_extractions:
        logger.error("No extractions produced for property '%s'", property_name)
        return ""

    logger.info(
        "Extraction complete: %d chunk extractions for '%s'",
        len(chunk_extractions),
        property_name,
    )

    # ── Step 6-7: Merge and consolidate ──────────────────────────────────
    _report("consolidating", 75)
    logger.info("Step 6-7: Merging and consolidating KB for '%s'", property_name)

    # If we only have one extraction, we might still want a consolidation pass
    # to ensure consistent formatting
    merged_chunks = "\n\n--- EXTRACTION ---\n\n".join(chunk_extractions)

    # Truncate if the merged text is too long for the consolidation prompt
    # GPT-4o has a large context window, but we stay conservative
    max_consolidation_input = 80000
    if len(merged_chunks) > max_consolidation_input:
        merged_chunks = truncate_to_chars(merged_chunks, max_consolidation_input)
        logger.warning(
            "Merged extractions truncated to %d chars for consolidation",
            max_consolidation_input,
        )

    try:
        merge_prompt = MERGE_PROMPT.format(
            property_name=property_name,
            url=base_url,
            chunks=merged_chunks,
        )

        _report("consolidating", 80)

        final_kb = await _call_llm(
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            user_prompt=merge_prompt,
            max_tokens=settings.llm_max_tokens,
        )

        _report("consolidating", 95)

        if not final_kb.strip():
            # Fallback: use the raw merged extractions
            logger.warning(
                "Consolidation returned empty result for '%s', using raw extractions",
                property_name,
            )
            final_kb = merged_chunks

        offer_kb_text = ""
        if offer_pages:
            try:
                offer_kb_text = await _build_offer_kb_text(property_name, base_url, offer_pages)
            except Exception as exc:
                logger.warning(
                    "Offer-only extraction failed for '%s': %s",
                    property_name,
                    exc,
                )

        if offer_kb_text:
            existing_offer_block = _extract_section_block(final_kb, _SPECIAL_OFFERS_TITLE)
            if _offer_section_richness(offer_kb_text) >= _offer_section_richness(existing_offer_block):
                final_kb = _replace_or_append_section(final_kb, _SPECIAL_OFFERS_TITLE, offer_kb_text)
            property_data["offer_kb_text"] = offer_kb_text

        logger.info(
            "KB structuring complete for '%s': %d chars",
            property_name,
            len(final_kb),
        )

        _report("complete", 100)
        return final_kb.strip()

    except Exception as exc:
        logger.error(
            "Final consolidation failed for '%s': %s — returning raw extractions",
            property_name,
            exc,
        )
        _report("consolidation_failed", 90)
        # Return the un-consolidated extractions as a fallback
        return merged_chunks.strip()
