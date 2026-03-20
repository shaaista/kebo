"""
Flow Logger

Single human-readable log file covering the entire bot pipeline:

  KB Upload     â†’ what was wiped, what new file was saved
  Re-Index      â†’ files indexed, chunks created
  Pull from KB  â†’ service info, full KB sent to LLM, verbatim extraction result
  Prompt Regen  â†’ service details, extracted KB used, generated system prompt
  LLM Calls     â†’ every call: actor, model, full messages in, full response out
  Chat Turn     â†’ user message, phase, available services, context history,
                  user info, orchestration decision, service routing, final reply

All written to: logs/flow.log
"""

from __future__ import annotations

import json
import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_LOG_FILE = Path("./logs/flow.log")
_KB_LOG_FILE = Path("./logs/kb_pipeline.log")
_SERVICE_CONFIG_LOG_FILE = Path("./logs/service_config.log")
_SERVICE_RUNTIME_LOG_FILE = Path("./logs/service_runtime.log")
_PROCESSING_JSONL_FILE = Path("./logs/processing_debug.jsonl")
_LOCK = threading.Lock()

_WIDE  = "â”" * 80
_THIN  = "â”€" * 80
_SEP   = "â•" * 80


# â”€â”€ internal helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _write(lines: list[str]) -> None:
    _write_to_file(_LOG_FILE, lines)


def _write_to_file(path: Path, lines: list[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        block = "\n".join(lines) + "\n"
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(block)
    except Exception:
        pass


def _write_json_event(*, category: str, event: str, payload: dict[str, Any]) -> None:
    try:
        _PROCESSING_JSONL_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "category": str(category or "").strip() or "general",
            "event": str(event or "").strip() or "event",
            "payload": payload or {},
        }
        encoded = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with _PROCESSING_JSONL_FILE.open("a", encoding="utf-8") as fh:
                fh.write(encoded + "\n")
    except Exception:
        pass


def _field(label: str, value: Any, width: int = 20) -> str:
    return f"  {label:<{width}}: {value}"


def _block(header: str, text: str, indent: int = 2) -> list[str]:
    lines = [f"  {'â”€' * (78 - 2)}", f"  {header}"]
    if text:
        for line in str(text).splitlines():
            lines.append(" " * indent + line)
    return lines


def _truncate(text: str, max_chars: int = 60000) -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return f"{text[:half]}\n\n... [{omitted:,} chars omitted] ...\n\n{text[-half:]}"


def _msgs_to_lines(messages: list[dict]) -> list[str]:
    """Render a messages list into readable lines."""
    out: list[str] = []
    for i, msg in enumerate(messages or []):
        role = str(msg.get("role") or "?").upper()
        content = str(msg.get("content") or "")
        out.append(f"  â”€â”€ [{i}] {role} {'â”€' * max(0, 70 - len(role))}")
        for line in _truncate(content, 40000).splitlines():
            out.append("  " + line)
    return out


# â”€â”€ public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def log_kb_upload(
    *,
    files_wiped_disk: int,
    db_records_deleted: int,
    new_file_name: str,
    new_file_bytes: int,
    saved_path: str,
    tenant_id: str,
) -> None:
    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  ðŸ“  KB UPLOAD",
        _WIDE,
        _field("Tenant",              tenant_id),
        _field("Previous files wiped", f"{files_wiped_disk} on disk  |  {db_records_deleted} DB records deleted"),
        _field("New file",             f"{new_file_name}  ({new_file_bytes:,} bytes)"),
        _field("Saved to",             saved_path),
        _field("Sources reset to",     "only the new file"),
    ]
    _write(lines)
    _write_to_file(_KB_LOG_FILE, lines)
    _write_json_event(
        category="kb",
        event="kb_upload",
        payload={
            "tenant_id": tenant_id,
            "files_wiped_disk": int(files_wiped_disk or 0),
            "db_records_deleted": int(db_records_deleted or 0),
            "new_file_name": str(new_file_name or ""),
            "new_file_bytes": int(new_file_bytes or 0),
            "saved_path": str(saved_path or ""),
        },
    )


