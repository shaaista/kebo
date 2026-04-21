"""Filter and normalize URLs to keep only same-domain, useful hotel pages."""

import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse

logger = logging.getLogger(__name__)

# File extensions to skip
SKIP_EXTENSIONS = frozenset({
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff",
    # Documents / media
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar",
    # Video / audio
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv", ".wav", ".ogg", ".webm",
    # Code / style assets
    ".css", ".js", ".json", ".xml", ".rss", ".atom",
    # Fonts
    ".woff", ".woff2", ".ttf", ".eot",
})

# URL path segments that indicate non-content pages
SKIP_PATH_PATTERNS = re.compile(
    r"(/wp-admin|/wp-login|/admin|/login|/logout|/signin|/signup|/register"
    r"|/cart|/checkout|/account|/my-account|/wp-json|/feed|/trackback"
    r"|/xmlrpc|/wp-content/uploads|/wp-includes"
    r"|/cdn-cgi|/\.well-known"
    r"|/print/|/share/|/email-friend|/add-to-cart)",
    re.IGNORECASE,
)

# Social media domains to skip
SOCIAL_DOMAINS = frozenset({
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "pinterest.com", "tiktok.com", "snapchat.com",
    "wa.me", "t.me", "reddit.com", "tumblr.com",
})

# Schemes that are not crawlable
NON_HTTP_SCHEMES = frozenset({"tel", "mailto", "javascript", "data", "ftp"})

# Path segments that are almost always noise during discovery for hotel/property sites
DISCOVERY_SKIP_SEGMENTS = frozenset({
    "about", "about-us", "aboutus", "awards", "board", "board-of-directors",
    "blog", "blogs", "book-closure-intimation", "booking", "careers", "committee",
    "committees", "compliance", "complaince-certificate", "confirmation-certificate",
    "contact", "contact-us", "corporate", "corporate-governance-reports", "credit-ratings",
    "csr", "directorships", "disclosures-under-regulation-46-of-the-lodr", "employee-login",
    "events", "faq", "financial-results-security-cover-certificate", "governance",
    "grievance", "index", "index.html", "insider-trading", "interest-payment-and-redemption",
    "investor", "investors", "investor-contact", "investor-grievance", "investor-notices",
    "investor-presentations", "investor-relations", "login", "media", "meeting",
    "milestones", "news", "newsroom", "other-disclosures-debt", "other-policies",
    "others", "partner-with-us", "policies", "policy", "postal-ballot", "press",
    "privacy", "privacy-policy", "quarterly-results", "record-date-intimation",
    "register", "regulation-30", "related-party-policy", "related-party-transactions",
    "report", "reports", "reservations", "rewards", "sast-regulations", "search",
    "search-result", "search-result.php", "secretarial", "shareholding-pattern",
    "statement-of-deviation-and-variation", "structural-digital-database", "terms",
    "terms-and-conditions", "trading-window-closure", "unclaimed-dividend",
    "unclaimed-shares-dividend", "wedding", "weddings",
})

DISCOVERY_SKIP_KEYWORDS = frozenset({
    "analyst", "annual", "audit", "award", "board", "capital", "career",
    "certificate", "closure", "committee", "compliance", "corporate", "cover",
    "credit", "csr", "director", "dividend", "governance", "grievance",
    "insider", "interest-payment", "investor", "media", "merger", "milestone",
    "newspaper", "notice", "payment", "policy", "postal", "presentation",
    "deviation", "digital-database", "privacy", "publication", "quarterly",
    "reconciliation", "redemption", "rating", "record-date", "regulation",
    "related-party", "report", "search", "secretarial", "security-cover",
    "share", "shareholding", "subsidiary", "terms", "trading-window",
    "variation",
})

DISCOVERY_HIGH_VALUE_KEYWORDS = frozenset({
    "accommodation", "apartment", "apartments", "destination", "destinations",
    "guesthouse", "guests", "hotel", "hotels", "inn", "property", "properties",
    "residence", "residences", "residency", "residencies", "resort", "resorts",
    "room", "rooms", "stay", "stays", "suite", "suites", "villa", "villas",
})


def _path_segments(path: str) -> list[str]:
    """Split a URL path into lowercase slash-separated segments."""
    return [segment.lower() for segment in path.strip("/").split("/") if segment]


def _path_tokens(path: str) -> list[str]:
    """Split a path into lowercase word-like tokens."""
    cleaned = path.lower().strip("/")
    if not cleaned:
        return []
    return [token for token in re.split(r"[/._-]+", cleaned) if token]


def _contains_discovery_noise(path: str) -> bool:
    """Return True when the path clearly looks like a low-value discovery page."""
    path_lower = path.lower()
    segments = _path_segments(path_lower)
    tokens = _path_tokens(path_lower)

    if any(segment in DISCOVERY_SKIP_SEGMENTS for segment in segments):
        return True
    if any(token in DISCOVERY_SKIP_SEGMENTS for token in tokens):
        return True
    if any(keyword in path_lower for keyword in DISCOVERY_SKIP_KEYWORDS):
        return True
    return False


