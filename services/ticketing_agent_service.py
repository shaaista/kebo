"""
Ticketing Agent Service

Decides whether to activate ticketing flow for a turn after the first-layer
intent decision (typically from full-KB LLM intent output).

Goals:
- Keep transactional flows (order/table booking) conversational.
- Activate complaint-style ticket intake only when staff action is needed.
- Avoid spurious ticket hijacks during slot collection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any

from config.settings import settings
from llm.client import llm_client
from schemas.chat import IntentType
from services.config_service import config_service

logger = logging.getLogger(__name__)


_TICKETING_PENDING_ACTIONS = {
    "escalate_complaint",
    "collect_ticket_room_number",
    "collect_ticket_issue_details",
    "collect_ticket_identity_details",
    "confirm_ticket_creation",
    "collect_ticket_update_note",
    "confirm_ticket_escalation",
}

_ORDER_PENDING_ACTIONS = {
    "order_food",
    "collect_order_item",
    "collect_order_quantity",
    "collect_order_addons",
    "confirm_order",
}

_BOOKING_PENDING_ACTIONS = {
    "table_booking",
    "room_booking",
    "collect_booking_party_size",
    "collect_booking_time",
    "collect_room_booking_details",
    "select_room_type",
    "confirm_booking",
    "confirm_room_booking",
    "confirm_room_availability_check",
}

_TICKETING_CASE_STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "for",
    "of",
    "and",
    "or",
    "in",
    "on",
    "at",
    "is",
    "are",
    "be",
    "by",
    "with",
    "from",
    "this",
    "that",
    "these",
    "those",
    "user",
    "guest",
    "customer",
    "bot",
    "case",
    "ticket",
    "tickets",
    "needs",
    "need",
    "required",
    "requires",
    "request",
    "requests",
    "create",
    "created",
    "background",
    "staff",
    "action",
}

_SEMANTIC_TOPIC_ALIASES: dict[str, set[str]] = {
    "book": {"booking", "book", "reserve", "reservation", "arrange", "schedule", "request"},
    "table": {"table", "restaurant", "dining", "reservation"},
    "room": {"room", "suite", "stay", "checkin", "checkout", "check", "accommodation"},
    "spa": {"spa", "massage", "therapy", "wellness", "treatment"},
    "transport": {"transport", "cab", "taxi", "airport", "pickup", "drop", "ride", "shuttle", "chauffeur", "transfer"},
    "food": {"food", "meal", "dish", "menu", "dining", "dessert"},
    "human": {"human", "agent", "handoff", "escalation", "escalate", "manager", "staff", "callback"},
    "complaint": {"complaint", "issue", "problem", "maintenance", "broken", "notwork", "dirty", "cockroach"},
}
_SEMANTIC_TOPICS = set(_SEMANTIC_TOPIC_ALIASES.keys())


@dataclass
class TicketingAgentDecision:
    activate: bool
    routed_intent: IntentType | None
    route: str = "none"
    reason: str = ""
    source: str = ""
    matched_case: str = ""


class TicketingAgentService:
    """Decision layer for when ticketing agent should take over."""

    def get_configured_cases(self) -> list[str]:
        """
        Return normalized configured ticketing cases when plugin service is enabled.
        """
        plugin_config = self._get_ticketing_plugin_config()
        if plugin_config is None:
            return []
        if not self._is_ticketing_plugin_enabled(plugin_config):
            return []
        return self._extract_ticketing_cases(plugin_config)

    def match_configured_case(self, message: str) -> str:
        """
        Return the configured ticketing case text that matches this message.
        Empty string means no configured-case match.
        """
        configured_cases = self.get_configured_cases()
        if not configured_cases:
            return ""
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return ""
        return self._match_configured_ticketing_case(msg_lower, configured_cases)

    async def match_configured_case_async(
        self,
        *,
        message: str,
        conversation_excerpt: str = "",
        llm_response_text: str = "",
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> str:
        """
        LLM-first configured-case matching with deterministic fallback.
        """
        configured_cases = self.get_configured_cases()
        if not configured_cases:
            return ""
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return ""

        llm_match, llm_used = await self._llm_match_configured_ticketing_case(
            message=msg_lower,
            conversation_excerpt=conversation_excerpt,
            llm_response_text=llm_response_text,
            configured_cases=configured_cases,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
        )
        if llm_used and llm_match:
            logger.info(
                "ticketing_case_match source=llm matched_case=%s message=%s",
                llm_match,
                str(msg_lower)[:180],
            )
            return llm_match
        if not bool(getattr(settings, "ticketing_case_match_fallback_enabled", False)):
            if llm_used:
                logger.info(
                    "ticketing_case_match source=llm no_match fallback_disabled=true message=%s",
                    str(msg_lower)[:180],
                )
            return ""
        fallback_match = self._match_configured_ticketing_case(msg_lower, configured_cases)
        logger.info(
            "ticketing_case_match source=fallback matched_case=%s llm_used=%s message=%s",
            fallback_match,
            llm_used,
            str(msg_lower)[:180],
        )
        return fallback_match

    async def decide_async(
        self,
        *,
        intent: IntentType,
        message: str,
        llm_response_text: str,
        llm_ticketing_preference: bool | None,
        current_pending_action: str | None,
        pending_action_target: str | None,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
        conversation_excerpt: str = "",
    ) -> TicketingAgentDecision:
        """
        Async variant that lets LLM resolve configured ticketing-case matches
        from conversation context before running core routing logic.
        """
        configured_case_override = await self.match_configured_case_async(
            message=message,
            conversation_excerpt=conversation_excerpt,
            llm_response_text=llm_response_text,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
        )
        return self.decide(
            intent=intent,
            message=message,
            llm_response_text=llm_response_text,
            llm_ticketing_preference=llm_ticketing_preference,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            configured_case_match_override=configured_case_override,
        )

    def decide(
        self,
        *,
        intent: IntentType,
        message: str,
        llm_response_text: str,
        llm_ticketing_preference: bool | None,
        current_pending_action: str | None,
        pending_action_target: str | None,
        configured_case_match_override: str | None = None,
    ) -> TicketingAgentDecision:
        msg = str(message or "").strip().lower()
        pending_current = str(current_pending_action or "").strip().lower()
        pending_target = str(pending_action_target or "").strip().lower()

        plugin_service = self._get_ticketing_plugin_config()
        configured_cases: list[str] = []
        configured_case_match = ""
        configured_case_gate_active = False

        if plugin_service is not None:
            if not self._is_ticketing_plugin_enabled(plugin_service):
                return TicketingAgentDecision(
                    activate=False,
                    routed_intent=None,
                    route="none",
                    reason="ticketing_plugin_service_disabled",
                    source="service_toggle",
                )
            configured_cases = self.get_configured_cases()
            if configured_cases:
                if configured_case_match_override is not None:
                    configured_case_match = str(configured_case_match_override or "").strip()
                else:
                    configured_case_match = self._match_configured_ticketing_case(
                        msg,
                        configured_cases,
                    )
                configured_case_gate_active = True

        if bool(getattr(settings, "ticketing_agent_llm_only", True)):
            return self._decide_llm_only(
                intent=intent,
                llm_ticketing_preference=llm_ticketing_preference,
                current_pending_action=pending_current,
                pending_action_target=pending_target,
                configured_case_gate_active=configured_case_gate_active,
                configured_case_match=configured_case_match,
                message=msg,
            )

        if self._is_ticketing_pending_action(pending_target) or self._is_ticketing_pending_action(pending_current):
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.COMPLAINT,
                route="complaint_handler",
                reason="existing_ticketing_pending_action",
                source="pending_action",
            )

        if self._looks_like_explicit_ticket_command(msg):
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.COMPLAINT,
                route="complaint_handler",
                reason="explicit_ticket_request",
                source="user_message",
            )

        if configured_case_match and not self._looks_like_information_query(msg):
            if self._configured_case_is_transactional(configured_case_match):
                if intent == IntentType.HUMAN_REQUEST or self._looks_like_human_handoff_request(msg):
                    return TicketingAgentDecision(
                        activate=True,
                        routed_intent=IntentType.HUMAN_REQUEST,
                        route="escalation_handler",
                        reason="configured_transaction_case_human_request",
                        source="service_ticketing_cases",
                        matched_case=configured_case_match,
                    )
                if (
                    intent == IntentType.COMPLAINT
                    or self._looks_like_explicit_ticket_command(msg)
                    or self._looks_like_strong_issue_marker(msg)
                ):
                    return TicketingAgentDecision(
                        activate=True,
                        routed_intent=IntentType.COMPLAINT,
                        route="complaint_handler",
                        reason="configured_transaction_case_issue_signal",
                        source="service_ticketing_cases",
                        matched_case=configured_case_match,
                    )
                return TicketingAgentDecision(
                    activate=False,
                    routed_intent=None,
                    route="none",
                    reason="configured_transaction_case_deferred_until_confirmation",
                    source="service_ticketing_cases",
                    matched_case=configured_case_match,
                )
            routed_intent = (
                IntentType.HUMAN_REQUEST
                if self._configured_case_prefers_human_route(configured_case_match)
                else IntentType.COMPLAINT
            )
            has_escalation_signal = (
                intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}
                or self._looks_like_explicit_ticket_command(msg)
                or self._looks_like_strong_issue_marker(msg)
                or self._looks_like_operational_issue(msg)
                or llm_ticketing_preference is True
                or self._response_implies_staff_action(llm_response_text)
                or (routed_intent == IntentType.HUMAN_REQUEST and self._looks_like_human_handoff_request(msg))
            )
            if not has_escalation_signal:
                return TicketingAgentDecision(
                    activate=False,
                    routed_intent=None,
                    route="none",
                    reason="configured_case_match_missing_escalation_signal",
                    source="service_ticketing_cases",
                    matched_case=configured_case_match,
                )
            return TicketingAgentDecision(
                activate=True,
                routed_intent=routed_intent,
                route="escalation_handler" if routed_intent == IntentType.HUMAN_REQUEST else "complaint_handler",
                reason="configured_ticketing_case_match",
                source="service_ticketing_cases",
                matched_case=configured_case_match,
            )

        if intent == IntentType.HUMAN_REQUEST:
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.HUMAN_REQUEST,
                route="escalation_handler",
                reason=(
                    "human_request_configured_case_match"
                    if configured_case_match
                    else "human_request"
                ),
                source=(
                    "service_ticketing_cases"
                    if configured_case_match
                    else "intent"
                ),
                matched_case=configured_case_match,
            )

        in_order_or_booking_collection = (
            self._is_order_pending_action(pending_current)
            or self._is_order_pending_action(pending_target)
            or self._is_booking_pending_action(pending_current)
            or self._is_booking_pending_action(pending_target)
        )
        if (
            configured_case_gate_active
            and not configured_case_match
            and self._should_block_without_configured_case(
                intent=intent,
                msg=msg,
                llm_ticketing_preference=llm_ticketing_preference,
            )
        ):
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="no_matching_configured_ticketing_case",
                source="service_ticketing_cases",
            )
        if intent in {IntentType.COMPLAINT}:
            if in_order_or_booking_collection and not self._looks_like_strong_issue_marker(msg):
                return TicketingAgentDecision(
                    activate=False,
                    routed_intent=None,
                    route="none",
                    reason="complaint_intent_suppressed_during_transaction_slot_collection",
                    source="intent+pending_action",
                )
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.COMPLAINT,
                route="complaint_handler",
                reason="complaint_intent",
                source="intent",
            )

        if in_order_or_booking_collection:
            # Keep slot-filling stable unless user clearly switches to complaint/ticket.
            if self._looks_like_strong_issue_marker(msg):
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.COMPLAINT,
                    route="complaint_handler",
                    reason="explicit_issue_switch_during_transaction",
                    source="user_message",
                )
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="transaction_slot_collection_in_progress",
                source="pending_action",
            )

        if intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING}:
            # Preserve natural conversational flow; tickets are created on confirmation.
            if self._looks_like_strong_issue_marker(msg):
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.COMPLAINT,
                    route="complaint_handler",
                    reason="order_booking_message_contains_issue",
                    source="user_message",
                )
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="transaction_flow_deferred_ticket_until_confirmation",
                source="intent",
            )

        if intent == IntentType.ROOM_SERVICE:
            if self._looks_like_actionable_room_service_request(msg):
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.COMPLAINT,
                    route="complaint_handler",
                    reason="actionable_room_service_request",
                    source="intent+message",
                )
            if llm_ticketing_preference is True and not self._looks_like_information_query(msg):
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.COMPLAINT,
                    route="complaint_handler",
                    reason="llm_requested_ticket_for_room_service",
                    source="llm_signal",
                )
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="room_service_information_only",
                source="intent+message",
            )

        if llm_ticketing_preference is False:
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="llm_ticketing_preference_false",
                source="llm_signal",
            )

        if llm_ticketing_preference is True:
            if self._looks_like_operational_issue(msg) or self._response_implies_staff_action(llm_response_text):
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.COMPLAINT,
                    route="complaint_handler",
                    reason="llm_requested_ticket_with_operational_signal",
                    source="llm_signal",
                )
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="llm_requested_ticket_but_no_operational_signal",
                source="llm_signal",
            )

        if self._looks_like_operational_issue(msg):
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.COMPLAINT,
                route="complaint_handler",
                reason="operational_issue_detected",
                source="message",
            )

        return TicketingAgentDecision(
            activate=False,
            routed_intent=None,
            route="none",
            reason="no_ticketing_trigger",
            source="default",
        )

    def _decide_llm_only(
        self,
        *,
        intent: IntentType,
        llm_ticketing_preference: bool | None,
        current_pending_action: str,
        pending_action_target: str,
        configured_case_gate_active: bool,
        configured_case_match: str,
        message: str,
    ) -> TicketingAgentDecision:
        if self._is_ticketing_pending_action(pending_action_target) or self._is_ticketing_pending_action(
            current_pending_action
        ):
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.COMPLAINT,
                route="complaint_handler",
                reason="existing_ticketing_pending_action",
                source="pending_action",
            )

        in_order_or_booking_collection = (
            self._is_order_pending_action(current_pending_action)
            or self._is_order_pending_action(pending_action_target)
            or self._is_booking_pending_action(current_pending_action)
            or self._is_booking_pending_action(pending_action_target)
        )

        if intent == IntentType.HUMAN_REQUEST:
            if configured_case_match and self._configured_case_is_transactional(configured_case_match):
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.HUMAN_REQUEST,
                    route="escalation_handler",
                    reason="configured_transaction_case_human_request",
                    source="service_ticketing_cases",
                    matched_case=configured_case_match,
                )
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.HUMAN_REQUEST,
                route="escalation_handler",
                reason=(
                    "human_request_configured_case_match"
                    if configured_case_match
                    else "human_request"
                ),
                source=(
                    "service_ticketing_cases"
                    if configured_case_match
                    else "intent"
                ),
                matched_case=configured_case_match,
            )

        if configured_case_gate_active and not configured_case_match:
            if intent in {IntentType.COMPLAINT, IntentType.ROOM_SERVICE} or llm_ticketing_preference is True:
                return TicketingAgentDecision(
                    activate=False,
                    routed_intent=None,
                    route="none",
                    reason="no_matching_configured_ticketing_case",
                    source="service_ticketing_cases",
                )

        if configured_case_match:
            prefers_human = self._configured_case_prefers_human_route(configured_case_match)
            is_transactional = self._configured_case_is_transactional(configured_case_match)
            if is_transactional and intent not in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
                return TicketingAgentDecision(
                    activate=False,
                    routed_intent=None,
                    route="none",
                    reason="configured_transaction_case_deferred_until_confirmation",
                    source="service_ticketing_cases",
                    matched_case=configured_case_match,
                )
            if llm_ticketing_preference is True or intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
                routed_intent = IntentType.HUMAN_REQUEST if prefers_human else IntentType.COMPLAINT
                route = "escalation_handler" if routed_intent == IntentType.HUMAN_REQUEST else "complaint_handler"
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=routed_intent,
                    route=route,
                    reason="configured_ticketing_case_match",
                    source="service_ticketing_cases",
                    matched_case=configured_case_match,
                )
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="configured_case_match_missing_escalation_signal",
                source="service_ticketing_cases",
                matched_case=configured_case_match,
            )

        if intent == IntentType.COMPLAINT:
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.COMPLAINT,
                route="complaint_handler",
                reason="complaint_intent",
                source="intent",
            )

        if in_order_or_booking_collection:
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="transaction_slot_collection_in_progress",
                source="pending_action",
            )

        if intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING}:
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="transaction_flow_deferred_ticket_until_confirmation",
                source="intent",
            )

        if intent == IntentType.ROOM_SERVICE:
            if llm_ticketing_preference is True:
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.COMPLAINT,
                    route="complaint_handler",
                    reason="llm_requested_ticket_for_room_service",
                    source="llm_signal",
                )
            if llm_ticketing_preference is False:
                return TicketingAgentDecision(
                    activate=False,
                    routed_intent=None,
                    route="none",
                    reason="room_service_information_only",
                    source="intent+message",
                )
            if self._looks_like_actionable_room_service_request(message):
                return TicketingAgentDecision(
                    activate=True,
                    routed_intent=IntentType.COMPLAINT,
                    route="complaint_handler",
                    reason="actionable_room_service_request",
                    source="intent+message",
                )
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="room_service_information_only",
                source="intent+message",
            )

        if llm_ticketing_preference is True:
            return TicketingAgentDecision(
                activate=True,
                routed_intent=IntentType.COMPLAINT,
                route="complaint_handler",
                reason="llm_requested_ticket",
                source="llm_signal",
            )
        if llm_ticketing_preference is False:
            return TicketingAgentDecision(
                activate=False,
                routed_intent=None,
                route="none",
                reason="llm_ticketing_preference_false",
                source="llm_signal",
            )
        return TicketingAgentDecision(
            activate=False,
            routed_intent=None,
            route="none",
            reason="no_ticketing_trigger_llm_only",
            source="llm_mode",
        )

    @staticmethod
    def _is_ticketing_plugin_enabled(config_row: dict[str, Any]) -> bool:
        enabled_value = config_row.get("enabled")
        if enabled_value is None:
            enabled_value = config_row.get("is_active", True)
        plugin_enabled_value = config_row.get("ticketing_plugin_enabled")
        if plugin_enabled_value is None:
            plugin_enabled_value = True
        return bool(enabled_value) and bool(plugin_enabled_value)

    @classmethod
    def _get_ticketing_plugin_tool_config(cls) -> dict[str, Any] | None:
        try:
            tools = config_service.get_tools()
        except Exception:
            return None
        if not isinstance(tools, list):
            return None
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = str(tool.get("id") or "").strip().lower()
            handler = str(tool.get("handler") or "").strip().lower()
            tool_type = str(tool.get("type") or "").strip().lower()
            has_cases = isinstance(tool.get("ticketing_cases"), list)
            has_plugin_flag = "ticketing_plugin_enabled" in tool
            if tool_id in {"ticketing", "ticketing_plugin", "ticketing_agent"}:
                if has_cases or has_plugin_flag or handler == "ticket_create":
                    return dict(tool)
            if handler == "ticket_create":
                return dict(tool)
            if tool_type in {"workflow", "plugin"} and has_cases:
                return dict(tool)
        return None

    @classmethod
    def _get_ticketing_plugin_service_config(cls) -> dict[str, Any] | None:
        try:
            services = config_service.get_services()
        except Exception:
            return None
        if not isinstance(services, list):
            return None
        for service in services:
            if not isinstance(service, dict):
                continue
            service_id = str(service.get("id") or "").strip().lower()
            service_type = str(service.get("type") or "").strip().lower()
            service_name = str(service.get("name") or "").strip().lower()
            if service_id in {"ticketing_agent", "ticketing_plugin", "ticketing"}:
                return dict(service)
            if service_type == "plugin" and "ticket" in service_name:
                return dict(service)
            if bool(service.get("ticketing_plugin_enabled", False)):
                return dict(service)
            if isinstance(service.get("ticketing_cases"), list):
                return dict(service)
        return None

    @classmethod
    def _get_ticketing_plugin_config(cls) -> dict[str, Any] | None:
        tool_config = cls._get_ticketing_plugin_tool_config()
        if tool_config is not None:
            return tool_config
        return cls._get_ticketing_plugin_service_config()

    @staticmethod
    def _extract_ticketing_cases(service_config: dict[str, Any]) -> list[str]:
        cases = service_config.get("ticketing_cases")
        if not isinstance(cases, list):
            return []
        extracted: list[str] = []
        for item in cases:
            if isinstance(item, dict):
                text = str(item.get("description") or item.get("case") or item.get("label") or "").strip()
            else:
                text = str(item or "").strip()
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text)
            if normalized and normalized not in extracted:
                extracted.append(normalized)
        return extracted[:40]

    @staticmethod
    def _configured_case_prefers_human_route(case_text: str) -> bool:
        text = str(case_text or "").strip().lower()
        if not text:
            return False
        markers = (
            "human",
            "agent",
            "live chat",
            "handoff",
            "escalation",
            "escalate",
            "call me",
            "callback",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _configured_case_is_transactional(case_text: str) -> bool:
        tokens = TicketingAgentService._tokenize_case_text(case_text)
        if not tokens:
            return False
        domain_tokens = {"table", "room", "spa", "transport", "food"}
        if not (tokens & domain_tokens):
            return False
        if "human" in tokens and "book" not in tokens and "food" not in tokens:
            return False
        if "complaint" in tokens and "book" not in tokens and "food" not in tokens:
            return False
        # Transactional cases are booking/order style requests within a service domain.
        if "book" in tokens or "food" in tokens or "transport" in tokens:
            return True
        return len(tokens & domain_tokens) >= 2

    @staticmethod
    def _tokenize_case_text(value: str) -> set[str]:
        normalized_tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            normalized = TicketingAgentService._normalize_semantic_token(token)
            if not normalized:
                continue
            if len(normalized) < 3:
                continue
            if normalized in _TICKETING_CASE_STOPWORDS:
                continue
            normalized_tokens.add(normalized)
        return TicketingAgentService._expand_semantic_tokens(normalized_tokens)

    @staticmethod
    def _normalize_semantic_token(token: str) -> str:
        text = str(token or "").strip().lower()
        if not text:
            return ""
        squash_map = {
            "checkin": "checkin",
            "check": "check",
            "checkout": "checkout",
            "check-in": "checkin",
            "check-out": "checkout",
            "complaints": "complaint",
            "issues": "issue",
            "problems": "problem",
            "arranged": "arrange",
            "requested": "request",
            "requests": "request",
            "reserving": "reserve",
            "reservation": "reserve",
            "bookings": "book",
            "booking": "book",
            "ordered": "order",
            "ordering": "order",
            "orders": "order",
            "pickup": "pickup",
            "pick": "pickup",
            "dropoff": "drop",
            "dropped": "drop",
            "transfers": "transfer",
            "transferring": "transfer",
        }
        if text in squash_map:
            return squash_map[text]
        if text.endswith("ing") and len(text) > 5:
            text = text[:-3]
        elif text.endswith("ed") and len(text) > 4:
            text = text[:-2]
        elif text.endswith("s") and len(text) > 4:
            text = text[:-1]
        return text

    @staticmethod
    def _expand_semantic_tokens(tokens: set[str]) -> set[str]:
        expanded = set(tokens)
        if not expanded:
            return expanded
        for canonical, aliases in _SEMANTIC_TOPIC_ALIASES.items():
            if canonical in expanded or any(alias in expanded for alias in aliases):
                expanded.add(canonical)
        return expanded

    def _match_configured_ticketing_case(self, msg_lower: str, configured_cases: list[str]) -> str:
        text = str(msg_lower or "").strip().lower()
        if not text or not configured_cases:
            return ""

        best_case = ""
        best_score = 0.0

        for case in configured_cases:
            case_text = str(case or "").strip().lower()
            if not case_text:
                continue
            score = self._case_semantic_match_score(case_text=case_text, msg_text=text)
            if self._case_semantically_matches_message(case_text, text):
                score = max(score, 0.74)

            if score > best_score:
                best_score = score
                best_case = case

        if best_score >= 0.62:
            return best_case
        if best_score >= 0.48:
            msg_tokens = self._tokenize_case_text(text)
            case_tokens = self._tokenize_case_text(str(best_case).lower())
            topic_overlap = len((msg_tokens & case_tokens) & _SEMANTIC_TOPICS)
            if topic_overlap >= 1:
                return best_case
        return ""

    def _case_semantic_match_score(self, *, case_text: str, msg_text: str) -> float:
        case_tokens = self._tokenize_case_text(case_text)
        msg_tokens = self._tokenize_case_text(msg_text)
        if not case_tokens or not msg_tokens:
            return SequenceMatcher(None, msg_text, case_text).ratio() * 0.4

        shared = case_tokens & msg_tokens
        coverage = len(shared) / max(1, len(case_tokens))
        precision = len(shared) / max(1, len(msg_tokens))
        ratio = SequenceMatcher(None, msg_text, case_text).ratio()
        topic_overlap = len(shared & _SEMANTIC_TOPICS)
        score = (coverage * 0.62) + (precision * 0.16) + (ratio * 0.22) + min(0.14, topic_overlap * 0.06)

        case_prefers_human = self._configured_case_prefers_human_route(case_text)
        msg_has_human = bool(msg_tokens & {"human"})
        if case_prefers_human and msg_has_human:
            score += 0.18

        msg_looks_issue = self._looks_like_operational_issue(msg_text)
        case_has_issue = bool(case_tokens & {"complaint"})
        if case_has_issue and msg_looks_issue:
            score += 0.18

        if "book" in case_tokens and "book" in msg_tokens and topic_overlap >= 1:
            score += 0.16
        return min(score, 1.0)

    def _case_semantically_matches_message(self, case_text: str, msg_text: str) -> bool:
        case_lower = str(case_text or "").strip().lower()
        msg_lower = str(msg_text or "").strip().lower()
        if not case_lower or not msg_lower:
            return False
        score = self._case_semantic_match_score(case_text=case_lower, msg_text=msg_lower)
        if score >= 0.62:
            return True
        case_tokens = self._tokenize_case_text(case_lower)
        msg_tokens = self._tokenize_case_text(msg_lower)
        shared_topics = (case_tokens & msg_tokens) & _SEMANTIC_TOPICS
        if "complaint" in case_tokens and self._looks_like_operational_issue(msg_lower):
            return True
        if "human" in case_tokens and "human" in msg_tokens:
            return True
        non_verb_shared_topics = set(shared_topics) - {"book"}
        if "book" in case_tokens and "book" in msg_tokens and non_verb_shared_topics:
            return True
        return False

    @staticmethod
    def _contains_booking_domain_signal(text: str) -> bool:
        tokens = TicketingAgentService._tokenize_case_text(text)
        if not tokens:
            return False
        return bool(tokens & {"table", "room", "spa", "transport", "food", "book"})

    def _llm_case_match_is_consistent(
        self,
        *,
        case_text: str,
        message: str,
        llm_response_text: str,
        conversation_excerpt: str,
    ) -> bool:
        primary_text = " ".join(
            part.strip()
            for part in (str(message or ""), str(llm_response_text or ""))
            if str(part or "").strip()
        ).strip().lower()
        if self._case_semantically_matches_message(case_text, primary_text):
            return True
        if self._contains_booking_domain_signal(primary_text):
            return False
        excerpt_text = str(conversation_excerpt or "").strip().lower()
        if excerpt_text and self._case_semantically_matches_message(case_text, excerpt_text):
            return True
        return False

    def _should_block_without_configured_case(
        self,
        *,
        intent: IntentType,
        msg: str,
        llm_ticketing_preference: bool | None,
    ) -> bool:
        if intent in {IntentType.HUMAN_REQUEST}:
            return False
        if self._looks_like_explicit_ticket_command(msg):
            return False
        if intent in {IntentType.COMPLAINT, IntentType.ROOM_SERVICE}:
            return True
        if llm_ticketing_preference is True:
            return True
        if self._looks_like_operational_issue(msg):
            return True
        return False

    @staticmethod
    def _coerce_optional_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if not text:
            return None
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return None

    def _normalize_case_to_configured(
        self,
        *,
        candidate: str,
        configured_cases: list[str],
    ) -> str:
        cand = str(candidate or "").strip().lower()
        if not cand:
            return ""

        for case in configured_cases:
            case_text = str(case or "").strip().lower()
            if not case_text:
                continue
            if cand == case_text:
                return case

        for case in configured_cases:
            case_text = str(case or "").strip().lower()
            if not case_text:
                continue
            if cand in case_text or case_text in cand:
                return case

        best_case = ""
        best_score = 0.0
        for case in configured_cases:
            case_text = str(case or "").strip().lower()
            if not case_text:
                continue
            ratio = SequenceMatcher(None, cand, case_text).ratio()
            if ratio > best_score:
                best_score = ratio
                best_case = case
        if best_score >= 0.64:
            return best_case
        return ""

    async def _llm_match_configured_ticketing_case(
        self,
        *,
        message: str,
        conversation_excerpt: str,
        llm_response_text: str,
        configured_cases: list[str],
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> tuple[str, bool]:
        """
        Returns (matched_case, llm_used).
        llm_used=True means model produced a usable decision, including no-match.
        llm_used=False means fallback logic should run.
        """
        if not bool(getattr(settings, "ticketing_case_match_use_llm", True)):
            return "", False
        if not str(settings.openai_api_key or "").strip():
            return "", False
        if not configured_cases:
            return "", False

        phase_label = str(selected_phase_name or "").strip() or (
            str(selected_phase_id or "").replace("_", " ").title()
        )
        phase_id = str(selected_phase_id or "").strip() or "unknown"
        context_chars = max(1200, int(getattr(settings, "ticketing_case_match_context_chars", 5000) or 5000))
        latest_user_preview = str(message or "").strip()[:800] or "(none)"
        assistant_preview = str(llm_response_text or "").strip()[:1200] or "(none)"
        conversation_preview = str(conversation_excerpt or "").strip()
        if len(conversation_preview) > context_chars:
            conversation_preview = conversation_preview[-context_chars:]
        prompt = (
            "You are a ticketing-case matcher.\n"
            "Decide if the latest user request should create a ticket under one of configured ticketing cases.\n\n"
            "Rules:\n"
            "1) Use latest user message + conversation excerpt + assistant response.\n"
            "2) Choose a case only when domain and objective both match.\n"
            "3) If no configured case fits exactly, return should_create_ticket=false.\n"
            "4) Do not force-match across different service domains "
            "(for example, transport must not map to table booking).\n"
            "5) Do not match only because generic words overlap (book, request, service, support).\n"
            "6) If a case fits, return that exact configured case text.\n"
            "6.1) Treat explicit ticket/escalation asks and clear operational issue wording as escalation signals.\n"
            "7) Output strict JSON only:\n"
            "{\"should_create_ticket\":true|false, \"matched_case\":\"...\", \"reason\":\"...\"}\n\n"
            f"Selected user journey phase: {phase_label} ({phase_id})\n"
            f"Configured cases: {configured_cases}\n"
            f"Latest user message: {latest_user_preview}\n"
            f"Assistant response draft: {assistant_preview}\n"
            f"Conversation excerpt: {conversation_preview or '(none)'}"
        )
        messages = [
            {"role": "system", "content": "You return strict JSON for ticketing-case matching."},
            {"role": "user", "content": prompt},
        ]
        model = str(getattr(settings, "ticketing_case_match_model", "") or "").strip() or None
        try:
            parsed = await llm_client.chat_with_json(messages, model=model, temperature=0.0)
        except Exception:
            logger.exception("ticketing_case_match_llm_failed")
            return "", False

        if not isinstance(parsed, dict):
            return "", False

        should_create = self._coerce_optional_bool(parsed.get("should_create_ticket"))
        matched_raw = str(parsed.get("matched_case") or parsed.get("case") or "").strip()
        matched_normalized = self._normalize_case_to_configured(
            candidate=matched_raw,
            configured_cases=configured_cases,
        )

        if should_create is False:
            return "", True
        if matched_normalized:
            if not self._llm_case_match_is_consistent(
                case_text=matched_normalized,
                message=message,
                llm_response_text=llm_response_text,
                conversation_excerpt=conversation_excerpt,
            ):
                return "", True
            return matched_normalized, True
        if should_create is True and not matched_normalized:
            # Model said yes but gave no valid configured case; treat as no-match.
            return "", True
        # Ambiguous model output, use deterministic fallback.
        return "", False

    @staticmethod
    def _is_ticketing_pending_action(action: str | None) -> bool:
        return str(action or "").strip().lower() in _TICKETING_PENDING_ACTIONS

    @staticmethod
    def _is_order_pending_action(action: str | None) -> bool:
        return str(action or "").strip().lower() in _ORDER_PENDING_ACTIONS

    @staticmethod
    def _is_booking_pending_action(action: str | None) -> bool:
        return str(action or "").strip().lower() in _BOOKING_PENDING_ACTIONS

    @staticmethod
    def _looks_like_information_query(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        if "?" in text:
            return True
        info_markers = (
            "what",
            "which",
            "when",
            "where",
            "how",
            "show",
            "list",
            "menu",
            "options",
            "available",
            "amenities",
            "inquire",
            "check",
        )
        return any(text.startswith(token) for token in info_markers)

    @staticmethod
    def _looks_like_explicit_ticket_command(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        markers = (
            "create ticket",
            "raise ticket",
            "open ticket",
            "log complaint",
            "file complaint",
            "ticket status",
            "update ticket",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _looks_like_actionable_room_service_request(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False

        # Guard: stay-booking language should not be treated as room-service ticketing.
        stay_booking_markers = (
            "book a room",
            "book room",
            "room booking",
            "need a room",
            "want a room",
            "room for",
            "check in",
            "check-in",
            "check out",
            "check-out",
            "stay",
        )
        if any(marker in text for marker in stay_booking_markers):
            return False

        if TicketingAgentService._looks_like_information_query(text):
            # For explicit "request verbs", still consider actionable.
            action_verbs = ("need", "send", "bring", "arrange", "require", "please")
            if not any(verb in text for verb in action_verbs):
                return False

        request_markers = (
            "i need",
            "need ",
            "please send",
            "send ",
            "bring ",
            "arrange ",
            "housekeeping",
            "towel",
            "blanket",
            "pillow",
            "soap",
            "shampoo",
            "toothbrush",
            "hairdryer",
            "laundry",
            "clean room",
            "cleaning",
            "maintenance",
            "ac not",
            "not working",
            "broken",
            "leak",
        )
        return any(marker in text for marker in request_markers)

    @staticmethod
    def _looks_like_strong_issue_marker(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        markers = (
            "complaint",
            "issue",
            "problem",
            "not working",
            "broken",
            "dirty",
            "cockroach",
            "roach",
            "refund",
            "wrong charge",
            "billing issue",
            "technical issue",
            "technical error",
            "booking failed",
            "payment failed",
            "otp",
            "login issue",
            "unable to book",
            "can't book",
            "cannot book",
            "manager",
            "escalate",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _looks_like_human_handoff_request(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        markers = (
            "human",
            "live agent",
            "agent",
            "representative",
            "support team",
            "talk to",
            "speak to",
            "connect me",
            "manager",
            "callback",
            "call me",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _looks_like_operational_issue(msg_lower: str) -> bool:
        return TicketingAgentService._looks_like_actionable_room_service_request(msg_lower) or TicketingAgentService._looks_like_strong_issue_marker(msg_lower)

    @staticmethod
    def _response_implies_staff_action(response_text: str) -> bool:
        text = str(response_text or "").strip().lower()
        if not text:
            return False
        markers = (
            "i will escalate",
            "i'll escalate",
            "our team",
            "our staff",
            "will arrange",
            "priority follow-up",
            "someone will contact",
        )
        return any(marker in text for marker in markers)


ticketing_agent_service = TicketingAgentService()
