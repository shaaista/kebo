"""Coordinates the entire scrape pipeline from URL discovery to content extraction.

The pipeline is split into TWO phases so the UI can pause for property selection:

  PHASE 1 (automatic):
    1. DISCOVERY  — find all crawlable pages
    2. CRAWLING   — fetch raw HTML/text for every page
    3. PROPERTY DETECTION — group pages by hotel property (URL-slug based)
    → PAUSE: return property list to UI for user selection

  PHASE 2 (user-triggered, only selected properties):
    4. EXTRACTION — extract structured content per page
    5. IMAGE DOWNLOAD — download property images + create Excel catalog
"""

import asyncio
import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config.settings import settings
from scraper.content_extractor import extract_content
from scraper.crawl_engine import crawl_page, crawl_pages_batch
from scraper.discovery import SiteBlockedError, discover_urls
from scraper.image_downloader import download_property_images
from scraper.url_filter import (
    deduplicate_urls,
    extract_domain,
    is_same_domain,
    normalize_url,
    prioritize_discovery_links,
    should_skip_url,
)

logger = logging.getLogger(__name__)

# Type alias for the callback the API layer passes in
UpdateCallback = Callable[..., Coroutine[Any, Any, None]] | None


# ── Property Detection ───────────────────────────────────────────────────────

# Top-level URL slugs that are NOT individual hotel properties
_NON_PROPERTY_SLUGS = {
    "our-destinations", "blogs", "blog", "offers", "offer", "about-us", "about",
    "weddings", "wedding", "experiences", "experience", "foodgully", "golden-tulip",
    "opening-soon", "contact", "contact-us", "privacy", "privacy-policy",
    "terms", "terms-and-conditions", "careers", "media", "newsroom", "news",
    "awards", "hotel-directory", "spin-and-win", "newsletter", "summer-offer",
    "flash-sale", "home-delivery", "lywwh", "beyond-consultancy", "partner-with-us",
    "events", "happiness-sarovar", "sustainability", "testimonials", "gallery",
    "faq", "sitemap", "search", "login", "register", "booking", "reservations",
    "loyalty", "rewards", "gift-cards", "press", "investors", "corporate",
    "our-legacy", "legacy", "ecotel", "orchidrewards", "orchid-membership-programs",
}

_PROPERTY_HINT_KEYWORDS = {
    "hotel", "resort", "spa", "villa", "inn", "suites", "stay", "residency",
    "retreat", "palace", "orchid", "ira",
}

_GENERIC_PROPERTY_PREFIXES = (
    "book ",
    "best ",
    "luxury ",
    "premium ",
    "top ",
    "official ",
    "hotel in ",
    "hotel near ",
    "hotel at ",
    "resort in ",
    "resort near ",
    "stay in ",
    "rooms in ",
    "banquet hall in ",
)

_GENERIC_PROPERTY_TERMS = {
    "airport",
    "banquet",
    "best",
    "book",
    "city",
    "deal",
    "dining",
    "hall",
    "halls",
    "hotel",
    "luxury",
    "near",
    "offer",
    "offers",
    "premium",
    "rate",
    "rates",
    "resort",
    "restaurant",
    "room",
    "rooms",
    "spa",
    "stay",
}

_LOCATION_STOP_WORDS = {
    "and",
    "at",
    "airport",
    "banquet",
    "best",
    "book",
    "by",
    "city",
    "for",
    "hall",
    "halls",
    "hotel",
    "hotels",
    "in",
    "india",
    "madhapur",
    "near",
    "of",
    "or",
    "packages",
    "railway",
    "rates",
    "resort",
    "rooms",
    "sector",
    "spa",
    "stay",
    "station",
    "the",
    "up",
    "with",
}


def _normalise_property_name(raw: str) -> str:
    """Convert a URL slug or raw string into a readable property name."""
    name = re.sub(r"[-_]+", " ", raw)
    name = re.sub(r"\s+\d+$", "", name)
    name = name.strip().title()
    return name if len(name) > 1 else ""


def _compact_text(raw: str) -> str:
    """Lowercase and strip separators to make loose URL/title matching easier."""
    return re.sub(r"[^a-z0-9]+", "", (raw or "").lower())


def _dedupe_strings(values: list[str]) -> list[str]:
    """Return strings in first-seen order, dropping empty/duplicate values."""
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", (value or "")).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _extract_page_signals(page: dict) -> list[str]:
    """Extract extra property-identifying signals from page HTML/meta content."""
    html = page.get("html", "") or ""
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []

    signals: list[str] = []
    for prop_name in ("og:title", "twitter:title", "og:site_name"):
        tag = soup.find("meta", attrs={"property": prop_name}) or soup.find("meta", attrs={"name": prop_name})
        if tag and tag.get("content"):
            signals.append(tag.get("content", ""))

    desc_tag = soup.find("meta", attrs={"name": "description"})
    if desc_tag and desc_tag.get("content"):
        signals.append(desc_tag.get("content", ""))

    h1 = soup.find("h1")
    if h1:
        signals.append(h1.get_text(" ", strip=True))

    return _dedupe_strings(signals)


def _split_property_candidates(text: str) -> list[str]:
    """Split a title/meta string into meaningful candidate segments."""
    if not text:
        return []

    split_parts = [
        part.strip()
        for part in re.split(r"\s+(?:\||-|–|—|::)\s+", text)
        if part.strip()
    ]
    return _dedupe_strings(split_parts + [text])


def _split_property_candidates_robust(text: str) -> list[str]:
    """Split metadata strings on ASCII and Unicode title separators."""
    if not text:
        return []

    robust_parts = [
        part.strip()
        for part in re.split(r"\s+(?:\||-|::|\u2013|\u2014)\s+", text)
        if part.strip()
    ]
    return _dedupe_strings(_split_property_candidates(text) + robust_parts + [text])


