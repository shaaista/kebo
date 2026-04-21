"""
Log Retention Service

Deletes old runtime log files based on age-only retention policy.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from config.settings import settings


class LogRetentionService:
    """Age-based log retention manager."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "log_retention_enabled", True))
        self.retention_days = max(1, int(getattr(settings, "log_retention_days", 7) or 7))
        self.include_root_temp_logs = bool(
            getattr(settings, "log_retention_include_root_temp_logs", True)
        )
        self._lock = threading.Lock()
        self._repo_root = Path(__file__).resolve().parent.parent
        self._logs_root = self._repo_root / "logs"

    def _iter_candidate_paths(self) -> list[Path]:
        candidates: list[Path] = []
        if self._logs_root.exists():
            candidates.extend([p for p in self._logs_root.rglob("*") if p.is_file()])

        if self.include_root_temp_logs:
            for pattern in ("tmp_uvicorn_*.log",):
                candidates.extend([p for p in self._repo_root.glob(pattern) if p.is_file()])
        return candidates

    @staticmethod
    def _file_mtime_utc(path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    def cleanup_old_logs(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "retention_days": self.retention_days,
                "deleted_count": 0,
                "deleted_paths": [],
                "failed_count": 0,
                "failed_paths": [],
            }

        threshold = datetime.now(UTC) - timedelta(days=self.retention_days)
        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        with self._lock:
            for path in self._iter_candidate_paths():
                try:
                    modified_at = self._file_mtime_utc(path)
                except Exception as exc:
                    failed.append({"path": str(path), "error": f"stat_failed:{exc}"})
                    continue
                if modified_at >= threshold:
                    continue

                try:
                    path.unlink()
                    deleted.append(str(path))
                except Exception as exc:
                    failed.append({"path": str(path), "error": f"delete_failed:{exc}"})

        return {
            "enabled": True,
            "retention_days": self.retention_days,
            "threshold_utc": threshold.isoformat(),
            "deleted_count": len(deleted),
            "deleted_paths": deleted[:200],
            "failed_count": len(failed),
            "failed_paths": failed[:200],
        }


log_retention_service = LogRetentionService()

