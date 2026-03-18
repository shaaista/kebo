"""
Conversation Audit Service

Writes one structured JSONL record per chat turn so admins can review:
- user/bot messages
- phase details
- enabled services snapshot
- ticketing outcomes and metadata
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from schemas.chat import ChatRequest, ChatResponse
from services.config_service import config_service
from config.settings import settings


class ConversationAuditService:
    """Append-only chat turn logger."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "conversation_audit_enabled", True))
        self.log_file = Path(
            getattr(settings, "conversation_audit_log_file", "./logs/conversation_audit.jsonl")
        )
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_phase_identifier(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        aliases = {
            "prebooking": "pre_booking",
            "booking": "pre_checkin",
            "precheckin": "pre_checkin",
            "duringstay": "during_stay",
            "instay": "during_stay",
            "in_stay": "during_stay",
            "postcheckout": "post_checkout",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): ConversationAuditService._sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ConversationAuditService._sanitize(v) for v in value]
        return str(value)

    @staticmethod
    def _as_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _enum_value(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip()

    def _resolve_phase(self, request: ChatRequest, response: ChatResponse) -> dict[str, Any]:
        metadata = response.metadata if isinstance(response.metadata, dict) else {}
        request_meta = request.metadata if isinstance(request.metadata, dict) else {}

        requested_phase = self._normalize_phase_identifier(
            request_meta.get("phase")
            or request_meta.get("journey_phase")
            or (request_meta.get("_integration") or {}).get("phase")
        )
        response_phase = self._normalize_phase_identifier(
            metadata.get("phase")
            or metadata.get("current_phase_id")
            or metadata.get("phase_gate_current_phase_id")
            or metadata.get("phase_id")
        )
        resolved_phase = response_phase or requested_phase

        phase_name = ""
        try:
            for phase in config_service.get_journey_phases():
                if not isinstance(phase, dict):
                    continue
                phase_id = self._normalize_phase_identifier(phase.get("id"))
                if phase_id == resolved_phase:
                    phase_name = self._as_text(phase.get("name"))
                    break
        except Exception:
            phase_name = ""

        return {
            "requested_phase_id": requested_phase,
            "response_phase_id": response_phase,
            "resolved_phase_id": resolved_phase,
            "resolved_phase_name": phase_name,
            "phase_gate_current_phase_id": self._normalize_phase_identifier(
                metadata.get("phase_gate_current_phase_id")
            ),
            "phase_gate_service_phase_id": self._normalize_phase_identifier(
                metadata.get("phase_gate_service_phase_id")
            ),
        }

    def _services_snapshot(self, phase_id: str = "") -> dict[str, Any]:
        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            services = []

        normalized_phase = self._normalize_phase_identifier(phase_id)
        active_services: list[dict[str, Any]] = []
        active_phase_services: list[dict[str, Any]] = []

        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            row = {
                "id": self._as_text(service.get("id")),
                "name": self._as_text(service.get("name")),
                "phase_id": self._normalize_phase_identifier(service.get("phase_id")),
                "ticketing_enabled": bool(service.get("ticketing_enabled", True)),
            }
            active_services.append(row)
            if normalized_phase and row["phase_id"] == normalized_phase:
                active_phase_services.append(row)

        return {
            "active_total_count": len(active_services),
            "active_service_ids": [item["id"] for item in active_services if item.get("id")],
            "phase_active_count": len(active_phase_services),
            "phase_active_service_ids": [
                item["id"] for item in active_phase_services if item.get("id")
            ],
            "phase_active_services": active_phase_services,
        }

    def _ticketing_summary(self, response: ChatResponse) -> dict[str, Any]:
        metadata = response.metadata if isinstance(response.metadata, dict) else {}
        ticket_fields = {
            str(k): self._sanitize(v)
            for k, v in metadata.items()
            if "ticket" in str(k).lower()
        }

        ticket_id_candidates = []
        for key, value in ticket_fields.items():
            key_lower = key.lower()
            if "ticket_id" in key_lower or key_lower in {"ticket", "ticketid"}:
                text_value = self._as_text(value)
                if text_value:
                    ticket_id_candidates.append(text_value)

        created = bool(metadata.get("ticket_created", False))
        if not created and ticket_id_candidates:
            created = not bool(metadata.get("ticket_create_failed", False)) and not bool(
                metadata.get("ticket_create_skipped", False)
            )

        return {
            "created": created,
            "ticket_ids": ticket_id_candidates,
            "ticket_fields": ticket_fields,
        }

    def _build_record(
        self,
        *,
        request: ChatRequest,
        response: ChatResponse | None,
        trace_id: str = "",
        db_fallback: bool = False,
        error: str = "",
    ) -> dict[str, Any]:
        request_meta = request.metadata if isinstance(request.metadata, dict) else {}
        response_meta = (
            response.metadata if response is not None and isinstance(response.metadata, dict) else {}
        )

        channel = (
            self._as_text(request.channel)
            or self._as_text(request_meta.get("channel"))
            or "web"
        )

        phase_payload = (
            self._resolve_phase(request, response)
            if response is not None
            else {
                "requested_phase_id": self._normalize_phase_identifier(
                    request_meta.get("phase") or request_meta.get("journey_phase")
                ),
                "response_phase_id": "",
                "resolved_phase_id": "",
                "resolved_phase_name": "",
                "phase_gate_current_phase_id": "",
                "phase_gate_service_phase_id": "",
            }
        )
        services_snapshot = self._services_snapshot(phase_payload.get("resolved_phase_id", ""))

        ticketing_summary = (
            self._ticketing_summary(response) if response is not None else {"created": False, "ticket_ids": [], "ticket_fields": {}}
        )

        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "trace_id": self._as_text(trace_id),
            "turn_trace_id": self._as_text(
                request_meta.get("turn_trace_id")
                or response_meta.get("turn_trace_id")
            ),
            "session_id": self._as_text(request.session_id),
            "hotel_code": self._as_text(request.hotel_code),
            "channel": channel,
            "db_fallback": bool(db_fallback),
            "error": self._as_text(error),
            "conversation": {
                "user_message": self._as_text(request.message),
                "bot_message": self._as_text(response.message if response is not None else ""),
            },
            "intent": self._as_text((response.metadata or {}).get("classified_intent")) if response is not None else "",
            "state": self._enum_value(response.state) if response is not None else "",
            "phase": phase_payload,
            "services_enabled": services_snapshot,
            "ticketing": ticketing_summary,
            "routing": {
                "routing_path": self._as_text(response_meta.get("routing_path")),
                "response_source": self._as_text(response_meta.get("response_source")),
                "orchestration_trace_id": self._as_text(response_meta.get("orchestration_trace_id")),
                "full_kb_trace_id": self._as_text(response_meta.get("full_kb_trace_id")),
            },
            "request_metadata": self._sanitize(request_meta),
            "response_metadata": self._sanitize(response_meta),
        }

    def _append_record(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(self._sanitize(record), ensure_ascii=False)
            with self._lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            return

    def log_turn(
        self,
        *,
        request: ChatRequest,
        response: ChatResponse,
        trace_id: str = "",
        db_fallback: bool = False,
    ) -> None:
        """Log a successful chat turn."""
        record = self._build_record(
            request=request,
            response=response,
            trace_id=trace_id,
            db_fallback=db_fallback,
        )
        self._append_record(record)

    def log_failed_turn(
        self,
        *,
        request: ChatRequest,
        trace_id: str = "",
        error: str = "",
        db_fallback: bool = False,
    ) -> None:
        """Log a failed chat turn."""
        record = self._build_record(
            request=request,
            response=None,
            trace_id=trace_id,
            db_fallback=db_fallback,
            error=error,
        )
        self._append_record(record)


conversation_audit_service = ConversationAuditService()