def extract_domain(url: str) -> str:
    """Get base domain from URL (e.g. 'www.hotel.com' -> 'hotel.com')."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # Strip 'www.' prefix for consistent comparison
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname.lower()
    except Exception:
        logger.warning("Failed to extract domain from: %s", url)
        return ""


def normalize_url(url: str) -> str:
    """Strip fragments, trailing slashes, normalize scheme/host case.

    Returns a canonical form of the URL suitable for deduplication.
    """
    try:
        parsed = urlparse(url.strip())

        # Skip non-HTTP schemes entirely
        scheme = (parsed.scheme or "https").lower()
        if scheme in NON_HTTP_SCHEMES:
            return ""

        hostname = (parsed.hostname or "").lower()
        port = parsed.port

        # Rebuild path: collapse double slashes, strip trailing slash
        path = parsed.path or "/"
        path = re.sub(r"/+", "/", path)
        path = re.sub(r"(?:%20)+$", "", path, flags=re.IGNORECASE)
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        # Keep query but drop fragment
        query = parsed.query
        fragment = ""  # always strip

        # Reconstruct netloc
        netloc = hostname
        if port and port not in (80, 443):
            netloc = f"{hostname}:{port}"

        normalized = urlunparse((scheme, netloc, path, parsed.params, query, fragment))
        return normalized
    except Exception:
        logger.warning("Failed to normalize URL: %s", url)
        return url.strip()


def is_same_domain(url: str, base_domain: str) -> bool:
    """Check if *url* belongs to the same domain as *base_domain*.

    Exact hostname match after stripping the ``www.`` prefix from both sides.
    This intentionally does NOT follow arbitrary sub-domains so that a base URL
    of ``mumbai.iconiqa.com`` stays on that host and does not bleed into sibling
    sub-domains like ``blog.mumbai.iconiqa.com``.
    """
    url_domain = extract_domain(url)
    base = base_domain.lower().removeprefix("www.")

    if not url_domain or not base:
        return False

    return url_domain == base


def should_skip_url(url: str) -> bool:
    """Return True if the URL should NOT be crawled.

    Skips images, PDFs, videos, CSS, JS, social media links,
    tel:/mailto:/javascript: schemes, fragment-only anchors,
    login/admin pages, query-heavy search pages, and clearly
    non-property/corporate pages that cause discovery fan-out.
    """
    stripped = url.strip()

    # Empty or fragment-only
    if not stripped or stripped.startswith("#"):
        return True

    # Non-HTTP schemes
    try:
        parsed = urlparse(stripped)
    except Exception:
        return True

    if parsed.scheme.lower() in NON_HTTP_SCHEMES:
        return True

    # Social media domains
    hostname = (parsed.hostname or "").lower()
    bare_host = hostname.removeprefix("www.")
    if bare_host in SOCIAL_DOMAINS:
        return True

    # Query-string URLs tend to be search/filter/tracking pages during discovery.
    if parsed.query:
        return True

    # File extension check
    path_lower = parsed.path.lower()
    for ext in SKIP_EXTENSIONS:
        if path_lower.endswith(ext):
            return True

    # Path pattern check (admin, login, cart, etc.)
    if SKIP_PATH_PATTERNS.search(parsed.path):
        return True

    # Hotel corp sites often expose investor/blog/contact/careers pages that
    # explode BFS without improving property detection.
    if _contains_discovery_noise(parsed.path):
        return True

    return False


def score_url_for_discovery(url: str) -> int:
    """Assign a relevance score used to prioritize discovery fan-out.

    Higher is better. Negative scores indicate low-value URLs that should be
    deprioritized or dropped when better options exist.
    """
    normalized = normalize_url(url)
    if not normalized:
        return -1000

    parsed = urlparse(normalized)
    path = parsed.path.lower()
    segments = _path_segments(path)
    tokens = _path_tokens(path)

    score = 0

    if not segments or segments == ["index.html"] or segments == ["index"]:
        score += 35
    elif len(segments) == 1:
        score += 20
    elif len(segments) == 2:
        score += 30
    elif len(segments) == 3:
        score += 35
    else:
        score += 20
        score -= min((len(segments) - 3) * 5, 20)

    if parsed.query:
        score -= 120

    for token in tokens:
        if token in DISCOVERY_HIGH_VALUE_KEYWORDS:
            score += 45
        elif any(keyword in token for keyword in DISCOVERY_HIGH_VALUE_KEYWORDS):
            score += 25

    # Reward property-detail pages that sit under hotel/property listing paths.
    if any(segment in DISCOVERY_HIGH_VALUE_KEYWORDS for segment in segments[:-1]):
        last_segment = segments[-1] if segments else ""
        if last_segment and last_segment not in DISCOVERY_HIGH_VALUE_KEYWORDS:
            score += 35

    # Brand and destination index pages are useful seeds but lower value than
    # actual property detail pages when max_pages is constrained.
    if "brand" in segments:
        score -= 40
    if segments and segments[-1] in {"destination", "destinations"}:
        score -= 35

    if _contains_discovery_noise(path):
        score -= 160

    return score


def prioritize_discovery_links(urls: list[str], limit: int | None = None) -> list[str]:
    """Deduplicate and rank discovery links, keeping only positive-signal URLs."""
    ranked: list[tuple[int, int, int, str]] = []
    seen: set[str] = set()

    for raw_url in urls:
        normalized = normalize_url(raw_url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        if should_skip_url(normalized):
            continue

        score = score_url_for_discovery(normalized)
        if score < 0:
            continue

        depth = len(_path_segments(urlparse(normalized).path))
        ranked.append((score, depth, len(normalized), normalized))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    ordered = [url for _, _, _, url in ranked]
    if limit is not None:
        return ordered[:limit]
    return ordered


def deduplicate_urls(urls: list[str]) -> list[str]:
    """Remove duplicates after normalization, preserving first-seen order."""
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        normalized = normalize_url(url)
        if not normalized:
            continue
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique
