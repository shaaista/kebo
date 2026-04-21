"""Main crawling engine for both static HTML and JS-rendered pages.

Strategy per page (cascading from fastest to heaviest):

  PRIMARY PATH -- Scrapling
    1. Scrapling Fetcher        -- fast HTTP with TLS fingerprint impersonation
    2. Scrapling StealthyFetcher -- headless stealth browser + Cloudflare solver
    3. Scrapling DynamicFetcher  -- full Playwright-backed browser via Scrapling

  FALLBACK PATH -- existing tools
    4. protection_handler        -- httpx -> curl_cffi -> cloudscraper cascade
    5. Crawl4AI AsyncWebCrawler  -- Playwright-backed crawl with markdown output
    6. Manual Playwright         -- raw Playwright stealth (Tier 3: Cloudflare)
    7. Camoufox                  -- hardened Firefox (Tier 4: Akamai/PerimeterX/DataDome)

  Text extraction cascade:
    trafilatura -> readability-lxml -> BeautifulSoup
"""

import asyncio
import re
import logging
import random
import time
from collections import defaultdict
from typing import Any, Callable, Coroutine
from urllib.parse import urlparse

import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify
from readability import Document as ReadabilityDocument

from config.settings import settings
from scraper.protection_handler import detect_protection_interstitial, fetch_with_protection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Scrapling imports -- graceful degradation when not installed
# ---------------------------------------------------------------------------

_SCRAPLING_AVAILABLE = False
try:
    from scrapling import Fetcher as ScraplingFetcher
    _SCRAPLING_AVAILABLE = True
    logger.debug("scrapling Fetcher available")
except ImportError:
    ScraplingFetcher = None  # type: ignore[assignment,misc]
    logger.debug("scrapling package not installed -- scrapling methods will be skipped")

try:
    from scrapling import StealthyFetcher as ScraplingStealthyFetcher
except ImportError:
    ScraplingStealthyFetcher = None  # type: ignore[assignment,misc]
    logger.debug("scrapling StealthyFetcher unavailable (patchright not installed)")

try:
    from scrapling import DynamicFetcher as ScraplingDynamicFetcher
except ImportError:
    ScraplingDynamicFetcher = None  # type: ignore[assignment,misc]
    logger.debug("scrapling DynamicFetcher unavailable (msgspec not installed)")

# ---------------------------------------------------------------------------
# Optional Camoufox import -- Tier 4 anti-bot (Akamai / PerimeterX / DataDome)
# Camoufox is a hardened Firefox build with full fingerprint spoofing.
# It handles sites that defeat Playwright's stealth patches.
# ---------------------------------------------------------------------------
_CAMOUFOX_AVAILABLE = False
try:
    from camoufox.async_api import AsyncCamoufox  # type: ignore[import-not-found]
    _CAMOUFOX_AVAILABLE = True
    logger.debug("camoufox detected -- Tier 4 anti-bot fallback available")
except ImportError:
    AsyncCamoufox = None  # type: ignore[assignment,misc]
    logger.debug("camoufox not installed -- Tier 4 fallback unavailable")

# ---------------------------------------------------------------------------
# Optional playwright-stealth import — hides navigator.webdriver and other
# automation tells that Akamai/PerimeterX fingerprint checks look for.
# ---------------------------------------------------------------------------
_PLAYWRIGHT_STEALTH_AVAILABLE = False
try:
    from playwright_stealth import Stealth as _PlaywrightStealth  # type: ignore[import-not-found]
    _PLAYWRIGHT_STEALTH_AVAILABLE = True
    logger.debug("playwright-stealth detected -- automation fingerprint hiding enabled")
except ImportError:
    _PlaywrightStealth = None  # type: ignore[assignment,misc]
    logger.debug("playwright-stealth not installed -- continuing without stealth patches")


# ── Behavioural Signal Simulation ────────────────────────────────────────
# Akamai Bot Manager and PerimeterX score sessions partly on mouse movement,
# scroll cadence, and inter-action timing.  Simulating these inside the page
# brings the behavioural score closer to a real human visitor.


async def _human_behaviour(page) -> None:  # type: ignore[no-untyped-def]
    """Simulate human-like mouse movement and scroll to lower bot-score.

    Called after every page load inside Playwright (Step 6) and Camoufox
    (Step 7).  Adds:
      - Random mouse moves (5–8 points across the viewport)
      - Smooth scroll: 30–60 % of page height in 2–4 increments
      - Random inter-event pauses (80–400 ms) matching human reading pace
    """
    try:
        vw = 1920
        vh = 1080
        # Random mouse trajectory — move to 5–8 random viewport coords
        points = random.randint(5, 8)
        for _ in range(points):
            x = random.randint(50, vw - 50)
            y = random.randint(50, vh - 50)
            await page.mouse.move(x, y)
            await page.wait_for_timeout(random.randint(80, 300))

        # Scroll down in 2–4 increments, total 30–60 % of viewport height
        scroll_steps = random.randint(2, 4)
        total_scroll = random.randint(int(vh * 0.3), int(vh * 0.6))
        per_step = total_scroll // scroll_steps
        for _ in range(scroll_steps):
            await page.mouse.wheel(0, per_step)
            await page.wait_for_timeout(random.randint(150, 400))

        # Final pause (0.8–2.5 s) simulating reading before extracting content
        await page.wait_for_timeout(random.randint(800, 2500))
    except Exception as exc:
        logger.debug("_human_behaviour skipped: %s", exc)


# ── JS Rendering Detection ────────────────────────────────────────────────

# Markers that indicate the page requires JS rendering to get real content
_JS_MARKERS = [
    "__NEXT_DATA__",
    "__NUXT__",
    "window.__INITIAL_STATE__",
    'id="__next"',
    'id="app"',
    'id="root"',
    "react-root",
    "ng-app",
    "ng-version",
    "data-reactroot",
    "vue-app",
    '<div id="app"></div>',
    '<div id="root"></div>',
]

_NOSCRIPT_HINT = "<noscript"


def needs_js_rendering(html: str) -> bool:
    """Detect if a page likely requires a real browser to render content.

    Checks for:
      - Very short body text (SPA shell with no server-rendered content)
      - <noscript> tags suggesting JS is required
      - Framework markers (React, Next.js, Vue, Angular, Nuxt)
    """
    if not html or len(html.strip()) < 200:
        return True

    html_lower = html.lower()

    # Check for noscript hints
    if _NOSCRIPT_HINT in html_lower:
        # If there's a noscript tag telling users to enable JS,
        # the real content is probably JS-rendered.
        soup = BeautifulSoup(html, "lxml")
        noscripts = soup.find_all("noscript")
        for ns in noscripts:
            text = ns.get_text(strip=True).lower()
            if "enable javascript" in text or "javascript is required" in text:
                return True

    # Check for SPA framework markers
    for marker in _JS_MARKERS:
        if marker.lower() in html_lower:
            # Marker found -- but only flag it if the body is thin
            soup = BeautifulSoup(html, "lxml")
            body = soup.find("body")
            body_text = body.get_text(strip=True) if body else ""
            if len(body_text) < 300:
                return True

    return False


