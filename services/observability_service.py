"""
Observability Service

Provides lightweight structured event logging with trace IDs.
This is intentionally file-based and dependency-free so it works
in local/dev setups and can later be shipped to external sinks.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings


class ObservabilityService:
    """Structured event logger with trace-id helpers."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "observability_enabled", True))
        self.log_file = Path(getattr(settings, "observability_log_file", "./logs/observability.log"))
        self._lock = threading.Lock()

    @staticmethod
    def new_trace_id() -> str:
        return f"trc-{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): ObservabilityService._sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ObservabilityService._sanitize(v) for v in value]
        return str(value)

    def log_event(self, event: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": str(event or "").strip() or "unknown",
            "payload": self._sanitize(payload),
        }
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(record, ensure_ascii=False)
            with self._lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(encoded + "\n")
        except Exception:
            # Logging must never fail the request path.
            return

    def get_status(self) -> dict[str, Any]:
        """Return current logger status for admin dashboards."""
        exists = self.log_file.exists()
        size_bytes = self.log_file.stat().st_size if exists else 0
        modified_at = (
            datetime.fromtimestamp(self.log_file.stat().st_mtime, tz=UTC).isoformat()
            if exists
            else None
        )
        return {
            "enabled": self.enabled,
            "log_file": str(self.log_file),
            "exists": exists,
            "size_bytes": size_bytes,
            "last_modified_at": modified_at,
        }

    def read_recent_events(self, limit: int = 100, event_filter: str = "") -> list[dict[str, Any]]:
        """
        Read last N events from the JSONL log file.
        Returns newest-first records.
        """
        if limit <= 0:
            return []
        if not self.log_file.exists():
            return []

        normalized_filter = str(event_filter or "").strip().lower()
        tail_size = min(2000, max(1, int(limit)) * 8)

        try:
            with self.log_file.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            return []

        rows: list[dict[str, Any]] = []
        for raw in reversed(lines[-tail_size:]):
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if normalized_filter:
                event_name = str(item.get("event") or "").strip().lower()
                if normalized_filter not in event_name:
                    continue
            rows.append(item)
            if len(rows) >= limit:
                break
        return rows


observability_service = ObservabilityService()
