"""Build review-ready content manifests for the UI content manager."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

_NAME_MAPPINGS = (
    ("home", "Home Page"),
    ("room", "Rooms & Suites"),
    ("suite", "Rooms & Suites"),
    ("dining", "Dining"),
    ("restaurant", "Dining"),
    ("menu", "Dining"),
    ("bar", "Dining"),
    ("spa", "Spa & Wellness"),
    ("wellness", "Spa & Wellness"),
    ("activity", "Activities"),
    ("experience", "Experiences"),
    ("contact", "Contact"),
    ("gallery", "Gallery"),
    ("amenit", "Amenities"),
    ("facility", "Facilities"),
    ("pool", "Pool & Beach"),
    ("beach", "Pool & Beach"),
    ("event", "Events & Weddings"),
    ("wedding", "Events & Weddings"),
    ("meeting", "Meetings"),
    ("offer", "Offers"),
    ("rate", "Rates"),
    ("tariff", "Rates"),
)

_LABEL_MAPPINGS = (
    ("home", "Main"),
    ("hero", "Hero"),
    ("banner", "Hero"),
    ("room", "Rooms"),
    ("suite", "Rooms"),
    ("dining", "Dining"),
    ("restaurant", "Dining"),
    ("menu", "Dining"),
    ("bar", "Dining"),
    ("spa", "Spa"),
    ("wellness", "Spa"),
    ("pool", "Facilities"),
    ("beach", "Facilities"),
    ("amenit", "Facilities"),
    ("facility", "Facilities"),
    ("activity", "Activities"),
    ("experience", "Activities"),
    ("event", "Events"),
    ("wedding", "Events"),
    ("meeting", "Events"),
    ("gallery", "Views"),
    ("view", "Views"),
    ("lobby", "Lobby"),
    ("contact", "Info"),
    ("about", "Info"),
    ("location", "Info"),
    ("map", "Info"),
    ("price", "Pricing"),
    ("rate", "Pricing"),
    ("tariff", "Pricing"),
    ("brochure", "Pricing"),
    ("pdf", "Pricing"),
)


def slugify(value: str) -> str:
    """Return a stable slug-like identifier."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "item"


def _title_case_tokens(raw: str) -> str:
    """Convert a slug or filename stem into a readable title."""
    text = re.sub(r"\.[a-z0-9]+$", "", raw or "", flags=re.IGNORECASE)
    text = re.sub(r"[_\-]+", " ", text).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text.title() if text else ""


def _filename_from_url(url: str) -> str:
    """Extract the filename from a URL path."""
    path = urlparse(url or "").path
    filename = Path(path).name
    return filename or url


def _display_path(url: str) -> str:
    """Create the compact path shown in the UI list."""
    parsed = urlparse(url or "")
    if parsed.path and parsed.path != "/":
        return parsed.path
    return f"/{parsed.netloc}/" if parsed.netloc else "/"


def _match_mapping(source_text: str, mappings: tuple[tuple[str, str], ...], fallback: str) -> str:
    """Return the first mapping whose token exists in source text."""
    haystack = (source_text or "").lower()
    for token, label in mappings:
        if token in haystack:
            return label
    return fallback


def suggest_page_name(url: str, title: str, property_name: str) -> str:
    """Suggest a clean human-readable page name."""
    parsed = urlparse(url or "")
    last_segment = Path(parsed.path.rstrip("/")).stem
    if not last_segment or last_segment.lower() in {"index", "home"} or parsed.path in {"", "/"}:
        return "Home Page"

    mapped = _match_mapping(last_segment, _NAME_MAPPINGS, "")
    if mapped:
        return mapped

    title = (title or "").strip()
    if title:
        for separator in (" | ", " - ", " – ", " — ", " :: "):
            if separator in title:
                title = title.split(separator, 1)[0].strip()
                break

        property_lower = (property_name or "").strip().lower()
        if title and title.lower() != property_lower:
            return title

    return _title_case_tokens(last_segment) or "Page"


def suggest_item_label(item_type: str, url: str, title: str = "", description: str = "") -> str:
    """Suggest a short chip label for pages and assets."""
    source = " ".join(
        part for part in [
            item_type,
            urlparse(url or "").path,
            title or "",
            description or "",
            _filename_from_url(url or ""),
        ]
        if part
    )
    fallback = "Label" if item_type != "page" else "Main"
    return _match_mapping(source, _LABEL_MAPPINGS, fallback)


def suggest_asset_name(url: str, title: str = "", description: str = "") -> str:
    """Suggest the display name for an asset item."""
    preferred = (title or "").strip()
    if preferred:
        return preferred

    filename = _filename_from_url(url)
    if filename:
        return filename

    if description:
        return description.strip()[:80]

    return url


