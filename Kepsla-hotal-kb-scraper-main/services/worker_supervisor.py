"""Manage an optional worker subprocess owned by the API process."""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MANAGED_WORKER: subprocess.Popen[bytes] | None = None
_MANAGED_WORKER_STARTED_AT: float | None = None
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_WORKER_SCRIPT = _PROJECT_ROOT / "worker.py"


def _build_worker_command() -> list[str]:
    return [sys.executable, str(_WORKER_SCRIPT)]


def maybe_start_managed_worker() -> bool:
    """Start a worker subprocess if managed auto-start is enabled and no worker is running."""
    global _MANAGED_WORKER, _MANAGED_WORKER_STARTED_AT

    if not settings.worker_auto_start_enabled:
        return False

    with _LOCK:
        if _MANAGED_WORKER is not None and _MANAGED_WORKER.poll() is None:
            return False

        env = os.environ.copy()
        env["HOTEL_KB_WORKER_AUTO_START_ENABLED"] = "false"
        env["PYTHONUNBUFFERED"] = "1"

        _MANAGED_WORKER = subprocess.Popen(
            _build_worker_command(),
            cwd=str(_PROJECT_ROOT),
            env=env,
        )
        _MANAGED_WORKER_STARTED_AT = time.time()

        logger.info(
            "Managed worker started | pid=%s | cwd=%s",
            _MANAGED_WORKER.pid,
            _PROJECT_ROOT,
        )
        return True


def shutdown_managed_worker() -> None:
    """Terminate the worker subprocess started by this API process."""
    global _MANAGED_WORKER, _MANAGED_WORKER_STARTED_AT

    with _LOCK:
        process = _MANAGED_WORKER
        _MANAGED_WORKER = None
        _MANAGED_WORKER_STARTED_AT = None

    if process is None or process.poll() is not None:
        return

    logger.info("Stopping managed worker | pid=%s", process.pid)
    process.terminate()
    try:
        process.wait(timeout=settings.worker_shutdown_timeout_seconds)
    except subprocess.TimeoutExpired:
        logger.warning("Managed worker did not exit cleanly; killing pid=%s", process.pid)
        process.kill()
        process.wait(timeout=5)


def get_managed_worker_snapshot() -> dict[str, Any]:
    """Return a lightweight status snapshot for the managed worker."""
    with _LOCK:
        process = _MANAGED_WORKER
        started_at = _MANAGED_WORKER_STARTED_AT

    running = process is not None and process.poll() is None
    uptime_seconds = None
    if running and started_at is not None:
        uptime_seconds = max(0.0, time.time() - started_at)

    return {
        "auto_start_enabled": settings.worker_auto_start_enabled,
        "managed": True,
        "running": running,
        "pid": process.pid if running and process is not None else None,
        "uptime_seconds": uptime_seconds,
    }


atexit.register(shutdown_managed_worker)
