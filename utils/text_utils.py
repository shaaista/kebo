"""
Text utility functions for menu parsing and order handling.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Optional


_PRICE_PATTERN = re.compile(
    r"(?:(?:rs|inr)\.?\s*)?"
    r"(?:₹\s*)?"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*/-)?",
    re.IGNORECASE,
)

_NON_VEG_PATTERN = re.compile(
    r"\b(non[-\s]?veg|nonveg|nv)\b",
    re.IGNORECASE,
)

_VEG_PATTERN = re.compile(
    r"\b(veg|vegetarian|pure\s*veg)\b|\(\s*v\s*\)|\[\s*v\s*\]",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """Lowercase, remove extra whitespace, remove special chars."""
    if not text:
        return ""
    lowered = text.lower()
    # Replace non-alphanumeric characters with spaces
    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    # Collapse whitespace
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_price(text: str) -> Optional[float]:
    """Extract price from 'Rs. 450', '450/-', 'INR 450'."""
    if not text:
        return None
    match = _PRICE_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def is_vegetarian_indicator(text: str) -> Optional[bool]:
    """Check for 'veg', 'vegetarian', or non-veg indicators."""
    if not text:
        return None
    norm = normalize_text(text)
    if _NON_VEG_PATTERN.search(norm):
        return False
    if _VEG_PATTERN.search(norm):
        return True
    return None


def clean_menu_item_name(text: str) -> str:
    """Remove price, remove (V)/(NV), proper capitalization."""
    if not text:
        return ""
    # Remove price segments
    no_price = _PRICE_PATTERN.sub("", text)
    # Remove veg / non-veg markers
    no_markers = re.sub(r"\b(non[-\s]?veg|nonveg|nv|veg|vegetarian)\b", "", no_price, flags=re.IGNORECASE)
    no_markers = re.sub(r"[\(\[\{]\s*[vV]\s*[\)\]\}]", "", no_markers)
    # Remove leftover symbols and collapse spaces
    cleaned = re.sub(r"[^\w\s]", " ", no_markers)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title()


def generate_order_id() -> str:
    """Generate 'ORD-20260204-A1B2C3' format."""
    date_part = datetime.now(UTC).strftime("%Y%m%d")
    rand_part = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
    return f"ORD-{date_part}-{rand_part}"
