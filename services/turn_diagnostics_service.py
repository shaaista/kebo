"""
Turn Diagnostics Service

Creates an ordered forensic timeline per chat turn:
- turn_start / turn_end (or turn_error)
- every LLM call made during the turn
- orchestration decisions
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings

_ACTIVE_TURN_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "active_turn_diagnostics_context",
    default=None,
)


class TurnDiagnosticsService:
    """Append-only ordered diagnostics timeline for each chat turn."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "turn_diagnostics_enabled", True))
        self.log_file = Path(
            str(getattr(settings, "turn_diagnostics_log_file", "./logs/turn_diagnostics.jsonl"))
        )
        self.max_chars = max(
            2000,
            int(getattr(settings, "turn_diagnostics_max_chars", 250000) or 250000),
        )
        self.turn_llm_summary_enabled = bool(
            getattr(settings, "turn_llm_summary_enabled", True)
        )
        self.turn_llm_summary_log_file = Path(
            str(
                getattr(
                    settings,
                    "turn_llm_summary_log_file",
                    "./logs/chat_turn_llm_summary.jsonl",
                )
            )
        )
        self._lock = threading.Lock()
        self._logger = logging.getLogger("turn_diagnostics")
        if not self._logger.handlers:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(self.log_file, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False
        self._turn_llm_summary_logger: logging.Logger | None = None
        if self.turn_llm_summary_enabled:
            self._turn_llm_summary_logger = logging.getLogger("turn_llm_summary")
            if not self._turn_llm_summary_logger.handlers:
                self.turn_llm_summary_log_file.parent.mkdir(parents=True, exist_ok=True)
                summary_handler = logging.FileHandler(
                    self.turn_llm_summary_log_file,
                    encoding="utf-8",
                )
                summary_handler.setFormatter(logging.Formatter("%(message)s"))
                self._turn_llm_summary_logger.addHandler(summary_handler)
                self._turn_llm_summary_logger.setLevel(logging.INFO)
                self._turn_llm_summary_logger.propagate = False

    @staticmethod
    def _as_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        if parsed != parsed:
            return 0.0
        return max(0.0, parsed)

    @staticmethod
    def _coerce_json_safe(value: Any, depth: int = 0) -> Any:
        if depth > 10:
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(k): TurnDiagnosticsService._coerce_json_safe(v, depth + 1)
                for k, v in list(value.items())[:300]
            }
        if isinstance(value, list):
            return [TurnDiagnosticsService._coerce_json_safe(v, depth + 1) for v in value[:300]]
        if isinstance(value, tuple):
            return [TurnDiagnosticsService._coerce_json_safe(v, depth + 1) for v in value[:300]]
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

    @staticmethod
    def _normalize_phase_identifier(value: Any) -> str:
        text = str(value or "").strip().lower().replace(" ", "_")
        aliases = {
            "prebooking": "pre_booking",
            "precheckin": "pre_checkin",
            "duringstay": "during_stay",
            "instay": "during_stay",
            "in_stay": "during_stay",
            "postcheckout": "post_checkout",
        }
        return aliases.get(text, text)

    def begin_turn(self, *, request: Any, api_trace_id: str = "") -> tuple[str, Token]:
        turn_trace_id = f"turn-{uuid.uuid4().hex[:12]}"
        request_meta = request.metadata if isinstance(getattr(request, "metadata", None), dict) else {}
        channel = (
            self._as_text(getattr(request, "channel", ""))
            or self._as_text(request_meta.get("channel"))
            or "web"
        )
        phase_hint_id = self._normalize_phase_identifier(
            request_meta.get("phase")
            or request_meta.get("journey_phase")
            or ((request_meta.get("_integration") or {}).get("phase") if isinstance(request_meta.get("_integration"), dict) else "")
        )
        ctx: dict[str, Any] = {
            "turn_trace_id": turn_trace_id,
            "api_trace_id": self._as_text(api_trace_id),
            "session_id": self._as_text(getattr(request, "session_id", "")),
            "hotel_code": self._as_text(getattr(request, "hotel_code", "")),
            "channel": channel,
            "phase_id": phase_hint_id,
            "phase_name": "",
            "_user_message": self._as_text(getattr(request, "message", "")),
            "_llm_call_metrics": [],
            "_seq": 0,
        }
        token = _ACTIVE_TURN_CONTEXT.set(ctx)
        self.log_event(
            "turn_start",
            {
                "request": {
                    "session_id": self._as_text(getattr(request, "session_id", "")),
                    "hotel_code": self._as_text(getattr(request, "hotel_code", "")),
                    "channel": channel,
                    "message": self._as_text(getattr(request, "message", "")),
                    "metadata": request_meta,
                },
                "phase_hint_id": phase_hint_id,
            },
        )
        return turn_trace_id, token

    def clear_turn(self, token: Token) -> None:
        try:
            _ACTIVE_TURN_CONTEXT.reset(token)
        except Exception:
            return

    def get_turn_context(self) -> dict[str, Any]:
        ctx = _ACTIVE_TURN_CONTEXT.get()
        if isinstance(ctx, dict):
            return ctx
        return {}

    def update_turn_context(
        self,
        *,
        phase_id: str = "",
        phase_name: str = "",
    ) -> None:
        ctx = self.get_turn_context()
        if not ctx:
            return
        normalized_phase_id = self._normalize_phase_identifier(phase_id)
        if normalized_phase_id:
            ctx["phase_id"] = normalized_phase_id
        if str(phase_name or "").strip():
            ctx["phase_name"] = str(phase_name).strip()

    def _next_seq(self) -> int:
        ctx = self.get_turn_context()
        if not ctx:
            return 0
        seq = int(ctx.get("_seq", 0)) + 1
        ctx["_seq"] = seq
        return seq

    def log_event(self, event_type: str, payload: Any) -> None:
        if not self.enabled:
            return
        ctx = self.get_turn_context()
        seq = self._next_seq()
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": str(event_type or "").strip() or "event",
            "turn_trace_id": self._as_text(ctx.get("turn_trace_id") if ctx else ""),
            "sequence": seq,
            "session_id": self._as_text(ctx.get("session_id") if ctx else ""),
            "hotel_code": self._as_text(ctx.get("hotel_code") if ctx else ""),
            "channel": self._as_text(ctx.get("channel") if ctx else ""),
            "api_trace_id": self._as_text(ctx.get("api_trace_id") if ctx else ""),
            "phase": {
                "id": self._as_text(ctx.get("phase_id") if ctx else ""),
                "name": self._as_text(ctx.get("phase_name") if ctx else ""),
            },
            "payload": payload,
        }
        try:
            safe_record = self._coerce_json_safe(record)
            safe_record = self._truncate_large_strings(safe_record)
            line = json.dumps(safe_record, ensure_ascii=False)
            with self._lock:
                self._logger.info(line)
        except Exception:
            return

    def record_llm_call_metrics(self, call_record: dict[str, Any]) -> None:
        """Collect normalized per-call metrics for the active turn summary."""
        if not self.enabled:
            return
        ctx = self.get_turn_context()
        if not ctx or not isinstance(call_record, dict):
            return

        calls = ctx.get("_llm_call_metrics")
        if not isinstance(calls, list):
            calls = []
            ctx["_llm_call_metrics"] = calls

        normalized = {
            "timestamp": self._as_text(call_record.get("timestamp")),
            "call_id": self._as_text(call_record.get("call_id")),
            "operation": self._as_text(call_record.get("operation")),
            "model": self._as_text(call_record.get("model")),
            "what_this_call_is_for": self._as_text(call_record.get("what_this_call_is_for")),
            "status": self._as_text(call_record.get("status")),
            "duration_ms": round(self._as_float(call_record.get("duration_ms")), 2),
            "input_tokens": self._as_int(call_record.get("input_tokens")),
            "output_tokens": self._as_int(call_record.get("output_tokens")),
            "total_tokens": self._as_int(call_record.get("total_tokens")),
            "input_cost_usd": round(self._as_float(call_record.get("input_cost_usd")), 8),
            "output_cost_usd": round(self._as_float(call_record.get("output_cost_usd")), 8),
            "total_cost_usd": round(self._as_float(call_record.get("total_cost_usd")), 8),
            "component": self._as_text(call_record.get("component")),
            "caller_module": self._as_text(call_record.get("caller_module")),
            "caller_function": self._as_text(call_record.get("caller_function")),
            "route": self._as_text(call_record.get("route")),
            "error": self._as_text(call_record.get("error")),
        }
        calls.append(normalized)

    def _build_llm_turn_summary(self, llm_calls: list[dict[str, Any]]) -> dict[str, Any]:
        calls = llm_calls if isinstance(llm_calls, list) else []
        total_duration = 0.0
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_cost = 0.0
        for item in calls:
            if not isinstance(item, dict):
                continue
            total_duration += self._as_float(item.get("duration_ms"))
            total_input_tokens += self._as_int(item.get("input_tokens"))
            total_output_tokens += self._as_int(item.get("output_tokens"))
            total_tokens += self._as_int(item.get("total_tokens"))
            total_cost += self._as_float(item.get("total_cost_usd"))

        return {
            "llm_call_count": len(calls),
            "total_duration_ms": round(total_duration, 2),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 8),
            "calls": calls,
        }

    def _log_turn_llm_summary(
        self,
        *,
        event_name: str,
        request_message: str,
        llm_summary: dict[str, Any],
        db_fallback: bool,
        error: str,
    ) -> None:
        if not self.turn_llm_summary_enabled:
            return
        if self._turn_llm_summary_logger is None:
            return
        ctx = self.get_turn_context()
        if not ctx:
            return

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_name,
            "turn_trace_id": self._as_text(ctx.get("turn_trace_id")),
            "api_trace_id": self._as_text(ctx.get("api_trace_id")),
            "session_id": self._as_text(ctx.get("session_id")),
            "hotel_code": self._as_text(ctx.get("hotel_code")),
            "channel": self._as_text(ctx.get("channel")),
            "phase": {
                "id": self._as_text(ctx.get("phase_id")),
                "name": self._as_text(ctx.get("phase_name")),
            },
            "request_message": self._as_text(request_message),
            "db_fallback": bool(db_fallback),
            "error": self._as_text(error),
            "llm_summary": llm_summary if isinstance(llm_summary, dict) else {},
        }

        try:
            safe_record = self._coerce_json_safe(record)
            safe_record = self._truncate_large_strings(safe_record)
            line = json.dumps(safe_record, ensure_ascii=False)
            with self._lock:
                self._turn_llm_summary_logger.info(line)
        except Exception:
            return

    def log_llm_trace(self, trace_payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if not self.get_turn_context():
            return
        trace = trace_payload if isinstance(trace_payload, dict) else {}
        llm_event = {
            "why_called": {
                "caller_module": str(((trace.get("caller") or {}).get("caller_module") or "")).strip(),
                "caller_function": str(((trace.get("caller") or {}).get("caller_function") or "")).strip(),
                "agent": str(((trace.get("trace_actor") or {}).get("agent") or "")).strip(),
                "responder_type": str(((trace.get("trace_actor") or {}).get("responder_type") or "")).strip(),
                "answered_by": str(((trace.get("trace_actor") or {}).get("answered_by") or "")).strip(),
            },
            "llm": {
                "method": str(trace.get("method") or "").strip(),
                "model": str(trace.get("model") or "").strip(),
                "temperature": trace.get("temperature"),
                "max_tokens": trace.get("max_tokens"),
                "status": str(trace.get("status") or "").strip(),
                "duration_ms": trace.get("duration_ms"),
                "usage": trace.get("usage") or {},
            },
            "inputs": trace.get("inputs") or {},
            "user_query": str(trace.get("user_query") or "").strip(),
            "output": trace.get("output"),
            "raw_output": trace.get("raw_output"),
            "error": str(trace.get("error") or "").strip(),
            "trace_context": trace.get("trace_context") or {},
            "service_llm": trace.get("service_llm") or {},
            "request_id": str(trace.get("request_id") or "").strip(),
        }
        self.log_event("llm_call", llm_event)

    def log_orchestration_decision(self, decision_record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if not self.get_turn_context():
            return
        self.log_event("orchestration_decision", decision_record or {})

    def log_turn_end(
        self,
        *,
        request: Any,
        response: Any | None,
        db_fallback: bool = False,
        error: str = "",
    ) -> None:
        if not self.enabled:
            return
        request_meta = request.metadata if isinstance(getattr(request, "metadata", None), dict) else {}
        response_meta = (
            response.metadata if response is not None and isinstance(getattr(response, "metadata", None), dict)
            else {}
        )
        resolved_phase_id = self._normalize_phase_identifier(
            response_meta.get("current_phase_id")
            or response_meta.get("phase")
            or response_meta.get("phase_id")
            or response_meta.get("phase_gate_current_phase_id")
            or request_meta.get("phase")
            or request_meta.get("journey_phase")
        )
        resolved_phase_name = str(
            response_meta.get("current_phase_name")
            or response_meta.get("phase_name")
            or ""
        ).strip()
        self.update_turn_context(phase_id=resolved_phase_id, phase_name=resolved_phase_name)
        ctx = self.get_turn_context()
        llm_calls = []
        if isinstance(ctx.get("_llm_call_metrics"), list):
            llm_calls = list(ctx.get("_llm_call_metrics"))
        llm_summary = self._build_llm_turn_summary(llm_calls)
        request_message = self._as_text(getattr(request, "message", ""))

        payload = {
            "request": {
                "message": request_message,
                "metadata": request_meta,
            },
            "response": {
                "message": self._as_text(getattr(response, "message", "") if response is not None else ""),
                "state": self._as_text(getattr(getattr(response, "state", None), "value", getattr(response, "state", "")) if response is not None else ""),
                "intent": self._as_text(getattr(getattr(response, "intent", None), "value", getattr(response, "intent", "")) if response is not None else ""),
                "confidence": (getattr(response, "confidence", None) if response is not None else None),
                "metadata": response_meta,
            },
            "routing_summary": {
                "response_source": self._as_text(response_meta.get("response_source")),
                "routing_path": self._as_text(response_meta.get("routing_path")),
                "orchestration_trace_id": self._as_text(response_meta.get("orchestration_trace_id")),
                "full_kb_trace_id": self._as_text(response_meta.get("full_kb_trace_id")),
                "service_llm_label": self._as_text(response_meta.get("service_llm_label")),
                "service_resolution_source": self._as_text(response_meta.get("service_resolution_source")),
            },
            "db_fallback": bool(db_fallback),
            "error": self._as_text(error),
            "llm_summary": llm_summary,
        }
        event_name = "turn_error" if error else "turn_end"
        self.log_event(event_name, payload)
        self._log_turn_llm_summary(
            event_name=event_name,
            request_message=request_message,
            llm_summary=llm_summary,
            db_fallback=bool(db_fallback),
            error=self._as_text(error),
        )


turn_diagnostics_service = TurnDiagnosticsService()