def log_reindex(
    *,
    tenant_id: str,
    files: list[str],
    chunks_created: int,
    backend: str,
    clear_existing: bool,
) -> None:
    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  ðŸ”„  RE-INDEX",
        _WIDE,
        _field("Tenant",         tenant_id),
        _field("Clear existing", str(clear_existing)),
        _field("Files",          ", ".join(files) if files else "(none)"),
        _field("Chunks created", str(chunks_created)),
        _field("Backend",        backend),
    ]
    _write(lines)
    _write_to_file(_KB_LOG_FILE, lines)
    _write_json_event(
        category="kb",
        event="kb_reindex",
        payload={
            "tenant_id": tenant_id,
            "files": files or [],
            "chunks_created": int(chunks_created or 0),
            "backend": str(backend or ""),
            "clear_existing": bool(clear_existing),
        },
    )


def log_kb_db_persist(
    *,
    tenant_id: str,
    original_name: str,
    stored_name: str,
    content_chars: int,
    content_hash: str,
    success: bool,
    error: str = "",
) -> None:
    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  KB DB PERSIST  [{stored_name}]",
        _WIDE,
        _field("Tenant", tenant_id or "(unknown)"),
        _field("Original name", original_name or "(unknown)"),
        _field("Stored name", stored_name or "(unknown)"),
        _field("Content size", f"{int(content_chars or 0):,} chars"),
        _field("Content hash", content_hash or "(none)"),
        _field("Status", "success" if success else "failed"),
    ]
    if error:
        lines.append(_field("Error", error))
    _write(lines)
    _write_to_file(_KB_LOG_FILE, lines)
    _write_json_event(
        category="kb",
        event="kb_db_persist",
        payload={
            "tenant_id": str(tenant_id or ""),
            "original_name": str(original_name or ""),
            "stored_name": str(stored_name or ""),
            "content_chars": int(content_chars or 0),
            "content_hash": str(content_hash or ""),
            "success": bool(success),
            "error": str(error or ""),
        },
    )


def log_llm_call(
    *,
    actor: str,
    messages: list[dict],
    response: str,
    model: str,
    temperature: float,
    max_tokens: int,
    duration_ms: float,
    status: str = "success",
    error: str = "",
    trace_context: dict | None = None,
) -> None:
    ctx = trace_context or {}
    extra_bits: list[str] = []
    for k, v in ctx.items():
        if k not in ("actor",) and v:
            extra_bits.append(f"{k}={v}")
    extra = ("  " + "  ".join(extra_bits)) if extra_bits else ""

    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  ðŸ¤–  LLM CALL  [{actor}]{extra}",
        _WIDE,
        _field("Model",       model),
        _field("Temperature", temperature),
        _field("Max tokens",  max_tokens),
        _field("Status",      status),
        _field("Duration",    f"{duration_ms:.0f} ms"),
    ]
    if error:
        lines.append(_field("ERROR", error))

    lines += _block("â”€â”€ INPUT MESSAGES â”€â”€", "")
    lines += _msgs_to_lines(messages)

    lines += [
        f"  {'â”€' * 76}",
        "  â”€â”€ OUTPUT â”€â”€",
    ]
    for line in _truncate(response, 40000).splitlines():
        lines.append("  " + line)

    lines.append(_THIN)
    _write(lines)


def log_pull_from_kb(
    *,
    service_name: str,
    service_description: str,
    kb_chars: int,
    extracted_chars: int,
    extraction_mode: str,
) -> None:
    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  ðŸ§²  PULL FROM KB  [{service_name}]",
        _WIDE,
        _field("Service",         service_name),
        _field("Description",     service_description[:200] or "(none)"),
        _field("Full KB size",    f"{kb_chars:,} chars fed to LLM"),
        _field("Extracted size",  f"{extracted_chars:,} chars"),
        _field("Mode",            extraction_mode),
        "  (Full LLM call details logged in the LLM CALL block above)",
    ]
    _write(lines)
    _write_to_file(_KB_LOG_FILE, lines)
    _write_json_event(
        category="kb",
        event="pull_from_kb",
        payload={
            "service_name": str(service_name or ""),
            "service_description": str(service_description or ""),
            "kb_chars": int(kb_chars or 0),
            "extracted_chars": int(extracted_chars or 0),
            "extraction_mode": str(extraction_mode or ""),
        },
    )


