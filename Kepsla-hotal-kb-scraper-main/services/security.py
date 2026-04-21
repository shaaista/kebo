"""Authentication and rate-limiting helpers."""

from __future__ import annotations

import base64
import binascii
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import Request

from config.settings import settings


class InMemoryRateLimiter:
    """Simple per-client sliding-window rate limiter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, Deque[float]] = defaultdict(deque)

    def allow_request(
        self,
        client_id: str,
        *,
        limit: int | None = None,
        window_seconds: int | None = None,
        now: float | None = None,
    ) -> bool:
        """Return ``True`` when the request should be allowed."""
        effective_limit = limit if limit is not None else settings.api_rate_limit_requests
        effective_window = (
            window_seconds
            if window_seconds is not None
            else settings.api_rate_limit_window_seconds
        )
        timestamp = time.time() if now is None else now

        with self._lock:
            bucket = self._events[client_id]
            cutoff = timestamp - effective_window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= max(1, effective_limit):
                return False

            bucket.append(timestamp)
            return True

    def reset(self) -> None:
        """Clear all recorded request history."""
        with self._lock:
            self._events.clear()


def has_valid_basic_auth(authorization_header: str | None) -> bool:
    """Validate an incoming Basic Auth header."""
    if not settings.api_basic_auth_enabled:
        return True

    if not authorization_header or not authorization_header.startswith("Basic "):
        return False

    token = authorization_header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(token).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False

    if ":" not in decoded:
        return False

    username, password = decoded.split(":", 1)
    return secrets.compare_digest(username, settings.api_basic_auth_username) and secrets.compare_digest(
        password,
        settings.api_basic_auth_password,
    )


def get_client_identifier(request: Request) -> str:
    """Return the best available client identifier for rate limiting."""
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


rate_limiter = InMemoryRateLimiter()
