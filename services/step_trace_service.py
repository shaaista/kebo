"""
Step Trace Service

Append-only structured step logger used to trace request execution paths
across gateway, admin, chat, and ticketing flows.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings


class StepTraceService:
    """Best-effort structured step logger."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "step_trace_logging_enabled", True))
        self.log_file = Path(str(getattr(settings, "step_trace_log_file", "./logs/step_trace.jsonl")))
        self.max_chars = max(1000, int(getattr(settings, "step_trace_max_chars", 25000) or 25000))
        self._lock = threading.Lock()

    def _truncate(self, value: str) -> str:
        text = str(value or "")
        if len(text) <= self.max_chars:
            return text
        omitted = len(text) - self.max_chars
        return f"{text[:self.max_chars]}...[TRUNCATED:{omitted}]"

    def _sanitize(self, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return self._truncate(str(value))
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self._truncate(value)
        if isinstance(value, dict):
            return {
                str(k): self._sanitize(v, depth + 1)
                for k, v in list(value.items())[:400]
            }
        if isinstance(value, list):
            return [self._sanitize(v, depth + 1) for v in value[:400]]
        if isinstance(value, tuple):
            return [self._sanitize(v, depth + 1) for v in value[:400]]
        return self._truncate(str(value))

    @staticmethod
    def _clean(value: Any) -> str:
        return str(value or "").strip()

    def log_event(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        trace_id: str = "",
        turn_trace_id: str = "",
        session_id: str = "",
        hotel_code: str = "",
        channel: str = "",
        endpoint: str = "",
        method: str = "",
        status_code: int | None = None,
        component: str = "",
        step: str = "",
        stage: str = "",
        error: str = "",
    ) -> None:
        if not self.enabled:
            return

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": self._clean(event) or "step_event",
            "trace_id": self._clean(trace_id),
            "turn_trace_id": self._clean(turn_trace_id),
            "session_id": self._clean(session_id),
            "hotel_code": self._clean(hotel_code),
            "channel": self._clean(channel),
            "endpoint": self._clean(endpoint),
            "method": self._clean(method).upper(),
            "status_code": status_code,
            "component": self._clean(component),
            "step": self._clean(step),
            "stage": self._clean(stage),
            "error": self._truncate(self._clean(error)),
            "payload": self._sanitize(payload or {}),
        }
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(record, ensure_ascii=False, default=str)
            with self._lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(encoded + "\n")
        except Exception:
            return


step_trace_service = StepTraceService()