def log_prompt_regen(
    *,
    service_id: str,
    service_name: str,
    extracted_kb_chars: int,
    generated_prompt_chars: int,
) -> None:
    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  âœï¸   PROMPT REGENERATE  [{service_id}]",
        _WIDE,
        _field("Service",              f"{service_name}  (id={service_id})"),
        _field("Extracted KB used",    f"{extracted_kb_chars:,} chars"),
        _field("Generated prompt",     f"{generated_prompt_chars:,} chars"),
        "  (Full LLM call details logged in the LLM CALL block above)",
    ]
    _write(lines)
    _write_to_file(_SERVICE_CONFIG_LOG_FILE, lines)
    _write_json_event(
        category="service_config",
        event="prompt_regenerated",
        payload={
            "service_id": str(service_id or ""),
            "service_name": str(service_name or ""),
            "extracted_kb_chars": int(extracted_kb_chars or 0),
            "generated_prompt_chars": int(generated_prompt_chars or 0),
        },
    )


def log_chat_turn(
    *,
    session_id: str,
    user_message: str,
    phase: str,
    available_services: list[str],
    context_history_count: int,
    guest_info: dict,
    summary: str,
) -> None:
    svc_str = ", ".join(available_services) if available_services else "(none)"
    guest_bits = "  |  ".join(f"{k}={v}" for k, v in (guest_info or {}).items() if v)
    lines = [
        "",
        _SEP,
        f"[{_ts()}]  ðŸ’¬  CHAT TURN  [session: {session_id}]",
        _SEP,
        _field("User message",       user_message[:300]),
        _field("Phase",              phase or "(unknown)"),
        _field("Available services", svc_str),
        _field("Context history",    f"{context_history_count} messages"),
        _field("Guest info",         guest_bits or "(none)"),
    ]
    if summary:
        lines.append(_field("Summary",  summary[:200]))
    lines.append("  (Orchestration + service LLM calls follow in LLM CALL blocks below)")
    _write(lines)


def log_chat_response(
    *,
    session_id: str,
    routed_service: str,
    final_response: str,
) -> None:
    lines = [
        f"  â”€â”€ FINAL RESPONSE  [session: {session_id}  |  service: {routed_service}]",
    ]
    for line in _truncate(final_response, 2000).splitlines():
        lines.append("  " + line)
    lines.append(_SEP)
    _write(lines)


def log_orchestration_decision(
    *,
    session_id: str,
    decision: dict,
) -> None:
    lines = [
        f"  â”€â”€ ORCHESTRATION DECISION  [session: {session_id}]",
    ]
    try:
        decision_str = json.dumps(decision, indent=4, ensure_ascii=False)
    except Exception:
        decision_str = str(decision)
    for line in decision_str.splitlines():
        lines.append("  " + line)
    _write(lines)


def log_service_config_save(
    *,
    action: str,
    source: str,
    service_id: str,
    service_name: str,
    description_len: int,
    ticketing_policy_len: int,
    extracted_knowledge_len: int,
    generated_prompt_len: int,
    success: bool,
    error: str = "",
) -> None:
    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  SERVICE CONFIG SAVE  [{service_id}]",
        _WIDE,
        _field("Action", action),
        _field("Source", source),
        _field("Service", f"{service_name} (id={service_id})"),
        _field("Description len", description_len),
        _field("Ticket policy len", ticketing_policy_len),
        _field("Extracted KB len", extracted_knowledge_len),
        _field("Prompt len", generated_prompt_len),
        _field("Status", "success" if success else "failed"),
    ]
    if error:
        lines.append(_field("Error", error))
    _write(lines)
    _write_to_file(_SERVICE_CONFIG_LOG_FILE, lines)
    _write_json_event(
        category="service_config",
        event="service_config_save",
        payload={
            "action": str(action or ""),
            "source": str(source or ""),
            "service_id": str(service_id or ""),
            "service_name": str(service_name or ""),
            "description_len": int(description_len or 0),
            "ticketing_policy_len": int(ticketing_policy_len or 0),
            "extracted_knowledge_len": int(extracted_knowledge_len or 0),
            "generated_prompt_len": int(generated_prompt_len or 0),
            "success": bool(success),
            "error": str(error or ""),
        },
    )