_DEFAULT_COMPLETENESS_BASELINE = {
    "text_len": 260,
    "heading_count": 3,
    "content_blocks": 6,
}

_PAGE_TYPE_COMPLETENESS_BASELINES: dict[str, dict[str, int]] = {
    "home": {"text_len": 600, "heading_count": 5, "content_blocks": 10},
    "room_detail": {"text_len": 420, "heading_count": 4, "content_blocks": 8},
    "offers": {"text_len": 320, "heading_count": 3, "content_blocks": 6},
    "dining": {"text_len": 360, "heading_count": 3, "content_blocks": 6},
    "gallery": {"text_len": 180, "heading_count": 2, "content_blocks": 3},
    "meetings": {"text_len": 340, "heading_count": 3, "content_blocks": 6},
    "experiences": {"text_len": 320, "heading_count": 3, "content_blocks": 6},
    "generic": dict(_DEFAULT_COMPLETENESS_BASELINE),
}

_HEADING_TAG_PATTERN = re.compile(r"^h[1-3]$")


def _classify_page_type(url: str) -> str:
    """Classify page type from URL path using generic hotel-web patterns."""
    parsed = urlparse(url)
    path = (parsed.path or "").lower().strip("/")
    if not path:
        return "home"

    tokens = [segment for segment in path.split("/") if segment]
    path_text = "/".join(tokens)

    if any(key in path_text for key in ("room", "suite", "accommodation", "stay")):
        return "room_detail"
    if any(key in path_text for key in ("offer", "package", "deal", "promotion")):
        return "offers"
    if any(key in path_text for key in ("dine", "dining", "restaurant", "bar", "cafe", "eat")):
        return "dining"
    if any(key in path_text for key in ("gallery", "photos", "images", "media")):
        return "gallery"
    if any(key in path_text for key in ("banquet", "meeting", "event", "wedding", "conference")):
        return "meetings"
    if any(key in path_text for key in ("experience", "activities", "attraction", "wellness", "spa")):
        return "experiences"
    return "generic"


def _extract_completeness_features(result: dict[str, Any]) -> dict[str, int]:
    """Compute raw extraction-quality features from crawl output."""
    html = result.get("html", "") or ""
    text = result.get("text", "") or ""

    text_len = len(text.strip())
    heading_count = 0
    content_blocks = 0

    if html:
        try:
            soup = BeautifulSoup(html, "lxml")
            heading_count = len(soup.find_all(_HEADING_TAG_PATTERN))
            content_blocks = sum(
                1
                for tag in soup.find_all(["p", "li", "article", "section"])
                if len(tag.get_text(" ", strip=True)) >= 40
            )
            if content_blocks < 3:
                content_blocks += sum(
                    1
                    for tag in soup.find_all("div")
                    if len(tag.get_text(" ", strip=True)) >= 90
                )
            content_blocks = min(content_blocks, 60)
        except Exception as exc:
            logger.debug("Completeness feature extraction failed for %s: %s", result.get("url", ""), exc)

    return {
        "text_len": text_len,
        "heading_count": heading_count,
        "content_blocks": content_blocks,
    }


