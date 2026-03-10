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
        gate = self.detect_service_ticketing_disabled(payload)
        if gate is not None:
            response_payload = {
                "skip_reason": "phase_service_ticketing_disabled",
                **gate,
            }
            return TicketingResult(
                success=False,
                status_code=409,
                error="phase_service_ticketing_disabled",
                payload=dict(payload or {}),
                response=response_payload,
            )

        if self._use_local_mode():
            result = await self._create_ticket_local(payload)
            logger.info(
                "Ticket create completed in local mode: success=%s ticket_id=%s error=%s",
                result.success,
                result.ticket_id,
                self._safe_str(result.error),
            )
            return result

        base_url = self._ticketing_base_url()
        if not base_url:
            return TicketingResult(success=False, error="TICKETING_BASE_URL is not configured")

        path = str(settings.ticketing_create_path or "/insert/ticket.htm").strip() or "/insert/ticket.htm"
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"
        return await self._request_json("POST", url, payload)

    async def update_ticket(
        self,
        ticket_id: str,
        manager_notes: str,
        extra_fields: dict[str, Any] | None = None,
    ) -> TicketingResult:
        """Update an existing ticket (manager notes and optional additional fields)."""
        tid = str(ticket_id or "").strip()
        notes = str(manager_notes or "").strip()
        if not tid:
            return TicketingResult(success=False, error="ticket_id is required")
        if not notes:
            return TicketingResult(success=False, error="manager_notes is required")
        if self._use_local_mode():
            result = await self._update_ticket_local(tid, notes, extra_fields=extra_fields)
            logger.info(
                "Ticket update completed in local mode: success=%s ticket_id=%s error=%s",
                result.success,
                result.ticket_id or tid,
                self._safe_str(result.error),
            )
            return result
        base_url = self._ticketing_base_url()
        if not base_url:
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

        result = await self._request_json("PATCH", url, body)
        if not result.ticket_id:
            result.ticket_id = tid
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
            return TicketingResult(success=False, error="AGENT_HANDOFF_API_URL is not configured")

        payload = {
            "from_responder": "BOT",
            "to_responder": "AGENT",
            "conversation_id": str(conversation_id or "").strip(),
            "session_id": str(session_id or "").strip(),
            "to_agent_id": str(agent_id or "").strip(),
            "reason": str(reason or "").strip(),
        }
        return await self._request_json("POST", url, payload)

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
        integration = self.get_integration_context(context)

        guest_id = (
            integration.get("guest_id")
            or integration.get("user_id")
            or integration.get("wa_number")
            or context.guest_phone
            or ""
        )
        organisation_id = (
            integration.get("organisation_id")
            or integration.get("organization_id")
            or integration.get("org_id")
            or integration.get("entity_id")
            or context.hotel_code
            or ""
        )
        room_number = (
            context.room_number
            or integration.get("room_number")
            or ""
        )

        now_utc = datetime.now(UTC).strftime("%H:%M:%S %d-%m-%Y")
        issue_text = str(issue or "").strip() or str(message or "").strip()
        message_text = str(message or "").strip() or issue_text
        source_value = self._resolve_ticket_source(
            explicit_source=source,
            integration=integration,
            channel=context.channel,
        )
        phase_value = self._normalize_phase(
            phase
            or integration.get("phase")
            or self._default_phase_for_context(integration)
        )
        priority_value = self._normalize_priority(priority)
        category_value = self._normalize_category(category)
        status_value = self._normalize_ticket_status(ticket_status)

        payload = {
            "guest_id": str(guest_id or "").strip(),
            "room_number": str(room_number or "").strip(),
            "organisation_id": str(organisation_id or "").strip(),
            "issue": issue_text,
            "message": message_text,
            "sentiment_score": str(sentiment_score or "").strip(),
            "department_allocated": str(department_id or "").strip(),
            "department_manager": str(department_manager or "").strip(),
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
        }

        # Backward-compatible aliases some APIs accept.
        payload["department_id"] = payload["department_allocated"]
        payload["category"] = payload["categorization"]
        payload["sub_category"] = payload["sub_categorization"]

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

    async def _request_json(self, method: str, url: str, payload: dict[str, Any]) -> TicketingResult:
        method_upper = str(method or "POST").upper()
        timeout = float(getattr(settings, "ticketing_timeout_seconds", 10.0) or 10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method_upper, url, json=payload)
        except Exception as exc:
            logger.exception("Ticketing request failed: %s %s", method_upper, url)
            return TicketingResult(
                success=False,
                error=str(exc),
                payload=dict(payload or {}),
            )

        raw = self._safe_json(response)
        success = 200 <= response.status_code < 300
        ticket_id = self._extract_first_value(raw, ("ticket_id", "ticketId", "id", "ticketNo", "ticket_number"))
        assigned_id = self._extract_first_value(raw, ("assignedId", "assigned_id", "agent_id", "to_agent_id"))
        error = ""
        if not success:
            error = (
                self._extract_first_value(raw, ("error", "message", "detail"))
                or response.text
                or f"HTTP {response.status_code}"
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
        return TicketingResult(
            success=True,
            ticket_id=ticket_id,
            assigned_id=str(record.get("assigned_id") or "").strip(),
            status_code=200,
            payload={"manager_notes": manager_notes},
            response=response,
        )


ticketing_service = TicketingService()
