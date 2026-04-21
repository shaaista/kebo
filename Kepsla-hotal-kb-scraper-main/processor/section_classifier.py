"""Classify extracted page content into hotel KB sections."""

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Each section ID maps to a list of keyword patterns (case-insensitive).
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "rooms_and_suites": [
        "room", "suite", "accommodation", "stay", "guest room",
        "deluxe", "premier", "executive room", "presidential suite",
        "standard room", "superior room", "bedroom", "bed type",
        "occupancy", "room type", "room rate", "room amenities",
        "twin room", "double room", "single room", "family room",
        "penthouse", "villa", "cottage", "bungalow",
    ],
    "dining_restaurants": [
        "restaurant", "dining", "menu", "bar", "cafe", "lounge",
        "cuisine", "breakfast", "lunch", "dinner", "buffet",
        "room service", "in-room dining", "chef", "culinary",
        "wine", "cocktail", "pub", "bistro", "brasserie",
        "food", "eat", "dine", "grill", "kitchen",
        "all-day dining", "fine dining",
    ],
    "spa_wellness": [
        "spa", "wellness", "massage", "treatment", "therapy",
        "sauna", "steam", "jacuzzi", "hot tub", "facial",
        "body wrap", "aromatherapy", "ayurveda", "beauty",
        "relaxation", "rejuvenation", "health club",
        "yoga", "meditation", "holistic",
    ],
    "meetings_events": [
        "meeting", "event", "conference", "banquet", "wedding",
        "ballroom", "convention", "seminar", "function",
        "corporate event", "celebration", "reception",
        "board room", "boardroom", "event space", "venue",
        "mice", "incentive", "exhibition", "gala",
        "social event", "catering",
    ],
    "amenities_facilities": [
        "pool", "swimming", "gym", "fitness", "amenity", "amenities",
        "facility", "facilities", "parking", "wifi", "wi-fi",
        "business center", "business centre", "concierge",
        "laundry", "dry cleaning", "kids club", "children",
        "playground", "recreation", "game room", "library",
        "tennis", "golf", "water sport", "bicycle",
        "accessible", "accessibility", "disability",
        "gift shop", "boutique", "salon",
    ],
    "policies_rules": [
        "policy", "policies", "cancellation", "check-in", "check-out",
        "checkin", "checkout", "pet", "smoking", "no smoking",
        "child policy", "extra bed", "cot", "crib",
        "damage deposit", "security deposit", "identification",
        "id requirement", "dress code", "age restriction",
        "terms and conditions", "house rules", "guest policy",
        "refund", "payment policy", "guarantee",
    ],
    "location_transport": [
        "location", "direction", "transport", "airport",
        "map", "nearby", "attraction", "distance",
        "shuttle", "transfer", "taxi", "cab", "car rental",
        "how to reach", "getting here", "getting there",
        "train station", "railway", "metro", "bus",
        "landmark", "neighbourhood", "neighborhood",
        "explore", "local area", "surroundings",
        "gps", "coordinates",
    ],
    "contact_info": [
        "contact", "phone", "email", "reservation",
        "reservations", "book now", "booking", "enquiry",
        "inquiry", "reach us", "get in touch", "call us",
        "write to us", "customer service", "front desk",
        "reception", "toll free", "toll-free", "helpline",
        "feedback", "support",
    ],
    "special_offers": [
        "offer", "package", "deal", "promotion", "discount",
        "special", "seasonal", "limited time", "early bird",
        "last minute", "loyalty", "reward", "member",
        "gift card", "voucher", "coupon", "complimentary",
        "inclusive", "bundle", "value", "save",
        "honeymoon package", "weekend getaway", "staycation",
        "corporate rate", "long stay", "extended stay",
    ],
}

_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {}
for _section_id, _keywords in _SECTION_KEYWORDS.items():
    sorted_kw = sorted(_keywords, key=len, reverse=True)
    escaped = [re.escape(keyword) for keyword in sorted_kw]
    _SECTION_PATTERNS[_section_id] = re.compile(
        r"\b(?:" + "|".join(escaped) + r")\b",
        re.IGNORECASE,
    )

_SECTION_MIN_SCORES: dict[str, float] = {
    "rooms_and_suites": 2.0,
    "dining_restaurants": 2.0,
    "spa_wellness": 2.5,
    "meetings_events": 2.5,
    "amenities_facilities": 2.5,
    "policies_rules": 3.0,
    "location_transport": 3.0,
    "contact_info": 3.0,
    "special_offers": 3.0,
}

