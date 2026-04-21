"""Discover all crawlable pages from a hotel website URL.

Uses robots.txt, sitemap.xml, and BFS link crawling to build a
comprehensive list of pages to scrape.

Fetch strategy:
  - PRIMARY: Scrapling Fetcher (synchronous, wrapped in run_in_executor)
  - FALLBACK: httpx AsyncClient (original behaviour)
"""

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config.settings import settings
from scraper.protection_handler import detect_protection_interstitial
from scraper.retry_policy import RetryableHttpError, run_with_retry
from scraper.url_filter import (
    deduplicate_urls,
    extract_domain,
    is_same_domain,
    normalize_url,
    prioritize_discovery_links,
    should_skip_url,
)

# ── Scrapling import (graceful degradation) ──────────────────────────────────
# Each fetcher is imported separately so a missing optional dep (e.g. msgspec
# for DynamicFetcher, patchright for StealthyFetcher) does not prevent the
# basic Fetcher from being available.

_SCRAPLING_AVAILABLE = False

try:
    from scrapling import Fetcher
    _SCRAPLING_AVAILABLE = True
except ImportError:
    Fetcher = None  # type: ignore[misc,assignment]

try:
    from scrapling import DynamicFetcher as _ScraplingDynamicFetcher
except ImportError:
    _ScraplingDynamicFetcher = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


class SiteBlockedError(Exception):
    """Raised when a site is permanently blocked by bot-protection.

    Caught by the orchestrator to fail the job immediately with a
    human-readable reason rather than silently crawling 0 pages.
    """

    def __init__(self, reason: str, url: str) -> None:
        super().__init__(f"Site blocked at {url}: {reason}")
        self.reason = reason
        self.url = url


_EXTERNAL_PROPERTY_HINTS = {
    "hotel",
    "resort",
    "spa",
    "villa",
    "stay",
    "suite",
    "suites",
    "property",
    "properties",
    "room",
    "rooms",
    "orchid",
    "ira",
}

_EXTERNAL_PROPERTY_EXCLUDES = {
    "investor",
    "career",
    "careers",
    "privacy",
    "terms",
    "policy",
    "contact",
    "linkedin",
    "instagram",
    "facebook",
    "twitter",
    "youtube",
}

if _SCRAPLING_AVAILABLE:
    logger.info("[DISCOVERY] Scrapling is available — will be used as PRIMARY fetcher")
else:
    logger.warning(
        "[DISCOVERY] Scrapling is NOT installed — falling back to httpx for all requests. "
        "Install with: pip install scrapling"
    )

# Default timeout for discovery HTTP requests
_TIMEOUT = httpx.Timeout(timeout=settings.request_timeout_seconds)

# Common User-Agent for discovery phase (non-stealth; we identify politely)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DiscoveryProgressCallback = Callable[..., Awaitable[None]] | None


# ── JS-Rendering Detection (BFS) ─────────────────────────────────────────────
# Lightweight markers that indicate the server HTML is a SPA shell.
# Only used inside BFS to decide whether to try DynamicFetcher for links.

# Markers that unconditionally mean "use Playwright" — these frameworks
# always do client-side routing so nav links won't be in static HTML even
# when the server renders substantial SSR content.
_JS_MARKERS_ALWAYS = [
    "__next_data__",     # Next.js (SSR or SSG — always client-side router)
    '"__nuxt__"',        # Nuxt.js
    "/_next/static/",    # Next.js static chunk path
    "nuxt-link",         # Nuxt router-link
]

# Markers that only trigger Playwright when body text is thin (<400 chars)
# — these can appear on mostly-static sites too.
_JS_MARKERS_THIN_ONLY = [
    'id="__next"',
    'id="app"',
    'id="root"',
    "react-root",
    "ng-app",
    "data-reactroot",
    "vue-app",
    "__nuxt__",
]


def _is_js_rendered_page(html: str) -> bool:
    """Return True when the page uses a JS framework that needs Playwright
    for reliable link extraction.

    Two tiers:
    - ALWAYS markers: Next.js / Nuxt always do client-side routing regardless
      of how much SSR content the server sends.  Playwright is always needed.
    - THIN_ONLY markers: generic SPA shells — only use Playwright when body
      text is thin (<400 chars), avoiding false positives on hybrid static sites.
    """
    if not html or len(html.strip()) < 500:
        return True
    try:
        html_lower = html.lower()

        # Tier 1: unconditional — these frameworks always need Playwright
        for marker in _JS_MARKERS_ALWAYS:
            if marker in html_lower:
                return True

        soup = BeautifulSoup(html, "lxml")
        body = soup.find("body")
        body_text = body.get_text(strip=True) if body else ""
        if len(body_text) < 200:
            return True

        # Tier 2: only when body is thin
        for marker in _JS_MARKERS_THIN_ONLY:
            if marker in html_lower and len(body_text) < 400:
                return True
    except Exception:
        pass
    return False


