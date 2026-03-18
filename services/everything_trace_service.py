"""
Everything Trace Service

Single append-only JSONL log for deep backend debugging across
HTTP, chat pipeline, orchestration, and LLM calls.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings


class EverythingTraceService:
    """Best-effort write-only trace stream for full backend events."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "everything_trace_enabled", True))
        self.log_file = Path(
            str(
                getattr(
                    settings,
                    "everything_trace_log_file",
                    "./logs/everything_backend_trace.jsonl",
                )
            )
        )
        self.max_chars = max(
            1000,
            int(getattr(settings, "everything_trace_max_chars", 400000) or 400000),
        )
        self._lock = threading.Lock()

    def _truncate(self, value: str) -> str:
        text = str(value or "")
        if len(text) <= self.max_chars:
            return text
        omitted = len(text) - self.max_chars
        return f"{text[:self.max_chars]}...[TRUNCATED:{omitted}]"

    def _sanitize(self, value: Any, depth: int = 0) -> Any:
        if depth > 10:
            return self._truncate(str(value))
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self._truncate(value)
        if isinstance(value, dict):
            return {
                str(key): self._sanitize(item, depth + 1)
                for key, item in list(value.items())[:400]
            }
        if isinstance(value, list):
            return [self._sanitize(item, depth + 1) for item in value[:400]]
        if isinstance(value, tuple):
            return [self._sanitize(item, depth + 1) for item in value[:400]]
        return self._truncate(str(value))

    @staticmethod
    def _clean(value: Any) -> str:
        return str(value or "").strip()

    def _active_turn_context(self) -> dict[str, Any]:
        try:
            from services.turn_diagnostics_service import turn_diagnostics_service

            ctx = turn_diagnostics_service.get_turn_context()
            return ctx if isinstance(ctx, dict) else {}
        except Exception:
            return {}

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

        turn_ctx = self._active_turn_context()
        resolved_turn_trace_id = self._clean(turn_trace_id) or self._clean(turn_ctx.get("turn_trace_id"))
        resolved_trace_id = self._clean(trace_id) or self._clean(turn_ctx.get("api_trace_id"))
        resolved_session_id = self._clean(session_id) or self._clean(turn_ctx.get("session_id"))
        resolved_hotel_code = self._clean(hotel_code) or self._clean(turn_ctx.get("hotel_code"))
        resolved_channel = self._clean(channel) or self._clean(turn_ctx.get("channel"))

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": self._clean(event) or "backend_event",
            "trace_id": resolved_trace_id,
            "turn_trace_id": resolved_turn_trace_id,
            "session_id": resolved_session_id,
            "hotel_code": resolved_hotel_code,
            "channel": resolved_channel,
            "endpoint": self._clean(endpoint),
            "method": self._clean(method),
            "status_code": status_code,
            "component": self._clean(component),
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


everything_trace_service = EverythingTraceService()

