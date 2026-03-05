"""
Gateway Service

API-gateway style controls for:
- API key authorization
- In-memory rate limiting
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Optional

from config.settings import settings


class GatewayService:
    """Simple in-process gateway policy helper."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _now() -> float:
        return time.time()

    def is_auth_required(self) -> bool:
        return bool(getattr(settings, "api_gateway_auth_enabled", False))

    def is_rate_limit_enabled(self) -> bool:
        return bool(getattr(settings, "api_gateway_rate_limit_enabled", True))

    def expected_api_key(self) -> str:
        return str(getattr(settings, "api_gateway_api_key", "") or "").strip()

    def is_authorized(self, provided_api_key: Optional[str]) -> bool:
        if not self.is_auth_required():
            return True
        expected = self.expected_api_key()
        if not expected:
            # Fail closed: auth is enabled but key is missing from config.
            return False
        candidate = str(provided_api_key or "").strip()
        return bool(candidate and candidate == expected)

    def _limit(self) -> int:
        return max(1, int(getattr(settings, "api_gateway_rate_limit_requests", 80)))

    def _window_seconds(self) -> int:
        return max(1, int(getattr(settings, "api_gateway_rate_limit_window_seconds", 60)))

    def allow_request(self, client_key: str) -> tuple[bool, int]:
        """
        Returns (allowed, retry_after_seconds).
        """
        if not self.is_rate_limit_enabled():
            return True, 0

        key = str(client_key or "").strip() or "unknown"
        now = self._now()
        window_seconds = self._window_seconds()
        limit = self._limit()
        cutoff = now - float(window_seconds)

        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = max(1, int(round(bucket[0] + window_seconds - now)))
                return False, retry_after

            bucket.append(now)
            return True, 0

    def snapshot_state(self) -> dict[str, int | bool]:
        """Expose lightweight gateway counters for admin diagnostics."""
        with self._lock:
            active_keys = 0
            total_hits = 0
            now = self._now()
            cutoff = now - float(self._window_seconds())
            for bucket in self._hits.values():
                # prune stale entries while measuring
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                if bucket:
                    active_keys += 1
                    total_hits += len(bucket)
        return {
            "auth_required": self.is_auth_required(),
            "rate_limit_enabled": self.is_rate_limit_enabled(),
            "rate_limit_requests": self._limit(),
            "rate_limit_window_seconds": self._window_seconds(),
            "active_rate_limit_keys": active_keys,
            "active_window_hits": total_hits,
        }


gateway_service = GatewayService()