def _percentile(values: list[int], p: float) -> float:
    """Return interpolated percentile for values in [0, 1]."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    index = (len(sorted_values) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _build_dynamic_thresholds(
    features_by_page_type: dict[str, list[dict[str, int]]]
) -> dict[str, dict[str, int]]:
    """Build adaptive thresholds per page type from current batch + safe floors."""
    thresholds: dict[str, dict[str, int]] = {}

    for page_type, features_list in features_by_page_type.items():
        base = _PAGE_TYPE_COMPLETENESS_BASELINES.get(page_type, _DEFAULT_COMPLETENESS_BASELINE)
        if not features_list:
            thresholds[page_type] = dict(base)
            continue

        text_values = [f["text_len"] for f in features_list]
        heading_values = [f["heading_count"] for f in features_list]
        block_values = [f["content_blocks"] for f in features_list]

        dynamic_text = int(_percentile(text_values, 0.25) * 0.72)
        dynamic_headings = int(_percentile(heading_values, 0.25) * 0.70)
        dynamic_blocks = int(_percentile(block_values, 0.25) * 0.70)

        thresholds[page_type] = {
            "text_len": max(base["text_len"], dynamic_text),
            "heading_count": max(base["heading_count"], dynamic_headings),
            "content_blocks": max(base["content_blocks"], dynamic_blocks),
        }

    return thresholds


def _evaluate_completeness(
    features: dict[str, int],
    thresholds: dict[str, int],
) -> tuple[float, list[str]]:
    """Score completeness and return reasons when the page is under-threshold."""
    text_ratio = min(features["text_len"] / max(thresholds["text_len"], 1), 1.0)
    heading_ratio = min(features["heading_count"] / max(thresholds["heading_count"], 1), 1.0)
    block_ratio = min(features["content_blocks"] / max(thresholds["content_blocks"], 1), 1.0)

    score = (0.65 * text_ratio) + (0.20 * heading_ratio) + (0.15 * block_ratio)
    reasons: list[str] = []

    if features["text_len"] < thresholds["text_len"]:
        reasons.append(f"text_len {features['text_len']} < {thresholds['text_len']}")
    if features["heading_count"] < thresholds["heading_count"]:
        reasons.append(f"heading_count {features['heading_count']} < {thresholds['heading_count']}")
    if features["content_blocks"] < thresholds["content_blocks"]:
        reasons.append(f"content_blocks {features['content_blocks']} < {thresholds['content_blocks']}")

    return score, reasons


# ── Text Extraction Cascade ───────────────────────────────────────────────


def _extract_text_trafilatura(html: str, url: str) -> str:
    """Extract main content text using trafilatura."""
    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=True,
            favor_precision=False,
            favor_recall=True,
        )
        return text or ""
    except Exception as exc:
        logger.debug("trafilatura extraction failed for %s: %s", url, exc)
        return ""


def _extract_text_readability(html: str, url: str) -> str:
    """Fallback: extract main content using readability-lxml."""
    try:
        doc = ReadabilityDocument(html, url=url)
        summary_html = doc.summary()
        soup = BeautifulSoup(summary_html, "lxml")
        return soup.get_text(separator="\n", strip=True)
    except Exception as exc:
        logger.debug("readability extraction failed for %s: %s", url, exc)
        return ""


def _extract_text_bs4(html: str) -> str:
    """Last-resort fallback: strip tags with BeautifulSoup."""
    try:
        soup = BeautifulSoup(html, "lxml")
        # Remove script/style elements
        for el in soup(["script", "style", "nav", "footer", "header"]):
            el.decompose()
        return soup.get_text(separator="\n", strip=True)
    except Exception as exc:
        logger.debug("BeautifulSoup extraction failed: %s", exc)
        return ""


def _extract_title(html: str) -> str:
    """Extract page title from HTML."""
    try:
        soup = BeautifulSoup(html, "lxml")
        # Try <title> tag first
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)
        # Fall back to first <h1>
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)
    except Exception:
        pass
    return ""


def _html_to_markdown(html: str) -> str:
    """Convert HTML to markdown using markdownify."""
    try:
        return markdownify(html, heading_style="ATX", strip=["script", "style"])
    except Exception as exc:
        logger.debug("markdownify conversion failed: %s", exc)
        return ""


def _extract_text_cascade(html: str, url: str) -> str:
    """Run the text extraction cascade: trafilatura -> readability -> BS4.

    Returns the first non-empty extraction result.
    """
    text = _extract_text_trafilatura(html, url)
    if text:
        return text

    text = _extract_text_readability(html, url)
    if text:
        return text

    return _extract_text_bs4(html)


def _raise_for_protection_interstitial(url: str, method: str, html: str, status: int) -> None:
    """Reject bot-protection pages before they contaminate extracted content."""
    reason = detect_protection_interstitial(html, status)
    if reason:
        logger.warning(
            "--- [%s] Rejected bot-protection response for %s: %s (status=%d)",
            method.upper(),
            url,
            reason,
            status,
        )
        raise RuntimeError(f"{reason} detected ({method})")


# ── PRIMARY PATH: Scrapling Methods ──────────────────────────────────────


async def _crawl_scrapling_fast(url: str) -> dict:
    """Use Scrapling Fetcher for fast HTTP fetch with TLS impersonation.

    This is the fastest Scrapling method -- pure HTTP, no browser. Good for
    static pages and pages with light protection.

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    if not _SCRAPLING_AVAILABLE:
        raise RuntimeError("scrapling package is not installed")

    logger.info(">>> [SCRAPLING-FAST] Attempting: %s", url)
    start = time.perf_counter()

    try:
        def _sync_fetch():
            fetcher = ScraplingFetcher()
            page = fetcher.get(url, stealthy_headers=True, timeout=settings.request_timeout_seconds)
            return page

        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(None, _sync_fetch)

        html = str(page.html_content) if hasattr(page, "html_content") else ""
        status = page.status if hasattr(page, "status") else 0

        if not html or status == 0:
            raise RuntimeError(f"Empty response (status={status}, html_len={len(html or '')})")

        if status >= 400:
            raise RuntimeError(f"HTTP error status {status}")

        _raise_for_protection_interstitial(url, "scrapling_fast", html, status)

        title = _extract_title(html)
        text = _extract_text_cascade(html, url)

        elapsed = time.perf_counter() - start
        logger.info(
            "<<< [SCRAPLING-FAST] SUCCESS: %s (status=%d, text_len=%d, %.2fs)",
            url, status, len(text), elapsed,
        )

        return {
            "url": url,
            "html": html,
            "text": text,
            "title": title,
            "status": status,
            "method": "scrapling_fast",
            "error": None,
        }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.warning(
            "xxx [SCRAPLING-FAST] FAILED: %s - %s (%.2fs)", url, exc, elapsed,
        )
        raise


async def _crawl_scrapling_stealth(url: str) -> dict:
    """Use Scrapling StealthyFetcher for stealth headless browser crawling.

    Includes Cloudflare bypass capabilities. Heavier than the fast fetcher
    but can handle JS challenges and bot detection.

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    if not _SCRAPLING_AVAILABLE or ScraplingStealthyFetcher is None:
        raise RuntimeError("scrapling StealthyFetcher not available (patchright not installed)")

    logger.info(">>> [SCRAPLING-STEALTH] Attempting: %s", url)
    start = time.perf_counter()

    try:
        def _sync_fetch():
            fetcher = ScraplingStealthyFetcher()
            # solve_cloudflare=True automatically detects and solves
            # Cloudflare Turnstile / Interstitial JS challenges (Tier 3).
            # Requires a 60s+ timeout — already covered by the executor.
            page = fetcher.fetch(url, solve_cloudflare=True)
            return page

        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(None, _sync_fetch)

        html = str(page.html_content) if hasattr(page, "html_content") else ""
        status = page.status if hasattr(page, "status") else 0

        if not html or status == 0:
            raise RuntimeError(f"Empty response (status={status}, html_len={len(html or '')})")

        if status >= 400:
            raise RuntimeError(f"HTTP error status {status}")

        _raise_for_protection_interstitial(url, "scrapling_stealth", html, status)

        title = _extract_title(html)
        text = _extract_text_cascade(html, url)

        elapsed = time.perf_counter() - start
        logger.info(
            "<<< [SCRAPLING-STEALTH] SUCCESS: %s (status=%d, text_len=%d, %.2fs)",
            url, status, len(text), elapsed,
        )

        return {
            "url": url,
            "html": html,
            "text": text,
            "title": title,
            "status": status,
            "method": "scrapling_stealth",
            "error": None,
        }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.warning(
            "xxx [SCRAPLING-STEALTH] FAILED: %s - %s (%.2fs)", url, exc, elapsed,
        )
        raise


async def _crawl_scrapling_dynamic(url: str) -> dict:
    """Use Scrapling DynamicFetcher for full Playwright-backed browser rendering.

    This is the heaviest Scrapling method -- launches a full browser, waits
    for the page to render, and returns the fully-rendered HTML. Use when
    StealthyFetcher fails or the page needs heavy JS execution.

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    if not _SCRAPLING_AVAILABLE or ScraplingDynamicFetcher is None:
        raise RuntimeError("scrapling DynamicFetcher not available (msgspec not installed)")

    logger.info(">>> [SCRAPLING-DYNAMIC] Attempting: %s", url)
    start = time.perf_counter()

    try:
        def _sync_fetch():
            fetcher = ScraplingDynamicFetcher()
            # Wait for real content to appear, not just for <body> to exist.
            # The selector tries common SPA root containers (React/Vue/Next)
            # first; Playwright resolves the first matching element, giving
            # the JS framework time to hydrate before we extract text.
            page = fetcher.fetch(
                url,
                wait_selector=(
                    "#root > *, #app > *, #__next > *, "
                    "[data-reactroot] > *, "
                    "main, article, "
                    "body"
                ),
            )
            return page

        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(None, _sync_fetch)

        html = str(page.html_content) if hasattr(page, "html_content") else ""
        status = page.status if hasattr(page, "status") else 0

        if not html:
            raise RuntimeError(f"Empty response (status={status}, html_len=0)")

        # DynamicFetcher may not always report a proper status code;
        # treat presence of HTML as success if status is 0.
        effective_status = status if status > 0 else 200

        _raise_for_protection_interstitial(url, "scrapling_dynamic", html, effective_status)

        title = _extract_title(html)
        text = _extract_text_cascade(html, url)

        elapsed = time.perf_counter() - start
        logger.info(
            "<<< [SCRAPLING-DYNAMIC] SUCCESS: %s (status=%d, text_len=%d, %.2fs)",
            url, effective_status, len(text), elapsed,
        )

        return {
            "url": url,
            "html": html,
            "text": text,
            "title": title,
            "status": effective_status,
            "method": "scrapling_dynamic",
            "error": None,
        }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.warning(
            "xxx [SCRAPLING-DYNAMIC] FAILED: %s - %s (%.2fs)", url, exc, elapsed,
        )
        raise