_SECTION_BODY_ONLY_MIN_SCORES: dict[str, float] = {
    "spa_wellness": 3.5,
    "meetings_events": 4.0,
    "amenities_facilities": 4.0,
    "policies_rules": 4.5,
    "location_transport": 4.5,
    "contact_info": 4.5,
    "special_offers": 4.5,
}

_SECTION_PAGE_LIMITS: dict[str, int] = {
    "property_overview": 4,
    "rooms_and_suites": 12,
    "dining_restaurants": 6,
    "spa_wellness": 4,
    "meetings_events": 5,
    "amenities_facilities": 6,
    "policies_rules": 4,
    "location_transport": 4,
    "contact_info": 4,
    "special_offers": 4,
}

_ROOM_LIKE_PAGE_RE = re.compile(
    r"\b(room|suite|accommodation|villa|cottage|tent|daalan|kholi)\b",
    re.IGNORECASE,
)
_HISTORY_LIKE_PAGE_RE = re.compile(
    r"\b(history|heritage|legacy|fort[-\s]?history)\b",
    re.IGNORECASE,
)
_DIRECT_CONTACT_PAGE_RE = re.compile(
    r"\b(contact|contact us|reach us|get in touch|call us|write to us|"
    r"reservations?|reservation desk|enquiry|inquiry|customer service|"
    r"support|front desk|reception)\b",
    re.IGNORECASE,
)

_ROOM_PAGE_PENALTIES: dict[str, float] = {
    "contact_info": 1.5,
    "policies_rules": 1.5,
    "location_transport": 1.5,
    "special_offers": 1.5,
}

_HISTORY_PAGE_PENALTIES: dict[str, float] = {
    "spa_wellness": 1.5,
    "special_offers": 1.5,
}

_ROOM_LIKE_PRIMARY_REQUIRED = {
    "contact_info",
    "dining_restaurants",
    "location_transport",
    "spa_wellness",
    "special_offers",
}

_HISTORY_LIKE_PRIMARY_REQUIRED = {
    "amenities_facilities",
    "contact_info",
    "spa_wellness",
    "special_offers",
}


def _score_text_for_section(text: str, section_id: str) -> int:
    """Count how many keyword matches a text has for a given section."""
    pattern = _SECTION_PATTERNS.get(section_id)
    if not pattern:
        return 0
    return len(pattern.findall(text))


def _build_page_signals(page_data: dict) -> tuple[str, str, str, str]:
    """Collect normalized page signals used for section scoring."""
    title = (page_data.get("title") or "").lower()
    main_text = (page_data.get("_cleaned_text") or page_data.get("main_text") or "").lower()
    source_url = page_data.get("source_url") or page_data.get("url") or ""

    url_path = ""
    try:
        parsed = urlparse(source_url)
        url_path = parsed.path.replace("/", " ").replace("-", " ").replace("_", " ").lower()
    except Exception:
        pass

    meta = page_data.get("meta") or {}
    meta_desc = (meta.get("description") or "").lower()
    return title, main_text, url_path, meta_desc


