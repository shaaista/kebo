"""
Backend Trace Service

Writes structured JSONL events for request and turn-level backend debugging.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings


class BackendTraceService:
    """Best-effort append-only logger for backend runtime tracing."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "backend_trace_enabled", True))
        self.log_file = Path(
            str(getattr(settings, "backend_trace_log_file", "./logs/backend_trace.jsonl"))
        )
        self.max_chars = max(200, int(getattr(settings, "backend_trace_max_chars", 2000) or 2000))
        self._lock = threading.Lock()

    def _truncate_text(self, value: str) -> str:
        text = str(value or "")
        if len(text) <= self.max_chars:
            return text
        omitted = len(text) - self.max_chars
        return f"{text[:self.max_chars]}...[TRUNCATED:{omitted}]"

    def _sanitize(self, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return self._truncate_text(str(value))
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self._truncate_text(value)
        if isinstance(value, dict):
            return {
                str(k): self._sanitize(v, depth + 1)
                for k, v in list(value.items())[:300]
            }
        if isinstance(value, list):
            return [self._sanitize(v, depth + 1) for v in value[:300]]
        if isinstance(value, tuple):
            return [self._sanitize(v, depth + 1) for v in value[:300]]
        return self._truncate_text(str(value))

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
        error: str = "",
    ) -> None:
        if not self.enabled:
            return

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": str(event or "").strip() or "backend_event",
            "trace_id": str(trace_id or "").strip(),
            "turn_trace_id": str(turn_trace_id or "").strip(),
            "session_id": str(session_id or "").strip(),
            "hotel_code": str(hotel_code or "").strip(),
            "channel": str(channel or "").strip(),
            "endpoint": str(endpoint or "").strip(),
            "method": str(method or "").strip(),
            "status_code": status_code,
            "component": str(component or "").strip(),
            "error": self._truncate_text(str(error or "").strip()),
            "payload": self._sanitize(payload or {}),
        }
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(record, ensure_ascii=False)
            with self._lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(encoded + "\n")
        except Exception:
            return


backend_trace_service = BackendTraceService()