# ── FALLBACK PATH: Existing Tools ────────────────────────────────────────


async def _crawl_with_protection_handler(url: str) -> dict:
    """Fallback: use protection_handler cascade (httpx -> curl_cffi -> cloudscraper).

    Wraps the existing fetch_with_protection() and adds text extraction on
    top of the raw HTML result.

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    logger.info(">>> [PROTECTION-HANDLER] Attempting: %s", url)
    start = time.perf_counter()

    try:
        result = await fetch_with_protection(url, timeout=settings.request_timeout_seconds)

        html = result.get("html", "")
        status = result.get("status", 0)
        method = result.get("method", "unknown")
        error = result.get("error")

        if error or status == 0 or not html:
            raise RuntimeError(
                f"protection_handler failed: method={method}, status={status}, "
                f"error={error or 'empty response'}"
            )

        title = _extract_title(html)
        text = _extract_text_cascade(html, url)

        elapsed = time.perf_counter() - start
        logger.info(
            "<<< [PROTECTION-HANDLER] SUCCESS: %s (method=%s, status=%d, text_len=%d, %.2fs)",
            url, method, status, len(text), elapsed,
        )

        return {
            "url": url,
            "html": html,
            "text": text,
            "title": title,
            "status": status,
            "method": f"protection_{method}",
            "error": None,
        }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.warning(
            "xxx [PROTECTION-HANDLER] FAILED: %s - %s (%.2fs)", url, exc, elapsed,
        )
        raise


async def _crawl_with_browser(url: str) -> dict:
    """Use Crawl4AI's AsyncWebCrawler (Playwright-backed) for JS-heavy pages.

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    logger.info(">>> [CRAWL4AI] Attempting: %s", url)
    start = time.perf_counter()

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        browser_config = BrowserConfig(
            headless=True,
            verbose=False,
        )
        crawler_config = CrawlerRunConfig(
            # "networkidle" times out on sites with background analytics/polling.
            # Use "load" to match the direct-Playwright strategy used in Step 6.
            wait_until="load",
            page_timeout=settings.request_timeout_seconds * 1000,
        )

        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=crawler_config)

            if result and result.success:
                html = result.html or ""
                text = result.markdown or ""
                _raise_for_protection_interstitial(url, "crawl4ai", html, 200)
                title = _extract_title(html) if html else ""

                # If Crawl4AI's markdown extraction was empty, use our own cascade
                if not text and html:
                    text = _extract_text_cascade(html, url)

                elapsed = time.perf_counter() - start
                logger.info(
                    "<<< [CRAWL4AI] SUCCESS: %s (status=200, text_len=%d, %.2fs)",
                    url, len(text), elapsed,
                )

                return {
                    "url": url,
                    "html": html,
                    "text": text,
                    "title": title,
                    "status": 200,
                    "method": "crawl4ai",
                    "error": None,
                }
            else:
                error_msg = getattr(result, "error_message", "Crawl4AI returned no result")
                raise RuntimeError(error_msg)

    except ImportError:
        elapsed = time.perf_counter() - start
        logger.warning(
            "xxx [CRAWL4AI] NOT AVAILABLE: %s - crawl4ai not installed (%.2fs)",
            url, elapsed,
        )
        raise
    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.warning(
            "xxx [CRAWL4AI] FAILED: %s - %s (%.2fs)", url, exc, elapsed,
        )
        raise


