"""
Log Setup Service

Ensures configured log files exist so operators do not see missing-path
issues when tracing incidents.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from config.settings import settings


class LogSetupService:
    """Creates configured log file paths without truncating existing logs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._repo_root = Path(__file__).resolve().parent.parent

    def _resolve_path(self, value: str) -> Path:
        candidate = Path(str(value or "").strip())
        if candidate.is_absolute():
            return candidate
        return (self._repo_root / candidate).resolve()

    def ensure_configured_log_files(self) -> dict[str, Any]:
        settings_names = sorted(
            name for name in dir(settings) if name.endswith("_log_file")
        )
        created: list[str] = []
        existing: list[str] = []
        failed: list[dict[str, str]] = []
        seen: set[str] = set()

        with self._lock:
            for name in settings_names:
                raw_value = getattr(settings, name, "")
                if not isinstance(raw_value, str) or not raw_value.strip():
                    continue

                path = self._resolve_path(raw_value)
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)

                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.exists():
                        existing.append(key)
                    else:
                        path.touch(exist_ok=True)
                        created.append(key)
                except Exception as exc:
                    failed.append({"setting": name, "path": key, "error": str(exc)})

        return {
            "settings_scanned": len(settings_names),
            "created_count": len(created),
            "created_paths": created[:200],
            "existing_count": len(existing),
            "existing_paths": existing[:200],
            "failed_count": len(failed),
            "failed_paths": failed[:200],
        }


log_setup_service = LogSetupService()