async def _fetch_and_extract_links_dynamic(url: str, page_url: str) -> list[str]:
    """Re-fetch *url* with Playwright (networkidle) and return all links.

    This is the JS link-extraction fallback for BFS.  Called when static
    HTML yields 0 links and the page looks like a client-side-rendered SPA.

    Uses direct Playwright with ``wait_until="networkidle"`` and an extra
    hydration pause instead of Scrapling's DynamicFetcher — the Scrapling
    ``wait_selector`` parameter is deprecated in v0.3+ and has no effect,
    so it cannot reliably wait for React/Vue/Next to finish mounting.
    """
    logger.info("    [BFS-JS] 0 links in static HTML — launching Playwright (networkidle) for %s", url)
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                )
                bfs_page = await context.new_page()
                try:
                    from playwright_stealth import stealth_async
                    await stealth_async(bfs_page)
                except ImportError:
                    pass
                # "networkidle" times out on sites with background analytics/polling.
                # "load" fires once the page + subresources load; the 4s pause
                # then lets React/Vue/Next finish mounting navigation components.
                await bfs_page.goto(url, wait_until="load", timeout=30000)
                await bfs_page.wait_for_timeout(4000)
                rendered_html = await bfs_page.content()
            finally:
                await browser.close()

        if not rendered_html:
            logger.warning("    [BFS-JS] Playwright returned empty HTML for %s", url)
            return []

        links = _extract_links_scrapling(rendered_html, page_url)
        if not links:
            links = _extract_links_bs4(rendered_html, page_url)

        logger.info(
            "    [BFS-JS] Playwright extracted %d links from %s (html_len=%d)",
            len(links),
            url,
            len(rendered_html),
        )
        return links

    except Exception as exc:
        logger.warning("    [BFS-JS] Playwright link fallback failed for %s: %s", url, exc)
        return []


def _max_links_for_depth(depth: int) -> int:
    """Cap queued links per page so one noisy hub page cannot dominate discovery."""
    if depth <= 0:
        return 80
    if depth == 1:
        return 25
    return 12


def _discovery_collection_limit(max_pages: int) -> int:
    """Allow discovery to over-collect before final ranking trims the result set."""
    if max_pages <= 0:
        return 0
    return min(max(max_pages * 10, max_pages + 500), 5000)


async def _report_discovery_progress(
    callback: DiscoveryProgressCallback,
    **payload: Any,
) -> None:
    """Emit discovery progress safely without interrupting crawling."""
    if callback is None:
        return
    try:
        await callback(**payload)
    except Exception as exc:
        logger.debug("Discovery progress callback failed: %s", exc)


# ── Fetch Helper ─────────────────────────────────────────────────────────────


async def _fetch_page_once(
    url: str,
    client: httpx.AsyncClient,
    *,
    label: str = "page",
) -> tuple[str, int]:
    """Fetch a URL, returning ``(html_text, status_code)``.

    Strategy:
      1. Try Scrapling ``Fetcher.get`` (sync, wrapped in ``run_in_executor``).
      2. On any failure, fall back to *httpx*.
      3. Return ``("", 0)`` when both methods fail.

    *label* is used for clearer log messages (e.g. ``"robots.txt"``,
    ``"sitemap"``, ``"BFS"``).
    """
    html_text: str = ""
    status_code: int = 0
    prefer_plain_http = label in {"robots.txt", "sitemap"}

    # ── Attempt 1: Scrapling ─────────────────────────────────────────────
    if _SCRAPLING_AVAILABLE and not prefer_plain_http:
        try:
            loop = asyncio.get_running_loop()

            def _scrapling_get() -> tuple[str, int]:
                fetcher = Fetcher()
                page = fetcher.get(url, stealthy_headers=True)
                html = str(page.html_content) if hasattr(page, "html_content") else ""
                return html, (page.status or 0)

            t0 = time.perf_counter()
            html_text, status_code = await loop.run_in_executor(None, _scrapling_get)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if status_code and html_text:
                blocked_reason = detect_protection_interstitial(html_text, status_code)
                if blocked_reason:
                    logger.warning(
                        "    [FETCH] Scrapling got bot-protection page for %s [%s]: %s - trying httpx",
                        url,
                        label,
                        blocked_reason,
                    )
                    html_text = ""
                    status_code = 0
                    raise RuntimeError(f"{blocked_reason} detected")
                if status_code in {429, 500, 502, 503, 504}:
                    raise RetryableHttpError(
                        status_code,
                        f"Scrapling returned retryable status {status_code} for {url}",
                    )
                logger.info(
                    "    [FETCH] Scrapling OK for %s [%s] — status=%d, len=%d, %.0fms",
                    url,
                    label,
                    status_code,
                    len(html_text),
                    elapsed_ms,
                )
                return html_text, status_code

            # Scrapling returned empty or bad status — fall through to httpx
            logger.warning(
                "    [FETCH] Scrapling returned status=%d / empty body for %s [%s] — trying httpx",
                status_code,
                url,
                label,
            )
        except Exception as exc:
            logger.warning(
                "    [FETCH] Scrapling FAILED for %s [%s]: %s — trying httpx",
                url,
                label,
                exc,
            )
    elif prefer_plain_http:
        logger.debug(
            "    [FETCH] Skipping Scrapling for %s [%s] to preserve plain-text/XML fidelity",
            url,
            label,
        )

    # ── Attempt 2: httpx fallback ────────────────────────────────────────
    try:
        t0 = time.perf_counter()
        resp = await client.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        html_text = resp.text
        status_code = resp.status_code

        blocked_reason = detect_protection_interstitial(html_text, status_code)
        if blocked_reason:
            logger.warning(
                "    [FETCH] httpx got bot-protection page for %s [%s]: %s - treating as unavailable",
                url,
                label,
                blocked_reason,
            )
            return "", status_code

        if status_code in {429, 500, 502, 503, 504}:
            raise RetryableHttpError(
                status_code,
                f"httpx returned retryable status {status_code} for {url}",
            )

        logger.info(
            "    [FETCH] httpx OK for %s [%s] — status=%d, len=%d, %.0fms",
            url,
            label,
            status_code,
            len(html_text),
            elapsed_ms,
        )
        return html_text, status_code
    except Exception as exc:
        logger.error(
            "    [FETCH] httpx ALSO FAILED for %s [%s]: %s — giving up",
            url,
            label,
            exc,
        )

    return "", 0


