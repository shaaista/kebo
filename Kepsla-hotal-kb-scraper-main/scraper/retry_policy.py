"""Per-site retry and backoff policy helpers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urlparse

from config.settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableHttpError(RuntimeError):
    """Exception that carries an HTTP status code for retry policy decisions."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Resolved retry policy for a target host."""

    host_pattern: str
    attempts: int
    backoff_seconds: tuple[float, ...]
    retry_statuses: tuple[int, ...]


def get_retry_policy_for_url(url: str) -> RetryPolicy:
    """Return the configured retry policy for a URL."""
    host = (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    policies = settings.site_retry_policies
    matched_pattern = "*"
    matched_policy = policies.get("*", {})

    for host_pattern, policy in policies.items():
        if host_pattern == "*":
            continue
        normalized_pattern = host_pattern.lower()
        if host == normalized_pattern or host.endswith(f".{normalized_pattern}"):
            matched_pattern = normalized_pattern
            matched_policy = policy
            break

    attempts = int(matched_policy.get("attempts", 2))
    backoff = tuple(float(value) for value in matched_policy.get("backoff_seconds", (0.5, 1.5)))
    retry_statuses = tuple(int(value) for value in matched_policy.get("retry_statuses", (429, 500, 502, 503, 504)))
    return RetryPolicy(
        host_pattern=matched_pattern,
        attempts=max(1, attempts),
        backoff_seconds=backoff or (0.5,),
        retry_statuses=retry_statuses,
    )


def _is_retryable_exception(exc: Exception, policy: RetryPolicy) -> bool:
    if isinstance(exc, RetryableHttpError):
        return exc.status_code in policy.retry_statuses
    return True


async def run_with_retry(
    url: str,
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    label: str = "operation",
) -> T:
    """Run an async operation with a host-specific retry policy."""
    effective_policy = policy or get_retry_policy_for_url(url)
    last_error: Exception | None = None

    for attempt in range(1, effective_policy.attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            last_error = exc
            if not _is_retryable_exception(exc, effective_policy) or attempt >= effective_policy.attempts:
                raise

            delay_index = min(attempt - 1, len(effective_policy.backoff_seconds) - 1)
            delay = effective_policy.backoff_seconds[delay_index]
            logger.warning(
                "Retrying %s for %s after %s attempt %d/%d failed: %s",
                label,
                url,
                delay,
                attempt,
                effective_policy.attempts,
                exc,
            )
            await sleep(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Retry loop for {label} on {url} exhausted unexpectedly")

