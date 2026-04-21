"""Handle Cloudflare, bot detection, and CAPTCHAs with free tools.

Cascading fetch strategy (ordered from fastest/lightest to heaviest):

  1. Scrapling Fetcher     -- fast HTTP with TLS fingerprint impersonation
  2. Scrapling StealthyFetcher -- headless stealth browser + Cloudflare solver
  3. httpx                 -- async HTTP/2 with browser-like headers
  4. curl_cffi             -- Chrome TLS impersonation via libcurl
  5. cloudscraper           -- JS challenge solver (synchronous, run in executor)

Each method is attempted in order. On success the result is returned
immediately. On failure the next method is tried. A final summary log
line is emitted regardless of outcome so every fetch is fully traceable.

If the ``scrapling`` package is not installed, steps 1 and 2 are
silently skipped and the cascade falls through to the remaining methods.
"""

import asyncio
import logging
import random
import time

import httpx

from scraper.retry_policy import RetryableHttpError, run_with_retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Scrapling imports -- graceful degradation when not installed
# ---------------------------------------------------------------------------

_SCRAPLING_AVAILABLE = False
try:
    from scrapling import Fetcher as ScraplingFetcher
    from scrapling import StealthyFetcher as ScraplingStealthyFetcher

    _SCRAPLING_AVAILABLE = True
    logger.debug("scrapling package detected -- Fetcher and StealthyFetcher available")
except ImportError:
    ScraplingFetcher = None  # type: ignore[assignment,misc]
    ScraplingStealthyFetcher = None  # type: ignore[assignment,misc]
    logger.debug("scrapling package not installed -- scrapling methods will be skipped")


# ── Realistic User-Agent Rotation ────────────────────────────────────────────

_USER_AGENTS = [
    # Chrome 124 -- Windows 10
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # Chrome 123 -- macOS
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    # Firefox 125 -- Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
    # Edge 124 -- Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
    # Safari 17 -- macOS
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
    # Chrome 124 -- Linux
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
]