async def _fetch_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    label: str = "page",
) -> tuple[str, int]:
    """Fetch a page with host-specific retry and backoff handling."""

    async def _operation() -> tuple[str, int]:
        return await _fetch_page_once(url, client, label=label)

    try:
        return await run_with_retry(url, _operation, label=f"discovery:{label}")
    except Exception as exc:
        logger.error("    [FETCH] Exhausted retries for %s [%s]: %s", url, label, exc)
        return "", 0


def _extract_links_scrapling(html: str, page_url: str) -> list[str]:
    """Extract all ``href`` values from anchor tags using Scrapling.

    Returns a list of *absolute* URLs.  Falls back to an empty list on
    any error (caller should then try BeautifulSoup).
    """
    if not _SCRAPLING_AVAILABLE:
        return []

    try:
        # Build a Selector from raw HTML so we can use CSS selectors
        # Scrapling's Fetcher returns a parsed response from .get(); we re-parse
        # the already-fetched HTML to avoid a second network call.
        from scrapling import Selector

        page = Selector(html, url=page_url)
        anchors = page.css("a[href]")
        links: list[str] = []
        for elem in anchors:
            href = elem.attrib.get("href", "").strip()
            if href:
                links.append(urljoin(page_url, href))
        logger.debug(
            "    [LINKS] Scrapling CSS extracted %d raw links from %s",
            len(links),
            page_url,
        )
        return links
    except Exception as exc:
        logger.debug(
            "    [LINKS] Scrapling link extraction failed for %s: %s — will use BS4",
            page_url,
            exc,
        )
        return []


def _extract_links_bs4(html: str, page_url: str) -> list[str]:
    """Extract all ``href`` values from anchor tags using BeautifulSoup."""
    try:
        soup = BeautifulSoup(html, "lxml")
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if href:
                links.append(urljoin(page_url, href))
        logger.debug(
            "    [LINKS] BS4 extracted %d raw links from %s",
            len(links),
            page_url,
        )
        return links
    except Exception as exc:
        logger.warning(
            "    [LINKS] BS4 link extraction failed for %s: %s",
            page_url,
            exc,
        )
        return []