async def _crawl_with_playwright(url: str, *, strict: bool = False) -> dict:
    """Manual Playwright fallback with stealth for JS-rendered pages.

    This is the last-resort method. Launches a full Chromium browser with
    optional stealth plugin.

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    method_label = "PLAYWRIGHT-STRICT" if strict else "PLAYWRIGHT"
    logger.info(">>> [%s] Attempting: %s", method_label, url)
    start = time.perf_counter()

    try:
        from playwright.async_api import async_playwright

        # Build proxy config if a residential proxy is configured.
        # This routes the browser's traffic through a residential IP so Akamai
        # and PerimeterX IP-reputation checks see a real ISP address, not a
        # datacenter block.
        proxy_config = None
        if settings.residential_proxy_url:
            proxy_config = {"server": settings.residential_proxy_url}
            logger.info(
                "--- [%s] Using residential proxy: %s",
                method_label,
                settings.residential_proxy_url.split("@")[-1],  # hide credentials
            )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, proxy=proxy_config)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                )

                # Apply playwright-stealth to hide navigator.webdriver, chrome
                # runtime, and other Chromium automation fingerprints that
                # Akamai/PerimeterX look for in JS property checks.
                if _PLAYWRIGHT_STEALTH_AVAILABLE and _PlaywrightStealth is not None:
                    stealth = _PlaywrightStealth()
                    await stealth.apply_stealth_async(context)
                    logger.debug("--- [%s] playwright-stealth applied to context", method_label)

                page = await context.new_page()

                # "networkidle" times out on sites with background analytics/
                # polling (e.g. live-chat, booking widgets).  Use "load" +
                # a 4s post-load pause instead — reliably captures React/Vue/
                # Next content after the initial hydration burst completes.
                nav_timeout_ms = max(settings.request_timeout_seconds * 1000, 45000)
                if strict:
                    try:
                        await page.goto(
                            url,
                            wait_until="networkidle",
                            timeout=nav_timeout_ms,
                        )
                    except Exception as goto_exc:
                        logger.debug(
                            "--- [%s] networkidle timeout for %s (%s); retrying with load",
                            method_label,
                            url,
                            goto_exc,
                        )
                        await page.goto(
                            url,
                            wait_until="load",
                            timeout=nav_timeout_ms,
                        )

                    for selector in ("main", "article", "section", "[role='main']"):
                        try:
                            await page.wait_for_selector(selector, timeout=2500)
                            break
                        except Exception:
                            continue

                    await page.wait_for_timeout(3500)
                    await page.evaluate(
                        """
                        async () => {
                            const maxHeight = Math.max(
                                document.body?.scrollHeight || 0,
                                document.documentElement?.scrollHeight || 0
                            );
                            const step = Math.max(window.innerHeight * 0.75, 300);
                            let y = 0;
                            while (y < maxHeight) {
                                y += step;
                                window.scrollTo(0, Math.min(y, maxHeight));
                                await new Promise((resolve) => setTimeout(resolve, 220));
                            }
                            window.scrollTo(0, 0);
                        }
                        """
                    )
                    await page.wait_for_timeout(1400)
                else:
                    await page.goto(
                        url,
                        wait_until="load",
                        timeout=nav_timeout_ms,
                    )
                    await page.wait_for_timeout(4000)

                # Simulate human behaviour (mouse movement + scroll) to pass
                # behavioural-signal checks used by Akamai Bot Manager and
                # PerimeterX after the page loads.
                await _human_behaviour(page)

                html = await page.content()
                title = await page.title()
                # Capture body innerText from the live DOM BEFORE closing the
                # browser.  When concurrent Playwright instances run together,
                # trafilatura/readability may return thin content (<200 chars)
                # because they parse the serialized HTML which can still have
                # partially-hydrated SPA sections.  The live DOM innerText is
                # always the fully-rendered text and is a reliable fallback.
                try:
                    body_inner_text = await page.inner_text("body")
                except Exception:
                    body_inner_text = ""
                _raise_for_protection_interstitial(
                    url,
                    "playwright_strict" if strict else "playwright",
                    html,
                    200,
                )
            finally:
                await browser.close()

            text = _extract_text_cascade(html, url)
            # If the HTML parsing cascade returned thin content but the live
            # DOM captured richer text, use the DOM capture instead.
            if len(text) < 200 and len(body_inner_text) > len(text):
                logger.debug(
                    "--- [%s] HTML cascade thin (%d chars), using DOM innerText (%d chars) for %s",
                    method_label,
                    len(text),
                    len(body_inner_text),
                    url,
                )
                text = body_inner_text

            elapsed = time.perf_counter() - start
            logger.info(
                "<<< [%s] SUCCESS: %s (status=200, text_len=%d, %.2fs)",
                method_label,
                url,
                len(text),
                elapsed,
            )

            return {
                "url": url,
                "html": html,
                "text": text,
                "title": title or _extract_title(html),
                "status": 200,
                "method": "playwright_strict" if strict else "playwright",
                "error": None,
            }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.error(
            "xxx [%s] FAILED: %s - %s (%.2fs)", method_label, url, exc, elapsed,
        )
        return {
            "url": url,
            "html": "",
            "text": "",
            "title": "",
            "status": 0,
            "method": "playwright_strict" if strict else "playwright",
            "error": str(exc),
        }


async def _crawl_with_camoufox(url: str) -> dict:
    """Camoufox fallback — hardened Firefox for Tier 4 anti-bot protection.

    Handles Akamai Bot Manager, PerimeterX, DataDome, and other fingerprint-
    based protections that defeat vanilla Playwright/Chromium.

    Camoufox is a custom Firefox build that:
    - Spoofs canvas, WebGL, audio, font, and WebRTC fingerprints
    - Patches navigator properties (platform, hardwareConcurrency, UA)
    - Removes all headless browser tells
    - Uses Firefox's TLS stack (harder to fingerprint than Chromium)

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    if not _CAMOUFOX_AVAILABLE:
        raise RuntimeError("camoufox not installed — run: pip install camoufox && python -m camoufox fetch")

    logger.info(">>> [CAMOUFOX] Attempting: %s", url)
    start = time.perf_counter()

    try:
        # Build Camoufox kwargs.  A residential proxy routes traffic through a
        # real ISP address so Akamai's IP-reputation check sees a non-datacenter
        # block.  Camoufox accepts the same proxy dict Playwright uses.
        camoufox_kwargs: dict = {"headless": True, "block_images": True}
        if settings.residential_proxy_url:
            camoufox_kwargs["proxy"] = {"server": settings.residential_proxy_url}
            logger.info(
                "--- [CAMOUFOX] Using residential proxy: %s",
                settings.residential_proxy_url.split("@")[-1],  # hide credentials
            )

        async with AsyncCamoufox(**camoufox_kwargs) as browser:
            page = await browser.new_page()
            response = await page.goto(url, wait_until="load", timeout=60000)
            await page.wait_for_timeout(3000)

            # Simulate human behaviour (mouse movement + scroll) after page load
            # to pass Akamai/PerimeterX behavioural signal checks.
            await _human_behaviour(page)

            html = await page.content()
            title = await page.title()
            try:
                body_inner_text = await page.inner_text("body")
            except Exception:
                body_inner_text = ""

        status = response.status if response else 200
        _raise_for_protection_interstitial(url, "camoufox", html, status)

        text = _extract_text_cascade(html, url)
        if len(text) < 200 and len(body_inner_text) > len(text):
            logger.debug(
                "--- [CAMOUFOX] HTML cascade thin (%d chars), using DOM innerText (%d chars)",
                len(text), len(body_inner_text),
            )
            text = body_inner_text

        elapsed = time.perf_counter() - start
        logger.info(
            "<<< [CAMOUFOX] SUCCESS: %s (status=%d, text_len=%d, %.2fs)",
            url, status, len(text), elapsed,
        )
        return {
            "url": url,
            "html": html,
            "text": text,
            "title": title or _extract_title(html),
            "status": status,
            "method": "camoufox",
            "error": None,
        }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.error("xxx [CAMOUFOX] FAILED: %s - %s (%.2fs)", url, exc, elapsed)
        return {
            "url": url,
            "html": "",
            "text": "",
            "title": "",
            "status": 0,
            "method": "camoufox",
            "error": str(exc),
        }


