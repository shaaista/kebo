"""
Ticketing Service

Lumira-compatible ticketing integration:
- Create ticket: POST  {TICKETING_BASE_URL}/insert/ticket.htm
- Update ticket: PATCH {TICKETING_BASE_URL}/insert/ticket/{id}.htm
- Agent handoff: POST  {AGENT_HANDOFF_API_URL}
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Iterable

import httpx

from config.settings import settings
from schemas.chat import ConversationContext
from services.config_service import config_service

logger = logging.getLogger(__name__)


@dataclass
class TicketingResult:
    """Normalized result returned by all ticketing operations."""

    success: bool
    ticket_id: str = ""
    assigned_id: str = ""
    status_code: int | None = None
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)


class TicketingService:
    """Async client for external ticketing + handoff APIs."""

    _DUPLICATE_CREATE_WINDOW_SECONDS = 120

    _LOCAL_TICKET_CSV_HEADERS: tuple[str, ...] = (
        "id",
        "ticket_id",
        "session_id",
        "guest_id",
        "entity_id",
        "fms_entity_id",
        "issue",
        "message_id",
        "sentiment_score",
        "department_alloc",
        "priority",
        "category",
        "phase",
        "status",
        "sla_due",
        "escalation_stage",
        "created_at",
        "updated_at",
        "room_number",
        "message",
        "customer_feedback",
        "manager_notes",
        "assignee_notes",
        "assigned_to",
        "assigned_id",
        "customer_rating",
        "department_id",
        "sla_due_at",
        "sla_duration_minutes",
        "ticket_auto_assign",
        "closed_at",
        "outlet_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost",
        "sub_category",
        "guest_name",
        "property_name",
        "room_type",
        "compensation_type",
        "compensation_currency",
        "compensation_amount",
        "group_id",
        "ticket_source",
        "cancelled_notes",
        "mode",
        "local_created_at_utc",
        "local_updated_at_utc",
        "payload_json",
        "response_json",
    )

    def __init__(self) -> None:
        self._recent_create_cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _ticket_debug_enabled() -> bool:
        return bool(getattr(settings, "ticketing_debug_log_enabled", True))

    @staticmethod
    def _ticket_debug_path() -> Path:
        raw = str(getattr(settings, "ticketing_debug_log_file", "") or "").strip()
        if not raw:
            raw = "./logs/ticketing_debug.jsonl"
        return Path(raw)

    @classmethod
    def _sanitize_debug_value(cls, value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return str(value)[:400]
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:1800]
        if isinstance(value, list):
            return [cls._sanitize_debug_value(item, depth + 1) for item in value[:60]]
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:100]:
                key = str(raw_key)[:100]
                normalized[key] = cls._sanitize_debug_value(raw_value, depth + 1)
            return normalized
        return str(value)[:1800]

    @classmethod
    def _write_ticket_debug_event(cls, event: str, **data: Any) -> None:
        if not cls._ticket_debug_enabled():
            return
        path = cls._ticket_debug_path()
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": str(event or "").strip() or "unknown_event",
        }
        for key, value in data.items():
            payload[str(key)] = cls._sanitize_debug_value(value)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")
        except Exception:
            logger.exception("Failed writing ticket debug log event=%s path=%s", event, path)

    @staticmethod
    def _ticket_duplicate_window_seconds() -> int:
        raw = getattr(settings, "ticketing_duplicate_window_seconds", 120)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = TicketingService._DUPLICATE_CREATE_WINDOW_SECONDS
        return max(0, value)

    def _cleanup_recent_create_cache(self) -> None:
        if not self._recent_create_cache:
            return
        window_seconds = self._ticket_duplicate_window_seconds()
        if window_seconds <= 0:
            self._recent_create_cache.clear()
            return
        now_ts = datetime.now(UTC).timestamp()
        stale_keys = [
            key
            for key, record in self._recent_create_cache.items()
            if (now_ts - float(record.get("created_ts") or 0.0)) > window_seconds
        ]
        for key in stale_keys:
            self._recent_create_cache.pop(key, None)

    @classmethod
    def _build_create_dedupe_fingerprint(cls, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        normalized = {
            "session_id": cls._safe_str(payload.get("session_id")).lower(),
            "guest_id": cls._safe_str(payload.get("guest_id")).lower(),
            "entity_id": cls._safe_str(
                payload.get("entity_id")
                or payload.get("organisation_id")
                or payload.get("organization_id")
                or payload.get("org_id")
            ).lower(),
            "room_number": cls._safe_str(payload.get("room_number")).lower(),
            "issue": cls._safe_str(payload.get("issue")).lower(),
            "phase": cls._safe_str(payload.get("phase")).lower(),
            "category": cls._safe_str(payload.get("categorization") or payload.get("category")).lower(),
            "sub_category": cls._safe_str(
                payload.get("sub_categorization") or payload.get("sub_category")
            ).lower(),
            "service_id": cls._safe_str(payload.get("service_id") or payload.get("service")).lower(),
        }
        if not normalized["issue"]:
            return ""
        serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    def _find_recent_duplicate_result(self, payload: dict[str, Any]) -> TicketingResult | None:
        self._cleanup_recent_create_cache()
        fingerprint = self._build_create_dedupe_fingerprint(payload)
        if not fingerprint:
            return None
        cached = self._recent_create_cache.get(fingerprint)
        if not isinstance(cached, dict):
            return None
        response = dict(cached.get("response") or {})
        response["duplicate_suppressed"] = True
        return TicketingResult(
            success=True,
            ticket_id=self._safe_str(cached.get("ticket_id")),
            assigned_id=self._safe_str(cached.get("assigned_id")),
            status_code=int(cached.get("status_code") or 200),
            payload=dict(payload or {}),
            response=response,
        )

    def _remember_recent_create_result(self, payload: dict[str, Any], result: TicketingResult) -> None:
        if not isinstance(payload, dict):
            return
        if not bool(result and result.success):
            return
        ticket_id = self._safe_str(result.ticket_id)
        if not ticket_id:
            return
        fingerprint = self._build_create_dedupe_fingerprint(payload)
        if not fingerprint:
            return
        self._recent_create_cache[fingerprint] = {
            "created_ts": datetime.now(UTC).timestamp(),
            "ticket_id": ticket_id,
            "assigned_id": self._safe_str(result.assigned_id),
            "status_code": int(result.status_code or 200),
            "response": dict(result.response or {}),
        }

    def is_ticketing_enabled(self, capabilities: dict[str, Any] | None = None) -> bool:
        """Ticketing is enabled only when endpoint exists and tool toggle allows it."""
        if not bool(getattr(settings, "ticketing_plugin_enabled", True)):
            return False
        if self._use_local_mode():
            return True
        if not self._ticketing_base_url():
            return False

        tools: Iterable[dict[str, Any]] = []
        if isinstance(capabilities, dict):
            tools = capabilities.get("tools", []) or []
        if not tools:
            tools = config_service.get_tools()

        ticket_tool: dict[str, Any] | None = None
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = self._normalize_identifier(tool.get("id"))
            if tool_id in {"ticketing", "ticket_create"}:
                ticket_tool = tool
                break

        # If no explicit tool row exists, keep endpoint-driven enablement.
        if ticket_tool is None:
            return True
        return bool(ticket_tool.get("enabled", False))

    def is_handoff_enabled(self, capabilities: dict[str, Any] | None = None) -> bool:
        """Handoff requires AGENT_HANDOFF_API_URL and enabled handoff tool/capability."""
        if not str(settings.agent_handoff_api_url or "").strip():
            return False

        if not config_service.is_capability_enabled("human_escalation"):
            return False

        tools: Iterable[dict[str, Any]] = []
        if isinstance(capabilities, dict):
            tools = capabilities.get("tools", []) or []
        if not tools:
            tools = config_service.get_tools()

        handoff_tool_enabled_values: list[bool] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = self._normalize_identifier(tool.get("id"))
            if tool_id in {"human_handoff", "live_chat", "callback", "email_followup"}:
                handoff_tool_enabled_values.append(bool(tool.get("enabled", False)))

        # If no explicit tool row exists, allow handoff by capability + endpoint.
        if not handoff_tool_enabled_values:
            return True
        return any(handoff_tool_enabled_values)

    async def create_ticket(self, payload: dict[str, Any]) -> TicketingResult:
        """Create a ticket using Lumira-compatible payload contract."""
        debug_context = {
            "operation": "create_ticket",
            "mode": "local" if self._use_local_mode() else "api",
            "ticketing_base_url": self._ticketing_base_url(),
            "ticketing_create_path": str(settings.ticketing_create_path or "/insert/ticket.htm").strip(),
            "local_store_path": str(self._local_store_path()),
            "local_csv_path": str(self._local_csv_path()),
        }
        self._write_ticket_debug_event(
            "ticket_create_requested",
            payload=payload,
            context=debug_context,
        )
        payload_effective = dict(payload or {})
        if not self._use_local_mode():
            payload_effective = await self._apply_department_mapping(payload_effective)
        self._write_ticket_debug_event(
            "ticket_create_payload_resolved",
            payload=payload_effective,
            context=debug_context,
        )

        gate = self.detect_service_ticketing_disabled(payload_effective)
        if gate is not None:
            response_payload = {
                "skip_reason": "phase_service_ticketing_disabled",
                **gate,
            }
            self._write_ticket_debug_event(
                "ticket_create_blocked",
                reason="phase_service_ticketing_disabled",
                gate=gate,
                payload=payload_effective,
                context=debug_context,
            )
            return TicketingResult(
                success=False,
                status_code=409,
                error="phase_service_ticketing_disabled",
                payload=dict(payload_effective or {}),
                response=response_payload,
            )

        duplicate_result = self._find_recent_duplicate_result(payload_effective)
        if duplicate_result is not None:
            self._write_ticket_debug_event(
                "ticket_create_deduplicated",
                ticket_id=duplicate_result.ticket_id,
                assigned_id=duplicate_result.assigned_id,
                payload=payload_effective,
                context=debug_context,
            )
            return duplicate_result

        if self._use_local_mode():
            result = await self._create_ticket_local(payload_effective)
            logger.info(
                "Ticket create completed in local mode: success=%s ticket_id=%s error=%s",
                result.success,
                result.ticket_id,
                self._safe_str(result.error),
            )
            self._write_ticket_debug_event(
                "ticket_create_local_completed",
                success=result.success,
                ticket_id=result.ticket_id,
                status_code=result.status_code,
                error=result.error,
                result_response=result.response,
                context=debug_context,
            )
            self._remember_recent_create_result(payload_effective, result)
            return result

        base_url = self._ticketing_base_url()
        if not base_url:
            self._write_ticket_debug_event(
                "ticket_create_failed_missing_base_url",
                error="TICKETING_BASE_URL is not configured",
                payload=payload_effective,
                context=debug_context,
            )
            return TicketingResult(success=False, error="TICKETING_BASE_URL is not configured")

        path = str(settings.ticketing_create_path or "/insert/ticket.htm").strip() or "/insert/ticket.htm"
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"
        result = await self._request_json(
            "POST",
            url,
            payload_effective,
            debug_context=debug_context,
        )
        self._write_ticket_debug_event(
            "ticket_create_api_completed",
            success=result.success,
            ticket_id=result.ticket_id,
            assigned_id=result.assigned_id,
            status_code=result.status_code,
            error=result.error,
            result_response=result.response,
            context={**debug_context, "request_url": url},
        )

        # If the external API failed (5xx or any non-success), fall back to local mode
        if not result.success:
            fallback_reason = (
                f"HTTP {result.status_code}" if result.status_code is not None
                else "request_failed"
            )
            if not self._allow_api_failure_local_fallback():
                logger.warning(
                    "External ticketing API failed (%s) — returning explicit failure (local fallback disabled)",
                    fallback_reason,
                )
                self._write_ticket_debug_event(
                    "ticket_create_api_fallback_skipped",
                    api_status_code=result.status_code,
                    fallback_reason=fallback_reason,
                    payload=payload_effective,
                    context=debug_context,
                )
                self._remember_recent_create_result(payload_effective, result)
                return result
            logger.warning(
                "External ticketing API failed (%s) — falling back to local mode",
                fallback_reason,
            )
            self._write_ticket_debug_event(
                "ticket_create_api_fallback_to_local",
                api_status_code=result.status_code,
                fallback_reason=fallback_reason,
                payload=payload_effective,
                context=debug_context,
            )
            try:
                result = await self._create_ticket_local(payload_effective)
            except Exception as local_exc:
                logger.exception("Local ticket fallback also failed")
                result = TicketingResult(
                    success=False,
                    error=f"Ticket creation failed (API and local fallback): {local_exc}",
                    payload=dict(payload_effective or {}),
                )
            self._write_ticket_debug_event(
                "ticket_create_local_fallback_completed",
                success=result.success,
                ticket_id=result.ticket_id,
                error=result.error,
                context=debug_context,
            )

        self._remember_recent_create_result(payload_effective, result)
        return result

    async def update_ticket(
        self,
        ticket_id: str,
        manager_notes: str,
        extra_fields: dict[str, Any] | None = None,
    ) -> TicketingResult:
        """Update an existing ticket (manager notes and optional additional fields)."""
        debug_context = {
            "operation": "update_ticket",
            "mode": "local" if self._use_local_mode() else "api",
            "ticketing_base_url": self._ticketing_base_url(),
            "ticketing_update_path_template": str(
                settings.ticketing_update_path_template or "/insert/ticket/{ticket_id}.htm"
            ).strip(),
            "local_store_path": str(self._local_store_path()),
            "local_csv_path": str(self._local_csv_path()),
        }
        tid = str(ticket_id or "").strip()
        notes = str(manager_notes or "").strip()
        if not tid:
            self._write_ticket_debug_event(
                "ticket_update_rejected",
                reason="ticket_id is required",
                ticket_id=ticket_id,
                context=debug_context,
            )
            return TicketingResult(success=False, error="ticket_id is required")
        if not notes:
            self._write_ticket_debug_event(
                "ticket_update_rejected",
                reason="manager_notes is required",
                ticket_id=ticket_id,
                context=debug_context,
            )
            return TicketingResult(success=False, error="manager_notes is required")
        self._write_ticket_debug_event(
            "ticket_update_requested",
            ticket_id=tid,
            manager_notes=notes,
            extra_fields=extra_fields or {},
            context=debug_context,
        )
        if self._use_local_mode():
            result = await self._update_ticket_local(tid, notes, extra_fields=extra_fields)
            logger.info(
                "Ticket update completed in local mode: success=%s ticket_id=%s error=%s",
                result.success,
                result.ticket_id or tid,
                self._safe_str(result.error),
            )
            self._write_ticket_debug_event(
                "ticket_update_local_completed",
                success=result.success,
                ticket_id=result.ticket_id or tid,
                status_code=result.status_code,
                error=result.error,
                result_response=result.response,
                context=debug_context,
            )
            return result
        base_url = self._ticketing_base_url()
        if not base_url:
            self._write_ticket_debug_event(
                "ticket_update_failed_missing_base_url",
                ticket_id=tid,
                error="TICKETING_BASE_URL is not configured",
                context=debug_context,
            )
            return TicketingResult(success=False, error="TICKETING_BASE_URL is not configured")

        path_template = (
            str(settings.ticketing_update_path_template or "/insert/ticket/{ticket_id}.htm").strip()
            or "/insert/ticket/{ticket_id}.htm"
        )
        try:
            path = path_template.format(ticket_id=tid, id=tid)
        except Exception:
            path = f"/insert/ticket/{tid}.htm"
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"

        body: dict[str, Any] = {"manager_notes": notes}
        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if key not in body and value is not None:
                    body[key] = value

        result = await self._request_json(
            "PATCH",
            url,
            body,
            debug_context={**debug_context, "ticket_id": tid},
        )
        if not result.ticket_id:
            result.ticket_id = tid
        self._write_ticket_debug_event(
            "ticket_update_api_completed",
            success=result.success,
            ticket_id=result.ticket_id,
            assigned_id=result.assigned_id,
            status_code=result.status_code,
            error=result.error,
            result_response=result.response,
            context={**debug_context, "request_url": url, "ticket_id": tid},
        )
        return result

    async def handoff_to_agent(
        self,
        conversation_id: str,
        session_id: str,
        reason: str,
        agent_id: str,
    ) -> TicketingResult:
        """Trigger human-agent handoff API."""
        url = str(settings.agent_handoff_api_url or "").strip()
        if not url:
            self._write_ticket_debug_event(
                "handoff_rejected_missing_url",
                error="AGENT_HANDOFF_API_URL is not configured",
                conversation_id=conversation_id,
                session_id=session_id,
            )
            return TicketingResult(success=False, error="AGENT_HANDOFF_API_URL is not configured")

        payload = {
            "from_responder": "BOT",
            "to_responder": "AGENT",
            "conversation_id": str(conversation_id or "").strip(),
            "session_id": str(session_id or "").strip(),
            "to_agent_id": str(agent_id or "").strip(),
            "reason": str(reason or "").strip(),
        }
        self._write_ticket_debug_event(
            "handoff_requested",
            payload=payload,
            request_url=url,
        )
        result = await self._request_json(
            "POST",
            url,
            payload,
            debug_context={
                "operation": "handoff_to_agent",
                "request_url": url,
                "conversation_id": str(conversation_id or "").strip(),
                "session_id": str(session_id or "").strip(),
            },
        )
        self._write_ticket_debug_event(
            "handoff_completed",
            success=result.success,
            status_code=result.status_code,
            error=result.error,
            result_response=result.response,
            request_url=url,
            conversation_id=str(conversation_id or "").strip(),
            session_id=str(session_id or "").strip(),
        )
        return result

    def build_lumira_ticket_payload(
        self,
        context: ConversationContext,
        issue: str,
        message: str,
        *,
        category: str = "complaint",
        sub_category: str = "",
        priority: str = "medium",
        department_id: str = "",
        department_manager: str = "",
        manager_notes: str = "",
        phase: str = "",
        sla_due_time: str = "",
        outlet_id: str = "",
        assigned_to: str = "",
        sentiment_score: str = "",
        source: str = "",
        group_id: str | int | None = None,
        message_id: str | int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cost: float | None = None,
        ticket_status: str = "open",
    ) -> dict[str, Any]:
        """
        Build a Lumira-style ticket payload from context + normalized fields.
        """
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = self.get_integration_context(context)
        latest_ticket = self.get_latest_ticket(context)
        phase_value = self._normalize_phase(
            phase
            or integration.get("phase")
            or self._default_phase_for_context(integration)
        )

        guest_id = self._first_non_empty(
            integration.get("guest_id"),
            pending.get("guest_id"),
            latest_ticket.get("guest_id"),
        )
        channel_user_id = self._first_non_empty(
            integration.get("user_id"),
            pending.get("user_id"),
            integration.get("wa_number"),
            pending.get("wa_number"),
        )
        if guest_id and channel_user_id and guest_id != channel_user_id:
            logger.warning(
                "Ticket identity mismatch: guest_id and user_id differ (session_id=%s guest_id=%s user_id=%s)",
                str(context.session_id or "").strip(),
                guest_id,
                self._mask_identifier(channel_user_id),
            )
        if not guest_id:
            logger.warning(
                "Missing real guest_id; attempting synthetic fallback (session_id=%s entity_id=%s user_id=%s phone=%s)",
                str(context.session_id or "").strip(),
                str(integration.get("entity_id") or integration.get("organisation_id") or "").strip(),
                self._mask_identifier(channel_user_id),
                self._mask_identifier(
                    pending.get("guest_phone")
                    or integration.get("guest_phone")
                    or context.guest_phone
                ),
            )
            guest_id = self._derive_generated_guest_id(
                context=context,
                integration=integration,
                pending=pending,
            )
            if guest_id and isinstance(context.pending_data, dict):
                integration_snapshot = context.pending_data.get("_integration", {})
                integration_update = (
                    dict(integration_snapshot) if isinstance(integration_snapshot, dict) else {}
                )
                integration_update["guest_id"] = guest_id
                context.pending_data["_integration"] = integration_update
                context.pending_data.setdefault("guest_id", guest_id)
                integration = integration_update
                logger.warning(
                    "Using synthetic guest_id fallback (session_id=%s guest_id=%s)",
                    str(context.session_id or "").strip(),
                    guest_id,
                )
        organisation_id = self._first_non_empty(
            integration.get("organisation_id"),
            integration.get("organization_id"),
            integration.get("org_id"),
            integration.get("entity_id"),
            pending.get("organisation_id"),
            pending.get("organization_id"),
            pending.get("org_id"),
            pending.get("entity_id"),
            latest_ticket.get("organisation_id"),
            latest_ticket.get("organization_id"),
            latest_ticket.get("org_id"),
            latest_ticket.get("entity_id"),
            context.hotel_code,
        )
        room_number = self._first_non_empty(
            context.room_number,
            pending.get("room_number"),
            integration.get("room_number"),
            latest_ticket.get("room_number"),
        )

        now_utc = datetime.now(UTC).strftime("%H:%M:%S %d-%m-%Y")
        issue_text = str(issue or "").strip() or str(message or "").strip()
        if not room_number:
            room_number = self._extract_room_number_from_text(issue_text)
        if not room_number:
            room_number = self._extract_room_number_from_text(message)

        department_allocated = self._first_non_empty(
            department_id,
            pending.get("department_id"),
            pending.get("ticket_department_id"),
            integration.get("department_id"),
            integration.get("department_allocated"),
            latest_ticket.get("department_id"),
            latest_ticket.get("department_allocated"),
        )
        department_manager_value = self._first_non_empty(
            department_manager,
            pending.get("department_head"),
            pending.get("department_manager"),
            integration.get("department_head"),
            integration.get("department_manager"),
            latest_ticket.get("department_manager"),
        )
        sentiment_value = self._first_non_empty(
            sentiment_score,
            pending.get("sentiment_score"),
            pending.get("ticket_sentiment_score"),
            integration.get("sentiment_score"),
            latest_ticket.get("sentiment_score"),
        )
        message_text = self._build_ticket_message_context(
            context=context,
            explicit_message=message,
            issue_text=issue_text,
        )
        source_value = self._resolve_ticket_source(
            explicit_source=source,
            integration=integration,
            channel=context.channel,
        )
        priority_value = self._normalize_priority(priority)
        category_value = self._normalize_category(category)
        status_value = self._normalize_ticket_status(ticket_status)

        # Resolve guest name from form data, pending data, integration, or context
        guest_name = self._first_non_empty(
            pending.get("guest_name"),
            pending.get("full_name"),
            pending.get("name"),
            integration.get("guest_name"),
            context.guest_name,
        )

        # Resolve property name and room type from pending data / context
        property_name = self._first_non_empty(
            pending.get("property_name"),
            pending.get("property"),
            pending.get("hotel_name"),
            integration.get("property_name"),
            getattr(context, "booking_property_name", None),
            pending.get("_selected_property_scope_name"),
        )
        if not property_name:
            _active_names = pending.get("_active_property_names")
            if isinstance(_active_names, list) and len(_active_names) == 1:
                property_name = str(_active_names[0]).strip() or None
        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_category"),
            integration.get("room_type"),
            getattr(context, "booking_room_type", None),
        )

        payload = {
            "guest_id": str(guest_id or "").strip(),
            "room_number": str(room_number or "").strip(),
            "organisation_id": str(organisation_id or "").strip(),
            "issue": issue_text,
            "message": message_text,
            "sentiment_score": str(sentiment_value or "").strip(),
            "department_allocated": str(department_allocated or "").strip(),
            "department_manager": str(department_manager_value or "").strip(),
            "assigned_to": str(assigned_to or "").strip(),
            "priority": priority_value,
            "categorization": category_value,
            "sub_categorization": str(sub_category or "").strip(),
            "manager_notes": str(manager_notes or "").strip(),
            "created_at": now_utc,
            "session_id": str(context.session_id or "").strip(),
            "phase": phase_value,
            "ticket_status": status_value,
            "chatBot": "yes",
            "sla_due_time": str(sla_due_time or "").strip(),
            "outlet_id": str(outlet_id or "").strip(),
            "source": source_value,
            "guest_name": str(guest_name or "").strip(),
            "property_name": str(property_name or "").strip(),
            "room_type": str(room_type or "").strip(),
        }

        # Backward-compatible aliases some APIs accept.
        payload["department_id"] = payload["department_allocated"]
        payload["category"] = payload["categorization"]
        payload["sub_category"] = payload["sub_categorization"]
        # Kepsla API expects camelCase variants
        payload["guestName"] = payload["guest_name"]
        payload["propertyName"] = payload["property_name"]
        payload["roomType"] = payload["room_type"]

        resolved_group_id = group_id if group_id is not None else integration.get("group_id")
        resolved_message_id = message_id if message_id is not None else integration.get("message_id")
        if resolved_group_id not in (None, ""):
            payload["group_id"] = resolved_group_id
        if resolved_message_id not in (None, ""):
            payload["message_id"] = resolved_message_id

        if input_tokens is not None:
            payload["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            payload["output_tokens"] = int(output_tokens)
        if total_tokens is not None:
            payload["total_tokens"] = int(total_tokens)
        if cost is not None:
            payload["cost"] = float(cost)

        return payload

    @classmethod
    def _first_non_empty(cls, *values: Any) -> str:
        for value in values:
            text = cls._safe_str(value)
            if text:
                return text
        return ""

    @classmethod
    def _derive_generated_guest_id(
        cls,
        *,
        context: ConversationContext,
        integration: dict[str, Any],
        pending: dict[str, Any],
    ) -> str:
        seed_parts = [
            cls._safe_str(getattr(context, "session_id", "")),
            cls._safe_str(getattr(context, "hotel_code", "")),
            cls._safe_str(integration.get("conversation_id")),
            cls._safe_str(integration.get("group_id")),
            cls._safe_str(
                integration.get("user_id")
                or pending.get("user_id")
                or integration.get("wa_number")
                or pending.get("wa_number")
            ),
            cls._safe_str(
                pending.get("guest_phone")
                or integration.get("guest_phone")
                or integration.get("wa_number")
                or getattr(context, "guest_phone", "")
            ),
        ]
        seed = "|".join(part for part in seed_parts if part)
        if not seed:
            return ""
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        try:
            numeric_value = int(digest[:15], 16) % 900000000 + 100000000
        except (TypeError, ValueError):
            return ""
        return str(numeric_value)

    @classmethod
    def _extract_room_number_from_text(cls, value: Any) -> str:
        text = cls._safe_str(value)
        if not text:
            return ""
        match = re.search(
            r"\broom(?:\s*(?:number|no\.?))?\s*(?:is|=|:)?\s*([A-Za-z0-9-]{2,10})\b",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        candidate = cls._safe_str(match.group(1))
        if not candidate:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9-]{2,10}", candidate):
            return ""
        return candidate.upper()

    def _build_ticket_message_context(
        self,
        *,
        context: ConversationContext,
        explicit_message: str,
        issue_text: str,
        max_messages: int = 12,
        max_chars: int = 3200,
    ) -> str:
        explicit = self._safe_str(explicit_message)
        issue = self._safe_str(issue_text)

        lines: list[str] = []
        for msg in context.get_recent_messages(max_messages):
            role_raw = self._safe_str(getattr(msg, "role", ""))
            role = role_raw.lower()
            if role.endswith("user"):
                prefix = "User"
            elif role.endswith("assistant"):
                prefix = "AI"
            elif role:
                prefix = role.title()
            else:
                prefix = "AI"

            content = self._safe_str(getattr(msg, "content", ""))
            if not content:
                continue
            lines.append(f"{prefix}: {content}")

        # Ensure the latest explicit message and issue summary are both represented.
        if explicit:
            explicit_line = f"User: {explicit}"
            if explicit_line not in lines:
                lines.append(explicit_line)
        if issue:
            issue_line = f"Issue Summary: {issue}"
            if issue_line not in lines:
                lines.append(issue_line)

        if not lines:
            text = explicit or issue
            return text[:max_chars]

        combined = "\n".join(lines).strip()
        if len(combined) <= max_chars:
            return combined

        # Keep the most recent context in bounds.
        trimmed = combined[-max_chars:]
        newline_pos = trimmed.find("\n")
        if 0 <= newline_pos < 120:
            trimmed = trimmed[newline_pos + 1 :]
        return trimmed.strip()

    @staticmethod
    def get_integration_context(context: ConversationContext) -> dict[str, Any]:
        """Read reserved integration context from pending_data."""
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending.get("_integration", {})
        return integration if isinstance(integration, dict) else {}

    def get_latest_ticket(self, context: ConversationContext) -> dict[str, Any]:
        """Fetch latest ticket snapshot from conversation memory/pending data."""
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}

        memory = pending.get("_memory", {})
        if isinstance(memory, dict):
            facts = memory.get("facts", {})
            if isinstance(facts, dict):
                latest = facts.get("latest_ticket")
                if isinstance(latest, dict):
                    return self._normalize_ticket_snapshot(latest)

        latest_from_pending = pending.get("latest_ticket")
        if isinstance(latest_from_pending, dict):
            return self._normalize_ticket_snapshot(latest_from_pending)
        return {}

    @staticmethod
    def _normalize_ticket_snapshot(ticket: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(ticket or {})
        if not normalized.get("id"):
            normalized["id"] = str(
                normalized.get("ticket_id")
                or normalized.get("ticketId")
                or normalized.get("ticketNo")
                or ""
            ).strip()
        if not normalized.get("assigned_id"):
            normalized["assigned_id"] = str(
                normalized.get("assignedId")
                or normalized.get("agent_id")
                or normalized.get("to_agent_id")
                or ""
            ).strip()
        if not normalized.get("department_id"):
            normalized["department_id"] = str(
                normalized.get("ticket_department_id")
                or normalized.get("department_allocated")
                or normalized.get("department")
                or ""
            ).strip()
        return normalized

    async def _apply_department_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Resolve a department for the payload using live entity departments.
        Preference order:
        1) LLM mapping (when available)
        2) Existing explicit department_id from payload (current behavior fallback)
        3) Deterministic keyword/name heuristic fallback
        Also remaps 'categorization' to a valid Kepsla dashboard category
        using the resolved department name as the canonical source.
        """
        payload_next = dict(payload or {})
        existing_department_id = self._safe_str(
            payload_next.get("department_allocated") or payload_next.get("department_id")
        )

        entity_id = self._first_non_empty(
            payload_next.get("entity_id"),
            payload_next.get("organisation_id"),
            payload_next.get("organization_id"),
            payload_next.get("org_id"),
        )
        if not entity_id:
            return payload_next

        try:
            from integrations.lumira_ticketing_repository import lumira_ticketing_repository
            from models.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db_session:
                departments = await lumira_ticketing_repository.fetch_departments_of_entity(
                    db_session,
                    entity_id=entity_id,
                )
        except Exception as exc:
            self._write_ticket_debug_event(
                "ticket_department_mapping_lookup_failed",
                entity_id=entity_id,
                error=str(exc),
            )
            return payload_next

        existing_dept: dict[str, Any] | None = None
        if existing_department_id and existing_department_id not in {"0", "null", "none"}:
            for dept in (departments or []):
                if isinstance(dept, dict) and self._safe_str(dept.get("department_id")) == existing_department_id:
                    existing_dept = dept
                    break
            if existing_dept is None:
                self._write_ticket_debug_event(
                    "ticket_department_mapping_invalid_existing_department_id",
                    entity_id=entity_id,
                    existing_department_id=existing_department_id,
                )

        llm_department = await self._llm_resolve_department_for_ticket_payload(
            payload=payload_next,
            departments=departments,
        )
        matched_department: dict[str, Any] | None = None
        mapping_source = ""

        if isinstance(llm_department, dict):
            matched_department = llm_department
            mapping_source = "llm"
        elif isinstance(existing_dept, dict):
            matched_department = existing_dept
            mapping_source = "existing_department_fallback"
        else:
            matched_department = self._resolve_department_for_ticket_payload(
                payload=payload_next,
                departments=departments,
            )
            mapping_source = "heuristic_fallback"

        if not isinstance(matched_department, dict):
            self._write_ticket_debug_event(
                "ticket_department_mapping_unresolved",
                entity_id=entity_id,
                payload=payload_next,
            )
            return payload_next

        self._write_ticket_debug_event(
            "ticket_department_mapping_resolved",
            source=mapping_source,
            entity_id=entity_id,
            matched_department=matched_department,
        )

        mapped_department_id = self._safe_str(matched_department.get("department_id"))
        if mapped_department_id:
            payload_next["department_allocated"] = mapped_department_id
            payload_next["department_id"] = mapped_department_id
        mapped_department_manager = self._safe_str(
            matched_department.get("department_head")
            or matched_department.get("department_name")
        )
        if mapped_department_manager:
            payload_next["department_manager"] = mapped_department_manager

        # ── Remap categorization to a valid Kepsla dashboard category ──
        payload_next = self._remap_categorization_to_accepted_value(
            payload_next, matched_department
        )
        return payload_next

    @classmethod
    def _remap_categorization_to_accepted_value(
        cls,
        payload: dict[str, Any],
        matched_department: dict[str, Any],
    ) -> dict[str, Any]:
        """Remap categorization to a value the external ticketing API accepts.

        The Kepsla dashboard API rejects unknown categorization strings (returns
        ticket_id=0).  Known-good values include service-style names like
        'room booking request', 'spa booking', 'in room dining', etc.

        This method uses the resolved department name to derive a sensible
        categorization when the current one is not in the known-good set.
        """
        current_cat = cls._safe_str(payload.get("categorization"))
        current_lower = current_cat.lower().strip()

        # Known-good categorization values that the API has historically accepted.
        # If the current value already works, keep it as-is.
        _known_good: set[str] = {
            "room booking request", "room booking", "room_booking",
            "airport transfer", "airport pick up",
            "spa booking", "complaint", "technical_support",
            "in room dining", "in-room dining", "in-room_dining",
        }
        if current_lower in _known_good:
            return payload

        # For unknown categorizations, use the resolved department name directly.
        # The Kepsla dashboard lists department names as the category filter
        # (RESTAURANT, SPA, IN-ROOM DINING, GUEST RELATIONS, etc.) so the
        # department name is the most reliable source for a valid category.
        dept_name = cls._safe_str(
            matched_department.get("department_name")
            or matched_department.get("department_head")
        ).strip()
        if dept_name:
            original_cat = current_cat
            payload["categorization"] = dept_name
            # Preserve original service name as sub-categorization
            if not cls._safe_str(payload.get("sub_categorization")) and original_cat:
                payload["sub_categorization"] = original_cat
            logger.info(
                "Remapped categorization '%s' -> '%s' (from department '%s')",
                original_cat, dept_name, dept_name,
            )

        return payload

    async def _llm_resolve_department_for_ticket_payload(
        self,
        *,
        payload: dict[str, Any],
        departments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not bool(getattr(settings, "openai_api_key", "")):
            return None
        if not isinstance(departments, list) or not departments:
            return None

        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        normalized_departments: list[dict[str, str]] = []
        for department in departments:
            if not isinstance(department, dict):
                continue
            dep_id = self._safe_str(department.get("department_id"))
            dep_name = self._safe_str(department.get("department_name"))
            if not dep_id or not dep_name:
                continue
            normalized_departments.append(
                {
                    "department_id": dep_id,
                    "department_name": dep_name,
                }
            )
            by_id[dep_id] = department
            by_name[dep_name.lower()] = department
        if not normalized_departments:
            return None

        issue_text = self._safe_str(payload.get("issue"))
        message_text = self._safe_str(payload.get("message"))
        if len(issue_text) > 700:
            issue_text = issue_text[:700]
        if len(message_text) > 2400:
            message_text = message_text[:2400]

        prompt_payload = {
            "ticket_context": {
                "issue": issue_text,
                "message": message_text,
                "category": self._safe_str(payload.get("categorization") or payload.get("category")),
                "sub_category": self._safe_str(payload.get("sub_categorization") or payload.get("sub_category")),
                "service_id": self._safe_str(payload.get("service_id")),
                "service_name": self._safe_str(payload.get("service_name")),
                "phase": self._safe_str(payload.get("phase")),
            },
            "available_departments": normalized_departments,
        }
        system_prompt = (
            "You are a strict department mapper for hotel support tickets.\n"
            "From available_departments, choose exactly one best department for this ticket.\n"
            "Rules:\n"
            "1) You MUST select one department_id from available_departments.\n"
            "2) Never return empty department_id.\n"
            "3) Never invent IDs or names.\n"
            "4) If ambiguous, choose the most operationally relevant department.\n"
            "Return ONLY JSON: {\"department_id\":\"...\",\"department_name\":\"...\",\"reason\":\"short\"}"
        )

        try:
            from llm.client import llm_client

            llm_result = await llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
                ],
                temperature=0.0,
                trace_context={
                    "responder_type": "service",
                    "agent": "ticket_department_mapper",
                },
            )
        except Exception as exc:
            self._write_ticket_debug_event(
                "ticket_department_mapping_llm_failed",
                error=str(exc),
            )
            return None

        if not isinstance(llm_result, dict):
            return None

        selected_dep_id = self._safe_str(llm_result.get("department_id") or llm_result.get("id"))
        selected_dep_name = self._safe_str(llm_result.get("department_name"))
        if selected_dep_id and selected_dep_id in by_id:
            return by_id[selected_dep_id]
        if selected_dep_name and selected_dep_name.lower() in by_name:
            return by_name[selected_dep_name.lower()]
        return None

    @classmethod
    def _resolve_department_for_ticket_payload(
        cls,
        *,
        payload: dict[str, Any],
        departments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not isinstance(departments, list) or not departments:
            return None

        explicit_department_id = cls._safe_str(
            payload.get("department_id") or payload.get("department_allocated")
        )
        if explicit_department_id and explicit_department_id not in {"0", "null", "none"}:
            for department in departments:
                if cls._safe_str(department.get("department_id")) == explicit_department_id:
                    return department

        text = " ".join(
            cls._safe_str(payload.get(field))
            for field in (
                "issue",
                "message",
                "categorization",
                "category",
                "sub_categorization",
                "sub_category",
                "service_id",
                "service_name",
            )
        ).lower()
        if not text:
            return departments[0]

        # Direct department-name mention match.
        for department in sorted(
            departments,
            key=lambda item: len(cls._safe_str(item.get("department_name"))),
            reverse=True,
        ):
            department_name = cls._safe_str(department.get("department_name")).lower()
            if department_name and department_name in text:
                return department

        keyword_groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
            (("housekeeping", "cleaning", "towel", "linen", "bed", "laundry"), ("housekeeping",)),
            (("ac", "air conditioner", "maintenance", "plumbing", "electrical", "repair"), ("maintenance", "engineering")),
            (("food", "meal", "room service", "breakfast", "dinner", "restaurant", "dining", "menu"), ("room service", "f&b", "food", "dining", "restaurant", "ird")),
            (("spa", "wellness", "massage", "therapy"), ("spa", "wellness")),
            (("taxi", "airport", "pickup", "drop", "transport", "cab", "chauffeur"), ("transport", "concierge", "travel")),
            (("billing", "bill", "invoice", "payment", "refund"), ("front desk", "finance", "accounts", "guest relations")),
            (("booking", "reservation", "check in", "check out"), ("front office", "reception", "reservation", "sales", "guest relations")),
            (("security", "unsafe", "emergency"), ("security",)),
            (("complaint", "issue"), ("guest relations", "front desk")),
        ]

        for issue_keywords, department_keywords in keyword_groups:
            if not any(token in text for token in issue_keywords):
                continue
            for department in departments:
                department_name = cls._safe_str(department.get("department_name")).lower()
                if any(keyword in department_name for keyword in department_keywords):
                    return department

        return departments[0]

    async def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        *,
        debug_context: dict[str, Any] | None = None,
    ) -> TicketingResult:
        method_upper = str(method or "POST").upper()
        timeout = float(getattr(settings, "ticketing_timeout_seconds", 10.0) or 10.0)
        self._write_ticket_debug_event(
            "ticketing_http_request",
            method=method_upper,
            url=url,
            payload=payload,
            timeout_seconds=timeout,
            context=debug_context or {},
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method_upper, url, json=payload)
        except Exception as exc:
            logger.exception("Ticketing request failed: %s %s", method_upper, url)
            self._write_ticket_debug_event(
                "ticketing_http_exception",
                method=method_upper,
                url=url,
                payload=payload,
                error=str(exc),
                context=debug_context or {},
            )
            return TicketingResult(
                success=False,
                error=str(exc),
                payload=dict(payload or {}),
            )

        raw = self._safe_json(response)
        success = 200 <= response.status_code < 300
        ticket_id = self._extract_first_value(raw, ("ticket_id", "ticketId", "id", "ticketNo", "ticket_number"))
        # Treat ticket_id=0 or "0" as a failed creation — the API didn't actually create a ticket
        if str(ticket_id or "").strip() in ("0", ""):
            ticket_id = ""
            if success:
                success = False
                logger.warning(
                    "Ticket API returned HTTP 200 but ticket_id is 0/empty — treating as failure (url=%s)",
                    url,
                )
        assigned_id = self._extract_first_value(raw, ("assignedId", "assigned_id", "agent_id", "to_agent_id"))
        error = ""
        if not success:
            extracted = self._extract_first_value(raw, ("error", "message", "detail"))
            if extracted:
                error = extracted
            else:
                # Avoid storing raw HTML error pages as the error message
                text = str(response.text or "").strip()
                if text and "<html" in text.lower():
                    error = f"External API returned HTTP {response.status_code}"
                else:
                    error = text or f"HTTP {response.status_code}"
        self._write_ticket_debug_event(
            "ticketing_http_response",
            method=method_upper,
            url=url,
            status_code=response.status_code,
            success=success,
            ticket_id=ticket_id,
            assigned_id=assigned_id,
            error=error,
            response_body=raw,
            context=debug_context or {},
        )

        return TicketingResult(
            success=success,
            ticket_id=ticket_id,
            assigned_id=assigned_id,
            status_code=response.status_code,
            error=error,
            payload=dict(payload or {}),
            response=raw,
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except Exception:
            text = str(response.text or "").strip()
            return {"raw": text}

        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
        return {"value": parsed}

    @classmethod
    def _extract_first_value(cls, data: Any, preferred_keys: tuple[str, ...]) -> str:
        """Depth-first search to find first non-empty scalar by key preference."""
        if not preferred_keys:
            return ""

        stack: list[Any] = [data]
        visited: set[int] = set()
        while stack:
            current = stack.pop()
            current_id = id(current)
            if current_id in visited:
                continue
            visited.add(current_id)

            if isinstance(current, dict):
                for key in preferred_keys:
                    if key in current and current[key] not in (None, ""):
                        return str(current[key]).strip()
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)

            elif isinstance(current, list):
                for item in current:
                    if isinstance(item, (dict, list)):
                        stack.append(item)
                    elif item not in (None, "") and len(preferred_keys) == 1:
                        return str(item).strip()
        return ""

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")

    @classmethod
    def _normalize_phase_identifier(cls, value: Any) -> str:
        normalized = cls._normalize_identifier(value).replace("-", "_")
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
    def _service_aliases_for_matching(service: dict[str, Any]) -> list[str]:
        aliases: list[str] = []
        if not isinstance(service, dict):
            return aliases
        for field in ("id", "name", "type", "description", "cuisine"):
            value = str(service.get(field) or "").strip()
            if value and value not in aliases:
                aliases.append(value)
        return aliases

    @staticmethod
    def _service_alias_match_score(message_text: str, message_tokens: list[str], alias: str) -> float:
        alias_text = str(alias or "").strip().lower()
        if not alias_text:
            return 0.0
        if alias_text in message_text:
            return 0.98

        alias_tokens = [token for token in re.findall(r"[a-z0-9]+", alias_text) if len(token) >= 3]
        if not alias_tokens:
            return 0.0
        if not message_tokens:
            return 0.0

        hits = 0
        for token in alias_tokens:
            token_hit = False
            for msg_token in message_tokens:
                if msg_token == token:
                    token_hit = True
                    break
                if len(token) >= 5 and len(msg_token) >= 4 and (
                    msg_token.startswith(token[:4]) or token.startswith(msg_token[:4])
                ):
                    token_hit = True
                    break
                if len(token) >= 5 and len(msg_token) >= 4 and SequenceMatcher(a=token, b=msg_token).ratio() >= 0.86:
                    token_hit = True
                    break
            if token_hit:
                hits += 1
        if hits <= 0:
            return 0.0
        return hits / max(1, len(alias_tokens))

    def _match_service_for_ticket_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            return None

        payload_phase = self._normalize_phase_identifier(payload.get("phase"))
        explicit_service_id = self._normalize_identifier(payload.get("service_id") or payload.get("service"))
        explicit_service_name = str(payload.get("service_name") or "").strip().lower()
        message_blob = " ".join(
            str(payload.get(field) or "").strip().lower()
            for field in (
                "service_name",
                "service",
                "service_id",
                "issue",
                "message",
                "sub_category",
                "categorization",
                "category",
                "ticket_sub_category",
            )
            if str(payload.get(field) or "").strip()
        ).strip()
        if not message_blob and not explicit_service_id and not explicit_service_name:
            return None

        message_tokens = re.findall(r"[a-z0-9]+", message_blob)
        best_service: dict[str, Any] | None = None
        best_score = 0.0

        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue

            service_phase = self._normalize_phase_identifier(service.get("phase_id"))
            if payload_phase and service_phase and service_phase != payload_phase:
                continue

            service_id = self._normalize_identifier(service.get("id"))
            service_name = str(service.get("name") or "").strip().lower()

            score = 0.0
            if explicit_service_id and service_id and explicit_service_id == service_id:
                score = 1.0
            elif explicit_service_name and service_name and (
                explicit_service_name == service_name
                or explicit_service_name in service_name
                or service_name in explicit_service_name
            ):
                score = 0.98
            else:
                for alias in self._service_aliases_for_matching(service):
                    score = max(
                        score,
                        self._service_alias_match_score(message_blob, message_tokens, alias),
                    )

            if score > best_score:
                best_score = score
                best_service = service

        if best_service is None:
            return None
        if best_score < 0.72:
            return None

        return {
            "service_id": str(best_service.get("id") or "").strip(),
            "service_name": str(best_service.get("name") or best_service.get("id") or "").strip(),
            "service_phase_id": self._normalize_phase_identifier(best_service.get("phase_id")),
            "ticketing_enabled": bool(best_service.get("ticketing_enabled", True)),
            "match_score": best_score,
            "payload_phase_id": payload_phase,
        }

    def detect_service_ticketing_disabled(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        matched = self._match_service_for_ticket_payload(payload)
        if not isinstance(matched, dict):
            return None
        if bool(matched.get("ticketing_enabled", True)):
            return None
        return {
            "service_id": str(matched.get("service_id") or ""),
            "service_name": str(matched.get("service_name") or ""),
            "service_phase_id": str(matched.get("service_phase_id") or ""),
            "payload_phase_id": str(matched.get("payload_phase_id") or ""),
            "match_score": float(matched.get("match_score") or 0.0),
        }

    @staticmethod
    def _normalize_priority(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "MEDIUM"
        mapping = {
            "critical": "CRITICAL",
            "urgent": "CRITICAL",
            "high": "HIGH",
            "medium": "MEDIUM",
            "med": "MEDIUM",
            "low": "LOW",
        }
        return mapping.get(raw, "MEDIUM")

    @staticmethod
    def _normalize_category(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "request"
        mapping = {
            "request": "request",
            "complaint": "complaint",
            "upsell": "upsell",
            "inquiry": "inquiry",
            "enquiry": "inquiry",
            "conversation": "inquiry",
            "order": "request",
            "food_order": "request",
            "food order": "request",
            "in-room dining": "request",
            "dining": "request",
            "maintenance": "complaint",
            "emergency": "complaint",
        }
        return mapping.get(raw, raw)

    @classmethod
    def _normalize_phase(cls, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "During Stay"

        if raw in {"Booking", "Pre Checkin", "During Stay", "Post Checkout", "Pre Booking"}:
            return raw

        normalized_key = cls._normalize_identifier(raw).replace("-", "_")
        mapping = {
            "booking": "Booking",
            "pre_checkin": "Pre Checkin",
            "precheckin": "Pre Checkin",
            "during_stay": "During Stay",
            "duringstay": "During Stay",
            "in_stay": "During Stay",
            "instay": "During Stay",
            "post_checkout": "Post Checkout",
            "postcheckout": "Post Checkout",
            "pre_booking": "Pre Booking",
            "prebooking": "Pre Booking",
        }
        return mapping.get(normalized_key, raw)

    @staticmethod
    def _normalize_ticket_status(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "open"
        mapping = {
            "open": "open",
            "in_progress": "in_progress",
            "in progress": "in_progress",
            "closed": "closed",
            "cancel": "cancel",
            "cancelled": "cancel",
            "canceled": "cancel",
            "breached": "breached",
        }
        return mapping.get(raw, "open")

    @staticmethod
    def _default_phase_for_context(integration: dict[str, Any]) -> str:
        flow = str(integration.get("flow") or integration.get("bot_mode") or "").strip().lower()
        if flow in {"engage", "booking", "booking_bot"}:
            return "Pre Booking"
        return "During Stay"

    @staticmethod
    def _source_from_channel(channel: str | None) -> str:
        normalized = str(channel or "").strip().lower()
        if normalized in {"whatsapp", "wa"}:
            return "whatsapp_bot"
        return "manual"

    @classmethod
    def _resolve_ticket_source(
        cls,
        *,
        explicit_source: str,
        integration: dict[str, Any],
        channel: str | None,
    ) -> str:
        def normalize_source(raw_source: str) -> str:
            normalized_source = cls._normalize_identifier(raw_source).replace("-", "_")
            mapping = {
                "whatsapp": "whatsapp_bot",
                "wa": "whatsapp_bot",
                "whatsapp_bot": "whatsapp_bot",
                "booking": "booking_bot",
                "booking_bot": "booking_bot",
                "engage": "booking_bot",
                "taskly": "taskly",
                "manual": "manual",
                "chat_bot": "manual",
                "web": "manual",
            }
            return mapping.get(normalized_source, "manual")

        if str(explicit_source or "").strip():
            return normalize_source(str(explicit_source))

        from_integration = str(
            integration.get("ticket_source")
            or integration.get("source")
            or ""
        ).strip()
        if from_integration:
            return normalize_source(from_integration)

        flow = str(integration.get("flow") or integration.get("bot_mode") or "").strip().lower()
        if flow in {"engage", "booking", "booking_bot"}:
            return "booking_bot"

        return normalize_source(cls._source_from_channel(channel))

    @staticmethod
    def _ticketing_base_url() -> str:
        return str(settings.ticketing_base_url or "").strip().rstrip("/")

    @staticmethod
    def _use_local_mode() -> bool:
        return bool(getattr(settings, "ticketing_local_mode", False))

    @staticmethod
    def _allow_api_failure_local_fallback() -> bool:
        """
        Allow local fallback only when explicitly enabled.
        This prevents exposing LOCAL-* ticket IDs as successful external tickets.
        """
        return bool(getattr(settings, "ticketing_api_failure_local_fallback_enabled", False))

    @staticmethod
    def _local_store_path() -> Path:
        raw = str(getattr(settings, "ticketing_local_store_file", "") or "").strip()
        if not raw:
            raw = "./data/ticketing/local_tickets.json"
        return Path(raw)

    def _local_csv_path(self) -> Path:
        raw = str(getattr(settings, "ticketing_local_csv_file", "") or "").strip()
        if raw:
            return Path(raw)
        store_path = self._local_store_path()
        if store_path.suffix.lower() == ".json":
            return store_path.with_suffix(".csv")
        return store_path.parent / f"{store_path.name}.csv"

    @staticmethod
    def _default_local_store() -> dict[str, Any]:
        return {"next_id": 1, "tickets": []}

    def _normalize_local_store_data(self, raw_store: Any) -> dict[str, Any]:
        if not isinstance(raw_store, dict):
            return self._default_local_store()

        raw_tickets = raw_store.get("tickets")
        tickets: list[dict[str, Any]] = []
        if isinstance(raw_tickets, list):
            for index, item in enumerate(raw_tickets, start=1):
                if not isinstance(item, dict):
                    continue
                record = dict(item)
                ticket_id = self._safe_str(record.get("ticket_id") or record.get("id")) or f"LOCAL-{index}"
                record["id"] = ticket_id
                record["ticket_id"] = ticket_id
                if not isinstance(record.get("payload"), dict):
                    record["payload"] = {}
                created_at = self._safe_str(record.get("created_at")) or datetime.now(UTC).isoformat()
                updated_at = self._safe_str(record.get("updated_at")) or created_at
                record["created_at"] = created_at
                record["updated_at"] = updated_at
                if not self._safe_str(record.get("status")):
                    record["status"] = self._safe_str(record["payload"].get("ticket_status")) or "open"
                if not self._safe_str(record.get("mode")):
                    record["mode"] = "local_simulation"
                tickets.append(record)

        max_numeric_id = 0
        for index, record in enumerate(tickets, start=1):
            numeric_id = self._extract_local_numeric_id(record.get("ticket_id") or record.get("id"), fallback=index)
            if numeric_id > max_numeric_id:
                max_numeric_id = numeric_id

        next_id = max_numeric_id + 1 if tickets else 1
        raw_next = raw_store.get("next_id")
        if isinstance(raw_next, int) and raw_next > next_id:
            next_id = raw_next
        return {"next_id": next_id, "tickets": tickets}

    def _rebuild_local_store_from_csv(self, *, reason: str) -> dict[str, Any]:
        csv_path = self._local_csv_path()
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            logger.warning(
                "Local ticket JSON store unusable (%s) and CSV recovery file missing/empty at %s",
                reason,
                csv_path,
            )
            return self._default_local_store()

        recovered_tickets: list[dict[str, Any]] = []
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    if not isinstance(row, dict):
                        continue
                    ticket_id = self._safe_str(row.get("ticket_id") or row.get("id"))
                    if not ticket_id:
                        continue

                    payload: dict[str, Any] = {}
                    payload_json = self._safe_str(row.get("payload_json"))
                    if payload_json:
                        try:
                            parsed_payload = json.loads(payload_json)
                            if isinstance(parsed_payload, dict):
                                payload = parsed_payload
                        except Exception:
                            payload = {}

                    if not payload:
                        payload = {
                            "session_id": self._safe_str(row.get("session_id")),
                            "guest_id": self._safe_str(row.get("guest_id")),
                            "room_number": self._safe_str(row.get("room_number")),
                            "issue": self._safe_str(row.get("issue")),
                            "message": self._safe_str(row.get("message") or row.get("issue")),
                            "ticket_status": self._safe_str(row.get("status") or "open"),
                            "categorization": self._safe_str(row.get("category")),
                            "sub_categorization": self._safe_str(row.get("sub_category")),
                            "source": self._safe_str(row.get("ticket_source")),
                        }

                    created_at = self._safe_str(
                        row.get("local_created_at_utc")
                        or row.get("created_at")
                    ) or datetime.now(UTC).isoformat()
                    updated_at = self._safe_str(
                        row.get("local_updated_at_utc")
                        or row.get("updated_at")
                    ) or created_at

                    recovered_tickets.append(
                        {
                            "id": ticket_id,
                            "ticket_id": ticket_id,
                            "status": self._safe_str(row.get("status") or payload.get("ticket_status")) or "open",
                            "issue": self._safe_str(row.get("issue") or payload.get("issue")),
                            "manager_notes": self._safe_str(row.get("manager_notes") or payload.get("manager_notes")),
                            "room_number": self._safe_str(row.get("room_number") or payload.get("room_number")),
                            "guest_id": self._safe_str(row.get("guest_id") or payload.get("guest_id")),
                            "assigned_id": self._safe_str(row.get("assigned_id") or payload.get("assigned_to")),
                            "created_at": created_at,
                            "updated_at": updated_at,
                            "payload": payload,
                            "mode": self._safe_str(row.get("mode")) or "local_simulation",
                        }
                    )
        except Exception:
            logger.exception("Failed to recover local ticket JSON store from CSV at %s", csv_path)
            return self._default_local_store()

        normalized = self._normalize_local_store_data({"tickets": recovered_tickets})
        logger.warning(
            "Recovered local ticket JSON store from CSV at %s (reason=%s, recovered_tickets=%s)",
            csv_path,
            reason,
            len(normalized.get("tickets", [])),
        )
        return normalized

    def _load_local_store(self) -> dict[str, Any]:
        path = self._local_store_path()
        if not path.exists():
            recovered = self._rebuild_local_store_from_csv(reason="json_missing")
            if recovered.get("tickets"):
                try:
                    self._save_local_store(recovered)
                except Exception:
                    logger.exception("Failed to persist recovered local ticket JSON store to %s", path)
            return recovered

        try:
            raw_text = path.read_text(encoding="utf-8")
            if not str(raw_text).strip():
                recovered = self._rebuild_local_store_from_csv(reason="json_empty")
                if recovered.get("tickets"):
                    try:
                        self._save_local_store(recovered)
                    except Exception:
                        logger.exception("Failed to persist recovered local ticket JSON store to %s", path)
                return recovered

            parsed = json.loads(raw_text)
            normalized = self._normalize_local_store_data(parsed)
            self._bootstrap_local_csv_if_needed(normalized.get("tickets", []))
            return normalized
        except Exception:
            logger.exception("Failed to read local ticket store at %s", path)
            recovered = self._rebuild_local_store_from_csv(reason="json_parse_error")
            if recovered.get("tickets"):
                try:
                    self._save_local_store(recovered)
                except Exception:
                    logger.exception("Failed to persist recovered local ticket JSON store to %s", path)
            return recovered

    def _save_local_store(self, data: dict[str, Any]) -> None:
        path = self._local_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized = self._normalize_local_store_data(data)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
        logger.info(
            "Local ticket JSON store saved: path=%s tickets=%s next_id=%s",
            path,
            len(normalized.get("tickets", [])),
            normalized.get("next_id"),
        )

    @staticmethod
    def _safe_str(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _mask_identifier(value: Any, keep: int = 4) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) <= keep:
            return "*" * len(text)
        return ("*" * (len(text) - keep)) + text[-keep:]

    def _extract_local_numeric_id(self, value: Any, fallback: int) -> int:
        raw = self._safe_str(value)
        if raw.isdigit():
            return int(raw)
        if raw.upper().startswith("LOCAL-"):
            numeric = raw.split("-", 1)[1].strip()
            if numeric.isdigit():
                return int(numeric)
        return fallback

    def _build_local_csv_row(
        self,
        *,
        local_id: int,
        ticket_id: str,
        payload: dict[str, Any],
        mode: str,
        local_created_at: str,
        local_updated_at: str,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_safe = payload if isinstance(payload, dict) else {}
        response_safe = response if isinstance(response, dict) else {}

        department_alloc = self._safe_str(
            payload_safe.get("department_allocated") or payload_safe.get("department_id")
        )
        category = self._safe_str(payload_safe.get("categorization") or payload_safe.get("category"))
        sub_category = self._safe_str(
            payload_safe.get("sub_categorization") or payload_safe.get("sub_category")
        )
        status = self._safe_str(payload_safe.get("ticket_status")) or "open"
        ticket_source = self._safe_str(payload_safe.get("source") or payload_safe.get("ticket_source"))
        entity_id = self._safe_str(
            payload_safe.get("entity_id")
            or payload_safe.get("organisation_id")
            or payload_safe.get("organization_id")
            or payload_safe.get("org_id")
        )
        assigned_to = self._safe_str(payload_safe.get("assigned_to"))
        assigned_id = self._safe_str(
            payload_safe.get("assigned_id")
            or payload_safe.get("assignedId")
            or response_safe.get("assigned_id")
            or response_safe.get("assignedId")
            or assigned_to
        )

        return {
            "id": local_id,
            "ticket_id": ticket_id,
            "session_id": self._safe_str(payload_safe.get("session_id")),
            "guest_id": self._safe_str(payload_safe.get("guest_id")),
            "entity_id": entity_id,
            "fms_entity_id": self._safe_str(payload_safe.get("fms_entity_id")),
            "issue": self._safe_str(payload_safe.get("issue")),
            "message_id": self._safe_str(payload_safe.get("message_id")),
            "sentiment_score": self._safe_str(payload_safe.get("sentiment_score")),
            "department_alloc": department_alloc,
            "priority": self._safe_str(payload_safe.get("priority")),
            "category": category,
            "phase": self._safe_str(payload_safe.get("phase")),
            "status": status,
            "sla_due": self._safe_str(payload_safe.get("sla_due_time")),
            "escalation_stage": self._safe_str(payload_safe.get("escalation_stage") or 0),
            "created_at": self._safe_str(payload_safe.get("created_at")) or local_created_at,
            "updated_at": local_updated_at,
            "room_number": self._safe_str(payload_safe.get("room_number")),
            "message": self._safe_str(payload_safe.get("message")),
            "customer_feedback": self._safe_str(payload_safe.get("customer_feedback")),
            "manager_notes": self._safe_str(payload_safe.get("manager_notes")),
            "assignee_notes": self._safe_str(payload_safe.get("assignee_notes")),
            "assigned_to": assigned_to,
            "assigned_id": assigned_id,
            "customer_rating": self._safe_str(payload_safe.get("customer_rating")),
            "department_id": self._safe_str(payload_safe.get("department_id") or department_alloc),
            "sla_due_at": self._safe_str(payload_safe.get("sla_due_at")),
            "sla_duration_minutes": self._safe_str(payload_safe.get("sla_duration_minutes")),
            "ticket_auto_assign": self._safe_str(payload_safe.get("ticket_auto_assign") or 0),
            "closed_at": self._safe_str(payload_safe.get("closed_at")),
            "outlet_id": self._safe_str(payload_safe.get("outlet_id")),
            "input_tokens": self._safe_str(payload_safe.get("input_tokens") or 0),
            "output_tokens": self._safe_str(payload_safe.get("output_tokens") or 0),
            "total_tokens": self._safe_str(payload_safe.get("total_tokens") or 0),
            "cost": self._safe_str(payload_safe.get("cost") or 0),
            "sub_category": sub_category,
            "guest_name": self._safe_str(payload_safe.get("guest_name")),
            "property_name": self._safe_str(payload_safe.get("property_name")),
            "room_type": self._safe_str(payload_safe.get("room_type")),
            "compensation_type": self._safe_str(payload_safe.get("compensation_type")),
            "compensation_currency": self._safe_str(payload_safe.get("compensation_currency")),
            "compensation_amount": self._safe_str(payload_safe.get("compensation_amount") or 0),
            "group_id": self._safe_str(payload_safe.get("group_id")),
            "ticket_source": ticket_source,
            "cancelled_notes": self._safe_str(payload_safe.get("cancelled_notes")),
            "mode": mode,
            "local_created_at_utc": local_created_at,
            "local_updated_at_utc": local_updated_at,
            "payload_json": json.dumps(payload_safe, ensure_ascii=False),
            "response_json": json.dumps(response_safe, ensure_ascii=False),
        }

    def _append_local_ticket_csv_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        csv_path = self._local_csv_path()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = csv_path.exists() and csv_path.stat().st_size > 0
        with csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self._LOCAL_TICKET_CSV_HEADERS))
            if not file_exists:
                writer.writeheader()
            for row in rows:
                writer.writerow({header: row.get(header, "") for header in self._LOCAL_TICKET_CSV_HEADERS})

    def _rewrite_local_csv_from_tickets(self, tickets: list[dict[str, Any]]) -> tuple[bool, Path]:
        """
        Rewrite local CSV from JSON store to keep both stores fully in sync.
        This prevents drift when append operations fail or are interrupted.
        """
        csv_path = self._local_csv_path()
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        rows: list[dict[str, Any]] = []
        for index, ticket in enumerate(tickets or [], start=1):
            if not isinstance(ticket, dict):
                continue
            payload = ticket.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            ticket_id = self._safe_str(ticket.get("ticket_id") or ticket.get("id")) or f"LOCAL-{index}"
            local_id = self._extract_local_numeric_id(ticket.get("id") or ticket_id, fallback=index)
            local_created_at = self._safe_str(ticket.get("created_at")) or datetime.now(UTC).isoformat()
            local_updated_at = self._safe_str(ticket.get("updated_at")) or local_created_at
            rows.append(
                self._build_local_csv_row(
                    local_id=local_id,
                    ticket_id=ticket_id,
                    payload=payload,
                    mode=self._safe_str(ticket.get("mode")) or "local_simulation",
                    local_created_at=local_created_at,
                    local_updated_at=local_updated_at,
                    response={"status": self._safe_str(ticket.get("status")) or "open"},
                )
            )

        try:
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self._LOCAL_TICKET_CSV_HEADERS))
                writer.writeheader()
                for row in rows:
                    writer.writerow({header: row.get(header, "") for header in self._LOCAL_TICKET_CSV_HEADERS})
            logger.info(
                "Local ticket CSV synced: path=%s rows=%s",
                csv_path,
                len(rows),
            )
            return True, csv_path
        except PermissionError:
            # Fallback mirror file when primary CSV is locked by another process.
            mirror_path = csv_path.with_name(f"{csv_path.stem}_mirror{csv_path.suffix}")
            with mirror_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self._LOCAL_TICKET_CSV_HEADERS))
                writer.writeheader()
                for row in rows:
                    writer.writerow({header: row.get(header, "") for header in self._LOCAL_TICKET_CSV_HEADERS})
            logger.warning(
                "Local ticket CSV primary path locked; wrote mirror file instead: mirror_path=%s rows=%s",
                mirror_path,
                len(rows),
            )
            return False, mirror_path

    def _bootstrap_local_csv_if_needed(self, existing_tickets: list[dict[str, Any]]) -> None:
        csv_path = self._local_csv_path()
        if csv_path.exists() and csv_path.stat().st_size > 0:
            return
        if not existing_tickets:
            return

        rows: list[dict[str, Any]] = []
        for index, ticket in enumerate(existing_tickets, start=1):
            if not isinstance(ticket, dict):
                continue
            payload = ticket.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            ticket_id = self._safe_str(ticket.get("ticket_id") or ticket.get("id"))
            local_id = self._extract_local_numeric_id(ticket.get("id") or ticket_id, fallback=index)
            local_created_at = self._safe_str(ticket.get("created_at")) or datetime.now(UTC).isoformat()
            local_updated_at = self._safe_str(ticket.get("updated_at")) or local_created_at
            rows.append(
                self._build_local_csv_row(
                    local_id=local_id,
                    ticket_id=ticket_id or f"LOCAL-{local_id}",
                    payload=payload,
                    mode=self._safe_str(ticket.get("mode")) or "local_simulation",
                    local_created_at=local_created_at,
                    local_updated_at=local_updated_at,
                    response={"status": "backfilled_from_json"},
                )
            )

        self._append_local_ticket_csv_rows(rows)

    async def _create_ticket_local(self, payload: dict[str, Any]) -> TicketingResult:
        self._write_ticket_debug_event(
            "local_ticket_create_start",
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
            payload=payload,
        )
        logger.info(
            "Local ticket create requested: session_id=%s issue=%s category=%s sub_category=%s",
            self._safe_str((payload or {}).get("session_id")),
            self._safe_str((payload or {}).get("issue"))[:160],
            self._safe_str((payload or {}).get("categorization") or (payload or {}).get("category")),
            self._safe_str((payload or {}).get("sub_categorization") or (payload or {}).get("sub_category")),
        )
        store = self._load_local_store()
        next_id = int(store.get("next_id") or 1)
        ticket_id = f"LOCAL-{next_id}"
        now_iso = datetime.now(UTC).isoformat()

        record = {
            "id": ticket_id,
            "ticket_id": ticket_id,
            "status": str(payload.get("ticket_status") or "open").strip().lower() or "open",
            "issue": str(payload.get("issue") or "").strip(),
            "manager_notes": str(payload.get("manager_notes") or "").strip(),
            "room_number": str(payload.get("room_number") or "").strip(),
            "guest_id": str(payload.get("guest_id") or "").strip(),
            "assigned_id": str(payload.get("assigned_to") or "").strip(),
            "created_at": now_iso,
            "updated_at": now_iso,
            "payload": dict(payload or {}),
            "mode": "local_simulation",
        }

        tickets = list(store.get("tickets") or [])
        logger.info(
            "Local ticket store loaded for create: tickets=%s next_id=%s",
            len(tickets),
            next_id,
        )
        tickets.append(record)
        store["tickets"] = tickets
        store["next_id"] = next_id + 1
        try:
            self._save_local_store(store)
        except Exception as exc:
            logger.exception("Failed to persist local ticket create")
            return TicketingResult(
                success=False,
                error=str(exc),
                payload=dict(payload or {}),
            )

        response = {
            "status": "success",
            "ticket_id": ticket_id,
            "id": ticket_id,
            "mode": "local_simulation",
            "ticket_record": dict(record),
        }

        csv_written = True
        csv_primary_synced = True
        csv_path_used = self._local_csv_path()
        try:
            csv_primary_synced, csv_path_used = self._rewrite_local_csv_from_tickets(store["tickets"])
        except Exception:
            logger.exception("Failed to sync local ticket CSV from JSON store")
            csv_written = False

        response["csv_written"] = csv_written
        response["csv_primary_synced"] = csv_primary_synced
        response["csv_path"] = str(csv_path_used)
        logger.info(
            "Local ticket create completed: ticket_id=%s total_tickets=%s csv_written=%s csv_primary_synced=%s csv_path=%s",
            ticket_id,
            len(store.get("tickets") or []),
            csv_written,
            csv_primary_synced,
            csv_path_used,
        )
        self._write_ticket_debug_event(
            "local_ticket_create_completed",
            success=True,
            ticket_id=ticket_id,
            total_tickets=len(store.get("tickets") or []),
            csv_written=csv_written,
            csv_primary_synced=csv_primary_synced,
            csv_path=str(csv_path_used),
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
            response=response,
        )
        return TicketingResult(
            success=True,
            ticket_id=ticket_id,
            assigned_id=record["assigned_id"],
            status_code=200,
            payload=dict(payload or {}),
            response=response,
        )

    async def _update_ticket_local(
        self,
        ticket_id: str,
        manager_notes: str,
        *,
        extra_fields: dict[str, Any] | None = None,
    ) -> TicketingResult:
        self._write_ticket_debug_event(
            "local_ticket_update_start",
            ticket_id=ticket_id,
            manager_notes=manager_notes,
            extra_fields=extra_fields or {},
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
        )
        logger.info(
            "Local ticket update requested: ticket_id=%s manager_notes_chars=%s",
            ticket_id,
            len(str(manager_notes or "")),
        )
        store = self._load_local_store()
        tickets = list(store.get("tickets") or [])
        target_index = -1
        for idx, item in enumerate(tickets):
            if not isinstance(item, dict):
                continue
            current_id = str(item.get("id") or item.get("ticket_id") or "").strip()
            if current_id == ticket_id:
                target_index = idx
                break

        if target_index < 0:
            logger.warning("Local ticket update failed: ticket not found ticket_id=%s", ticket_id)
            return TicketingResult(
                success=False,
                error=f"Local ticket not found: {ticket_id}",
                ticket_id=ticket_id,
                status_code=404,
            )

        record = dict(tickets[target_index])
        existing_notes = str(record.get("manager_notes") or "").strip()
        if existing_notes:
            record["manager_notes"] = f"{existing_notes}\n{manager_notes}"
        else:
            record["manager_notes"] = manager_notes
        record["updated_at"] = datetime.now(UTC).isoformat()

        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if value is not None:
                    record[key] = value

        tickets[target_index] = record
        store["tickets"] = tickets
        try:
            self._save_local_store(store)
        except Exception as exc:
            logger.exception("Failed to persist local ticket update")
            return TicketingResult(
                success=False,
                error=str(exc),
                ticket_id=ticket_id,
                status_code=500,
            )

        csv_written = True
        csv_primary_synced = True
        csv_path_used = self._local_csv_path()
        try:
            csv_primary_synced, csv_path_used = self._rewrite_local_csv_from_tickets(store["tickets"])
        except Exception:
            logger.exception("Failed to sync local ticket CSV after update")
            csv_written = False

        response = {
            "status": "success",
            "ticket_id": ticket_id,
            "id": ticket_id,
            "manager_notes": record.get("manager_notes"),
            "mode": "local_simulation",
            "csv_written": csv_written,
            "csv_primary_synced": csv_primary_synced,
            "csv_path": str(csv_path_used),
        }
        logger.info(
            "Local ticket update completed: ticket_id=%s csv_written=%s csv_primary_synced=%s csv_path=%s",
            ticket_id,
            csv_written,
            csv_primary_synced,
            csv_path_used,
        )
        self._write_ticket_debug_event(
            "local_ticket_update_completed",
            success=True,
            ticket_id=ticket_id,
            csv_written=csv_written,
            csv_primary_synced=csv_primary_synced,
            csv_path=str(csv_path_used),
            local_store_path=str(self._local_store_path()),
            local_csv_path=str(self._local_csv_path()),
            response=response,
        )
        return TicketingResult(
            success=True,
            ticket_id=ticket_id,
            assigned_id=str(record.get("assigned_id") or "").strip(),
            status_code=200,
            payload={"manager_notes": manager_notes},
            response=response,
        )


ticketing_service = TicketingService()