def _extract_external_property_urls(
    html: str,
    page_url: str,
    base_domain: str,
    *,
    limit: int,
) -> list[str]:
    """Extract likely property links on external domains from the homepage."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:
        logger.debug("Failed to parse homepage for external property links: %s", exc)
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        absolute = normalize_url(urljoin(page_url, anchor["href"].strip()))
        if not absolute or absolute in seen:
            continue
        if is_same_domain(absolute, base_domain):
            continue
        if should_skip_url(absolute):
            continue

        anchor_text = " ".join(
            part
            for part in (
                anchor.get_text(" ", strip=True),
                anchor.get("title", ""),
                anchor.get("aria-label", ""),
                absolute,
            )
            if part
        ).lower()

        candidate_host = extract_domain(absolute)
        candidate_path = urlparse(absolute).path or "/"
        if candidate_path in {"", "/"} and candidate_host in {"irahotels.com", "orchidhotel.com"}:
            continue

        if not any(token in anchor_text for token in _EXTERNAL_PROPERTY_HINTS):
            continue
        if any(token in anchor_text for token in _EXTERNAL_PROPERTY_EXCLUDES):
            continue

        seen.add(absolute)
        candidates.append(absolute)

    prioritized = prioritize_discovery_links(candidates, limit=limit)
    logger.info(
        "--- [DISCOVERY] External property seed extraction found %d candidate URLs",
        len(prioritized),
    )
    return prioritized


# ── Robots.txt Parsing ──────────────────────────────────────────────────────


async def _fetch_robots_txt(
    base_url: str, client: httpx.AsyncClient
) -> tuple[list[str], list[str]]:
    """Fetch and parse robots.txt, returning ``(sitemap_urls, disallowed_paths)``."""
    robots_url = urljoin(base_url, "/robots.txt")
    sitemap_urls: list[str] = []
    disallowed: list[str] = []

    logger.info(">>> [DISCOVERY] Fetching robots.txt: %s", robots_url)
    t0 = time.perf_counter()

    try:
        html_text, status_code = await _fetch_page(robots_url, client, label="robots.txt")

        if status_code != 200 or not html_text:
            logger.info(
                "<<< [DISCOVERY] No robots.txt found at %s (status=%d) — %.2fs",
                robots_url,
                status_code,
                time.perf_counter() - t0,
            )
            return sitemap_urls, disallowed

        current_agent = None
        for line in html_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Sitemap directives (agent-independent)
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                if sm_url:
                    sitemap_urls.append(sm_url)
                continue

            if line.lower().startswith("user-agent:"):
                current_agent = line.split(":", 1)[1].strip()
                continue

            # Only respect * (all bots) disallow rules
            if current_agent == "*" and line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path:
                    disallowed.append(path)

        elapsed = time.perf_counter() - t0
        logger.info(
            "<<< [DISCOVERY] robots.txt: %d sitemaps, %d disallowed paths — %.2fs",
            len(sitemap_urls),
            len(disallowed),
            elapsed,
        )
    except Exception as exc:
        logger.warning(
            "<<< [DISCOVERY] Error fetching robots.txt from %s: %s — %.2fs",
            robots_url,
            exc,
            time.perf_counter() - t0,
        )

    return sitemap_urls, disallowed


def _is_disallowed(url: str, disallowed_paths: list[str]) -> bool:
    """Check if a URL path matches any disallowed pattern from robots.txt."""
    parsed_path = urlparse(url).path
    for pattern in disallowed_paths:
        # Simple prefix matching (robots.txt standard)
        if parsed_path.startswith(pattern):
            return True
    return False


def _extract_locale_prefix(url: str) -> str | None:
    """Extract a locale path prefix like ``en-us`` from a URL, if present."""
    path = urlparse(url).path.strip("/")
    if not path:
        return None

    first_segment = path.split("/", 1)[0].lower()
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", first_segment):
        return first_segment
    return None


def _matches_locale_scope(url: str, locale_prefix: str | None) -> bool:
    """Keep locale-scoped sitemap discovery aligned with the requested locale."""
    if not locale_prefix:
        return True

    path = urlparse(url).path.strip("/")
    if not path:
        return True

    first_segment = path.split("/", 1)[0].lower()
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", first_segment):
        return first_segment == locale_prefix
    return True


def _finalize_discovered_urls(
    all_urls: list[str],
    disallowed_paths: list[str],
    max_pages: int,
    *,
    manual_urls: list[str] | None = None,
) -> list[str]:
    """Finalize discovery results with hotel-first prioritization."""
    filtered = deduplicate_urls(all_urls)
    filtered = [u for u in filtered if not _is_disallowed(u, disallowed_paths)]
    if not filtered or max_pages <= 0:
        return []

    manual_selected: list[str] = []
    if manual_urls:
        manual_set = set(deduplicate_urls(manual_urls))
        manual_selected = [u for u in filtered if u in manual_set]

    manual_seen = set(manual_selected)
    remaining_pool = [u for u in filtered if u not in manual_seen]
    remaining_limit = max(max_pages - len(manual_selected), 0)
    prioritized = prioritize_discovery_links(remaining_pool, limit=remaining_limit)

    selected = deduplicate_urls(manual_selected + prioritized)
    if len(selected) < min(max_pages, len(filtered)):
        selected_set = set(selected)
        for url in filtered:
            if url in selected_set:
                continue
            selected.append(url)
            selected_set.add(url)
            if len(selected) >= max_pages:
                break

    logger.info(
        "--- [DISCOVERY] Final selection prioritization | filtered=%d | manual=%d | prioritized=%d | returned=%d",
        len(filtered),
        len(manual_selected),
        len(prioritized),
        len(selected),
    )
    return selected[:max_pages]


# ── Sitemap Parsing ──────────────────────────────────────────────────────────


# ── Common Hotel URL Path Probing ────────────────────────────────────────────
# SPA sites (React/Next/Vue) don't always server-render all navigation links,
# so BFS may miss critical pages on different runs.  We probe a fixed list of
# known hotel URL patterns after BFS and include any that return HTTP 200.
# This guarantees rooms, dining, meetings pages are always crawled consistently.

_HOTEL_PROBE_PATHS: list[str] = [
    # Rooms & Accommodation
    "/rooms", "/rooms/", "/accommodation", "/accommodation/",
    "/suites", "/suites/", "/guest-rooms", "/guestrooms",
    # Dining
    "/dining", "/dining/", "/restaurants", "/restaurants/",
    "/food-and-beverage", "/food-beverage",
    # Meetings & Events
    "/meetings", "/meetings/", "/events", "/events/",
    "/meetings-events", "/meetings-and-events",
    "/banquet", "/banquets", "/banquet-halls",
    "/weddings", "/conferences",
    # Spa & Wellness
    "/spa", "/spa/", "/wellness", "/wellness/",
    "/fitness", "/gym",
    # Offers & Packages
    "/offers", "/offers/", "/packages", "/packages/",
    "/special-offers", "/promotions",
    # About & Contact
    "/about", "/about-us", "/contact", "/contact-us",
    # Location
    "/location", "/directions", "/getting-here",
    # Gallery
    "/gallery", "/gallery/", "/photos",
]


async def _probe_hotel_paths(
    base_url: str,
    client: httpx.AsyncClient,
    already_found: set[str],
) -> list[str]:
    """Probe common hotel URL patterns and return genuine new pages.

    Called after BFS to catch critical pages (rooms, dining, meetings) that
    SPA sites might omit from their server-rendered HTML or sitemap.

    SPA catch-all detection: many Next.js/React/Vue sites return HTTP 200 for
    EVERY route — even non-existent ones — because routing is handled client-
    side. We detect this by comparing each probed page's title and content
    fingerprint against the homepage. If they match, the URL is a catch-all
    redirect and is skipped.
    """
    found: list[str] = []
    base = base_url.rstrip("/")

    # ── Step 1: fingerprint the homepage ────────────────────────────────────
    # We need the homepage title + content length to detect SPA catch-all URLs.
    homepage_title: str = ""
    homepage_text_len: int = 0
    try:
        hp_resp = await client.get(
            base_url, headers=_HEADERS, timeout=15, follow_redirects=True
        )
        if hp_resp.status_code == 200:
            hp_soup = BeautifulSoup(hp_resp.text, "lxml")
            title_tag = hp_soup.find("title")
            homepage_title = title_tag.get_text(strip=True).lower() if title_tag else ""
            body = hp_soup.find("body")
            homepage_text_len = len(body.get_text(strip=True)) if body else 0
            logger.debug(
                "    [PROBE] Homepage fingerprint: title=%r text_len=%d",
                homepage_title[:60], homepage_text_len,
            )
    except Exception as exc:
        logger.debug("    [PROBE] Could not fingerprint homepage: %s", exc)

    # ── Step 2: probe each candidate path ───────────────────────────────────
    async def _probe(path: str) -> str | None:
        url = urljoin(base + "/", path.lstrip("/"))
        norm = normalize_url(url)
        if norm in already_found:
            return None
        try:
            resp = await client.get(
                url, headers=_HEADERS, timeout=12, follow_redirects=True
            )
            if resp.status_code != 200:
                return None

            # Parse the response to check for SPA catch-all
            soup = BeautifulSoup(resp.text, "lxml")
            title_tag = soup.find("title")
            page_title = title_tag.get_text(strip=True).lower() if title_tag else ""
            body = soup.find("body")
            page_text_len = len(body.get_text(strip=True)) if body else 0

            # Detect SPA catch-all: same title as homepage = same page served
            if homepage_title and page_title == homepage_title:
                logger.debug(
                    "    [PROBE] SPA catch-all (same title as homepage): %s", url
                )
                return None

            # Detect SPA catch-all: near-identical content length (±5%)
            if homepage_text_len > 0 and page_text_len > 0:
                ratio = page_text_len / homepage_text_len
                if 0.92 <= ratio <= 1.08:
                    logger.debug(
                        "    [PROBE] SPA catch-all (same content length %d≈%d): %s",
                        page_text_len, homepage_text_len, url,
                    )
                    return None

            logger.debug("    [PROBE] Real page found: %s (title=%r)", url, page_title[:60])
            return norm

        except Exception as exc:
            logger.debug("    [PROBE] Error probing %s: %s", url, exc)
        return None

    tasks = [_probe(path) for path in _HOTEL_PROBE_PATHS]
    results = await asyncio.gather(*tasks)
    seen: set[str] = set()
    for r in results:
        if r and r not in already_found and r not in seen:
            found.append(r)
            seen.add(r)

    if found:
        logger.info(
            "--- [PROBE] Found %d genuine hotel pages via path probing: %s",
            len(found), found,
        )
    else:
        logger.info("--- [PROBE] No additional pages found via path probing (SPA catch-alls filtered)")

    return found


async def _fetch_sitemap_urls(
    sitemap_url: str,
    client: httpx.AsyncClient,
    base_domain: str,
    max_pages: int,
    locale_prefix: str | None = None,
    _depth: int = 0,
) -> list[str]:
    """Recursively fetch and parse sitemap XML, returning page URLs.

    Handles both sitemap index files (nested sitemaps) and regular
    urlset sitemaps.  Limits recursion depth to 3 to avoid infinite loops.
    """
    if _depth > 3:
        logger.debug(
            "    [SITEMAP] Max recursion depth reached for %s — skipping", sitemap_url
        )
        return []

    urls: list[str] = []

    logger.info(">>> [DISCOVERY] Fetching sitemap: %s (depth=%d)", sitemap_url, _depth)
    t0 = time.perf_counter()

    try:
        html_text, status_code = await _fetch_page(sitemap_url, client, label="sitemap")

        if status_code != 200 or not html_text:
            logger.info(
                "<<< [DISCOVERY] Sitemap not available at %s (status=%d) — %.2fs",
                sitemap_url,
                status_code,
                time.perf_counter() - t0,
            )
            return urls

        content = html_text

        # SPAs (React/Vue/Next) serve their HTML shell for ALL routes including
        # /sitemap.xml.  Detect this early instead of trying to parse HTML as XML.
        stripped = content.lstrip()
        if stripped.startswith("<!") or stripped.lower().startswith("<html"):
            logger.info(
                "<<< [DISCOVERY] Sitemap URL returned HTML (SPA catch-all routing) at %s — skipping XML parse — %.2fs",
                sitemap_url,
                time.perf_counter() - t0,
            )
            return urls

        # Strip XML namespace for easier parsing
        content_clean = re.sub(r'\s+xmlns="[^"]+"', "", content, count=1)

        try:
            root = ET.fromstring(content_clean)
        except ET.ParseError:
            logger.warning(
                "<<< [DISCOVERY] Failed to parse sitemap XML at %s — %.2fs",
                sitemap_url,
                time.perf_counter() - t0,
            )
            return urls

        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        if tag == "sitemapindex":
            # Nested sitemaps
            nested_count = 0
            for sitemap_el in root.iter():
                loc_tag = (
                    sitemap_el.tag.split("}")[-1]
                    if "}" in sitemap_el.tag
                    else sitemap_el.tag
                )
                if loc_tag == "loc":
                    nested_url = (sitemap_el.text or "").strip()
                    if nested_url and _matches_locale_scope(nested_url, locale_prefix):
                        nested_count += 1
                        nested = await _fetch_sitemap_urls(
                            nested_url,
                            client,
                            base_domain,
                            max_pages,
                            locale_prefix,
                            _depth + 1,
                        )
                        urls.extend(nested)
                        if len(urls) >= max_pages:
                            break
            logger.debug(
                "    [SITEMAP] Index at %s contained %d nested sitemaps",
                sitemap_url,
                nested_count,
            )
        else:
            # Regular urlset
            for url_el in root.iter():
                loc_tag = (
                    url_el.tag.split("}")[-1]
                    if "}" in url_el.tag
                    else url_el.tag
                )
                if loc_tag == "loc":
                    page_url = (url_el.text or "").strip()
                    if (
                        page_url
                        and is_same_domain(page_url, base_domain)
                        and _matches_locale_scope(page_url, locale_prefix)
                        and not should_skip_url(page_url)
                    ):
                        urls.append(normalize_url(page_url))
                        if len(urls) >= max_pages:
                            break

        elapsed = time.perf_counter() - t0
        logger.info(
            "<<< [DISCOVERY] Sitemap yielded %d URLs from %s — %.2fs",
            len(urls),
            sitemap_url,
            elapsed,
        )
    except Exception as exc:
        logger.warning(
            "<<< [DISCOVERY] Error fetching sitemap %s: %s — %.2fs",
            sitemap_url,
            exc,
            time.perf_counter() - t0,
        )

    return urls


# ── BFS Link Crawling ────────────────────────────────────────────────────────


async def _bfs_crawl(
    base_url: str,
    client: httpx.AsyncClient,
    base_domain: str,
    max_depth: int,
    max_pages: int,
    disallowed_paths: list[str],
    already_found: set[str],
    progress_callback: DiscoveryProgressCallback = None,
) -> list[str]:
    """Breadth-first crawl starting from *base_url*, extracting internal links.

    Respects max_depth, max_pages, robots.txt disallowed paths, and URL
    filter rules.

    Link extraction strategy:
      1. Try Scrapling CSS selectors (``page.css('a[href]')``)
      2. Fall back to BeautifulSoup if Scrapling yields nothing
    """
    seed_urls: set[str] = set(already_found)
    visited: set[str] = set(already_found)
    discovered: list[str] = []

    # Queue entries: (url, depth)
    queue: deque[tuple[str, int]] = deque()
    start = normalize_url(base_url)
    queue.append((start, 0))
    visited.add(start)

    logger.info(
        ">>> [DISCOVERY] BFS crawl starting from %s (max_depth=%d, max_pages=%d)",
        base_url,
        max_depth,
        max_pages,
    )
    bfs_t0 = time.perf_counter()
    pages_fetched = 0

    while queue and len(discovered) < max_pages:
        url, depth = queue.popleft()

        try:
            html_text, status_code = await _fetch_page(
                url, client, label=f"BFS-d{depth}"
            )

            if status_code != 200 or not html_text:
                continue

            # Quick content-type heuristic: if the body doesn't look like
            # HTML at all, skip link extraction.  (Scrapling always returns
            # HTML; httpx may return other types.)
            if not re.search(r"<\s*(html|head|body|div|a)\b", html_text[:2000], re.I):
                logger.debug(
                    "    [BFS] Skipping non-HTML content at %s (depth=%d)", url, depth
                )
                continue

            if url not in seed_urls:
                discovered.append(url)
            pages_fetched += 1
            logger.debug(
                "    [BFS] Discovered: %s (depth=%d, total=%d)",
                url,
                depth,
                len(discovered),
            )

            # Log progress periodically
            if pages_fetched == 1 or pages_fetched % 10 == 0:
                total_found = len(seed_urls) + len(discovered)
                logger.info(
                    "--- [DISCOVERY] BFS depth=%d: visited %d, discovered %d, queue=%d",
                    depth,
                    len(visited),
                    total_found,
                    len(queue),
                )
                await _report_discovery_progress(
                    progress_callback,
                    stage="bfs",
                    pages_found=total_found,
                    visited=len(visited),
                    queue_size=len(queue),
                    depth=depth,
                )

            # Don't extract links beyond max_depth
            if depth >= max_depth:
                continue

            # ── Link extraction: Scrapling first, BS4 fallback ───────
            links = _extract_links_scrapling(html_text, url)
            if not links:
                links = _extract_links_bs4(html_text, url)

            # ── JS link-extraction fallback ───────────────────────
            # If static HTML had 0 links AND the page looks like a
            # client-side-rendered SPA, re-render with Playwright and
            # re-extract.  This is the fix for sites like Iconiqa where
            # all navigation is injected by React/Vue/Next at runtime.
            if not links and _is_js_rendered_page(html_text):
                links = await _fetch_and_extract_links_dynamic(url, url)

            candidate_links: list[str] = []
            page_seen: set[str] = set()
            for absolute in links:
                normalized = normalize_url(absolute)

                if not normalized:
                    continue
                if normalized in page_seen:
                    continue
                if not is_same_domain(normalized, base_domain):
                    continue
                if _is_disallowed(normalized, disallowed_paths):
                    continue
                if normalized in visited:
                    continue

                page_seen.add(normalized)
                candidate_links.append(normalized)

            prioritized_links = prioritize_discovery_links(
                candidate_links,
                limit=_max_links_for_depth(depth),
            )
            for normalized in prioritized_links:
                visited.add(normalized)
                queue.append((normalized, depth + 1))

            logger.debug(
                "    [BFS] Extracted %d total links from %s, %d candidate internal links, %d queued after prioritization",
                len(links),
                url,
                len(candidate_links),
                len(prioritized_links),
            )

        except Exception as exc:
            logger.warning("    [BFS] Error processing %s: %s", url, exc)
            continue

        # Polite delay between requests
        await asyncio.sleep(0.3)

    bfs_elapsed = time.perf_counter() - bfs_t0
    logger.info(
        "<<< [DISCOVERY] BFS complete: %d pages found, %d visited, %.2fs elapsed",
        len(seed_urls) + len(discovered),
        len(visited),
        bfs_elapsed,
    )
    await _report_discovery_progress(
        progress_callback,
        stage="bfs_complete",
        pages_found=len(seed_urls) + len(discovered),
        visited=len(visited),
        queue_size=len(queue),
        depth=max_depth,
    )
    return discovered


# ── Public API ────────────────────────────────────────────────────────────────


async def discover_urls(
    base_url: str,
    max_depth: int | None = None,
    max_pages: int | None = None,
    specific_urls: list[str] | None = None,
    progress_callback: DiscoveryProgressCallback = None,
) -> list[str]:
    """Discover all crawlable pages from a hotel website.

    Pipeline:
      1. Fetch /robots.txt -- parse sitemaps and disallowed paths
      2. Fetch /sitemap.xml (and any sitemaps from robots.txt) -- extract URLs
      3. BFS crawl from homepage -- follow internal links up to *max_depth*

    Returns a sorted, deduplicated list of URLs.
    """
    if max_depth is None:
        max_depth = settings.max_depth
    if max_pages is None:
        max_pages = settings.max_pages_per_site

    base_domain = extract_domain(base_url)
    locale_prefix = _extract_locale_prefix(base_url)
    collection_limit = _discovery_collection_limit(max_pages)
    if not base_domain:
        logger.error("[DISCOVERY] Cannot extract domain from URL: %s", base_url)
        return []

    logger.info(
        "=== [DISCOVERY] START for %s (max_depth=%d, max_pages=%d, collection_limit=%d, scrapling=%s)",
        base_url,
        max_depth,
        max_pages,
        collection_limit,
        "YES" if _SCRAPLING_AVAILABLE else "NO",
    )
    overall_t0 = time.perf_counter()

    all_urls: list[str] = []
    manual_urls = deduplicate_urls(specific_urls or [])
    probe_results: list[str] = []  # populated by Phase 4; init here for log scope
    homepage_spa_count: int = 0    # populated by Phase 2.5; init here for log scope

    async with httpx.AsyncClient(follow_redirects=True, http2=True) as client:
        # ── Phase 1: robots.txt ──────────────────────────────────────
        phase1_t0 = time.perf_counter()
        sitemap_urls_from_robots, disallowed_paths = await _fetch_robots_txt(
            base_url, client
        )
        phase1_elapsed = time.perf_counter() - phase1_t0
        logger.info(
            "--- [DISCOVERY] Phase 1 (robots.txt) done in %.2fs", phase1_elapsed
        )
        await _report_discovery_progress(
            progress_callback,
            stage="robots",
            pages_found=0,
            visited=0,
            queue_size=0,
            depth=0,
        )

        # ── Phase 2: Sitemaps ────────────────────────────────────────
        phase2_t0 = time.perf_counter()
        # Always try the common sitemap defaults even if robots.txt omits them.
        sitemap_candidates = list(
            set(
                sitemap_urls_from_robots
                + [
                    urljoin(base_url, "/sitemap.xml"),
                    urljoin(base_url, "/sitemap_index.xml"),
                    urljoin(base_url, "/sitemap-index.xml"),
                ]
            )
        )
        logger.info(
            ">>> [DISCOVERY] Will try %d sitemap candidate(s): %s",
            len(sitemap_candidates),
            sitemap_candidates,
        )

        for sm_url in sitemap_candidates:
            if len(deduplicate_urls(all_urls)) >= collection_limit:
                logger.info(
                    "--- [DISCOVERY] Sitemap phase: already at collection_limit=%d — stopping",
                    collection_limit,
                )
                break
            sm_results = await _fetch_sitemap_urls(
                sm_url, client, base_domain, collection_limit, locale_prefix
            )
            all_urls.extend(sm_results)
            await _report_discovery_progress(
                progress_callback,
                stage="sitemaps",
                pages_found=len(set(all_urls)),
                visited=0,
                queue_size=0,
                depth=0,
            )

        sitemap_count = len(set(all_urls))
        phase2_elapsed = time.perf_counter() - phase2_t0
        logger.info(
            "--- [DISCOVERY] Phase 2 (sitemaps) done in %.2fs: %d unique URLs from sitemaps",
            phase2_elapsed,
            sitemap_count,
        )

        # ── Phase 2.5: Playwright homepage discovery (Option A) ─────
        # For SPA sites (React/Next.js/Vue) the server-rendered HTML often
        # omits nav links, making BFS miss key pages like /rooms or /meetings.
        # We render the homepage with Playwright and extract ALL links from the
        # live hydrated DOM before BFS starts — guaranteeing nav coverage
        # regardless of URL path names.
        phase25_t0 = time.perf_counter()
        homepage_spa_count = 0

        # Fetch homepage statically to detect if it's a SPA
        homepage_html_static, homepage_status = await _fetch_page(
            base_url, client, label="spa-detect"
        )
        if homepage_status == 200 and homepage_html_static and _is_js_rendered_page(homepage_html_static):
            logger.info(
                "--- [DISCOVERY] SPA detected at %s — rendering homepage with Playwright",
                base_url,
            )
            spa_raw_links = await _fetch_and_extract_links_dynamic(base_url, base_url)
            already_norm = set(normalize_url(u) for u in all_urls)
            for link in spa_raw_links:
                norm = normalize_url(link)
                if (
                    is_same_domain(link, base_domain)
                    and not should_skip_url(link)
                    and norm not in already_norm
                ):
                    all_urls.append(norm)
                    already_norm.add(norm)
                    homepage_spa_count += 1

        phase25_elapsed = time.perf_counter() - phase25_t0
        logger.info(
            "--- [DISCOVERY] Phase 2.5 (Playwright homepage) done in %.2fs: %d new links",
            phase25_elapsed,
            homepage_spa_count,
        )
        await _report_discovery_progress(
            progress_callback,
            stage="homepage_playwright",
            pages_found=len(deduplicate_urls(all_urls)),
            visited=0,
            queue_size=0,
            depth=0,
        )

        # ── Phase 3: BFS crawl (Option C: skip if sitemap is sufficient) ─
        # If sitemaps + Playwright homepage already gave us a solid URL set
        # (≥ 8 URLs), BFS adds little value and can be skipped entirely.
        # BFS on SPA sites is slow and non-deterministic — the sitemap +
        # Playwright combo is more reliable for well-structured hotel sites.
        phase3_t0 = time.perf_counter()
        already_found = set(normalize_url(u) for u in all_urls)
        bfs_count = 0

        _SITEMAP_SUFFICIENT = 8  # if we already have this many URLs, skip BFS

        if len(already_found) >= collection_limit:
            logger.info(
                "--- [DISCOVERY] Skipping BFS: already at collection_limit=%d",
                collection_limit,
            )
        elif len(already_found) >= _SITEMAP_SUFFICIENT:
            logger.info(
                "--- [DISCOVERY] Skipping BFS: sitemap + Playwright homepage already "
                "found %d URLs (threshold=%d) — sitemap-first strategy active",
                len(already_found),
                _SITEMAP_SUFFICIENT,
            )
        else:
            logger.info(
                "--- [DISCOVERY] Only %d URLs so far (threshold=%d) — running BFS",
                len(already_found),
                _SITEMAP_SUFFICIENT,
            )
            bfs_results = await _bfs_crawl(
                base_url,
                client,
                base_domain,
                max_depth,
                collection_limit - len(already_found),
                disallowed_paths,
                already_found,
                progress_callback=progress_callback,
            )
            all_urls.extend(bfs_results)
            bfs_count = len(bfs_results)

        phase3_elapsed = time.perf_counter() - phase3_t0
        logger.info(
            "--- [DISCOVERY] Phase 3 (BFS) done in %.2fs: %d new URLs from BFS",
            phase3_elapsed,
            bfs_count,
        )

        # ── Phase 4: Hotel path probing ──────────────────────────────
        # Probe known hotel URL patterns to catch pages that SPA sites
        # don't server-render into BFS-discoverable links (rooms, dining,
        # meetings, spa etc.).  This makes coverage deterministic across runs.
        phase4_t0 = time.perf_counter()
        already_after_bfs = set(normalize_url(u) for u in all_urls)
        probe_results = await _probe_hotel_paths(base_url, client, already_after_bfs)
        all_urls.extend(probe_results)
        phase4_elapsed = time.perf_counter() - phase4_t0
        logger.info(
            "--- [DISCOVERY] Phase 4 (path probe) done in %.2fs: %d additional URLs",
            phase4_elapsed,
            len(probe_results),
        )
        await _report_discovery_progress(
            progress_callback,
            stage="probe",
            pages_found=len(deduplicate_urls(all_urls)),
            visited=0,
            queue_size=0,
            depth=0,
        )

        unique_after_bfs = deduplicate_urls(all_urls)
        remaining_capacity = collection_limit - len(unique_after_bfs)

        if remaining_capacity > 0 and len(unique_after_bfs) <= 3:
            homepage_html, homepage_status = await _fetch_page(
                base_url,
                client,
                label="external-seeds",
            )
            if homepage_status == 200 and homepage_html:
                external_urls = _extract_external_property_urls(
                    homepage_html,
                    base_url,
                    base_domain,
                    limit=remaining_capacity,
                )
                all_urls.extend(external_urls)
                await _report_discovery_progress(
                    progress_callback,
                    stage="external_seeds",
                    pages_found=len(deduplicate_urls(all_urls)),
                    visited=0,
                    queue_size=0,
                    depth=0,
                )

        # ── Block detection ──────────────────────────────────────────────────
        # If every phase returned 0 URLs and the user did not manually supply
        # any, do one raw HTTP check to determine whether the site is behind a
        # bot-protection wall.  When confirmed, raise SiteBlockedError so the
        # orchestrator can fail the job immediately with a clear message instead
        # of silently crawling 0 pages and crashing at publish time.
        if not deduplicate_urls(all_urls) and not manual_urls:
            logger.info(
                "--- [DISCOVERY] 0 URLs found — checking if %s is permanently blocked",
                base_url,
            )
            try:
                check_resp = await client.get(
                    base_url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True
                )
                block_reason = detect_protection_interstitial(
                    check_resp.text, check_resp.status_code
                )
                if block_reason:
                    logger.warning(
                        "=== [DISCOVERY] SITE BLOCKED: %s — reason: %s", base_url, block_reason
                    )
                    raise SiteBlockedError(block_reason, base_url)
            except SiteBlockedError:
                raise
            except Exception as exc:
                logger.debug("    [DISCOVERY] Block detection check failed: %s", exc)

        if manual_urls:
            logger.info(
                "--- [DISCOVERY] Adding %d manually specified URL(s)",
                len(manual_urls),
            )
            all_urls.extend(manual_urls)

    # ── Deduplicate, filter disallowed, sort ─────────────────────────
    unique = _finalize_discovered_urls(
        all_urls,
        disallowed_paths,
        max_pages,
        manual_urls=manual_urls,
    )

    overall_elapsed = time.perf_counter() - overall_t0

    logger.info(
        "=== DISCOVERY COMPLETE: %s | total=%d URLs "
        "(sitemap=%d, playwright_homepage=%d, bfs=%d, probe=%d) | time=%.2fs",
        base_url,
        len(unique),
        sitemap_count,
        homepage_spa_count,
        bfs_count,
        len(probe_results),
        overall_elapsed,
    )
    await _report_discovery_progress(
        progress_callback,
        stage="complete",
        pages_found=len(unique),
        visited=len(unique),
        queue_size=0,
        depth=max_depth,
    )
    return unique