def _clean_property_candidate(text: str) -> str:
    """Normalize whitespace/punctuation for a potential property label."""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip(" -|:–—")
    cleaned = re.sub(
        r"\bby\s+([A-Za-z][A-Za-z\s&']+?)\s+Hotels\b",
        lambda match: f"by {match.group(1).strip()}",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^[\s\-\|\:\u2013\u2014]+|[\s\-\|\:\u2013\u2014]+$", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_branded_property_phrase(text: str) -> str | None:
    """Pull branded property names out of SEO-heavy titles and metadata."""
    if not text:
        return None

    normalized = re.sub(r"[^A-Za-z0-9&'/-]+", " ", text)
    tokens = re.findall(r"[A-Za-z0-9&'-]+", normalized)
    if not tokens:
        return None

    lower_tokens = [token.lower() for token in tokens]
    branded_prefixes = (
        (("ira", "by", "orchid", "hotels"), "IRA by Orchid"),
        (("ira", "by", "orchid"), "IRA by Orchid"),
        (("the", "orchid", "hotel"), "The Orchid Hotel"),
        (("orchid", "hotel"), "Orchid Hotel"),
    )
    stop_words = _GENERIC_PROPERTY_TERMS | {
        "a",
        "an",
        "airport",
        "best",
        "book",
        "business",
        "city",
        "deluxe",
        "domestic",
        "for",
        "in",
        "international",
        "jacuzzi",
        "luxury",
        "near",
        "official",
        "premium",
        "reserve",
        "star",
        "top",
        "with",
    }
    leading_fillers = {"a", "an", "the", "top", "best", "premium"}

    for prefix_tokens, canonical_prefix in branded_prefixes:
        prefix_len = len(prefix_tokens)
        for index in range(len(tokens) - prefix_len + 1):
            if tuple(lower_tokens[index:index + prefix_len]) != prefix_tokens:
                continue

            remaining_tokens = tokens[index + prefix_len:]
            remaining_lower = lower_tokens[index + prefix_len:]
            while remaining_tokens and remaining_lower and remaining_lower[0] in leading_fillers:
                remaining_tokens = remaining_tokens[1:]
                remaining_lower = remaining_lower[1:]

            location_tokens: list[str] = []
            for token, lowered in zip(remaining_tokens, remaining_lower):
                if location_tokens and not token[:1].isupper():
                    break
                if not location_tokens and not token[:1].isupper():
                    break
                lowered = token.lower()
                if lowered in stop_words or lowered.isdigit() or re.fullmatch(r"t\d+", lowered):
                    break
                if len(location_tokens) >= 4:
                    break
                location_tokens.append(token)

            if location_tokens:
                return _clean_property_candidate(f"{canonical_prefix} {' '.join(location_tokens)}")
            return _clean_property_candidate(canonical_prefix)

    return None


def _looks_generic_property_candidate(text: str) -> bool:
    """Return True when a candidate looks like SEO copy, not a property name."""
    if _extract_branded_property_phrase(text):
        return False

    candidate = _clean_property_candidate(text).lower()
    if not candidate:
        return True
    if candidate.startswith(_GENERIC_PROPERTY_PREFIXES):
        return True
    if re.match(r"^[a-z\s]+ hotel\b", candidate) and " by " not in candidate:
        return True

    tokens = re.findall(r"[a-z0-9]+", candidate)
    generic_hits = sum(1 for token in tokens if token in _GENERIC_PROPERTY_TERMS)
    if generic_hits >= max(3, len(tokens) // 2 + 1) and " by " not in candidate:
        return True

    return False


def _extract_location_name(text: str) -> str | None:
    """Extract a human-readable location from a title or metadata string."""
    if not text:
        return None

    text_lower = re.sub(r"\s+", " ", text.lower()).strip()
    patterns = (
        r"\b(?:hotel|resort|stay|villa|property|retreat)\s+(?:in|at|near)\s+([a-z][a-z\s&-]{2,60})",
        r"\b(?:rooms?|banquet(?: halls?)?|events?)\s+in\s+([a-z][a-z\s&-]{2,60})",
        r"^([a-z][a-z\s-]{2,40})\s+hotel\b",
        r"^([a-z][a-z\s-]{2,40})\s+resort\b",
    )

    for pattern in patterns:
        for match in re.findall(pattern, text_lower):
            words: list[str] = []
            for word in re.split(r"[^a-z]+", match):
                if not word or word in _LOCATION_STOP_WORDS:
                    break
                words.append(word)
                if len(words) >= 3:
                    break
            if words:
                return _normalise_property_name(" ".join(words))

    return None


def _extract_brand_segment(text: str) -> str | None:
    """Find the most property-like segment from a title/meta string."""
    branded_phrase = _extract_branded_property_phrase(text)
    if branded_phrase:
        return branded_phrase

    fallback_candidates: list[str] = []
    for candidate in _split_property_candidates_robust(text):
        cleaned = _clean_property_candidate(candidate)
        lowered = cleaned.lower()
        if not cleaned or _looks_generic_property_candidate(cleaned):
            continue
        is_composite_segment = "|" in cleaned or " - " in cleaned or "::" in cleaned
        if (
            not is_composite_segment
            and (" by " in lowered or any(keyword in lowered for keyword in _PROPERTY_HINT_KEYWORDS))
        ):
            return cleaned
        fallback_candidates.append(cleaned)

    if not fallback_candidates:
        return None

    connector_markers = (" near ", " for ", " with ", " in ", " at ")

    def _rank(candidate: str) -> tuple[int, int, int]:
        lowered = f" {candidate.lower()} "
        has_connectors = 1 if any(marker in lowered for marker in connector_markers) else 0
        token_count = len(re.findall(r"[a-z0-9]+", lowered))
        return has_connectors, token_count, len(candidate)

    return sorted(fallback_candidates, key=_rank)[0]


def _derive_property_display_name(
    page: dict,
    base_domain: str,
    *,
    fallback_slug: str | None = None,
) -> str | None:
    """Build a human-readable property name from page title, metadata, and slug."""
    texts = _dedupe_strings(_extract_page_signals(page) + [page.get("title", "") or ""])

    for text in texts:
        branded_phrase = _extract_branded_property_phrase(text)
        if branded_phrase and branded_phrase.lower() not in {"ira by orchid", "the orchid hotel", "orchid hotel"}:
            return branded_phrase

    brand_segment: str | None = None
    for text in texts:
        brand_segment = _extract_brand_segment(text)
        if brand_segment:
            break

    location_name: str | None = None
    for text in texts:
        location_name = _extract_location_name(text)
        if location_name:
            break

    if not location_name and fallback_slug:
        location_name = _normalise_property_name(fallback_slug)

    if brand_segment:
        fallback_name = _normalise_property_name(fallback_slug) if fallback_slug else ""
        if fallback_name and _compact_text(fallback_name) in _compact_text(brand_segment):
            return _clean_property_candidate(brand_segment)
        if location_name and _compact_text(location_name) not in _compact_text(brand_segment):
            return _clean_property_candidate(f"{brand_segment} {location_name}")
        return _clean_property_candidate(brand_segment)

    for text in texts:
        title_candidate = _extract_property_from_title(text, base_domain)
        if title_candidate and not _looks_generic_property_candidate(title_candidate):
            return _clean_property_candidate(title_candidate)

    if location_name:
        return _clean_property_candidate(location_name)

    return None


def _build_property_aliases(property_name: str, seed_pages: list[dict]) -> set[str]:
    """Build compact aliases used to keep deep crawling inside one property."""
    aliases: set[str] = set()
    path_seed_aliases: set[str] = set()
    signal_texts: list[str] = []

    if property_name:
        aliases.add(_compact_text(property_name))

    words = re.findall(r"[A-Za-z]+", property_name or "")
    for width in (1, 2, 3):
        if len(words) >= width:
            aliases.add(_compact_text(" ".join(words[-width:])))

    for page in seed_pages[:3]:
        page_url = page.get("url", "")
        path_stem = Path(urlparse(page_url).path).stem
        if path_stem:
            compact_path_stem = _compact_text(path_stem)
            aliases.add(compact_path_stem)
            aliases.add(_compact_text(_normalise_property_name(path_stem)))
            if compact_path_stem:
                path_seed_aliases.add(compact_path_stem)

        signal_texts.extend(_extract_page_signals(page) + [page.get("title", "") or ""])

    for text in _dedupe_strings(signal_texts):
        brand_segment = _extract_brand_segment(text)
        if brand_segment:
            aliases.add(_compact_text(brand_segment))

        # When the seed URL already exposes a property-specific slug, avoid
        # weakening the matcher with broad city/location aliases like "pune".
        if path_seed_aliases:
            continue

        aliases.update(_extract_location_aliases_from_title(text))
        location_name = _extract_location_name(text)
        if location_name:
            aliases.add(_compact_text(location_name))

    return {alias for alias in aliases if len(alias) >= 4}


def _seed_property_signatures(seed_url: str) -> set[str]:
    """Build strong seed-only signatures from the selected property's URL."""
    normalized_seed = normalize_url(seed_url)
    if not normalized_seed:
        return set()

    parsed_seed = urlparse(normalized_seed)
    path_stem = Path(parsed_seed.path).stem
    property_slug = _extract_property_slug(normalized_seed)

    signatures: set[str] = set()
    for raw in {
        property_slug or "",
        path_stem,
        _normalise_property_name(property_slug or ""),
        _normalise_property_name(path_stem),
    }:
        compact = _compact_text(raw)
        if len(compact) >= 6:
            signatures.add(compact)

    return signatures


def _matches_compact_signatures(text: str, signatures: set[str]) -> bool:
    """Return True when any compact signature appears in the compacted text."""
    if not signatures:
        return False
    haystack = _compact_text(text)
    return any(signature in haystack for signature in signatures)


async def _resolve_redirect_target(url: str) -> str:
    """Resolve the final redirect target, tolerating broken public certificates."""
    normalized = normalize_url(url)
    if not normalized:
        return url

    timeout = max(10.0, float(settings.request_timeout_seconds))
    for verify in (True, False):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                http2=True,
                timeout=timeout,
                verify=verify,
            ) as client:
                response = await client.get(normalized)

            resolved = normalize_url(str(response.url))
            if resolved:
                return resolved
        except Exception:
            continue

    return normalized


def _extract_same_domain_links_from_html(html: str, page_url: str, *, limit: int) -> list[str]:
    """Extract likely follow-up links from a rendered seed page on the same domain."""
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []

    base_domain = extract_domain(page_url)
    if not base_domain:
        return []

    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href:
            continue
        absolute = normalize_url(urljoin(page_url, href))
        if not absolute:
            continue
        if not is_same_domain(absolute, base_domain):
            continue
        if should_skip_url(absolute):
            continue
        candidates.append(absolute)

    prioritized = prioritize_discovery_links(candidates, limit=max(limit, 8))
    if normalize_url(page_url):
        prioritized.insert(0, normalize_url(page_url))
    return deduplicate_urls(prioritized)[: max(limit, 8)]


def _synthesise_property_follow_up_urls(seed_url: str) -> list[str]:
    """Add predictable hotel subpages that discovery commonly misses."""
    normalized_seed = normalize_url(seed_url)
    if not normalized_seed:
        return []

    parsed = urlparse(normalized_seed)
    seed_path = parsed.path.strip("/")
    if not seed_path or seed_path.endswith(".html"):
        return [normalized_seed]

    base_url = normalized_seed.rstrip("/") + "/"
    host = extract_domain(normalized_seed) or parsed.netloc.lower()

    candidate_paths = [
        "restaurant.html",
        "gallery.html",
        "contact/contact-us.html",
        "offers.html",
        "special-offers.html",
        "exclusive-offers.html",
        "packages.html",
    ]

    if host.endswith("orchidhotel.com"):
        candidate_paths.extend(
            [
                "banquet-halls.html",
                "hotel-exclusive-deals.html",
                "facilities/5-star-hotel-facilities.html",
                "weddings/weddings-with-orchid.html",
            ]
        )

    synthesized = [normalized_seed]
    for candidate_path in candidate_paths:
        candidate_url = normalize_url(urljoin(base_url, candidate_path))
        if candidate_url:
            synthesized.append(candidate_url)

    return deduplicate_urls(synthesized)


def _select_property_urls(
    seed_url: str,
    discovered_urls: list[str],
    aliases: set[str],
    *,
    limit: int,
) -> list[str]:
    """Keep only the URLs that plausibly belong to the selected property."""
    normalized_seed = normalize_url(seed_url)
    if not normalized_seed:
        return []

    seed_path = urlparse(normalized_seed).path.strip("/")
    seed_signatures = _seed_property_signatures(normalized_seed)
    relevant: list[str] = []

    if seed_path and not seed_path.endswith(".html"):
        relevant = [
            url for url in discovered_urls
            if normalize_url(url) == normalized_seed
            or urlparse(normalize_url(url) or "").path.strip("/").startswith(seed_path)
        ]

    if seed_signatures:
        signature_matches: list[str] = []
        for url in discovered_urls:
            normalized = normalize_url(url)
            if not normalized:
                continue
            if normalized == normalized_seed:
                signature_matches.append(normalized)
                continue

            parsed = urlparse(normalized)
            if _matches_compact_signatures(
                f"{parsed.netloc}{parsed.path}",
                seed_signatures,
            ):
                signature_matches.append(normalized)

        if signature_matches:
            relevant = _dedupe_strings(relevant + signature_matches)

    if not relevant:
        for url in discovered_urls:
            normalized = normalize_url(url)
            if not normalized:
                continue
            if normalized == normalized_seed:
                relevant.append(normalized)
                continue

            compact_url = _compact_text(urlparse(normalized).path)
            if any(alias in compact_url for alias in aliases):
                relevant.append(normalized)

    if not relevant:
        relevant = list(discovered_urls)

    ordered = prioritize_discovery_links(relevant)
    ordered = _rebalance_property_urls(
        normalized_seed,
        ordered,
        limit=limit,
    )

    if normalized_seed not in ordered:
        ordered.insert(0, normalized_seed)

    return ordered[:limit]


def _page_matches_property_aliases(page: dict, seed_url: str, aliases: set[str]) -> bool:
    """Check if a crawled page still looks relevant to the selected property."""
    normalized_seed = normalize_url(seed_url)
    normalized_page = normalize_url(page.get("url", ""))
    seed_signatures = _seed_property_signatures(seed_url)
    if normalized_page and normalized_page == normalized_seed:
        return True

    if normalized_page:
        parsed = urlparse(normalized_page)
        signature_haystack = " ".join(
            part for part in (
                f"{parsed.netloc}{parsed.path}",
                page.get("title", "") or "",
            ) if part
        )
        if _matches_compact_signatures(signature_haystack, seed_signatures):
            return True

    compact_haystack = _compact_text(
        " ".join(
            part for part in [
                page.get("url", "") or "",
                page.get("title", "") or "",
            ]
            if part
        )
    )
    return any(alias in compact_haystack for alias in aliases)


def _property_url_dedupe_key(url: str) -> str:
    """Build a stable dedupe key that collapses www/non-www duplicates."""
    normalized = normalize_url(url)
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    host = extract_domain(normalized) or parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def _categorize_property_url(url: str, seed_url: str) -> str:
    """Group property URLs so selection keeps operational coverage balanced."""
    normalized = normalize_url(url)
    normalized_seed = normalize_url(seed_url)
    if not normalized:
        return "other"
    if normalized == normalized_seed:
        return "overview"

    path = urlparse(normalized).path.lower().strip("/")
    if not path:
        return "overview"

    seed_path = urlparse(normalized_seed).path.lower().strip("/") if normalized_seed else ""
    if seed_path and path == seed_path:
        return "overview"

    tokens = [token for token in re.split(r"[/._-]+", path) if token]
    token_set = set(tokens)

    if token_set & {"restaurant", "restaurants", "dining", "menu", "menus", "bar", "bars", "cafe"}:
        return "dining"
    if token_set & {"spa", "wellness", "massage", "jacuzzi", "pool", "yoga", "fitness", "gym"}:
        return "spa"
    if token_set & {"offer", "offers", "package", "packages", "deal", "deals", "promotion", "promotions"}:
        return "offers"
    if token_set & {"experience", "experiences", "activity", "activities", "gallery"}:
        return "experience"
    if token_set & {"wedding", "weddings", "banquet", "banquets", "meeting", "meetings", "conference", "events", "event", "venue", "venues"}:
        return "events"
    if token_set & {"contact", "location", "directions", "faq"}:
        return "contact"
    if token_set & {"room", "rooms", "suite", "suites", "villa", "villas", "accommodation", "stay", "residency", "retreat"}:
        return "rooms"
    return "other"


def _rebalance_property_urls(seed_url: str, urls: list[str], *, limit: int) -> list[str]:
    """Deduplicate property URLs and keep category coverage balanced."""
    normalized_seed = normalize_url(seed_url)
    if not normalized_seed or limit <= 0:
        return []

    unique_urls: list[str] = []
    seen_keys: set[str] = set()
    for raw_url in [normalized_seed, *urls]:
        normalized = normalize_url(raw_url)
        if not normalized:
            continue
        dedupe_key = _property_url_dedupe_key(normalized)
        if not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        unique_urls.append(normalized)

    buckets: dict[str, list[str]] = defaultdict(list)
    for normalized in unique_urls:
        buckets[_categorize_property_url(normalized, normalized_seed)].append(normalized)

    ordered_categories = [
        "overview",
        "dining",
        "spa",
        "offers",
        "experience",
        "events",
        "contact",
        "rooms",
        "other",
    ]

    result: list[str] = []
    seen_urls: set[str] = set()

    def _take_next(category: str) -> bool:
        bucket = buckets.get(category, [])
        while bucket and bucket[0] in seen_urls:
            bucket.pop(0)
        if not bucket:
            return False
        candidate = bucket.pop(0)
        seen_urls.add(candidate)
        result.append(candidate)
        return True

    _take_next("overview")

    while len(result) < limit:
        progressed = False
        for category in ordered_categories:
            if len(result) >= limit:
                break
            if _take_next(category):
                progressed = True
        if not progressed:
            break

    return result


def _looks_like_error_page(page: dict) -> bool:
    """Identify crawled pages that are clearly error/404 responses."""
    haystack = " ".join(
        part.strip().lower()
        for part in (
            page.get("title", "") or "",
            page.get("text", "") or "",
        )
        if isinstance(part, str) and part.strip()
    )
    if not haystack:
        return False

    error_markers = (
        "404",
        "page not found",
        "requested page could not be found",
        "the page you are looking for",
        "oops! page not found",
        "error 404",
    )
    return any(marker in haystack for marker in error_markers)


def _extract_property_slug(url: str) -> str | None:
    """Extract the property slug from the URL's first path segment."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return None

    parts = path.split("/")
    slug = parts[0]

    if slug.endswith(".html") and len(parts) == 1:
        return None

    slug_lower = slug.lower().removesuffix(".html")
    if slug_lower in _NON_PROPERTY_SLUGS:
        return None

    return slug


def _extract_property_from_title(title: str, base_domain: str) -> str | None:
    """Try to extract a property name from the page title."""
    if not title:
        return None

    separators = [" | ", " - ", " -- ", " — ", " :: "]
    parts: list[str] = [title]
    for sep in separators:
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            break

    parts = _split_property_candidates_robust(title)
    if len(parts) <= 1:
        return None

    domain_words = set(base_domain.replace(".", " ").lower().split())
    candidates: list[str] = []
    for part in parts:
        part_words = set(part.lower().split())
        if part_words.issubset(domain_words):
            continue
        if part.lower() in ("home", "homepage", "welcome", "official site", "official website"):
            continue
        candidates.append(part)

    for candidate in candidates:
        if not _looks_generic_property_candidate(candidate):
            return candidate.strip()

    if candidates:
        return candidates[0].strip()

    return None


def _looks_like_external_property_page(candidate: str, page_url: str, base_domain: str) -> bool:
    """Return True for external landing pages that likely represent a property."""
    candidate_lower = (candidate or "").strip().lower()
    if not candidate_lower or candidate_lower in _NON_PROPERTY_SLUGS:
        return False

    page_domain = extract_domain(page_url)
    if not page_domain or page_domain == base_domain:
        return False

    if any(keyword in candidate_lower for keyword in _PROPERTY_HINT_KEYWORDS):
        return True

    path_stem = Path(urlparse(page_url).path).stem.lower()
    if path_stem and path_stem not in _NON_PROPERTY_SLUGS:
        return True

    return False


def _extract_location_aliases_from_title(title: str) -> set[str]:
    """Extract compact location hints from titles like 'Hotel in Mumbai Near Airport'."""
    if not title:
        return set()

    aliases: set[str] = set()
    patterns = (
        r"\b(?:hotel|resort|stay)\s+in\s+([a-z\s]+)",
        r"\bira(?:\s+by\s+orchid)?\s+([a-z\s]+)",
        r"\bnear\s+([a-z\s]+)",
    )
    stop_words = {
        "near", "for", "with", "and", "or", "the", "best", "book", "room",
        "rooms", "banquet", "halls", "stay", "hotel", "resort", "in",
        "at", "of", "to", "by",
    }

    title_lower = title.lower()
    for pattern in patterns:
        for match in re.findall(pattern, title_lower):
            words: list[str] = []
            for word in re.split(r"[^a-z]+", match):
                if not word or word in stop_words:
                    break
                words.append(word)
                if len(words) >= 2:
                    break
            if words:
                aliases.add(_compact_text(" ".join(words)))

    return aliases


def _extract_same_domain_single_html_property_seed(page: dict, base_domain: str) -> tuple[str, set[str]] | None:
    """Identify same-domain landing pages like '/mumbai.html' or '/hotel-in-panchgani' as property seeds."""
    page_url = page.get("url", "")
    if extract_domain(page_url) != base_domain:
        return None

    path = urlparse(page_url).path.strip("/")
    parts = path.split("/") if path else []
    if len(parts) != 1:
        return None

    raw_part = parts[0].lower()
    is_html_root = raw_part.endswith(".html")
    stem = Path(parts[0]).stem.lower() if is_html_root else raw_part
    if not stem or stem in _NON_PROPERTY_SLUGS:
        return None

    stem_tokens = [token for token in stem.split("-") if token]
    if is_html_root:
        if len(stem_tokens) != 1:
            return None
    elif not any(token in _PROPERTY_HINT_KEYWORDS for token in stem_tokens):
        return None

    title = page.get("title", "") or ""
    title_lower = title.lower()
    if not any(keyword in title_lower for keyword in _PROPERTY_HINT_KEYWORDS):
        return None

    if any(
        word in title_lower
        for word in (
            "offer", "legacy", "about", "contact", "privacy", "terms", "news", "blog",
            "banquet", "event", "membership", "loyalty", "reward", "program",
        )
    ):
        return None

    aliases = {_compact_text(stem), _compact_text(_normalise_property_name(stem))}
    aliases.update(_extract_location_aliases_from_title(title))
    aliases = {alias for alias in aliases if len(alias) >= 4}
    if not aliases:
        return None

    property_name = _derive_property_display_name(page, base_domain, fallback_slug=stem) or _normalise_property_name(stem)
    return property_name, aliases


def _match_same_domain_single_html_property(
    page: dict,
    base_domain: str,
    seed_aliases: dict[str, set[str]],
) -> str | None:
    """Match same-domain pages to a detected single-html property seed."""
    page_url = page.get("url", "")
    if extract_domain(page_url) != base_domain:
        return None

    path_stem = Path(urlparse(page_url).path).stem
    compact_haystack = _compact_text(f"{path_stem} {page.get('title', '')}")

    best_match: str | None = None
    best_alias_len = 0
    for property_name, aliases in seed_aliases.items():
        for alias in aliases:
            if alias and alias in compact_haystack and len(alias) > best_alias_len:
                best_match = property_name
                best_alias_len = len(alias)

    return best_match


def _should_include_general_group(base_url: str, general_pages: list[dict]) -> bool:
    """Return True when leftover pages are meaningful enough to show as 'General'."""
    if not general_pages:
        return False

    if len(general_pages) > 1:
        return True

    only_page_url = normalize_url(general_pages[0].get("url", ""))
    normalized_base = normalize_url(base_url)
    return bool(only_page_url and normalized_base and only_page_url != normalized_base)


def detect_properties(pages: list[dict], base_url: str) -> dict[str, list[dict]]:
    """Group crawled pages by hotel property using URL slug detection.

    Returns dict mapping property name -> list of page dicts.
    """
    base_domain = urlparse(base_url).hostname or ""
    if base_domain.startswith("www."):
        base_domain = base_domain[4:]

    # ── Phase 1: Group by URL slug ──
    slug_map: dict[str, list[dict]] = defaultdict(list)
    unassigned: list[dict] = []

    for page in pages:
        url = page.get("url", "")
        slug = _extract_property_slug(url)

        if slug:
            slug_map[slug].append(page)
        else:
            unassigned.append(page)

    # Only count slugs with 2+ pages as genuine properties
    property_slugs = {slug: pages for slug, pages in slug_map.items() if len(pages) >= 2}

    if len(property_slugs) >= 2:
        property_map: dict[str, list[dict]] = {}
        for slug, pages in property_slugs.items():
            name = _normalise_property_name(slug)
            if name:
                property_map[name] = pages

        general_pages = list(unassigned)
        for slug, pages in slug_map.items():
            if slug not in property_slugs:
                general_pages.extend(pages)
        if general_pages:
            property_map["General"] = general_pages

        logger.info(
            "Detected %d properties via URL slugs (+ %d general pages)",
            len(property_map) - (1 if "General" in property_map else 0),
            len(general_pages),
        )
        return property_map

    # ── Phase 2: Fallback to title-based detection ──
    property_map_title: dict[str, list[dict]] = defaultdict(list)
    unassigned_title: list[dict] = []

    all_pages = list(unassigned)
    for pages in slug_map.values():
        all_pages.extend(pages)

    for page in all_pages:
        title = page.get("title", "")
        prop_name = _extract_property_from_title(title, base_domain)
        if prop_name:
            property_map_title[prop_name].append(page)
        else:
            unassigned_title.append(page)

    multi_title = {k: v for k, v in property_map_title.items() if len(v) >= 2}
    if len(multi_title) >= 2:
        if unassigned_title:
            property_map_title["General"].extend(unassigned_title)
        logger.info(
            "Detected %d properties via titles",
            len(property_map_title),
        )
        return dict(property_map_title)

    # ── Phase 3: Single property ──
    logger.info("Single-property site detected, grouping all %d pages under 'Main'", len(all_pages))
    return {"Main": all_pages}


def _detect_properties_v2(pages: list[dict], base_url: str) -> dict[str, list[dict]]:
    """Improved property grouping with support for external landing pages."""
    base_domain = urlparse(base_url).hostname or ""
    if base_domain.startswith("www."):
        base_domain = base_domain[4:]

    slug_map: dict[str, list[dict]] = defaultdict(list)
    unassigned: list[dict] = []

    for page in pages:
        url = page.get("url", "")
        slug = _extract_property_slug(url)
        if slug:
            slug_map[slug].append(page)
        else:
            unassigned.append(page)

    property_map: dict[str, list[dict]] = {}
    general_pages: list[dict] = []
    external_singletons: dict[str, list[dict]] = defaultdict(list)

    for slug, grouped_pages in slug_map.items():
        if len(grouped_pages) < 2:
            continue
        name = _derive_property_display_name(grouped_pages[0], base_domain, fallback_slug=slug) or _normalise_property_name(slug)
        if name:
            property_map[name] = list(grouped_pages)

    leftover_pages = list(unassigned)
    for slug, grouped_pages in slug_map.items():
        if len(grouped_pages) >= 2:
            continue
        leftover_pages.extend(grouped_pages)

    same_domain_seed_aliases: dict[str, set[str]] = {}
    for page in leftover_pages:
        seed = _extract_same_domain_single_html_property_seed(page, base_domain)
        if not seed:
            continue
        property_name, aliases = seed
        same_domain_seed_aliases.setdefault(property_name, set()).update(aliases)

    for page in leftover_pages:
        page_url = page.get("url", "")
        page_title = page.get("title", "")
        page_stem = Path(urlparse(page_url).path).stem
        title_candidate = _extract_property_from_title(page_title, base_domain)
        page_display_name = _derive_property_display_name(page, base_domain, fallback_slug=page_stem)

        if title_candidate and _looks_like_external_property_page(title_candidate, page_url, base_domain):
            external_singletons[page_display_name or title_candidate].append(page)
            continue

        page_domain = extract_domain(page_url)
        if page_domain and page_domain != base_domain:
            path_stem = Path(urlparse(page_url).path).stem
            fallback_candidate = _normalise_property_name(path_stem or page_title or page_domain.split(".")[0])
            if _looks_like_external_property_page(fallback_candidate, page_url, base_domain):
                external_singletons[page_display_name or fallback_candidate].append(page)
                continue

        same_domain_match = _match_same_domain_single_html_property(
            page, base_domain, same_domain_seed_aliases
        )
        if same_domain_match:
            property_map.setdefault(same_domain_match, []).append(page)
            continue

        general_pages.append(page)

    for name, grouped_pages in external_singletons.items():
        property_map.setdefault(name, []).extend(grouped_pages)

    if len(property_map) >= 2:
        if _should_include_general_group(base_url, general_pages):
            property_map["General"] = general_pages

        logger.info(
            "Detected %d properties via URL slugs and external landing pages (+ %d general pages)",
            len(property_map) - (1 if "General" in property_map else 0),
            len(general_pages),
        )
        return property_map

    property_map_title: dict[str, list[dict]] = defaultdict(list)
    unassigned_title: list[dict] = []

    all_pages = list(general_pages)
    for grouped_pages in property_map.values():
        all_pages.extend(grouped_pages)
    if not all_pages:
        all_pages = list(unassigned)
        for grouped_pages in slug_map.values():
            all_pages.extend(grouped_pages)

    for page in all_pages:
        page_stem = Path(urlparse(page.get("url", "")).path).stem
        prop_name = _derive_property_display_name(page, base_domain, fallback_slug=page_stem) or _extract_property_from_title(page.get("title", ""), base_domain)
        if prop_name:
            property_map_title[prop_name].append(page)
        else:
            unassigned_title.append(page)

    multi_title = {name: grouped_pages for name, grouped_pages in property_map_title.items() if len(grouped_pages) >= 2}
    if len(multi_title) >= 2:
        if unassigned_title:
            property_map_title["General"].extend(unassigned_title)
        logger.info("Detected %d properties via titles", len(property_map_title))
        return dict(property_map_title)

    logger.info("Single-property site detected, grouping all %d pages under 'Main'", len(all_pages))
    return {"Main": all_pages}


detect_properties = _detect_properties_v2


async def expand_property_pages_for_kb(
    property_name: str,
    seed_pages: list[dict],
    *,
    max_depth: int = 2,
    max_pages: int = 24,
) -> list[dict]:
    """Discover and crawl additional property-specific pages for KB generation.

    This is mainly used for multi-property hub sites where the initial discovery
    only returns a property's landing page. Expansion keeps the crawl inside the
    selected property's own domain/path before KB structuring starts.
    """
    if not seed_pages:
        return []

    primary_seed = seed_pages[0]
    seed_url = primary_seed.get("url", "")
    if not seed_url:
        return list(seed_pages)

    aliases = _build_property_aliases(property_name, seed_pages)
    discovery_seed = await _resolve_redirect_target(seed_url)
    try:
        discovered_urls = await discover_urls(
            discovery_seed,
            max_depth=max_depth,
            max_pages=max(max_pages * 2, 16),
        )
    except Exception as exc:
        logger.warning("Property expansion discovery failed for %s: %s", property_name, exc)
        return list(seed_pages)

    inline_seed_urls = _extract_same_domain_links_from_html(
        primary_seed.get("html", ""),
        discovery_seed,
        limit=max_pages * 2,
    )
    synthesized_urls = _synthesise_property_follow_up_urls(discovery_seed)
    discovered_urls = deduplicate_urls(
        [
            *discovered_urls,
            *inline_seed_urls,
            *synthesized_urls,
        ]
    )

    selection_limit = max(max_pages + 12, 32)
    candidate_urls = _select_property_urls(
        seed_url,
        discovered_urls,
        aliases,
        limit=selection_limit,
    )
    if len(candidate_urls) <= 1:
        try:
            seed_page = await crawl_page(discovery_seed)
        except Exception as exc:
            logger.warning("Seed-page fallback crawl failed for %s: %s", property_name, exc)
            seed_page = {}

        if seed_page.get("html"):
            fallback_urls = _extract_same_domain_links_from_html(
                seed_page.get("html", ""),
                discovery_seed,
                limit=max_pages * 2,
            )
            fallback_urls = deduplicate_urls(
                [
                    *fallback_urls,
                    *_synthesise_property_follow_up_urls(discovery_seed),
                ]
            )
            candidate_urls = _select_property_urls(
                seed_url,
                fallback_urls,
                aliases,
                limit=selection_limit,
            )

    if not candidate_urls:
        return list(seed_pages)

    try:
        crawl_results = await crawl_pages_batch(candidate_urls)
    except Exception as exc:
        logger.warning("Property expansion crawl failed for %s: %s", property_name, exc)
        return list(seed_pages)

    expanded_pages = [
        result for result in crawl_results
        if not result.get("error") and result.get("html") and not _looks_like_error_page(result)
    ]
    if not expanded_pages:
        return list(seed_pages)

    filtered_pages = [
        page for page in expanded_pages
        if _page_matches_property_aliases(page, seed_url, aliases)
    ]

    if filtered_pages:
        return filtered_pages[:max_pages]
    return expanded_pages[:max_pages]


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _safe_update(
    callback: UpdateCallback,
    job_id: str,
    status: str,
    progress_pct: int,
    progress_msg: str,
    **kwargs: Any,
) -> None:
    """Call the update callback safely, logging any errors."""
    if callback is None:
        return
    try:
        await callback(
            job_id,
            status=status,
            progress_pct=progress_pct,
            progress_msg=progress_msg,
            **kwargs,
        )
    except Exception as exc:
        logger.warning("Update callback failed: %s", exc)


# ── PHASE 1: Discovery + Crawl + Property Detection ─────────────────────────


async def run_discovery_phase(
    job_id: str,
    url: str,
    specific_urls: list[str] | None = None,
    update_callback: UpdateCallback = None,
) -> dict:
    """Run Phase 1: Discovery -> Crawl -> Property Detection.

    Returns dict with:
      - properties: dict[name -> {page_count, sample_urls, sample_titles}]
      - crawl_data: the raw crawl results (kept in memory for Phase 2)
      - total_pages, failed_pages
    """
    logger.info("=== PHASE 1 START === job=%s url=%s", job_id, url)
    logger.info(
        "[PHASE 1][DISCOVERY] Starting | job=%s | url=%s | max_depth=%s | max_pages=%s",
        job_id,
        url,
        settings.max_depth,
        settings.max_pages_per_site,
    )

    try:
        # ── Step 1: DISCOVERY ────────────────────────────────────────────
        await _safe_update(
            update_callback, job_id,
            status="discovering",
            progress_pct=5,
            progress_msg="Discovering pages...",
        )

        async def _discovery_progress(
            stage: str,
            pages_found: int = 0,
            visited: int = 0,
            queue_size: int = 0,
            depth: int = 0,
        ) -> None:
            if stage == "complete":
                pct = 9
                msg = f"Discovery complete: found {pages_found} pages"
            elif stage == "bfs":
                pct = min(9, 5 + min(max(pages_found, 1) // 50, 4))
                msg = (
                    f"Discovery: found {pages_found} pages, "
                    f"visited {visited}, queue {queue_size}"
                )
            elif stage == "sitemaps":
                pct = min(8, 5 + min(max(pages_found, 1) // 50, 3))
                msg = f"Discovery: found {pages_found} sitemap pages"
            else:
                pct = 5
                msg = "Discovering pages..."

            await _safe_update(
                update_callback,
                job_id,
                status="discovering",
                progress_pct=pct,
                progress_msg=msg,
                pages_found=pages_found,
            )

        try:
            discovered_urls = await discover_urls(
                base_url=url,
                max_depth=settings.max_depth,
                max_pages=settings.max_pages_per_site,
                specific_urls=specific_urls or [],
                progress_callback=_discovery_progress,
            )
        except SiteBlockedError as blocked_exc:
            # Site is behind a bot-protection wall that none of our tools
            # can bypass.  Fail the job immediately with a clear message
            # rather than falling through to crawl 0 pages and crashing at
            # publish time with a cryptic "No enabled page content" error.
            logger.warning(
                "[PHASE 1][BLOCKED] job=%s | url=%s | reason=%s",
                job_id,
                url,
                blocked_exc.reason,
            )
            await _safe_update(
                update_callback,
                job_id,
                status="failed",
                progress_pct=10,
                progress_msg=(
                    f"Site is permanently blocked by anti-bot protection: {blocked_exc.reason}. "
                    "Cannot scrape without a residential proxy or manual data entry."
                ),
            )
            return {
                "properties": {},
                "crawl_data": {},
                "total_pages": 0,
                "failed_pages": 0,
                "error": f"Site blocked: {blocked_exc.reason}",
            }

        if not discovered_urls:
            # Legitimate empty site (no links, not blocked) — try the homepage.
            discovered_urls = [url]
            logger.warning("Discovery found no URLs, falling back to homepage only")

        total_pages = len(discovered_urls)
        logger.info(
            "[PHASE 1][DISCOVERY] Complete | job=%s | urls_found=%d",
            job_id,
            total_pages,
        )

        await _safe_update(
            update_callback, job_id,
            status="discovering",
            progress_pct=10,
            progress_msg=f"Found {total_pages} pages to crawl",
            pages_found=total_pages,
        )

        # ── Step 2: CRAWLING ─────────────────────────────────────────────
        logger.info(
            "[PHASE 1][CRAWL] Starting | job=%s | urls_to_crawl=%d | concurrency=%s | delay=%s",
            job_id,
            total_pages,
            settings.max_concurrent_crawls,
            settings.crawl_delay_seconds,
        )

        await _safe_update(
            update_callback, job_id,
            status="crawling",
            progress_pct=12,
            progress_msg="Starting page crawl...",
        )

        async def _crawl_progress(crawled: int, total: int) -> None:
            pct = 12 + int((crawled / max(total, 1)) * 38)
            await _safe_update(
                update_callback, job_id,
                status="crawling",
                progress_pct=min(pct, 50),
                progress_msg=f"Crawled {crawled}/{total} pages",
                pages_crawled=crawled,
            )

        crawl_results = await crawl_pages_batch(
            urls=discovered_urls,
            max_concurrent=settings.max_concurrent_crawls,
            delay=settings.crawl_delay_seconds,
            progress_callback=_crawl_progress,
        )

        successful_pages = [
            r for r in crawl_results
            if not r.get("error")
            and r.get("html")
            # Drop SPA 404 shells — React/Vue/Next serve status=200 with a
            # "Page Not Found" UI component.  These have near-zero text and
            # a title that contains "not found".
            and "not found" not in (r.get("title", "") or "").lower()
        ]
        failed_pages = [r for r in crawl_results if r.get("error") or not r.get("html")]
        failed_count = len(failed_pages)

        logger.info(
            "[PHASE 1][CRAWL] Complete | job=%s | succeeded=%d | failed=%d",
            job_id,
            len(successful_pages),
            failed_count,
        )

        await _safe_update(
            update_callback, job_id,
            status="crawling",
            progress_pct=50,
            progress_msg=f"Crawled {len(successful_pages)} pages ({failed_count} failed)",
            pages_crawled=len(successful_pages),
            pages_failed=failed_count,
        )

        if not successful_pages:
            return {
                "properties": {},
                "crawl_data": {},
                "total_pages": total_pages,
                "failed_pages": failed_count,
                "error": "All pages failed to crawl",
            }

        # ── Step 3: PROPERTY DETECTION ───────────────────────────────────
        logger.info(
            "[PHASE 1][PROPERTY DETECTION] Starting | job=%s | crawled_pages=%d",
            job_id,
            len(successful_pages),
        )

        property_groups = detect_properties(successful_pages, url)
        properties_found = len(property_groups)

        logger.info(
            "[PHASE 1][PROPERTY DETECTION] Complete | job=%s | properties_found=%d | property_names=%s",
            job_id,
            properties_found,
            list(property_groups.keys()),
        )

        # Build property summary for UI (without heavy HTML data)
        properties_summary = {}
        for prop_name, prop_pages in property_groups.items():
            properties_summary[prop_name] = {
                "page_count": len(prop_pages),
                "sample_urls": [p.get("url", "") for p in prop_pages[:5]],
                "sample_titles": [p.get("title", "") for p in prop_pages[:5] if p.get("title")],
            }

        await _safe_update(
            update_callback, job_id,
            status="properties_detected",
            progress_pct=55,
            progress_msg=f"Found {properties_found} properties. Select which ones to process.",
            properties_found=properties_found,
        )

        logger.info("=== PHASE 1 COMPLETE === %d properties detected", properties_found)

        return {
            "properties": properties_summary,
            "crawl_data": property_groups,  # full data kept for Phase 2
            "total_pages": total_pages,
            "failed_pages": failed_count,
        }

    except Exception as exc:
        error_msg = f"Phase 1 failed: {exc}"
        logger.error("%s (job_id=%s)", error_msg, job_id, exc_info=True)
        await _safe_update(
            update_callback, job_id,
            status="failed",
            progress_pct=0,
            progress_msg=error_msg,
        )
        return {
            "properties": {},
            "crawl_data": {},
            "total_pages": 0,
            "failed_pages": 0,
            "error": error_msg,
        }


# ── PHASE 2: Process Selected Properties ────────────────────────────────────


async def run_processing_phase(
    job_id: str,
    selected_properties: list[str],
    property_groups: dict[str, list[dict]],
    update_callback: UpdateCallback = None,
) -> dict:
    """Run Phase 2: Extract content + Download images for selected properties.

    Args:
        job_id: The job ID.
        selected_properties: List of property names the user selected.
        property_groups: Full crawl data from Phase 1 (name -> list of page dicts).
        update_callback: Progress callback.

    Returns:
        dict with: properties (list of property result dicts)
    """
    logger.info(
        "=== PHASE 2 START === job=%s, processing %d/%d properties",
        job_id, len(selected_properties), len(property_groups),
    )

    # Filter to only selected properties
    selected_groups = {
        name: pages for name, pages in property_groups.items()
        if name in selected_properties
    }

    if not selected_groups:
        logger.warning(
            "[PHASE 2] No valid properties selected | job=%s | selected=%s | available=%s",
            job_id,
            selected_properties,
            list(property_groups.keys()),
        )
        return {"properties": [], "error": "No valid properties selected"}

    total_props = len(selected_groups)
    total_extraction = sum(len(pages) for pages in selected_groups.values())
    logger.info(
        "[PHASE 2] Selected properties resolved | job=%s | selected_count=%d | selected=%s",
        job_id,
        total_props,
        list(selected_groups.keys()),
    )
    logger.info(
        "[PHASE 2][EXTRACTION] Starting | job=%s | properties=%d | total_pages=%d",
        job_id,
        total_props,
        total_extraction,
    )

    # ── Step 4: EXTRACTION ───────────────────────────────────────────
    await _safe_update(
        update_callback, job_id,
        status="extracting",
        progress_pct=58,
        progress_msg=f"Extracting content from {total_props} properties...",
    )

    properties_result: list[dict] = []
    extraction_done = 0

    for prop_name, prop_pages in selected_groups.items():
        logger.info(
            "[PHASE 2][EXTRACTION] Property start | job=%s | property=%s | pages=%d",
            job_id,
            prop_name,
            len(prop_pages),
        )
        extracted_content: list[dict] = []

        for page in prop_pages:
            html = page.get("html", "")
            page_url = page.get("url", "")

            try:
                content = extract_content(html, page_url)
                # When trafilatura/readability return thin content (< 200 chars)
                # from a React/Vue/Next SPA, fall back to the pre-extracted DOM
                # text captured in crawl_engine from the live Playwright page
                # before the browser was closed (body_inner_text fallback).
                pre_text = page.get("text", "")
                if len(content.get("main_text", "")) < 200 and len(pre_text) > len(content.get("main_text", "")):
                    logger.info(
                        "[PHASE 2][EXTRACTION] Thin HTML extraction (%d chars), "
                        "using pre-crawl DOM text (%d chars) for %s",
                        len(content.get("main_text", "")), len(pre_text), page_url,
                    )
                    content["main_text"] = pre_text
                content["source_url"] = page_url
                content["crawl_method"] = page.get("method", "unknown")
                extracted_content.append(content)
            except Exception as exc:
                logger.warning("Extraction failed for %s: %s", page_url, exc)
                extracted_content.append({
                    "source_url": page_url,
                    "error": str(exc),
                    "title": page.get("title", ""),
                    "main_text": page.get("text", ""),
                    "markdown": "",
                    "tables": [],
                    "images": [],
                    "contacts": {"phones": [], "emails": [], "address": ""},
                    "structured_data": [],
                    "meta": {},
                    "crawl_method": page.get("method", "unknown"),
                })

            extraction_done += 1
            pct = 58 + int((extraction_done / max(total_extraction, 1)) * 20)
            await _safe_update(
                update_callback, job_id,
                status="extracting",
                progress_pct=min(pct, 78),
                progress_msg=f"Extracting: {extraction_done}/{total_extraction} pages ({prop_name})",
            )

        logger.info(
            "[PHASE 2][EXTRACTION] Property complete | job=%s | property=%s | extracted_pages=%d",
            job_id,
            prop_name,
            len(extracted_content),
        )
        properties_result.append({
            "name": prop_name,
            "pages": [{"url": p.get("url", ""), "title": p.get("title", "")} for p in prop_pages],
            "extracted_content": extracted_content,
        })

    await _safe_update(
        update_callback, job_id,
        status="extracting",
        progress_pct=80,
        progress_msg="Content extraction complete",
    )
    logger.info(
        "[PHASE 2][EXTRACTION] Complete | job=%s | processed_properties=%d | processed_pages=%d",
        job_id,
        len(properties_result),
        total_extraction,
    )

    # ── Step 5: IMAGE DOWNLOAD ────────────────────────────────────
    logger.info(
        "[PHASE 2][IMAGES] Starting | job=%s | properties=%d",
        job_id,
        total_props,
    )
    await _safe_update(
        update_callback, job_id,
        status="downloading_images",
        progress_pct=82,
        progress_msg="Downloading property images...",
    )

    images_done = 0
    for prop in properties_result:
        prop_name = prop["name"]
        safe_name = re.sub(r"[^\w\s-]", "", prop_name).strip().replace(" ", "_").lower()
        prop_output_dir = Path(settings.output_dir) / job_id / safe_name

        try:
            logger.info(
                "[PHASE 2][IMAGES] Property start | job=%s | property=%s | output_dir=%s",
                job_id,
                prop_name,
                prop_output_dir,
            )
            image_result = await download_property_images(
                property_name=prop_name,
                extracted_pages=prop.get("extracted_content", []),
                output_dir=prop_output_dir,
                max_concurrent=10,
            )
            prop["images"] = image_result
            logger.info(
                "[PHASE 2][IMAGES] Property complete | job=%s | property=%s | found=%s | downloaded=%s | failed=%s",
                job_id,
                prop_name,
                image_result.get("total_found"),
                image_result.get("downloaded"),
                image_result.get("failed"),
            )
        except Exception as exc:
            logger.warning("Image download failed for %s: %s", prop_name, exc)
            prop["images"] = {
                "total_found": 0, "downloaded": 0, "skipped": 0, "failed": 0,
                "excel_path": None, "images_dir": None, "error": str(exc),
            }

        images_done += 1
        pct = 82 + int((images_done / max(total_props, 1)) * 13)
        await _safe_update(
            update_callback, job_id,
            status="downloading_images",
            progress_pct=min(pct, 95),
            progress_msg=f"Images: {images_done}/{total_props} properties done ({prop_name})",
        )

    await _safe_update(
        update_callback, job_id,
        status="downloading_images",
        progress_pct=95,
        progress_msg="Image download complete",
    )
    logger.info(
        "[PHASE 2][IMAGES] Complete | job=%s | processed_properties=%d",
        job_id,
        len(properties_result),
    )

    logger.info(
        "=== PHASE 2 COMPLETE === %d properties processed",
        len(properties_result),
    )

    return {"properties": properties_result}


# ── Legacy: Full pipeline in one shot (for backward compatibility) ───────────


async def run_scrape_job(
    job_id: str,
    url: str,
    update_callback: UpdateCallback = None,
) -> dict:
    """Run the full scrape pipeline (both phases) without pausing.

    Kept for backward compatibility with test scripts.
    """
    phase1 = await run_discovery_phase(job_id, url, update_callback)

    if phase1.get("error") or not phase1.get("crawl_data"):
        return {
            "properties": [],
            "total_pages": phase1.get("total_pages", 0),
            "failed_pages": phase1.get("failed_pages", 0),
            "error": phase1.get("error", "No properties found"),
        }

    # Process ALL properties (no selection)
    all_names = list(phase1["crawl_data"].keys())

    phase2 = await run_processing_phase(
        job_id=job_id,
        selected_properties=all_names,
        property_groups=phase1["crawl_data"],
        update_callback=update_callback,
    )

    return {
        "properties": phase2.get("properties", []),
        "total_pages": phase1.get("total_pages", 0),
        "failed_pages": phase1.get("failed_pages", 0),
    }