async def _crawl_with_bright_data(url: str) -> dict:
    """Step 8: Bright Data Scraping Browser — Tier 5 (CAPTCHA + IP reputation).

    Bright Data's Scraping Browser is a managed cloud browser running on a
    residential IP with built-in unblocking.  It handles:
      - IP reputation (residential IPs, automatic rotation)
      - Behavioural signals (Bright Data injects mouse/scroll simulation)
      - CAPTCHA solving (automatic via Bright Data's solver)
      - Akamai, PerimeterX, DataDome (full managed bypass)

    Only attempted when BRIGHT_DATA_WS_URL is set in .env.

    Connection: Playwright CDP → Bright Data's cloud Chromium instance.
    Credentials: set BRIGHT_DATA_WS_URL in .env.

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    ws_url = settings.bright_data_ws_url
    if not ws_url:
        raise RuntimeError("BRIGHT_DATA_WS_URL not configured — skipping Bright Data step")

    logger.info(">>> [BRIGHT-DATA] Attempting: %s", url)
    start = time.perf_counter()

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            # Connect to Bright Data's managed cloud browser via CDP WebSocket.
            # The cloud browser runs on a residential IP — no datacenter block.
            browser = await p.chromium.connect_over_cdp(ws_url)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                # Bright Data's unlocker may take a few extra seconds to solve
                # challenges — wait for the page to settle.
                await page.wait_for_timeout(5000)

                # Additional human-behaviour simulation on top of Bright Data's
                # built-in signals for maximum score reduction.
                await _human_behaviour(page)

                html = await page.content()
                title = await page.title()
                try:
                    body_inner_text = await page.inner_text("body")
                except Exception:
                    body_inner_text = ""

                _raise_for_protection_interstitial(url, "bright_data", html, 200)
            finally:
                await browser.close()

        text = _extract_text_cascade(html, url)
        if len(text) < 200 and len(body_inner_text) > len(text):
            logger.debug(
                "--- [BRIGHT-DATA] HTML cascade thin (%d chars), using DOM innerText (%d chars)",
                len(text), len(body_inner_text),
            )
            text = body_inner_text

        elapsed = time.perf_counter() - start
        logger.info(
            "<<< [BRIGHT-DATA] SUCCESS: %s (text_len=%d, %.2fs)",
            url, len(text), elapsed,
        )
        return {
            "url": url,
            "html": html,
            "text": text,
            "title": title or _extract_title(html),
            "status": 200,
            "method": "bright_data",
            "error": None,
        }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.error("xxx [BRIGHT-DATA] FAILED: %s - %s (%.2fs)", url, exc, elapsed)
        raise


# ── Public API ───────────────────────────────────────────────────────────


async def crawl_page(url: str, *, strict_mode: bool = False) -> dict:
    """Crawl a single page, cascading through methods from fastest to heaviest.

    Cascade order:
      PRIMARY (Scrapling):
        Step 1: Scrapling Fetcher (fast HTTP + TLS impersonation)
                -- if fails OR JS rendering needed -> Step 2
        Step 2: Scrapling StealthyFetcher (headless stealth browser)
                -- if fails -> Step 3
        Step 3: Scrapling DynamicFetcher (full Playwright via Scrapling)
                -- if fails -> Step 4

      FALLBACK (existing tools):
        Step 4: protection_handler (httpx -> curl_cffi -> cloudscraper)
                -- if fails OR JS rendering needed -> Step 5
        Step 5: Crawl4AI AsyncWebCrawler
                -- if fails -> Step 6
        Step 6: Manual Playwright with stealth + proxy + behavioural signals
                -- if fails or thin -> Step 7
        Step 7: Camoufox hardened Firefox + proxy + behavioural signals
                -- if fails and BRIGHT_DATA_WS_URL set -> Step 8
        Step 8: Bright Data Scraping Browser (residential IP + managed unblock)

    Returns:
        dict with keys: url, html, text, title, status, method, error
    """
    overall_start = time.perf_counter()
    logger.info("=== CRAWL START: %s", url)

    if strict_mode:
        strict_result = await _crawl_with_playwright(url, strict=True)
        elapsed = time.perf_counter() - overall_start
        logger.info(
            "=== CRAWL RESULT: %s | method=%s | strict_mode=1 | status=%d | text_len=%d | total=%.2fs",
            url,
            strict_result.get("method", "playwright_strict"),
            strict_result.get("status", 0),
            len(strict_result.get("text", "")),
            elapsed,
        )
        return strict_result

    # ------------------------------------------------------------------
    # Step 1: Scrapling Fetcher (fast HTTP)
    # ------------------------------------------------------------------
    if _SCRAPLING_AVAILABLE:
        try:
            result = await _crawl_scrapling_fast(url)
            html = result.get("html", "")

            # Check if the page actually needs JS rendering
            if needs_js_rendering(html):
                logger.info(
                    "--- [SCRAPLING-FAST] JS rendering needed for %s, escalating to StealthyFetcher",
                    url,
                )
                # Fall through to Step 2
            else:
                # Success -- static content extracted
                elapsed = time.perf_counter() - overall_start
                logger.info(
                    "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
                    url, result["method"], result["status"],
                    len(result.get("text", "")),
                    (result.get("title", "") or "")[:50],
                    elapsed,
                )
                return result
        except Exception:
            pass  # Already logged inside _crawl_scrapling_fast
    else:
        logger.info("--- [SCRAPLING] Not installed, skipping to fallback methods")

    # ------------------------------------------------------------------
    # Step 2: Scrapling StealthyFetcher (stealth headless browser)
    # ------------------------------------------------------------------
    if _SCRAPLING_AVAILABLE:
        try:
            result = await _crawl_scrapling_stealth(url)
            html = result.get("html", "")
            text = result.get("text", "")

            # Apply the same thin-content check used between Fast→Stealth.
            # StealthyFetcher can return status=200 with only a shell HTML
            # (e.g. React SPA that hasn't hydrated yet), giving ~72 chars of
            # extracted text.  When that happens, fall through to
            # DynamicFetcher which waits for JS hydration before extracting.
            if needs_js_rendering(html) or len(text) < 200:
                logger.info(
                    "--- [SCRAPLING-STEALTH] Content still thin for %s "
                    "(text_len=%d, js_needed=%s) — escalating to DynamicFetcher",
                    url,
                    len(text),
                    needs_js_rendering(html),
                )
                # Fall through to Step 3
            else:
                elapsed = time.perf_counter() - overall_start
                logger.info(
                    "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
                    url, result["method"], result["status"],
                    len(text),
                    (result.get("title", "") or "")[:50],
                    elapsed,
                )
                return result
        except Exception:
            pass  # Already logged inside _crawl_scrapling_stealth

    # ------------------------------------------------------------------
    # Step 3: Scrapling DynamicFetcher (full Playwright via Scrapling)
    # ------------------------------------------------------------------
    if _SCRAPLING_AVAILABLE:
        try:
            result = await _crawl_scrapling_dynamic(url)
            html = result.get("html", "")
            text = result.get("text", "")

            # Scrapling's DynamicFetcher wait_selector param is deprecated in
            # v0.3+ and silently ignored, so it may still return a partially-
            # rendered SPA shell.  Apply the same thin-content guard used at
            # Step 2: if content is still thin, fall through to the direct
            # Playwright step (Step 6) which uses wait_until="networkidle" and
            # a post-hydration pause — reliably capturing React/Vue/Next output.
            if needs_js_rendering(html) or len(text) < 200:
                logger.info(
                    "--- [SCRAPLING-DYNAMIC] Content still thin for %s "
                    "(text_len=%d, js_needed=%s) — falling through to Playwright",
                    url,
                    len(text),
                    needs_js_rendering(html),
                )
                # Fall through to Steps 4 → 5 → 6
            else:
                elapsed = time.perf_counter() - overall_start
                logger.info(
                    "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
                    url, result["method"], result["status"],
                    len(text),
                    (result.get("title", "") or "")[:50],
                    elapsed,
                )
                return result
        except Exception:
            pass  # Already logged inside _crawl_scrapling_dynamic

    # ------------------------------------------------------------------
    # Step 4: protection_handler (httpx -> curl_cffi -> cloudscraper)
    # ------------------------------------------------------------------
    try:
        result = await _crawl_with_protection_handler(url)
        html = result.get("html", "")

        # Check if the page needs JS rendering
        if needs_js_rendering(html):
            logger.info(
                "--- [PROTECTION-HANDLER] JS rendering needed for %s, escalating to Crawl4AI",
                url,
            )
            # Fall through to Step 5
        else:
            elapsed = time.perf_counter() - overall_start
            logger.info(
                "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
                url, result["method"], result["status"],
                len(result.get("text", "")),
                (result.get("title", "") or "")[:50],
                elapsed,
            )
            return result
    except Exception:
        pass  # Already logged inside _crawl_with_protection_handler

    # ------------------------------------------------------------------
    # Step 5: Crawl4AI AsyncWebCrawler
    # ------------------------------------------------------------------
    try:
        result = await _crawl_with_browser(url)

        elapsed = time.perf_counter() - overall_start
        logger.info(
            "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
            url, result["method"], result["status"],
            len(result.get("text", "")),
            (result.get("title", "") or "")[:50],
            elapsed,
        )
        return result
    except Exception:
        pass  # Already logged inside _crawl_with_browser

    # ------------------------------------------------------------------
    # Step 6: Manual Playwright (stealth Chromium)
    # ------------------------------------------------------------------
    result = await _crawl_with_playwright(url)

    if not result.get("error") and len(result.get("text", "")) >= 200:
        elapsed = time.perf_counter() - overall_start
        logger.info(
            "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
            url, result["method"], result["status"],
            len(result.get("text", "")),
            (result.get("title", "") or "")[:50],
            elapsed,
        )
        return result

    # ------------------------------------------------------------------
    # Step 7: Camoufox (hardened Firefox — Tier 4 anti-bot last resort)
    # Handles Akamai Bot Manager, PerimeterX, DataDome.
    # Only attempted when all Chromium-based methods have failed or
    # returned thin content, indicating active fingerprint blocking.
    # ------------------------------------------------------------------
    if _CAMOUFOX_AVAILABLE:
        try:
            camoufox_result = await _crawl_with_camoufox(url)
            elapsed = time.perf_counter() - overall_start
            if camoufox_result.get("error"):
                logger.warning(
                    "xxx [CAMOUFOX] FAILED: %s | error=%s | total=%.2fs",
                    url, camoufox_result["error"], elapsed,
                )
            else:
                logger.info(
                    "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
                    url, camoufox_result["method"], camoufox_result["status"],
                    len(camoufox_result.get("text", "")),
                    (camoufox_result.get("title", "") or "")[:50],
                    elapsed,
                )
                return camoufox_result
        except Exception:
            pass
    else:
        logger.warning(
            "xxx [CAMOUFOX] NOT AVAILABLE: %s - camoufox not installed. "
            "Run: pip install camoufox && python -m camoufox fetch",
            url,
        )

    # ------------------------------------------------------------------
    # Step 8: Bright Data Scraping Browser (residential IP + managed unblock)
    # Only attempted when BRIGHT_DATA_WS_URL is configured.
    # Handles Tier 5: CAPTCHA + IP reputation + behavioural signals via
    # Bright Data's cloud infrastructure.
    # ------------------------------------------------------------------
    if settings.bright_data_ws_url:
        try:
            bd_result = await _crawl_with_bright_data(url)
            elapsed = time.perf_counter() - overall_start
            if not bd_result.get("error") and len(bd_result.get("text", "")) >= 100:
                logger.info(
                    "=== CRAWL RESULT: %s | method=%s | status=%d | text_len=%d | title=%r | total=%.2fs",
                    url, bd_result["method"], bd_result["status"],
                    len(bd_result.get("text", "")),
                    (bd_result.get("title", "") or "")[:50],
                    elapsed,
                )
                return bd_result
            else:
                logger.warning(
                    "xxx [BRIGHT-DATA] FAILED or thin: %s | error=%s | total=%.2fs",
                    url, bd_result.get("error"), elapsed,
                )
        except Exception:
            pass  # Already logged inside _crawl_with_bright_data
    else:
        logger.debug("--- [BRIGHT-DATA] Not configured (BRIGHT_DATA_WS_URL not set), skipping")

    # All steps exhausted — return the last Playwright result (even if thin/failed)
    elapsed = time.perf_counter() - overall_start
    logger.warning(
        "=== CRAWL RESULT: %s | method=%s | ALL METHODS EXHAUSTED | text_len=%d | total=%.2fs",
        url, result.get("method", "unknown"), len(result.get("text", "")), elapsed,
    )
    return result


# ── Batch Crawling ───────────────────────────────────────────────────────

ProgressCallback = Callable[[int, int], Coroutine[Any, Any, None]] | Callable[[int, int], None] | None


async def crawl_pages_batch(
    urls: list[str],
    max_concurrent: int | None = None,
    delay: float | None = None,
    progress_callback: ProgressCallback = None,
) -> list[dict]:
    """Crawl multiple pages with concurrency control and polite delays.

    Args:
        urls: List of URLs to crawl.
        max_concurrent: Maximum number of simultaneous crawls.
        delay: Seconds to wait between starting each crawl.
        progress_callback: Optional async/sync callable(crawled_count, total_count).

    Returns:
        List of crawl result dicts (same shape as crawl_page output).
    """
    if max_concurrent is None:
        max_concurrent = settings.max_concurrent_crawls
    if delay is None:
        delay = settings.crawl_delay_seconds

    total = len(urls)
    if total == 0:
        return []

    logger.info(
        "=== BATCH START: %d URLs, concurrency=%d, delay=%.1fs",
        total, max_concurrent, delay,
    )

    semaphore = asyncio.Semaphore(max_concurrent)
    results: list[dict] = []
    crawled_count = 0
    succeeded_count = 0
    lock = asyncio.Lock()

    async def _crawl_one(url: str) -> dict:
        nonlocal crawled_count, succeeded_count
        async with semaphore:
            result = await crawl_page(url)
            await asyncio.sleep(delay)

            async with lock:
                crawled_count += 1
                if not result.get("error"):
                    succeeded_count += 1

                logger.info(
                    "--- BATCH PROGRESS: %d/%d crawled (%.0f%%)",
                    crawled_count, total,
                    crawled_count / total * 100,
                )

                if progress_callback is not None:
                    try:
                        ret = progress_callback(crawled_count, total)
                        if asyncio.iscoroutine(ret):
                            await ret
                    except Exception as cb_exc:
                        logger.warning("Progress callback error: %s", cb_exc)

            return result

    tasks = [asyncio.create_task(_crawl_one(url)) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    # Ensure we always return a list of dicts (handle any stray exceptions)
    clean_results: list[dict] = []
    for i, r in enumerate(results):
        if isinstance(r, dict):
            clean_results.append(r)
        else:
            logger.error("Task %d returned non-dict: %s", i, r)
            clean_results.append({
                "url": urls[i],
                "html": "",
                "text": "",
                "title": "",
                "status": 0,
                "method": "error",
                "error": str(r),
            })

    if settings.enable_completeness_gate:
        features_by_page_type: dict[str, list[dict[str, int]]] = defaultdict(list)
        evaluated_pages: list[dict[str, Any]] = []

        for index, result in enumerate(clean_results):
            if result.get("error") or not result.get("html"):
                continue

            page_url = str(result.get("url") or urls[index])
            page_type = _classify_page_type(page_url)
            features = _extract_completeness_features(result)
            features_by_page_type[page_type].append(features)
            evaluated_pages.append(
                {
                    "index": index,
                    "url": page_url,
                    "page_type": page_type,
                    "features": features,
                }
            )

        thresholds_by_page_type = _build_dynamic_thresholds(features_by_page_type)

        recrawl_candidates: list[dict[str, Any]] = []
        for item in evaluated_pages:
            page_type = item["page_type"]
            thresholds = thresholds_by_page_type.get(
                page_type,
                _PAGE_TYPE_COMPLETENESS_BASELINES.get(page_type, _DEFAULT_COMPLETENESS_BASELINE),
            )
            score, reasons = _evaluate_completeness(item["features"], thresholds)

            result = clean_results[item["index"]]
            result["page_type"] = page_type
            result["completeness"] = {
                "score": round(score, 3),
                "thresholds": dict(thresholds),
                "features": dict(item["features"]),
                "reasons": reasons,
            }

            if score < settings.completeness_min_score and reasons:
                recrawl_candidates.append(
                    {
                        **item,
                        "score": score,
                        "reasons": reasons,
                        "thresholds": dict(thresholds),
                    }
                )

        recrawl_candidates.sort(key=lambda candidate: candidate["score"])
        max_recrawls = max(0, settings.completeness_max_recrawls_per_batch)
        if max_recrawls:
            recrawl_candidates = recrawl_candidates[:max_recrawls]
        else:
            recrawl_candidates = []

        if recrawl_candidates:
            recrawl_concurrency = max(
                1,
                min(
                    max_concurrent,
                    settings.completeness_recrawl_concurrency,
                ),
            )
            strict_semaphore = asyncio.Semaphore(recrawl_concurrency)
            logger.info(
                "--- COMPLETENESS GATE: strict recrawl for %d/%d pages (min_score=%.2f, concurrency=%d)",
                len(recrawl_candidates),
                len(evaluated_pages),
                settings.completeness_min_score,
                recrawl_concurrency,
            )

            async def _strict_recrawl(candidate: dict[str, Any]) -> None:
                index = int(candidate["index"])
                page_url = str(candidate["url"])

                async with strict_semaphore:
                    strict_result = await crawl_page(page_url, strict_mode=True)

                existing_result = clean_results[index]
                if strict_result.get("error") or not strict_result.get("html"):
                    existing_result.setdefault("completeness", {})["strict_recrawl_attempted"] = True
                    return

                strict_features = _extract_completeness_features(strict_result)
                strict_score, strict_reasons = _evaluate_completeness(
                    strict_features,
                    candidate["thresholds"],
                )

                old_features = candidate["features"]
                old_score = float(candidate["score"])
                improved = (
                    (strict_score > old_score + 0.04)
                    or (strict_features["text_len"] > old_features["text_len"] + 140)
                )

                if improved:
                    strict_result["page_type"] = candidate["page_type"]
                    strict_result["completeness"] = {
                        "score": round(strict_score, 3),
                        "thresholds": dict(candidate["thresholds"]),
                        "features": dict(strict_features),
                        "reasons": strict_reasons,
                        "strict_recrawl_attempted": True,
                        "recovered_via_strict": True,
                        "previous_score": round(old_score, 3),
                    }
                    clean_results[index] = strict_result
                    logger.info(
                        "--- COMPLETENESS GATE: improved %s via strict recrawl (score %.3f -> %.3f, text %d -> %d)",
                        page_url,
                        old_score,
                        strict_score,
                        old_features["text_len"],
                        strict_features["text_len"],
                    )
                else:
                    existing_result.setdefault("completeness", {})["strict_recrawl_attempted"] = True
                    existing_result["completeness"]["strict_score"] = round(strict_score, 3)

            await asyncio.gather(*(_strict_recrawl(candidate) for candidate in recrawl_candidates))

    final_succeeded = sum(1 for r in clean_results if not r.get("error"))
    final_failed = total - final_succeeded

    logger.info(
        "=== BATCH COMPLETE: %d/%d succeeded, %d failed",
        final_succeeded, total, final_failed,
    )

    return clean_results
