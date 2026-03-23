"""
new_detailed_logger.py

A single, self-contained logger that writes a human-readable, fully detailed
record of EVERYTHING that happens in this application to:

    logs/new_detailed.log

Covers:
  - App startup  (begin, each init step, ready)
  - DB init      (success / failure)
  - KB restore   (how many files, success / failure)
  - HTTP traffic (every request in and out, incl. auth/rate-limit rejections)
  - Chat turns   (user message, metadata, bot reply, intent, routing, duration)
  - Chat errors  (full traceback, session info)
  - App shutdown

Nothing is truncated for startup/shutdown/error blocks.
Chat messages and bot replies are captured in full (up to 30 000 chars each
before a soft truncation marker is added so the file stays readable).

Thread-safe: uses a single module-level lock.
No external dependencies beyond the Python standard library.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Always resolve relative to THIS file so the log ends up in logs/ regardless
# of the process working directory.
_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "new_detailed.log"
_MAX_MSG_CHARS = 30_000          # soft cap for user/bot message bodies
_LOCK = threading.Lock()

# Box-drawing characters for visual structure
_DOUBLE = "=" * 80
_SINGLE = "-" * 80
_THICK  = "#" * 80

# ---------------------------------------------------------------------------
# Internal: persistent file handle (opened once, kept open for the process
# lifetime — avoids repeated open/close races on Windows)
# ---------------------------------------------------------------------------

_fh = None          # the open file object
_fh_lock = threading.Lock()


def _open_file() -> None:
    """Open (or re-open) the log file for appending. Called inside _fh_lock."""
    global _fh
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _fh = open(str(_LOG_FILE), "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception as _open_exc:
        _fh = None
        _msg = (
            f"[new_detailed_logger] OPEN FAILED: {_open_exc!r}  "
            f"log_file={_LOG_FILE}"
        )
        try:
            print(_msg, file=sys.stderr, flush=True)
        except Exception:
            pass
        try:
            print(_msg, flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ts_local() -> str:
    """Local timestamp string with UTC offset."""
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S %Z")


def _ts_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ts_both() -> str:
    return f"{_ts_local()}  /  {_ts_utc()}"


def _field(label: str, value: Any, width: int = 22) -> str:
    v = str(value) if value is not None else "(none)"
    return f"  {label:<{width}}: {v}"


def _soft_truncate(text: str, max_chars: int = _MAX_MSG_CHARS) -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return (
        f"{text[:half]}\n\n"
        f"  ... [{omitted:,} chars omitted — increase _MAX_MSG_CHARS to see all] ...\n\n"
        f"{text[-half:]}"
    )


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in str(text).splitlines())


def _write(lines: list[str]) -> None:
    """Append lines to the log file (thread-safe, persistent file handle)."""
    global _fh
    block = "\n".join(lines) + "\n"
    try:
        with _LOCK:
            # Ensure we have an open handle (lazy init + re-open after errors).
            if _fh is None or _fh.closed:
                with _fh_lock:
                    if _fh is None or _fh.closed:
                        _open_file()
            if _fh is not None:
                _fh.write(block)
                _fh.flush()
    except Exception as _write_exc:
        # Primary write failed — print error and try a one-shot fallback.
        _err_msg = (
            f"[new_detailed_logger] WRITE FAILED: {_write_exc!r}\n"
            f"  log_file={_LOG_FILE}\n"
            f"  {traceback.format_exc()}"
        )
        try:
            print(_err_msg, file=sys.stderr, flush=True)
        except Exception:
            pass
        try:
            print(_err_msg, flush=True)
        except Exception:
            pass
        # Mark handle as dead so next call re-opens it.
        try:
            if _fh and not _fh.closed:
                _fh.close()
        except Exception:
            pass
        _fh = None
        # Last-resort one-shot write.
        try:
            with open(str(_LOG_FILE), "a", encoding="utf-8", errors="replace") as _fb:
                _fb.write(block)
                _fb.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API — Startup / Shutdown
# ---------------------------------------------------------------------------

def log_startup_begin(*, app_name: str, host: str, port: int, env: str) -> None:
    """Called as the very first thing during lifespan startup."""
    lines = [
        "",
        _DOUBLE,
        f"  🚀  APP STARTED",
        _DOUBLE,
        _field("Timestamp",   _ts_both()),
        _field("App name",    app_name),
        _field("Environment", env),
        _field("Bind address", f"{host}:{port}"),
        _field("Python",      sys.version.split()[0]),
        _field("Log file",    str(_LOG_FILE.resolve())),
        _SINGLE,
        "  Waiting for init steps...",
    ]
    _write(lines)


def log_db_init(*, success: bool, error: str = "") -> None:
    """Called after init_db() completes (or fails)."""
    status = "✅  SUCCESS" if success else "❌  FAILED"
    lines = [
        _field("DB init", status),
    ]
    if error:
        lines += [
            _field("DB error", error),
        ]
    _write(lines)


def log_kb_restore(*, restored: int, error: str = "") -> None:
    """Called after KB file restoration from DB."""
    if error:
        lines = [_field("KB restore", f"⚠️  WARNING — {error}")]
    else:
        lines = [_field("KB restore", f"✅  {restored} file(s) restored from DB")]
    _write(lines)


def log_startup_ready(*, host: str, port: int) -> None:
    """Called when the server is fully ready to accept requests."""
    lines = [
        _field("Status", "✅  READY — accepting requests"),
        _field("Chat UI", f"http://{host}:{port}/chat"),
        _field("Admin UI", f"http://{host}:{port}/admin"),
        _field("API docs", f"http://{host}:{port}/docs"),
        _DOUBLE,
        "",
    ]
    _write(lines)


def log_shutdown() -> None:
    """Called during lifespan shutdown."""
    lines = [
        "",
        _DOUBLE,
        f"  👋  APP SHUTDOWN",
        _DOUBLE,
        _field("Timestamp", _ts_both()),
        _field("Reason",    "Normal shutdown (SIGINT / server stop)"),
        _DOUBLE,
        "",
    ]
    _write(lines)


# ---------------------------------------------------------------------------
# Public API — HTTP Traffic
# ---------------------------------------------------------------------------

def log_http_request(
    *,
    trace_id: str,
    method: str,
    path: str,
    client_host: str,
    user_agent: str = "",
    query: dict | None = None,
) -> None:
    """Called at the start of every API request reaching the gateway middleware."""
    lines = [
        "",
        _SINGLE,
        f"  ➡️   HTTP REQUEST  [{method}  {path}]",
        _SINGLE,
        _field("Timestamp",   _ts_both()),
        _field("Trace ID",    trace_id or "(none)"),
        _field("Method",      method),
        _field("Path",        path),
        _field("Client",      client_host or "unknown"),
        _field("User-Agent",  (user_agent or "")[:120] or "(none)"),
    ]
    if query:
        lines.append(_field("Query params", json.dumps(query, ensure_ascii=False)))
    _write(lines)


def log_http_response(
    *,
    trace_id: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    error: str = "",
) -> None:
    """Called after processing every API request (success or hard exception)."""
    emoji = "✅" if 200 <= status_code < 300 else ("⚠️" if 300 <= status_code < 500 else "❌")
    lines = [
        f"  {emoji}  HTTP RESPONSE  [{status_code}  {method}  {path}]",
        _field("Status code", status_code),
        _field("Duration",    f"{duration_ms:.1f} ms"),
    ]
    if error:
        lines.append(_field("Error", error))
    lines.append(_SINGLE + "\n")
    _write(lines)


def log_http_rejected(
    *,
    trace_id: str,
    reason: str,
    path: str,
    method: str,
    client_host: str,
    status_code: int,
    extra: str = "",
) -> None:
    """Called when a request is rejected before reaching a handler (auth / rate limit)."""
    lines = [
        "",
        _SINGLE,
        f"  🚫  REQUEST REJECTED  [{status_code}]  {reason.upper()}",
        _SINGLE,
        _field("Timestamp",   _ts_both()),
        _field("Trace ID",    trace_id or "(none)"),
        _field("Method",      method),
        _field("Path",        path),
        _field("Client",      client_host or "unknown"),
        _field("Status code", status_code),
        _field("Reason",      reason),
    ]
    if extra:
        lines.append(_field("Detail", extra))
    lines.append(_SINGLE + "\n")
    _write(lines)


# ---------------------------------------------------------------------------
# Public API — Chat Turns
# ---------------------------------------------------------------------------

def log_chat_turn_in(
    *,
    session_id: str,
    message: str,
    hotel_code: str,
    channel: str,
    phase: str,
    trace_id: str,
    turn_trace_id: str,
    guest_info: dict | None = None,
    history_count: int = 0,
    metadata: dict | None = None,
) -> None:
    """
    Called at the START of every chat turn (before processing).
    Logs everything the server received from the caller.
    """
    meta_safe: dict = {
        k: v for k, v in (metadata or {}).items()
        if k not in ("conversation_history",)  # skip heavy blob
    }

    lines = [
        "",
        _THICK,
        f"  💬  CHAT TURN IN  [session: {session_id}]",
        _THICK,
        _field("Timestamp",     _ts_both()),
        _field("Session ID",    session_id or "(none)"),
        _field("Hotel code",    hotel_code or "(none)"),
        _field("Channel",       channel or "(none)"),
        _field("Phase",         phase or "(none)"),
        _field("Trace ID",      trace_id or "(none)"),
        _field("Turn trace ID", turn_trace_id or "(none)"),
        _field("Msg length",    f"{len(str(message or ''))} chars"),
        _field("History turns", history_count),
    ]

    # Guest info block
    if guest_info:
        lines.append(f"  {'─' * 50}")
        lines.append("  Guest / Profile Fields:")
        for k, v in guest_info.items():
            lines.append(_field(f"    {k}", v))

    # Metadata (excluding large blobs)
    if meta_safe:
        lines.append(f"  {'─' * 50}")
        lines.append("  Request Metadata (non-history):")
        for k, v in meta_safe.items():
            lines.append(_field(f"    {k}", str(v)[:200]))

    # User message (full, with soft truncation)
    lines += [
        f"  {'─' * 76}",
        "  USER MESSAGE (full text):",
        _indent(_soft_truncate(message)),
        f"  {'─' * 76}",
    ]
    _write(lines)


def log_chat_turn_out_success(
    *,
    session_id: str,
    bot_reply: str,
    display_message: str,
    intent: str,
    routing_path: str,
    response_source: str,
    service_label: str,
    confidence: float | None,
    state: str,
    duration_ms: float,
    trace_id: str,
    turn_trace_id: str,
    db_fallback: bool = False,
    ticket_created: bool = False,
    suggestions: list[str] | None = None,
    orchestration: dict | None = None,
    full_metadata: dict | None = None,
) -> None:
    """Called when a chat turn succeeds and a bot reply is returned."""
    lines = [
        f"  ✅  CHAT TURN OUT — SUCCESS  [session: {session_id}]",
        _field("Timestamp",       _ts_both()),
        _field("Duration",        f"{duration_ms:.1f} ms"),
        _field("State",           state or "(none)"),
        _field("Intent",          intent or "(none)"),
        _field("Routing path",    routing_path or "(none)"),
        _field("Response source", response_source or "(none)"),
        _field("Service label",   service_label or "(none)"),
        _field("Confidence",      f"{confidence:.3f}" if confidence is not None else "(none)"),
        _field("Ticket created",  str(ticket_created)),
        _field("DB fallback",     str(db_fallback)),
        _field("Trace ID",        trace_id or "(none)"),
        _field("Turn trace ID",   turn_trace_id or "(none)"),
    ]

    if suggestions:
        lines.append(_field("Suggestions", " | ".join(suggestions)))

    # Orchestration decision if present
    if orchestration and isinstance(orchestration, dict):
        lines += [
            f"  {'─' * 50}",
            "  Orchestration Decision:",
            _indent(json.dumps(orchestration, indent=2, ensure_ascii=False, default=str)),
        ]

    # Full metadata snapshot
    if full_metadata:
        try:
            meta_str = json.dumps(full_metadata, indent=2, ensure_ascii=False, default=str)
        except Exception:
            meta_str = str(full_metadata)
        lines += [
            f"  {'─' * 50}",
            "  Full Response Metadata:",
            _indent(_soft_truncate(meta_str, 5000)),
        ]

    # Bot reply (canonical)
    lines += [
        f"  {'─' * 76}",
        "  BOT REPLY (canonical text):",
        _indent(_soft_truncate(bot_reply)),
    ]

    # Display message (if different)
    if display_message and display_message.strip() != bot_reply.strip():
        lines += [
            f"  {'─' * 50}",
            "  BOT REPLY (display / beautified):",
            _indent(_soft_truncate(display_message)),
        ]

    lines += [
        _THICK,
        "",
    ]
    _write(lines)


def log_chat_turn_out_failed(
    *,
    session_id: str,
    message: str,
    hotel_code: str,
    trace_id: str,
    turn_trace_id: str,
    duration_ms: float,
    error_type: str,
    error_msg: str,
    tb: str = "",
    db_fallback: bool = False,
    http_status: int = 500,
) -> None:
    """Called when a chat turn fails (any exception path)."""
    lines = [
        f"  ❌  CHAT TURN OUT — FAILED  [session: {session_id}]",
        _field("Timestamp",     _ts_both()),
        _field("Duration",      f"{duration_ms:.1f} ms"),
        _field("HTTP status",   http_status),
        _field("Session ID",    session_id or "(none)"),
        _field("Hotel code",    hotel_code or "(none)"),
        _field("Trace ID",      trace_id or "(none)"),
        _field("Turn trace ID", turn_trace_id or "(none)"),
        _field("DB fallback",   str(db_fallback)),
        _field("Error type",    error_type or "Exception"),
        _field("Error message", error_msg or "(no message)"),
    ]

    # User message that caused the failure
    lines += [
        f"  {'─' * 50}",
        "  Failing user message:",
        _indent(_soft_truncate(message, 2000)),
    ]

    # Full traceback
    if tb:
        lines += [
            f"  {'─' * 50}",
            "  Traceback:",
            _indent(tb),
        ]

    lines += [
        _THICK,
        "",
    ]
    _write(lines)


# ---------------------------------------------------------------------------
# Convenience: write a plain note (used for ad-hoc debug lines)
# ---------------------------------------------------------------------------

def log_note(note: str, *, component: str = "") -> None:
    """Write a free-form note to the log (for debug/trace use)."""
    prefix = f"[{component}] " if component else ""
    lines = [
        _field("NOTE", f"{_ts_both()}  {prefix}{note}"),
    ]
    _write(lines)
