"""
Chat Step Timing Service

Writes one JSONL record per chat message turn with:
- total turn duration
- per-step durations
- LLM call timing/token/cost breakdown (captured from active turn context)
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings


class ChatStepTimingService:
    """Append-only per-turn timing breakdown logger."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "chat_step_timing_enabled", True))
        self.log_file = Path(
            str(getattr(settings, "chat_step_timing_log_file", "./logs/chat_step_timing.jsonl"))
        )
        self.max_chars = max(
            2000,
            int(getattr(settings, "chat_step_timing_max_chars", 250000) or 250000),
        )
        self._lock = threading.Lock()

    @classmethod
    def _coerce_json_safe(cls, value: Any, depth: int = 0) -> Any:
        if depth > 10:
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(k): cls._coerce_json_safe(v, depth + 1)
                for k, v in list(value.items())[:400]
            }
        if isinstance(value, list):
            return [cls._coerce_json_safe(v, depth + 1) for v in value[:400]]
        if isinstance(value, tuple):
            return [cls._coerce_json_safe(v, depth + 1) for v in value[:400]]
        return str(value)

    def _truncate_large_strings(self, value: Any, depth: int = 0) -> Any:
        if depth > 12:
            return str(value)
        if isinstance(value, str):
            if len(value) <= self.max_chars:
                return value
            omitted = len(value) - self.max_chars
            return f"{value[:self.max_chars]}\n...[TRUNCATED {omitted} chars]"
        if isinstance(value, dict):
            return {
                str(k): self._truncate_large_strings(v, depth + 1)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._truncate_large_strings(v, depth + 1) for v in value]
        return value

    def log_turn(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            **(payload or {}),
        }
        try:
            safe_record = self._coerce_json_safe(record)
            safe_record = self._truncate_large_strings(safe_record)
            encoded = json.dumps(safe_record, ensure_ascii=False)
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(encoded + "\n")
        except Exception:
            return


chat_step_timing_service = ChatStepTimingService()