def _build_page_item(entity_id: str, index: int, page: dict, extracted: dict, property_name: str) -> tuple[dict, dict]:
    """Build the UI manifest row and store payload for a page."""
    page_url = page.get("url", "")
    title = extracted.get("title") or page.get("title", "")
    suggested_name = suggest_page_name(page_url, title, property_name)
    suggested_label = suggest_item_label("page", page_url, suggested_name, title)
    item_id = f"{entity_id}-page-{index}"

    item = {
        "id": item_id,
        "type": "page",
        "name": suggested_name,
        "suggested_name": suggested_name,
        "label": suggested_label,
        "suggested_label": suggested_label,
        "enabled": True,
        "url": page_url,
        "display_path": _display_path(page_url),
        "preview_url": None,
        "meta_text": (title or "").strip(),
    }
    store = {
        "page": page,
        "extracted_content": extracted,
    }
    return item, store


def _build_asset_item(
    entity_id: str,
    item_type: str,
    index: int,
    asset: dict,
    *,
    page_url: str,
    page_item_id: str,
) -> dict:
    """Build the UI manifest row for an image, video, or file."""
    asset_url = asset.get("src") or asset.get("url") or ""
    title = asset.get("title") or asset.get("alt") or ""
    description = asset.get("description") or ""
    suggested_name = suggest_asset_name(asset_url, title, description)
    suggested_label = suggest_item_label(item_type, asset_url, suggested_name, description)

    enabled = suggested_label != "Label"
    item = {
        "id": f"{entity_id}-{item_type}-{index}",
        "type": item_type,
        "name": suggested_name,
        "suggested_name": suggested_name,
        "label": suggested_label,
        "suggested_label": suggested_label,
        "enabled": enabled,
        "url": asset_url,
        "display_path": _display_path(asset_url),
        "preview_url": asset_url if item_type == "image" else None,
        "meta_text": description or _display_path(page_url),
        "source_page": page_url,
        "source_page_item_id": page_item_id,
    }
    return item


def build_review_entity(
    property_name: str,
    property_pages: list[dict],
    extracted_pages: list[dict],
    *,
    entity_index: int,
) -> tuple[dict, dict[str, dict]]:
    """Create a single entity card manifest plus in-memory page payloads."""
    entity_id = f"entity-{entity_index}-{slugify(property_name)}"
    items: list[dict] = []
    page_store: dict[str, dict] = {}
    asset_seen: set[tuple[str, str]] = set()
    image_count = 0
    video_count = 0
    file_count = 0

    for page_index, (page, extracted) in enumerate(zip(property_pages, extracted_pages), start=1):
        page_item, page_payload = _build_page_item(
            entity_id,
            page_index,
            page,
            extracted,
            property_name,
        )
        items.append(page_item)
        page_store[page_item["id"]] = page_payload

        page_url = page.get("url", "")

        for image in extracted.get("images", []):
            image_url = image.get("src", "")
            key = ("image", image_url)
            if not image_url or key in asset_seen:
                continue
            asset_seen.add(key)
            image_count += 1
            items.append(
                _build_asset_item(
                    entity_id,
                    "image",
                    image_count,
                    image,
                    page_url=page_url,
                    page_item_id=page_item["id"],
                )
            )

        for video in extracted.get("videos", []):
            video_url = video.get("url", "")
            key = ("video", video_url)
            if not video_url or key in asset_seen:
                continue
            asset_seen.add(key)
            video_count += 1
            items.append(
                _build_asset_item(
                    entity_id,
                    "video",
                    video_count,
                    video,
                    page_url=page_url,
                    page_item_id=page_item["id"],
                )
            )

        for file_item in extracted.get("files", []):
            file_url = file_item.get("url", "")
            key = ("file", file_url)
            if not file_url or key in asset_seen:
                continue
            asset_seen.add(key)
            file_count += 1
            items.append(
                _build_asset_item(
                    entity_id,
                    "file",
                    file_count,
                    file_item,
                    page_url=page_url,
                    page_item_id=page_item["id"],
                )
            )

    enabled_count = sum(1 for item in items if item.get("enabled"))
    entity = {
        "id": entity_id,
        "name": property_name,
        "suggested_name": property_name,
        "enabled": True,
        "stats": {
            "pages": len(property_pages),
            "images": image_count,
            "videos": video_count,
            "files": file_count,
        },
        "enabled_count": enabled_count,
        "total_count": len(items),
        "items": items,
    }
    return entity, page_store


def build_review_data(
    *,
    job_id: str,
    source_url: str,
    project: dict,
    entities: list[dict],
) -> dict:
    """Wrap entity manifests in the top-level review payload."""
    return {
        "job_id": job_id,
        "source_url": source_url,
        "project": {
            "name": project.get("name") or "Grand Hotel Bot",
            "bot_enabled": bool(project.get("bot_enabled", True)),
            "language": project.get("language") or "English",
            "auto_sync": bool(project.get("auto_sync", False)),
            "specific_urls": project.get("specific_urls") or [],
        },
        "entities": entities,
    }
