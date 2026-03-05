"""
Health Support Handler

Handles medication/health-support requests with a strict safety policy:
- no diagnosis
- no dosage instructions
- route to human support quickly
"""

from __future__ import annotations

import re
from typing import Any

from handlers.base_handler import BaseHandler, HandlerResult
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType
from services.config_service import config_service


class HealthSupportHandler(BaseHandler):
    """Safety-first workflow for medication/health-support requests."""

    _EMERGENCY_MARKERS = (
        "emergency",
        "chest pain",
        "cant breathe",
        "can't breathe",
        "difficulty breathing",
        "severe bleeding",
        "unconscious",
        "fainted",
        "heart attack",
        "stroke",
        "anaphylaxis",
        "overdose",
        "suicidal",
        "not breathing",
    )

    _YES_MARKERS = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "please do", "connect me"}
    _NO_MARKERS = {"no", "n", "nope", "cancel", "stop", "not now", "no thanks", "no thank you"}
    _ROOM_PATTERN = re.compile(r"\broom(?:\s*number)?\s*(?:is|:|#)?\s*([a-z0-9-]{2,10})\b", re.IGNORECASE)

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        msg_lower = str(message or "").strip().lower()

        if context.pending_action == "confirm_health_support":
            return self._handle_confirmation(msg_lower, intent_result, context)

        if self._is_emergency_request(msg_lower):
            return self._build_emergency_result(message, context)

        room_number = context.room_number or self._extract_room_number(message)
        pending_data = {
            "health_request": str(message or "").strip(),
            "room_number": room_number,
        }

        return HandlerResult(
            response_text=(
                "I can help coordinate medication or medical support, but I can't provide diagnosis or dosage advice. "
                "Would you like me to connect you with our team now?"
            ),
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_health_support",
            pending_data=pending_data,
            suggested_actions=["Yes, connect me", "No, thanks"],
            metadata={
                "health_support": True,
                "health_support_severity": "non_emergency",
                "room_number": room_number,
            },
        )

    def _handle_confirmation(
        self,
        msg_lower: str,
        intent_result: IntentResult,
        context: ConversationContext,
    ) -> HandlerResult:
        normalized = re.sub(r"\s+", " ", str(msg_lower or "").strip())

        is_yes = (
            intent_result.intent == IntentType.CONFIRMATION_YES
            or normalized in self._YES_MARKERS
            or normalized.startswith("yes")
        )
        is_no = (
            intent_result.intent == IntentType.CONFIRMATION_NO
            or normalized in self._NO_MARKERS
            or normalized.startswith("no")
        )

        if is_yes:
            if config_service.is_capability_enabled("human_escalation"):
                return HandlerResult(
                    response_text=(
                        "Understood. I'm connecting you with our team right away for medical assistance. "
                        "If this is urgent, please contact emergency services and the front desk immediately."
                    ),
                    next_state=ConversationState.ESCALATED,
                    pending_action=None,
                    pending_data={},
                    suggested_actions=["Return to bot"],
                    metadata={
                        "health_support": True,
                        "escalated": True,
                        "escalation_reason": "health_support_requested",
                    },
                )
            return HandlerResult(
                response_text=(
                    "I'm unable to start a live handoff right now. "
                    "Please contact the front desk immediately for medical assistance."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=config_service.get_quick_actions(limit=4),
                metadata={
                    "health_support": True,
                    "escalated": False,
                    "escalation_unavailable": True,
                },
            )

        if is_no:
            return HandlerResult(
                response_text=(
                    "Understood. If you need medical help at any time, message me and I'll connect you to our team immediately."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=config_service.get_quick_actions(limit=4),
                metadata={
                    "health_support": True,
                    "escalated": False,
                },
            )

        return HandlerResult(
            response_text="Please reply with 'Yes' to connect with our team, or 'No' to cancel.",
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_health_support",
            pending_data=context.pending_data,
            suggested_actions=["Yes, connect me", "No, thanks"],
            metadata={"health_support": True},
        )

    def _build_emergency_result(self, message: str, context: ConversationContext) -> HandlerResult:
        room_number = context.room_number or self._extract_room_number(message)
        if config_service.is_capability_enabled("human_escalation"):
            return HandlerResult(
                response_text=(
                    "This sounds urgent. Please call emergency services and contact the front desk immediately. "
                    "I'm alerting our team now."
                ),
                next_state=ConversationState.ESCALATED,
                pending_action=None,
                pending_data={},
                suggested_actions=["Return to bot"],
                metadata={
                    "health_support": True,
                    "health_support_severity": "emergency",
                    "escalated": True,
                    "escalation_reason": "health_emergency",
                    "room_number": room_number,
                },
            )

        return HandlerResult(
            response_text=(
                "This sounds urgent. Please call emergency services and contact the front desk immediately for urgent help."
            ),
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=config_service.get_quick_actions(limit=4),
            metadata={
                "health_support": True,
                "health_support_severity": "emergency",
                "escalated": False,
                "room_number": room_number,
            },
        )

    def _is_emergency_request(self, msg_lower: str) -> bool:
        if not msg_lower:
            return False
        return any(marker in msg_lower for marker in self._EMERGENCY_MARKERS)

    def _extract_room_number(self, message: str) -> str | None:
        match = self._ROOM_PATTERN.search(str(message or ""))
        if not match:
            return None
        value = str(match.group(1) or "").strip().upper()
        return value or None