def score_page_sections(page_data: dict) -> dict[str, float]:
    """Return qualifying section scores for a page."""
    if not page_data:
        return {}

    try:
        title, main_text, url_path, meta_desc = _build_page_signals(page_data)
        page_descriptor = " ".join(part for part in (title, url_path) if part)
        is_room_like = bool(_ROOM_LIKE_PAGE_RE.search(page_descriptor))
        is_history_like = bool(_HISTORY_LIKE_PAGE_RE.search(page_descriptor))
        is_root_page = not url_path.strip()
        contacts = page_data.get("contacts") or {}
        has_contacts = bool(
            contacts.get("phones") or contacts.get("emails") or contacts.get("address")
        )
        direct_contact_page_signal = bool(_DIRECT_CONTACT_PAGE_RE.search(page_descriptor))

        section_scores: dict[str, float] = {}
        fallback_scores: dict[str, float] = {}

        for section_id in _SECTION_KEYWORDS:
            title_score = _score_text_for_section(title, section_id) * 3.0
            url_score = _score_text_for_section(url_path, section_id) * 2.0
            meta_score = _score_text_for_section(meta_desc, section_id) * 1.5

            body_sample = main_text[:3000]
            body_score = _score_text_for_section(body_sample, section_id) * 1.0

            total = title_score + url_score + meta_score + body_score
            if section_id == "contact_info" and is_root_page and has_contacts:
                total += 4.0
            if total <= 0:
                continue

            strong_primary_signal = title_score > 0 or url_score > 0
            contact_root_signal = section_id == "contact_info" and is_root_page and has_contacts
            primary_signal = strong_primary_signal or meta_score > 0 or contact_root_signal
            relevance_primary_signal = strong_primary_signal
            if section_id == "contact_info":
                primary_signal = direct_contact_page_signal or contact_root_signal
                relevance_primary_signal = primary_signal
            if section_id == "contact_info" and not primary_signal:
                continue
            if is_room_like and not relevance_primary_signal and section_id in _ROOM_LIKE_PRIMARY_REQUIRED:
                continue
            if is_history_like and not relevance_primary_signal and section_id in _HISTORY_LIKE_PRIMARY_REQUIRED:
                continue
            if not primary_signal and is_room_like:
                total -= _ROOM_PAGE_PENALTIES.get(section_id, 0.0)
            if not primary_signal and is_history_like:
                total -= _HISTORY_PAGE_PENALTIES.get(section_id, 0.0)
            if total <= 0:
                continue

            fallback_scores[section_id] = total

            min_score = _SECTION_MIN_SCORES.get(section_id, 2.0)
            if total < min_score:
                continue

            body_only_min = _SECTION_BODY_ONLY_MIN_SCORES.get(section_id, min_score)
            if not primary_signal and body_score < body_only_min:
                continue

            section_scores[section_id] = total

        if section_scores:
            return dict(sorted(section_scores.items(), key=lambda item: item[1], reverse=True))

        if fallback_scores:
            best_section, best_score = max(fallback_scores.items(), key=lambda item: item[1])
            if best_score >= 1.0:
                return {best_section: best_score}

        return {}
    except Exception as exc:
        logger.warning("Page classification failed: %s", exc)
        return {}


def classify_page(page_data: dict) -> list[str]:
    """Classify a single page into one or more hotel KB sections."""
    if not page_data:
        return ["property_overview"]

    section_scores = score_page_sections(page_data)
    if not section_scores:
        return ["property_overview"]

    sections = list(section_scores.keys())
    logger.debug(
        "Classified page '%s' into sections: %s (scores: %s)",
        (page_data.get("title") or "")[:60],
        sections,
        {section: round(score, 1) for section, score in section_scores.items()},
    )
    return sections


def _canonical_page_key(page_data: dict) -> str:
    """Create a stable page key so www/non-www duplicates collapse together."""
    source_url = page_data.get("source_url") or page_data.get("url") or ""
    if not source_url:
        return ""

    try:
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = re.sub(r"/+", "/", parsed.path or "/")
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return f"{host}{path}"
    except Exception:
        return source_url.strip().lower()


def group_content_by_section(pages_data: list[dict]) -> dict[str, list[dict]]:
    """Group a list of extracted page contents by KB section."""
    if not pages_data:
        return {}

    section_groups: dict[str, list[tuple[float, dict]]] = {}

    for page in pages_data:
        if not page:
            continue

        try:
            section_scores = score_page_sections(page)
            if not section_scores:
                section_scores = {"property_overview": 1.0}

            for section_id, score in section_scores.items():
                section_groups.setdefault(section_id, []).append((score, page))
        except Exception as exc:
            logger.warning(
                "Failed to classify page '%s': %s",
                (page.get("title") or "unknown")[:60],
                exc,
            )
            section_groups.setdefault("property_overview", []).append((1.0, page))

    limited_groups: dict[str, list[dict]] = {}
    for section_id, scored_pages in section_groups.items():
        seen_keys: set[str] = set()
        ranked_pages: list[dict] = []
        for _, page in sorted(scored_pages, key=lambda item: item[0], reverse=True):
            page_key = _canonical_page_key(page)
            if page_key and page_key in seen_keys:
                continue
            if page_key:
                seen_keys.add(page_key)
            ranked_pages.append(page)

        limit = _SECTION_PAGE_LIMITS.get(section_id, len(ranked_pages))
        limited_groups[section_id] = ranked_pages[:limit]

    if limited_groups:
        summary = {section_id: len(pages) for section_id, pages in limited_groups.items()}
        logger.info(
            "Grouped %d pages into %d sections: %s",
            len(pages_data),
            len(limited_groups),
            summary,
        )

    return limited_groups