def log_service_runtime(
    *,
    session_id: str,
    service_id: str,
    service_name: str,
    phase_id: str,
    prompt_source: str,
    extracted_knowledge_chars: int,
    generated_prompt_chars: int,
    full_kb_fallback_chars: int,
    pending_action_before: str,
    response_action: str,
    missing_fields: list[str],
    ticket_ready_to_create: bool,
    context_switched: bool,
) -> None:
    lines = [
        "",
        _WIDE,
        f"[{_ts()}]  SERVICE RUNTIME  [{service_id}]",
        _WIDE,
        _field("Session", session_id),
        _field("Service", f"{service_name} (id={service_id})"),
        _field("Phase", phase_id or "(unknown)"),
        _field("Prompt source", prompt_source or "(unknown)"),
        _field("Extracted KB", f"{int(extracted_knowledge_chars or 0):,} chars"),
        _field("Generated prompt", f"{int(generated_prompt_chars or 0):,} chars"),
        _field("Full KB fallback", f"{int(full_kb_fallback_chars or 0):,} chars"),
        _field("Pending before", pending_action_before or "(none)"),
        _field("Decision action", response_action or "(none)"),
        _field("Missing fields", ", ".join(missing_fields or []) or "(none)"),
        _field("Ticket ready", str(bool(ticket_ready_to_create))),
        _field("Context switched", str(bool(context_switched))),
    ]
    _write(lines)
    _write_to_file(_SERVICE_RUNTIME_LOG_FILE, lines)
    _write_json_event(
        category="service_runtime",
        event="service_agent_decision",
        payload={
            "session_id": str(session_id or ""),
            "service_id": str(service_id or ""),
            "service_name": str(service_name or ""),
            "phase_id": str(phase_id or ""),
            "prompt_source": str(prompt_source or ""),
            "extracted_knowledge_chars": int(extracted_knowledge_chars or 0),
            "generated_prompt_chars": int(generated_prompt_chars or 0),
            "full_kb_fallback_chars": int(full_kb_fallback_chars or 0),
            "pending_action_before": str(pending_action_before or ""),
            "response_action": str(response_action or ""),
            "missing_fields": [str(item or "") for item in (missing_fields or []) if str(item or "").strip()],
            "ticket_ready_to_create": bool(ticket_ready_to_create),
            "context_switched": bool(context_switched),
        },
    )


def clear_log() -> None:
    """Truncate primary debug logs to start fresh (called on startup)."""
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        started_line = f"# Started: {_ts()}\n\n"
        with _LOCK:
            with _LOG_FILE.open("w", encoding="utf-8") as fh:
                fh.write(f"# KePSLA Bot - Flow Log\n{started_line}")
            with _KB_LOG_FILE.open("w", encoding="utf-8") as fh:
                fh.write(f"# KB Pipeline Log\n{started_line}")
            with _SERVICE_CONFIG_LOG_FILE.open("w", encoding="utf-8") as fh:
                fh.write(f"# Service Config Log\n{started_line}")
            with _SERVICE_RUNTIME_LOG_FILE.open("w", encoding="utf-8") as fh:
                fh.write(f"# Service Runtime Log\n{started_line}")
            with _PROCESSING_JSONL_FILE.open("w", encoding="utf-8") as fh:
                fh.write("")
    except Exception:
        pass