def get_random_headers() -> dict:
    """Return realistic browser headers with a random User-Agent."""
    ua = random.choice(_USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


# ── Cloudflare Detection ────────────────────────────────────────────────────


def detect_protection_interstitial(html: str, status: int) -> str | None:
    """Return a human-readable reason when a response is a bot-protection page."""
    if not html:
        return None

    html_lower = html.lower()

    cloudflare_markers = [
        "cf-browser-verification",
        "challenge-platform",
        "cf-challenge",
        "cf_chl_opt",
        "jschl-answer",
        "jschl_vc",
        "cf-turnstile",
        "checking your browser",
        "just a moment...",
        "enable javascript and cookies to continue",
        "attention required! | cloudflare",
        "ray id",
    ]
    if status in (403, 429, 503) and any(marker in html_lower for marker in cloudflare_markers):
        return "cloudflare challenge"

    if "sec-if-cpt-container" in html_lower:
        return "akamai bot-protection interstitial"

    if "powered and protected by" in html_lower and (
        "akamai" in html_lower or "privacy" in html_lower
    ):
        return "akamai bot-protection interstitial"

    if "access restricted | radisson hotel group" in html_lower:
        return "radisson access-restricted interstitial"

    if "temporarily restricted" in html_lower and "automated detection" in html_lower:
        return "radisson access-restricted interstitial"

    if "app-403-landing-page" in html_lower and "access restricted" in html_lower:
        return "radisson access-restricted interstitial"

    return None


def is_cloudflare_challenge(html: str, status: int) -> bool:
    """Detect Cloudflare-specific challenge pages."""
    reason = detect_protection_interstitial(html, status)
    return bool(reason and reason.startswith("cloudflare"))


def _raise_for_retryable_status(url: str, status: int, method: str) -> None:
    """Raise a retryable error for transient upstream statuses."""
    if status in {429, 500, 502, 503, 504}:
        raise RetryableHttpError(status, f"{method} returned retryable status {status} for {url}")


# ── Method 1: Scrapling Fetcher (fast HTTP + TLS impersonation) ─────────────


async def _fetch_scrapling_fast(url: str, timeout: int) -> dict:
    """Fetch using Scrapling's Fetcher with stealthy_headers for TLS impersonation.

    Scrapling Fetcher is synchronous, so we run it inside a thread-pool
    executor to keep the event loop free.
    """
    if not _SCRAPLING_AVAILABLE:
        raise RuntimeError("scrapling package is not installed")

    def _sync_fetch() -> dict:
        fetcher = ScraplingFetcher()
        page = fetcher.get(url, stealthy_headers=True, timeout=timeout)
        html = str(page.html_content) if hasattr(page, "html_content") else ""
        status = page.status

        reason = detect_protection_interstitial(html, status)
        if reason:
            raise RuntimeError(f"{reason} detected (scrapling_fast)")
        _raise_for_retryable_status(url, status, "scrapling_fast")

        return {
            "html": html,
            "status": status,
            "method": "scrapling_fast",
            "error": None,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_fetch)


# ── Method 2: Scrapling StealthyFetcher (stealth browser + CF solver) ───────


async def _fetch_scrapling_stealth(url: str, timeout: int) -> dict:
    """Fetch using Scrapling's StealthyFetcher -- a headless stealth browser
    that can solve Cloudflare challenges automatically.

    Like the fast fetcher this is synchronous under the hood, so it is
    dispatched to a thread-pool executor.
    """
    if not _SCRAPLING_AVAILABLE:
        raise RuntimeError("scrapling package is not installed")

    def _sync_fetch() -> dict:
        fetcher = ScraplingStealthyFetcher()
        page = fetcher.fetch(url, timeout=timeout)
        html = str(page.html_content) if hasattr(page, "html_content") else ""
        status = page.status

        reason = detect_protection_interstitial(html, status)
        if reason:
            raise RuntimeError(f"{reason} detected (scrapling_stealth)")
        _raise_for_retryable_status(url, status, "scrapling_stealth")

        return {
            "html": html,
            "status": status,
            "method": "scrapling_stealth",
            "error": None,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_fetch)


# ── Method 3: httpx ─────────────────────────────────────────────────────────


async def _fetch_httpx(url: str, timeout: int) -> dict:
    """Fast async fetch with httpx and browser-like headers."""
    headers = get_random_headers()
    async with httpx.AsyncClient(
        follow_redirects=True,
        http2=True,
        timeout=httpx.Timeout(timeout=timeout),
    ) as client:
        resp = await client.get(url, headers=headers)
        html = resp.text

        reason = detect_protection_interstitial(html, resp.status_code)
        if reason:
            raise RuntimeError(f"{reason} detected (httpx)")
        _raise_for_retryable_status(url, resp.status_code, "httpx")

        return {
            "html": html,
            "status": resp.status_code,
            "method": "httpx",
            "error": None,
        }


# ── Method 4: curl_cffi ────────────────────────────────────────────────────


async def _fetch_curl_cffi(url: str, timeout: int) -> dict:
    """Fetch using curl_cffi with Chrome TLS impersonation (bypasses many CF checks)."""
    from curl_cffi.requests import AsyncSession as CurlSession

    headers = get_random_headers()
    async with CurlSession() as session:
        resp = await session.get(
            url,
            headers=headers,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )
        html = resp.text

        reason = detect_protection_interstitial(html, resp.status_code)
        if reason:
            raise RuntimeError(f"{reason} detected (curl_cffi)")
        _raise_for_retryable_status(url, resp.status_code, "curl_cffi")

        return {
            "html": html,
            "status": resp.status_code,
            "method": "curl_cffi",
            "error": None,
        }


# ── Method 5: cloudscraper ─────────────────────────────────────────────────


async def _fetch_cloudscraper(url: str, timeout: int) -> dict:
    """Fetch using cloudscraper which solves JS challenges.

    cloudscraper is synchronous, so we run it in an executor to avoid
    blocking the event loop.
    """
    import cloudscraper

    def _sync_fetch() -> dict:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        resp = scraper.get(url, timeout=timeout)
        html = resp.text

        reason = detect_protection_interstitial(html, resp.status_code)
        if reason:
            raise RuntimeError(f"{reason} detected (cloudscraper)")
        _raise_for_retryable_status(url, resp.status_code, "cloudscraper")

        return {
            "html": html,
            "status": resp.status_code,
            "method": "cloudscraper",
            "error": None,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_fetch)


# ── Public API ───────────────────────────────────────────────────────────────


async def _fetch_with_protection_once(url: str, timeout: int = 30) -> dict:
    """Fetch a URL once, cascading through increasingly aggressive methods.

    Cascade order:
      1. scrapling_fast     -- Scrapling Fetcher (TLS impersonation)
      2. scrapling_stealth  -- Scrapling StealthyFetcher (headless + CF solver)
      3. httpx              -- async HTTP/2 with browser headers
      4. curl_cffi          -- Chrome TLS impersonation via libcurl
      5. cloudscraper       -- JS challenge solver

    Returns:
        dict with keys: html, status, method, error
    """
    methods = [
        ("scrapling_fast", _fetch_scrapling_fast),
        ("scrapling_stealth", _fetch_scrapling_stealth),
        ("httpx", _fetch_httpx),
        ("curl_cffi", _fetch_curl_cffi),
        ("cloudscraper", _fetch_cloudscraper),
    ]

    total_methods = len(methods)
    last_error: str | None = None
    all_errors: list[str] = []
    attempt_number = 0

    for name, fetch_fn in methods:
        attempt_number += 1

        # --- Skip scrapling methods early if the package is missing ----------
        if name.startswith("scrapling_") and not _SCRAPLING_AVAILABLE:
            logger.info(
                "--- [%s] SKIPPED (scrapling not installed): %s [%d/%d]",
                name.upper(),
                url,
                attempt_number,
                total_methods,
            )
            continue

        logger.info(
            ">>> [%s] Attempting fetch: %s [%d/%d]",
            name.upper(),
            url,
            attempt_number,
            total_methods,
        )

        start = time.perf_counter()
        try:
            result = await fetch_fn(url, timeout)
            elapsed = time.perf_counter() - start

            html_len = len(result.get("html", ""))
            status = result.get("status", 0)

            logger.info(
                "<<< [%s] SUCCESS: %s (status=%d, html_len=%d, %.2fs)",
                name.upper(),
                url,
                status,
                html_len,
                elapsed,
            )
            logger.info(
                "=== FETCH SUMMARY: %s | method=%s | status=%d | attempts=%d/%d | elapsed=%.2fs",
                url,
                name,
                status,
                attempt_number,
                total_methods,
                elapsed,
            )
            return result

        except Exception as exc:
            elapsed = time.perf_counter() - start
            last_error = f"{name}: {exc}"
            all_errors.append(last_error)

            logger.warning(
                "xxx [%s] FAILED: %s - %s (%.2fs)",
                name.upper(),
                url,
                exc,
                elapsed,
            )
            continue

    # All methods exhausted ------------------------------------------------
    summary_error = next(
        (
            err
            for err in all_errors
            if "interstitial" in err.lower() or "challenge" in err.lower()
        ),
        last_error,
    )
    logger.warning(
        "xxx ALL METHODS EXHAUSTED for %s. Summary=%s | errors=%s",
        url,
        summary_error,
        " | ".join(all_errors) if all_errors else "none",
    )
    logger.info(
        "=== FETCH SUMMARY: %s | method=none | status=0 | attempts=%d/%d | result=FAILURE",
        url,
        attempt_number,
        total_methods,
    )

    raise RuntimeError(summary_error or "All fetch methods failed")


async def fetch_with_protection(url: str, timeout: int = 30) -> dict:
    """Fetch a URL with host-specific retry/backoff around the protection cascade."""

    async def _operation() -> dict:
        return await _fetch_with_protection_once(url, timeout)

    try:
        return await run_with_retry(url, _operation, label="protection_fetch")
    except Exception as exc:
        last_error = str(exc)
        logger.warning("xxx PROTECTION FETCH FAILED for %s after retries: %s", url, last_error)
        return {
            "html": "",
            "status": 0,
            "method": "none",
            "error": last_error or "All fetch methods failed",
        }
