"""
Chat Service

Main orchestrator for handling chat messages.
Uses handler registry for structured intents, falls back to LLM for unhandled ones.
Now uses config_service for business configuration (synced with Admin Portal).
"""

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Optional
from pydantic import BaseModel, Field

from schemas.chat import (
    ChatRequest,
    ChatResponse,
    ConversationContext,
    ConversationState,
    IntentType,
    IntentResult,
    MessageRole,
)
from schemas.orchestration import OrchestrationDecision
from core.complexity_router import ProcessingPath, complexity_router
from core.context_manager import context_manager
from core.state_machine import state_machine
from llm.client import llm_client
from agents.complex_query_orchestrator import complex_query_orchestrator
from config.settings import settings
from services.config_service import config_service
from services.conversation_memory_service import conversation_memory_service
from services.full_kb_llm_service import full_kb_llm_service
from services.agent_plugin_service import agent_plugin_service
from services.response_validator import response_validator
from services.ticketing_agent_service import ticketing_agent_service
from services.ticketing_service import ticketing_service
from services.llm_orchestration_service import llm_orchestration_service
from services.orchestration_policy_service import orchestration_policy_service
from handlers import handler_registry
from handlers.base_handler import HandlerResult

logger = logging.getLogger(__name__)


class CapabilityCheck(BaseModel):
    """Result of a capability check."""
    allowed: bool
    reason: str
    alternatives: list[str] = Field(default=[])
    constraints: Optional[dict] = None


# Map pending_action values to the IntentType whose handler should process
# contextual follow-ups.
_PENDING_ACTION_TO_INTENT = {
    "confirm_order": IntentType.ORDER_FOOD,
    "confirm_booking": IntentType.TABLE_BOOKING,
    "confirm_room_booking": IntentType.TABLE_BOOKING,
    "confirm_room_availability_check": IntentType.TABLE_BOOKING,
    "escalate_complaint": IntentType.COMPLAINT,
    "collect_ticket_room_number": IntentType.COMPLAINT,
    "collect_ticket_issue_details": IntentType.COMPLAINT,
    "collect_ticket_identity_details": IntentType.COMPLAINT,
    "confirm_ticket_creation": IntentType.COMPLAINT,
    "collect_ticket_update_note": IntentType.COMPLAINT,
    "confirm_ticket_escalation": IntentType.COMPLAINT,
    "show_menu": IntentType.FAQ,
    "select_service": IntentType.TABLE_BOOKING,
    "select_restaurant": IntentType.TABLE_BOOKING,
    "collect_booking_party_size": IntentType.TABLE_BOOKING,
    "collect_booking_time": IntentType.TABLE_BOOKING,
    "collect_room_booking_details": IntentType.TABLE_BOOKING,
    "select_room_type": IntentType.TABLE_BOOKING,
    "awaiting_request_detail": IntentType.ROOM_SERVICE,
    "awaiting_room_number": IntentType.ROOM_SERVICE,
    "collect_service_details": IntentType.FAQ,
}

# Detail-collection actions should continue deterministically even when
# the LLM classifies a short reply with low confidence.
_DETAIL_COLLECTION_PENDING_ACTIONS = {
    "select_service",
    "select_restaurant",
    "collect_booking_party_size",
    "collect_booking_time",
    "collect_room_booking_details",
    "select_room_type",
    "collect_ticket_room_number",
    "collect_ticket_issue_details",
    "collect_ticket_identity_details",
    "collect_ticket_update_note",
    "awaiting_request_detail",
    "awaiting_room_number",
    "collect_transport_details",
    "collect_service_details",
}

_TICKETING_PENDING_ACTIONS = {
    "escalate_complaint",
    "collect_ticket_room_number",
    "collect_ticket_issue_details",
    "collect_ticket_identity_details",
    "confirm_ticket_creation",
    "collect_ticket_update_note",
    "confirm_ticket_escalation",
}

_INTERNAL_PENDING_TICKET_DRAFT_KEY = "_pending_ticket_draft"
_INTERNAL_TASK_STATE_KEY = "_task_state"
_MAX_PARKED_TASKS = 5

_RETURN_TO_BOT_MARKERS = {
    "return to bot",
    "back to bot",
    "resume bot",
    "switch to bot",
    "continue with bot",
}

_RESUME_TASK_MARKERS = {
    "resume",
    "continue",
    "continue pending",
    "resume pending",
    "resume task",
    "continue task",
    "yes resume",
    "resume previous",
}

_CANCEL_TASK_MARKERS = {
    "cancel pending",
    "cancel task",
    "drop task",
    "cancel previous",
    "skip pending",
}

_INFORMATION_QUERY_PREFIXES = (
    "what",
    "when",
    "where",
    "which",
    "who",
    "how",
    "is",
    "are",
    "do",
    "can",
)

_INFORMATION_QUERY_IMPERATIVE_PREFIXES = (
    "tell me",
    "give me",
    "show me",
    "share",
    "explain",
    "describe",
)

_GENERIC_SERVICE_ALIASES = {
    "service",
    "services",
    "booking",
    "order",
    "support",
}

_BROAD_FIRST_ALIAS_TOKENS = {
    "hotel",
    "room",
    "service",
    "services",
    "support",
    "booking",
    "request",
    "enquiry",
    "sales",
    "status",
    "modification",
    "coordination",
    "help",
}

_SERVICE_DESCRIPTION_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "your",
    "our",
    "can",
    "you",
    "are",
    "is",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "or",
    "service",
    "services",
    "support",
    "assistance",
    "help",
}

_PHASE_INTENT_SERVICE_MARKERS = {
    IntentType.ORDER_FOOD: (
        "food",
        "dining",
        "restaurant",
        "menu",
        "meal",
        "in-room dining",
        "room dining",
        "aviator",
        "kadak",
        "swim club",
    ),
    IntentType.TABLE_BOOKING: (
        "booking",
        "book",
        "reservation",
        "appointment",
        "checkin",
        "checkout",
        "check in",
        "check out",
        "status",
        "modification",
        "cancellation",
        "transfer",
        "transport",
        "cab",
        "taxi",
        "pickup",
        "drop",
        "table",
        "restaurant",
        "spa",
        "recreation",
    ),
    IntentType.ROOM_SERVICE: (
        "room service",
        "housekeeping",
        "maintenance",
        "laundry",
        "amenity",
        "front desk",
        "support",
    ),
}

_PHASE_INTENT_SERVICE_MARKERS_STRICT = {
    IntentType.ORDER_FOOD: _PHASE_INTENT_SERVICE_MARKERS[IntentType.ORDER_FOOD],
    IntentType.TABLE_BOOKING: (
        "spa",
        "treatment",
        "table",
        "reservation",
        "restaurant",
        "recreation",
        "appointment",
        "transport",
        "cab",
        "taxi",
        "pickup",
        "drop",
        "airport transfer",
    ),
    IntentType.ROOM_SERVICE: _PHASE_INTENT_SERVICE_MARKERS[IntentType.ROOM_SERVICE],
}


class ChatService:
    """Handles chat message processing."""

    async def process_message(self, request: ChatRequest, db_session=None) -> ChatResponse:
        """Process an incoming chat message and generate response."""

        # 1. Get or create conversation context
        request_channel = (
            request.channel
            or request.metadata.get("channel")
            or None
        )
        resolved_hotel_code = config_service.resolve_hotel_code(request.hotel_code)
        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=resolved_hotel_code,
            guest_phone=request.guest_phone,
            channel=request_channel,
            db_session=db_session,
        )
        # Keep existing sessions aligned with canonical tenant mapping.
        if context.hotel_code != resolved_hotel_code:
            context.hotel_code = resolved_hotel_code
        if request_channel:
            context.channel = request_channel
        self._ingest_request_metadata(context, request)
        selected_phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        raw_user_message = str(request.message or "")
        preprocessed_user_message = await self._preprocess_user_message_with_llm(
            raw_user_message,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
        )
        effective_user_message = preprocessed_user_message or raw_user_message
        request_for_processing = (
            request.model_copy(update={"message": effective_user_message})
            if effective_user_message != raw_user_message
            else request
        )

        # 2. Add user message to history
        user_metadata = {"channel": context.channel}
        if effective_user_message != raw_user_message:
            user_metadata["preprocessed_message"] = effective_user_message
        context.add_message(
            MessageRole.USER,
            raw_user_message,
            metadata=user_metadata,
        )
        conversation_memory_service.capture_user_message(context, raw_user_message)
        await conversation_memory_service.maybe_refresh_summary(context)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        # 2a. Route complexity (initial hybrid architecture implementation)
        routing_decision = complexity_router.route(effective_user_message, context)
        history_window = self._resolve_history_window(context)

        # 3. Get hotel capabilities from config_service (synced with Admin Portal)
        capabilities_summary = config_service.get_capability_summary(context.hotel_code)
        capabilities_summary = await self._augment_capabilities_from_db(
            capabilities_summary,
            db_session,
        )
        self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context,
        )

        no_template_mode = bool(getattr(settings, "chat_no_template_response_mode", False))
        if no_template_mode:
            no_template_response = await self._process_policy_verified_llm_message(
                request=request_for_processing,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                selected_phase_context=selected_phase_context,
                db_session=db_session,
            )
            if no_template_response is not None:
                return no_template_response

        parked_task = self._peek_parked_task(context)
        if context.pending_action is None and parked_task is not None:
            if self._is_resume_task_request(effective_user_message) or self._is_resume_task_request(raw_user_message):
                resumed_task = self._restore_parked_task(context)
                resumed_summary = str((resumed_task or {}).get("summary") or "your previous request").strip()
                response_text = (
                    f"Resuming {resumed_summary}. "
                    "Please continue with the remaining details."
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.95, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="parked_task_resume",
                    validator_replaced=False,
                )
                assistant_metadata = {
                    "intent": IntentType.FAQ.value,
                    "confidence": 0.95,
                    "channel": context.channel,
                    "parked_task_resumed": True,
                    "parked_task_summary": resumed_summary,
                }
                context.add_message(
                    MessageRole.ASSISTANT,
                    response_text,
                    metadata=assistant_metadata,
                )
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata=assistant_metadata,
                )
                await conversation_memory_service.maybe_refresh_summary(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                remaining_parked = self._ensure_internal_task_state(context).get("parked_tasks", [])
                remaining_count = len(remaining_parked) if isinstance(remaining_parked, list) else 0

                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=IntentType.FAQ,
                    confidence=0.95,
                    state=context.state,
                    suggested_actions=self._build_contextual_suggested_actions(
                        context.state,
                        IntentType.FAQ,
                        context.pending_action,
                        capabilities_summary,
                        context.pending_data,
                    ),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "parked_task_resume"},
                        "response_source": "parked_task_resume",
                        "parked_task_resumed": True,
                        "parked_task_summary": resumed_summary,
                        "parked_tasks_remaining": remaining_count,
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

            if self._is_cancel_task_request(effective_user_message) or self._is_cancel_task_request(raw_user_message):
                canceled_task = self._cancel_parked_task(context)
                canceled_summary = str((canceled_task or {}).get("summary") or "that pending request").strip()
                response_text = (
                    f"Cancelled {canceled_summary}. "
                    "What would you like to do next?"
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.95, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="parked_task_cancel",
                    validator_replaced=False,
                )
                context.state = ConversationState.IDLE
                assistant_metadata = {
                    "intent": IntentType.FAQ.value,
                    "confidence": 0.95,
                    "channel": context.channel,
                    "parked_task_cancelled": True,
                    "parked_task_summary": canceled_summary,
                }
                context.add_message(
                    MessageRole.ASSISTANT,
                    response_text,
                    metadata=assistant_metadata,
                )
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata=assistant_metadata,
                )
                await conversation_memory_service.maybe_refresh_summary(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                remaining_parked = self._ensure_internal_task_state(context).get("parked_tasks", [])
                remaining_count = len(remaining_parked) if isinstance(remaining_parked, list) else 0

                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=IntentType.FAQ,
                    confidence=0.95,
                    state=ConversationState.IDLE,
                    suggested_actions=config_service.get_quick_actions(limit=4),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "parked_task_cancel"},
                        "response_source": "parked_task_cancel",
                        "parked_task_cancelled": True,
                        "parked_task_summary": canceled_summary,
                        "parked_tasks_remaining": remaining_count,
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

            if self._should_auto_resume_parked_task(
                message=effective_user_message,
                parked_task=parked_task,
                capabilities_summary=capabilities_summary,
            ):
                self._restore_parked_task(context)
                selected_phase_context = self._get_selected_phase_context(
                    context=context,
                    pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                    entities={},
                )
                self._sync_active_task_snapshot(
                    context=context,
                    selected_phase_context=selected_phase_context,
                )

        # Ticketing status evaluation runs for every turn (required or not).
        # This does not create tickets; it only computes status metadata.
        turn_ticketing_status = await self._evaluate_ticketing_status_for_turn(
            message=effective_user_message,
            context=context,
            intent=IntentType.FAQ,
            response_text="",
            current_pending_action=context.pending_action,
            pending_action_target=None,
            capabilities_summary=capabilities_summary,
        )

        # 3.5 Service-agent plugin runtime (deterministic, low latency).
        plugin_runtime_enabled = bool(getattr(settings, "agent_plugin_runtime_enabled", False))
        if plugin_runtime_enabled:
            plugin_settings = config_service.get_agent_plugin_settings()
            if bool(plugin_settings.get("enabled", False)):
                plugin_result = agent_plugin_service.handle_message(
                    message=effective_user_message,
                    context=context,
                    channel=context.channel,
                )
                if plugin_result.handled:
                    internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                    internal_pending_entries.pop("_agent_runtime", None)
                    context.state = plugin_result.next_state
                    context.pending_action = plugin_result.pending_action
                    context.pending_data = conversation_memory_service.merge_with_internal(
                        plugin_result.pending_data or {},
                        internal_pending_entries,
                    )
                    response_text = str(plugin_result.response_text or "").strip()
                    response_text, _ = await self._maybe_llm_rewrite_response(
                        response_text=response_text,
                        user_message=effective_user_message,
                        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
                        context=context,
                        capabilities_summary=capabilities_summary,
                        capability_check_allowed=True,
                        capability_reason="",
                        response_source="agent_plugin_runtime",
                        validator_replaced=False,
                    )

                    assistant_metadata = {
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.9,
                        "channel": context.channel,
                        "agent_plugin_handled": True,
                    }
                    if plugin_result.metadata:
                        assistant_metadata.update(plugin_result.metadata)

                    context.add_message(
                        MessageRole.ASSISTANT,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    conversation_memory_service.capture_assistant_message(
                        context,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    await conversation_memory_service.maybe_refresh_summary(context)
                    await context_manager.save_context(context, db_session=db_session)
                    memory_snapshot = conversation_memory_service.get_snapshot(context)

                    return ChatResponse(
                        session_id=request.session_id,
                        message=response_text,
                        intent=IntentType.FAQ,
                        confidence=0.9,
                        state=plugin_result.next_state,
                        suggested_actions=plugin_result.suggested_actions or self._get_suggested_actions(
                            plugin_result.next_state,
                            IntentType.FAQ,
                            capabilities_summary,
                        ),
                        metadata={
                            "message_count": len(context.messages),
                            "routing_path": ProcessingPath.SIMPLE.value,
                            "routing_score": 1.0,
                            "routing_signals": {"path": "agent_plugin_runtime"},
                            "agent_plugin_handled": True,
                            "memory_summary": memory_snapshot.get("summary", ""),
                            "memory_facts": memory_snapshot.get("facts", {}),
                            **(plugin_result.metadata or {}),
                        },
                    )

        # Optional two-stage LLM orchestration mode:
        # 1) orchestrator LLM decides service/action/ticketing
        # 2) service LLM refines slot collection/response
        # Deterministic policy gate runs before any execution side-effect.
        if bool(getattr(settings, "chat_llm_orchestration_mode", False)):
            orchestration_response = await self._process_llm_orchestration_message(
                request=request_for_processing,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                selected_phase_context=selected_phase_context,
                db_session=db_session,
            )
            if orchestration_response is not None:
                return orchestration_response

        # Optional pure-LLM mode: full KB + full context with no deterministic
        # post-processing overrides in runtime.
        if bool(getattr(settings, "chat_pure_llm_mode", False)):
            return await self._process_pure_llm_message(
                request=request_for_processing,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                db_session=db_session,
            )

        # Optional full-file mode: LLM sees complete KB text and handles flow end-to-end.
        if bool(getattr(settings, "chat_full_kb_llm_mode", False)):
            return await self._process_full_kb_llm_message(
                request=request_for_processing,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                db_session=db_session,
            )

        # Optional strict mode: answer only via KB-grounded FAQ retrieval path.
        if bool(getattr(settings, "chat_kb_only_mode", False)):
            return await self._process_kb_only_message(
                request=request_for_processing,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                db_session=db_session,
            )

        multi_ask_response = await self._maybe_handle_multi_ask_message(
            request=request_for_processing,
            context=context,
            capabilities_summary=capabilities_summary,
            routing_decision=routing_decision,
            memory_snapshot=memory_snapshot,
            history_window=history_window,
            db_session=db_session,
        )
        if multi_ask_response is not None:
            return multi_ask_response

        # 3a. Deterministic de-escalation from human handoff back to bot flow.
        if (
            context.state == ConversationState.ESCALATED
            and (
                self._is_return_to_bot_request(effective_user_message)
                or self._is_return_to_bot_request(raw_user_message)
            )
        ):
            internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal({}, internal_pending_entries)
            context.pending_data.pop("_clarification_attempts", None)
            context.state = ConversationState.IDLE
            response_text = "You're back with the bot. How can I help you next?"
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=IntentResult(intent=IntentType.HUMAN_REQUEST, confidence=1.0, entities={}),
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="deescalation",
                validator_replaced=False,
            )
            assistant_metadata = {
                "intent": IntentType.HUMAN_REQUEST.value,
                "confidence": 1.0,
                "channel": context.channel,
                "system_action": "deescalated_to_bot",
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=IntentType.HUMAN_REQUEST,
                confidence=1.0,
                state=ConversationState.IDLE,
                suggested_actions=config_service.get_quick_actions(limit=4),
                metadata={
                    "message_count": len(context.messages),
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "deescalation"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                    "deescalated": True,
                },
            )

        # 3aa. Deterministic generic service detail collection flow.
        if context.pending_action == "collect_service_details":
            msg_lower = str(effective_user_message or "").strip().lower()
            should_consume_service_followup = (
                msg_lower in {"cancel", "stop", "no", "nope", "never mind", "nevermind"}
                or self._looks_like_service_detail_reply(msg_lower)
            )
            # Only consume detail-like/cancel replies here. Otherwise let normal
            # routing classify and interrupt pending flow when user switches topics.
            if should_consume_service_followup:
                followup_result = self._handle_service_detail_followup(effective_user_message, context, capabilities_summary)
                if followup_result is not None:
                    context.state = followup_result.next_state
                    context.pending_action = followup_result.pending_action
                    context.pending_data = conversation_memory_service.merge_with_internal(
                        followup_result.pending_data or {},
                        conversation_memory_service.internal_pending_entries(context.pending_data),
                    )
                    response_text = str(followup_result.response_text or "").strip()
                    response_text, _ = await self._maybe_llm_rewrite_response(
                        response_text=response_text,
                        user_message=effective_user_message,
                        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.88, entities={}),
                        context=context,
                        capabilities_summary=capabilities_summary,
                        capability_check_allowed=True,
                        capability_reason="",
                        response_source="service_detail_followup",
                        validator_replaced=False,
                    )
                    assistant_metadata = {
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.88,
                        "channel": context.channel,
                        "service_followup": True,
                    }
                    if followup_result.metadata:
                        assistant_metadata.update(followup_result.metadata)
                    context.add_message(
                        MessageRole.ASSISTANT,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    conversation_memory_service.capture_assistant_message(
                        context,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    await conversation_memory_service.maybe_refresh_summary(context)
                    await context_manager.save_context(context, db_session=db_session)
                    memory_snapshot = conversation_memory_service.get_snapshot(context)
                    return ChatResponse(
                        session_id=request.session_id,
                        message=response_text,
                        intent=IntentType.FAQ,
                        confidence=0.88,
                        state=followup_result.next_state,
                        suggested_actions=followup_result.suggested_actions or self._get_suggested_actions(
                            followup_result.next_state,
                            IntentType.FAQ,
                            capabilities_summary,
                        ),
                        metadata={
                            "message_count": len(context.messages),
                            "routing_path": ProcessingPath.SIMPLE.value,
                            "routing_score": 1.0,
                            "routing_signals": {"path": "service_detail_followup"},
                            "service_followup": True,
                            "memory_summary": memory_snapshot.get("summary", ""),
                            "memory_facts": memory_snapshot.get("facts", {}),
                        },
                    )

        # 3b. Deterministic personal-memory shortcut (latest reservation/order/departure facts).
        memory_match = self._match_memory_information_response(
            effective_user_message,
            context,
            memory_snapshot,
        )
        if memory_match is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.9,
                entities={
                    "memory_match_type": memory_match.get("match_type"),
                },
            )
            response_text = memory_match["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="memory_shortcut",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "memory_match_type": memory_match.get("match_type"),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "memory_shortcut"},
                    "memory_shortcut_match": True,
                    "memory_match_type": memory_match.get("match_type"),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3c. Deterministic identity/profile shortcut.
        identity_match = self._match_identity_response(effective_user_message, capabilities_summary, context)
        if identity_match is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.92,
                entities={"identity_match_type": identity_match.get("match_type")},
            )
            response_text = identity_match["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="identity_shortcut",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "identity_match_type": identity_match.get("match_type"),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "identity_shortcut"},
                    "identity_shortcut_match": True,
                    "identity_match_type": identity_match.get("match_type"),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3d. Deterministic service-overview shortcut.
        services_overview = self._match_service_overview_response(effective_user_message, capabilities_summary, context)
        if services_overview is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.9,
                entities={"match_type": "service_overview"},
            )
            response_text = services_overview["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="service_overview",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "service_overview": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "service_overview"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3e. Deterministic profile update shortcut for room number capture.
        room_update = self._match_room_number_update_response(effective_user_message, context)
        if room_update is not None:
            context.room_number = room_update["room_number"]
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.94,
                entities={
                    "match_type": "room_number_update",
                    "room_number": context.room_number,
                },
            )
            response_text = room_update["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="room_number_update",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "room_number": context.room_number,
                "profile_update": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "room_number_update"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3f. Deterministic room-number recall shortcut.
        room_lookup = self._match_room_number_lookup_response(effective_user_message, context, memory_snapshot)
        if room_lookup is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.92,
                entities={"match_type": "room_number_lookup"},
            )
            response_text = room_lookup["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="room_number_lookup",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "room_lookup": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "room_number_lookup"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3d. Deterministic FAQ-bank shortcut (admin-defined Q/A pairs).
        faq_match = self._match_faq_bank_answer(effective_user_message, context)
        if faq_match is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=max(0.8, float(faq_match.get("match_score", 0.8))),
                entities={
                    "faq_id": faq_match.get("id"),
                    "match_score": faq_match.get("match_score", 0.0),
                },
            )
            response_text = str(faq_match.get("answer") or "").strip()
            if faq_match.get("description"):
                response_text = f"{response_text}\n\n{str(faq_match['description']).strip()}"
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="faq_bank",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "faq_id": faq_match.get("id"),
                "faq_match_score": faq_match.get("match_score", 0.0),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "faq_bank"},
                    "faq_bank_match": True,
                    "faq_id": faq_match.get("id"),
                    "faq_match_score": faq_match.get("match_score", 0.0),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3g. Deterministic menu recommendation shortcut.
        menu_recommendation = await self._match_menu_recommendation_response(
            message=effective_user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            db_session=db_session,
        )
        if menu_recommendation is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.88,
                entities={
                    "match_type": menu_recommendation.get("match_type", "menu_recommendation"),
                },
            )
            response_text = menu_recommendation["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="menu_recommendation",
                validator_replaced=False,
            )
            context.state = ConversationState.IDLE
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                conversation_memory_service.internal_pending_entries(context.pending_data),
            )
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "menu_recommendation": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "menu_recommendation"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3d. Deterministic service-aware shortcut (status/hours/description from admin services).
        service_match = self._match_service_information_response(effective_user_message, context, capabilities_summary)
        if service_match is not None:
            pending_interrupted = False
            if context.pending_action and self._should_interrupt_pending_flow_for_user_query(
                message=effective_user_message,
                context=context,
                capabilities_summary=capabilities_summary,
            ):
                parked = self._park_active_task(
                    context=context,
                    selected_phase_context=selected_phase_context,
                    reason="topic_diversion",
                    user_message=effective_user_message,
                )
                pending_interrupted = parked is not None
            service_match_type = str(service_match.get("match_type") or "")
            if service_match_type == "service_catalog_unavailable_handoff":
                handoff_response = await self._handle_service_catalog_unavailable_handoff(
                    request=request_for_processing,
                    context=context,
                    capabilities_summary=capabilities_summary,
                    service_match=service_match,
                    db_session=db_session,
                    routing_path_tag="service_catalog_unavailable_handoff",
                    pending_interrupted=pending_interrupted,
                )
                if handoff_response is not None:
                    return handoff_response

            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.86,
                entities={
                    "service_id": service_match.get("service_id"),
                    "service_name": service_match.get("service_name"),
                    "service_match_type": service_match.get("match_type"),
                },
            )
            response_text = str(service_match.get("response_text") or "")
            if service_match_type == "service_catalog_action":
                context.state = ConversationState.AWAITING_INFO
                internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                context.pending_action = "collect_service_details"
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {
                        "service_id": service_match.get("service_id"),
                        "service_name": service_match.get("service_name"),
                    },
                    internal_pending_entries,
                )
            else:
                context.state = ConversationState.IDLE
                context.pending_action = None
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    conversation_memory_service.internal_pending_entries(context.pending_data),
                )
            response_text, resume_actions = self._append_resume_checkpoint(
                response_text=response_text,
                context=context,
                pending_interrupted=pending_interrupted,
            )
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="service_catalog",
                validator_replaced=False,
            )
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "service_id": service_match.get("service_id"),
                "service_match_type": service_match.get("match_type"),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=context.state,
                suggested_actions=self._finalize_user_query_suggestions(
                    resume_actions + (
                        list(service_match.get("suggested_actions") or [])
                        if isinstance(service_match.get("suggested_actions"), list)
                        and service_match.get("suggested_actions")
                        else (
                            ["Share details", "Cancel"]
                            if service_match_type == "service_catalog_action"
                            else self._get_suggested_actions(
                                context.state,
                                IntentType.FAQ,
                                capabilities_summary,
                            )
                        )
                    ),
                    state=context.state,
                    intent=IntentType.FAQ,
                    pending_action=context.pending_action,
                    pending_data=context.pending_data,
                    capabilities_summary=capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "service_catalog"},
                    "pending_interrupted": pending_interrupted,
                    "resume_checkpoint": bool(resume_actions),
                    "parked_task_available": self._peek_parked_task(context) is not None,
                    "service_catalog_match": True,
                    "service_id": service_match.get("service_id"),
                    "service_match_type": service_match.get("match_type"),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        context_pack = self._build_llm_context_pack(
            context=context,
            capabilities_summary=capabilities_summary,
            selected_phase_context=selected_phase_context,
            memory_snapshot=memory_snapshot,
            user_message=effective_user_message,
        )

        # 4. Build context dict for LLM (with capabilities)
        llm_context = {
            "hotel_code": context.hotel_code,
            "hotel_name": capabilities_summary.get("hotel_name", context.hotel_code),
            "bot_name": capabilities_summary.get("bot_name", "Assistant"),
            "business_type": capabilities_summary.get("business_type", "hotel"),
            "selected_phase_id": selected_phase_context.get("selected_phase_id", ""),
            "selected_phase_name": selected_phase_context.get("selected_phase_name", ""),
            "enabled_intents": [
                intent.get("id")
                for intent in capabilities_summary.get("intents", [])
                if intent.get("enabled", False)
            ],
            "city": capabilities_summary.get("city", ""),
            "guest_name": context.guest_name or "Guest",
            "room_number": context.room_number,
            "state": context.state.value,
            "pending_action": context.pending_action,
            "pending_data": context.pending_data,
            "channel": context.channel,
            "capabilities": capabilities_summary,
            "intent_catalog": capabilities_summary.get("intents", []),
            "service_catalog": capabilities_summary.get("service_catalog", []),
            "faq_bank": capabilities_summary.get("faq_bank", []),
            "tools": capabilities_summary.get("tools", []),
            "nlu_policy": capabilities_summary.get("nlu_policy", {}),
            "prompts": capabilities_summary.get("prompts", {}),
            "conversation_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
            "context_pack": context_pack,
            "current_phase": context_pack.get("current_phase", {}),
            "active_flow": context_pack.get("active_flow", {}),
            "missing_slots": context_pack.get("missing_slots", []),
            "phase_services": context_pack.get("phase_services", []),
            "ticketing_enabled_by_service": context_pack.get("ticketing_enabled_by_service", {}),
            "stay_window": context_pack.get("stay_window", {}),
            "current_time": context_pack.get("current_time", ""),
            "recent_user_goal": context_pack.get("recent_user_goal", ""),
        }

        # 5. Classify intent using LLM
        intent_result = await self._classify_intent(
            effective_user_message,
            context.to_llm_messages(count=history_window),
            llm_context,
        )

        pending_interrupted = False
        if context.pending_action and self._should_interrupt_pending_flow(
            message=effective_user_message,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
        ):
            parked = self._park_active_task(
                context=context,
                selected_phase_context=selected_phase_context,
                reason="topic_diversion",
                user_message=effective_user_message,
            )
            pending_interrupted = parked is not None

        if pending_interrupted:
            identity_match = self._match_identity_response(effective_user_message, capabilities_summary, context)
            if identity_match is not None:
                response_text, resume_actions = self._append_resume_checkpoint(
                    response_text=str(identity_match["response_text"] or ""),
                    context=context,
                    pending_interrupted=pending_interrupted,
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.92, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="identity_shortcut_post_interrupt",
                    validator_replaced=False,
                )
                context.add_message(
                    MessageRole.ASSISTANT,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.92,
                        "channel": context.channel,
                        "identity_match_type": identity_match.get("match_type"),
                    },
                )
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.92,
                        "channel": context.channel,
                        "identity_match_type": identity_match.get("match_type"),
                    },
                )
                await conversation_memory_service.maybe_refresh_summary(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=IntentType.FAQ,
                    confidence=0.92,
                    state=ConversationState.IDLE,
                    suggested_actions=self._finalize_user_query_suggestions(
                        resume_actions
                        + self._get_suggested_actions(
                            ConversationState.IDLE,
                            IntentType.FAQ,
                            capabilities_summary,
                        ),
                        state=ConversationState.IDLE,
                        intent=IntentType.FAQ,
                        pending_action=context.pending_action,
                        pending_data=context.pending_data,
                        capabilities_summary=capabilities_summary,
                    ),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "identity_shortcut_post_interrupt"},
                        "pending_interrupted": True,
                        "resume_checkpoint": bool(resume_actions),
                        "parked_task_available": self._peek_parked_task(context) is not None,
                        "identity_shortcut_match": True,
                        "identity_match_type": identity_match.get("match_type"),
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

            service_match = self._match_service_information_response(
                effective_user_message,
                context,
                capabilities_summary,
            )
            if service_match is not None:
                service_match_type = str(service_match.get("match_type") or "")
                if service_match_type == "service_catalog_unavailable_handoff":
                    handoff_response = await self._handle_service_catalog_unavailable_handoff(
                        request=request_for_processing,
                        context=context,
                        capabilities_summary=capabilities_summary,
                        service_match=service_match,
                        db_session=db_session,
                        routing_path_tag="service_catalog_unavailable_handoff_post_interrupt",
                        pending_interrupted=True,
                    )
                    if handoff_response is not None:
                        return handoff_response

                response_text, resume_actions = self._append_resume_checkpoint(
                    response_text=str(service_match["response_text"] or ""),
                    context=context,
                    pending_interrupted=pending_interrupted,
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.86, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="service_catalog_post_interrupt",
                    validator_replaced=False,
                )
                context.add_message(
                    MessageRole.ASSISTANT,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.86,
                        "channel": context.channel,
                        "service_id": service_match.get("service_id"),
                        "service_match_type": service_match.get("match_type"),
                    },
                )
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.86,
                        "channel": context.channel,
                        "service_id": service_match.get("service_id"),
                        "service_match_type": service_match.get("match_type"),
                    },
                )
                await conversation_memory_service.maybe_refresh_summary(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=IntentType.FAQ,
                    confidence=0.86,
                    state=ConversationState.IDLE,
                    suggested_actions=self._finalize_user_query_suggestions(
                        resume_actions + (
                            list(service_match.get("suggested_actions") or [])
                            if isinstance(service_match.get("suggested_actions"), list)
                            and service_match.get("suggested_actions")
                            else self._get_suggested_actions(
                                ConversationState.IDLE,
                                IntentType.FAQ,
                                capabilities_summary,
                            )
                        ),
                        state=ConversationState.IDLE,
                        intent=IntentType.FAQ,
                        pending_action=context.pending_action,
                        pending_data=context.pending_data,
                        capabilities_summary=capabilities_summary,
                    ),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "service_catalog_post_interrupt"},
                        "pending_interrupted": True,
                        "resume_checkpoint": bool(resume_actions),
                        "parked_task_available": self._peek_parked_task(context) is not None,
                        "service_catalog_match": True,
                        "service_id": service_match.get("service_id"),
                        "service_match_type": service_match.get("match_type"),
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

        # 5b. Confidence-aware handling (clarification/escalation)
        low_confidence = self._handle_low_confidence(intent_result, context)
        if low_confidence is not None:
            response_text, next_state = low_confidence
            response_text, resume_actions = self._append_resume_checkpoint(
                response_text=response_text,
                context=context,
                pending_interrupted=pending_interrupted,
            )
            response_text, low_confidence_rewrite_metadata = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="low_confidence",
                validator_replaced=False,
            )
            context.state = next_state
            self._sync_active_task_snapshot(
                context=context,
                selected_phase_context=selected_phase_context,
            )
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            metadata = {
                "message_count": len(context.messages),
                "entities": intent_result.entities,
                "routing_path": routing_decision.path.value,
                "routing_score": routing_decision.score,
                "routing_signals": routing_decision.signals,
                "pending_interrupted": pending_interrupted,
                "resume_checkpoint": bool(resume_actions),
                "parked_task_available": self._peek_parked_task(context) is not None,
                "memory_summary": memory_snapshot.get("summary", ""),
                "memory_facts": memory_snapshot.get("facts", {}),
            }
            metadata.update(low_confidence_rewrite_metadata)
            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=next_state,
                suggested_actions=self._finalize_user_query_suggestions(
                    resume_actions
                    + self._get_suggested_actions(
                        next_state,
                        intent_result.intent,
                        capabilities_summary,
                    ),
                    state=next_state,
                    intent=intent_result.intent,
                    pending_action=context.pending_action,
                    pending_data=context.pending_data,
                    capabilities_summary=capabilities_summary,
                ),
                metadata=metadata,
            )
        context.pending_data.pop("_clarification_attempts", None)

        # 6. Check intent enablement and capability BEFORE generating response
        intent_enabled_check = self._check_intent_enabled(intent_result.intent)
        if intent_enabled_check.allowed:
            capability_check = self._check_capability_for_intent(
                context.hotel_code,
                intent_result,
                effective_user_message,
            )
        else:
            capability_check = intent_enabled_check

        # 7. Validate intent for current state
        is_valid, error_msg = state_machine.validate_intent_for_state(
            intent_result.intent, context.state
        )

        message_lower = str(effective_user_message or "").strip().lower()
        has_operational_ticket_signal = (
            self._looks_like_operational_issue_for_ticketing(message_lower)
            or self._looks_like_strong_ticket_issue_marker(message_lower)
        )
        phase_gate_intent = intent_result.intent
        if has_operational_ticket_signal and intent_result.intent in {
            IntentType.FAQ,
            IntentType.MENU_REQUEST,
            IntentType.HUMAN_REQUEST,
            IntentType.COMPLAINT,
        }:
            phase_gate_intent = IntentType.COMPLAINT

        contextual_sanity_issue = self._detect_contextual_sanity_issue(
            message=effective_user_message,
            intent=phase_gate_intent,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
        )
        phase_gate = None
        if phase_gate_intent != IntentType.COMPLAINT:
            phase_gate = self._detect_ticketing_phase_service_mismatch(
                message=effective_user_message,
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
            )
            if phase_gate is None:
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=effective_user_message,
                    intent=phase_gate_intent,
                    context=context,
                    pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                    entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
                )
        contextual_sanity_handler_result = (
            self._build_contextual_sanity_handler_result(contextual_sanity_issue)
            if contextual_sanity_issue is not None
            else None
        )
        phase_gate_handler_result = (
            await self._build_ticketing_phase_gate_handler_result(
                phase_gate,
                user_message=effective_user_message,
                intent=phase_gate_intent,
                context=context,
                capabilities_summary=capabilities_summary,
            )
            if phase_gate is not None
            else None
        )

        # 8. Generate response - try handler first, then complex agent team or LLM fallback
        handler_result: Optional[HandlerResult] = None
        agent_orchestrator_result = None
        response_source = "unknown"

        if contextual_sanity_handler_result is not None:
            handler_result = contextual_sanity_handler_result
            response_text = handler_result.response_text
            response_source = "contextual_sanity_gate"
        elif phase_gate_handler_result is not None:
            handler_result = phase_gate_handler_result
            response_text = handler_result.response_text
            response_source = "ticketing_phase_gate"
        elif not capability_check.allowed:
            # Capability not available
            response_text = self._build_capability_denial_response(capability_check)
            response_source = "capability_denial"
        elif not is_valid:
            response_text = self._handle_unexpected_intent(context, intent_result, error_msg)
            response_source = "state_validation"
        else:
            # Try handler-based response
            handler_result = await self._dispatch_to_handler(
                effective_user_message, intent_result, context, capabilities_summary, db_session
            )

            if handler_result is not None:
                response_text = handler_result.response_text
                response_source = "handler"
            else:
                # No handler -> for COMPLEX route run agent team before generic LLM fallback
                if routing_decision.path == ProcessingPath.COMPLEX:
                    agent_orchestrator_result = await complex_query_orchestrator.handle(
                        message=effective_user_message,
                        intent_result=intent_result,
                        context=context,
                        capabilities_summary=capabilities_summary,
                        llm_context=llm_context,
                        routing_signals=routing_decision.signals,
                    )
                    if agent_orchestrator_result.handled:
                        response_text = agent_orchestrator_result.response_text
                        response_source = "agent_orchestrator"
                    else:
                        response_text = await self._generate_response(
                            effective_user_message,
                            intent_result,
                            context.to_llm_messages(count=history_window),
                            llm_context,
                            capability_check,
                        )
                        response_source = "llm_fallback"
                else:
                    # SIMPLE route fallback
                    response_text = await self._generate_response(
                        effective_user_message,
                        intent_result,
                        context.to_llm_messages(count=history_window),
                        llm_context,
                        capability_check,
                    )
                    response_source = "llm_fallback"

        # 8b. Validate final response against capabilities/rules.
        validation = response_validator.validate(
            response_text=response_text,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
        )
        validator_replaced = False
        if not validation.valid and validation.action == "replace" and validation.replacement_response:
            response_text = validation.replacement_response
            validator_replaced = True
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()
        state_basis_response_text = response_text

        # 9. Determine next state
        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        resume_actions: list[str] = []
        response_rewrite_metadata: dict[str, Any] = {}
        if handler_result is not None:
            next_state = handler_result.next_state
            # Apply pending action/data from handler
            if handler_result.pending_action is not None:
                context.pending_action = handler_result.pending_action
            if handler_result.pending_data is not None:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    handler_result.pending_data,
                    internal_pending_entries,
                )
            # Clear pending if handler explicitly set to None and state is not awaiting
            if handler_result.pending_action is None and next_state not in (
                ConversationState.AWAITING_CONFIRMATION,
                ConversationState.AWAITING_INFO,
            ):
                context.pending_action = None
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )
                context.pending_data.pop("_clarification_attempts", None)
            if handler_result.metadata.get("room_number"):
                context.room_number = str(handler_result.metadata["room_number"])
        else:
            next_state = self._determine_next_state(context, intent_result, state_basis_response_text)

        response_text, response_rewrite_metadata = await self._maybe_llm_rewrite_response(
            response_text=response_text,
            user_message=effective_user_message,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
            response_source=response_source,
            validator_replaced=validator_replaced,
        )

        response_text, resume_actions = self._append_resume_checkpoint(
            response_text=response_text,
            context=context,
            pending_interrupted=pending_interrupted,
        )

        turn_ticketing_status = await self._evaluate_ticketing_status_for_turn(
            message=effective_user_message,
            context=context,
            intent=intent_result.intent,
            response_text=response_text,
            current_pending_action=context.pending_action,
            pending_action_target=context.pending_action,
            capabilities_summary=capabilities_summary,
        )

        # 10. Update context
        context.state = next_state
        self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context,
        )
        assistant_metadata = {
            "intent": intent_result.intent.value,
            "confidence": intent_result.confidence,
            "channel": context.channel,
        }
        if handler_result is not None and handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        # 11. Build suggested actions
        if handler_result is not None and handler_result.suggested_actions:
            suggested_actions = resume_actions + handler_result.suggested_actions
        else:
            suggested_actions = resume_actions + self._get_suggested_actions(
                next_state,
                intent_result.intent,
                capabilities_summary,
            )
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=intent_result.intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        # 12. Build metadata
        metadata = {
            "message_count": len(context.messages),
            "entities": intent_result.entities,
            "classified_intent": intent_result.intent.value,
            "classified_confidence": intent_result.confidence,
            "routing_path": routing_decision.path.value,
            "routing_score": routing_decision.score,
            "routing_signals": routing_decision.signals,
            "pending_interrupted": pending_interrupted,
            "resume_checkpoint": bool(resume_actions),
            "parked_task_available": self._peek_parked_task(context) is not None,
            "response_valid": validation.valid,
            "validation_issues": [issue.code for issue in validation.issues],
            "validator_replaced": validator_replaced,
            "response_source": response_source,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        if handler_result is not None:
            metadata["routed_handler"] = intent_result.intent.value
        if agent_orchestrator_result is not None:
            metadata["agent_orchestration"] = {
                "handled": agent_orchestrator_result.handled,
                "reason": agent_orchestrator_result.reason,
            }
            metadata.update(agent_orchestrator_result.metadata)
        if handler_result is not None and handler_result.metadata:
            metadata.update(handler_result.metadata)
        metadata.update(response_rewrite_metadata)
        metadata.update(turn_ticketing_status)
        metadata = self._apply_ticket_metadata_contract(metadata)

        # 13. Build response
        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=intent_result.intent,
            confidence=intent_result.confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _process_kb_only_message(
        self,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict,
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        db_session=None,
    ) -> ChatResponse:
        """
        Force all requests through FAQ retrieval so answers stay KB-grounded.
        This bypasses deterministic shortcuts and transactional handlers.
        """
        effective_user_message = str(request.message or "").strip()
        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        context.pending_action = None
        context.pending_data = conversation_memory_service.merge_with_internal(
            {},
            internal_pending_entries,
        )
        context.pending_data.pop("_clarification_attempts", None)

        forced_intent = IntentResult(
            intent=IntentType.FAQ,
            confidence=1.0,
            entities={
                "forced_mode": "kb_only",
                "kb_only_llm_mode": True,
            },
        )

        handler_result = await handler_registry.dispatch(
            forced_intent,
            effective_user_message,
            context,
            capabilities_summary,
            db_session,
        )
        if handler_result is None:
            handler_result = HandlerResult(
                response_text=(
                    "I could not find this in the current knowledge base for this property. "
                    "If you want, I can connect you with our team."
                ),
                next_state=ConversationState.IDLE,
                suggested_actions=["Ask another question", "Talk to human"],
                metadata={
                    "kb_only_mode": True,
                    "kb_only_no_handler": True,
                },
            )

        next_state = handler_result.next_state
        if handler_result.pending_action is not None:
            context.pending_action = handler_result.pending_action
        if handler_result.pending_data is not None:
            context.pending_data = conversation_memory_service.merge_with_internal(
                handler_result.pending_data,
                internal_pending_entries,
            )
        if handler_result.pending_action is None and next_state not in (
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
        ):
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )
            context.pending_data.pop("_clarification_attempts", None)
        if handler_result.metadata.get("room_number"):
            context.room_number = str(handler_result.metadata["room_number"])
        context.state = next_state

        response_text = handler_result.response_text
        kb_validation = response_validator.validate(
            response_text=response_text,
            intent_result=forced_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=True,
            capability_reason="",
        )
        kb_validator_replaced = False
        kb_validation_issue_codes = [
            str(issue.code)
            for issue in (kb_validation.issues or [])
            if getattr(issue, "code", None)
        ]
        if not kb_validation.valid and kb_validation.action == "replace" and kb_validation.replacement_response:
            response_text = kb_validation.replacement_response
            kb_validator_replaced = True
        response_text, kb_response_rewrite_metadata = await self._maybe_llm_rewrite_response(
            response_text=response_text,
            user_message=effective_user_message,
            intent_result=forced_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=True,
            capability_reason="",
            response_source="kb_only_faq_handler",
            validator_replaced=kb_validator_replaced,
        )

        assistant_metadata = {
            "intent": forced_intent.intent.value,
            "confidence": forced_intent.confidence,
            "channel": context.channel,
            "kb_only_mode": True,
            "kb_only_llm_mode": True,
            "response_validator_applied": True,
            "response_validator_replaced": kb_validator_replaced,
            "response_validator_issues": kb_validation_issue_codes,
        }
        if handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        assistant_metadata.update(kb_response_rewrite_metadata)
        assistant_metadata["kb_only_mode"] = True
        assistant_metadata["kb_only_llm_mode"] = True
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if handler_result.suggested_actions:
            suggested_actions = handler_result.suggested_actions
        else:
            suggested_actions = self._get_suggested_actions(
                next_state,
                forced_intent.intent,
                capabilities_summary,
            )
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=forced_intent.intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        routing_path = getattr(getattr(routing_decision, "path", ProcessingPath.SIMPLE), "value", ProcessingPath.SIMPLE.value)
        routing_score = float(getattr(routing_decision, "score", 1.0))
        routing_signals = list(getattr(routing_decision, "signals", []))
        metadata = {
            "message_count": len(context.messages),
            "entities": forced_intent.entities,
            "classified_intent": forced_intent.intent.value,
            "classified_confidence": forced_intent.confidence,
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": "kb_only_faq_handler",
            "kb_only_mode": True,
            "kb_only_llm_mode": True,
            "response_validator_applied": True,
            "response_validator_replaced": kb_validator_replaced,
            "response_validator_issues": kb_validation_issue_codes,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        if handler_result.metadata:
            metadata.update(handler_result.metadata)
        metadata.update(kb_response_rewrite_metadata)
        metadata["kb_only_mode"] = True
        metadata["kb_only_llm_mode"] = True

        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=forced_intent.intent,
            confidence=forced_intent.confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    @staticmethod
    def _normalize_service_identifier(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[^a-z0-9]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text

    def _resolve_service_from_phase_signals(
        self,
        *,
        services: list[dict[str, Any]],
        entities: dict[str, Any] | None = None,
        pending_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(services, list) or not services:
            return None

        id_index: dict[str, dict[str, Any]] = {}
        name_index: dict[str, dict[str, Any]] = {}
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            sid = self._normalize_service_identifier(service.get("id"))
            if sid and sid not in id_index:
                id_index[sid] = service
            sname = self._normalize_service_identifier(service.get("name"))
            if sname and sname not in name_index:
                name_index[sname] = service

        signal_map: dict[str, Any] = {}
        if isinstance(pending_data, dict):
            signal_map.update(pending_data)
        if isinstance(entities, dict):
            signal_map.update(entities)

        id_candidates = (
            signal_map.get("service_id"),
            signal_map.get("target_service_id"),
            signal_map.get("resolved_service_id"),
            signal_map.get("workflow_id"),
            signal_map.get("booking_sub_category"),
            signal_map.get("sub_category"),
            signal_map.get("ticket_sub_category"),
        )
        for candidate in id_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in id_index:
                service = id_index[key]
                result = {
                    "service_id": str(service.get("id") or "").strip(),
                    "service_name": str(service.get("name") or service.get("id") or "This service").strip(),
                    "phase_id": self._normalize_phase_identifier(service.get("phase_id")),
                    "score": 1.0,
                    "matched_by_signal": True,
                }
                if "ticketing_enabled" in service:
                    result["ticketing_enabled"] = service.get("ticketing_enabled")
                return result

        name_candidates = (
            signal_map.get("service_name"),
            signal_map.get("target_service"),
            signal_map.get("resolved_service_name"),
        )
        for candidate in name_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in name_index:
                service = name_index[key]
                result = {
                    "service_id": str(service.get("id") or "").strip(),
                    "service_name": str(service.get("name") or service.get("id") or "This service").strip(),
                    "phase_id": self._normalize_phase_identifier(service.get("phase_id")),
                    "score": 0.95,
                    "matched_by_signal": True,
                }
                if "ticketing_enabled" in service:
                    result["ticketing_enabled"] = service.get("ticketing_enabled")
                return result
        return None

    def _resolve_pure_llm_ticket_service(
        self,
        *,
        capabilities_summary: dict[str, Any],
        llm_result: Any,
    ) -> dict[str, Any] | None:
        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return None

        id_index: dict[str, dict[str, Any]] = {}
        name_index: dict[str, dict[str, Any]] = {}
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_id = self._normalize_service_identifier(service.get("id"))
            if service_id:
                id_index[service_id] = service
            service_name = self._normalize_service_identifier(service.get("name"))
            if service_name and service_name not in name_index:
                name_index[service_name] = service

        llm_output = llm_result.llm_output if isinstance(getattr(llm_result, "llm_output", None), dict) else {}
        pending_data = llm_result.pending_data if isinstance(getattr(llm_result, "pending_data", None), dict) else {}
        entities = self._extract_full_kb_entities_for_handler(llm_result)

        service_id_candidates = (
            llm_output.get("service_id"),
            llm_output.get("target_service_id"),
            llm_output.get("resolved_service_id"),
            llm_output.get("workflow_id"),
            pending_data.get("service_id"),
            pending_data.get("target_service_id"),
            pending_data.get("resolved_service_id"),
            pending_data.get("workflow_id"),
            entities.get("service_id"),
            entities.get("target_service_id"),
            entities.get("resolved_service_id"),
            entities.get("workflow_id"),
            entities.get("booking_sub_category"),
            pending_data.get("booking_sub_category"),
            pending_data.get("sub_category"),
            llm_output.get("ticket_sub_category"),
        )
        for candidate in service_id_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in id_index:
                return id_index[key]

        service_name_candidates = (
            llm_output.get("service_name"),
            llm_output.get("target_service"),
            pending_data.get("service_name"),
            pending_data.get("target_service"),
            entities.get("service_name"),
            entities.get("target_service"),
        )
        for candidate in service_name_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in name_index:
                return name_index[key]
        return None

    async def _maybe_create_pure_llm_ticket(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_result: Any,
        effective_intent: IntentType,
    ) -> dict[str, Any]:
        ticket_source = "pure_llm"
        if not self._is_ticketing_plugin_enabled():
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticketing_plugin_disabled",
                "ticket_source": ticket_source,
            }
        if not ticketing_service.is_ticketing_enabled(capabilities_summary):
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticketing_service_disabled",
                "ticket_source": ticket_source,
            }

        pending = llm_result.pending_data if isinstance(getattr(llm_result, "pending_data", None), dict) else {}
        if str(pending.get("ticket_id") or "").strip():
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticket_already_present_in_pending_data",
                "ticket_source": ticket_source,
            }

        llm_ticketing_preference = self._extract_full_kb_ticketing_preference(llm_result)
        if llm_ticketing_preference is not True:
            return {}
        ticket_ready_to_create = self._is_pure_llm_ticket_ready(llm_result)
        if not ticket_ready_to_create:
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticket_not_ready_to_create",
                "ticket_source": ticket_source,
            }

        service = self._resolve_pure_llm_ticket_service(
            capabilities_summary=capabilities_summary,
            llm_result=llm_result,
        )
        if not isinstance(service, dict):
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "service_unresolved_for_ticketing",
                "ticket_source": ticket_source,
            }

        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=self._extract_full_kb_entities_for_handler(llm_result),
        )
        current_phase_id = self._normalize_phase_identifier(selected_phase.get("selected_phase_id"))
        service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
        if current_phase_id and service_phase_id and current_phase_id != service_phase_id:
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "phase_service_mismatch",
                "ticket_source": ticket_source,
                "phase_gate_service_id": str(service.get("id") or "").strip(),
                "phase_gate_service_name": str(service.get("name") or "").strip(),
                "phase_gate_current_phase_id": current_phase_id,
                "phase_gate_current_phase_name": str(selected_phase.get("selected_phase_name") or "").strip(),
                "phase_gate_service_phase_id": service_phase_id,
                "phase_gate_service_phase_name": str(service.get("phase_name") or self._phase_label(service_phase_id)).strip(),
            }

        if "ticketing_enabled" in service and not bool(service.get("ticketing_enabled")):
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "phase_service_ticketing_disabled",
                "ticket_source": ticket_source,
                "phase_gate_service_id": str(service.get("id") or "").strip(),
                "phase_gate_service_name": str(service.get("name") or "").strip(),
                "phase_gate_current_phase_id": current_phase_id,
                "phase_gate_current_phase_name": str(selected_phase.get("selected_phase_name") or "").strip(),
                "phase_gate_service_phase_id": service_phase_id,
                "phase_gate_service_phase_name": str(service.get("phase_name") or self._phase_label(service_phase_id)).strip(),
            }

        llm_output = llm_result.llm_output if isinstance(getattr(llm_result, "llm_output", None), dict) else {}
        entities = self._extract_full_kb_entities_for_handler(llm_result)
        issue = self._first_non_empty(
            llm_output.get("ticket_issue"),
            pending.get("ticket_issue"),
            entities.get("ticket_issue"),
            llm_output.get("ticket_summary"),
            pending.get("ticket_summary"),
            entities.get("ticket_summary"),
            self._extract_full_kb_ticketing_reason(llm_result),
            llm_result.normalized_query,
            request.message,
        )
        if not issue:
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "empty_issue",
                "ticket_source": ticket_source,
            }

        category = self._first_non_empty(
            llm_output.get("ticket_category"),
            pending.get("ticket_category"),
            entities.get("ticket_category"),
        ).lower()
        if category not in {"complaint", "request", "query"}:
            category = "complaint" if effective_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST} else "request"

        sub_category = self._first_non_empty(
            llm_output.get("ticket_sub_category"),
            pending.get("ticket_sub_category"),
            entities.get("ticket_sub_category"),
            pending.get("sub_category"),
            entities.get("sub_category"),
            str(service.get("id") or "").strip(),
        ) or "general_request"

        priority = self._first_non_empty(
            llm_output.get("ticket_priority"),
            pending.get("ticket_priority"),
            entities.get("ticket_priority"),
        ).upper()
        if priority not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            priority = "MEDIUM"

        phase_name = self._first_non_empty(
            selected_phase.get("selected_phase_name"),
            str(service.get("phase_name") or ""),
            self._phase_label(current_phase_id),
            "During Stay",
        )
        room_number = self._first_non_empty(
            llm_result.room_number,
            context.room_number,
            pending.get("room_number"),
            entities.get("room_number"),
        )

        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category=category,
                sub_category=sub_category,
                priority=priority,
                phase=phase_name,
                source="manual",
            )
            if room_number:
                payload["room_number"] = room_number
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            logger.exception("pure_llm_ticket_create_failed")
            return {
                "ticket_create_failed": True,
                "ticket_error": str(exc),
                "ticket_source": ticket_source,
            }

        if not create_result.success:
            return {
                "ticket_create_failed": True,
                "ticket_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": ticket_source,
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        return {
            "ticket_created": True,
            "ticket_id": str(create_result.ticket_id or "").strip(),
            "ticket_status": "open",
            "ticket_category": category,
            "ticket_sub_category": sub_category,
            "ticket_priority": priority,
            "ticket_summary": issue[:180],
            "ticket_source": ticket_source,
            "room_number": room_number,
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
            "ticket_service_id": str(service.get("id") or "").strip(),
            "ticket_service_name": str(service.get("name") or service.get("id") or "").strip(),
        }

    @staticmethod
    def _parse_orchestration_intent(raw_intent: str, fallback: IntentType = IntentType.FAQ) -> IntentType:
        normalized = str(raw_intent or "").strip().lower().replace(" ", "_")
        mapping = {
            "greeting": IntentType.GREETING,
            "faq": IntentType.FAQ,
            "order_food": IntentType.ORDER_FOOD,
            "food_order": IntentType.ORDER_FOOD,
            "table_booking": IntentType.TABLE_BOOKING,
            "room_booking": IntentType.GENERAL_SERVICE,
            "stay_booking": IntentType.GENERAL_SERVICE,
            "reservation": IntentType.GENERAL_SERVICE,
            "room_service": IntentType.ROOM_SERVICE,
            "complaint": IntentType.COMPLAINT,
            "human_request": IntentType.HUMAN_REQUEST,
            "confirmation_yes": IntentType.CONFIRMATION_YES,
            "confirmation_no": IntentType.CONFIRMATION_NO,
            "unclear": IntentType.UNCLEAR,
            "out_of_scope": IntentType.OUT_OF_SCOPE,
        }
        return mapping.get(normalized, fallback)

    def _intent_from_service_id(
        self,
        *,
        service_id: str,
        capabilities_summary: dict[str, Any],
        fallback: IntentType = IntentType.FAQ,
    ) -> IntentType:
        normalized = self._normalize_service_identifier(service_id)
        if not normalized:
            return fallback
        service_catalog = capabilities_summary.get("service_catalog", [])
        service_row = None
        if isinstance(service_catalog, list):
            for row in service_catalog:
                if not isinstance(row, dict):
                    continue
                if self._normalize_service_identifier(row.get("id")) == normalized:
                    service_row = row
                    break
        if not isinstance(service_row, dict):
            service_row = {"id": normalized, "name": normalized}

        prompt_pack = service_row.get("service_prompt_pack")
        if not isinstance(prompt_pack, dict):
            prompt_pack = config_service.get_service_prompt_pack(normalized)
        if not isinstance(prompt_pack, dict):
            prompt_pack = {}
        profile = str(prompt_pack.get("profile") or "").strip().lower()
        semantic_text = " ".join(
            [
                str(service_row.get("id") or ""),
                str(service_row.get("name") or ""),
                str(service_row.get("type") or ""),
                str(service_row.get("description") or ""),
                profile,
            ]
        ).strip().lower()

        if any(token in semantic_text for token in ("complaint", "issue", "maintenance", "housekeeping")):
            return IntentType.COMPLAINT
        if any(token in semantic_text for token in ("handoff", "human", "escalation", "staff")):
            return IntentType.HUMAN_REQUEST
        if any(token in semantic_text for token in ("food", "dining", "menu", "restaurant_order")):
            return IntentType.ORDER_FOOD
        if any(token in semantic_text for token in ("room_service", "housekeeping", "maintenance")):
            return IntentType.ROOM_SERVICE
        if any(token in semantic_text for token in ("table",)) and "room" not in semantic_text:
            return IntentType.TABLE_BOOKING
        if any(token in semantic_text for token in ("room_booking", "booking", "reservation", "room_discovery")):
            return IntentType.GENERAL_SERVICE
        return fallback

    def _service_name_from_id(
        self,
        *,
        service_id: str,
        capabilities_summary: dict[str, Any],
    ) -> str:
        normalized = self._normalize_service_identifier(service_id)
        if not normalized:
            return ""
        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            service_catalog = []
        for row in service_catalog:
            if not isinstance(row, dict):
                continue
            if self._normalize_service_identifier(row.get("id")) != normalized:
                continue
            return str(row.get("name") or row.get("id") or normalized).strip()
        return normalized

    def _service_llm_label_for_service(
        self,
        *,
        service_id: str,
        capabilities_summary: dict[str, Any],
        effective_intent: IntentType | None = None,
        fallback: str = "main",
    ) -> str:
        normalized = self._normalize_service_identifier(service_id)
        if not normalized:
            return "complain" if effective_intent == IntentType.COMPLAINT else fallback

        if normalized in {"complain", "complaint", "complaint_service", "complain_service"}:
            return "complain"

        mapped_intent = self._intent_from_service_id(
            service_id=normalized,
            capabilities_summary=capabilities_summary,
            fallback=IntentType.FAQ,
        )
        if mapped_intent == IntentType.COMPLAINT or effective_intent == IntentType.COMPLAINT:
            return "complain"

        service_name = self._service_name_from_id(
            service_id=normalized,
            capabilities_summary=capabilities_summary,
        )
        return service_name or normalized

    @staticmethod
    def _is_waiting_state_for_user_input(state: ConversationState) -> bool:
        return state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
        }

    def _can_create_orchestration_ticket_now(
        self,
        *,
        decision: OrchestrationDecision,
        policy_result: Any,
        next_state: ConversationState,
        pending_action: str | None,
    ) -> tuple[bool, str]:
        if not bool(decision.ticket.required):
            return False, "ticket_not_required"
        if not bool(getattr(policy_result, "ticket_create_allowed", False)):
            return False, str(getattr(policy_result, "ticket_skip_reason", "") or "ticket_policy_gate_blocked")
        # Do not require final action text to remain "create_ticket" here.
        # Downstream quality guards can rephrase action while ticket intent
        # remains explicitly ready via decision.ticket fields.
        if not bool(decision.ticket.ready_to_create):
            return False, "ticket_not_ready_to_create"
        if bool(decision.missing_fields):
            return False, "ticket_missing_required_fields"
        if str(pending_action or "").strip():
            # Confirmation state is the trigger for ticket creation — do not block it
            if not str(pending_action or "").strip().lower().startswith("confirm_"):
                return False, "ticket_pending_followup"
        if self._is_waiting_state_for_user_input(next_state):
            return False, "ticket_waiting_for_user_input"
        return True, ""

    @staticmethod
    def _should_hide_intent_in_no_template_mode() -> bool:
        return bool(getattr(settings, "chat_no_template_response_mode", False))

    @staticmethod
    def _derive_orchestration_state(
        *,
        decision: OrchestrationDecision,
        current_state: ConversationState,
    ) -> ConversationState:
        if bool(decision.requires_human_handoff):
            return ConversationState.ESCALATED
        action = str(decision.action or "").strip().lower()
        pending_action = str(decision.pending_action or "").strip().lower()
        if action == "collect_info" or pending_action.startswith("collect_") or pending_action.startswith("select_"):
            return ConversationState.AWAITING_INFO
        if pending_action.startswith("confirm_"):
            return ConversationState.AWAITING_CONFIRMATION
        if action == "dispatch_handler":
            return current_state
        return ConversationState.IDLE

    async def _process_policy_verified_llm_message(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        selected_phase_context: dict[str, Any] | None = None,
        db_session=None,
    ) -> ChatResponse | None:
        """
        Strict no-template response path:
        - prefer orchestration/service LLM responses
        - fallback to pure full-KB LLM runtime
        - deterministic policy/validator gates remain enforced
        """
        try:
            orchestration_response = await self._process_llm_orchestration_message(
                request=request,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                selected_phase_context=selected_phase_context,
                db_session=db_session,
            )
            if orchestration_response is not None:
                return orchestration_response
        except Exception:
            logger.exception("no_template_orchestration_path_failed")

        return await self._process_pure_llm_message(
            request=request,
            context=context,
            capabilities_summary=capabilities_summary,
            routing_decision=routing_decision,
            memory_snapshot=memory_snapshot,
            db_session=db_session,
        )

    async def _process_llm_orchestration_message(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        selected_phase_context: dict[str, Any] | None = None,
        db_session=None,
    ) -> ChatResponse | None:
        """
        Two-stage LLM orchestration runtime:
        - orchestrator LLM decides service/action/ticket intent
        - service LLM refines slot collection and response
        - deterministic policy gate validates all action execution
        """
        effective_user_message = str(request.message or "").strip()
        decision = await llm_orchestration_service.orchestrate_turn(
            user_message=effective_user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            memory_snapshot=memory_snapshot,
            selected_phase_context=selected_phase_context,
        )
        if decision is None:
            return None
        no_template_mode = bool(getattr(settings, "chat_no_template_response_mode", False))
        hide_intent_in_response = self._should_hide_intent_in_no_template_mode()
        if not str(decision.target_service_id or "").strip() and isinstance(decision.metadata, dict):
            metadata_service_id = str(
                decision.metadata.get("target_service_id")
                or decision.metadata.get("service_id")
                or decision.metadata.get("resolved_service_id")
                or ""
            ).strip()
            if metadata_service_id:
                decision.target_service_id = metadata_service_id

        pending_action_active = str(context.pending_action or "").strip().lower()
        pending_public = context.pending_data if isinstance(context.pending_data, dict) else {}
        pending_service_id_hint = self._normalize_service_identifier(
            pending_public.get("service_id")
            or pending_public.get("target_service_id")
            or pending_public.get("resolved_service_id")
            or ""
        )
        if pending_action_active.startswith("confirm_"):
            confirmation_intent = self._detect_confirmation_intent(
                re.sub(r"\s+", " ", str(effective_user_message or "").strip().lower())
            )
            if pending_service_id_hint and not str(decision.target_service_id or "").strip():
                decision.target_service_id = pending_service_id_hint
            # In no_template/orchestration mode the service agent handles confirmations
            # and returns action="create_ticket" directly — do NOT override its decision.
            if not no_template_mode and confirmation_intent == IntentType.CONFIRMATION_YES:
                decision.action = "dispatch_handler"
                decision.use_handler = True
                decision.handler_intent = IntentType.CONFIRMATION_YES.value
                decision.intent = IntentType.CONFIRMATION_YES.value
                decision.interrupt_pending = False
                decision.resume_pending = False
                decision.cancel_pending = False
                decision.pending_action = context.pending_action
                decision.response_text = ""
                decision.pending_data_updates = (
                    dict(decision.pending_data_updates)
                    if isinstance(decision.pending_data_updates, dict)
                    else {}
                )
                if pending_service_id_hint:
                    decision.pending_data_updates.setdefault("service_id", pending_service_id_hint)
                decision.metadata.setdefault("confirmation_continuity_forced", True)
            elif (
                self._is_room_booking_pending_action(pending_action_active)
                and confirmation_intent is None
                and (
                    self._looks_like_room_booking_detail_followup(effective_user_message)
                    or self._looks_like_room_options_information_query(effective_user_message)
                    or self._looks_like_room_type_preference_reply(
                        str(effective_user_message or "").strip().lower()
                    )
                    or self._looks_like_room_stay_booking_request(effective_user_message)
                )
            ):
                decision.interrupt_pending = False
                decision.resume_pending = False
                decision.cancel_pending = False
                if str(decision.action or "").strip().lower() not in {"respond_only", "collect_info", "dispatch_handler"}:
                    decision.action = "respond_only"
                if not str(decision.pending_action or "").strip():
                    decision.pending_action = "collect_room_booking_details"
                decision.pending_data_updates = (
                    dict(decision.pending_data_updates)
                    if isinstance(decision.pending_data_updates, dict)
                    else {}
                )
                if pending_service_id_hint:
                    decision.pending_data_updates.setdefault("service_id", pending_service_id_hint)
                decision.metadata.setdefault("confirmation_continuity_reopened_collection", True)

        pending_interrupted = False
        if context.pending_action and bool(decision.interrupt_pending):
            parked = self._park_active_task(
                context=context,
                selected_phase_context=selected_phase_context or {},
                reason="llm_orchestration_topic_diversion",
                user_message=effective_user_message,
            )
            pending_interrupted = parked is not None

        if bool(decision.resume_pending) or str(decision.action or "").strip().lower() == "resume_pending":
            resumed = self._restore_parked_task(context)
            if resumed is not None:
                resumed_summary = str(resumed.get("summary") or "your previous request").strip()
                response_text = str(decision.response_text or "").strip() or (
                    f"Resuming {resumed_summary}. Please continue with the remaining details."
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=max(0.6, float(decision.confidence or 0.0))),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="llm_orchestration",
                    validator_replaced=False,
                )
                assistant_metadata = {
                    "intent": IntentType.FAQ.value,
                    "confidence": max(0.6, float(decision.confidence or 0.0)),
                    "service_llm_label": "main",
                    "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                    "channel": context.channel,
                    "orchestration_mode": True,
                    "orchestration_action": "resume_pending",
                }
                context.add_message(MessageRole.ASSISTANT, response_text, metadata=assistant_metadata)
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata=assistant_metadata,
                )
                await conversation_memory_service.maybe_refresh_summary(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=None if hide_intent_in_response else IntentType.FAQ,
                    confidence=None if hide_intent_in_response else max(0.6, float(decision.confidence or 0.0)),
                    service_llm_label="main",
                    service_llm_confidence=max(0.6, float(decision.confidence or 0.0)),
                    state=context.state,
                    suggested_actions=self._build_contextual_suggested_actions(
                        context.state,
                        IntentType.FAQ,
                        context.pending_action,
                        capabilities_summary,
                        context.pending_data,
                    ),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "llm_orchestration_resume_pending"},
                        "response_source": "llm_orchestration",
                        "orchestration_mode": True,
                        "service_llm_label": "main",
                        "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

        if bool(decision.cancel_pending) or str(decision.action or "").strip().lower() == "cancel_pending":
            canceled = self._cancel_parked_task(context)
            canceled_summary = str((canceled or {}).get("summary") or "that pending request").strip()
            response_text = str(decision.response_text or "").strip() or (
                f"Cancelled {canceled_summary}. What would you like to do next?"
            )
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=IntentResult(intent=IntentType.FAQ, confidence=max(0.6, float(decision.confidence or 0.0))),
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="llm_orchestration",
                validator_replaced=False,
            )
            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": IntentType.FAQ.value,
                "confidence": max(0.6, float(decision.confidence or 0.0)),
                "service_llm_label": "main",
                "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                "channel": context.channel,
                "orchestration_mode": True,
                "orchestration_action": "cancel_pending",
            }
            context.add_message(MessageRole.ASSISTANT, response_text, metadata=assistant_metadata)
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)
            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=None if hide_intent_in_response else IntentType.FAQ,
                confidence=None if hide_intent_in_response else max(0.6, float(decision.confidence or 0.0)),
                service_llm_label="main",
                service_llm_confidence=max(0.6, float(decision.confidence or 0.0)),
                state=ConversationState.IDLE,
                suggested_actions=config_service.get_quick_actions(limit=4),
                metadata={
                    "message_count": len(context.messages),
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "llm_orchestration_cancel_pending"},
                    "response_source": "llm_orchestration",
                    "orchestration_mode": True,
                    "service_llm_label": "main",
                    "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        policy_result = orchestration_policy_service.evaluate(
            decision=decision,
            context=context,
            capabilities_summary=capabilities_summary,
            selected_phase_context=selected_phase_context,
        )

        policy_override_applied = False
        if policy_result.override_response or not policy_result.action_allowed:
            override_payload = {
                "service_id": str(
                    ((policy_result.target_service or {}).get("id") if isinstance(policy_result.target_service, dict) else "")
                    or decision.target_service_id
                    or ""
                ).strip(),
                "service_name": str(
                    ((policy_result.target_service or {}).get("name") if isinstance(policy_result.target_service, dict) else "")
                    or decision.target_service_id
                    or "This service"
                ).strip(),
                "current_phase_id": str(policy_result.current_phase_id or "").strip(),
                "current_phase_name": str(policy_result.current_phase_name or "").strip(),
                "service_phase_id": str(policy_result.service_phase_id or "").strip(),
                "service_phase_name": str(policy_result.service_phase_name or "").strip(),
                "phase_service_unavailable": bool(policy_result.out_of_phase),
                "policy_blocked_reason": str(policy_result.blocked_reason or "").strip(),
                "ticket_skip_reason": str(policy_result.ticket_skip_reason or "").strip(),
                "suggested_actions": self._current_phase_service_names(
                    context=context,
                    capabilities_summary=capabilities_summary,
                )[:3] + ["Show available services", "Ask another question"],
            }
            override_intent = self._parse_orchestration_intent(
                decision.handler_intent or decision.intent,
                fallback=IntentType.FAQ,
            )
            override_text = await self._compose_policy_guardrail_response(
                user_message=effective_user_message,
                context=context,
                capabilities_summary=capabilities_summary,
                intent=override_intent,
                gate_payload=override_payload,
                fallback_text="",
                response_source="llm_orchestration_policy_gate",
            )
            decision.response_text = str(override_text or decision.response_text or "").strip()
            decision.use_handler = False
            decision.pending_action = None
            decision.pending_data_updates = {}
            policy_override_applied = True
            if policy_result.out_of_phase:
                decision.ticket = decision.ticket.model_copy(
                    update={
                        "required": False,
                        "ready_to_create": False,
                    }
                )

        # In orchestration/no_template mode the LLM controls everything —
        # intent mapping only causes validator interference. Use GENERAL_SERVICE.
        effective_intent = IntentType.GENERAL_SERVICE
        if bool(decision.requires_human_handoff):
            effective_intent = IntentType.HUMAN_REQUEST
        else:
            # Preserve explicit yes/no confirmation intent for in-flight
            # transactional continuations so handler dispatch gets the right signal.
            parsed_intent = self._parse_orchestration_intent(
                str(decision.handler_intent or decision.intent or ""),
                fallback=IntentType.GENERAL_SERVICE,
            )
            if parsed_intent in {IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO}:
                effective_intent = parsed_intent
        message_lower = str(effective_user_message or "").strip().lower()
        complaint_signal = (
            self._looks_like_operational_issue_for_ticketing(message_lower)
            or self._looks_like_strong_ticket_issue_marker(message_lower)
        )
        if complaint_signal and effective_intent in {
            IntentType.FAQ,
            IntentType.MENU_REQUEST,
            IntentType.UNCLEAR,
            IntentType.HUMAN_REQUEST,
            IntentType.GENERAL_SERVICE,
        }:
            effective_intent = IntentType.COMPLAINT
            if isinstance(decision.metadata, dict):
                decision.metadata["intent_override"] = "operational_issue_complaint"

        entities = dict(decision.metadata) if isinstance(decision.metadata, dict) else {}
        if decision.target_service_id:
            entities.setdefault("service_id", str(decision.target_service_id))
        if decision.missing_fields:
            entities.setdefault("missing_fields", list(decision.missing_fields))
        if bool(decision.ticket.required):
            entities.setdefault("ticketing_flow", True)
            if decision.ticket.reason:
                entities.setdefault("ticket_reason", decision.ticket.reason)
        intent_result = IntentResult(
            intent=effective_intent,
            confidence=max(0.0, min(1.0, float(decision.confidence or 0.0))),
            entities=entities,
            requires_confirmation=effective_intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING},
        )

        dispatch_handlers = bool(getattr(settings, "chat_llm_orchestration_dispatch_handlers", True))
        if no_template_mode:
            dispatch_handlers = False
        prefer_llm_response = bool(getattr(settings, "chat_llm_orchestration_prefer_llm_response", True))
        prefer_llm_suggested_actions = bool(getattr(settings, "chat_llm_orchestration_prefer_llm_suggested_actions", True))
        handler_result: HandlerResult | None = None
        if (
            policy_result.action_allowed
            and dispatch_handlers
            and bool(decision.use_handler or str(decision.action or "").strip().lower() == "dispatch_handler")
        ):
            handler_result = await self._dispatch_to_handler(
                effective_user_message,
                intent_result,
                context,
                capabilities_summary,
                db_session,
            )

        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        existing_public_pending = {
            key: value
            for key, value in (context.pending_data or {}).items()
            if isinstance(key, str) and not key.startswith("_")
        }

        response_text = str(decision.response_text or "").strip()
        next_state = self._derive_orchestration_state(decision=decision, current_state=context.state)
        if handler_result is not None:
            handler_text = str(handler_result.response_text or "").strip()
            if not prefer_llm_response or not response_text:
                response_text = handler_text or response_text
            next_state = handler_result.next_state
            if handler_result.pending_action is not None:
                context.pending_action = handler_result.pending_action
            if handler_result.pending_data is not None:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    handler_result.pending_data,
                    internal_pending_entries,
                )
            if handler_result.pending_action is None and next_state not in (
                ConversationState.AWAITING_CONFIRMATION,
                ConversationState.AWAITING_INFO,
                ConversationState.AWAITING_SELECTION,
                ConversationState.PROCESSING_ORDER,
            ):
                context.pending_action = None
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )
                context.pending_data.pop("_clarification_attempts", None)
            if handler_result.metadata.get("room_number"):
                context.room_number = str(handler_result.metadata["room_number"])
        else:
            pending_action_target = decision.pending_action
            if not pending_action_target and decision.missing_fields:
                first_missing = str(decision.missing_fields[0] or "").strip().lower()
                if first_missing:
                    pending_action_target = f"collect_{first_missing}"

            context.pending_action = pending_action_target
            merged_public_pending = dict(existing_public_pending)
            if isinstance(decision.pending_data_updates, dict):
                merged_public_pending.update(decision.pending_data_updates)

            if context.pending_action or merged_public_pending:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    merged_public_pending,
                    internal_pending_entries,
                )
            else:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )
                context.pending_data.pop("_clarification_attempts", None)

        if bool(decision.requires_human_handoff):
            next_state = ConversationState.ESCALATED
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )

        if context.pending_action and isinstance(context.pending_data, dict):
            resolved_pending_service_id = self._normalize_service_identifier(
                context.pending_data.get("service_id")
                or decision.target_service_id
                or pending_service_id_hint
                or ""
            )
            if resolved_pending_service_id:
                context.pending_data.setdefault("service_id", resolved_pending_service_id)

        ticket_meta: dict[str, Any] = {}
        if handler_result is None or not bool((handler_result.metadata or {}).get("ticket_created")):
            ticket_required = bool(decision.ticket.required)
            if ticket_required:
                ticket_allowed_now, ticket_skip_reason = self._can_create_orchestration_ticket_now(
                    decision=decision,
                    policy_result=policy_result,
                    next_state=next_state,
                    pending_action=context.pending_action,
                )
                if ticket_allowed_now:
                    issue_text = str(
                        decision.ticket.issue
                        or decision.ticket.reason
                        or response_text
                        or effective_user_message
                    ).strip()
                    category = str(
                        decision.ticket.category
                        or ("complaint" if effective_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST} else "request")
                    ).strip().lower()
                    sub_category = str(
                        decision.ticket.sub_category
                        or self._normalize_service_identifier(decision.target_service_id)
                        or "general_request"
                    ).strip().lower()
                    priority = str(decision.ticket.priority or "medium").strip().upper()
                    if priority not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                        priority = "MEDIUM"
                    phase_text = str(
                        policy_result.current_phase_name
                        or (selected_phase_context or {}).get("selected_phase_name")
                        or "During Stay"
                    ).strip()
                    try:
                        payload = ticketing_service.build_lumira_ticket_payload(
                            context=context,
                            issue=issue_text,
                            message=effective_user_message or issue_text,
                            category=category,
                            sub_category=sub_category,
                            priority=priority,
                            phase=phase_text,
                        )
                        if decision.target_service_id:
                            payload["service_id"] = str(decision.target_service_id).strip()
                        create_result = await ticketing_service.create_ticket(payload)
                        if create_result.success:
                            created_ticket_id = str(create_result.ticket_id or "").strip()
                            ticket_meta = {
                                "ticket_created": True,
                                "ticket_id": created_ticket_id,
                                "ticket_status": "open",
                                "ticket_category": category,
                                "ticket_sub_category": sub_category,
                                "ticket_priority": priority,
                                "ticket_source": "llm_orchestration",
                                "ticket_api_status_code": create_result.status_code,
                                "ticket_api_response": create_result.response,
                            }
                            # Update response_text with ticket confirmation so LLM response reflects reality
                            ticket_ref = f" (Reference: #{created_ticket_id})" if created_ticket_id else ""
                            # Build a summary from pending_data for the confirmation message
                            _pub = {k: v for k, v in (context.pending_data or {}).items() if not k.startswith("_")}
                            _parts = []
                            if _pub.get("room_type"): _parts.append(str(_pub["room_type"]))
                            if _pub.get("check_in_date"): _parts.append(f"check-in: {_pub['check_in_date']}")
                            if _pub.get("check_out_date"): _parts.append(f"check-out: {_pub['check_out_date']}")
                            if _pub.get("guest_count"): _parts.append(f"{_pub['guest_count']} guest(s)")
                            _summary = ", ".join(_parts)
                            response_text = (
                                f"Your booking has been confirmed{ticket_ref}! "
                                + (f"Details: {_summary}. " if _summary else "")
                                + "Our team will follow up with you shortly."
                            )
                            # Booking/ticket is completed: clear active pending flow
                            # and remove stale service carry-over data.
                            completed_service_id = self._normalize_service_identifier(
                                decision.target_service_id
                                or (
                                    context.pending_data.get("service_id")
                                    if isinstance(context.pending_data, dict)
                                    else ""
                                )
                                or ""
                            )
                            context.pending_action = None
                            context.pending_data = conversation_memory_service.merge_with_internal(
                                {},
                                internal_pending_entries,
                            )
                            context.pending_data.pop("_clarification_attempts", None)
                            if completed_service_id and isinstance(context.suspended_services, list):
                                context.suspended_services = [
                                    row
                                    for row in context.suspended_services
                                    if self._normalize_service_identifier((row or {}).get("service_id")) != completed_service_id
                                ]
                            if isinstance(context.suspended_services, list) and not context.suspended_services:
                                context.resume_prompt_sent = False
                            self._sync_active_task_snapshot(
                                context=context,
                                selected_phase_context=selected_phase_context or {},
                            )
                        else:
                            ticket_meta = {
                                "ticket_created": False,
                                "ticket_create_failed": True,
                                "ticket_error": str(create_result.error or "ticket_create_failed"),
                                "ticket_source": "llm_orchestration",
                                "ticket_api_status_code": create_result.status_code,
                                "ticket_api_response": create_result.response,
                            }
                            response_text = "I wasn't able to confirm your booking right now. Please try again or speak to our front desk."
                    except Exception as exc:
                        ticket_meta = {
                            "ticket_created": False,
                            "ticket_create_failed": True,
                            "ticket_error": str(exc),
                            "ticket_source": "llm_orchestration",
                        }
                else:
                    ticket_meta = {
                        "ticket_created": False,
                        "ticket_create_skipped": True,
                        "ticket_create_skip_reason": ticket_skip_reason or policy_result.ticket_skip_reason or "ticket_policy_gate_blocked",
                        "ticket_source": "llm_orchestration",
                    }

        if not response_text:
            response_text = "Please share one more detail so I can continue."

        if no_template_mode:
            capability_check = CapabilityCheck(allowed=True, reason="service_first_mode")
        else:
            intent_enabled_check = self._check_intent_enabled(effective_intent)
            capability_check = (
                intent_enabled_check
                if not intent_enabled_check.allowed
                else self._check_capability_for_intent(
                    context.hotel_code,
                    intent_result,
                    effective_user_message,
                )
            )
        # In orchestration mode the LLM/service-agent controls everything —
        # skip response_validator entirely to prevent canned responses overriding good LLM output.
        orchestration_validator_replaced = False
        if no_template_mode:
            from services.response_validator import ValidationResult as _VR
            orchestration_validation = _VR(valid=True, action="allow")
        else:
            orchestration_validation = response_validator.validate(
                response_text=response_text,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=capability_check.allowed,
                capability_reason=capability_check.reason,
            )
            if (
                not orchestration_validation.valid
                and orchestration_validation.action == "replace"
                and orchestration_validation.replacement_response
            ):
                response_text = orchestration_validation.replacement_response
                orchestration_validator_replaced = True
        response_text, orchestration_rewrite_metadata = await self._maybe_llm_rewrite_response(
            response_text=response_text,
            user_message=effective_user_message,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
            response_source="llm_orchestration",
            validator_replaced=(orchestration_validator_replaced or policy_override_applied),
        )

        response_text, resume_actions = self._append_resume_checkpoint(
            response_text=response_text,
            context=context,
            pending_interrupted=pending_interrupted,
        )

        context.state = next_state
        self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context or {},
        )
        assistant_intent_value = (
            "service_routed"
            if hide_intent_in_response
            else effective_intent.value
        )
        assistant_confidence_value = (
            float(decision.confidence or 0.0)
            if hide_intent_in_response
            else intent_result.confidence
        )
        orchestration_source = str((decision.metadata or {}).get("source") or "orchestrator")
        source_key = self._normalize_service_identifier(orchestration_source)
        service_llm_label = "main"
        if source_key.startswith("service_agent"):
            service_llm_label = self._service_llm_label_for_service(
                service_id=str(decision.target_service_id or ""),
                capabilities_summary=capabilities_summary,
                effective_intent=effective_intent,
                fallback="main",
            )
        service_llm_confidence = max(
            0.0,
            min(1.0, float(decision.confidence or intent_result.confidence or 0.0)),
        )
        assistant_metadata = {
            "intent": assistant_intent_value,
            "confidence": assistant_confidence_value,
            "service_llm_label": service_llm_label,
            "service_llm_confidence": service_llm_confidence,
            "channel": context.channel,
            "orchestration_mode": True,
            "orchestration_action": str(decision.action or ""),
            "orchestration_target_service_id": str(decision.target_service_id or ""),
            "orchestration_interrupt_pending": bool(decision.interrupt_pending),
            "orchestration_source": orchestration_source,
            "orchestration_trace_id": str((decision.metadata or {}).get("orchestration_trace_id") or ""),
            "policy_blocked_reason": str(policy_result.blocked_reason or ""),
            "policy_out_of_phase": bool(policy_result.out_of_phase),
            "policy_ticket_skip_reason": str(policy_result.ticket_skip_reason or ""),
            "orchestration_handler_executed": handler_result is not None,
            "orchestration_prefer_llm_response": prefer_llm_response,
            "intent_hidden_in_response": hide_intent_in_response,
            "response_validator_applied": True,
            "response_validator_replaced": orchestration_validator_replaced,
            "response_validator_issues": [
                str(issue.code)
                for issue in (orchestration_validation.issues or [])
                if getattr(issue, "code", None)
            ],
        }
        if handler_result is not None and handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        if ticket_meta:
            assistant_metadata.update(ticket_meta)
        assistant_metadata.update(orchestration_rewrite_metadata)

        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if prefer_llm_suggested_actions and decision.suggested_actions:
            suggested_actions = list(decision.suggested_actions)
        elif handler_result is not None and handler_result.suggested_actions:
            suggested_actions = list(handler_result.suggested_actions)
        elif decision.suggested_actions:
            suggested_actions = list(decision.suggested_actions)
        else:
            suggested_actions = self._get_suggested_actions(next_state, effective_intent, capabilities_summary)
        suggested_actions = resume_actions + suggested_actions
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=effective_intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        routing_path = getattr(
            getattr(routing_decision, "path", ProcessingPath.SIMPLE),
            "value",
            ProcessingPath.SIMPLE.value,
        )
        routing_score = float(getattr(routing_decision, "score", 1.0))
        routing_signals = list(getattr(routing_decision, "signals", []))

        metadata = {
            "message_count": len(context.messages),
            "entities": {
                "orchestration_action": str(decision.action or ""),
                "target_service_id": str(decision.target_service_id or ""),
                "missing_fields": list(decision.missing_fields),
                "pending_action": context.pending_action,
            },
            "classified_intent": assistant_intent_value,
            "classified_confidence": assistant_confidence_value,
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": "llm_orchestration",
            "orchestration_mode": True,
            "service_llm_label": service_llm_label,
            "service_llm_confidence": service_llm_confidence,
            "orchestration_policy_blocked_reason": str(policy_result.blocked_reason or ""),
            "orchestration_policy_out_of_phase": bool(policy_result.out_of_phase),
            "orchestration_policy_ticket_skip_reason": str(policy_result.ticket_skip_reason or ""),
            "intent_hidden_in_response": hide_intent_in_response,
            "response_validator_applied": True,
            "response_validator_replaced": orchestration_validator_replaced,
            "response_validator_issues": [
                str(issue.code)
                for issue in (orchestration_validation.issues or [])
                if getattr(issue, "code", None)
            ],
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        if ticket_meta:
            metadata.update(self._apply_ticket_metadata_contract(ticket_meta))
        metadata.update(orchestration_rewrite_metadata)

        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=None if hide_intent_in_response else effective_intent,
            confidence=None if hide_intent_in_response else intent_result.confidence,
            service_llm_label=service_llm_label,
            service_llm_confidence=service_llm_confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _process_pure_llm_message(
        self,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict,
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        db_session=None,
    ) -> ChatResponse:
        """
        Pure LLM runtime:
        - Uses full-KB LLM output directly for state/intent/pending response.
        - Avoids deterministic post-processing overrides.
        - Creates ticket only when LLM explicitly requests it and service toggles permit.
        """
        effective_user_message = str(request.message or "").strip()
        if bool(getattr(settings, "full_kb_llm_force_summary_refresh", True)):
            await conversation_memory_service.refresh_summary(context)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

        current_pending_action = context.pending_action
        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        existing_public_pending = {
            key: value
            for key, value in (context.pending_data or {}).items()
            if isinstance(key, str) and not key.startswith("_")
        }

        llm_result = await full_kb_llm_service.run_turn(
            user_message=effective_user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            memory_snapshot=memory_snapshot,
        )
        no_template_mode = bool(getattr(settings, "chat_no_template_response_mode", False))
        hide_intent_in_response = self._should_hide_intent_in_no_template_mode()

        next_state = llm_result.next_state
        response_text = str(llm_result.response_text or "").strip() or (
            "I could not find this in the current knowledge base for this property."
        )
        effective_intent = llm_result.intent
        effective_confidence = llm_result.confidence
        pending_action_target = str(llm_result.pending_action).strip() if llm_result.pending_action else None
        pending_data_target = dict(llm_result.pending_data or {})
        pure_llm_entities = self._extract_full_kb_entities_for_handler(llm_result)
        pending_data_target = self._enrich_pending_with_known_facts(
            pending_data=pending_data_target,
            context=context,
            memory_snapshot=memory_snapshot,
            entities=pure_llm_entities,
        )
        if no_template_mode:
            service_driven_intent = self._intent_from_service_id(
                service_id=str(
                    pure_llm_entities.get("service_id")
                    or pure_llm_entities.get("target_service_id")
                    or pure_llm_entities.get("resolved_service_id")
                    or ""
                ),
                capabilities_summary=capabilities_summary,
                fallback=effective_intent,
            )
            effective_intent = service_driven_intent
        clear_pending_data = bool(llm_result.clear_pending_data)
        contextual_sanity_issue = self._detect_contextual_sanity_issue(
            message=effective_user_message,
            intent=effective_intent,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=pure_llm_entities,
        )
        if contextual_sanity_issue is not None:
            response_text = str(contextual_sanity_issue.get("response_text") or "").strip() or response_text
            next_state = ConversationState.IDLE
            pending_action_target = None
            pending_data_target = {}
            clear_pending_data = True

        pure_validation_intent = IntentResult(
            intent=effective_intent,
            confidence=max(0.0, min(1.0, float(effective_confidence or 0.0))),
            entities=pure_llm_entities if isinstance(pure_llm_entities, dict) else {},
        )
        if no_template_mode:
            pure_capability_check = CapabilityCheck(allowed=True, reason="service_first_mode")
        else:
            intent_enabled_check = self._check_intent_enabled(effective_intent)
            pure_capability_check = (
                intent_enabled_check
                if not intent_enabled_check.allowed
                else self._check_capability_for_intent(
                    context.hotel_code,
                    pure_validation_intent,
                    effective_user_message,
                )
            )
        pure_validation = response_validator.validate(
            response_text=response_text,
            intent_result=pure_validation_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=pure_capability_check.allowed,
            capability_reason=pure_capability_check.reason,
        )
        pure_validator_replaced = False
        if not pure_validation.valid and pure_validation.action == "replace" and pure_validation.replacement_response:
            response_text = pure_validation.replacement_response
            pure_validator_replaced = True
        response_text, pure_response_rewrite_metadata = await self._maybe_llm_rewrite_response(
            response_text=response_text,
            user_message=effective_user_message,
            intent_result=pure_validation_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=pure_capability_check.allowed,
            capability_reason=pure_capability_check.reason,
            response_source="pure_llm",
            validator_replaced=pure_validator_replaced,
        )

        context.pending_action = pending_action_target
        if clear_pending_data:
            merged_public_pending: dict[str, Any] = {}
        elif pending_data_target:
            merged_public_pending = dict(pending_data_target)
        else:
            merged_public_pending = dict(existing_public_pending)
        context.pending_data = conversation_memory_service.merge_with_internal(
            merged_public_pending,
            internal_pending_entries,
        )

        if context.pending_action is None and next_state not in (
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
            ConversationState.PROCESSING_ORDER,
        ):
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )
            context.pending_data.pop("_clarification_attempts", None)

        if next_state == ConversationState.ESCALATED:
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )

        if llm_result.room_number:
            context.room_number = str(llm_result.room_number)
        context.state = next_state

        pure_ticket_meta = await self._maybe_create_pure_llm_ticket(
            request=request,
            context=context,
            capabilities_summary=capabilities_summary,
            llm_result=llm_result,
            effective_intent=effective_intent,
        )
        pure_ticket_meta = self._apply_ticket_metadata_contract(pure_ticket_meta or {})
        assistant_intent_value = (
            "service_routed"
            if hide_intent_in_response
            else effective_intent.value
        )
        assistant_confidence_value = (
            float(effective_confidence or 0.0)
            if hide_intent_in_response
            else effective_confidence
        )
        llm_output_map = llm_result.llm_output if isinstance(getattr(llm_result, "llm_output", None), dict) else {}
        pure_service_id = str(
            pure_llm_entities.get("service_id")
            or pure_llm_entities.get("target_service_id")
            or pure_llm_entities.get("resolved_service_id")
            or llm_output_map.get("service_id")
            or llm_output_map.get("target_service_id")
            or llm_output_map.get("resolved_service_id")
            or ""
        ).strip()
        service_llm_label = self._service_llm_label_for_service(
            service_id=pure_service_id,
            capabilities_summary=capabilities_summary,
            effective_intent=effective_intent,
            fallback="main",
        )
        service_llm_confidence = max(0.0, min(1.0, float(effective_confidence or 0.0)))

        assistant_metadata = {
            "intent": assistant_intent_value,
            "confidence": assistant_confidence_value,
            "service_llm_label": service_llm_label,
            "service_llm_confidence": service_llm_confidence,
            "channel": context.channel,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "pure_llm_mode": True,
            "full_kb_trace_id": llm_result.trace_id,
            "full_kb_raw_intent": llm_result.raw_intent,
            "full_kb_normalized_query": llm_result.normalized_query,
            "response_source": "pure_llm",
            "contextual_sanity_gate": bool(contextual_sanity_issue),
            "contextual_sanity_code": str((contextual_sanity_issue or {}).get("code") or ""),
            "intent_hidden_in_response": hide_intent_in_response,
            "response_validator_applied": True,
            "response_validator_replaced": pure_validator_replaced,
            "response_validator_issues": [
                str(issue.code)
                for issue in (pure_validation.issues or [])
                if getattr(issue, "code", None)
            ],
        }
        if pure_ticket_meta:
            assistant_metadata.update(pure_ticket_meta)
        assistant_metadata.update(pure_response_rewrite_metadata)

        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if llm_result.suggested_actions:
            suggested_actions = llm_result.suggested_actions
        else:
            suggested_actions = self._get_suggested_actions(
                next_state,
                effective_intent,
                capabilities_summary,
            )

        routing_path = getattr(
            getattr(routing_decision, "path", ProcessingPath.SIMPLE),
            "value",
            ProcessingPath.SIMPLE.value,
        )
        routing_score = float(getattr(routing_decision, "score", 1.0))
        routing_signals = list(getattr(routing_decision, "signals", []))

        metadata = {
            "message_count": len(context.messages),
            "entities": {
                "normalized_query": llm_result.normalized_query,
                "raw_intent": llm_result.raw_intent,
                "pending_action": context.pending_action,
            },
            "classified_intent": assistant_intent_value,
            "classified_confidence": assistant_confidence_value,
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": "pure_llm",
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "pure_llm_mode": True,
            "service_llm_label": service_llm_label,
            "service_llm_confidence": service_llm_confidence,
            "full_kb_trace_id": llm_result.trace_id,
            "full_kb_status": llm_result.status,
            "contextual_sanity_gate": bool(contextual_sanity_issue),
            "contextual_sanity_code": str((contextual_sanity_issue or {}).get("code") or ""),
            "intent_hidden_in_response": hide_intent_in_response,
            "response_validator_applied": True,
            "response_validator_replaced": pure_validator_replaced,
            "response_validator_issues": [
                str(issue.code)
                for issue in (pure_validation.issues or [])
                if getattr(issue, "code", None)
            ],
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        if pure_ticket_meta:
            metadata.update(pure_ticket_meta)
        metadata.update(pure_response_rewrite_metadata)

        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=None if hide_intent_in_response else effective_intent,
            confidence=None if hide_intent_in_response else effective_confidence,
            service_llm_label=service_llm_label,
            service_llm_confidence=service_llm_confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _process_full_kb_llm_message(
        self,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict,
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        db_session=None,
    ) -> ChatResponse:
        """
        Run strict full-KB prompting mode:
        - no retrieval chunks
        - full KB text is passed to LLM each turn
        - LLM returns state/intent/action JSON that drives the conversation.
        """
        effective_user_message = str(request.message or "").strip()
        pre_shortcuts_enabled = bool(
            getattr(settings, "full_kb_llm_pre_shortcuts_enabled", False)
        )
        greeting_match = (
            self._match_greeting_response(
                effective_user_message,
                capabilities_summary,
                context,
            )
            if pre_shortcuts_enabled
            else None
        )
        if greeting_match is not None:
            response_text = str(greeting_match.get("response_text") or "").strip()
            intent = IntentType.GREETING
            confidence = 0.95
            next_state = ConversationState.IDLE

            context.state = next_state
            assistant_metadata = {
                "intent": intent.value,
                "confidence": confidence,
                "channel": context.channel,
                "kb_only_mode": True,
                "full_kb_llm_mode": True,
                "response_source": "full_kb_shortcut",
                "full_kb_shortcut_type": "greeting",
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent,
                confidence=confidence,
                state=next_state,
                suggested_actions=self._get_suggested_actions(
                    next_state,
                    intent,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": {"shortcut": "greeting"},
                    "classified_intent": intent.value,
                    "classified_confidence": confidence,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "full_kb_greeting_shortcut"},
                    "response_source": "full_kb_shortcut",
                    "kb_only_mode": True,
                    "full_kb_llm_mode": True,
                    "full_kb_shortcut_type": "greeting",
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                    "memory_recent_changes": memory_snapshot.get("recent_changes", []),
                },
            )

        identity_match = (
            self._match_identity_response(
                effective_user_message,
                capabilities_summary,
                context,
            )
            if pre_shortcuts_enabled
            else None
        )
        if identity_match is not None:
            response_text = str(identity_match.get("response_text") or "").strip()
            intent = IntentType.FAQ
            confidence = 0.92
            next_state = ConversationState.IDLE

            context.state = next_state
            assistant_metadata = {
                "intent": intent.value,
                "confidence": confidence,
                "channel": context.channel,
                "kb_only_mode": True,
                "full_kb_llm_mode": True,
                "response_source": "full_kb_shortcut",
                "full_kb_shortcut_type": "identity",
                "identity_match_type": identity_match.get("match_type"),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent,
                confidence=confidence,
                state=next_state,
                suggested_actions=self._get_suggested_actions(
                    next_state,
                    intent,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": {"identity_match_type": identity_match.get("match_type")},
                    "classified_intent": intent.value,
                    "classified_confidence": confidence,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "full_kb_identity_shortcut"},
                    "response_source": "full_kb_shortcut",
                    "kb_only_mode": True,
                    "full_kb_llm_mode": True,
                    "full_kb_shortcut_type": "identity",
                    "identity_match_type": identity_match.get("match_type"),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                    "memory_recent_changes": memory_snapshot.get("recent_changes", []),
                },
            )

        if bool(getattr(settings, "full_kb_llm_force_summary_refresh", True)):
            await conversation_memory_service.refresh_summary(context)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

        current_pending_action = context.pending_action
        llm_passthrough_mode = bool(getattr(settings, "full_kb_llm_passthrough_mode", False))
        rewritten_user_message = effective_user_message
        rewrite_applied = False
        more_options_rewrite_applied = False
        if not llm_passthrough_mode:
            rewritten_user_message = self._rewrite_affirmative_selection_reply(context, effective_user_message)
            rewritten_user_message = self._rewrite_more_options_reply(context, rewritten_user_message)
            rewrite_applied = rewritten_user_message != effective_user_message
            more_options_rewrite_applied = rewrite_applied and self._looks_like_more_options_followup(effective_user_message)
        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        existing_public_pending = {
            key: value
            for key, value in (context.pending_data or {}).items()
            if isinstance(key, str) and not key.startswith("_")
        }
        llm_result = await full_kb_llm_service.run_turn(
            user_message=rewritten_user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            memory_snapshot=memory_snapshot,
        )

        next_state = llm_result.next_state
        response_text = llm_result.response_text
        effective_intent = llm_result.intent
        effective_confidence = llm_result.confidence
        pending_action_target = llm_result.pending_action
        pending_data_target = dict(llm_result.pending_data or {})
        llm_entity_map = self._extract_full_kb_entities_for_handler(llm_result)
        pending_data_target = self._enrich_pending_with_known_facts(
            pending_data=pending_data_target,
            context=context,
            memory_snapshot=memory_snapshot,
            entities=llm_entity_map,
        )
        clear_pending_data = bool(llm_result.clear_pending_data)
        confirmation_guard_applied = False

        full_kb_phase_gate_response = await self._maybe_handle_full_kb_ticketing_phase_gate(
            request=request,
            context=context,
            capabilities_summary=capabilities_summary,
            routing_decision=routing_decision,
            llm_result=llm_result,
            effective_intent=effective_intent,
            effective_confidence=effective_confidence,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=llm_entity_map,
            db_session=db_session,
        )
        if full_kb_phase_gate_response is not None:
            return full_kb_phase_gate_response

        ticketing_response = await self._maybe_route_full_kb_ticketing_handler(
            request=request,
            context=context,
            capabilities_summary=capabilities_summary,
            routing_decision=routing_decision,
            llm_result=llm_result,
            effective_intent=effective_intent,
            effective_confidence=effective_confidence,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            rewrite_applied=rewrite_applied,
            more_options_rewrite_applied=more_options_rewrite_applied,
            db_session=db_session,
        )
        if ticketing_response is not None:
            return ticketing_response

        if llm_passthrough_mode:
            deterministic_room_updates: dict[str, Any] = {}
            deterministic_order_updates: dict[str, Any] = {}
            merged_for_logic = dict(existing_public_pending)
            merged_for_logic.update(pending_data_target or {})

            room_booking_flow = self._is_room_booking_flow(
                raw_intent=llm_result.raw_intent,
                current_pending_action=current_pending_action,
                pending_action_target=pending_action_target,
                pending_data=merged_for_logic,
                response_text=response_text,
            )
            if room_booking_flow:
                deterministic_room_updates = self._extract_room_booking_slot_updates(
                    message=effective_user_message,
                    current_pending_action=current_pending_action,
                    state=context.state,
                    pending_data=merged_for_logic,
                    memory_facts=memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {},
                    conversation_context=context,
                )
                if deterministic_room_updates:
                    merged_for_logic.update(deterministic_room_updates)
                    pending_data_target = dict(merged_for_logic)

                room_missing_fields = self._missing_room_booking_fields(
                    pending_data=merged_for_logic,
                    memory_facts=memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {},
                )
                room_pending_action = str(pending_action_target or current_pending_action or "").strip().lower()
                if room_missing_fields:
                    effective_intent = IntentType.TABLE_BOOKING
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_room_booking_details"
                    pending_data_target = dict(merged_for_logic)
                    clear_pending_data = False
                    if not self._looks_like_room_options_information_query(effective_user_message):
                        response_text = self._build_room_booking_missing_details_prompt(
                            missing_fields=room_missing_fields,
                            pending_data=pending_data_target,
                        )
                elif (
                    next_state != ConversationState.AWAITING_CONFIRMATION
                    and not room_pending_action.startswith("confirm_")
                    and effective_intent != IntentType.CONFIRMATION_YES
                ):
                    effective_intent = IntentType.TABLE_BOOKING
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_CONFIRMATION
                    pending_action_target = "confirm_room_booking"
                    pending_data_target = dict(merged_for_logic)
                    clear_pending_data = False
                    response_text = self._build_room_booking_review_prompt(pending_data_target)
                    confirmation_phrase = str(
                        getattr(settings, "chat_confirmation_phrase", "yes confirm")
                    ).strip() or "yes confirm"
                    if bool(getattr(settings, "chat_require_strict_confirmation_phrase", True)):
                        response_text = self._ensure_explicit_confirmation_instruction(
                            response_text=response_text,
                            confirmation_phrase=confirmation_phrase,
                        )
                elif (
                    effective_intent == IntentType.CONFIRMATION_YES
                    and room_pending_action in {"confirm_booking", "confirm_room_booking", "confirm_room_availability_check"}
                    and self._looks_like_phase_unavailable_response(response_text)
                ):
                    # Guardrail: when room details are already complete and user confirms,
                    # do not let phase-wording drift in LLM output block the finalized booking.
                    effective_confidence = max(effective_confidence, 0.95)
                    next_state = ConversationState.COMPLETED
                    pending_action_target = None
                    pending_data_target = {}
                    clear_pending_data = True
                    response_text = self._build_room_booking_confirmation_success_response(merged_for_logic)

            if self._is_order_pending_action(current_pending_action) or self._is_order_pending_action(pending_action_target):
                deterministic_order_updates = self._extract_order_slot_updates(
                    message=effective_user_message,
                    response_text=response_text,
                    current_pending_action=current_pending_action,
                    pending_data=merged_for_logic,
                )
                if deterministic_order_updates:
                    merged_for_logic.update(deterministic_order_updates)
                    pending_data_target = dict(merged_for_logic)

                pending_lower = str(pending_action_target or current_pending_action or "").strip().lower()
                item_name = self._extract_order_item_name(merged_for_logic)
                quantity = self._extract_order_quantity_value(merged_for_logic)
                user_msg_lower = str(effective_user_message or "").strip().lower()
                order_category_hint = self._derive_order_category_hint(effective_user_message, item_name)
                order_options_followup = self._is_order_options_followup(
                    message=effective_user_message,
                    item_name=item_name,
                    current_pending_action=pending_lower,
                )
                explicit_issue_switch = (
                    self._looks_like_ticketing_request(user_msg_lower)
                    or self._looks_like_strong_ticket_issue_marker(user_msg_lower)
                )
                if (
                    item_name
                    and pending_lower in {"order_food", "collect_order_item"}
                    and not explicit_issue_switch
                ):
                    phase_context_current = self._get_selected_phase_context(
                        context=context,
                        pending_data=merged_for_logic,
                        entities=llm_entity_map,
                    )
                    disambiguation_response = await self._build_order_item_disambiguation_response(
                        item_name=item_name,
                        capabilities_summary=capabilities_summary,
                        db_session=db_session,
                        hotel_code=context.hotel_code,
                        selected_phase_id=phase_context_current.get("selected_phase_id", ""),
                        selected_phase_name=phase_context_current.get("selected_phase_name", ""),
                    )
                    if disambiguation_response:
                        effective_intent = IntentType.ORDER_FOOD
                        effective_confidence = max(effective_confidence, 0.9)
                        next_state = ConversationState.AWAITING_SELECTION
                        pending_action_target = "collect_order_item"
                        pending_data_target = dict(merged_for_logic)
                        for key in ("order_item", "order_quantity", "order_offer_done", "order_addons_choice"):
                            pending_data_target.pop(key, None)
                        option_candidates = self._extract_order_option_candidates_from_response(disambiguation_response)
                        if option_candidates:
                            pending_data_target["order_option_candidates"] = option_candidates
                        else:
                            pending_data_target.pop("order_option_candidates", None)
                        clear_pending_data = False
                        response_text = disambiguation_response
                        item_name = ""
                        quantity = None

                if order_options_followup and pending_lower in {"order_food", "collect_order_item", "collect_order_quantity"}:
                    phase_context_current = self._get_selected_phase_context(
                        context=context,
                        pending_data=merged_for_logic,
                        entities=llm_entity_map,
                    )
                    options_response = await self._build_order_options_list_response(
                        category_hint=order_category_hint or item_name,
                        capabilities_summary=capabilities_summary,
                        db_session=db_session,
                        hotel_code=context.hotel_code,
                        selected_phase_id=phase_context_current.get("selected_phase_id", ""),
                        selected_phase_name=phase_context_current.get("selected_phase_name", ""),
                    )
                    if options_response:
                        effective_intent = IntentType.ORDER_FOOD
                        effective_confidence = max(effective_confidence, 0.9)
                        next_state = ConversationState.AWAITING_SELECTION
                        pending_action_target = "collect_order_item"
                        pending_data_target = dict(merged_for_logic)
                        pending_data_target.pop("order_quantity", None)
                        option_candidates = self._extract_order_option_candidates_from_response(options_response)
                        if option_candidates:
                            pending_data_target["order_option_candidates"] = option_candidates
                        else:
                            pending_data_target.pop("order_option_candidates", None)
                        clear_pending_data = False
                        response_text = options_response

                if pending_lower == "collect_order_item" and item_name and not explicit_issue_switch and not order_options_followup:
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_quantity"
                    merged_for_logic["order_item"] = item_name
                    merged_for_logic.pop("order_option_candidates", None)
                    pending_data_target = dict(merged_for_logic)
                    clear_pending_data = False
                    if quantity is None:
                        response_text = self._build_order_quantity_prompt(item_name)
                    else:
                        merged_for_logic["order_offer_done"] = True
                        pending_data_target = dict(merged_for_logic)
                        pending_action_target = "collect_order_addons"
                        response_text = self._build_order_addons_prompt(item_name, quantity)

                if pending_lower == "collect_order_quantity" and not explicit_issue_switch:
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    clear_pending_data = False
                    if quantity is None:
                        pending_action_target = "collect_order_quantity"
                        response_text = self._build_order_quantity_prompt(item_name)
                    else:
                        merged_for_logic["order_offer_done"] = True
                        pending_data_target = dict(merged_for_logic)
                        pending_action_target = "collect_order_addons"
                        response_text = self._build_order_addons_prompt(item_name, quantity)

            if next_state == ConversationState.AWAITING_CONFIRMATION:
                pending_action_target = self._normalize_confirm_pending_action(
                    pending_action_target=pending_action_target,
                    intent=effective_intent,
                    room_booking_flow=room_booking_flow,
                )
                confirmation_phrase = str(
                    getattr(settings, "chat_confirmation_phrase", "yes confirm")
                ).strip() or "yes confirm"
                if bool(getattr(settings, "chat_require_strict_confirmation_phrase", True)):
                    response_text = self._ensure_explicit_confirmation_instruction(
                        response_text=response_text,
                        confirmation_phrase=confirmation_phrase,
                    )

            (
                response_text,
                full_kb_validator_replaced,
                full_kb_validation_issue_codes,
                full_kb_capability_allowed,
            ) = await self._apply_full_kb_response_validation(
                response_text=response_text,
                effective_intent=effective_intent,
                effective_confidence=effective_confidence,
                context=context,
                capabilities_summary=capabilities_summary,
                request_message=effective_user_message,
            )

            transaction_ticket_meta = await self._maybe_create_full_kb_transaction_ticket(
                context=context,
                capabilities_summary=capabilities_summary,
                current_pending_action=current_pending_action,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_data_snapshot=merged_for_logic,
                response_text=response_text,
            )
            plugin_ticket_meta: dict[str, Any] = {}
            if not bool(transaction_ticket_meta.get("ticket_created")):
                plugin_ticket_meta = await self._maybe_create_full_kb_plugin_ticket(
                    request=request,
                    context=context,
                    capabilities_summary=capabilities_summary,
                    llm_result=llm_result,
                    effective_intent=effective_intent,
                    next_state=next_state,
                    current_pending_action=current_pending_action,
                    pending_data_snapshot=merged_for_logic,
                    response_text=response_text,
                )
            combined_ticket_meta: dict[str, Any] = {}
            if transaction_ticket_meta:
                combined_ticket_meta.update(transaction_ticket_meta)
            if plugin_ticket_meta:
                combined_ticket_meta.update(plugin_ticket_meta)
            combined_ticket_meta = self._apply_ticket_metadata_contract(combined_ticket_meta)
            (
                pending_action_target,
                pending_data_target,
                next_state,
                clear_pending_data,
            ) = self._apply_full_kb_plugin_ticket_followup_state(
                combined_ticket_meta=combined_ticket_meta,
                current_pending_action=current_pending_action,
                pending_action_target=pending_action_target,
                pending_data_target=pending_data_target,
                internal_pending_entries=internal_pending_entries,
                next_state=next_state,
                clear_pending_data=clear_pending_data,
            )
            response_text = self._maybe_add_missing_room_number_prompt(
                response_text=response_text,
                ticket_meta=combined_ticket_meta,
                context=context,
                pending_data=merged_for_logic,
                entities=llm_entity_map,
            )
            response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

            context.pending_action = pending_action_target
            if clear_pending_data:
                merged_public_pending: dict[str, Any] = {}
            elif pending_data_target:
                merged_public_pending = dict(existing_public_pending)
                merged_public_pending.update(pending_data_target)
            else:
                merged_public_pending = dict(existing_public_pending)
            context.pending_data = conversation_memory_service.merge_with_internal(
                merged_public_pending,
                internal_pending_entries,
            )

            if context.pending_action is None and next_state not in (
                ConversationState.AWAITING_CONFIRMATION,
                ConversationState.AWAITING_INFO,
                ConversationState.AWAITING_SELECTION,
                ConversationState.PROCESSING_ORDER,
            ):
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )
                context.pending_data.pop("_clarification_attempts", None)

            if next_state == ConversationState.ESCALATED:
                context.pending_action = None
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )

            if llm_result.room_number:
                context.room_number = str(llm_result.room_number)

            context.state = next_state

            assistant_metadata = {
                "intent": effective_intent.value,
                "confidence": effective_confidence,
                "channel": context.channel,
                "kb_only_mode": True,
                "full_kb_llm_mode": True,
                "full_kb_llm_passthrough_mode": True,
                "full_kb_trace_id": llm_result.trace_id,
                "full_kb_raw_intent": llm_result.raw_intent,
                "full_kb_normalized_query": llm_result.normalized_query,
                "response_source": "full_kb_llm",
                "confirmation_guard_applied": confirmation_guard_applied,
                "affirmative_selection_rewrite_applied": rewrite_applied,
                "more_options_rewrite_applied": more_options_rewrite_applied,
                "deterministic_room_updates": deterministic_room_updates,
                "deterministic_order_updates": deterministic_order_updates,
                "full_kb_validator_replaced": full_kb_validator_replaced,
                "full_kb_validation_issue_codes": full_kb_validation_issue_codes,
                "full_kb_capability_allowed": full_kb_capability_allowed,
                "response_validator_applied": True,
                "response_validator_replaced": full_kb_validator_replaced,
                "response_validator_issues": full_kb_validation_issue_codes,
            }
            if combined_ticket_meta:
                assistant_metadata.update(combined_ticket_meta)
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            if llm_result.suggested_actions:
                suggested_actions = llm_result.suggested_actions
            else:
                suggested_actions = self._build_contextual_suggested_actions(
                    next_state,
                    effective_intent,
                    context.pending_action,
                    capabilities_summary,
                    context.pending_data,
                )
            suggested_actions = self._finalize_user_query_suggestions(
                suggested_actions,
                state=next_state,
                intent=effective_intent,
                pending_action=context.pending_action,
                pending_data=context.pending_data,
                capabilities_summary=capabilities_summary,
            )

            routing_path = getattr(
                getattr(routing_decision, "path", ProcessingPath.SIMPLE),
                "value",
                ProcessingPath.SIMPLE.value,
            )
            routing_score = float(getattr(routing_decision, "score", 1.0))
            routing_signals = list(getattr(routing_decision, "signals", []))

            metadata = {
                "message_count": len(context.messages),
                "entities": {
                    "normalized_query": llm_result.normalized_query,
                    "raw_intent": llm_result.raw_intent,
                    "pending_action": context.pending_action,
                    "confirmation_guard_applied": confirmation_guard_applied,
                    "affirmative_selection_rewrite_applied": rewrite_applied,
                    "more_options_rewrite_applied": more_options_rewrite_applied,
                    "deterministic_room_updates": deterministic_room_updates,
                    "deterministic_order_updates": deterministic_order_updates,
                },
                "classified_intent": effective_intent.value,
                "classified_confidence": effective_confidence,
                "routing_path": routing_path,
                "routing_score": routing_score,
                "routing_signals": routing_signals,
                "response_source": "full_kb_llm",
                "kb_only_mode": True,
                "full_kb_llm_mode": True,
                "full_kb_llm_passthrough_mode": True,
                "full_kb_trace_id": llm_result.trace_id,
                    "full_kb_status": llm_result.status,
                    "full_kb_confirmation_guard_applied": confirmation_guard_applied,
                    "full_kb_validator_replaced": full_kb_validator_replaced,
                    "full_kb_validation_issue_codes": full_kb_validation_issue_codes,
                    "full_kb_capability_allowed": full_kb_capability_allowed,
                    "response_validator_applied": True,
                    "response_validator_replaced": full_kb_validator_replaced,
                    "response_validator_issues": full_kb_validation_issue_codes,
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                    "memory_recent_changes": memory_snapshot.get("recent_changes", []),
                }
            if combined_ticket_meta:
                metadata.update(combined_ticket_meta)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=effective_intent,
                confidence=effective_confidence,
                state=next_state,
                suggested_actions=suggested_actions,
                metadata=metadata,
            )

        required_confirmation_phrase = str(
            getattr(settings, "chat_confirmation_phrase", "yes confirm")
        ).strip() or "yes confirm"
        strict_confirmation_enabled = bool(
            getattr(settings, "chat_require_strict_confirmation_phrase", True)
        )
        user_compact = re.sub(r"\s+", " ", str(effective_user_message or "").strip().lower())
        required_compact = re.sub(r"\s+", " ", required_confirmation_phrase.lower())
        strict_confirmation_required = self._requires_strict_confirmation(
            context=context,
            current_pending_action=current_pending_action,
        )

        if effective_intent == IntentType.CONFIRMATION_YES:
            if (
                strict_confirmation_enabled
                and strict_confirmation_required
                and user_compact != required_compact
            ):
                confirmation_guard_applied = True
                effective_intent = IntentType.FAQ
                effective_confidence = 0.95
                next_state = ConversationState.AWAITING_CONFIRMATION
                response_text = (
                    f"To confirm this request, please type \"{required_confirmation_phrase}\".\n"
                    "If you want to cancel, type \"cancel\"."
                )
                pending_action_target = current_pending_action
                pending_data_target = dict(existing_public_pending)
                clear_pending_data = False
            elif not strict_confirmation_required:
                pass
            elif not current_pending_action:
                confirmation_guard_applied = True
                effective_intent = IntentType.FAQ
                effective_confidence = 0.9
                next_state = ConversationState.IDLE
                response_text = (
                    "There is no pending request to confirm right now. "
                    "Please tell me what you would like to do."
                )
                pending_action_target = None
                pending_data_target = dict(existing_public_pending)
                clear_pending_data = False

        memory_facts_before = memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {}
        if not isinstance(memory_facts_before, dict):
            memory_facts_before = {}

        merged_for_logic = dict(existing_public_pending)
        merged_for_logic.update(pending_data_target or {})
        deterministic_room_updates = self._extract_room_booking_slot_updates(
            message=effective_user_message,
            current_pending_action=current_pending_action,
            state=context.state,
            pending_data=merged_for_logic,
            memory_facts=memory_facts_before,
            conversation_context=context,
        )
        if deterministic_room_updates:
            merged_for_logic.update(deterministic_room_updates)
            pending_data_target = dict(merged_for_logic)

        deterministic_order_updates = self._extract_order_slot_updates(
            message=effective_user_message,
            response_text=response_text,
            current_pending_action=current_pending_action,
            pending_data=merged_for_logic,
        )
        if deterministic_order_updates:
            merged_for_logic.update(deterministic_order_updates)
            pending_data_target = dict(merged_for_logic)

        room_booking_flow = self._is_room_booking_flow(
            raw_intent=llm_result.raw_intent,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            pending_data=merged_for_logic,
            response_text=response_text,
        )
        user_binary_reply = self._detect_simple_binary_reply(effective_user_message)

        # Correct obvious polarity mismatches in selection flow (e.g., user says "yes"
        # but model emits confirmation_no).
        if (
            room_booking_flow
            and context.state == ConversationState.AWAITING_SELECTION
            and str(current_pending_action or "").strip().lower() in {"select_room_type", "room_booking"}
            and user_binary_reply == IntentType.CONFIRMATION_YES
            and effective_intent == IntentType.CONFIRMATION_NO
        ):
            effective_intent = IntentType.TABLE_BOOKING
            effective_confidence = max(effective_confidence, 0.9)
            # User chose to continue exploring/booking; reset stale room choice and
            # continue slot collection cleanly.
            merged_for_logic.pop("room_type", None)
            pending_data_target = dict(merged_for_logic)
            pending_action_target = "collect_room_booking_details"
            next_state = ConversationState.AWAITING_INFO
            response_text = (
                "Sure. Please share your preferred room type, check-in date, "
                "check-out date, and number of guests so I can proceed."
            )
            clear_pending_data = False

        room_missing_fields = self._missing_room_booking_fields(
            pending_data=merged_for_logic,
            memory_facts=memory_facts_before,
        )

        # Prevent premature booking confirmation when essential stay details are missing.
        if room_booking_flow and self._is_confirmation_prompt_text(response_text):
            if room_missing_fields:
                effective_intent = IntentType.TABLE_BOOKING
                effective_confidence = max(effective_confidence, 0.9)
                next_state = ConversationState.AWAITING_INFO
                pending_action_target = "collect_room_booking_details"
                pending_data_target = dict(merged_for_logic)
                clear_pending_data = False
                response_text = self._build_room_booking_missing_details_prompt(
                    missing_fields=room_missing_fields,
                    pending_data=pending_data_target,
                )

        # If user explicitly asked for availability, do not loop on the same
        # "view room types or check availability" choice again.
        if (
            room_booking_flow
            and self._looks_like_availability_request(effective_user_message)
            and self._is_room_choice_loop_response(response_text)
        ):
            effective_intent = IntentType.TABLE_BOOKING
            effective_confidence = max(effective_confidence, 0.9)
            next_state = ConversationState.AWAITING_CONFIRMATION
            pending_action_target = "confirm_room_availability_check"
            pending_data_target = dict(merged_for_logic)
            clear_pending_data = False
            response_text = self._build_room_availability_forward_prompt(pending_data_target)

        # Room-booking flow must always end in explicit confirmation before any
        # backend ticket creation. If stay details are complete and model
        # attempts to end/forward early, convert to review+confirm step.
        room_pending_action = str(pending_action_target or current_pending_action or "").strip().lower()
        if room_booking_flow:
            if room_missing_fields:
                effective_intent = IntentType.TABLE_BOOKING
                effective_confidence = max(effective_confidence, 0.9)
                next_state = ConversationState.AWAITING_INFO
                pending_action_target = "collect_room_booking_details"
                pending_data_target = dict(merged_for_logic)
                clear_pending_data = False
                if not self._looks_like_room_options_information_query(effective_user_message):
                    response_text = self._build_room_booking_missing_details_prompt(
                        missing_fields=room_missing_fields,
                        pending_data=pending_data_target,
                    )
            elif (
                next_state != ConversationState.AWAITING_CONFIRMATION
                and not room_pending_action.startswith("confirm_")
                and effective_intent != IntentType.CONFIRMATION_YES
            ):
                effective_intent = IntentType.TABLE_BOOKING
                effective_confidence = max(effective_confidence, 0.9)
                next_state = ConversationState.AWAITING_CONFIRMATION
                pending_action_target = "confirm_room_booking"
                pending_data_target = dict(merged_for_logic)
                clear_pending_data = False
                response_text = self._build_room_booking_review_prompt(pending_data_target)
            elif (
                effective_intent == IntentType.CONFIRMATION_YES
                and room_pending_action in {"confirm_booking", "confirm_room_booking", "confirm_room_availability_check"}
                and self._looks_like_phase_unavailable_response(response_text)
            ):
                # Keep confirmation deterministic for completed room-booking flows.
                effective_confidence = max(effective_confidence, 0.95)
                next_state = ConversationState.COMPLETED
                pending_action_target = None
                pending_data_target = {}
                clear_pending_data = True
                response_text = self._build_room_booking_confirmation_success_response(merged_for_logic)

        order_flow = self._is_order_flow(
            raw_intent=llm_result.raw_intent,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            pending_data=merged_for_logic,
            response_text=response_text,
            intent=effective_intent,
        )

        if order_flow:
            item_name = self._extract_order_item_name(merged_for_logic)
            quantity = self._extract_order_quantity_value(merged_for_logic)
            addons_choice = self._extract_order_addons_choice(merged_for_logic)
            pending_lower = str(pending_action_target or current_pending_action or "").strip().lower()
            user_binary = self._detect_simple_binary_reply(effective_user_message)
            user_msg = str(effective_user_message or "").strip()
            order_category_hint = self._derive_order_category_hint(effective_user_message, item_name)
            order_options_followup = self._is_order_options_followup(
                message=effective_user_message,
                item_name=item_name,
                current_pending_action=pending_lower,
            )
            if item_name and pending_lower in {
                "order_food",
                "collect_order_item",
            }:
                disambiguation_response = await self._build_order_item_disambiguation_response(
                    item_name=item_name,
                    capabilities_summary=capabilities_summary,
                    db_session=db_session,
                    hotel_code=context.hotel_code,
                    selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
                    selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
                )
                if disambiguation_response:
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_SELECTION
                    pending_action_target = "collect_order_item"
                    pending_data_target = dict(merged_for_logic)
                    for key in ("order_item", "order_quantity", "order_offer_done", "order_addons_choice"):
                        pending_data_target.pop(key, None)
                    option_candidates = self._extract_order_option_candidates_from_response(disambiguation_response)
                    if option_candidates:
                        pending_data_target["order_option_candidates"] = option_candidates
                    else:
                        pending_data_target.pop("order_option_candidates", None)
                    clear_pending_data = False
                    response_text = disambiguation_response
                    item_name = ""
                    quantity = None
                    addons_choice = ""
                    order_options_followup = False

            if order_options_followup and pending_lower in {"order_food", "collect_order_item", "collect_order_quantity"}:
                options_response = await self._build_order_options_list_response(
                    category_hint=order_category_hint or item_name,
                    capabilities_summary=capabilities_summary,
                    db_session=db_session,
                    hotel_code=context.hotel_code,
                    selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
                    selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
                )
                if options_response:
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_SELECTION
                    pending_action_target = "collect_order_item"
                    pending_data_target = dict(merged_for_logic)
                    pending_data_target.pop("order_quantity", None)
                    option_candidates = self._extract_order_option_candidates_from_response(options_response)
                    if option_candidates:
                        pending_data_target["order_option_candidates"] = option_candidates
                    else:
                        pending_data_target.pop("order_option_candidates", None)
                    clear_pending_data = False
                    response_text = options_response

            if pending_lower == "collect_order_item" and item_name and not order_options_followup:
                effective_intent = IntentType.ORDER_FOOD
                effective_confidence = max(effective_confidence, 0.9)
                next_state = ConversationState.AWAITING_INFO
                pending_action_target = "collect_order_quantity"
                merged_for_logic["order_item"] = item_name
                merged_for_logic.pop("order_option_candidates", None)
                pending_data_target = dict(merged_for_logic)
                clear_pending_data = False
                if quantity is None:
                    response_text = self._build_order_quantity_prompt(item_name)
                else:
                    merged_for_logic["order_offer_done"] = True
                    pending_data_target = dict(merged_for_logic)
                    pending_action_target = "collect_order_addons"
                    response_text = self._build_order_addons_prompt(item_name, quantity)

            if pending_lower == "collect_order_quantity":
                if order_options_followup:
                    options_response = await self._build_order_options_list_response(
                        category_hint=order_category_hint or item_name,
                        capabilities_summary=capabilities_summary,
                        db_session=db_session,
                        hotel_code=context.hotel_code,
                        selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
                        selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
                    )
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_item"
                    pending_data_target = dict(merged_for_logic)
                    pending_data_target.pop("order_quantity", None)
                    option_candidates = self._extract_order_option_candidates_from_response(options_response)
                    if option_candidates:
                        pending_data_target["order_option_candidates"] = option_candidates
                    else:
                        pending_data_target.pop("order_option_candidates", None)
                    clear_pending_data = False
                    if options_response:
                        response_text = options_response
                    else:
                        hint_text = str(order_category_hint or item_name or "").strip()
                        if hint_text:
                            response_text = (
                                f"Please choose a specific {hint_text} from the menu, and I will place it for you."
                            )
                        else:
                            response_text = "Please choose a specific dish from the menu, and I will place it for you."
                elif quantity is None:
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_quantity"
                    pending_data_target = dict(merged_for_logic)
                    clear_pending_data = False
                    response_text = self._build_order_quantity_prompt(item_name)
                else:
                    merged_for_logic["order_offer_done"] = True
                    pending_data_target = dict(merged_for_logic)
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_addons"
                    response_text = self._build_order_addons_prompt(item_name, quantity)

            elif pending_lower == "collect_order_addons":
                if user_binary == IntentType.CONFIRMATION_NO:
                    merged_for_logic["order_addons_choice"] = "no"
                    pending_data_target = dict(merged_for_logic)
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_CONFIRMATION
                    pending_action_target = "confirm_order"
                    response_text = self._build_order_review_prompt(pending_data_target)
                elif user_binary == IntentType.CONFIRMATION_YES:
                    extra = self._strip_leading_yes(user_msg)
                    if extra:
                        merged_for_logic["order_additional_request"] = extra
                        merged_for_logic["order_addons_choice"] = "yes"
                        pending_data_target = dict(merged_for_logic)
                        effective_intent = IntentType.ORDER_FOOD
                        effective_confidence = max(effective_confidence, 0.9)
                        next_state = ConversationState.AWAITING_CONFIRMATION
                        pending_action_target = "confirm_order"
                        response_text = self._build_order_review_prompt(pending_data_target)
                    else:
                        effective_intent = IntentType.ORDER_FOOD
                        effective_confidence = max(effective_confidence, 0.9)
                        next_state = ConversationState.AWAITING_INFO
                        pending_action_target = "collect_order_addons"
                        pending_data_target = dict(merged_for_logic)
                        response_text = "Please tell me what else you'd like to add (for example, a drink or another dish), or type 'no'."
                else:
                    if user_msg:
                        merged_for_logic["order_additional_request"] = user_msg
                        merged_for_logic["order_addons_choice"] = "yes"
                        pending_data_target = dict(merged_for_logic)
                        effective_intent = IntentType.ORDER_FOOD
                        effective_confidence = max(effective_confidence, 0.9)
                        next_state = ConversationState.AWAITING_CONFIRMATION
                        pending_action_target = "confirm_order"
                        response_text = self._build_order_review_prompt(pending_data_target)

            if next_state == ConversationState.AWAITING_CONFIRMATION or self._is_confirmation_prompt_text(response_text):
                if not item_name:
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_item"
                    pending_data_target = dict(merged_for_logic)
                    clear_pending_data = False
                    response_text = "Please tell me the dish name you want to order."
                elif quantity is None:
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_quantity"
                    pending_data_target = dict(merged_for_logic)
                    clear_pending_data = False
                    response_text = self._build_order_quantity_prompt(item_name)
                elif not bool(merged_for_logic.get("order_offer_done")):
                    merged_for_logic["order_offer_done"] = True
                    pending_data_target = dict(merged_for_logic)
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_addons"
                    clear_pending_data = False
                    response_text = self._build_order_addons_prompt(item_name, quantity)
                elif addons_choice == "":
                    effective_intent = IntentType.ORDER_FOOD
                    effective_confidence = max(effective_confidence, 0.9)
                    next_state = ConversationState.AWAITING_INFO
                    pending_action_target = "collect_order_addons"
                    pending_data_target = dict(merged_for_logic)
                    clear_pending_data = False
                    response_text = self._build_order_addons_prompt(item_name, quantity)

        # Normalize final confirmation steps:
        # - use an explicit confirm_* pending action
        # - always ask user to type the strict confirmation phrase
        if next_state == ConversationState.AWAITING_CONFIRMATION:
            pending_action_target = self._normalize_confirm_pending_action(
                pending_action_target=pending_action_target,
                intent=effective_intent,
                room_booking_flow=room_booking_flow,
            )
            if strict_confirmation_enabled:
                response_text = self._ensure_explicit_confirmation_instruction(
                    response_text=response_text,
                    confirmation_phrase=required_confirmation_phrase,
                )

        (
            response_text,
            full_kb_validator_replaced,
            full_kb_validation_issue_codes,
            full_kb_capability_allowed,
        ) = await self._apply_full_kb_response_validation(
            response_text=response_text,
            effective_intent=effective_intent,
            effective_confidence=effective_confidence,
            context=context,
            capabilities_summary=capabilities_summary,
            request_message=effective_user_message,
        )

        transaction_ticket_meta = await self._maybe_create_full_kb_transaction_ticket(
            context=context,
            capabilities_summary=capabilities_summary,
            current_pending_action=current_pending_action,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_data_snapshot=merged_for_logic,
            response_text=response_text,
        )
        plugin_ticket_meta: dict[str, Any] = {}
        if not bool(transaction_ticket_meta.get("ticket_created")):
            plugin_ticket_meta = await self._maybe_create_full_kb_plugin_ticket(
                request=request,
                context=context,
                capabilities_summary=capabilities_summary,
                llm_result=llm_result,
                effective_intent=effective_intent,
                next_state=next_state,
                current_pending_action=current_pending_action,
                pending_data_snapshot=merged_for_logic,
                response_text=response_text,
            )
        combined_ticket_meta: dict[str, Any] = {}
        if transaction_ticket_meta:
            combined_ticket_meta.update(transaction_ticket_meta)
        if plugin_ticket_meta:
            combined_ticket_meta.update(plugin_ticket_meta)
        combined_ticket_meta = self._apply_ticket_metadata_contract(combined_ticket_meta)
        (
            pending_action_target,
            pending_data_target,
            next_state,
            clear_pending_data,
        ) = self._apply_full_kb_plugin_ticket_followup_state(
            combined_ticket_meta=combined_ticket_meta,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            pending_data_target=pending_data_target,
            internal_pending_entries=internal_pending_entries,
            next_state=next_state,
            clear_pending_data=clear_pending_data,
        )
        response_text = self._maybe_add_missing_room_number_prompt(
            response_text=response_text,
            ticket_meta=combined_ticket_meta,
            context=context,
            pending_data=merged_for_logic,
            entities=llm_entity_map,
        )

        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        context.pending_action = pending_action_target
        if clear_pending_data:
            merged_public_pending: dict[str, Any] = {}
        elif pending_data_target:
            merged_public_pending = dict(existing_public_pending)
            merged_public_pending.update(pending_data_target)
        else:
            merged_public_pending = dict(existing_public_pending)
        context.pending_data = conversation_memory_service.merge_with_internal(
            merged_public_pending,
            internal_pending_entries,
        )

        if context.pending_action is None and next_state not in (
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
            ConversationState.PROCESSING_ORDER,
        ):
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )
            context.pending_data.pop("_clarification_attempts", None)

        if next_state == ConversationState.ESCALATED:
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )

        if llm_result.room_number:
            context.room_number = str(llm_result.room_number)

        context.state = next_state

        assistant_metadata = {
            "intent": effective_intent.value,
            "confidence": effective_confidence,
            "channel": context.channel,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_trace_id": llm_result.trace_id,
            "full_kb_raw_intent": llm_result.raw_intent,
            "full_kb_normalized_query": llm_result.normalized_query,
            "response_source": "full_kb_llm",
            "full_kb_llm_passthrough_mode": llm_passthrough_mode,
            "confirmation_guard_applied": confirmation_guard_applied,
            "affirmative_selection_rewrite_applied": rewrite_applied,
            "more_options_rewrite_applied": more_options_rewrite_applied,
            "deterministic_room_updates": deterministic_room_updates,
            "deterministic_order_updates": deterministic_order_updates,
            "full_kb_validator_replaced": full_kb_validator_replaced,
            "full_kb_validation_issue_codes": full_kb_validation_issue_codes,
            "full_kb_capability_allowed": full_kb_capability_allowed,
            "response_validator_applied": True,
            "response_validator_replaced": full_kb_validator_replaced,
            "response_validator_issues": full_kb_validation_issue_codes,
        }
        if combined_ticket_meta:
            assistant_metadata.update(combined_ticket_meta)
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if llm_result.suggested_actions:
            suggested_actions = llm_result.suggested_actions
        else:
            suggested_actions = self._build_contextual_suggested_actions(
                next_state,
                effective_intent,
                context.pending_action,
                capabilities_summary,
                context.pending_data,
            )
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=effective_intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )
        if confirmation_guard_applied:
            suggested_actions = [required_confirmation_phrase, "cancel"]

        routing_path = getattr(
            getattr(routing_decision, "path", ProcessingPath.SIMPLE),
            "value",
            ProcessingPath.SIMPLE.value,
        )
        routing_score = float(getattr(routing_decision, "score", 1.0))
        routing_signals = list(getattr(routing_decision, "signals", []))

        metadata = {
            "message_count": len(context.messages),
            "entities": {
                "normalized_query": llm_result.normalized_query,
                "raw_intent": llm_result.raw_intent,
                "pending_action": context.pending_action,
                "confirmation_guard_applied": confirmation_guard_applied,
                "affirmative_selection_rewrite_applied": rewrite_applied,
                "more_options_rewrite_applied": more_options_rewrite_applied,
                "deterministic_room_updates": deterministic_room_updates,
                "deterministic_order_updates": deterministic_order_updates,
            },
            "classified_intent": effective_intent.value,
            "classified_confidence": effective_confidence,
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": "full_kb_llm",
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": llm_passthrough_mode,
                "full_kb_trace_id": llm_result.trace_id,
                "full_kb_status": llm_result.status,
                "full_kb_confirmation_guard_applied": confirmation_guard_applied,
                "full_kb_validator_replaced": full_kb_validator_replaced,
                "full_kb_validation_issue_codes": full_kb_validation_issue_codes,
                "full_kb_capability_allowed": full_kb_capability_allowed,
                "response_validator_applied": True,
                "response_validator_replaced": full_kb_validator_replaced,
                "response_validator_issues": full_kb_validation_issue_codes,
                "memory_summary": memory_snapshot.get("summary", ""),
                "memory_facts": memory_snapshot.get("facts", {}),
                "memory_recent_changes": memory_snapshot.get("recent_changes", []),
            }
        if combined_ticket_meta:
            metadata.update(combined_ticket_meta)

        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=effective_intent,
            confidence=effective_confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _maybe_handle_full_kb_ticketing_phase_gate(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        llm_result: Any,
        effective_intent: IntentType,
        effective_confidence: float,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
        db_session=None,
    ) -> ChatResponse | None:
        current_pending = pending_data if isinstance(pending_data, dict) else {}
        effective_user_message = str(request.message or "").strip()
        phase_gate_message = effective_user_message
        if context.messages:
            latest_msg = context.messages[-1]
            if getattr(latest_msg, "role", None) == MessageRole.USER:
                latest_text = str(getattr(latest_msg, "content", "") or "").strip()
                if latest_text:
                    phase_gate_message = latest_text
        message_lower = str(phase_gate_message or "").strip().lower()
        # Avoid over-gating informational/profile-style FAQ turns in full-KB mode.
        profile_memory_query = bool(
            re.search(
                r"\b(remember|call me|my room(?:\s*number)?\s*is|what room did i|which room did i|what is my room)\b",
                message_lower,
            )
        )
        has_operational_ticket_signal = (
            self._looks_like_operational_issue_for_ticketing(message_lower)
            or self._looks_like_strong_ticket_issue_marker(message_lower)
        )
        has_pending_ticket_draft = isinstance(
            current_pending.get(_INTERNAL_PENDING_TICKET_DRAFT_KEY),
            dict,
        )
        should_run_phase_gate = True
        if has_pending_ticket_draft:
            should_run_phase_gate = False
        elif str(context.pending_action or "").strip().lower() == "collect_ticket_room_number":
            should_run_phase_gate = False
        elif profile_memory_query:
            should_run_phase_gate = False
        elif effective_intent == IntentType.FAQ and "?" in message_lower:
            faq_action_like = (
                self._is_action_request_text(phase_gate_message)
                or has_operational_ticket_signal
                or self._looks_like_booking_change_request(phase_gate_message)
            )
            should_run_phase_gate = faq_action_like
        elif effective_intent == IntentType.HUMAN_REQUEST and not self._is_action_request_text(
            phase_gate_message
        ):
            should_run_phase_gate = False
        if not should_run_phase_gate:
            return None

        entity_map = entities if isinstance(entities, dict) else self._extract_full_kb_entities_for_handler(llm_result)
        phase_gate_intent = effective_intent
        if has_operational_ticket_signal and effective_intent in {
            IntentType.FAQ,
            IntentType.MENU_REQUEST,
            IntentType.HUMAN_REQUEST,
            IntentType.COMPLAINT,
        }:
            phase_gate_intent = IntentType.COMPLAINT
        contextual_sanity_issue = self._detect_contextual_sanity_issue(
            message=phase_gate_message,
            intent=phase_gate_intent,
            context=context,
            pending_data=current_pending,
            entities=entity_map,
        )
        gate_payload: dict[str, Any] | None = None
        gate_source = "full_kb_ticketing_phase_gate"
        if contextual_sanity_issue is not None:
            gate_payload = contextual_sanity_issue
            gate_source = "full_kb_contextual_sanity_gate"

        # Complaint-like turns should primarily use contextual sanity checks;
        # avoid brittle catalog mismatch gating for generic "room" wording.
        if gate_payload is None and phase_gate_intent == IntentType.COMPLAINT:
            return None

        # For human-request turns, rely on phase-intent availability checks
        # instead of lexical mismatch gating to avoid false out-of-phase blocks.
        if gate_payload is None:
            use_mismatch_gate = phase_gate_intent not in {IntentType.HUMAN_REQUEST, IntentType.COMPLAINT}
            phase_gate = None
            if use_mismatch_gate:
                phase_gate = self._detect_ticketing_phase_service_mismatch(
                    message=phase_gate_message,
                    context=context,
                    pending_data=current_pending,
                    entities=entity_map,
                )
            if phase_gate is None:
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=phase_gate_message,
                    intent=phase_gate_intent,
                    context=context,
                    pending_data=current_pending,
                    entities=entity_map,
                )
            gate_payload = phase_gate

        if gate_payload is None:
            return None

        response_text = await self._compose_policy_guardrail_response(
            user_message=phase_gate_message,
            context=context,
            capabilities_summary=capabilities_summary,
            intent=effective_intent,
            gate_payload=gate_payload,
            fallback_text="",
            response_source=gate_source,
        )
        if not response_text:
            return None
        if effective_intent == IntentType.HUMAN_REQUEST:
            handoff_line = "If you'd like, I can connect you with our staff for further help."
            if handoff_line.lower() not in response_text.lower():
                response_text = f"{response_text} {handoff_line}".strip()
        identity_match = self._match_identity_response(
            effective_user_message,
            capabilities_summary,
            context,
        )
        if identity_match is not None:
            identity_text = str(identity_match.get("response_text") or "").strip()
            if identity_text and identity_text.lower() not in response_text.lower():
                response_text = f"{identity_text} {response_text}".strip()
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        if context.pending_action:
            internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal({}, internal_pending_entries)
            context.pending_data.pop("_clarification_attempts", None)

        context.state = ConversationState.IDLE
        llm_trace_id = str(getattr(llm_result, "trace_id", "") or "")
        llm_status = str(getattr(llm_result, "status", "") or "")
        llm_normalized_query = str(getattr(llm_result, "normalized_query", "") or "")
        llm_raw_intent = str(getattr(llm_result, "raw_intent", "") or "")
        assistant_metadata = {
            "intent": effective_intent.value,
            "confidence": max(0.0, min(1.0, float(effective_confidence or 0.0))),
            "channel": context.channel,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_trace_id,
            "response_source": gate_source,
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_phase_gate": gate_source == "full_kb_ticketing_phase_gate",
            "contextual_sanity_gate": gate_source == "full_kb_contextual_sanity_gate",
            "contextual_sanity_code": str(gate_payload.get("code") or ""),
            "phase_gate_service_id": str(gate_payload.get("service_id") or ""),
            "phase_gate_service_name": str(gate_payload.get("service_name") or ""),
            "phase_gate_current_phase_id": str(gate_payload.get("current_phase_id") or ""),
            "phase_gate_current_phase_name": str(gate_payload.get("current_phase_name") or ""),
            "phase_gate_service_phase_id": str(gate_payload.get("service_phase_id") or ""),
            "phase_gate_service_phase_name": str(gate_payload.get("service_phase_name") or ""),
        }
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        gate_actions = gate_payload.get("suggested_actions")
        if isinstance(gate_actions, list):
            phase_suggestions = [str(item).strip() for item in gate_actions if str(item).strip()]
        else:
            phase_suggestions = ["Show available services", "Ask another question"]
        suggested_actions = self._finalize_user_query_suggestions(
            phase_suggestions,
            state=context.state,
            intent=effective_intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )
        routing_path = getattr(
            getattr(routing_decision, "path", ProcessingPath.SIMPLE),
            "value",
            ProcessingPath.SIMPLE.value,
        )
        routing_score = float(getattr(routing_decision, "score", 1.0))
        routing_signals = list(getattr(routing_decision, "signals", []))
        metadata = {
            "message_count": len(context.messages),
            "entities": {
                "normalized_query": llm_normalized_query,
                "raw_intent": llm_raw_intent,
                "pending_action": context.pending_action,
            },
            "classified_intent": effective_intent.value,
            "classified_confidence": max(0.0, min(1.0, float(effective_confidence or 0.0))),
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": gate_source,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_trace_id,
            "full_kb_status": llm_status,
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_phase_gate": gate_source == "full_kb_ticketing_phase_gate",
            "contextual_sanity_gate": gate_source == "full_kb_contextual_sanity_gate",
            "contextual_sanity_code": str(gate_payload.get("code") or ""),
            "phase_gate_service_id": str(gate_payload.get("service_id") or ""),
            "phase_gate_service_name": str(gate_payload.get("service_name") or ""),
            "phase_gate_current_phase_id": str(gate_payload.get("current_phase_id") or ""),
            "phase_gate_current_phase_name": str(gate_payload.get("current_phase_name") or ""),
            "phase_gate_service_phase_id": str(gate_payload.get("service_phase_id") or ""),
            "phase_gate_service_phase_name": str(gate_payload.get("service_phase_name") or ""),
            "phase_service_unavailable": bool(gate_payload.get("phase_service_unavailable")),
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=effective_intent,
            confidence=max(0.0, min(1.0, float(effective_confidence or 0.0))),
            state=context.state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _maybe_route_full_kb_ticketing_handler(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        llm_result: Any,
        effective_intent: IntentType,
        effective_confidence: float,
        current_pending_action: str | None,
        pending_action_target: str | None,
        rewrite_applied: bool,
        more_options_rewrite_applied: bool,
        db_session=None,
    ) -> ChatResponse | None:
        takeover_enabled = self._is_ticketing_plugin_takeover_enabled()
        llm_ticketing_preference = self._extract_full_kb_ticketing_preference(llm_result)
        llm_ticketing_reason = self._extract_full_kb_ticketing_reason(llm_result)
        entities = self._extract_full_kb_entities_for_handler(llm_result)
        current_pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        effective_user_message = str(request.message or "").strip()
        fallback_routed_intent, fallback_reason, fallback_source = self._resolve_unified_complaint_routing_intent(
            message=effective_user_message,
            effective_intent=effective_intent,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            llm_response_text=str(getattr(llm_result, "response_text", "") or ""),
            llm_ticketing_preference=llm_ticketing_preference,
            next_state=getattr(llm_result, "next_state", ConversationState.IDLE),
        )
        if not takeover_enabled and fallback_routed_intent is None:
            return None

        if takeover_enabled:
            phase_gate_response = await self._maybe_handle_full_kb_ticketing_phase_gate(
                request=request,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                llm_result=llm_result,
                effective_intent=effective_intent,
                effective_confidence=effective_confidence,
                pending_data=current_pending,
                entities=entities,
                db_session=db_session,
            )
            if phase_gate_response is not None:
                return phase_gate_response

        phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=current_pending,
            entities=entities,
        )
        ticketing_agent_decision = await ticketing_agent_service.decide_async(
            intent=effective_intent,
            message=effective_user_message,
            llm_response_text=getattr(llm_result, "response_text", ""),
            llm_ticketing_preference=llm_ticketing_preference,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            selected_phase_id=phase_context.get("selected_phase_id", ""),
            selected_phase_name=phase_context.get("selected_phase_name", ""),
            conversation_excerpt=self._build_ticketing_case_context_text(
                context=context,
                latest_user_message=effective_user_message,
                llm_response_text=getattr(llm_result, "response_text", ""),
            ),
        )
        decision_activate = bool(getattr(ticketing_agent_decision, "activate", False))
        decision_reason = str(getattr(ticketing_agent_decision, "reason", "") or "").strip()
        decision_source = str(getattr(ticketing_agent_decision, "source", "") or "").strip()
        decision_route = str(getattr(ticketing_agent_decision, "route", "") or "")
        decision_matched_case = str(getattr(ticketing_agent_decision, "matched_case", "") or "")
        routed_intent = getattr(ticketing_agent_decision, "routed_intent", None) or effective_intent

        if not decision_activate and fallback_routed_intent is not None:
            decision_activate = True
            routed_intent = fallback_routed_intent
            decision_reason = fallback_reason or decision_reason
            decision_source = fallback_source or decision_source
            if not decision_route:
                decision_route = "complaint_handler"
        elif fallback_routed_intent is not None and routed_intent not in {
            IntentType.COMPLAINT,
            IntentType.HUMAN_REQUEST,
        }:
            routed_intent = fallback_routed_intent
            decision_reason = fallback_reason or decision_reason
            decision_source = fallback_source or decision_source

        if not decision_activate:
            return None

        if not takeover_enabled and routed_intent != IntentType.COMPLAINT:
            return None

        if routed_intent == IntentType.COMPLAINT:
            entities.setdefault("ticketing_flow", True)
            entities.setdefault("ticket_route_reason", decision_reason or fallback_reason)
            entities.setdefault("ticket_route_source", decision_source or fallback_source)
        if llm_ticketing_reason:
            entities.setdefault("ticket_reason", llm_ticketing_reason)
        elif decision_reason:
            entities.setdefault("ticket_reason", decision_reason)
        intent_result = IntentResult(
            intent=routed_intent,
            confidence=max(0.0, min(1.0, float(effective_confidence or 0.0))),
            entities=entities,
            requires_confirmation=routed_intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING},
        )
        handler_result = await self._dispatch_to_handler(
            effective_user_message,
            intent_result,
            context,
            capabilities_summary,
            db_session,
        )
        if handler_result is None:
            return None

        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        next_state = handler_result.next_state
        if handler_result.pending_action is not None:
            context.pending_action = handler_result.pending_action
        if handler_result.pending_data is not None:
            context.pending_data = conversation_memory_service.merge_with_internal(
                handler_result.pending_data,
                internal_pending_entries,
            )
        if handler_result.pending_action is None and next_state not in (
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
        ):
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )
            context.pending_data.pop("_clarification_attempts", None)
        if handler_result.metadata.get("room_number"):
            context.room_number = str(handler_result.metadata["room_number"])
        if llm_result.room_number:
            context.room_number = str(llm_result.room_number)
        context.state = next_state

        assistant_metadata = {
            "intent": intent_result.intent.value,
            "confidence": intent_result.confidence,
            "channel": context.channel,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_result.trace_id,
            "full_kb_raw_intent": llm_result.raw_intent,
            "full_kb_normalized_query": llm_result.normalized_query,
            "response_source": "full_kb_llm_handler",
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_requested": llm_ticketing_preference,
            "full_kb_ticketing_reason": llm_ticketing_reason,
            "full_kb_ticketing_agent_route": decision_route,
            "full_kb_ticketing_agent_decision_reason": decision_reason,
            "full_kb_ticketing_agent_decision_source": decision_source,
            "full_kb_ticketing_agent_matched_case": decision_matched_case,
            "affirmative_selection_rewrite_applied": rewrite_applied,
            "more_options_rewrite_applied": more_options_rewrite_applied,
        }
        if handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        context.add_message(
            MessageRole.ASSISTANT,
            handler_result.response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            handler_result.response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if handler_result.suggested_actions:
            suggested_actions = handler_result.suggested_actions
        else:
            suggested_actions = self._build_contextual_suggested_actions(
                next_state,
                intent_result.intent,
                context.pending_action,
                capabilities_summary,
                context.pending_data,
            )
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=intent_result.intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        routing_path = getattr(
            getattr(routing_decision, "path", ProcessingPath.SIMPLE),
            "value",
            ProcessingPath.SIMPLE.value,
        )
        routing_score = float(getattr(routing_decision, "score", 1.0))
        routing_signals = list(getattr(routing_decision, "signals", []))
        metadata = {
            "message_count": len(context.messages),
            "entities": {
                "normalized_query": llm_result.normalized_query,
                "raw_intent": llm_result.raw_intent,
                "pending_action": context.pending_action,
            },
            "classified_intent": intent_result.intent.value,
            "classified_confidence": intent_result.confidence,
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": "full_kb_llm_handler",
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_result.trace_id,
            "full_kb_status": llm_result.status,
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_requested": llm_ticketing_preference,
            "full_kb_ticketing_reason": llm_ticketing_reason,
            "full_kb_ticketing_agent_route": decision_route,
            "full_kb_ticketing_agent_decision_reason": decision_reason,
            "full_kb_ticketing_agent_decision_source": decision_source,
            "full_kb_ticketing_agent_matched_case": decision_matched_case,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
            "routed_handler": intent_result.intent.value,
        }
        if handler_result.metadata:
            metadata.update(handler_result.metadata)

        return ChatResponse(
            session_id=request.session_id,
            message=handler_result.response_text,
            intent=intent_result.intent,
            confidence=intent_result.confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    def _match_greeting_response(
        self,
        message: str,
        capabilities_summary: dict[str, Any],
        context: ConversationContext,
    ) -> Optional[dict]:
        """
        Deterministically handle short greeting turns in full-KB mode so they
        do not depend on model variability.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg = str(message or "").strip().lower()
        if not msg:
            return None
        compact = re.sub(r"[^a-z0-9 ]+", " ", msg)
        compact = re.sub(r"\s+", " ", compact).strip()
        tokens = compact.split()
        if not tokens:
            return None
        if len(tokens) > 3:
            return None

        greeting_tokens = {
            "hi",
            "hii",
            "hiii",
            "hello",
            "hey",
            "heyy",
            "yo",
            "hola",
            "namaste",
            "good morning",
            "good afternoon",
            "good evening",
        }
        is_greeting = compact in greeting_tokens or any(token in {"hi", "hello", "hey", "hola", "namaste"} for token in tokens)
        if not is_greeting:
            return None

        welcome_message = str(
            capabilities_summary.get("welcome_message")
            or config_service.get_welcome_message()
            or "Welcome. How may I assist you today?"
        ).strip()
        return {
            "match_type": "greeting",
            "response_text": welcome_message,
        }

    @staticmethod
    def _is_ticketing_pending_action(action: str | None) -> bool:
        normalized = str(action or "").strip().lower()
        return normalized in _TICKETING_PENDING_ACTIONS

    @staticmethod
    def _is_order_pending_action(action: str | None) -> bool:
        normalized = str(action or "").strip().lower()
        return normalized in {
            "order_food",
            "collect_order_item",
            "collect_order_quantity",
            "collect_order_addons",
            "confirm_order",
        }

    @staticmethod
    def _looks_like_strong_ticket_issue_marker(msg_lower: str) -> bool:
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
            "billing issue",
            "wrong charge",
            "human agent",
            "talk to human",
            "escalate",
            "manager",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _ticket_log_text(value: Any, *, max_chars: int = 220) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    def _log_full_kb_ticket_event(
        self,
        *,
        event: str,
        ticket_source: str,
        context: ConversationContext,
        effective_intent: IntentType | None = None,
        next_state: ConversationState | None = None,
        pending_action: str | None = None,
        reason: str = "",
        matched_case: str = "",
        sub_category: str = "",
        ticket_id: str = "",
        error: str = "",
        issue: str = "",
    ) -> None:
        level = logging.INFO
        if event in {"failed"}:
            level = logging.WARNING
        if event in {"not_applicable"}:
            level = logging.DEBUG
        logger.log(
            level,
            (
                "full_kb_ticket_event event=%s source=%s session_id=%s intent=%s state=%s "
                "pending_action=%s reason=%s matched_case=%s sub_category=%s ticket_id=%s issue=%s error=%s"
            ),
            event,
            ticket_source,
            str(getattr(context, "session_id", "") or ""),
            str(getattr(effective_intent, "value", effective_intent or "") or ""),
            str(getattr(next_state, "value", next_state or "") or ""),
            str(pending_action or ""),
            self._ticket_log_text(reason),
            self._ticket_log_text(matched_case),
            self._ticket_log_text(sub_category),
            self._ticket_log_text(ticket_id),
            self._ticket_log_text(issue),
            self._ticket_log_text(error),
        )

    async def _maybe_create_full_kb_transaction_ticket(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        current_pending_action: str | None,
        effective_intent: IntentType,
        next_state: ConversationState,
        pending_data_snapshot: dict[str, Any] | None,
        response_text: str,
    ) -> dict[str, Any]:
        """
        Create Lumira-style backend ticket for confirmed transactional flows in
        full-KB mode, without changing assistant response text.
        """
        ticket_source = "full_kb_llm_transaction"

        if not self._is_ticketing_plugin_enabled():
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticketing_plugin_disabled",
            )
            return {}
        pending_action = str(current_pending_action or "").strip().lower()
        if effective_intent != IntentType.CONFIRMATION_YES:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="intent_not_confirmation_yes",
            )
            return {}
        if next_state not in {ConversationState.COMPLETED, ConversationState.IDLE}:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="state_not_finalized",
            )
            return {}
        if not ticketing_service.is_ticketing_enabled(capabilities_summary):
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="ticketing_service_disabled",
            )
            return {}

        pending = pending_data_snapshot if isinstance(pending_data_snapshot, dict) else {}
        if str(pending.get("ticket_id") or "").strip():
            # Avoid duplicating when some upstream path already created one.
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="ticket_already_present_in_pending_data",
            )
            return {}

        is_order_confirmation = pending_action == "confirm_order"
        is_booking_confirmation = pending_action in {
            "confirm_booking",
            "confirm_room_booking",
            "confirm_room_availability_check",
        } or self._looks_like_booking_confirmation_response(response_text)

        if not is_order_confirmation and not is_booking_confirmation:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="pending_action_not_transaction_confirmation",
            )
            return {}

        resolved_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending,
            entities={},
        )
        if is_order_confirmation:
            issue = self._build_order_ticket_issue_from_pending(pending, response_text=response_text)
            phase = resolved_phase or "during_stay"
            sub_category = "order_food"
        else:
            pre_sub_category = self._infer_transaction_booking_sub_category(
                pending_data=pending,
                response_text=response_text,
            )
            issue = self._build_booking_ticket_issue_from_pending(
                pending,
                response_text=response_text,
                sub_category_override=pre_sub_category,
            )
            phase = resolved_phase or "pre_checkin"
            sub_category = pre_sub_category

        if not issue:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="empty_transaction_issue",
            )
            return {}

        skip_phase_gate_for_room_booking_confirmation = (
            is_booking_confirmation
            and str(sub_category or "").strip().lower() == "room_booking"
        )
        phase_gate = None
        if not skip_phase_gate_for_room_booking_confirmation:
            phase_gate = self._detect_ticketing_phase_service_mismatch(
                message=issue,
                context=context,
                pending_data=pending,
                entities={},
            )
            if phase_gate is None:
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=issue,
                    intent=effective_intent,
                    context=context,
                    pending_data=pending,
                    entities={},
                )
        if phase_gate is not None:
            skip_reason = (
                "phase_service_unavailable"
                if bool(phase_gate.get("phase_service_unavailable"))
                else "phase_service_mismatch"
            )
            self._log_full_kb_ticket_event(
                event="skipped",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason=skip_reason,
                sub_category=sub_category,
                issue=issue,
            )
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": skip_reason,
                "ticket_source": ticket_source,
                "full_kb_ticketing_phase_gate": True,
                "phase_gate_response_text": str(phase_gate.get("response_text") or ""),
                "phase_gate_service_id": str(phase_gate.get("service_id") or ""),
                "phase_gate_service_name": str(phase_gate.get("service_name") or ""),
                "phase_gate_current_phase_id": str(phase_gate.get("current_phase_id") or ""),
                "phase_gate_current_phase_name": str(phase_gate.get("current_phase_name") or ""),
                "phase_gate_service_phase_id": str(phase_gate.get("service_phase_id") or ""),
                "phase_gate_service_phase_name": str(phase_gate.get("service_phase_name") or ""),
                "phase_service_unavailable": bool(phase_gate.get("phase_service_unavailable")),
            }

        ticketing_toggle_gate = self._detect_ticketing_phase_service_ticketing_disabled(
            message=issue,
            context=context,
            pending_data=pending,
            entities={},
        )
        if ticketing_toggle_gate is not None:
            self._log_full_kb_ticket_event(
                event="skipped",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="phase_service_ticketing_disabled",
                sub_category=sub_category,
                issue=issue,
            )
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "phase_service_ticketing_disabled",
                "ticket_source": ticket_source,
                "full_kb_ticketing_phase_gate": True,
                "phase_gate_response_text": str(ticketing_toggle_gate.get("response_text") or ""),
                "phase_gate_service_id": str(ticketing_toggle_gate.get("service_id") or ""),
                "phase_gate_service_name": str(ticketing_toggle_gate.get("service_name") or ""),
                "phase_gate_current_phase_id": str(ticketing_toggle_gate.get("current_phase_id") or ""),
                "phase_gate_current_phase_name": str(ticketing_toggle_gate.get("current_phase_name") or ""),
                "phase_gate_service_phase_id": str(ticketing_toggle_gate.get("service_phase_id") or ""),
                "phase_gate_service_phase_name": str(ticketing_toggle_gate.get("service_phase_name") or ""),
            }

        configured_cases = ticketing_agent_service.get_configured_cases()
        matched_case = ""
        if configured_cases:
            phase_context = self._get_selected_phase_context(
                context=context,
                pending_data=pending,
                entities={},
            )
            matched_case = await ticketing_agent_service.match_configured_case_async(
                message=issue,
                conversation_excerpt=self._build_ticketing_case_context_text(
                    context=context,
                    latest_user_message=issue,
                    llm_response_text=response_text,
                ),
                llm_response_text=response_text,
                selected_phase_id=phase_context.get("selected_phase_id", ""),
                selected_phase_name=phase_context.get("selected_phase_name", ""),
            )
        if not matched_case:
            matched_case = "generalized_unconfigured_transaction_fallback"
        if is_booking_confirmation:
            sub_category = self._infer_transaction_booking_sub_category(
                pending_data=pending,
                response_text=response_text,
                matched_case=matched_case,
            )
            if not sub_category:
                sub_category = "booking"
            issue = self._build_booking_ticket_issue_from_pending(
                pending,
                response_text=response_text,
                sub_category_override=sub_category,
            )
            if not issue:
                issue = str(response_text or "").strip() or "Booking confirmed."

        self._log_full_kb_ticket_event(
            event="attempted",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            issue=issue,
        )
        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category="request",
                sub_category=sub_category,
                priority="medium",
                phase=phase,
                source=str(pending.get("ticket_source") or ""),
                group_id=pending.get("group_id"),
                message_id=pending.get("message_id"),
                input_tokens=pending.get("input_tokens"),
                output_tokens=pending.get("output_tokens"),
                total_tokens=pending.get("total_tokens"),
                cost=pending.get("cost"),
            )
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(exc),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(exc),
                "ticket_source": ticket_source,
            }

        if not create_result.success:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(create_result.error or "ticket_create_failed"),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": ticket_source,
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        created_ticket_id = str(create_result.ticket_id or "").strip()
        self._log_full_kb_ticket_event(
            event="created",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            ticket_id=created_ticket_id,
            issue=issue,
        )
        return {
            "ticket_created": True,
            "ticket_id": created_ticket_id,
            "ticket_status": "open",
            "ticket_category": "request",
            "ticket_sub_category": sub_category,
            "ticket_priority": "medium",
            "ticket_summary": issue[:180],
            "ticket_source": ticket_source,
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
        }

    @staticmethod
    def _is_ticketing_plugin_enabled() -> bool:
        return bool(getattr(settings, "ticketing_plugin_enabled", True))

    @staticmethod
    def _is_ticketing_plugin_takeover_enabled() -> bool:
        return (
            bool(getattr(settings, "ticketing_plugin_enabled", True))
            and bool(getattr(settings, "ticketing_plugin_takeover_mode", False))
        )

    async def _maybe_create_full_kb_plugin_ticket(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_result: Any,
        effective_intent: IntentType,
        next_state: ConversationState,
        current_pending_action: str | None,
        pending_data_snapshot: dict[str, Any] | None,
        response_text: str,
    ) -> dict[str, Any]:
        """
        Plugin ticketing path: create backend ticket without altering user-facing text.
        """
        ticket_source = "full_kb_llm_plugin"
        if not self._is_ticketing_plugin_enabled():
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticketing_plugin_disabled",
            )
            return {}
        if not ticketing_service.is_ticketing_enabled(capabilities_summary):
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticketing_service_disabled",
            )
            return {}

        msg = str(request.message or "").strip()
        msg_lower = msg.lower()
        if not msg_lower:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="empty_user_message",
            )
            return {}
        pending = pending_data_snapshot if isinstance(pending_data_snapshot, dict) else {}
        pending_ticket_draft = self._get_pending_plugin_ticket_draft(
            pending_data_snapshot=pending,
            context=context,
        )
        room_number_only_reply = self._looks_like_room_number_reply(msg_lower)
        llm_ticketing_preference = self._extract_full_kb_ticketing_preference(llm_result)
        phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        ticketing_agent_decision = await ticketing_agent_service.decide_async(
            intent=effective_intent,
            message=msg,
            llm_response_text=response_text,
            llm_ticketing_preference=llm_ticketing_preference,
            current_pending_action=current_pending_action,
            pending_action_target=str(getattr(llm_result, "pending_action", "") or ""),
            selected_phase_id=phase_context.get("selected_phase_id", ""),
            selected_phase_name=phase_context.get("selected_phase_name", ""),
            conversation_excerpt=self._build_ticketing_case_context_text(
                context=context,
                latest_user_message=msg,
                llm_response_text=response_text,
            ),
        )
        matched_case = str(getattr(ticketing_agent_decision, "matched_case", "") or "").strip()
        decision_activate = bool(getattr(ticketing_agent_decision, "activate", False))
        decision_routed_intent = getattr(ticketing_agent_decision, "routed_intent", None)
        decision_reason = str(getattr(ticketing_agent_decision, "reason", "") or "").strip()
        decision_source = str(getattr(ticketing_agent_decision, "source", "") or "").strip()

        if (
            effective_intent in {
            IntentType.GREETING,
            IntentType.CONFIRMATION_YES,
            IntentType.CONFIRMATION_NO,
            IntentType.UNCLEAR,
            }
            and not decision_activate
        ):
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="intent_not_eligible_for_plugin_ticket",
                matched_case=matched_case,
            )
            return {}
        if effective_intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING}:
            # For booking/order intents, create ticket only on explicit complaint/escalation
            # asks. Normal transactional flow must wait for confirmation path.
            transactional_escalation_signal = (
                self._looks_like_ticketing_request(msg_lower)
                or self._looks_like_strong_ticket_issue_marker(msg_lower)
            )
            if not transactional_escalation_signal and not pending_ticket_draft:
                self._log_full_kb_ticket_event(
                    event="not_applicable",
                    ticket_source=ticket_source,
                    context=context,
                    effective_intent=effective_intent,
                    next_state=next_state,
                    pending_action=current_pending_action,
                    reason="transactional_intent_deferred_to_confirmation_path",
                    matched_case=matched_case,
                )
                return {}
        if room_number_only_reply and not pending_ticket_draft:
            # Slot-detail only message; do not create a new ticket.
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="room_number_only_reply",
            )
            return {}

        if str(pending.get("ticket_id") or "").strip():
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticket_already_present_in_pending_data",
            )
            return {}
        if pending_ticket_draft and room_number_only_reply:
            decision_activate = True
            draft_routed_intent_raw = str(pending_ticket_draft.get("routed_intent") or "").strip().lower()
            if draft_routed_intent_raw == IntentType.HUMAN_REQUEST.value:
                decision_routed_intent = IntentType.HUMAN_REQUEST
            else:
                decision_routed_intent = IntentType.COMPLAINT
            draft_case = str(pending_ticket_draft.get("matched_case") or "").strip()
            if draft_case:
                matched_case = draft_case
            decision_reason = "resume_pending_ticket_draft"
            decision_source = "chat_service_pending_ticket_draft"
        if not decision_activate:
            fallback_routed_intent = self._infer_plugin_ticket_fallback_intent(
                message=msg,
                effective_intent=effective_intent,
                next_state=next_state,
            )
            if fallback_routed_intent is not None:
                decision_activate = True
                decision_routed_intent = fallback_routed_intent
                decision_reason = "generalized_operational_ticket_fallback"
                decision_source = "chat_service_fallback"
        if not decision_activate:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason=decision_reason or "ticketing_agent_not_activated",
                matched_case=matched_case,
            )
            return {}
        if decision_routed_intent not in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason=f"unsupported_routed_intent:{decision_routed_intent}",
                matched_case=matched_case,
            )
            return {}

        entities = self._extract_full_kb_entities_for_handler(llm_result)
        if decision_routed_intent == IntentType.HUMAN_REQUEST:
            category = "request"
            sub_category = "human_handoff"
            priority = "high"
        else:
            category = self._derive_plugin_ticket_category(
                effective_intent=effective_intent,
                entities=entities,
                message=msg,
            )
            sub_category = self._infer_plugin_ticket_sub_category(
                effective_intent=effective_intent,
                entities=entities,
                message=msg,
                matched_case=matched_case,
            )
            priority = self._derive_plugin_ticket_priority(
                entities=entities,
                message=msg,
            )
        if pending_ticket_draft:
            category = str(pending_ticket_draft.get("category") or category).strip() or category
            sub_category = str(pending_ticket_draft.get("sub_category") or sub_category).strip() or sub_category
            priority = str(pending_ticket_draft.get("priority") or priority).strip() or priority
        resolved_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending,
            entities=entities,
        )
        phase = str(
            entities.get("phase")
            or pending.get("phase")
            or (pending_ticket_draft or {}).get("phase")
            or resolved_phase
            or "during_stay"
        ).strip() or "during_stay"

        issue = str(
            entities.get("issue")
            or pending.get("issue")
            or (pending_ticket_draft or {}).get("issue")
            or llm_result.normalized_query
            or msg
        ).strip()
        if not issue:
            issue = msg
        if not issue:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="empty_issue",
                matched_case=matched_case,
            )
            return {}

        bypass_service_phase_gates = decision_routed_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}
        if not bypass_service_phase_gates:
            phase_gate = self._detect_ticketing_phase_service_mismatch(
                message=msg or issue,
                context=context,
                pending_data=pending,
                entities=entities,
            )
            if phase_gate is None:
                phase_gate_intent = decision_routed_intent or effective_intent
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=msg or issue,
                    intent=phase_gate_intent,
                    context=context,
                    pending_data=pending,
                    entities=entities,
                )
            if phase_gate is not None:
                skip_reason = (
                    "phase_service_unavailable"
                    if bool(phase_gate.get("phase_service_unavailable"))
                    else "phase_service_mismatch"
                )
                self._log_full_kb_ticket_event(
                    event="skipped",
                    ticket_source=ticket_source,
                    context=context,
                    effective_intent=effective_intent,
                    next_state=next_state,
                    pending_action=current_pending_action,
                    reason=skip_reason,
                    matched_case=matched_case,
                    sub_category=sub_category,
                    issue=issue,
                )
                return {
                    "ticket_create_skipped": True,
                    "ticket_create_skip_reason": skip_reason,
                    "ticket_source": ticket_source,
                    "full_kb_ticketing_phase_gate": True,
                    "phase_gate_response_text": str(phase_gate.get("response_text") or ""),
                    "phase_gate_service_id": str(phase_gate.get("service_id") or ""),
                    "phase_gate_service_name": str(phase_gate.get("service_name") or ""),
                    "phase_gate_current_phase_id": str(phase_gate.get("current_phase_id") or ""),
                    "phase_gate_current_phase_name": str(phase_gate.get("current_phase_name") or ""),
                    "phase_gate_service_phase_id": str(phase_gate.get("service_phase_id") or ""),
                    "phase_gate_service_phase_name": str(phase_gate.get("service_phase_name") or ""),
                    "phase_service_unavailable": bool(phase_gate.get("phase_service_unavailable")),
                    "full_kb_ticketing_agent_decision_reason": decision_reason,
                    "full_kb_ticketing_agent_decision_source": decision_source,
                    "full_kb_ticketing_agent_matched_case": matched_case,
                }

            ticketing_toggle_gate = self._detect_ticketing_phase_service_ticketing_disabled(
                message=msg or issue,
                context=context,
                pending_data=pending,
                entities=entities,
            )
            if ticketing_toggle_gate is not None:
                self._log_full_kb_ticket_event(
                    event="skipped",
                    ticket_source=ticket_source,
                    context=context,
                    effective_intent=effective_intent,
                    next_state=next_state,
                    pending_action=current_pending_action,
                    reason="phase_service_ticketing_disabled",
                    matched_case=matched_case,
                    sub_category=sub_category,
                    issue=issue,
                )
                return {
                    "ticket_create_skipped": True,
                    "ticket_create_skip_reason": "phase_service_ticketing_disabled",
                    "ticket_source": ticket_source,
                    "full_kb_ticketing_phase_gate": True,
                    "phase_gate_response_text": str(ticketing_toggle_gate.get("response_text") or ""),
                    "phase_gate_service_id": str(ticketing_toggle_gate.get("service_id") or ""),
                    "phase_gate_service_name": str(ticketing_toggle_gate.get("service_name") or ""),
                    "phase_gate_current_phase_id": str(ticketing_toggle_gate.get("current_phase_id") or ""),
                    "phase_gate_current_phase_name": str(ticketing_toggle_gate.get("current_phase_name") or ""),
                    "phase_gate_service_phase_id": str(ticketing_toggle_gate.get("service_phase_id") or ""),
                    "phase_gate_service_phase_name": str(ticketing_toggle_gate.get("service_phase_name") or ""),
                    "full_kb_ticketing_agent_decision_reason": decision_reason,
                    "full_kb_ticketing_agent_decision_source": decision_source,
                    "full_kb_ticketing_agent_matched_case": matched_case,
                }

        room_number = (
            str(llm_result.room_number or "").strip()
            or str(context.room_number or "").strip()
            or str(pending.get("room_number") or "").strip()
            or str((pending_ticket_draft or {}).get("room_number") or "").strip()
            or self._extract_room_number_from_text(msg)
        )
        missing_required_fields = self._missing_plugin_ticket_details(
            phase=phase,
            room_number=room_number,
            issue=issue,
            context=context,
            pending=pending,
            entities=entities,
        )
        if missing_required_fields:
            pending_draft = self._build_pending_plugin_ticket_draft(
                context=context,
                issue=issue,
                category=category,
                sub_category=sub_category,
                priority=priority,
                phase=phase,
                matched_case=matched_case,
                routed_intent=decision_routed_intent,
                pending=pending,
                entities=entities,
                existing_draft=pending_ticket_draft,
            )
            self._log_full_kb_ticket_event(
                event="skipped",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="missing_required_details",
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
            )
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "missing_required_details",
                "ticket_missing_fields": missing_required_fields,
                "pending_ticket_draft": pending_draft,
                "ticket_pending_action": "collect_ticket_room_number"
                if "room_number" in {str(field).strip().lower() for field in missing_required_fields}
                else "collect_ticket_issue_details",
                "ticket_source": ticket_source,
                "full_kb_ticketing_agent_decision_reason": decision_reason,
                "full_kb_ticketing_agent_decision_source": decision_source,
                "full_kb_ticketing_agent_matched_case": matched_case,
            }

        self._log_full_kb_ticket_event(
            event="attempted",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=current_pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            issue=issue,
        )
        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category=category,
                sub_category=sub_category,
                priority=priority,
                phase=phase,
                source=str(pending.get("ticket_source") or ""),
                group_id=pending.get("group_id"),
                message_id=pending.get("message_id"),
                input_tokens=pending.get("input_tokens"),
                output_tokens=pending.get("output_tokens"),
                total_tokens=pending.get("total_tokens"),
                cost=pending.get("cost"),
            )
            if room_number:
                payload["room_number"] = room_number
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(exc),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(exc),
                "ticket_source": ticket_source,
            }

        if not create_result.success:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(create_result.error or "ticket_create_failed"),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": ticket_source,
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        created_ticket_id = str(create_result.ticket_id or "").strip()
        self._log_full_kb_ticket_event(
            event="created",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=current_pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            ticket_id=created_ticket_id,
            issue=issue,
        )
        return {
            "ticket_created": True,
            "ticket_id": created_ticket_id,
            "ticket_status": "open",
            "ticket_category": category,
            "ticket_sub_category": sub_category,
            "ticket_priority": priority,
            "ticket_summary": issue[:180],
            "ticket_source": ticket_source,
            "room_number": room_number,
            "ticket_resumed_from_pending_draft": bool(pending_ticket_draft and room_number_only_reply),
            "clear_pending_ticket_draft": True,
            "full_kb_ticketing_agent_decision_reason": decision_reason,
            "full_kb_ticketing_agent_decision_source": decision_source,
            "full_kb_ticketing_agent_matched_case": matched_case,
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
        }

    @staticmethod
    def _extract_room_number_from_text(message: str) -> str:
        if not message:
            return ""
        match = re.search(
            r"\b(?:room(?:\s*(?:number|no\.?))?\s*(?:is|=|:)?\s*)?([A-Za-z0-9-]{2,10})\b",
            str(message),
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        candidate = str(match.group(1) or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{2,10}", candidate):
            return ""
        if not any(ch.isdigit() for ch in candidate):
            return ""
        return candidate.upper()

    def _get_pending_plugin_ticket_draft(
        self,
        *,
        pending_data_snapshot: dict[str, Any] | None,
        context: ConversationContext,
    ) -> dict[str, Any] | None:
        snapshot = pending_data_snapshot if isinstance(pending_data_snapshot, dict) else {}
        context_pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        draft_candidates = (
            snapshot.get(_INTERNAL_PENDING_TICKET_DRAFT_KEY),
            snapshot.get("pending_ticket_draft"),
            context_pending.get(_INTERNAL_PENDING_TICKET_DRAFT_KEY),
            context_pending.get("pending_ticket_draft"),
        )
        for candidate in draft_candidates:
            if isinstance(candidate, dict) and candidate:
                return dict(candidate)
        return None

    def _build_pending_plugin_ticket_draft(
        self,
        *,
        context: ConversationContext,
        issue: str,
        category: str,
        sub_category: str,
        priority: str,
        phase: str,
        matched_case: str,
        routed_intent: IntentType | None,
        pending: dict[str, Any],
        entities: dict[str, Any],
        existing_draft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        draft = dict(existing_draft or {})
        draft.update(
            {
                "issue": str(issue or "").strip(),
                "category": str(category or "").strip().lower() or "request",
                "sub_category": str(sub_category or "").strip() or "general_request",
                "priority": str(priority or "").strip().lower() or "medium",
                "phase": str(phase or "").strip() or "during_stay",
                "matched_case": str(matched_case or draft.get("matched_case") or "").strip(),
                "routed_intent": (
                    routed_intent.value
                    if isinstance(routed_intent, IntentType)
                    else str(draft.get("routed_intent") or IntentType.COMPLAINT.value).strip().lower()
                ),
            }
        )
        integration = ticketing_service.get_integration_context(context)

        room_number = self._first_non_empty(
            pending.get("room_number"),
            entities.get("room_number"),
            draft.get("room_number"),
            context.room_number,
            integration.get("room_number"),
        )
        if room_number:
            draft["room_number"] = room_number

        guest_name = self._first_non_empty(
            pending.get("guest_name"),
            entities.get("guest_name"),
            draft.get("guest_name"),
            context.guest_name,
            integration.get("guest_name") if isinstance(integration, dict) else "",
        )
        if guest_name:
            draft["guest_name"] = guest_name

        guest_phone = self._first_non_empty(
            pending.get("guest_phone"),
            entities.get("guest_phone"),
            draft.get("guest_phone"),
            context.guest_phone,
            integration.get("guest_phone") if isinstance(integration, dict) else "",
            integration.get("wa_number") if isinstance(integration, dict) else "",
        )
        if guest_phone:
            draft["guest_phone"] = guest_phone

        return draft

    @staticmethod
    def _missing_plugin_ticket_details(
        *,
        phase: str,
        room_number: str,
        issue: str,
        context: ConversationContext,
        pending: dict[str, Any],
        entities: dict[str, Any],
    ) -> list[str]:
        missing: list[str] = []
        issue_text = str(issue or "").strip()
        if len(issue_text) < 5:
            missing.append("issue")

        phase_norm = str(phase or "").strip().lower().replace(" ", "_")
        integration = ticketing_service.get_integration_context(context)
        flow_norm = str(
            integration.get("flow")
            or integration.get("bot_mode")
            or ""
        ).strip().lower().replace("-", "_")
        source_norm = str(
            pending.get("ticket_source")
            or integration.get("ticket_source")
            or integration.get("source")
            or ""
        ).strip().lower().replace("-", "_")
        is_prebooking = (
            phase_norm in {"pre_booking", "booking", "pre_checkin"}
            or flow_norm in {"engage", "booking", "booking_bot", "pre_booking", "prebooking"}
            or source_norm in {"booking_bot", "engage", "booking"}
        )
        is_during_stay = (
            phase_norm in {"during_stay", "duringstay", "in_stay", "instay"}
            or flow_norm in {"during_stay", "duringstay", "in_stay", "instay", "service", "operations"}
            or source_norm in {"service", "during_stay", "in_stay", "operations"}
        )

        resolved_room = str(
            room_number
            or context.room_number
            or pending.get("room_number")
            or integration.get("room_number")
            or entities.get("room_number")
            or ""
        ).strip()

        resolved_phone = str(
            pending.get("guest_phone")
            or integration.get("guest_phone")
            or integration.get("wa_number")
            or context.guest_phone
            or entities.get("guest_phone")
            or ""
        ).strip()
        resolved_name = str(
            pending.get("guest_name")
            or integration.get("guest_name")
            or context.guest_name
            or entities.get("guest_name")
            or ""
        ).strip()

        if is_prebooking:
            if not (resolved_phone or resolved_name):
                missing.append("guest_identity")
            return missing

        if is_during_stay:
            if not resolved_room:
                missing.append("room_number")
            return missing

        if phase_norm == "post_checkout":
            if not (resolved_phone or resolved_name or resolved_room):
                missing.append("guest_identity")
            return missing

        if not (resolved_room or (resolved_phone and resolved_name)):
            missing.append("contact_or_room")
        return missing

    def _has_minimum_plugin_ticket_details(
        self,
        *,
        phase: str,
        room_number: str,
        issue: str,
        context: ConversationContext,
        pending: dict[str, Any],
        entities: dict[str, Any],
    ) -> bool:
        return not self._missing_plugin_ticket_details(
            phase=phase,
            room_number=room_number,
            issue=issue,
            context=context,
            pending=pending,
            entities=entities,
        )

    @staticmethod
    def _derive_plugin_ticket_category(
        *,
        effective_intent: IntentType,
        entities: dict[str, Any],
        message: str,
    ) -> str:
        explicit = str(entities.get("category") or entities.get("categorization") or "").strip().lower()
        if explicit:
            return explicit
        if effective_intent == IntentType.COMPLAINT:
            return "complaint"
        text = str(message or "").lower()
        if any(marker in text for marker in ("broken", "not working", "dirty", "cockroach", "roach", "problem")):
            return "complaint"
        return "request"

    @staticmethod
    def _derive_plugin_ticket_priority(
        *,
        entities: dict[str, Any],
        message: str,
    ) -> str:
        explicit = str(entities.get("priority") or "").strip().lower()
        if explicit in {"low", "medium", "high", "critical"}:
            return explicit
        text = str(message or "").lower()
        if any(
            marker in text
            for marker in ("fire", "smoke", "medical emergency", "unsafe", "security threat")
        ):
            return "critical"
        if any(
            marker in text
            for marker in ("urgent", "asap", "immediately", "critical", "emergency", "cockroach", "roach", "pest")
        ):
            return "high"
        if any(marker in text for marker in ("whenever", "not urgent", "later")):
            return "low"
        return "medium"

    @staticmethod
    def _infer_plugin_ticket_sub_category(
        *,
        effective_intent: IntentType,
        entities: dict[str, Any],
        message: str,
        matched_case: str = "",
    ) -> str:
        explicit = str(
            entities.get("sub_category")
            or entities.get("sub_categorization")
            or ""
        ).strip().lower()
        if explicit:
            return explicit

        text = str(message or "").lower()
        case_text = str(matched_case or "").strip().lower()
        if effective_intent == IntentType.ROOM_SERVICE:
            if any(marker in text for marker in ("towel", "blanket", "pillow", "amenities", "toiletries", "hairdryer")):
                return "amenities"
            if any(marker in text for marker in ("housekeeping", "cleaning", "clean room")):
                return "housekeeping"
            if any(marker in text for marker in ("laundry", "iron", "dry clean")):
                return "laundry"
            if any(marker in text for marker in ("ac", "air conditioner", "repair", "broken", "not working", "maintenance", "leak")):
                return "maintenance"
            return "room_service"

        combined = f"{text} {case_text}".strip()
        if any(marker in combined for marker in ("cockroach", "roach", "pest", "bed bug", "insect")):
            return "housekeeping"
        if any(marker in text for marker in ("taxi", "airport", "pickup", "drop", "transport", "shuttle", "chauffeur", "cab")):
            return "transport"
        if any(marker in text for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(
            marker in text
            for marker in ("room booking", "book a room", "book room", "stay booking", "check in", "check-out", "check out", "suite")
        ):
            return "room_booking"
        if any(marker in text for marker in ("food", "order", "meal", "menu", "dining", "in-room dining")):
            return "order_food"
        if any(marker in text for marker in ("billing", "invoice", "refund", "wrong charge", "payment")):
            return "billing"
        if any(marker in text for marker in ("table", "reservation", "book table", "restaurant booking")):
            return "table_booking"
        if any(marker in case_text for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(
            marker in case_text
            for marker in ("room booking", "book a room", "book room", "stay booking", "check in", "check-out", "check out", "suite")
        ):
            return "room_booking"
        if any(marker in case_text for marker in ("taxi", "airport", "pickup", "drop", "transport", "shuttle", "chauffeur", "cab")):
            return "transport"
        if any(marker in case_text for marker in ("food", "order", "meal", "menu", "dining", "in-room dining")):
            return "order_food"
        if any(marker in case_text for marker in ("billing", "invoice", "refund", "wrong charge", "payment")):
            return "billing"
        if any(marker in case_text for marker in ("table", "reservation", "book table", "restaurant booking")):
            return "table_booking"
        return "complaint"

    def _build_order_ticket_issue_from_pending(
        self,
        pending_data: dict[str, Any],
        *,
        response_text: str,
    ) -> str:
        item_name = self._extract_order_item_name(pending_data) or "food order"
        quantity = self._extract_order_quantity_value(pending_data)
        total_raw = self._first_non_empty(
            pending_data.get("order_total"),
            pending_data.get("total"),
        )
        extra = self._first_non_empty(
            pending_data.get("order_additional_request"),
            pending_data.get("special_request"),
        )
        summary = f"Food order confirmed: {(str(quantity) if quantity is not None else '1')} x {item_name}"
        if total_raw:
            summary += f", total Rs.{total_raw}"
        if extra:
            summary += f", notes: {extra}"
        if summary.strip():
            return summary.strip()
        return str(response_text or "").strip()

    def _build_booking_ticket_issue_from_pending(
        self,
        pending_data: dict[str, Any],
        *,
        response_text: str,
        sub_category_override: str = "",
    ) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        fallback = str(response_text or "").strip()
        sub_category = str(sub_category_override or "").strip().lower() or self._infer_transaction_booking_sub_category(
            pending_data=pending,
            response_text=response_text,
        )

        service_name = self._first_non_empty(
            pending.get("service_name"),
            pending.get("restaurant_name"),
            pending.get("booking_service"),
            pending.get("service"),
        )
        party_size = self._first_non_empty(
            pending.get("party_size"),
            pending.get("guests"),
            pending.get("guest_count"),
            pending.get("booking_party_size"),
        )
        booking_time = self._first_non_empty(
            pending.get("time"),
            pending.get("booking_time"),
        )
        booking_date = self._first_non_empty(
            pending.get("date"),
            pending.get("booking_date"),
            pending.get("stay_date_range"),
        )

        if sub_category == "room_booking":
            room_type = self._first_non_empty(
                pending.get("room_type"),
                pending.get("room_name"),
                service_name,
                "room stay",
            )
            check_in = self._first_non_empty(
                pending.get("check_in"),
                pending.get("stay_checkin_date"),
                pending.get("checkin_date"),
            )
            check_out = self._first_non_empty(
                pending.get("check_out"),
                pending.get("stay_checkout_date"),
                pending.get("checkout_date"),
            )
            parts = [f"Room booking confirmed: {room_type}"]
            if check_in and check_out:
                parts.append(f"from {check_in} to {check_out}")
            elif booking_date:
                parts.append(f"for {booking_date}")
            if party_size:
                parts.append(f"for {party_size} guests")
            summary = " ".join(parts).strip()
            if summary:
                if summary.lower() in {"room booking confirmed: room stay"} and fallback:
                    return fallback
                return summary

        if sub_category == "spa_booking":
            treatment = service_name or "spa treatment"
            parts = [f"Spa booking confirmed: {treatment}"]
            if booking_time:
                parts.append(f"at {booking_time}")
            if booking_date and booking_date.lower() not in {"today", "tonight"}:
                parts.append(f"on {booking_date}")
            if party_size:
                parts.append(f"for {party_size} guests")
            summary = " ".join(parts).strip()
            if summary:
                if summary.lower() in {"spa booking confirmed: spa treatment"} and fallback:
                    return fallback
                return summary

        if sub_category == "table_booking":
            service_label = service_name or "table booking"
            parts: list[str] = [f"Table booking confirmed: {service_label}"]
            if party_size:
                parts.append(f"for {party_size} guests")
            if booking_time:
                parts.append(f"at {booking_time}")
            if booking_date and booking_date.lower() not in {"today", "tonight"}:
                parts.append(f"on {booking_date}")
            summary = " ".join(parts).strip()
            if summary:
                if summary.lower() in {"table booking confirmed: table booking"} and fallback:
                    return fallback
                return summary

        if sub_category == "transport_booking":
            service_label = service_name or "transport booking"
            parts: list[str] = [f"Transport request confirmed: {service_label}"]
            if party_size:
                parts.append(f"for {party_size} guests")
            if booking_time:
                parts.append(f"at {booking_time}")
            if booking_date and booking_date.lower() not in {"today", "tonight"}:
                parts.append(f"on {booking_date}")
            summary = " ".join(parts).strip()
            if summary:
                return summary

        generic_label = service_name or "service booking"
        parts = [f"Booking confirmed: {generic_label}"]
        if party_size:
            parts.append(f"for {party_size} guests")
        if booking_time:
            parts.append(f"at {booking_time}")
        if booking_date and booking_date.lower() not in {"today", "tonight"}:
            parts.append(f"on {booking_date}")
        generic_summary = " ".join(parts).strip()
        if generic_summary:
            return generic_summary

        return fallback or "Booking confirmed."

    @staticmethod
    def _looks_like_booking_confirmation_response(response_text: str) -> bool:
        text = str(response_text or "").strip().lower()
        if not text:
            return False
        positive_markers = (
            "booking has been confirmed",
            "booking is confirmed",
            "successfully booked",
            "booking confirmed",
            "reservation confirmed",
        )
        if any(marker in text for marker in positive_markers):
            return True
        return ("booked" in text or "confirmed" in text) and "order" not in text

    def _infer_transaction_booking_sub_category(
        self,
        *,
        pending_data: dict[str, Any],
        response_text: str,
        matched_case: str = "",
    ) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        explicit = self._first_non_empty(
            pending.get("sub_category"),
            pending.get("booking_sub_category"),
            pending.get("booking_type"),
        ).lower().replace(" ", "_")
        if explicit in {"spa_booking", "room_booking", "transport_booking"}:
            return explicit
        explicit_table = explicit == "table_booking"

        service_name = self._first_non_empty(
            pending.get("service_name"),
            pending.get("restaurant_name"),
            pending.get("booking_service"),
            pending.get("service"),
        ).lower()
        primary_text = " ".join(
            [
                str(response_text or "").lower(),
                service_name,
                str(pending.get("room_type") or "").lower(),
                str(pending.get("check_in") or pending.get("stay_checkin_date") or "").lower(),
                str(pending.get("check_out") or pending.get("stay_checkout_date") or "").lower(),
                str(pending.get("stay_date_range") or "").lower(),
            ]
        )
        if any(
            marker in primary_text
            for marker in ("room", "suite", "stay", "check in", "check-in", "checkin", "checkout", "check-out")
        ) or any(
            pending.get(key)
            for key in ("room_type", "check_in", "check_out", "stay_checkin_date", "stay_checkout_date", "stay_date_range")
        ):
            return "room_booking"
        if any(
            marker in primary_text
            for marker in (
                "transport",
                "airport transfer",
                "airport pickup",
                "airport drop",
                "pickup",
                "drop",
                "cab",
                "taxi",
                "ride",
                "shuttle",
                "chauffeur",
            )
        ):
            return "transport_booking"
        if any(marker in primary_text for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(
            marker in primary_text
            for marker in (
                "table",
                "restaurant",
                "reservation",
                "book table",
                "dining",
            )
        ):
            return "table_booking"
        case_hint = str(matched_case or "").lower()
        if any(
            marker in case_hint
            for marker in ("room booking", "book room", "stay booking", "check in", "check-out")
        ):
            return "room_booking"
        if any(
            marker in case_hint
            for marker in (
                "transport",
                "airport transfer",
                "airport pickup",
                "airport drop",
                "pickup",
                "drop",
                "cab",
                "taxi",
                "ride",
                "shuttle",
                "chauffeur",
            )
        ):
            return "transport_booking"
        if any(marker in case_hint for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(marker in case_hint for marker in ("table", "restaurant", "reservation", "book table", "dining")):
            return "table_booking"
        if explicit_table and service_name and not any(
            marker in service_name
            for marker in (
                "room",
                "suite",
                "spa",
                "massage",
                "transport",
                "airport",
                "pickup",
                "drop",
                "cab",
                "taxi",
                "shuttle",
                "chauffeur",
            )
        ):
            return "table_booking"
        return ""

    def _should_route_full_kb_ticketing_handler(
        self,
        *,
        effective_intent: IntentType,
        current_pending_action: str | None,
        pending_action_target: str | None,
        message: str,
        llm_response_text: str,
        llm_ticketing_preference: bool | None,
    ) -> bool:
        msg_lower = str(message or "").strip().lower()
        current_pending = str(current_pending_action or "").strip().lower()
        if current_pending.startswith("confirm_"):
            binary_reply = self._detect_simple_binary_reply(message)
            explicit_complaint_evidence = (
                self._looks_like_ticketing_request(msg_lower)
                or self._looks_like_strong_ticket_issue_marker(msg_lower)
                or self._looks_like_operational_issue_for_ticketing(msg_lower)
            )
            # Confirmation lock: yes/no replies should stay in the active
            # transactional flow unless user shows explicit complaint intent.
            if binary_reply is not None and not explicit_complaint_evidence:
                return False

        # Keep order slot-collection conversational.
        # If the user is still giving order details (for example quantity),
        # do not jump to complaint/ticket handler on a spurious model label.
        # Allow switching only on explicit complaint/ticket asks.
        if self._is_order_pending_action(current_pending_action) or self._is_order_pending_action(pending_action_target):
            if self._is_ticketing_pending_action(pending_action_target):
                return True
            if self._is_ticketing_pending_action(current_pending_action):
                return True
            if not msg_lower:
                return False
            if self._looks_like_ticketing_request(msg_lower):
                return True
            if self._looks_like_strong_ticket_issue_marker(msg_lower):
                return True
            return False

        # Keep room/table booking flow conversational.
        # Do not auto-switch to complaint ticketing during slot collection just
        # because the model emitted `requires_ticket=true`.
        if effective_intent == IntentType.TABLE_BOOKING:
            if self._is_ticketing_pending_action(pending_action_target):
                return True
            if self._is_ticketing_pending_action(current_pending_action):
                return True
            if not msg_lower:
                return False
            if self._looks_like_ticketing_request(msg_lower):
                return True
            if self._looks_like_operational_issue_for_ticketing(msg_lower):
                return True
            return False

        if llm_ticketing_preference is True:
            return True

        # Keep core order/booking flows intact unless LLM explicitly asks for ticketing
        # or we are already in a ticket-confirm/update pending action.
        if effective_intent in {IntentType.ORDER_FOOD}:
            if self._is_ticketing_pending_action(pending_action_target):
                return True
            if (
                effective_intent in {IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO}
                and self._is_ticketing_pending_action(current_pending_action)
            ):
                return True
            return False

        if effective_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
            return True
        if self._is_ticketing_pending_action(pending_action_target):
            return True
        if (
            effective_intent in {IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO}
            and self._is_ticketing_pending_action(current_pending_action)
        ):
            return True
        if llm_ticketing_preference is False:
            return False
        if not msg_lower:
            return False
        if self._looks_like_ticketing_request(msg_lower):
            return True
        if self._looks_like_operational_issue_for_ticketing(msg_lower):
            return True
        if self._llm_response_implies_staff_action(llm_response_text):
            return True
        return False

    def _resolve_unified_complaint_routing_intent(
        self,
        *,
        message: str,
        effective_intent: IntentType,
        current_pending_action: str | None,
        pending_action_target: str | None,
        llm_response_text: str = "",
        llm_ticketing_preference: bool | None = None,
        next_state: ConversationState = ConversationState.IDLE,
    ) -> tuple[IntentType | None, str, str]:
        """
        Determine whether this turn should be handled by the complaint flow even
        when plugin takeover mode is disabled.
        """
        pending_current = str(current_pending_action or "").strip().lower()
        pending_target = str(pending_action_target or "").strip().lower()
        msg_lower = str(message or "").strip().lower()
        if pending_current.startswith("confirm_"):
            binary_reply = self._detect_simple_binary_reply(message)
            explicit_complaint_evidence = (
                self._looks_like_ticketing_request(msg_lower)
                or self._looks_like_strong_ticket_issue_marker(msg_lower)
                or self._looks_like_operational_issue_for_ticketing(msg_lower)
            )
            if binary_reply is not None and not explicit_complaint_evidence:
                return None, "", ""
        if self._is_ticketing_pending_action(pending_current) or self._is_ticketing_pending_action(pending_target):
            return IntentType.COMPLAINT, "existing_ticketing_pending_action", "pending_action"
        if effective_intent == IntentType.COMPLAINT:
            return IntentType.COMPLAINT, "complaint_intent", "intent"

        should_attempt = self._should_route_full_kb_ticketing_handler(
            effective_intent=effective_intent,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            message=message,
            llm_response_text=llm_response_text,
            llm_ticketing_preference=llm_ticketing_preference,
        )
        if not should_attempt:
            return None, "", ""

        fallback_intent = self._infer_plugin_ticket_fallback_intent(
            message=message,
            effective_intent=effective_intent,
            next_state=next_state,
        )
        if fallback_intent == IntentType.COMPLAINT:
            return IntentType.COMPLAINT, "default_complaint_agent_fallback", "chat_service"

        if (
            effective_intent == IntentType.HUMAN_REQUEST
            and self._llm_response_implies_staff_action(llm_response_text)
        ):
            return IntentType.COMPLAINT, "human_request_staff_action_fallback", "chat_service"
        return None, "", ""

    @staticmethod
    def _extract_full_kb_entities_for_handler(llm_result: Any) -> dict[str, Any]:
        entities: dict[str, Any] = {}
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        nested_entities = llm_output.get("entities")
        if isinstance(nested_entities, dict):
            entities.update(nested_entities)

        top_level_entity_keys = (
            "service_id",
            "service_name",
            "target_service_id",
            "target_service",
            "resolved_service_id",
            "resolved_service_name",
            "workflow_id",
            "booking_sub_category",
            "sub_category",
            "ticket_category",
            "ticket_sub_category",
            "ticket_priority",
            "ticket_issue",
            "ticket_reason",
            "requires_ticket",
        )
        for key in top_level_entity_keys:
            value = llm_output.get(key)
            if value in (None, ""):
                continue
            entities.setdefault(key, value)

        pending_updates = llm_output.get("pending_data_updates")
        if isinstance(pending_updates, dict):
            for key, value in pending_updates.items():
                if key not in entities and value not in (None, ""):
                    entities[key] = value

        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        for key, value in pending_data.items():
            if key not in entities and value not in (None, ""):
                entities[key] = value
        return entities

    @staticmethod
    def _extract_full_kb_ticketing_preference(llm_result: Any) -> bool | None:
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        candidates = (
            llm_output.get("requires_ticket"),
            llm_output.get("is_ticket_intent"),
            llm_output.get("ticket_required"),
            llm_output.get("ticketing_required"),
            pending_data.get("requires_ticket"),
            pending_data.get("is_ticket_intent"),
            pending_data.get("ticketing_flow"),
        )
        for value in candidates:
            parsed = ChatService._coerce_optional_bool(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _extract_full_kb_ticket_ready_flag(llm_result: Any) -> bool | None:
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        candidates = (
            llm_output.get("ticket_ready_to_create"),
            llm_output.get("ready_to_create"),
            llm_output.get("ticket_ready"),
            llm_output.get("ticket_create_ready"),
            pending_data.get("ticket_ready_to_create"),
            pending_data.get("ready_to_create"),
            pending_data.get("ticket_ready"),
            pending_data.get("ticket_create_ready"),
        )
        for value in candidates:
            parsed = ChatService._coerce_optional_bool(value)
            if parsed is not None:
                return parsed
        return None

    @classmethod
    def _is_pure_llm_ticket_ready(cls, llm_result: Any) -> bool:
        explicit_ready = cls._extract_full_kb_ticket_ready_flag(llm_result)
        pending_action = str(getattr(llm_result, "pending_action", "") or "").strip()
        next_state = getattr(llm_result, "next_state", ConversationState.IDLE)
        waiting_for_user = pending_action or next_state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
        }
        if explicit_ready is not None:
            return bool(explicit_ready) and not waiting_for_user
        return not waiting_for_user and next_state in {
            ConversationState.COMPLETED,
            ConversationState.ESCALATED,
            ConversationState.PROCESSING_ORDER,
        }

    @staticmethod
    def _extract_full_kb_ticketing_reason(llm_result: Any) -> str:
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        reason = (
            llm_output.get("ticket_reason")
            or llm_output.get("ticketing_reason")
            or pending_data.get("ticket_reason")
            or pending_data.get("ticketing_reason")
            or ""
        )
        return str(reason or "").strip()

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

    @staticmethod
    def _build_ticketing_case_context_text(
        *,
        context: ConversationContext,
        latest_user_message: str,
        llm_response_text: str = "",
        max_messages: int = 14,
        max_chars: int = 6000,
    ) -> str:
        lines: list[str] = []
        history_messages = (
            context.messages
            if max_messages <= 0
            else context.get_recent_messages(max_messages)
        )
        for msg in history_messages:
            role = "User" if msg.role == MessageRole.USER else "Assistant"
            content = str(msg.content or "").strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")

        latest_user = str(latest_user_message or "").strip()
        if latest_user:
            lines.append(f"Latest User: {latest_user}")
        assistant_text = str(llm_response_text or "").strip()
        if assistant_text:
            lines.append(f"Assistant Draft: {assistant_text}")

        joined = "\n".join(lines).strip()
        limit = max(1200, int(max_chars or 6000))
        if len(joined) <= limit:
            return joined
        return joined[-limit:]

    @staticmethod
    def _resolve_history_window(context: ConversationContext) -> int:
        """
        Use full available session history for agent calls, bounded by the
        configured maximum conversation history.
        """
        configured_max = max(6, int(getattr(settings, "max_conversation_history", 20) or 20))
        available = len(context.messages or [])
        if available <= 0:
            return configured_max
        return min(available, configured_max)

    @staticmethod
    def _normalize_multi_ask_query_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _response_sentence_key(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        return text.strip(" .,!?:;\"'")

    @classmethod
    def _dedupe_response_sentences(cls, response_text: str) -> str:
        text = str(response_text or "").strip()
        if not text:
            return ""

        normalized = re.sub(r"\r\n?", "\n", text).strip()
        if not normalized:
            return ""

        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        if not lines:
            return ""

        seen: set[str] = set()
        compact_lines: list[str] = []
        for line in lines:
            if re.match(r"^\d+\.\s+", line) or line.startswith("- "):
                key = cls._response_sentence_key(line)
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                compact_lines.append(line)
                continue

            fragments = re.split(r"(?<=[.!?])\s+", line)
            kept: list[str] = []
            for fragment in fragments:
                sentence = cls._normalize_multi_ask_query_text(fragment)
                if len(sentence) < 2:
                    continue
                key = cls._response_sentence_key(sentence)
                if not key or key in seen:
                    continue
                seen.add(key)
                kept.append(sentence)
            if kept:
                compact_lines.append(" ".join(kept))

        cleaned = "\n".join(compact_lines).strip()
        if cleaned:
            return cleaned
        return cls._normalize_multi_ask_query_text(normalized)

    @staticmethod
    def _response_surface_key(text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        return normalized.strip(" \n\t\r.,!?:;\"'")

    def _current_phase_service_names(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
    ) -> list[str]:
        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        phase_id = self._normalize_phase_identifier(selected_phase.get("selected_phase_id"))
        if not phase_id:
            return []
        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return []
        names: list[str] = []
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if service_phase_id != phase_id:
                continue
            name = str(service.get("name") or service.get("id") or "").strip()
            if not name:
                continue
            names.append(name)
            if len(names) >= 6:
                break
        return names

    async def _maybe_llm_rewrite_response(
        self,
        *,
        response_text: str,
        user_message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        capability_check_allowed: bool = True,
        capability_reason: str = "",
        response_source: str = "",
        validator_replaced: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        base_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()
        metadata = {
            "llm_surface_rewrite_attempted": False,
            "llm_surface_rewrite_applied": False,
            "llm_surface_rewrite_validator_replaced": False,
            "llm_surface_rewrite_validation_issues": [],
            "llm_surface_rewrite_source": str(response_source or ""),
        }
        if not base_text:
            return "", metadata

        response_surface_enabled = bool(getattr(settings, "chat_llm_response_surface_enabled", True))
        no_template_mode = bool(getattr(settings, "chat_no_template_response_mode", False))
        # In orchestration/no_template mode the service agent and ticket confirmation logic
        # already produce the correct response — never let a secondary LLM rewrite it.
        if no_template_mode:
            return base_text, metadata
        if not response_surface_enabled:
            return base_text, metadata
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return base_text, metadata

        control_sources = {
            "deescalation",
            "parked_task_resume",
            "parked_task_cancel",
            "low_confidence",
        }
        if str(response_source or "").strip().lower() in control_sources:
            return base_text, metadata

        llm_generated_sources = {
            "llm_fallback",
            "agent_orchestrator",
            "full_kb_llm",
            "full_kb_llm_handler",
            "full_kb_shortcut",
            "pure_llm",
            "llm_orchestration",
            "multi_ask_orchestrator",
        }
        rewrite_llm_outputs = bool(getattr(settings, "chat_llm_response_surface_rewrite_llm_outputs", False))
        if (
            str(response_source or "").strip().lower() in llm_generated_sources
            and not validator_replaced
            and not rewrite_llm_outputs
            and not no_template_mode
        ):
            return base_text, metadata

        metadata["llm_surface_rewrite_attempted"] = True
        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
        )
        phase_name = str(selected_phase.get("selected_phase_name") or "").strip()
        phase_id = str(selected_phase.get("selected_phase_id") or "").strip()
        phase_label = phase_name or (phase_id.replace("_", " ").title() if phase_id else "Current")
        phase_services = self._current_phase_service_names(
            context=context,
            capabilities_summary=capabilities_summary,
        )
        phase_services_text = ", ".join(phase_services) if phase_services else "Not specified"
        nlu_policy = capabilities_summary.get("nlu_policy", {}) if isinstance(capabilities_summary, dict) else {}
        dos = nlu_policy.get("dos", []) if isinstance(nlu_policy, dict) else []
        donts = nlu_policy.get("donts", []) if isinstance(nlu_policy, dict) else []
        dos_text = "; ".join(str(item).strip() for item in dos if str(item).strip()) or "None"
        donts_text = "; ".join(str(item).strip() for item in donts if str(item).strip()) or "None"
        model = str(getattr(settings, "chat_llm_response_surface_model", "") or "").strip() or None
        temperature = float(getattr(settings, "chat_llm_response_surface_temperature", 0.35) or 0.35)
        max_tokens = max(120, int(getattr(settings, "chat_llm_response_surface_max_tokens", 420) or 420))
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()

        system_prompt = (
            "You rewrite concierge assistant replies.\n"
            "Return plain text only.\n"
            "Strict rules:\n"
            "- Preserve factual meaning from the draft.\n"
            "- Do not add new facts, prices, policies, or promises.\n"
            "- Preserve all restrictions (phase limits, unavailable services, ticketing constraints).\n"
            "- Keep explicit confirmation phrase instructions unchanged when present.\n"
            "- Keep list/number formatting if the draft uses it.\n"
            "- Keep tone human, concise, and helpful.\n"
        )
        user_payload = (
            f"User message: {str(user_message or '').strip()}\n"
            f"Intent: {intent_result.intent.value}\n"
            f"Current phase: {phase_label} ({phase_id or 'unknown'})\n"
            f"Current phase services: {phase_services_text}\n"
            f"Capability allowed: {bool(capability_check_allowed)}\n"
            f"Capability reason (if blocked): {str(capability_reason or '').strip() or 'N/A'}\n"
            f"Policy DO rules: {dos_text}\n"
            f"Policy DONT rules: {donts_text}\n"
            f"Confirmation phrase: {confirmation_phrase}\n\n"
            f"Draft response:\n{base_text}"
        )
        try:
            rewritten = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception:
            rewritten = ""

        candidate = self._dedupe_response_sentences(rewritten) or str(rewritten or "").strip()
        if not candidate or len(candidate) < 3:
            return base_text, metadata

        validation = response_validator.validate(
            response_text=candidate,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check_allowed,
            capability_reason=capability_reason,
        )
        issue_codes = [str(issue.code) for issue in (validation.issues or []) if getattr(issue, "code", None)]
        metadata["llm_surface_rewrite_validation_issues"] = issue_codes
        if validation.valid:
            metadata["llm_surface_rewrite_applied"] = (
                self._response_surface_key(candidate) != self._response_surface_key(base_text)
            )
            return candidate, metadata

        if validation.action == "replace" and validation.replacement_response:
            safe_text = self._dedupe_response_sentences(validation.replacement_response) or str(
                validation.replacement_response or ""
            ).strip()
            metadata["llm_surface_rewrite_validator_replaced"] = True

            # First try an issue-aware LLM repair instead of surfacing deterministic replacement text.
            repair_issue_text = ", ".join(issue_codes) if issue_codes else "policy_violation"
            repair_prompt = (
                "Rewrite the assistant response so it passes runtime policy checks.\n"
                "Return plain text only.\n"
                "Do not use templated/canned phrasing.\n"
                "Keep meaning aligned with policy restrictions and available services.\n"
                "Do not promise blocked actions.\n\n"
                f"User message: {str(user_message or '').strip()}\n"
                f"Intent: {intent_result.intent.value}\n"
                f"Policy issue codes: {repair_issue_text}\n"
                f"Current phase: {phase_label} ({phase_id or 'unknown'})\n"
                f"Current phase services: {phase_services_text}\n"
                f"Capability allowed: {bool(capability_check_allowed)}\n"
                f"Capability reason (if blocked): {str(capability_reason or '').strip() or 'N/A'}\n\n"
                f"Original draft:\n{base_text}\n\n"
                f"Unsafe candidate:\n{candidate}\n\n"
                f"Validator-safe fallback reference:\n{safe_text}"
            )
            try:
                repaired = await llm_client.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You produce concise, policy-compliant concierge replies. "
                                "Output plain text only."
                            ),
                        },
                        {"role": "user", "content": repair_prompt},
                    ],
                    model=model,
                    temperature=max(0.1, min(temperature, 0.45)),
                    max_tokens=max_tokens,
                )
            except Exception:
                repaired = ""
            repaired_candidate = self._dedupe_response_sentences(repaired) or str(repaired or "").strip()
            if repaired_candidate:
                repaired_validation = response_validator.validate(
                    response_text=repaired_candidate,
                    intent_result=intent_result,
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=capability_check_allowed,
                    capability_reason=capability_reason,
                )
                if repaired_validation.valid:
                    metadata["llm_surface_rewrite_applied"] = (
                        self._response_surface_key(repaired_candidate) != self._response_surface_key(base_text)
                    )
                    return repaired_candidate
                if repaired_validation.action == "replace" and repaired_validation.replacement_response:
                    safe_text = self._dedupe_response_sentences(repaired_validation.replacement_response) or str(
                        repaired_validation.replacement_response or ""
                    ).strip()

            if bool(getattr(settings, "chat_llm_response_surface_rewrite_replacements", True)) and safe_text:
                try:
                    soft_rewrite = await llm_client.chat(
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Paraphrase the response with the same policy constraints and meaning. "
                                    "Do not relax restrictions. Return plain text only."
                                ),
                            },
                            {"role": "user", "content": safe_text},
                        ],
                        model=model,
                        temperature=max(0.0, min(temperature, 0.25)),
                        max_tokens=max_tokens,
                    )
                except Exception:
                    soft_rewrite = ""
                soft_candidate = self._dedupe_response_sentences(soft_rewrite) or str(soft_rewrite or "").strip()
                if soft_candidate:
                    soft_validation = response_validator.validate(
                        response_text=soft_candidate,
                        intent_result=intent_result,
                        context=context,
                        capabilities_summary=capabilities_summary,
                        capability_check_allowed=capability_check_allowed,
                        capability_reason=capability_reason,
                    )
                    if soft_validation.valid:
                        safe_text = soft_candidate
                    elif (
                        soft_validation.action == "replace"
                        and soft_validation.replacement_response
                    ):
                        safe_text = self._dedupe_response_sentences(soft_validation.replacement_response) or str(
                            soft_validation.replacement_response or ""
                        ).strip()

            metadata["llm_surface_rewrite_applied"] = (
                self._response_surface_key(safe_text) != self._response_surface_key(base_text)
            )
            return safe_text, metadata

        return base_text, metadata

    @classmethod
    def _split_stacked_multi_ask_part(cls, part: str) -> list[str]:
        text = cls._normalize_multi_ask_query_text(part)
        if not text:
            return []

        markers = list(
            re.finditer(r"\b(?:what|when|where|which|who|how|is|are|do|can|will)\b", text, flags=re.IGNORECASE)
        )
        if len(markers) <= 1:
            return [text]

        stop_prev_tokens = {
            "i",
            "you",
            "u",
            "me",
            "to",
            "the",
            "a",
            "an",
            "and",
            "or",
            "if",
            "please",
            "tell",
            "show",
            "explain",
            "know",
            "need",
            "want",
            "can",
            "do",
            "is",
            "are",
            "will",
        }
        split_points = [0]
        for marker in markers[1:]:
            prefix_tokens = re.findall(r"[a-z0-9']+", text[: marker.start()].lower())
            if len(prefix_tokens) < 4:
                continue
            prev_token = prefix_tokens[-1]
            if len(prev_token) < 3 or prev_token in stop_prev_tokens:
                continue
            split_points.append(marker.start())

        if len(split_points) == 1:
            return [text]

        split_points.append(len(text))
        chunks: list[str] = []
        for idx in range(len(split_points) - 1):
            chunk = cls._normalize_multi_ask_query_text(text[split_points[idx] : split_points[idx + 1]])
            if chunk:
                chunks.append(chunk)
        return chunks or [text]

    @classmethod
    def _fallback_split_multi_ask_queries(cls, message: str, max_items: int) -> list[str]:
        text = cls._normalize_multi_ask_query_text(message)
        if not text:
            return []

        parts = re.split(
            r"[?.!]+\s+|[;]+\s+|,\s+(?=(?:what|when|where|which|who|how|is|are|do|can|will|please|also)\b)|\s+(?:and|also|plus|then)\s+",
            text,
            flags=re.IGNORECASE,
        )
        expanded_parts: list[str] = []
        for part in parts:
            expanded_parts.extend(cls._split_stacked_multi_ask_part(part))

        cleaned: list[str] = []
        seen: set[str] = set()
        for part in expanded_parts:
            query = cls._normalize_multi_ask_query_text(part)
            if len(query) < 3:
                continue
            if len(re.findall(r"[a-z0-9]+", query.lower())) < 2:
                continue
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(query)
            if len(cleaned) >= max_items:
                break
        return cleaned

    async def _decompose_multi_ask_queries(
        self,
        message: str,
        *,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> list[str]:
        normalized_message = self._normalize_multi_ask_query_text(message)
        if not normalized_message:
            return []

        min_items = max(2, int(getattr(settings, "chat_multi_ask_min_items", 2) or 2))
        max_items = max(min_items, int(getattr(settings, "chat_multi_ask_max_items", 6) or 6))
        max_items = min(max_items, 12)

        if str(getattr(settings, "openai_api_key", "") or "").strip():
            decompose_model = str(getattr(settings, "chat_multi_ask_decompose_model", "") or "").strip() or None
            phase_label = str(selected_phase_name or "").strip() or (
                str(selected_phase_id or "").replace("_", " ").title()
            )
            phase_id = str(selected_phase_id or "").strip() or "unknown"
            prompt = (
                "You are a query decomposition assistant.\n"
                "Task: decide if the user message contains multiple independent asks.\n"
                f"Selected user journey phase: {phase_label} ({phase_id}).\n"
                "Return strict JSON with keys:\n"
                "is_multi_ask: boolean\n"
                "sub_requests: array of rewritten standalone asks preserving original meaning.\n"
                "Rules:\n"
                "- Keep wording concise and faithful.\n"
                "- Do not add/remove asks.\n"
                "- If single ask, return is_multi_ask=false and sub_requests as empty array.\n"
                f"- Output at most {max_items} sub_requests."
            )
            try:
                parsed = await llm_client.chat_with_json(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": normalized_message},
                    ],
                    model=decompose_model,
                    temperature=0.0,
                )
            except Exception:
                parsed = {}

            llm_multi = self._coerce_optional_bool(
                parsed.get("is_multi_ask") if isinstance(parsed, dict) else None
            )
            raw_items = []
            if isinstance(parsed, dict):
                raw_items = parsed.get("sub_requests", [])
            if not isinstance(raw_items, list):
                raw_items = []

            llm_queries: list[str] = []
            seen_queries: set[str] = set()
            for item in raw_items:
                query = self._normalize_multi_ask_query_text(item)
                if len(query) < 3:
                    continue
                key = query.lower()
                if key in seen_queries:
                    continue
                seen_queries.add(key)
                llm_queries.append(query)
                if len(llm_queries) >= max_items:
                    break

            if llm_multi is True and len(llm_queries) >= min_items:
                return llm_queries
            if len(llm_queries) >= min_items:
                return llm_queries

        fallback = self._fallback_split_multi_ask_queries(normalized_message, max_items)
        if len(fallback) >= min_items:
            return fallback
        return []

    @staticmethod
    def _build_multi_ask_fallback_response(sub_results: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for index, row in enumerate(sub_results, start=1):
            response_text = str(row.get("response_text") or "").strip()
            if not response_text:
                continue
            lines.append(f"{index}. {response_text}")
        if not lines:
            return (
                "I could not find this in the current knowledge base for this property. "
                "If you want, I can connect you with our team."
            )
        return "\n".join(lines)

    async def _compose_multi_ask_response_with_llm(
        self,
        *,
        original_message: str,
        sub_results: list[dict[str, Any]],
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> str:
        fallback = self._build_multi_ask_fallback_response(sub_results)
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return fallback

        compose_model = str(getattr(settings, "chat_multi_ask_compose_model", "") or "").strip() or None
        evidence_lines: list[str] = []
        for index, row in enumerate(sub_results, start=1):
            question = str(row.get("query") or "").strip()
            answer = str(row.get("response_text") or "").strip()
            if not question and not answer:
                continue
            evidence_lines.append(f"Q{index}: {question}")
            evidence_lines.append(f"A{index}: {answer}")
        evidence_blob = "\n".join(evidence_lines).strip()
        if not evidence_blob:
            return fallback

        phase_label = str(selected_phase_name or "").strip() or (
            str(selected_phase_id or "").replace("_", " ").title()
        )
        phase_id = str(selected_phase_id or "").strip() or "unknown"
        prompt = (
            "You are a response composer.\n"
            "Compose one final assistant reply from the provided sub-answer evidence.\n"
            f"Selected user journey phase: {phase_label} ({phase_id}).\n"
            "Rules:\n"
            "- Use only provided evidence; do not add new facts.\n"
            "- Cover each answered sub-question.\n"
            "- Keep tone concise and service-assistant friendly.\n"
            "- If evidence conflicts, prefer safer and more restrictive statements.\n"
            "- Return plain text only."
        )
        try:
            rendered = await llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": (
                            f"Original user message:\n{original_message}\n\n"
                            f"Sub-answer evidence:\n{evidence_blob}"
                        ),
                    },
                ],
                model=compose_model,
                temperature=0.1,
                max_tokens=380,
            )
        except Exception:
            rendered = ""

        candidate = self._normalize_multi_ask_query_text(rendered).strip()
        if not candidate:
            return fallback
        if len(candidate) < 12:
            return fallback
        return candidate

    async def _run_multi_ask_kb_answer(
        self,
        *,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        db_session=None,
    ) -> str:
        forced_intent = IntentResult(
            intent=IntentType.FAQ,
            confidence=1.0,
            entities={"forced_mode": "multi_ask_subquery"},
        )
        sub_context = context.model_copy(deep=True)
        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        sub_context.state = ConversationState.IDLE
        sub_context.pending_action = None
        sub_context.pending_data = conversation_memory_service.merge_with_internal(
            {},
            internal_pending_entries,
        )
        sub_context.pending_data.pop("_clarification_attempts", None)

        try:
            handler_result = await handler_registry.dispatch(
                forced_intent,
                message,
                sub_context,
                capabilities_summary,
                db_session,
            )
        except Exception:
            handler_result = None
        if handler_result is None:
            return ""
        return str(handler_result.response_text or "").strip()

    async def _run_multi_ask_subquery_agent(
        self,
        *,
        query: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_context: dict[str, Any],
        history_window: int,
        db_session=None,
    ) -> dict[str, Any]:
        normalized_query = self._normalize_multi_ask_query_text(query)
        if not normalized_query:
            return {
                "query": "",
                "response_text": "",
                "response_source": "multi_ask_empty_query",
                "intent": IntentType.UNCLEAR.value,
                "confidence": 0.0,
                "entities": {},
                "ticketing": {},
            }

        sub_llm_context = dict(llm_context)
        sub_llm_context["state"] = ConversationState.IDLE.value
        sub_llm_context["pending_action"] = None
        sub_llm_context["pending_data"] = {}
        intent_result = await self._classify_intent(
            normalized_query,
            context.to_llm_messages(count=history_window),
            sub_llm_context,
        )

        phase_gate = self._detect_ticketing_phase_service_mismatch(
            message=normalized_query,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
        )
        if phase_gate is None:
            phase_gate = self._detect_phase_service_unavailable_for_intent(
                message=normalized_query,
                intent=intent_result.intent,
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
            )

        if phase_gate is not None:
            response_text = await self._compose_policy_guardrail_response(
                user_message=normalized_query,
                context=context,
                capabilities_summary=capabilities_summary,
                intent=intent_result.intent,
                gate_payload=phase_gate,
                fallback_text="",
                response_source="multi_ask_phase_gate",
            )
            if not response_text:
                response_text = "I can help with services available in your current phase."
            response_source = "multi_ask_phase_gate"
        else:
            intent_enabled_check = self._check_intent_enabled(intent_result.intent)
            capability_check = (
                intent_enabled_check
                if not intent_enabled_check.allowed
                else self._check_capability_for_intent(
                    context.hotel_code,
                    intent_result,
                    normalized_query,
                )
            )
            if not capability_check.allowed:
                response_text = self._build_capability_denial_response(capability_check)
                response_source = "multi_ask_capability_denial"
            else:
                sub_context = context.model_copy(deep=True)
                internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                sub_context.state = ConversationState.IDLE
                sub_context.pending_action = None
                sub_context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )
                sub_context.pending_data.pop("_clarification_attempts", None)

                identity_match = self._match_identity_response(
                    normalized_query,
                    capabilities_summary,
                    sub_context,
                )
                if identity_match is not None:
                    response_text = str(identity_match.get("response_text") or "").strip()
                    response_source = "multi_ask_identity_shortcut"
                else:
                    service_overview = self._match_service_overview_response(
                        normalized_query,
                        capabilities_summary,
                        sub_context,
                    )
                    if service_overview is not None:
                        response_text = str(service_overview.get("response_text") or "").strip()
                        response_source = "multi_ask_service_overview_shortcut"
                    else:
                        faq_match = self._match_faq_bank_answer(normalized_query, sub_context)
                        if faq_match is not None:
                            response_text = str(faq_match.get("answer") or "").strip()
                            description = str(faq_match.get("description") or "").strip()
                            if description:
                                response_text = f"{response_text}\n\n{description}"
                            response_source = "multi_ask_faq_bank_shortcut"
                        else:
                            service_match = self._match_service_information_response(
                                normalized_query,
                                sub_context,
                                capabilities_summary,
                            )
                            if service_match is not None:
                                response_text = str(service_match.get("response_text") or "").strip()
                                response_source = "multi_ask_service_catalog_shortcut"
                            else:
                                kb_answer = await self._run_multi_ask_kb_answer(
                                    message=normalized_query,
                                    context=sub_context,
                                    capabilities_summary=capabilities_summary,
                                    db_session=db_session,
                                )
                                if kb_answer:
                                    response_text = kb_answer
                                    response_source = "multi_ask_kb_handler"
                                else:
                                    response_text = (
                                        "I could not find this in the current knowledge base for this property. "
                                        "If you want, I can connect you with our team."
                                    )
                                    response_source = "multi_ask_kb_miss"

        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        ticketing_status = await self._evaluate_ticketing_status_for_turn(
            message=normalized_query,
            context=context,
            intent=intent_result.intent,
            response_text=response_text,
            current_pending_action=context.pending_action,
            pending_action_target=None,
            capabilities_summary=capabilities_summary,
        )

        return {
            "query": normalized_query,
            "response_text": response_text,
            "response_source": response_source,
            "intent": intent_result.intent.value,
            "confidence": float(intent_result.confidence),
            "entities": intent_result.entities if isinstance(intent_result.entities, dict) else {},
            "ticketing": ticketing_status,
        }

    async def _maybe_handle_multi_ask_message(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        history_window: int,
        db_session=None,
    ) -> ChatResponse | None:
        if not bool(getattr(settings, "chat_multi_ask_orchestration_enabled", True)):
            return None
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        effective_user_message = self._normalize_multi_ask_query_text(request.message)
        if not effective_user_message:
            return None
        if context.state == ConversationState.ESCALATED and self._is_return_to_bot_request(effective_user_message):
            return None

        msg_lower = effective_user_message.lower()
        token_count = len(re.findall(r"[a-z0-9]+", msg_lower))
        if token_count < 4:
            return None
        has_connector_marker = any(
            marker in msg_lower
            for marker in (" and ", ",", " also ", " plus ", " then ", "?", " & ")
        )
        if not has_connector_marker and msg_lower.count("?") < 2:
            return None

        selected_phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )

        sub_queries = await self._decompose_multi_ask_queries(
            effective_user_message,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
        )
        min_items = max(2, int(getattr(settings, "chat_multi_ask_min_items", 2) or 2))
        if len(sub_queries) < min_items:
            return None

        llm_context = {
            "hotel_code": context.hotel_code,
            "hotel_name": capabilities_summary.get("hotel_name", context.hotel_code),
            "bot_name": capabilities_summary.get("bot_name", "Assistant"),
            "business_type": capabilities_summary.get("business_type", "hotel"),
            "selected_phase_id": selected_phase_context.get("selected_phase_id", ""),
            "selected_phase_name": selected_phase_context.get("selected_phase_name", ""),
            "enabled_intents": [
                intent.get("id")
                for intent in capabilities_summary.get("intents", [])
                if intent.get("enabled", False)
            ],
            "city": capabilities_summary.get("city", ""),
            "guest_name": context.guest_name or "Guest",
            "room_number": context.room_number,
            "state": context.state.value,
            "pending_action": context.pending_action,
            "pending_data": context.pending_data,
            "channel": context.channel,
            "capabilities": capabilities_summary,
            "intent_catalog": capabilities_summary.get("intents", []),
            "service_catalog": capabilities_summary.get("service_catalog", []),
            "faq_bank": capabilities_summary.get("faq_bank", []),
            "tools": capabilities_summary.get("tools", []),
            "nlu_policy": capabilities_summary.get("nlu_policy", {}),
            "prompts": capabilities_summary.get("prompts", {}),
            "conversation_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }

        sub_results: list[dict[str, Any]] = []
        for query in sub_queries:
            sub_result = await self._run_multi_ask_subquery_agent(
                query=query,
                context=context,
                capabilities_summary=capabilities_summary,
                llm_context=llm_context,
                history_window=history_window,
                db_session=db_session,
            )
            sub_results.append(sub_result)

        if not sub_results:
            return None

        response_text = await self._compose_multi_ask_response_with_llm(
            original_message=effective_user_message,
            sub_results=sub_results,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
        )
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        context.pending_action = None
        context.pending_data = conversation_memory_service.merge_with_internal(
            {},
            internal_pending_entries,
        )
        context.pending_data.pop("_clarification_attempts", None)
        context.state = ConversationState.IDLE

        ticketing_statuses = [
            item.get("ticketing")
            for item in sub_results
            if isinstance(item.get("ticketing"), dict)
        ]
        ticketing_required = any(
            bool(status.get("ticketing_required"))
            for status in ticketing_statuses
        )
        ticketing_create_allowed = any(
            bool(status.get("ticketing_create_allowed"))
            for status in ticketing_statuses
        )

        assistant_metadata = {
            "intent": IntentType.FAQ.value,
            "confidence": 0.9,
            "channel": context.channel,
            "response_source": "multi_ask_orchestrator",
            "multi_ask_orchestrated": True,
            "multi_ask_sub_count": len(sub_results),
            "ticketing_required": ticketing_required,
            "ticketing_create_allowed": ticketing_create_allowed,
        }
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        suggested_actions = self._finalize_user_query_suggestions(
            self._get_suggested_actions(
                ConversationState.IDLE,
                IntentType.FAQ,
                capabilities_summary,
            ),
            state=ConversationState.IDLE,
            intent=IntentType.FAQ,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        metadata = {
            "message_count": len(context.messages),
            "entities": {"multi_ask_queries": [item.get("query") for item in sub_results]},
            "classified_intent": IntentType.FAQ.value,
            "classified_confidence": 0.9,
            "routing_path": getattr(getattr(routing_decision, "path", ProcessingPath.COMPLEX), "value", "complex"),
            "routing_score": float(getattr(routing_decision, "score", 0.0)),
            "routing_signals": list(getattr(routing_decision, "signals", [])),
            "response_source": "multi_ask_orchestrator",
            "multi_ask_orchestrated": True,
            "multi_ask_sub_count": len(sub_results),
            "multi_ask_sub_results": sub_results,
            "ticketing_status_checked": True,
            "ticketing_required": ticketing_required,
            "ticketing_create_allowed": ticketing_create_allowed,
            "ticketing_statuses": ticketing_statuses,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=IntentType.FAQ,
            confidence=0.9,
            state=ConversationState.IDLE,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _evaluate_ticketing_status_for_turn(
        self,
        *,
        message: str,
        context: ConversationContext,
        intent: IntentType,
        response_text: str = "",
        current_pending_action: str | None = None,
        pending_action_target: str | None = None,
        capabilities_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_message = self._normalize_multi_ask_query_text(message)
        ticketing_plugin_enabled = bool(self._is_ticketing_plugin_enabled())
        ticketing_service_enabled = bool(ticketing_service.is_ticketing_enabled(capabilities_summary))

        base_status = {
            "ticketing_status_checked": bool(normalized_message),
            "ticketing_plugin_enabled": ticketing_plugin_enabled,
            "ticketing_service_enabled": ticketing_service_enabled,
            "ticketing_required": False,
            "ticketing_create_allowed": False,
            "ticketing_route": "none",
            "ticketing_reason": "",
            "ticketing_source": "",
            "ticketing_matched_case": "",
            "ticketing_skip_reason": "",
        }
        if not normalized_message:
            return base_status

        try:
            phase_context = self._get_selected_phase_context(
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities={},
            )
            decision = await ticketing_agent_service.decide_async(
                intent=intent,
                message=normalized_message,
                llm_response_text=str(response_text or ""),
                llm_ticketing_preference=None,
                current_pending_action=current_pending_action,
                pending_action_target=pending_action_target,
                selected_phase_id=phase_context.get("selected_phase_id", ""),
                selected_phase_name=phase_context.get("selected_phase_name", ""),
                conversation_excerpt=self._build_ticketing_case_context_text(
                    context=context,
                    latest_user_message=normalized_message,
                    llm_response_text=response_text,
                ),
            )
        except Exception as exc:
            status = dict(base_status)
            status["ticketing_status_checked"] = False
            status["ticketing_skip_reason"] = f"ticketing_decision_error:{exc}"
            return status

        status = dict(base_status)
        status["ticketing_required"] = bool(getattr(decision, "activate", False))
        status["ticketing_route"] = str(getattr(decision, "route", "") or "none")
        status["ticketing_reason"] = str(getattr(decision, "reason", "") or "")
        status["ticketing_source"] = str(getattr(decision, "source", "") or "")
        status["ticketing_matched_case"] = str(getattr(decision, "matched_case", "") or "")

        if not status["ticketing_required"]:
            return status
        if not ticketing_plugin_enabled:
            status["ticketing_skip_reason"] = "ticketing_plugin_disabled"
            return status
        if not ticketing_service_enabled:
            status["ticketing_skip_reason"] = "ticketing_service_disabled"
            return status

        phase_gate = self._detect_ticketing_phase_service_mismatch(
            message=normalized_message,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        if phase_gate is None:
            phase_gate = self._detect_phase_service_unavailable_for_intent(
                message=normalized_message,
                intent=intent,
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities={},
            )
        if phase_gate is not None:
            status["ticketing_skip_reason"] = (
                "phase_service_unavailable"
                if bool(phase_gate.get("phase_service_unavailable"))
                else "phase_service_mismatch"
            )
            status["phase_gate_service_id"] = str(phase_gate.get("service_id") or "")
            status["phase_gate_service_name"] = str(phase_gate.get("service_name") or "")
            status["phase_gate_current_phase_id"] = str(phase_gate.get("current_phase_id") or "")
            status["phase_gate_service_phase_id"] = str(phase_gate.get("service_phase_id") or "")
            return status

        ticketing_toggle_gate = self._detect_ticketing_phase_service_ticketing_disabled(
            message=normalized_message,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        if ticketing_toggle_gate is not None:
            status["ticketing_skip_reason"] = "phase_service_ticketing_disabled"
            status["phase_gate_service_id"] = str(ticketing_toggle_gate.get("service_id") or "")
            status["phase_gate_service_name"] = str(ticketing_toggle_gate.get("service_name") or "")
            status["phase_gate_current_phase_id"] = str(ticketing_toggle_gate.get("current_phase_id") or "")
            status["phase_gate_service_phase_id"] = str(ticketing_toggle_gate.get("service_phase_id") or "")
            return status

        status["ticketing_create_allowed"] = True
        return status

    @staticmethod
    def _looks_like_operational_issue_for_ticketing(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False

        complaint_markers = (
            "cockroach",
            "roach",
            "not working",
            "broken",
            "dirty",
            "bad smell",
            "smell",
            "leak",
            "water leak",
            "no water",
            "no hot water",
            "ac not",
            "air conditioner",
            "wifi not",
            "tv not",
            "noise",
            "billing issue",
            "wrong charge",
            "refund",
        )
        operational_request_nouns = (
            "towel",
            "blanket",
            "pillow",
            "soap",
            "shampoo",
            "housekeeping",
            "laundry",
            "clean room",
            "cleaning",
            "maintenance",
            "room service",
        )
        request_verbs = (
            "i need",
            "need ",
            "please send",
            "send ",
            "bring ",
            "arrange ",
            "require ",
            "please arrange",
        )
        if any(marker in text for marker in complaint_markers):
            return True
        if "room service" in text:
            return True
        if any(noun in text for noun in operational_request_nouns) and any(
            verb in text for verb in request_verbs
        ):
            return True
        return False

    @staticmethod
    def _looks_like_loss_or_security_issue(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        loss_markers = (
            "lost",
            "missing",
            "misplaced",
            "stolen",
            "left behind",
            "cannot find",
            "can't find",
            "cant find",
            "belonging",
            "item",
            "luggage",
            "wallet",
            "keys",
            "earring",
            "jewellery",
            "jewelry",
        )
        return any(marker in text for marker in loss_markers)

    def _infer_plugin_ticket_fallback_intent(
        self,
        *,
        message: str,
        effective_intent: IntentType,
        next_state: ConversationState,
    ) -> IntentType | None:
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if not msg_lower:
            return None
        if effective_intent in {IntentType.GREETING, IntentType.UNCLEAR, IntentType.CONFIRMATION_NO}:
            return None
        if effective_intent == IntentType.CONFIRMATION_YES and next_state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
        }:
            return None

        if self._looks_like_information_query(msg_lower):
            return None
        if self._looks_like_ticketing_request(msg_lower):
            return IntentType.COMPLAINT
        if self._looks_like_operational_issue_for_ticketing(msg_lower):
            return IntentType.COMPLAINT
        if self._looks_like_loss_or_security_issue(msg_lower):
            return IntentType.COMPLAINT
        if self._looks_like_booking_change_request(msg_lower):
            return IntentType.COMPLAINT
        if self._is_action_request_text(msg):
            return IntentType.COMPLAINT
        return None

    @staticmethod
    def _llm_response_implies_staff_action(response_text: str) -> bool:
        text = str(response_text or "").strip().lower()
        if not text:
            return False
        markers = (
            "i will escalate",
            "i'll escalate",
            "escalate this",
            "our staff",
            "our team",
            "team will",
            "someone will contact",
            "will be addressed",
            "will arrange",
            "priority follow-up",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_confirmation_prompt_text(message: str) -> bool:
        """
        Detect whether assistant text is explicitly asking for final confirmation.
        Kept generic so it works across workflows/domains.
        """
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        if "type \"yes confirm\"" in msg or "type 'yes confirm'" in msg:
            return True
        if "yes/no" in msg or "(yes/no)" in msg:
            return True
        confirm_markers = (
            "shall i confirm",
            "would you like to confirm",
            "please confirm",
            "confirm this",
            "confirm your",
            "confirmation",
        )
        return any(marker in msg for marker in confirm_markers)

    @staticmethod
    def _contains_explicit_confirmation_instruction(
        response_text: str,
        confirmation_phrase: str,
    ) -> bool:
        msg = re.sub(r"\s+", " ", str(response_text or "").strip().lower())
        phrase = re.sub(r"\s+", " ", str(confirmation_phrase or "").strip().lower())
        if not msg or not phrase:
            return False
        if phrase not in msg:
            return False
        return any(token in msg for token in ("type", "reply", "confirm"))

    def _ensure_explicit_confirmation_instruction(
        self,
        response_text: str,
        confirmation_phrase: str,
    ) -> str:
        """
        Ensure confirmation prompts explicitly instruct user to type the
        required confirmation phrase.
        """
        text = str(response_text or "").strip()
        if not text:
            return f"To proceed, please type \"{confirmation_phrase}\". To cancel, type \"cancel\"."
        if self._contains_explicit_confirmation_instruction(text, confirmation_phrase):
            return text
        if self._is_confirmation_prompt_text(text):
            return (
                f"{text}\n\n"
                f'To proceed, please type "{confirmation_phrase}". '
                'If you want to cancel, type "cancel".'
            )
        return text

    def _normalize_confirm_pending_action(
        self,
        pending_action_target: str | None,
        intent: IntentType,
        room_booking_flow: bool,
    ) -> str:
        pending = str(pending_action_target or "").strip().lower()
        if pending.startswith("confirm_"):
            return pending
        if room_booking_flow:
            return "confirm_room_booking"
        if intent == IntentType.ORDER_FOOD:
            return "confirm_order"
        if intent == IntentType.TABLE_BOOKING:
            return "confirm_booking"
        return "confirm_action"

    def _requires_strict_confirmation(
        self,
        context: ConversationContext,
        current_pending_action: str | None,
    ) -> bool:
        """
        Enforce strict confirmation only for actual final-confirm steps, not
        generic selection/browsing follow-ups.
        """
        pending = str(current_pending_action or "").strip().lower()
        if pending.startswith("confirm_"):
            return True
        if context.state == ConversationState.AWAITING_CONFIRMATION:
            return True

        for msg in reversed(context.messages):
            if msg.role != MessageRole.ASSISTANT:
                continue
            return self._is_confirmation_prompt_text(str(msg.content or ""))
        return False

    @staticmethod
    def _detect_simple_binary_reply(message: str) -> Optional[IntentType]:
        """
        Lightweight yes/no detector used only for polarity correction in
        multi-step selection flows.
        """
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return None
        yes_tokens = {"yes", "yeah", "yep", "y", "ok", "okay", "sure"}
        no_tokens = {"no", "nope", "n", "nah", "cancel", "stop"}
        if msg in yes_tokens:
            return IntentType.CONFIRMATION_YES
        if msg in no_tokens:
            return IntentType.CONFIRMATION_NO
        return None

    def _rewrite_affirmative_selection_reply(
        self,
        context: ConversationContext,
        message: str,
    ) -> str:
        """
        When user replies with bare 'yes' in an awaiting-selection step, map it
        to the first offered option from the previous assistant question.
        This avoids losing conversational intent due to under-specified affirmatives.
        """
        if context.state != ConversationState.AWAITING_SELECTION:
            return message
        if self._detect_simple_binary_reply(message) != IntentType.CONFIRMATION_YES:
            return message

        last_assistant = ""
        for msg in reversed(context.messages):
            if msg.role == MessageRole.ASSISTANT:
                last_assistant = str(msg.content or "").strip()
                break
        if not last_assistant:
            return message

        text = re.sub(r"\s+", " ", last_assistant).strip()
        lower = text.lower()
        if "would you like to" not in lower or " or " not in lower:
            return message

        match = re.search(r"would you like to\s+(.+?)\s+or\s+(.+?)(?:\?|$)", lower)
        if not match:
            return message

        first_option = match.group(1).strip(" .")
        if not first_option:
            return message
        # Use readable text for the LLM, preserving original semantics.
        return first_option

    @staticmethod
    def _looks_like_more_options_followup(message: str) -> bool:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        markers = (
            "show more",
            "more options",
            "any more",
            "do you have more",
            "do u have more",
            "do u hv more",
            "anything else",
            "more",
        )
        return any(marker in msg for marker in markers)

    def _rewrite_more_options_reply(
        self,
        context: ConversationContext,
        message: str,
    ) -> str:
        """
        Generic follow-up rewrite for "show more" requests.
        Adds a topic and explicit non-repeat instruction so the LLM returns only
        additional options, or clearly states no additional options remain.
        """
        if not self._looks_like_more_options_followup(message):
            return message

        stopwords = {
            "show",
            "more",
            "option",
            "options",
            "any",
            "do",
            "you",
            "u",
            "hv",
            "have",
            "please",
            "list",
            "tell",
            "me",
            "about",
            "the",
            "a",
            "an",
            "for",
            "to",
            "with",
            "and",
            "else",
            "anything",
        }

        def topic_tokens(text: str) -> list[str]:
            tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
            return [token for token in tokens if len(token) >= 3 and token not in stopwords]

        current_tokens = topic_tokens(message)
        topic = " ".join(current_tokens[:2]).strip()

        if not topic:
            for msg in reversed(context.messages[:-1]):
                if msg.role != MessageRole.USER:
                    continue
                previous_tokens = topic_tokens(str(msg.content or ""))
                if previous_tokens:
                    topic = " ".join(previous_tokens[:2]).strip()
                    break

        if not topic:
            return message

        return (
            f"Show more options for {topic}. "
            "Return only additional options not already listed in this conversation. "
            "If there are no additional options, clearly say there are no more options."
        )

    @staticmethod
    def _is_room_booking_flow(
        raw_intent: str,
        current_pending_action: str | None,
        pending_action_target: str | None,
        pending_data: dict[str, Any],
        response_text: str,
    ) -> bool:
        raw = str(raw_intent or "").strip().lower()
        current = str(current_pending_action or "").strip().lower()
        target = str(pending_action_target or "").strip().lower()
        combined = " ".join(
            part for part in (
                raw,
                current,
                target,
                str(response_text or "").lower(),
                " ".join(str(k).lower() for k in (pending_data or {}).keys()),
            )
            if part
        )
        if "room_booking" in combined or "stay_booking" in combined:
            return True
        if "select_room_type" in combined or "collect_room_booking_details" in combined:
            return True
        return False

    @staticmethod
    def _is_order_flow(
        raw_intent: str,
        current_pending_action: str | None,
        pending_action_target: str | None,
        pending_data: dict[str, Any],
        response_text: str,
        intent: IntentType,
    ) -> bool:
        current = str(current_pending_action or "").strip().lower()
        target = str(pending_action_target or "").strip().lower()
        raw = str(raw_intent or "").strip().lower()
        combined = " ".join(
            part for part in (
                current,
                target,
                raw,
                str(intent.value if isinstance(intent, IntentType) else intent).lower(),
                str(response_text or "").lower(),
                " ".join(str(k).lower() for k in (pending_data or {}).keys()),
            )
            if part
        )
        if "order_food" in combined:
            return True
        if "confirm_order" in combined or "collect_order_" in combined:
            return True
        if "dish" in combined or "menu" in combined:
            return True
        return False

    @staticmethod
    def _strip_leading_yes(message: str) -> str:
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if msg_lower.startswith("yes "):
            return msg[4:].strip()
        if msg_lower.startswith("yes,"):
            return msg[4:].strip()
        return ""

    def _extract_order_slot_updates(
        self,
        message: str,
        response_text: str,
        current_pending_action: str | None,
        pending_data: dict[str, Any],
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        pending = pending_data if isinstance(pending_data, dict) else {}
        current = str(current_pending_action or "").strip().lower()
        existing_item = self._extract_order_item_name(pending)
        option_candidates = self._extract_order_option_candidates(pending)

        selected_from_response = re.search(
            r"selected(?:\s+the)?\s+['\"]([^'\"]+)['\"]",
            str(response_text or ""),
            flags=re.IGNORECASE,
        )
        if selected_from_response:
            item = str(selected_from_response.group(1) or "").strip()
            if item:
                updates["order_item"] = item

        explicit_item = ""
        if current != "collect_order_addons":
            stripped_yes = self._strip_leading_yes(msg)
            if stripped_yes:
                explicit_item = stripped_yes
            else:
                patterns = (
                    r"\bi want\s+(.+)$",
                    r"\border\s+(.+)$",
                    r"\badd\s+(.+)$",
                    r"\bget me\s+(.+)$",
                )
                for pattern in patterns:
                    match = re.search(pattern, msg, flags=re.IGNORECASE)
                    if match:
                        explicit_item = str(match.group(1) or "").strip()
                        break

            if (
                not explicit_item
                and current in {"collect_order_item", "order_food"}
                and msg
                and not self._looks_like_information_query(msg_lower)
                and "?" not in msg
            ):
                explicit_item = msg

        if explicit_item:
            cleaned_item = explicit_item.strip(" .")
            cleaned_item = re.sub(r"^(?:a|an|the)\s+", "", cleaned_item, flags=re.IGNORECASE).strip()
            generic_tokens = {
                "something",
                "anything",
                "food",
                "menu",
                "drink",
                "drinks",
                "yes",
                "no",
                "confirm",
            }
            cleaned_norm = self._normalize_order_item_value(cleaned_item)
            if cleaned_item and cleaned_norm not in generic_tokens and re.search(r"[a-zA-Z]", cleaned_item):
                resolved_candidate_item = self._resolve_order_item_candidate_reference(
                    text=cleaned_item,
                    option_candidates=option_candidates,
                )
                if resolved_candidate_item:
                    updates["order_item"] = resolved_candidate_item
                elif self._is_ambiguous_short_reference_phrase(cleaned_item):
                    if len(option_candidates) == 1:
                        updates["order_item"] = option_candidates[0]
                else:
                    updates["order_item"] = cleaned_item

        guest_match = re.search(r"\bfor\s+(\d{1,2})\s*(?:guests?|people|persons|pax)\b", msg_lower)
        if guest_match:
            try:
                guests = int(guest_match.group(1))
                if 1 <= guests <= 20:
                    updates["order_guest_count"] = guests
            except (TypeError, ValueError):
                pass

        qty_match = re.search(
            r"\b(?:qty|quantity|x)?\s*(\d{1,2})\s*(?:x|qty|quantity|plate|plates|portion|portions|pcs|piece|pieces)?\b",
            msg_lower,
        )
        if qty_match:
            try:
                qty = int(qty_match.group(1))
                if 1 <= qty <= 20:
                    updates["order_quantity"] = qty
            except (TypeError, ValueError):
                pass

        if re.fullmatch(r"\d{1,2}", msg_lower):
            try:
                qty = int(msg_lower)
                if 1 <= qty <= 20:
                    updates["order_quantity"] = qty
            except (TypeError, ValueError):
                pass

        binary = self._detect_simple_binary_reply(msg)
        if current == "collect_order_addons":
            if binary == IntentType.CONFIRMATION_NO or any(
                token in msg_lower for token in ("nothing else", "no more", "that's all", "thats all")
            ):
                updates["order_addons_choice"] = "no"
            elif binary == IntentType.CONFIRMATION_YES:
                extra = self._strip_leading_yes(msg)
                if extra:
                    updates["order_additional_request"] = extra
                    updates["order_addons_choice"] = "yes"
            elif msg and msg_lower not in {"yes", "no", "cancel"}:
                updates["order_additional_request"] = msg
                updates["order_addons_choice"] = "yes"

        if str(updates.get("order_item") or "").strip():
            updates["order_option_candidates"] = []

        # Carry forward previously selected item if model didn't include it.
        if "order_item" not in updates:
            if existing_item:
                updates["order_item"] = existing_item

        return updates

    def _resolve_order_item_candidate_reference(
        self,
        *,
        text: str,
        option_candidates: list[str],
    ) -> str:
        candidates = [str(item).strip() for item in option_candidates if str(item).strip()]
        if not candidates:
            return ""

        normalized_text = self._normalize_order_item_value(text)
        if not normalized_text:
            return ""

        if normalized_text.isdigit():
            try:
                idx = int(normalized_text)
                if 1 <= idx <= len(candidates):
                    return candidates[idx - 1]
            except (TypeError, ValueError):
                pass

        normalized_candidate_map = {
            self._normalize_order_item_value(candidate): candidate
            for candidate in candidates
        }
        if normalized_text in normalized_candidate_map:
            return normalized_candidate_map[normalized_text]

        best_match = ""
        best_score = 0.0
        for candidate in candidates:
            candidate_norm = self._normalize_order_item_value(candidate)
            if not candidate_norm:
                continue
            score = SequenceMatcher(a=normalized_text, b=candidate_norm).ratio()
            if score > best_score:
                best_score = score
                best_match = candidate
        if best_match and best_score >= 0.75:
            return best_match
        return ""

    @staticmethod
    def _is_ambiguous_short_reference_phrase(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not normalized:
            return False
        if "?" in normalized:
            return False
        if re.search(r"\d", normalized):
            return False
        tokens = re.findall(r"[a-z]+", normalized)
        if not tokens:
            return False
        return len(tokens) <= 2 and len(normalized) <= 20

    @staticmethod
    def _extract_order_option_candidates(pending_data: dict[str, Any]) -> list[str]:
        pending = pending_data if isinstance(pending_data, dict) else {}
        raw_candidates = pending.get("order_option_candidates")
        if not isinstance(raw_candidates, list):
            return []
        seen: set[str] = set()
        parsed: list[str] = []
        for entry in raw_candidates:
            text = re.sub(r"\s+", " ", str(entry or "").strip())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            parsed.append(text)
        return parsed

    @staticmethod
    def _extract_order_option_candidates_from_response(response_text: str) -> list[str]:
        lines = str(response_text or "").splitlines()
        seen: set[str] = set()
        options: list[str] = []
        for line in lines:
            stripped = str(line or "").strip()
            if not stripped.startswith("-"):
                continue
            candidate = stripped.lstrip("-").strip()
            candidate = re.sub(
                r"\s*-\s*(?:rs\.?|₹)\s*[\d.,/()a-zA-Z-]+$",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip()
            if not candidate:
                continue
            if not re.search(r"[a-zA-Z]", candidate):
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            options.append(candidate)
        return options

    def _extract_order_item_name(self, pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        return self._first_non_empty(
            pending.get("order_item"),
            pending.get("selected_item"),
            pending.get("item_name"),
            pending.get("dish_name"),
            pending.get("requested_item"),
        )

    def _extract_order_quantity_value(self, pending_data: dict[str, Any]) -> Optional[int]:
        pending = pending_data if isinstance(pending_data, dict) else {}
        raw = self._first_non_empty(
            pending.get("order_quantity"),
            pending.get("quantity"),
        )
        if not raw:
            return None
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return None
        if value < 1 or value > 20:
            return None
        return value

    @staticmethod
    def _extract_order_addons_choice(pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        value = str(pending.get("order_addons_choice") or "").strip().lower()
        if value in {"yes", "no"}:
            return value
        return ""

    def _build_order_quantity_prompt(self, item_name: str) -> str:
        if item_name:
            return (
                f"Great choice: {item_name}. "
                "How many portions would you like? "
                "If this is for a group, you can also share number of guests."
            )
        return "How many portions would you like to order? You can also share number of guests."

    def _build_order_addons_prompt(self, item_name: str, quantity: Optional[int]) -> str:
        item_text = item_name or "this item"
        qty_text = f"{quantity}" if quantity is not None else "1"
        return (
            f"Got it. I have {qty_text} x {item_text}. "
            "Would you like to add anything else or include a drink? (yes/no)"
        )

    def _build_order_review_prompt(self, pending_data: dict[str, Any]) -> str:
        item_name = self._extract_order_item_name(pending_data) or "your selected item"
        quantity = self._extract_order_quantity_value(pending_data)
        quantity_text = f"{quantity}" if quantity is not None else "1"
        extra = str((pending_data or {}).get("order_additional_request") or "").strip()
        if extra:
            summary = f"{quantity_text} x {item_name}, plus {extra}"
        else:
            summary = f"{quantity_text} x {item_name}"
        return f"Please review your order: {summary}. Would you like to confirm this order?"

    def _build_room_booking_review_prompt(self, pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("service_name"),
            "your selected room",
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = "your selected room"
        check_in = self._first_non_empty(
            pending.get("check_in"),
            pending.get("stay_checkin_date"),
            pending.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            pending.get("check_out"),
            pending.get("stay_checkout_date"),
            pending.get("checkout_date"),
        )
        guests = self._first_non_empty(
            pending.get("guest_count"),
            pending.get("party_size"),
            pending.get("guests"),
        )

        parts: list[str] = [room_type]
        if check_in and check_out:
            parts.append(f"from {check_in} to {check_out}")
        elif self._first_non_empty(pending.get("stay_date_range")):
            parts.append(f"for {self._first_non_empty(pending.get('stay_date_range'))}")
        if guests:
            parts.append(f"for {guests} guests")

        summary = " ".join(part for part in parts if str(part or "").strip()).strip()
        if not summary:
            summary = "your room booking request"
        return (
            f"Please review your room booking request: {summary}. "
            "Would you like to confirm this booking?"
        )

    def _build_room_booking_confirmation_success_response(self, pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("service_name"),
            "your selected room",
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = "your selected room"
        check_in = self._first_non_empty(
            pending.get("check_in"),
            pending.get("stay_checkin_date"),
            pending.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            pending.get("check_out"),
            pending.get("stay_checkout_date"),
            pending.get("checkout_date"),
        )
        guests = self._first_non_empty(
            pending.get("guest_count"),
            pending.get("party_size"),
            pending.get("guests"),
        )

        details: list[str] = [room_type]
        if check_in and check_out:
            details.append(f"from {check_in} to {check_out}")
        elif self._first_non_empty(pending.get("stay_date_range")):
            details.append(f"for {self._first_non_empty(pending.get('stay_date_range'))}")
        if guests:
            details.append(f"for {guests} guests")

        details_text = " ".join(part for part in details if str(part or "").strip()).strip()
        if not details_text:
            details_text = "your requested stay"
        return (
            f"Your room booking has been confirmed: {details_text}. "
            "You'll receive a confirmation shortly. Is there anything else I can help you with?"
        )

    @staticmethod
    def _looks_like_phase_unavailable_response(response_text: str) -> bool:
        text = re.sub(r"\s+", " ", str(response_text or "").strip().lower())
        if not text:
            return False
        markers = (
            "not available for",
            "available in during stay phase",
            "available in pre checkin phase",
            "available in post checkout phase",
            "available after check-in",
            "available after check in",
            "cannot be pre-booked",
            "can't be pre-booked",
            "can only be requested after check-in",
            "action requests are available only for",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_order_options_followup(
        message: str,
        item_name: str,
        current_pending_action: str | None,
    ) -> bool:
        pending = str(current_pending_action or "").strip().lower()
        if pending not in {"collect_order_quantity", "collect_order_item", "order_food"}:
            return False

        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False

        option_markers = (
            "which",
            "what",
            "show",
            "list",
            "options",
            "available",
            "do you have",
            "do u have",
            "do u hv",
            "more",
        )
        if not any(marker in msg for marker in option_markers):
            return False

        normalized_item = re.sub(r"\s+", " ", str(item_name or "").strip().lower())
        normalized_item = re.sub(r"^(?:a|an|the)\s+", "", normalized_item).strip()
        if normalized_item:
            item_tokens = [token for token in re.findall(r"[a-z0-9]+", normalized_item) if len(token) >= 3]
            if item_tokens and any(token in msg for token in item_tokens):
                return True

        return "options" in msg or "available" in msg or msg.startswith("which ") or msg.startswith("what ")

    @staticmethod
    def _derive_order_category_hint(message: str, item_name: str) -> str:
        normalized_item = re.sub(r"\s+", " ", str(item_name or "").strip().lower())
        normalized_item = re.sub(r"^(?:a|an|the)\s+", "", normalized_item).strip(" .")
        if normalized_item:
            return normalized_item

        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return ""

        capture_patterns = (
            r"(?:which|what)\s+([a-z][a-z0-9\s-]{2,40})$",
            r"(?:show|list)\s+([a-z][a-z0-9\s-]{2,40})\s+(?:options|items)?$",
            r"(?:options|items)\s+for\s+([a-z][a-z0-9\s-]{2,40})$",
        )
        for pattern in capture_patterns:
            match = re.search(pattern, msg, flags=re.IGNORECASE)
            if not match:
                continue
            hint = re.sub(r"\s+", " ", str(match.group(1) or "").strip().lower())
            hint = re.sub(r"\b(?:options?|items?|menu)\b", "", hint).strip(" -.")
            if hint:
                return hint

        tokens = [token for token in re.findall(r"[a-z0-9]+", msg) if len(token) >= 3]
        if tokens:
            return " ".join(tokens[:2]).strip()
        return ""

    async def _build_order_options_list_response(
        self,
        category_hint: str,
        capabilities_summary: dict[str, Any],
        db_session=None,
        hotel_code: str | None = None,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> Optional[str]:
        hint = re.sub(r"\s+", " ", str(category_hint or "").strip().lower())
        hint = re.sub(r"^(?:a|an|the)\s+", "", hint).strip(" .")
        if not hint:
            return None

        option_map: dict[str, str] = {}
        option_display_map: dict[str, str] = {}

        def _add_option(name: str, price_text: str = "") -> None:
            option_name = re.sub(r"\s+", " ", str(name or "").strip())
            if not option_name:
                return
            key = option_name.lower()
            if key in option_map:
                if not option_map[key] and price_text:
                    option_map[key] = price_text
                return
            option_map[key] = price_text
            option_display_map[key] = option_name

        # Primary source: runtime menu DB.
        if db_session is not None:
            from sqlalchemy import or_, select
            from models.database import Hotel, MenuItem, Restaurant

            hotel_id = capabilities_summary.get("hotel_id")
            if hotel_id is None:
                hotel_name = str(capabilities_summary.get("hotel_name") or "").strip()
                if hotel_name:
                    hotel_row = (
                        await db_session.execute(select(Hotel).where(Hotel.name.ilike(f"%{hotel_name}%")))
                    ).scalar_one_or_none()
                    if hotel_row is not None:
                        hotel_id = hotel_row.id
            if hotel_id is None:
                hotel_row = (
                    await db_session.execute(select(Hotel).where(Hotel.is_active == True).limit(1))  # noqa: E712
                ).scalar_one_or_none()
                if hotel_row is not None:
                    hotel_id = hotel_row.id

            if hotel_id is not None:
                stmt = (
                    select(MenuItem)
                    .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
                    .where(
                        Restaurant.hotel_id == hotel_id,
                        Restaurant.is_active == True,  # noqa: E712
                        MenuItem.is_available == True,  # noqa: E712
                    )
                )

                tokens = [token for token in re.findall(r"[a-z0-9]+", hint) if len(token) >= 3][:5]
                if tokens:
                    clauses = []
                    for token in tokens:
                        like_value = f"%{token}%"
                        clauses.extend(
                            [
                                MenuItem.name.ilike(like_value),
                                MenuItem.category.ilike(like_value),
                                MenuItem.description.ilike(like_value),
                            ]
                        )
                    stmt = stmt.where(or_(*clauses))

                rows = list((await db_session.execute(stmt)).scalars().all())
                for row in sorted(rows, key=lambda item: str(item.name or "").lower()):
                    name = str(row.name or "").strip()
                    if not name:
                        continue
                    price_text = ""
                    try:
                        price_text = f"Rs.{float(row.price):.0f}"
                    except (TypeError, ValueError):
                        price_text = ""
                    _add_option(name, price_text)

        # Secondary source: KB-wide extraction via LLM for broader semantic coverage.
        if len(option_map) < 4 and settings.openai_api_key:
            tenant_id = str(hotel_code or "default").strip() or "default"
            kb_text, _ = full_kb_llm_service._load_full_kb_text(tenant_id=tenant_id)
            if kb_text:
                phase_label = str(selected_phase_name or "").strip() or (
                    str(selected_phase_id or "").replace("_", " ").title()
                )
                phase_id = str(selected_phase_id or "").strip() or "unknown"
                llm_json: dict[str, Any] = {}
                try:
                    llm_json = await llm_client.chat_with_json(
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Extract menu options strictly from KB content.\n"
                                    f"Selected user journey phase: {phase_label} ({phase_id}).\n"
                                    "Return complete options for the requested category.\n"
                                    "Include items where category is explicit or clearly implied by item/description/section.\n"
                                    "No invented items. Return only JSON: "
                                    "{\"options\":[{\"name\":\"...\",\"price\":\"...\"}]}"
                                ),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "category_hint": hint,
                                        "kb_content": kb_text,
                                        "existing_options": list(option_display_map.values()),
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        temperature=0.0,
                    )
                except Exception:
                    llm_json = {}

                for name, price in self._extract_option_entries_from_llm_json(llm_json):
                    _add_option(name, price)

        if not option_map:
            return None

        ordered_names = sorted(option_map.keys())
        lines = []
        for key in ordered_names:
            display_name = option_display_map.get(key) or key.title()
            price_text = option_map.get(key) or ""
            if price_text:
                lines.append(f"- {display_name} - {price_text}")
            else:
                lines.append(f"- {display_name}")

        return (
            f"Here are all available {hint} options:\n\n"
            + "\n".join(lines)
            + "\n\nPlease tell me the exact item you'd like to order."
        )

    @staticmethod
    def _extract_option_entries_from_llm_json(llm_json: dict[str, Any]) -> list[tuple[str, str]]:
        if not isinstance(llm_json, dict):
            return []
        raw_options = llm_json.get("options")
        if not isinstance(raw_options, list):
            raw_options = llm_json.get("items")
        if not isinstance(raw_options, list):
            return []

        parsed: list[tuple[str, str]] = []
        for item in raw_options:
            if isinstance(item, str):
                name = re.sub(r"\s+", " ", item).strip()
                if name:
                    parsed.append((name, ""))
                continue
            if not isinstance(item, dict):
                continue
            name = re.sub(r"\s+", " ", str(item.get("name") or item.get("item_name") or "").strip())
            if not name:
                continue
            price_raw = str(item.get("price") or item.get("price_inr") or "").strip()
            price_text = ""
            if price_raw:
                price_compact = re.sub(r"\s+", "", price_raw)
                if not price_compact.lower().startswith(("rs.", "rs", "₹")):
                    price_text = f"Rs.{price_compact}"
                else:
                    price_text = price_raw.replace("₹", "Rs.")
            parsed.append((name, price_text))
        return parsed

    @staticmethod
    def _normalize_order_item_value(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return ""
        text = re.sub(r"^(?:a|an|the)\s+", "", text).strip()
        text = re.sub(r"[^a-z0-9\s]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    async def _is_specific_order_item_from_menu(
        self,
        *,
        item_name: str,
        capabilities_summary: dict[str, Any],
        db_session=None,
    ) -> tuple[bool, bool]:
        normalized_item = self._normalize_order_item_value(item_name)
        if not normalized_item:
            return (False, False)
        if db_session is None:
            return (False, False)

        from sqlalchemy import or_, select
        from models.database import Hotel, MenuItem, Restaurant

        hotel_id = capabilities_summary.get("hotel_id")
        if hotel_id is None:
            hotel_name = str(capabilities_summary.get("hotel_name") or "").strip()
            if hotel_name:
                hotel_row = (
                    await db_session.execute(select(Hotel).where(Hotel.name.ilike(f"%{hotel_name}%")))
                ).scalar_one_or_none()
                if hotel_row is not None:
                    hotel_id = hotel_row.id
        if hotel_id is None:
            hotel_row = (
                await db_session.execute(select(Hotel).where(Hotel.is_active == True).limit(1))  # noqa: E712
            ).scalar_one_or_none()
            if hotel_row is not None:
                hotel_id = hotel_row.id
        if hotel_id is None:
            return (False, False)

        tokens = [token for token in re.findall(r"[a-z0-9]+", normalized_item) if len(token) >= 2][:5]
        stmt = (
            select(MenuItem)
            .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
            .where(
                Restaurant.hotel_id == hotel_id,
                Restaurant.is_active == True,  # noqa: E712
                MenuItem.is_available == True,  # noqa: E712
            )
        )
        if tokens:
            clauses = []
            for token in tokens:
                like_value = f"%{token}%"
                clauses.extend(
                    [
                        MenuItem.name.ilike(like_value),
                        MenuItem.category.ilike(like_value),
                        MenuItem.description.ilike(like_value),
                    ]
                )
            stmt = stmt.where(or_(*clauses))
        rows = list((await db_session.execute(stmt)).scalars().all())
        if not rows:
            return (False, False)

        candidate_names: list[str] = []
        seen: set[str] = set()
        for row in rows:
            name = str(getattr(row, "name", "") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            candidate_names.append(name)
        if not candidate_names:
            return (False, False)

        normalized_candidates = [self._normalize_order_item_value(name) for name in candidate_names]
        if normalized_item in normalized_candidates:
            return (True, True)
        if len(normalized_candidates) == 1 and SequenceMatcher(
            a=normalized_item,
            b=normalized_candidates[0],
        ).ratio() >= 0.9:
            return (True, True)
        return (False, True)

    async def _build_order_item_disambiguation_response(
        self,
        *,
        item_name: str,
        capabilities_summary: dict[str, Any],
        db_session=None,
        hotel_code: str | None = None,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> Optional[str]:
        normalized_item = self._normalize_order_item_value(item_name)
        if not normalized_item:
            return None

        item_is_specific, menu_available = await self._is_specific_order_item_from_menu(
            item_name=item_name,
            capabilities_summary=capabilities_summary,
            db_session=db_session,
        )
        if item_is_specific:
            return None

        options_response = await self._build_order_options_list_response(
            category_hint=normalized_item,
            capabilities_summary=capabilities_summary,
            db_session=db_session,
            hotel_code=hotel_code,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
        )
        if options_response:
            return options_response
        if menu_available:
            return (
                f"I found multiple menu options for {normalized_item}. "
                "Please share the exact item name from the menu."
            )
        return None

    @staticmethod
    def _first_non_empty(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _enrich_pending_with_known_facts(
        self,
        *,
        pending_data: dict[str, Any] | None,
        context: ConversationContext,
        memory_snapshot: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = dict(pending_data or {}) if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        integration = ticketing_service.get_integration_context(context)
        facts = {}
        if isinstance(memory_snapshot, dict):
            candidate_facts = memory_snapshot.get("facts", {})
            if isinstance(candidate_facts, dict):
                facts = candidate_facts
        if not facts:
            facts = self._memory_facts_from_context(context)

        known_values = {
            "guest_name": self._first_non_empty(
                pending.get("guest_name"),
                entity_map.get("guest_name"),
                context.guest_name,
                integration.get("guest_name"),
                facts.get("guest_name"),
            ),
            "guest_phone": self._first_non_empty(
                pending.get("guest_phone"),
                entity_map.get("guest_phone"),
                context.guest_phone,
                integration.get("guest_phone"),
                integration.get("wa_number"),
                facts.get("guest_phone"),
            ),
            "room_number": self._first_non_empty(
                pending.get("room_number"),
                entity_map.get("room_number"),
                context.room_number,
                integration.get("room_number"),
                facts.get("room_number"),
            ),
            "stay_checkin_date": self._first_non_empty(
                pending.get("stay_checkin_date"),
                pending.get("check_in"),
                entity_map.get("stay_checkin_date"),
                entity_map.get("check_in"),
                integration.get("stay_checkin_date"),
                integration.get("check_in"),
                facts.get("stay_checkin_date"),
                facts.get("check_in"),
                facts.get("checkin_date"),
            ),
            "stay_checkout_date": self._first_non_empty(
                pending.get("stay_checkout_date"),
                pending.get("check_out"),
                entity_map.get("stay_checkout_date"),
                entity_map.get("check_out"),
                integration.get("stay_checkout_date"),
                integration.get("check_out"),
                facts.get("stay_checkout_date"),
                facts.get("check_out"),
                facts.get("checkout_date"),
            ),
            "stay_date_range": self._first_non_empty(
                pending.get("stay_date_range"),
                entity_map.get("stay_date_range"),
                integration.get("stay_date_range"),
                facts.get("stay_date_range"),
            ),
            "booking_ref": self._first_non_empty(
                pending.get("booking_ref"),
                entity_map.get("booking_ref"),
                integration.get("booking_ref"),
                facts.get("last_booking_ref"),
            ),
        }

        for key, value in known_values.items():
            if value and not str(pending.get(key) or "").strip():
                pending[key] = value

        if str(pending.get("stay_checkin_date") or "").strip() and not str(pending.get("check_in") or "").strip():
            pending["check_in"] = str(pending.get("stay_checkin_date") or "").strip()
        if str(pending.get("stay_checkout_date") or "").strip() and not str(pending.get("check_out") or "").strip():
            pending["check_out"] = str(pending.get("stay_checkout_date") or "").strip()

        return pending

    def _missing_room_booking_fields(
        self,
        pending_data: dict[str, Any],
        memory_facts: dict[str, Any],
    ) -> list[str]:
        pending = pending_data if isinstance(pending_data, dict) else {}
        facts = memory_facts if isinstance(memory_facts, dict) else {}

        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("selected_room_type"),
            facts.get("room_type"),
            facts.get("room_name"),
            facts.get("selected_room_type"),
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = ""

        party_size = self._first_non_empty(
            pending.get("party_size"),
            pending.get("guest_count"),
            pending.get("guests"),
            facts.get("party_size"),
            facts.get("guest_count"),
            facts.get("guests"),
        )
        checkin_date = self._first_non_empty(
            pending.get("stay_checkin_date"),
            pending.get("check_in"),
            pending.get("checkin_date"),
            facts.get("stay_checkin_date"),
            facts.get("check_in"),
            facts.get("checkin_date"),
        )
        checkout_date = self._first_non_empty(
            pending.get("stay_checkout_date"),
            pending.get("check_out"),
            pending.get("checkout_date"),
            facts.get("stay_checkout_date"),
            facts.get("check_out"),
            facts.get("checkout_date"),
        )
        date_range = self._first_non_empty(
            pending.get("stay_date_range"),
            facts.get("stay_date_range"),
            pending.get("booking_day"),
            facts.get("booking_day"),
        )

        missing: list[str] = []
        if not room_type:
            missing.append("room_type")
        if not party_size:
            missing.append("guest_count")
        if not checkin_date and not date_range:
            missing.append("checkin_date")
        if not checkout_date and not date_range:
            missing.append("checkout_date")
        return missing

    def _build_room_booking_missing_details_prompt(
        self,
        missing_fields: list[str],
        pending_data: dict[str, Any],
    ) -> str:
        room_type = self._first_non_empty(
            pending_data.get("room_type"),
            pending_data.get("room_name"),
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = ""
        parts: list[str] = []
        requested_room_type = self._first_non_empty(
            pending_data.get("requested_room_type"),
        )
        room_type_candidates_raw = pending_data.get("room_type_candidates")
        room_type_candidates = [
            str(candidate or "").strip()
            for candidate in (room_type_candidates_raw if isinstance(room_type_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        if "room_type" in missing_fields and requested_room_type and room_type_candidates:
            options = ", ".join(room_type_candidates[:8])
            parts.append(
                f'I could not match "{requested_room_type}" to our available room types ({options}).'
            )
        if room_type:
            parts.append(f"I can proceed with {room_type}.")
        else:
            parts.append("I can help with your room booking.")

        asks: list[str] = []
        if "room_type" in missing_fields:
            asks.append("preferred room type")
        if "guest_count" in missing_fields:
            asks.append("number of guests")
        if "checkin_date" in missing_fields:
            asks.append("check-in date")
        if "checkout_date" in missing_fields:
            asks.append("check-out date")

        if asks:
            parts.append("Please share your " + ", ".join(asks) + ".")
        else:
            parts.append("Please share your check-in and check-out dates.")

        return " ".join(parts).strip()

    @staticmethod
    def _looks_like_availability_request(message: str) -> bool:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        markers = (
            "availability",
            "available",
            "check availability",
            "check room availability",
            "room available",
            "is it available",
        )
        return any(marker in msg for marker in markers)

    @staticmethod
    def _looks_like_room_options_information_query(message: str) -> bool:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        ordinal_reference = bool(
            re.search(
                r"\b(?:the\s+)?(?:1st|2nd|3rd|[4-9]th|10th|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last)\s+"
                r"(?:one|option|room|type|suite|villa|residence)\b",
                msg,
            )
        )
        room_markers = (
            "room",
            "rooms",
            "room type",
            "room types",
            "suite",
            "suites",
            "stay options",
        )
        info_markers = (
            "what",
            "which",
            "show",
            "list",
            "options",
            "types",
            "available",
            "have",
            "details",
            "tell me about",
        )
        if not any(marker in msg for marker in room_markers):
            if ordinal_reference and any(marker in msg for marker in ("tell me about", "more about", "details", "detail", "what about")):
                return True
            return False
        if "?" in msg:
            return True
        return any(marker in msg for marker in info_markers)

    @staticmethod
    def _extract_room_option_index_reference(message: str) -> int:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return 0

        token_to_index = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
            "sixth": 6,
            "seventh": 7,
            "eighth": 8,
            "ninth": 9,
            "tenth": 10,
            "1st": 1,
            "2nd": 2,
            "3rd": 3,
            "4th": 4,
            "5th": 5,
            "6th": 6,
            "7th": 7,
            "8th": 8,
            "9th": 9,
            "10th": 10,
        }

        numbered = re.search(
            r"\b(?:the\s+)?(?P<idx>\d{1,2})(?:st|nd|rd|th)\s+(?:one|option|room|type|suite|villa|residence)\b",
            msg,
        )
        if numbered:
            try:
                index = int(numbered.group("idx"))
            except (TypeError, ValueError):
                index = 0
            return index if index > 0 else 0

        worded = re.search(
            r"\b(?:the\s+)?(?P<idx>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+"
            r"(?:one|option|room|type|suite|villa|residence)\b",
            msg,
        )
        if worded:
            return token_to_index.get(str(worded.group("idx") or "").strip().lower(), 0)

        return 0

    @classmethod
    def _extract_structured_room_option_entries(cls, content: str) -> list[dict[str, str]]:
        text = str(content or "")
        if not text:
            return []

        line_pattern = re.compile(
            r"^\s*(?:[-*]|\d+[.)])\s*(?:\*\*)?(?P<label>[a-z0-9&'/-][^:\n]{1,120}?(?:room|suite|villa|residence))"
            r"(?:\*\*)?\s*[:\-]\s*(?P<detail>[^\n]+?)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        inline_pattern = re.compile(
            r"(?:^|\s)(?:\d+[.)])\s*(?:\*\*)?(?P<label>[a-z0-9&'/-][^:]{1,120}?(?:room|suite|villa|residence))"
            r"(?:\*\*)?\s*[:\-]\s*(?P<detail>.+?)(?=(?:\s+\d+[.)]\s+(?:\*\*)?[a-z0-9]|$))",
            re.IGNORECASE,
        )

        matches = list(line_pattern.finditer(text))
        if not matches:
            matches = list(inline_pattern.finditer(text))
        if not matches:
            return []

        entries: list[dict[str, str]] = []
        for match in matches:
            label = re.sub(r"\s+", " ", str(match.group("label") or "").strip(" .,:;!?*-"))
            detail = re.sub(r"\s+", " ", str(match.group("detail") or "").strip(" .,:;!?*-"))
            if not label or not detail:
                continue
            if cls._is_generic_room_reference(label):
                continue
            entries.append({"label": label, "detail": detail})
        return entries

    @classmethod
    def _find_room_option_entry(
        cls,
        entries: list[dict[str, str]],
        *,
        room_type: str = "",
        option_index: int = 0,
    ) -> dict[str, str]:
        if not isinstance(entries, list) or not entries:
            return {}

        if option_index > 0 and option_index <= len(entries):
            entry = entries[option_index - 1]
            if isinstance(entry, dict):
                return entry

        room_norm = cls._normalize_room_type_label(room_type)
        if room_norm:
            room_tokens = set(cls._room_type_tokens(room_norm))
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("label") or "").strip()
                if not label:
                    continue
                label_norm = cls._normalize_room_type_label(label)
                if not label_norm:
                    continue
                if room_norm == label_norm:
                    return entry
                if room_norm in label_norm or label_norm in room_norm:
                    return entry
                label_tokens = set(cls._room_type_tokens(label_norm))
                if room_tokens and label_tokens and room_tokens.issubset(label_tokens):
                    return entry
        return {}

    def _resolve_room_reference_details(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any],
    ) -> dict[str, Any]:
        persisted_candidates_raw = pending_data.get("room_type_candidates")
        persisted_candidates = [
            str(candidate or "").strip()
            for candidate in (persisted_candidates_raw if isinstance(persisted_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        room_candidates = self._merge_room_type_candidates(
            persisted_candidates,
            self._extract_room_type_candidates_from_context_messages(context),
        )
        msg_lower = str(message or "").strip().lower()
        room_type = self._match_room_type_candidate_from_message(msg_lower, room_candidates)
        option_index = self._extract_room_option_index_reference(msg_lower)
        if not room_type and option_index > 0 and option_index <= len(room_candidates):
            room_type = room_candidates[option_index - 1]

        detail_entry: dict[str, str] = {}
        for msg in reversed(context.messages):
            if msg.role != MessageRole.ASSISTANT:
                continue
            content = str(msg.content or "").strip()
            if not content:
                continue
            entries = self._extract_structured_room_option_entries(content)
            if not entries:
                continue
            detail_entry = self._find_room_option_entry(
                entries,
                room_type=room_type,
                option_index=option_index,
            )
            if detail_entry:
                break

        resolved_type = str(detail_entry.get("label") or room_type or "").strip()
        resolved_detail = str(detail_entry.get("detail") or "").strip()
        return {
            "room_type": resolved_type,
            "detail": resolved_detail,
            "option_index": option_index,
            "candidates": room_candidates,
        }

    @staticmethod
    def _is_room_choice_loop_response(response_text: str) -> bool:
        msg = re.sub(r"\s+", " ", str(response_text or "").strip().lower())
        if not msg:
            return False
        return (
            "would you like to view" in msg
            and "check" in msg
            and "availability" in msg
        )

    def _build_room_availability_forward_prompt(self, pending_data: dict[str, Any]) -> str:
        check_in = self._first_non_empty(
            pending_data.get("check_in"),
            pending_data.get("stay_checkin_date"),
            pending_data.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            pending_data.get("check_out"),
            pending_data.get("stay_checkout_date"),
            pending_data.get("checkout_date"),
        )
        guests = self._first_non_empty(
            pending_data.get("guest_count"),
            pending_data.get("party_size"),
            pending_data.get("guests"),
        )
        room_type = self._first_non_empty(
            pending_data.get("room_type"),
            pending_data.get("room_name"),
        )

        details: list[str] = []
        if room_type:
            details.append(f"{room_type}")
        if check_in and check_out:
            details.append(f"from {check_in} to {check_out}")
        if guests:
            details.append(f"for {guests} guests")
        detail_text = " ".join(details).strip()

        base = (
            "I can check availability by forwarding this request to our staff for confirmation."
        )
        if detail_text:
            base = f"{base} I have {detail_text}."
        return base + " Please type \"yes confirm\" to proceed."

    def _extract_room_booking_slot_updates(
        self,
        message: str,
        current_pending_action: str | None,
        state: ConversationState,
        pending_data: dict[str, Any],
        memory_facts: dict[str, Any],
        conversation_context: ConversationContext | None = None,
    ) -> dict[str, Any]:
        """
        Deterministically extract room-booking slot updates from compact user
        replies so context doesn't depend only on model JSON fidelity.
        """
        pending = pending_data if isinstance(pending_data, dict) else {}
        facts = memory_facts if isinstance(memory_facts, dict) else {}
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if not msg_lower:
            return {}

        room_context = self._is_room_detail_collection_context(
            current_pending_action=current_pending_action,
            state=state,
            pending_data=pending,
            memory_facts=facts,
            message=msg_lower,
        )
        if not room_context:
            return {}

        month_map = {
            "jan": "Jan", "january": "Jan",
            "feb": "Feb", "february": "Feb",
            "mar": "Mar", "march": "Mar",
            "apr": "Apr", "april": "Apr",
            "may": "May",
            "jun": "Jun", "june": "Jun",
            "jul": "Jul", "july": "Jul",
            "aug": "Aug", "august": "Aug",
            "sep": "Sep", "sept": "Sep", "september": "Sep",
            "oct": "Oct", "october": "Oct",
            "nov": "Nov", "november": "Nov",
            "dec": "Dec", "december": "Dec",
        }
        month_token = (
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        )

        def _month_norm(token: str) -> str:
            return month_map.get(str(token or "").strip().lower(), "")

        def _format(month: str, day: str) -> str:
            m = _month_norm(month)
            try:
                d = int(str(day or "").strip())
            except (TypeError, ValueError):
                return ""
            if not m or d < 1 or d > 31:
                return ""
            return f"{m} {d:02d}"

        updates: dict[str, Any] = {}

        range_month_first = re.search(
            rf"\b(?P<month>{month_token})\s*(?P<start>\d{{1,2}})\s*(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})(?:[,\s]+(?P<guests>\d{{1,2}}))?\b",
            msg_lower,
        )
        range_month_last = re.search(
            rf"\b(?P<start>\d{{1,2}})\s*(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})\s*(?P<month>{month_token})(?:[,\s]+(?P<guests>\d{{1,2}}))?\b",
            msg_lower,
        )

        chosen = range_month_first or range_month_last
        if chosen:
            month = str(chosen.group("month") or "")
            start = str(chosen.group("start") or "")
            end = str(chosen.group("end") or "")
            start_fmt = _format(month, start)
            end_fmt = _format(month, end)
            if start_fmt and end_fmt:
                updates["stay_checkin_date"] = start_fmt
                updates["stay_checkout_date"] = end_fmt
                updates["stay_date_range"] = f"{start_fmt} to {end_fmt}"
            guests_raw = chosen.group("guests")
            if guests_raw:
                try:
                    guests_value = int(guests_raw)
                    if 1 <= guests_value <= 20:
                        updates["guest_count"] = guests_value
                except (TypeError, ValueError):
                    pass

        guests_match = re.search(r"\b(?:for|party of)?\s*(\d{1,2})\s*(?:guests?|people|persons|pax)\b", msg_lower)
        if guests_match:
            try:
                guests_value = int(guests_match.group(1))
                if 1 <= guests_value <= 20:
                    updates["guest_count"] = guests_value
            except (TypeError, ValueError):
                pass

        # Bare numeric reply in room-detail collection generally means guest count.
        if re.fullmatch(r"\d{1,2}", msg_lower):
            known_dates = self._first_non_empty(
                pending.get("check_in"),
                pending.get("stay_checkin_date"),
                pending.get("checkin_date"),
                facts.get("check_in"),
                facts.get("stay_checkin_date"),
                facts.get("checkin_date"),
            ) and self._first_non_empty(
                pending.get("check_out"),
                pending.get("stay_checkout_date"),
                pending.get("checkout_date"),
                facts.get("check_out"),
                facts.get("stay_checkout_date"),
                facts.get("checkout_date"),
            )
            if known_dates:
                try:
                    guests_value = int(msg_lower)
                    if 1 <= guests_value <= 20:
                        updates["guest_count"] = guests_value
                except (TypeError, ValueError):
                    pass

        persisted_candidates_raw = pending.get("room_type_candidates")
        persisted_candidates = [
            str(candidate or "").strip()
            for candidate in (persisted_candidates_raw if isinstance(persisted_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        room_candidates = self._merge_room_type_candidates(
            persisted_candidates,
            self._extract_room_type_candidates_from_context_messages(conversation_context),
        )
        if room_candidates:
            updates["room_type_candidates"] = room_candidates
        matched_room = self._match_room_type_candidate_from_message(msg_lower, room_candidates)
        if matched_room:
            updates["room_type"] = matched_room

        if "room_type" not in updates:
            compact_msg = re.sub(r"[^a-z0-9 ]+", " ", msg_lower)
            cheapest_markers = ("cheapest", "lowest", "affordable", "budget", "least expensive")
            luxury_markers = ("luxury", "luxurious", "premium", "best room", "most luxurious")
            preference = ""
            if any(marker in compact_msg for marker in cheapest_markers):
                preference = "cheapest"
            elif any(marker in compact_msg for marker in luxury_markers):
                preference = "luxury"
            if preference:
                updates["room_type_preference"] = preference
                inferred_from_context = self._select_room_type_from_preference(preference, room_candidates)
                if inferred_from_context:
                    updates["room_type"] = inferred_from_context

        return updates

    @staticmethod
    def _match_room_type_candidate_from_message(msg_lower: str, candidates: list[str]) -> str:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return ""
        for candidate in candidates:
            cand_lower = str(candidate or "").strip().lower()
            if not cand_lower:
                continue
            if cand_lower in text:
                return candidate
            ratio = SequenceMatcher(a=text, b=cand_lower).ratio()
            if ratio >= 0.72:
                return candidate
        return ""

    @staticmethod
    def _normalize_room_type_label(value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[^a-z0-9\s/&'-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def _room_type_tokens(cls, value: str) -> list[str]:
        normalized = cls._normalize_room_type_label(value)
        tokens = re.findall(r"[a-z0-9]+", normalized)
        ignored = {
            "room",
            "rooms",
            "suite",
            "suites",
            "villa",
            "villas",
            "residence",
            "residences",
            "the",
            "a",
            "an",
        }
        return [token for token in tokens if token not in ignored]

    @classmethod
    def _is_generic_room_reference(cls, value: str) -> bool:
        normalized = cls._normalize_room_type_label(value)
        if not normalized:
            return True

        tokens = re.findall(r"[a-z0-9]+", normalized)
        if not tokens:
            return True

        accommodation_tokens = {
            "room",
            "rooms",
            "suite",
            "suites",
            "villa",
            "villas",
            "residence",
            "residences",
        }
        helper_tokens = {
            "i",
            "hotel",
            "hotels",
            "we",
            "our",
            "your",
            "my",
            "me",
            "us",
            "the",
            "a",
            "an",
            "of",
            "and",
            "or",
            "for",
            "to",
            "with",
            "from",
            "in",
            "at",
            "by",
            "this",
            "that",
            "these",
            "those",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "should",
            "may",
            "might",
            "must",
            "also",
            "just",
            "available",
            "availability",
            "offer",
            "offers",
            "offering",
            "variety",
            "type",
            "types",
            "option",
            "options",
            "booking",
            "book",
            "request",
            "requested",
            "selected",
            "select",
            "details",
            "detail",
            "help",
            "proceed",
            "share",
            "provide",
            "need",
            "want",
            "can",
            "could",
            "please",
            "about",
            "preferred",
            "specific",
            "any",
            "more",
            "here",
            "there",
            "are",
            "each",
            "on",
            "if",
            "you",
            "like",
            "let",
            "know",
            "which",
            "what",
            "show",
            "list",
            "tell",
            "explore",
        }
        phrase_markers = (
            "room booking",
            "book a room",
            "room request",
            "selected room",
            "room type",
            "room types",
            "room option",
            "room options",
            "variety of room",
            "preferred room",
            "specific room",
            "here are the room",
            "each room",
        )
        if any(marker in normalized for marker in phrase_markers):
            return True

        descriptor_tokens = [token for token in tokens if token not in accommodation_tokens]
        if not descriptor_tokens:
            return True

        informative = [token for token in descriptor_tokens if token not in helper_tokens]
        if not informative:
            return True
        if any(token in {"offer", "offers", "available", "availability", "variety", "booking"} for token in descriptor_tokens):
            return len(informative) < 2
        return False

    @classmethod
    def _resolve_room_type_candidate(cls, requested_label: str, candidates: list[str]) -> str:
        requested_norm = cls._normalize_room_type_label(requested_label)
        if not requested_norm or not candidates:
            return ""

        requested_tokens = set(cls._room_type_tokens(requested_norm))
        best_match = ""
        best_score = 0.0

        for candidate in candidates:
            candidate_text = str(candidate or "").strip()
            if not candidate_text:
                continue
            candidate_norm = cls._normalize_room_type_label(candidate_text)
            if not candidate_norm:
                continue

            if requested_norm == candidate_norm:
                return candidate_text
            if requested_norm in candidate_norm and len(requested_norm) >= max(6, int(len(candidate_norm) * 0.55)):
                return candidate_text
            if candidate_norm in requested_norm and len(candidate_norm) >= max(6, int(len(requested_norm) * 0.55)):
                return candidate_text

            ratio = SequenceMatcher(a=requested_norm, b=candidate_norm).ratio()
            candidate_tokens = set(cls._room_type_tokens(candidate_norm))
            if not requested_tokens:
                requested_tokens = set(re.findall(r"[a-z0-9]+", requested_norm))
            if not candidate_tokens:
                candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate_norm))
            union = requested_tokens | candidate_tokens
            overlap = (len(requested_tokens & candidate_tokens) / len(union)) if union else 0.0

            score = 0.0
            if overlap >= 0.8:
                score = max(score, 0.84 + min(0.1, overlap * 0.1))
            if overlap >= 0.6 and ratio >= 0.86:
                score = max(score, ratio)
            if ratio >= 0.9:
                score = max(score, ratio)

            if score > best_score:
                best_score = score
                best_match = candidate_text

        return best_match if best_score >= 0.86 else ""

    @classmethod
    def _merge_room_type_candidates(cls, *candidate_groups: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in candidate_groups:
            if not isinstance(group, list):
                continue
            for raw in group:
                candidate = str(raw or "").strip()
                if not candidate:
                    continue
                if cls._is_generic_room_reference(candidate):
                    continue
                key = cls._normalize_room_type_label(candidate)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(candidate)
        return merged

    @classmethod
    def _select_room_type_from_preference(cls, preference: str, candidates: list[str]) -> str:
        if not candidates:
            return ""
        pref = str(preference or "").strip().lower()
        valid_candidates = [
            str(candidate or "").strip()
            for candidate in candidates
            if str(candidate or "").strip() and not cls._is_generic_room_reference(candidate)
        ]
        if not valid_candidates:
            return ""
        if pref == "cheapest":
            return valid_candidates[0]
        if pref == "luxury":
            premium_candidates = [
                candidate
                for candidate in valid_candidates
                if any(marker in cls._normalize_room_type_label(candidate) for marker in ("suite", "villa", "residence"))
            ]
            if premium_candidates:
                return premium_candidates[-1]
            return valid_candidates[-1]
        return ""

    def _extract_room_type_candidates_from_context_messages(
        self,
        conversation_context: ConversationContext | None,
    ) -> list[str]:
        # Room type candidates are now managed by the LLM via service_knowledge
        # (extracted from KB at upload time). No regex extraction from history needed.
        return []

    @staticmethod
    def _is_bot_instruction_style_action(action_text: str) -> bool:
        text = str(action_text or "").strip().lower()
        if not text:
            return True
        prefixes = (
            "provide ",
            "specify ",
            "share ",
            "enter ",
            "type ",
            "kindly provide",
            "please provide",
            "please specify",
            "request ",
        )
        return any(text.startswith(prefix) for prefix in prefixes)

    @staticmethod
    def _looks_like_bot_question_suggestion(action_text: str) -> bool:
        text = str(action_text or "").strip().lower()
        if not text:
            return True
        if not text.endswith("?"):
            return False

        bot_question_markers = (
            "would you like",
            "what time would you like",
            "are you interested",
            "do you have a preferred",
            "do you have any preferred",
            "please let me know",
            "could you please let me know",
            "kindly let me know",
            "what is your",
            "what's your",
            "may i know your",
            "how many guests",
            "which option would you like",
            "which spa treatment are you interested in",
            "which treatment are you interested in",
            "which package are you interested in",
        )
        if any(marker in text for marker in bot_question_markers):
            return True
        return bool(
            re.search(
                r"\bwhich\b.{0,40}\b(?:treatment|therap(?:y|ies)|package|option)\b.{0,20}\bare you interested in\b",
                text,
            )
        )

    @staticmethod
    def _rewrite_bot_question_to_user_query(action_text: str) -> str:
        text = str(action_text or "").strip().lower()
        if not text:
            return ""
        if "are you interested in" in text and any(
            marker in text for marker in ("treatment", "treatments", "therapy", "therapies")
        ):
            return "Show spa treatments"
        if "are you interested in" in text and any(
            marker in text for marker in ("package", "packages")
        ):
            return "Show spa packages"
        if any(marker in text for marker in ("spa", "massage")) and any(
            marker in text for marker in ("treatment", "treatments", "therapy", "therapies")
        ):
            return "Show spa treatments"
        if any(marker in text for marker in ("spa", "massage")) and any(
            marker in text for marker in ("package", "packages")
        ):
            return "Show spa packages"
        if "therapist" in text:
            return "Show available therapists"
        if "time" in text or "preferred time" in text:
            return "Book at 6 PM"
        if "room number" in text:
            return "My room number is 305"
        if any(marker in text for marker in ("how many guests", "party size", "number of guests")):
            return "Book for 2 guests"
        if any(marker in text for marker in ("check in", "check-in", "check out", "check-out", "dates")):
            return "Share booking dates"
        return ""

    def _is_suggested_action_phase_allowed(
        self,
        *,
        action_text: str,
        pending_data: dict[str, Any],
        capabilities_summary: dict[str, Any],
    ) -> bool:
        phase_id = self._extract_phase_from_pending_data(pending_data)
        if not phase_id:
            return True
        inferred_intent = self._infer_phase_gate_transactional_intent(action_text)
        if inferred_intent is None:
            return True

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return True
        phase_services = [
            service
            for service in service_catalog
            if isinstance(service, dict)
            and bool(service.get("is_active", True))
            and self._normalize_phase_identifier(service.get("phase_id")) == phase_id
        ]
        if not phase_services:
            return True
        requested_label = self._extract_requested_service_label_for_phase_gate(action_text)
        if requested_label:
            return self._phase_services_include_requested_label(
                requested_label=requested_label,
                phase_services=phase_services,
            )
        has_spa_marker = self._has_spa_booking_marker(action_text)
        has_transport_marker = self._has_transport_booking_marker(action_text)
        room_only_booking = (
            self._looks_like_room_stay_booking_request(action_text)
            and not has_spa_marker
            and not has_transport_marker
        )
        if (
            inferred_intent == IntentType.TABLE_BOOKING
            and room_only_booking
        ):
            return self._phase_has_room_booking_support(phase_services)
        return self._phase_has_intent_compatible_service(
            intent=inferred_intent,
            phase_services=phase_services,
            strict=True,
        )

    def _finalize_user_query_suggestions(
        self,
        suggestions: list[str],
        state: ConversationState,
        intent: IntentType,
        pending_action: str | None,
        pending_data: dict[str, Any],
        capabilities_summary: dict[str, Any],
    ) -> list[str]:
        """
        Ensure UI bubbles are user-askable prompts, not bot-side instructions.
        """
        confirmation_phrase = str(
            getattr(settings, "chat_confirmation_phrase", "yes confirm")
        ).strip() or "yes confirm"
        if state == ConversationState.AWAITING_CONFIRMATION:
            return [confirmation_phrase, "cancel", "Talk to human"]

        cleaned: list[str] = []
        for item in (suggestions or []):
            text = str(item or "").strip()
            if not text:
                continue
            if self._is_bot_instruction_style_action(text):
                continue
            if self._looks_like_bot_question_suggestion(text):
                rewritten = self._rewrite_bot_question_to_user_query(text)
                if rewritten:
                    cleaned.append(rewritten)
                continue
            cleaned.append(text)

        if not cleaned:
            room_flow = self._is_room_booking_flow(
                raw_intent=intent.value,
                current_pending_action=pending_action,
                pending_action_target=pending_action,
                pending_data=pending_data if isinstance(pending_data, dict) else {},
                response_text="",
            )
            if room_flow:
                cleaned = [
                    "Show available room types",
                    "Show room amenities",
                    "Check availability for my dates",
                    "Talk to human",
                ]
            elif intent == IntentType.ORDER_FOOD:
                cleaned = [
                    "Show menu options",
                    "Suggest popular dishes",
                    "Show vegetarian options",
                    "Talk to human",
                ]
            else:
                cleaned = self._build_contextual_suggested_actions(
                    state,
                    intent,
                    pending_action,
                    capabilities_summary,
                    pending_data,
                )

        deduped: list[str] = []
        seen: set[str] = set()
        excluded_suggestion_keys = {
            "greeting",
            "knowledge query",
            "knowledge_query",
        }
        for action in cleaned:
            key = re.sub(r"\s+", " ", str(action).strip().lower())
            if not key or key in seen:
                continue
            if key in excluded_suggestion_keys:
                continue
            if not self._is_suggested_action_phase_allowed(
                action_text=str(action).strip(),
                pending_data=pending_data if isinstance(pending_data, dict) else {},
                capabilities_summary=capabilities_summary if isinstance(capabilities_summary, dict) else {},
            ):
                continue
            seen.add(key)
            deduped.append(str(action).strip())
            if len(deduped) >= 4:
                break
        return deduped or ["Ask another question", "Talk to human"]

    def _is_room_detail_collection_context(
        self,
        current_pending_action: str | None,
        state: ConversationState,
        pending_data: dict[str, Any],
        memory_facts: dict[str, Any],
        message: str,
    ) -> bool:
        pending = str(current_pending_action or "").strip().lower()
        if pending in {"collect_room_booking_details", "select_room_type", "confirm_room_booking"}:
            return True
        if state in {ConversationState.AWAITING_INFO, ConversationState.AWAITING_SELECTION, ConversationState.AWAITING_CONFIRMATION}:
            if any(
                key in (pending_data or {})
                for key in ("check_in", "check_out", "stay_checkin_date", "stay_checkout_date", "room_type", "guest_count")
            ):
                return True
            if any(
                key in (memory_facts or {})
                for key in ("check_in", "check_out", "stay_checkin_date", "stay_checkout_date", "party_size")
            ):
                return True
        room_markers = ("room", "stay", "check in", "check-out", "checkout", "checkin", "suite")
        return any(marker in message for marker in room_markers)

    def _handle_low_confidence(
        self,
        intent_result: IntentResult,
        context: ConversationContext,
    ) -> Optional[tuple[str, ConversationState]]:
        """
        Handle low-confidence intent predictions with clarify/escalate behavior.
        """
        if intent_result.intent in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO):
            return None

        if context.pending_action in _DETAIL_COLLECTION_PENDING_ACTIONS:
            # Slot-filling flows handle terse messages like "5", "202", "10 pm".
            return None

        if intent_result.confidence >= settings.intent_confidence_threshold:
            return None

        escalation_cfg = config_service.get_escalation_config()
        max_tries = int(escalation_cfg.get("max_clarification_attempts", 3))
        attempts = int(context.pending_data.get("_clarification_attempts", 0)) + 1
        context.pending_data["_clarification_attempts"] = attempts

        if attempts >= max_tries and config_service.is_capability_enabled("human_escalation"):
            escalate_msg = escalation_cfg.get(
                "escalation_message",
                "Let me connect you with our team for better assistance.",
            )
            return escalate_msg, ConversationState.ESCALATED

        return (
            "I want to make sure I understood correctly. Could you share a bit more detail so I can help you accurately?",
            ConversationState.AWAITING_INFO,
        )

    async def _dispatch_to_handler(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict,
        db_session=None,
    ) -> Optional[HandlerResult]:
        """Route to the appropriate handler. Handles confirmation intents contextually."""
        intent = intent_result.intent
        pending_action = str(context.pending_action or "").strip().lower()
        unified_complaint_intent, unified_reason, unified_source = self._resolve_unified_complaint_routing_intent(
            message=message,
            effective_intent=intent,
            current_pending_action=context.pending_action,
            pending_action_target=None,
            llm_response_text="",
            llm_ticketing_preference=None,
            next_state=context.state,
        )

        if unified_complaint_intent == IntentType.COMPLAINT:
            complaint_handler = handler_registry.get_handler(IntentType.COMPLAINT)
            if complaint_handler is not None:
                complaint_entities = (
                    dict(intent_result.entities)
                    if isinstance(intent_result.entities, dict)
                    else {}
                )
                complaint_entities.setdefault("ticketing_flow", True)
                if unified_reason:
                    complaint_entities.setdefault("ticket_route_reason", unified_reason)
                if unified_source:
                    complaint_entities.setdefault("ticket_route_source", unified_source)
                complaint_intent_result = IntentResult(
                    intent=IntentType.COMPLAINT,
                    confidence=max(0.0, min(1.0, float(intent_result.confidence or 0.0))),
                    entities=complaint_entities,
                    requires_confirmation=False,
                )
                return await complaint_handler.handle(
                    message,
                    complaint_intent_result,
                    context,
                    capabilities,
                    db_session,
                )

        # Keep room-booking flow context while answering informational room
        # follow-ups (for example: "what rooms are there?").
        if (
            self._is_room_booking_pending_action(pending_action)
            and intent not in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO)
            and self._looks_like_room_options_information_query(message)
        ):
            pending_snapshot = context.pending_data if isinstance(context.pending_data, dict) else {}
            room_reference = self._resolve_room_reference_details(
                message=message,
                context=context,
                pending_data=pending_snapshot,
            )
            referenced_room_type = str(room_reference.get("room_type") or "").strip()
            referenced_room_detail = str(room_reference.get("detail") or "").strip()

            followup_queries: list[str] = [message]
            if referenced_room_type:
                followup_queries.insert(0, f"tell me about {referenced_room_type}")

            for followup_query in followup_queries:
                faq_match = self._match_faq_bank_answer(followup_query, context, allow_pending=True)
                if faq_match is not None:
                    answer = str(faq_match.get("answer") or "").strip()
                    description = str(faq_match.get("description") or "").strip()
                    if description:
                        answer = f"{answer}\n\n{description}"
                    if answer:
                        return HandlerResult(
                            response_text=answer,
                            next_state=context.state if context.state in {
                                ConversationState.AWAITING_INFO,
                                ConversationState.AWAITING_SELECTION,
                                ConversationState.AWAITING_CONFIRMATION,
                            } else ConversationState.AWAITING_INFO,
                            pending_action=context.pending_action,
                            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                            suggested_actions=["Select room type", "Share number of guests", "Talk to human"],
                            metadata={
                                "room_booking_context_preserved": True,
                                "response_source": "faq_bank_room_followup",
                            },
                        )

                service_match = self._match_service_information_response(
                    followup_query,
                    context,
                    capabilities,
                    allow_pending=True,
                )
                if service_match is not None:
                    return HandlerResult(
                        response_text=str(service_match.get("response_text") or "").strip(),
                        next_state=context.state if context.state in {
                            ConversationState.AWAITING_INFO,
                            ConversationState.AWAITING_SELECTION,
                            ConversationState.AWAITING_CONFIRMATION,
                        } else ConversationState.AWAITING_INFO,
                        pending_action=context.pending_action,
                        pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                        suggested_actions=list(service_match.get("suggested_actions") or ["Select room type", "Share number of guests"]),
                        metadata={
                            "room_booking_context_preserved": True,
                            "response_source": "service_catalog_room_followup",
                            "service_id": str(service_match.get("service_id") or ""),
                            "service_match_type": str(service_match.get("match_type") or ""),
                        },
                    )

            if referenced_room_type and referenced_room_detail:
                response_text = (
                    f"{referenced_room_type}: {referenced_room_detail}\n\n"
                    "If you'd like to book this room, please share your number of guests, "
                    "check-in date, and check-out date."
                )
                pending_data = dict(context.pending_data) if isinstance(context.pending_data, dict) else {}
                pending_data["requested_room_type"] = referenced_room_type
                if isinstance(room_reference.get("candidates"), list):
                    pending_data["room_type_candidates"] = room_reference["candidates"]
                return HandlerResult(
                    response_text=response_text,
                    next_state=context.state if context.state in {
                        ConversationState.AWAITING_INFO,
                        ConversationState.AWAITING_SELECTION,
                        ConversationState.AWAITING_CONFIRMATION,
                    } else ConversationState.AWAITING_INFO,
                    pending_action=context.pending_action,
                    pending_data=pending_data,
                    suggested_actions=["Select this room type", "Show all room types", "Share number of guests"],
                    metadata={
                        "room_booking_context_preserved": True,
                        "response_source": "room_option_reference_followup",
                        "room_type": referenced_room_type,
                    },
                )

        room_intent_context = self._is_room_booking_intent_context(
            message=message,
            intent_result=intent_result,
            context=context,
        )
        if room_intent_context:
            return await self._handle_room_booking_intent_flow(
                message=message,
                intent_result=intent_result,
                context=context,
                capabilities=capabilities,
                db_session=db_session,
            )

        # For confirmation intents, route to the handler that owns the pending action
        if intent in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO):
            if context.pending_action == "confirm_service_request":
                service_name = str(context.pending_data.get("service_name") or "your request").strip()
                service_details = str(context.pending_data.get("service_details") or "").strip()
                if intent == IntentType.CONFIRMATION_YES:
                    details_text = f"\nDetails: {service_details}" if service_details else ""
                    return HandlerResult(
                        response_text=(
                            f"Confirmed. I've forwarded your {service_name} request to our team.{details_text}\n\n"
                            "You'll receive an update shortly."
                        ),
                        next_state=ConversationState.COMPLETED,
                        pending_action=None,
                        pending_data={},
                        suggested_actions=config_service.get_quick_actions(limit=4),
                        metadata={
                            "service_request_confirmed": True,
                            "service_name": service_name,
                            "service_details": service_details,
                        },
                    )
                return HandlerResult(
                    response_text="No problem, I won't proceed with that request.",
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data={},
                    suggested_actions=config_service.get_quick_actions(limit=4),
                    metadata={"service_request_confirmed": False, "service_name": service_name},
                )

            pending_intent = _PENDING_ACTION_TO_INTENT.get(context.pending_action)
            if pending_intent:
                handler = handler_registry.get_handler(pending_intent)
                if handler:
                    return await handler.handle(
                        message, intent_result, context, capabilities, db_session
                    )
            # No matching handler for the pending action - use simple defaults
            if intent == IntentType.CONFIRMATION_YES:
                return HandlerResult(
                    response_text=self._handle_confirmation_yes(
                        {"pending_action": context.pending_action, "pending_data": context.pending_data}
                    ),
                    next_state=ConversationState.COMPLETED,
                )
            else:
                return HandlerResult(
                    response_text="No problem, I've cancelled that. Is there anything else I can help you with?",
                    next_state=ConversationState.IDLE,
                )

        # Deterministic slot-filling follow-up routing based on pending action.
        if context.pending_action in _DETAIL_COLLECTION_PENDING_ACTIONS:
            pending_intent = _PENDING_ACTION_TO_INTENT.get(context.pending_action)
            if pending_intent:
                handler = handler_registry.get_handler(pending_intent)
                if handler:
                    return await handler.handle(
                        message, intent_result, context, capabilities, db_session
                    )

        # Transport follow-up flow (detail collection).
        msg_lower = message.lower()
        if context.pending_action == "collect_transport_details":
            from handlers.transport_handler import TransportHandler
            transport_handler = TransportHandler()
            return await transport_handler.handle(
                message, intent_result, context, capabilities, db_session
            )

        # Keyword transport routing for actionable requests only.
        if self._should_route_transport_action(msg_lower, intent_result.intent):
            from handlers.transport_handler import TransportHandler
            transport_handler = TransportHandler()
            return await transport_handler.handle(
                message, intent_result, context, capabilities, db_session
            )

        # Standard handler dispatch
        return await handler_registry.dispatch(
            intent_result, message, context, capabilities, db_session
        )

    @staticmethod
    def _is_room_booking_pending_action(action: str | None) -> bool:
        pending = str(action or "").strip().lower()
        return pending in {
            "room_booking",
            "collect_room_booking_details",
            "select_room_type",
            "confirm_room_booking",
            "confirm_room_availability_check",
        }

    @staticmethod
    def _looks_like_room_type_preference_reply(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text or "?" in text:
            return False
        preference_markers = (
            "cheapest",
            "budget",
            "affordable",
            "lowest",
            "luxury",
            "luxurious",
            "premium",
            "best",
            "king",
            "twin",
            "suite",
            "ultimate",
            "premier",
            "reserve",
            "prestige",
            "this one",
            "that one",
            "most luxurious",
        )
        if any(marker in text for marker in preference_markers):
            return True
        tokens = re.findall(r"[a-z0-9]+", text)
        if 0 < len(tokens) <= 4 and any(token in {"one", "room", "suite", "king", "twin"} for token in tokens):
            return True
        return False

    def _is_room_booking_intent_context(
        self,
        *,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
    ) -> bool:
        pending_action = str(context.pending_action or "").strip().lower()
        if self._is_room_booking_pending_action(pending_action):
            return True

        entities = intent_result.entities if isinstance(intent_result.entities, dict) else {}
        entity_markers = (
            str(entities.get("booking_sub_category") or ""),
            str(entities.get("booking_type") or ""),
            str(entities.get("custom_intent") or ""),
            str(entities.get("resolved_intent") or ""),
            str(entities.get("service_name") or ""),
            str(entities.get("room_type") or ""),
        )
        entity_blob = " ".join(marker.lower() for marker in entity_markers if str(marker or "").strip())
        if any(marker in entity_blob for marker in ("room_booking", "room", "suite", "stay", "check in", "check-out")):
            return True

        pending_data = context.pending_data if isinstance(context.pending_data, dict) else {}
        if any(
            key in pending_data
            for key in (
                "room_type",
                "room_name",
                "stay_checkin_date",
                "stay_checkout_date",
                "check_in",
                "check_out",
                "stay_date_range",
                "guest_count",
            )
        ):
            return True

        return self._looks_like_room_stay_booking_request(message)

    def _extract_room_booking_entity_updates(self, entities: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        data = entities if isinstance(entities, dict) else {}

        room_type = self._first_non_empty(
            data.get("room_type"),
            data.get("room_name"),
        )
        if room_type and not self._is_generic_room_reference(room_type):
            updates["room_type"] = room_type

        guests_raw = self._first_non_empty(
            data.get("guest_count"),
            data.get("party_size"),
            data.get("guests"),
        )
        if guests_raw:
            try:
                guests = int(float(str(guests_raw)))
                if 1 <= guests <= 20:
                    updates["guest_count"] = guests
            except (TypeError, ValueError):
                pass

        check_in = self._first_non_empty(
            data.get("check_in"),
            data.get("stay_checkin_date"),
            data.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            data.get("check_out"),
            data.get("stay_checkout_date"),
            data.get("checkout_date"),
        )
        date_range = self._first_non_empty(
            data.get("stay_date_range"),
            data.get("date_range"),
            data.get("date"),
        )
        if check_in:
            updates["stay_checkin_date"] = check_in
        if check_out:
            updates["stay_checkout_date"] = check_out
        if date_range and "to" in str(date_range).lower():
            updates["stay_date_range"] = date_range
        return updates

    async def _handle_room_booking_intent_flow(
        self,
        *,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session=None,
    ) -> HandlerResult:
        msg_lower = re.sub(r"\s+", " ", str(message or "").strip().lower())
        pending = dict(context.pending_data) if isinstance(context.pending_data, dict) else {}

        if intent_result.intent == IntentType.CONFIRMATION_NO or msg_lower in {"cancel", "stop", "no", "nope"}:
            return HandlerResult(
                response_text="No problem, I have cancelled this room booking request.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Show available room types", "Ask another question"],
            )

        entity_updates = self._extract_room_booking_entity_updates(
            intent_result.entities if isinstance(intent_result.entities, dict) else {}
        )
        if entity_updates:
            pending.update(entity_updates)

        slot_updates = self._extract_room_booking_slot_updates(
            message=message,
            current_pending_action=context.pending_action,
            state=context.state,
            pending_data=pending,
            memory_facts={},
            conversation_context=context,
        )
        if slot_updates:
            pending.update(slot_updates)

        persisted_candidates_raw = pending.get("room_type_candidates")
        persisted_candidates = [
            str(candidate or "").strip()
            for candidate in (persisted_candidates_raw if isinstance(persisted_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        room_candidates = self._merge_room_type_candidates(
            persisted_candidates,
            self._extract_room_type_candidates_from_context_messages(context),
        )
        if room_candidates:
            pending["room_type_candidates"] = room_candidates

        current_room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("selected_room_type"),
        )
        if current_room_type:
            if self._is_generic_room_reference(current_room_type):
                pending.pop("room_type", None)
                pending.pop("room_name", None)
                pending.pop("selected_room_type", None)
            elif room_candidates:
                resolved_room = self._resolve_room_type_candidate(current_room_type, room_candidates)
                if resolved_room:
                    pending["room_type"] = resolved_room
                    pending.pop("room_name", None)
                    pending.pop("selected_room_type", None)
                    pending.pop("requested_room_type", None)
                else:
                    pending["requested_room_type"] = current_room_type
                    pending.pop("room_type", None)
                    pending.pop("room_name", None)
                    pending.pop("selected_room_type", None)

        pending.setdefault("service_name", "Room Booking")
        pending.setdefault("booking_sub_category", "room_booking")
        pending.setdefault("booking_type", "room_booking")
        if "guest_count" in pending and not str(pending.get("party_size") or "").strip():
            pending["party_size"] = str(pending.get("guest_count"))
        if str(pending.get("stay_checkin_date") or "").strip() and not str(pending.get("check_in") or "").strip():
            pending["check_in"] = str(pending.get("stay_checkin_date") or "").strip()
        if str(pending.get("stay_checkout_date") or "").strip() and not str(pending.get("check_out") or "").strip():
            pending["check_out"] = str(pending.get("stay_checkout_date") or "").strip()

        missing = self._missing_room_booking_fields(
            pending_data=pending,
            memory_facts={},
        )
        pending_action = str(context.pending_action or "").strip().lower()
        if (
            intent_result.intent == IntentType.CONFIRMATION_YES
            and pending_action in {"confirm_booking", "confirm_room_booking", "confirm_room_availability_check"}
            and not missing
        ):
            booking_handler = handler_registry.get_handler(IntentType.TABLE_BOOKING)
            if booking_handler is not None:
                context.pending_action = "confirm_booking"
                context.pending_data = pending
                return await booking_handler.handle(
                    message,
                    intent_result,
                    context,
                    capabilities,
                    db_session,
                )

        if missing:
            response_text = self._build_room_booking_missing_details_prompt(
                missing_fields=missing,
                pending_data=pending,
            )
            return HandlerResult(
                response_text=response_text,
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_room_booking_details",
                pending_data=pending,
                suggested_actions=["Show available room types", "Share number of guests", "Share check-in and check-out dates"],
                metadata={"room_booking_flow": True},
            )

        pending.pop("requested_room_type", None)
        pending.pop("room_type_candidates", None)
        response_text = self._build_room_booking_review_prompt(pending)
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm")).strip() or "yes confirm"
        if bool(getattr(settings, "chat_require_strict_confirmation_phrase", True)):
            response_text = self._ensure_explicit_confirmation_instruction(
                response_text=response_text,
                confirmation_phrase=confirmation_phrase,
            )
        return HandlerResult(
            response_text=response_text,
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_booking",
            pending_data=pending,
            suggested_actions=[confirmation_phrase, "cancel"],
            metadata={"room_booking_flow": True},
        )

    def _should_interrupt_pending_flow(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities_summary: dict,
    ) -> bool:
        """
        Allow user to switch topics while a slot-filling action is pending.
        """
        pending_action = str(context.pending_action or "").strip()
        if not pending_action:
            return False
        if pending_action not in _DETAIL_COLLECTION_PENDING_ACTIONS:
            return False
        if intent_result.intent in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO):
            return False

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False
        if msg_lower in {"cancel", "stop", "never mind", "nevermind"}:
            return False

        if self._is_room_booking_pending_action(pending_action):
            # Keep room-booking slot context active for room-detail/room-option
            # follow-ups so the user doesn't lose collected booking data.
            if self._looks_like_problem_report(msg_lower):
                return True
            if self._looks_like_room_booking_detail_followup(msg_lower):
                return False
            if self._looks_like_room_options_information_query(msg_lower):
                return False
            if self._looks_like_room_type_preference_reply(msg_lower):
                return False
            if self._looks_like_room_stay_booking_request(msg_lower):
                return False

        if pending_action in {"select_service", "select_restaurant"}:
            if self._looks_like_service_selection(msg_lower, capabilities_summary):
                return False
        elif pending_action == "collect_booking_party_size":
            if self._looks_like_party_size_reply(msg_lower):
                return False
        elif pending_action == "collect_booking_time":
            if self._looks_like_time_reply(msg_lower):
                return False
        elif pending_action == "collect_ticket_room_number":
            if self._looks_like_room_number_reply(msg_lower):
                return False
        elif pending_action == "collect_ticket_issue_details":
            if self._looks_like_ticket_update_note(msg_lower):
                return False
        elif pending_action == "collect_ticket_identity_details":
            if self._looks_like_ticket_identity_details_reply(msg_lower):
                return False
        elif pending_action == "collect_ticket_update_note":
            if self._looks_like_ticket_update_note(msg_lower):
                return False
        elif pending_action == "awaiting_room_number":
            if self._looks_like_room_number_reply(msg_lower):
                return False
        elif pending_action == "awaiting_request_detail":
            if self._looks_like_room_service_detail(msg_lower):
                return False
        elif pending_action == "collect_transport_details":
            if self._looks_like_transport_detail(msg_lower):
                return False
        elif pending_action == "collect_service_details":
            if self._looks_like_service_detail_reply(msg_lower):
                return False

        pending_owner = _PENDING_ACTION_TO_INTENT.get(pending_action)
        if pending_owner and intent_result.intent not in {IntentType.UNCLEAR, pending_owner}:
            return True
        if self._looks_like_problem_report(msg_lower):
            return True
        if self._looks_like_information_query(msg_lower):
            return True
        return False

    def _should_interrupt_pending_flow_for_user_query(
        self,
        *,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
    ) -> bool:
        """
        Lightweight interruption check for deterministic user-query shortcuts.
        Uses content heuristics without forcing intent mismatch interruption.
        """
        synthetic_intent = IntentResult(
            intent=IntentType.UNCLEAR,
            confidence=0.5,
            entities={},
        )
        return self._should_interrupt_pending_flow(
            message=message,
            intent_result=synthetic_intent,
            context=context,
            capabilities_summary=capabilities_summary,
        )

    @staticmethod
    def _looks_like_service_selection(msg_lower: str, capabilities_summary: dict) -> bool:
        service_catalog = capabilities_summary.get("service_catalog", [])
        for service in service_catalog:
            name = str(service.get("name") or "").strip().lower()
            if name and (name in msg_lower or msg_lower in name):
                return True
        return False

    @staticmethod
    def _looks_like_party_size_reply(msg_lower: str) -> bool:
        if re.fullmatch(r"\s*\d{1,2}\s*", msg_lower):
            return True
        return bool(re.search(r"\b(?:for|party of)\s+\d{1,2}\b", msg_lower)) or bool(
            re.search(r"\b\d{1,2}\s*(?:people|guests|persons|pax)\b", msg_lower)
        )

    @staticmethod
    def _looks_like_time_reply(msg_lower: str) -> bool:
        if any(token in msg_lower for token in ("today", "tomorrow", "tonight", "am", "pm")):
            return True
        return bool(
            re.search(
                r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
                msg_lower,
            )
        )

    @staticmethod
    def _looks_like_room_number_reply(msg_lower: str) -> bool:
        return bool(re.fullmatch(r"\s*[a-z0-9-]{2,10}\s*", msg_lower))

    def _apply_full_kb_plugin_ticket_followup_state(
        self,
        *,
        combined_ticket_meta: dict[str, Any],
        current_pending_action: str | None,
        pending_action_target: str | None,
        pending_data_target: dict[str, Any] | None,
        internal_pending_entries: dict[str, Any],
        next_state: ConversationState,
        clear_pending_data: bool,
    ) -> tuple[str | None, dict[str, Any] | None, ConversationState, bool]:
        meta = combined_ticket_meta if isinstance(combined_ticket_meta, dict) else {}
        if not meta:
            return pending_action_target, pending_data_target, next_state, clear_pending_data

        pending_draft = meta.get("pending_ticket_draft")
        if isinstance(pending_draft, dict) and pending_draft:
            internal_pending_entries[_INTERNAL_PENDING_TICKET_DRAFT_KEY] = dict(pending_draft)

        skip_reason = str(
            meta.get("ticket_skip_reason")
            or meta.get("ticket_create_skip_reason")
            or meta.get("ticketing_skip_reason")
            or ""
        ).strip().lower()
        if skip_reason == "missing_required_details":
            missing_fields = {
                str(field).strip().lower()
                for field in (meta.get("ticket_missing_fields") or [])
                if str(field).strip()
            }
            pending_action_hint = str(meta.get("ticket_pending_action") or "").strip().lower()
            if pending_action_hint:
                pending_action_target = pending_action_hint
            if "room_number" in missing_fields:
                pending_action_target = "collect_ticket_room_number"
                next_state = ConversationState.AWAITING_INFO
                clear_pending_data = False

        if bool(meta.get("clear_pending_ticket_draft")):
            internal_pending_entries.pop(_INTERNAL_PENDING_TICKET_DRAFT_KEY, None)

        if bool(meta.get("ticket_resumed_from_pending_draft")):
            internal_pending_entries.pop(_INTERNAL_PENDING_TICKET_DRAFT_KEY, None)
            if str(current_pending_action or "").strip().lower() == "collect_ticket_room_number":
                pending_action_target = None
            if next_state == ConversationState.ESCALATED:
                next_state = ConversationState.IDLE
            clear_pending_data = False

        return pending_action_target, pending_data_target, next_state, clear_pending_data

    def _maybe_add_missing_room_number_prompt(
        self,
        *,
        response_text: str,
        ticket_meta: dict[str, Any] | None,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> str:
        meta = ticket_meta if isinstance(ticket_meta, dict) else {}
        if str(
            meta.get("ticket_skip_reason")
            or meta.get("ticket_create_skip_reason")
            or meta.get("ticketing_skip_reason")
            or ""
        ).strip().lower() != "missing_required_details":
            return str(response_text or "").strip()

        resolved_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data if isinstance(pending_data, dict) else {},
            entities=entities if isinstance(entities, dict) else {},
        )
        if resolved_phase != "during_stay":
            return str(response_text or "").strip()

        room_number = self._first_non_empty(
            context.room_number,
            (pending_data or {}).get("room_number") if isinstance(pending_data, dict) else "",
        )
        if room_number:
            return str(response_text or "").strip()

        current = str(response_text or "").strip()
        if "room number" in current.lower():
            return current
        if current:
            return (
                f"{current} To create this service ticket, please share your room number."
            ).strip()
        return "Please share your room number so I can create this service ticket."

    @staticmethod
    def _looks_like_ticket_update_note(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if msg_lower in {"yes", "no", "cancel", "stop"}:
            return False
        # Free-form note text should generally be consumed by ticket-update flow.
        return len(msg_lower.split()) >= 3

    @staticmethod
    def _looks_like_ticket_identity_details_reply(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if msg_lower in {"cancel", "stop", "no", "not now"}:
            return True
        if bool(re.search(r"(?:\+?\d[\d\s\-()]{6,}\d)", msg_lower)):
            return True
        if "name" in msg_lower and len(msg_lower.split()) >= 2:
            return True
        if len(msg_lower.split()) >= 2 and all(token.isalpha() for token in msg_lower.split()[:2]):
            return True
        return False

    @staticmethod
    def _looks_like_room_service_detail(msg_lower: str) -> bool:
        markers = (
            "clean",
            "cleaning",
            "towel",
            "amenit",
            "laundry",
            "maintenance",
            "ac",
            "room service",
            "housekeeping",
            "soap",
            "shampoo",
        )
        return any(marker in msg_lower for marker in markers)

    @staticmethod
    def _looks_like_transport_detail(msg_lower: str) -> bool:
        markers = ("flight", "terminal", "pickup", "drop", "airport")
        if any(marker in msg_lower for marker in markers):
            return True
        return bool(
            re.search(
                r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
                msg_lower,
            )
        )

    @staticmethod
    def _looks_like_service_detail_reply(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if "?" in msg_lower:
            return False
        detail_markers = (
            "from",
            "to",
            "date",
            "dates",
            "night",
            "nights",
            "guest",
            "guests",
            "people",
            "person",
            "check in",
            "check out",
            "checkin",
            "checkout",
            "room",
            "stay",
            "arrival",
            "departure",
        )
        if any(marker in msg_lower for marker in detail_markers):
            return True
        if re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\b", msg_lower):
            return True
        if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", msg_lower):
            return True
        if re.search(r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm))\b", msg_lower):
            return True
        return len(msg_lower.split()) >= 3

    @staticmethod
    def _looks_like_room_booking_detail_followup(message: str) -> bool:
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False

        has_guest_detail = bool(
            re.search(r"\b\d{1,2}\s*(?:guests?|people|persons|pax)\b", msg_lower)
        ) or bool(re.search(r"\b(?:for|foe|fpr|fr)\s+\d{1,2}\b", msg_lower))
        has_date_detail = bool(
            re.search(
                r"\b(?:from\s+[^.?!,]{1,30}\s+to\s+[^.?!,]{1,30}|"
                r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{1,2}\s*(?:-|to)\s*\d{1,2}|"
                r"\d{1,2}(?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
                r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{1,2}(?:st|nd|rd|th)?|"
                r"\d{1,2}\s*[-/]\s*\d{1,2})\b",
                msg_lower,
            )
        ) or any(marker in msg_lower for marker in ("check in", "check-in", "check out", "check-out", "checkin", "checkout"))
        has_structured_detail = ChatService._looks_like_service_detail_reply(msg_lower) and bool(
            re.search(r"\b\d{1,2}\b", msg_lower)
        )
        return has_guest_detail or has_date_detail or has_structured_detail

    def _recent_user_turn_mentions_room_booking(self, context: ConversationContext, current_message: str) -> bool:
        current_norm = re.sub(r"\s+", " ", str(current_message or "").strip().lower())
        skipped_current = False
        for msg in reversed(context.messages):
            text = str(msg.content or "").strip()
            if not text:
                continue
            norm = re.sub(r"\s+", " ", text.lower())
            if msg.role == MessageRole.USER:
                if not skipped_current and norm == current_norm:
                    skipped_current = True
                    continue
                if self._looks_like_room_stay_booking_request(text):
                    return True
                if any(
                    marker in norm
                    for marker in (
                        "room available",
                        "room type",
                        "cheapest room",
                        "stay",
                        "check in",
                        "check-out",
                        "check out",
                        "book room",
                    )
                ):
                    return True
            elif msg.role == MessageRole.ASSISTANT:
                asks_stay_dates = (
                    ("check-in" in norm or "check in" in norm)
                    and ("check-out" in norm or "check out" in norm)
                )
                asks_guest_count = "guest" in norm or "guests" in norm
                mentions_room_flow = any(
                    marker in norm
                    for marker in (
                        "room booking",
                        "room type",
                        "for your stay",
                        "cheapest room",
                    )
                )
                if asks_stay_dates and asks_guest_count:
                    return True
                if mentions_room_flow and asks_guest_count:
                    return True
        return False

    @staticmethod
    def _looks_like_room_stay_booking_request(message: str) -> bool:
        """
        Detect hotel-stay booking language so it is not misrouted into
        slot/service booking flows.
        """
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False

        # Exclude operational room-service asks.
        if any(
            term in msg_lower
            for term in ("room service", "housekeeping", "cleaning", "towel", "laundry", "maintenance")
        ):
            return False

        # Exclude obvious slot-based booking asks.
        if any(term in msg_lower for term in ("table", "appointment", "slot", "service")):
            return False

        booking_like = any(term in msg_lower for term in ("book", "booking", "reserve", "stay"))
        room_stay_like = any(
            term in msg_lower
            for term in (
                "room",
                "suite",
                "suites",
                "villa",
                "residence",
                "check in",
                "check-in",
                "check out",
                "check-out",
                "checkin",
                "checkout",
                "night",
                "nights",
            )
        )
        has_date_window = bool(
            re.search(
                r"\b(from\s+.+\s+to\s+.+|\d{1,2}(?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b",
                msg_lower,
            )
        )

        has_room_enquiry_phrase = bool(
            re.search(r"\b(looking\s+for|need|want|require)\s+(?:a\s+)?room\b", msg_lower)
            or "room for" in msg_lower
        )

        return bool(room_stay_like and (booking_like or has_date_window or has_room_enquiry_phrase))

    @staticmethod
    def _looks_like_spa_booking_action(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        if not any(token in text for token in ("spa", "massage", "wellness", "therapy", "treatment")):
            return False

        action_markers = (
            "book",
            "booking",
            "reserve",
            "schedule",
            "confirm",
            "i want",
            "i need",
            "please book",
            "set up",
        )
        has_action = any(marker in text for marker in action_markers)
        if not has_action:
            return False

        info_only_markers = (
            "show",
            "list",
            "what",
            "which",
            "package",
            "packages",
            "timing",
            "hours",
            "available",
            "tell me about",
        )
        if "?" in text and not has_action:
            return False
        if any(marker in text for marker in info_only_markers) and not any(
            marker in text for marker in ("book", "reserve", "schedule", "confirm")
        ):
            return False
        return True

    def _handle_service_detail_followup(
        self,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
    ) -> Optional[HandlerResult]:
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if not msg:
            return HandlerResult(
                response_text="Please share the details you'd like me to record for this request.",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_service_details",
                pending_data=context.pending_data,
                suggested_actions=["Share dates", "Share preferred time", "Cancel"],
            )

        if msg_lower in {"cancel", "stop", "no", "nope", "never mind", "nevermind"}:
            return HandlerResult(
                response_text="No problem. I've cancelled this request. Let me know if you need anything else.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=config_service.get_quick_actions(limit=4),
            )

        service_name = str(context.pending_data.get("service_name") or "this service").strip()
        service_id = str(context.pending_data.get("service_id") or "").strip()
        details = dict(context.pending_data or {})
        details["service_details"] = msg

        return HandlerResult(
            response_text=(
                f"Thanks, I've noted your {service_name} request details: {msg}. "
                "I can proceed with this request now. Shall I confirm and continue?"
            ),
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_service_request",
            pending_data=details,
            suggested_actions=["Yes, confirm", "No, cancel"],
            metadata={"service_id": service_id, "service_name": service_name},
        )

    async def _handle_service_catalog_unavailable_handoff(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        service_match: dict[str, Any],
        db_session=None,
        routing_path_tag: str,
        pending_interrupted: bool,
    ) -> ChatResponse | None:
        """
        Auto-handoff flow for action requests against services that are not configured.
        """
        service_name = str(service_match.get("service_name") or "").strip()
        escalation_intent = IntentResult(
            intent=IntentType.HUMAN_REQUEST,
            confidence=0.88,
            entities={
                "requested_service": service_name,
                "service_match_type": str(service_match.get("match_type") or ""),
                "handoff_reason": "service_not_configured",
            },
        )
        effective_user_message = str(request.message or "").strip()
        handler_result = await self._dispatch_to_handler(
            effective_user_message,
            escalation_intent,
            context,
            capabilities_summary,
            db_session,
        )
        if handler_result is None:
            return None

        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        next_state = handler_result.next_state
        if handler_result.pending_action is not None:
            context.pending_action = handler_result.pending_action
        if handler_result.pending_data is not None:
            context.pending_data = conversation_memory_service.merge_with_internal(
                handler_result.pending_data,
                internal_pending_entries,
            )
        if handler_result.pending_action is None and next_state not in (
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
        ):
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )
            context.pending_data.pop("_clarification_attempts", None)
        if handler_result.metadata.get("room_number"):
            context.room_number = str(handler_result.metadata["room_number"])
        context.state = next_state

        fallback_prefix = str(service_match.get("response_text") or "").strip()
        selected_phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=escalation_intent.entities if isinstance(escalation_intent.entities, dict) else {},
        )
        prefix = await self._compose_service_catalog_unavailable_preface(
            requested_service=service_name,
            user_message=effective_user_message,
            capabilities_summary=capabilities_summary,
            fallback_text=fallback_prefix,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
        )
        if prefix:
            response_text = f"{prefix}\n\n{handler_result.response_text}"
        else:
            response_text = handler_result.response_text
        response_text, resume_actions = self._append_resume_checkpoint(
            response_text=response_text,
            context=context,
            pending_interrupted=pending_interrupted,
        )

        assistant_metadata = {
            "intent": escalation_intent.intent.value,
            "confidence": escalation_intent.confidence,
            "channel": context.channel,
            "service_id": service_match.get("service_id"),
            "service_name": service_name,
            "service_match_type": service_match.get("match_type"),
            "service_catalog_handoff": True,
        }
        if handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await conversation_memory_service.maybe_refresh_summary(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if handler_result.suggested_actions:
            suggested_actions = resume_actions + handler_result.suggested_actions
        else:
            suggested_actions = resume_actions + list(service_match.get("suggested_actions") or [])
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=escalation_intent.intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        metadata = {
            "message_count": len(context.messages),
            "entities": escalation_intent.entities,
            "routing_path": ProcessingPath.SIMPLE.value,
            "routing_score": 1.0,
            "routing_signals": {"path": routing_path_tag},
            "pending_interrupted": pending_interrupted,
            "resume_checkpoint": bool(resume_actions),
            "parked_task_available": self._peek_parked_task(context) is not None,
            "service_catalog_match": True,
            "service_id": service_match.get("service_id"),
            "service_match_type": service_match.get("match_type"),
            "service_catalog_handoff": True,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
        }
        if handler_result.metadata:
            metadata.update(handler_result.metadata)

        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=escalation_intent.intent,
            confidence=escalation_intent.confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _compose_service_catalog_unavailable_preface(
        self,
        *,
        requested_service: str,
        user_message: str,
        capabilities_summary: dict[str, Any],
        fallback_text: str,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> str:
        """
        Build a natural handoff preface for unconfigured requests.
        Uses LLM when available; falls back to deterministic text.
        """
        fallback = str(fallback_text or "").strip()
        if not str(settings.openai_api_key or "").strip():
            return fallback

        service_label = str(requested_service or "").strip() or "that request"
        available_services: list[str] = []
        for service in capabilities_summary.get("service_catalog", []):
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            name = str(service.get("name") or service.get("id") or "").strip()
            if not name:
                continue
            if name not in available_services:
                available_services.append(name)
            if len(available_services) >= 8:
                break
        available_text = ", ".join(available_services) if available_services else "configured hotel services"
        phase_label = str(selected_phase_name or "").strip() or (
            str(selected_phase_id or "").replace("_", " ").title()
        )
        phase_id = str(selected_phase_id or "").strip() or "unknown"

        prompt = (
            "Write one short guest-facing preface before human handoff.\n"
            "Context:\n"
            f"- Requested service: {service_label}\n"
            f"- User message: {str(user_message or '').strip()[:280] or '(none)'}\n"
            f"- Available services: {available_text}\n\n"
            f"- Selected phase: {phase_label} ({phase_id})\n\n"
            "Rules:\n"
            "1) Do not promise booking/order completion.\n"
            "2) Clearly say staff will assist manually.\n"
            "3) Keep it concise and natural (max 2 short sentences).\n"
            "4) Return plain text only."
        )
        try:
            rendered = await llm_client.chat(
                messages=[
                    {"role": "system", "content": "You write concise guest support responses."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=120,
            )
        except Exception:
            return fallback

        text = " ".join(str(rendered or "").split()).strip()
        if not text:
            return fallback

        lower = text.lower()
        if any(
            marker in lower
            for marker in (
                "booked",
                "confirmed",
                "placed",
                "arranged",
                "i will arrange",
                "i'll arrange",
                "i can arrange",
            )
        ):
            return fallback
        if "staff" not in lower and "team" not in lower and "human" not in lower:
            return fallback
        return text

    @staticmethod
    def _looks_like_information_query(msg_lower: str) -> bool:
        if "?" in msg_lower:
            return True
        normalized = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        if not normalized:
            return False
        if ChatService._looks_like_imperative_information_request(normalized):
            return True
        tokens = re.findall(r"[a-z]+", normalized)
        if not tokens:
            return False
        if tokens[0] in _INFORMATION_QUERY_PREFIXES:
            return True
        return any(
            token in normalized
            for token in (
                "timing",
                "hours",
                "price",
                "cost",
                "available",
                "address",
                "location",
            )
        )

    @staticmethod
    def _looks_like_imperative_information_request(msg_lower: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        if not normalized:
            return False
        if any(normalized.startswith(prefix) for prefix in _INFORMATION_QUERY_IMPERATIVE_PREFIXES):
            return True
        imperative_markers = (
            "tell me about",
            "more about",
            "give me the address",
            "share the address",
            "share details",
            "hotel address",
        )
        return any(marker in normalized for marker in imperative_markers)

    @staticmethod
    def _looks_like_problem_report(msg_lower: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        if not normalized:
            return False
        direct_markers = (
            "booking system",
            "bookingsystem",
            "cannot access",
            "can't access",
            "cant access",
            "unable to access",
            "not able to access",
        )
        if any(marker in normalized for marker in direct_markers):
            return True

        issue_markers = (
            "not able",
            "unable",
            "can't",
            "cannot",
            "cant",
            "issue",
            "problem",
            "error",
            "failed",
            "not working",
        )
        system_markers = (
            "website",
            "site",
            "portal",
            "system",
            "app",
            "booking",
            "login",
            "sign in",
            "signin",
            "access",
        )
        return any(marker in normalized for marker in issue_markers) and any(
            marker in normalized for marker in system_markers
        )

    def _check_capability_for_intent(
        self,
        hotel_code: str,
        intent_result: IntentResult,
        message: str,
    ) -> CapabilityCheck:
        """Check if the detected intent is allowed by business capabilities from config_service."""
        intent = intent_result.intent
        entities = intent_result.entities

        # Map intents to capability checks using config_service
        if intent == IntentType.ORDER_FOOD:
            # Check if food ordering is enabled
            if not self._is_any_capability_enabled(
                "food_ordering",
                "room_service",
                "order_placement",
            ):
                return CapabilityCheck(
                    allowed=False,
                    reason="Food ordering is not available at this time.",
                    alternatives=config_service.get_quick_actions(limit=4),
                )
            # Check room-delivery restrictions only when user explicitly requests
            # room delivery and mentions a specific service.
            service_target = str(
                entities.get("service")
                or entities.get("restaurant")
                or entities.get("restaurant_name")
                or ""
            ).strip()
            message_service_target = self._extract_service_target_from_message(message)
            if message_service_target:
                service_target = message_service_target
            wants_room_delivery = bool(
                re.search(r"\b(room|to my room|in my room|room delivery|deliver to room)\b", message.lower())
            )
            explicitly_mentioned = self._entity_explicitly_mentioned_in_message(message, service_target)
            if service_target and wants_room_delivery and explicitly_mentioned:
                if config_service.can_deliver_to_room(service_target.lower()):
                    return CapabilityCheck(allowed=True, reason="Room delivery available")
                return CapabilityCheck(
                    allowed=False,
                    reason=f"{service_target} is dine-in only and does not currently support room delivery.",
                    alternatives=config_service.get_quick_actions(limit=4),
                )
            return CapabilityCheck(allowed=True, reason="Food ordering available")

        if intent == IntentType.TABLE_BOOKING:
            if not self._is_any_capability_enabled("table_booking", "appointment_booking"):
                return CapabilityCheck(
                    allowed=False,
                    reason="Booking is not available at this time.",
                    alternatives=config_service.get_quick_actions(limit=4),
                )
            return CapabilityCheck(allowed=True, reason="Table booking available")

        if intent == IntentType.MENU_REQUEST:
            # Keep this intent as a backward-compatible alias for knowledge lookup.
            if not config_service.is_menu_runtime_enabled():
                return CapabilityCheck(
                    allowed=True,
                    reason="Knowledge-base lookup allowed while menu runtime is disabled.",
                )
            return CapabilityCheck(allowed=True, reason="Menu capability available")

        if intent == IntentType.HUMAN_REQUEST:
            if not config_service.is_capability_enabled("human_escalation"):
                return CapabilityCheck(
                    allowed=False,
                    reason="Human support is temporarily unavailable. Please try again later.",
                )
            return CapabilityCheck(allowed=True, reason="Human escalation available")

        msg_lower = str(message or "").lower()

        # Check transport capability
        if "cab" in msg_lower or "taxi" in msg_lower or "transport" in msg_lower:
            asks_local_transport = any(
                marker in msg_lower
                for marker in ("local cab", "local taxi", "city cab", "within city", "around city")
            )
            asks_airport_transfer = any(
                marker in msg_lower
                for marker in ("airport", "terminal", "pickup", "drop", "transfer")
            )
            if asks_local_transport and not asks_airport_transfer:
                if not self._is_any_capability_enabled("local_transport", "local_cab"):
                    return CapabilityCheck(
                        allowed=False,
                        reason=(
                            "That specific transport request is not configured for instant booking here yet. "
                            "I can connect you with our staff team to assist manually."
                        ),
                        alternatives=["Connect to staff", "Transport details", "Need help"],
                    )
            if not self._is_any_capability_enabled(
                "transport",
                "local_transport",
                "airport_transfer",
            ):
                return CapabilityCheck(
                    allowed=False,
                    reason="Transport services are not available at this time.",
                )

        # Check spa booking
        if "spa" in message.lower() or "massage" in message.lower():
            if not config_service.is_capability_enabled("spa_booking"):
                return CapabilityCheck(
                    allowed=False,
                    reason="Spa booking is not available at this time.",
                )

        # Default: allow
        return CapabilityCheck(allowed=True, reason="Action allowed")

    @staticmethod
    def _is_any_capability_enabled(*capability_ids: str) -> bool:
        """Return True if at least one provided capability flag is enabled."""
        return any(config_service.is_capability_enabled(cap_id) for cap_id in capability_ids)

    @staticmethod
    def _entity_explicitly_mentioned_in_message(message: str, entity_value: str) -> bool:
        """Check whether an extracted entity text is explicitly present in the user message."""
        entity = str(entity_value or "").strip().lower()
        if not entity:
            return False

        entity_compact = re.sub(r"[^a-z0-9]+", " ", entity).strip()
        msg_compact = re.sub(r"[^a-z0-9]+", " ", str(message or "").lower()).strip()
        if not entity_compact or not msg_compact:
            return False
        if entity_compact in msg_compact:
            return True

        entity_tokens = [token for token in entity_compact.split() if token]
        msg_tokens = [token for token in msg_compact.split() if token]
        if not entity_tokens or not msg_tokens:
            return False

        for token in entity_tokens:
            if any(
                token == msg_token
                or (len(token) >= 5 and (token.startswith(msg_token[:4]) or msg_token.startswith(token[:4])))
                or (len(token) >= 5 and SequenceMatcher(a=token, b=msg_token).ratio() >= 0.86)
                for msg_token in msg_tokens
            ):
                continue
            return False
        return True

    @staticmethod
    def _extract_service_target_from_message(message: str) -> str:
        """
        Extract explicit service mentions from delivery phrasing like:
        "deliver X from kadak to my room".
        """
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return ""

        patterns = (
            r"\bfrom\s+(?:the\s+)?([a-z][a-z0-9&' -]{1,40}?)(?=\s+(?:to|for|in|at|with|now|please)\b|[?.!,]|$)",
            r"\bat\s+(?:the\s+)?([a-z][a-z0-9&' -]{1,40}?)(?=\s+(?:to|for|in|with|now|please)\b|[?.!,]|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, msg_lower)
            if not match:
                continue
            candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,!?:;")
            if not candidate or candidate.isdigit():
                continue
            return candidate
        return ""

    @staticmethod
    def _is_transport_info_query(msg_lower: str) -> bool:
        markers = (
            "what is",
            "what are",
            "how much",
            "price",
            "cost",
            "charge",
            "charges",
            "rate",
            "rates",
            "timing",
            "timings",
            "do you provide",
            "can you provide",
            "available",
        )
        return any(marker in msg_lower for marker in markers)

    def _should_route_transport_action(self, msg_lower: str, intent: IntentType) -> bool:
        transport_keywords = ("cab", "taxi", "transport", "airport", "transfer", "pickup", "drop", "ride")
        if not any(kw in msg_lower for kw in transport_keywords):
            return False

        action_markers = (
            "book",
            "arrange",
            "need",
            "want",
            "send",
            "schedule",
            "confirm",
            "get me",
            "pick me",
            "drop me",
            "transfer me",
        )
        is_actionable = any(marker in msg_lower for marker in action_markers)

        # FAQ-like transport questions should stay on retrieval/FAQ path.
        if intent == IntentType.FAQ and not is_actionable:
            return False
        if self._is_transport_info_query(msg_lower) and not is_actionable:
            return False

        return is_actionable

    def _check_intent_enabled(self, intent: IntentType) -> CapabilityCheck:
        """
        Validate whether a classified intent is enabled in admin config.
        Keeps the bot industry-agnostic by honoring configured intent toggles.
        """
        # Internal/system intents are always allowed
        if intent in {
            IntentType.CONFIRMATION_YES,
            IntentType.CONFIRMATION_NO,
            IntentType.UNCLEAR,
            IntentType.OUT_OF_SCOPE,
        }:
            return CapabilityCheck(allowed=True, reason="System intent allowed")

        if any(config_service.is_intent_enabled(intent_id) for intent_id in self._intent_ids_for_enablement(intent)):
            return CapabilityCheck(allowed=True, reason="Intent enabled")

        return CapabilityCheck(
            allowed=False,
            reason="That workflow is currently disabled for this business configuration.",
            alternatives=config_service.get_quick_actions(limit=4),
        )

    def _match_identity_response(
        self,
        message: str,
        capabilities_summary: dict,
        context: ConversationContext,
    ) -> Optional[dict]:
        """Handle bot/business identity questions deterministically."""
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        compact = re.sub(r"[^a-z0-9 ]+", " ", msg_lower)
        compact = re.sub(r"\s+", " ", compact).strip()
        tokens = re.findall(r"[a-z0-9]+", compact)
        token_set = set(tokens)
        joined = "".join(tokens)

        direct_identity_markers = (
            "your name",
            "ur name",
            "who are you",
            "who r u",
            "bot name",
            "what is your name",
            "what's your name",
            "whats your name",
            "who are u",
            "who u",
            "who ru",
            "who r you",
        )
        asks_bot_name = any(marker in compact for marker in direct_identity_markers)
        if not asks_bot_name and token_set:
            who_like = any(SequenceMatcher(a=tok, b="who").ratio() >= 0.75 for tok in token_set)
            are_like = any(
                tok in {"are", "r"}
                or SequenceMatcher(a=tok, b="are").ratio() >= 0.66
                for tok in token_set
            )
            you_like = any(tok in {"you", "u", "ur"} for tok in token_set)
            name_like = "name" in token_set
            bot_like = any(tok in {"bot", "assistant"} for tok in token_set)

            if who_like and you_like and (are_like or len(tokens) <= 3):
                asks_bot_name = True
            elif name_like and (you_like or bot_like):
                asks_bot_name = True
            elif joined in {"whoru", "whoareu", "whoareyou", "whatsyourname"}:
                asks_bot_name = True

        if asks_bot_name:
            bot_name = str(capabilities_summary.get("bot_name") or "Assistant").strip()
            business_name = str(
                capabilities_summary.get("hotel_name")
                or capabilities_summary.get("business_name")
                or "our business"
            ).strip()
            city = str(capabilities_summary.get("city") or "").strip()
            if city:
                response_text = (
                    f"I am {bot_name}, your concierge assistant at {business_name} in {city}. "
                    "How may I assist you today?"
                )
            else:
                response_text = (
                    f"I am {bot_name}, your concierge assistant at {business_name}. "
                    "How may I assist you today?"
                )
            return {
                "match_type": "bot_name",
                "response_text": response_text,
            }

        asks_business_name = any(
            marker in msg_lower
            for marker in (
                "which hotel is this",
                "hotel name",
                "which business is this",
                "where am i",
                "tell me about your hotel",
                "tell me about ur hotel",
                "about your hotel",
                "about ur hotel",
                "about this hotel",
            )
        )
        if asks_business_name:
            business_name = str(capabilities_summary.get("hotel_name") or "our business").strip()
            city = str(capabilities_summary.get("city") or "").strip()
            location = f" in {city}" if city else ""
            return {
                "match_type": "business_name",
                "response_text": f"This is {business_name}{location}.",
            }

        return None

    def _match_service_overview_response(
        self,
        message: str,
        capabilities_summary: dict,
        context: ConversationContext,
    ) -> Optional[dict]:
        """
        Deterministically answer "what services are available" style requests
        so they do not fall through to FAQ/RAG when service metadata is local.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        tokens = re.findall(r"[a-z]+", msg_lower)
        if not tokens:
            return None

        has_service_signal = any(
            token.startswith("serv")
            or SequenceMatcher(a=token, b="services").ratio() >= 0.78
            for token in tokens
        )
        has_availability_signal = any(
            token in {"available", "provide", "provides", "offered", "offer", "offers", "show", "list", "all", "options"}
            or SequenceMatcher(a=token, b="available").ratio() >= 0.74
            for token in tokens
        )
        normalized_message = re.sub(r"\s+", " ", msg_lower).strip()
        is_overview_query = (
            "what services" in msg_lower
            or "which services" in msg_lower
            or "services available" in msg_lower
            or "what can you" in msg_lower
            or "what do you offer" in msg_lower
            or "what are available" in msg_lower
            or "all services" in msg_lower
            or "list services" in msg_lower
            or "show services" in msg_lower
            or normalized_message in {"services", "service list", "available services"}
        )
        broad_query_markers = {"what", "which", "list", "show", "all", "available", "options"}
        has_broad_query_marker = any(token in broad_query_markers for token in tokens)
        if not is_overview_query and has_service_signal and has_availability_signal and has_broad_query_marker:
            is_overview_query = True
        if not is_overview_query:
            return None

        service_catalog = capabilities_summary.get("service_catalog", [])
        active_services = []
        if isinstance(service_catalog, list):
            active_services = [
                svc
                for svc in service_catalog
                if isinstance(svc, dict) and bool(svc.get("is_active", True))
            ]

        if active_services:
            names = [str(svc.get("name") or svc.get("id") or "").strip() for svc in active_services]
            names = [name for name in names if name]
            names = names[:8]
            if names:
                return {
                    "response_text": "Here are the currently available services: " + ", ".join(names) + ".",
                }

        services_flags = capabilities_summary.get("services", {})
        if isinstance(services_flags, dict):
            labels: list[str] = []
            for capability_id, enabled in services_flags.items():
                if capability_id.endswith("_hours") or not enabled:
                    continue
                label = str(capability_id).replace("_", " ").strip().title()
                if label and label not in labels:
                    labels.append(label)
            if labels:
                return {
                    "response_text": "Here are the currently enabled services: " + ", ".join(labels[:8]) + ".",
                }

        return {
            "response_text": "I don't see any configured services right now. I can connect you with our team to assist further.",
        }

    def _match_room_number_update_response(
        self,
        message: str,
        context: ConversationContext,
    ) -> Optional[dict]:
        """
        Detect profile-style room number updates and acknowledge them deterministically.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        if "room" not in msg_lower:
            return None
        if "?" in msg_lower:
            return None
        if msg_lower.startswith(("what", "which", "where", "is my room", "what is my room")):
            return None

        match = re.search(r"\bmy\s+room(?:\s*number)?\s*(?:is|=|:)\s*([a-z0-9-]{2,10})\b", msg_lower)
        if not match:
            return None

        room_number = match.group(1).upper()
        if not any(ch.isdigit() for ch in room_number):
            return None
        return {
            "room_number": room_number,
            "response_text": f"Got it, I've updated your room number to {room_number}.",
        }

    def _match_room_number_lookup_response(
        self,
        message: str,
        context: ConversationContext,
        memory_snapshot: dict,
    ) -> Optional[dict]:
        """Answer room-number recall questions from context/memory."""
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        asks_room = (
            "my room number" in msg_lower
            or "what is my room" in msg_lower
            or "which room" in msg_lower
        )
        if not asks_room:
            return None

        room_number = str(context.room_number or "").strip().upper()
        if not room_number:
            facts = memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {}
            if isinstance(facts, dict):
                room_number = str(facts.get("room_number") or "").strip().upper()

        if room_number:
            return {"response_text": f"Your room number is {room_number}."}
        return {"response_text": "I don't have your room number yet. Please share it in this format: 'My room number is 202'."}

    async def _match_menu_recommendation_response(
        self,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
        db_session=None,
    ) -> Optional[dict]:
        """
        Deterministic menu recommendation helper for preference asks.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        recommendation_markers = (
            "recommend",
            "suggest",
            "what should i eat",
            "what can i eat",
            "what food do you have",
            "what do you have",
            "show options",
            "show me options",
            "show more options",
            "more options",
            "in mood for",
            "craving",
            "hungry",
        )
        if not any(token in msg_lower for token in recommendation_markers):
            return None

        if db_session is None:
            return None

        prefer_nonveg = bool(
            re.search(r"\b(non[\s-]?veg|chicken|mutton|fish|prawn|meat|seafood)\b", msg_lower)
        )
        prefer_veg = bool(re.search(r"\b(vegetarian|vegan|veg)\b", msg_lower)) and not prefer_nonveg

        from sqlalchemy import select
        from models.database import Hotel, Restaurant, MenuItem

        hotel_id = capabilities_summary.get("hotel_id")
        if hotel_id is None:
            hotel_name = str(capabilities_summary.get("hotel_name") or "").strip()
            if hotel_name:
                hotel_row = (
                    await db_session.execute(select(Hotel).where(Hotel.name.ilike(f"%{hotel_name}%")))
                ).scalar_one_or_none()
                if hotel_row is not None:
                    hotel_id = hotel_row.id
        if hotel_id is None:
            hotel_row = (
                await db_session.execute(select(Hotel).where(Hotel.is_active == True).limit(1))  # noqa: E712
            ).scalar_one_or_none()
            if hotel_row is not None:
                hotel_id = hotel_row.id
        if hotel_id is None:
            return None

        stmt = (
            select(MenuItem)
            .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
            .where(
                Restaurant.hotel_id == hotel_id,
                Restaurant.is_active == True,  # noqa: E712
                MenuItem.is_available == True,  # noqa: E712
            )
        )
        if prefer_nonveg and not prefer_veg:
            stmt = stmt.where(MenuItem.is_vegetarian == False)  # noqa: E712
        elif prefer_veg and not prefer_nonveg:
            stmt = stmt.where(MenuItem.is_vegetarian == True)  # noqa: E712

        rows = list((await db_session.execute(stmt)).scalars().all())
        if not rows:
            return None

        stopwords = {
            "i",
            "im",
            "in",
            "the",
            "a",
            "an",
            "for",
            "to",
            "my",
            "me",
            "show",
            "more",
            "options",
            "option",
            "want",
            "need",
            "food",
            "eat",
            "drink",
            "please",
            "some",
            "only",
            "now",
            "menu",
            "recommend",
            "suggest",
            "what",
            "which",
            "have",
            "available",
            "mood",
            "hungry",
            "craving",
            "nonveg",
            "non",
            "veg",
            "vegetarian",
            "vegan",
        }

        def extract_focus_terms(text: str) -> list[str]:
            return [
                token
                for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
                if len(token) >= 3 and token not in stopwords
            ]

        focus_terms = extract_focus_terms(msg_lower)
        if any(marker in msg_lower for marker in ("show more options", "more options", "show more")):
            previous_user_messages = [
                str(msg.content or "")
                for msg in context.messages[:-1]
                if msg.role == MessageRole.USER and str(msg.content or "").strip()
            ]
            for previous_text in reversed(previous_user_messages):
                prev_terms = extract_focus_terms(previous_text)
                if prev_terms:
                    focus_terms = prev_terms
                    break

        filtered_rows = rows
        if focus_terms:
            matched_rows = []
            for row in rows:
                haystack = f"{str(row.name or '').lower()} {str(row.category or '').lower()}"
                if any(term in haystack for term in focus_terms):
                    matched_rows.append(row)
            if matched_rows:
                filtered_rows = matched_rows

        deduped_rows = []
        seen_item_names: set[str] = set()
        for item in sorted(filtered_rows, key=lambda entry: str(entry.name or "").lower()):
            item_name = str(item.name or "").strip()
            if not item_name:
                continue
            key = item_name.lower()
            if key in seen_item_names:
                continue
            seen_item_names.add(key)
            deduped_rows.append(item)

        if not deduped_rows:
            return None

        lines = [f"- {item.name} - Rs.{float(item.price):.0f}" for item in deduped_rows]
        if focus_terms:
            preference_label = " ".join(focus_terms[:2]).strip()
        else:
            preference_label = (
                "non-veg" if prefer_nonveg and not prefer_veg else ("veg" if prefer_veg and not prefer_nonveg else "menu")
            )
        return {
            "match_type": "menu_recommendation",
            "response_text": (
                f"Here are all available {preference_label} options:\n\n"
                + "\n".join(lines)
                + "\n\nTell me what you'd like to order."
            ),
        }

    def _match_faq_bank_answer(
        self,
        message: str,
        context: ConversationContext,
        allow_pending: bool = False,
    ) -> Optional[dict]:
        """
        Match message against admin-defined FAQ bank.
        Keeps deterministic answers for common policy questions.
        """
        configured_intents = config_service.get_intents()
        if configured_intents and not config_service.is_intent_enabled("faq"):
            return None

        # Do not interrupt active transactional follow-ups.
        if not allow_pending and context.state not in {ConversationState.IDLE, ConversationState.COMPLETED}:
            return None
        if not allow_pending and context.pending_action:
            return None

        match = config_service.find_faq_entry(message)
        if not match:
            return None
        if not str(match.get("answer") or "").strip():
            return None
        return match

    def _match_service_information_response(
        self,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
        allow_pending: bool = False,
    ) -> Optional[dict]:
        """
        Match user queries against configured services so service metadata is always considered.
        """
        if not allow_pending and context.pending_action:
            return None
        if not allow_pending and context.state not in {ConversationState.IDLE, ConversationState.COMPLETED}:
            return None

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list) or not service_catalog:
            return None

        msg_lower = self._normalize_message_for_routing(message)
        if not msg_lower:
            return None
        if self._is_personal_reservation_query(msg_lower):
            return None

        action_query = any(
            marker in msg_lower
            for marker in ("book", "reserve", "order", "arrange", "need", "send", "request", "confirm", "want", "wanna")
        )
        info_query = any(
            marker in msg_lower
            for marker in (
                "what",
                "when",
                "where",
                "show",
                "list",
                "timing",
                "hours",
                "open",
                "close",
                "available",
                "provide",
                "provides",
                "offered",
                "offer",
                "offers",
                "tell me about",
                "details",
                "catalog",
                "treatment",
                "treatments",
                "package",
                "packages",
                "therapist",
                "therapists",
                "service",
            )
        )
        if not action_query and not info_query:
            return None

        msg_tokens = re.findall(r"[a-z0-9]+", msg_lower)
        matched_service: Optional[dict] = None
        best_score = 0.0
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            score = 0.0
            for alias in self._service_aliases_for_matching(service):
                score = max(score, self._service_alias_match_score(msg_lower, msg_tokens, alias))

            description_keywords = self._service_description_keywords(service)
            if description_keywords:
                desc_hits = sum(1 for token in description_keywords if self._token_matches_message(token, msg_tokens))
                if desc_hits >= 2:
                    score = max(score, 0.72 + min(0.2, desc_hits * 0.04))

            if score > best_score:
                best_score = score
                matched_service = service

        unsupported_label = self._extract_unconfigured_service_request_label(msg_lower, service_catalog)
        if matched_service is None or best_score < 0.72:
            if unsupported_label:
                return self._build_service_catalog_unavailable_match(unsupported_label)
            return None

        if unsupported_label and not self._service_entry_supports_request_label(matched_service, unsupported_label):
            return self._build_service_catalog_unavailable_match(unsupported_label)

        service_name = str(matched_service.get("name") or matched_service.get("id") or "This service").strip()
        service_id = str(matched_service.get("id") or "").strip()
        service_type = str(matched_service.get("type") or "").strip().lower()
        active = bool(matched_service.get("is_active", True))
        description = str(
            matched_service.get("description")
            or matched_service.get("cuisine")
            or ""
        ).strip()
        hours = matched_service.get("hours")
        if isinstance(hours, dict):
            open_time = str(hours.get("open") or "").strip()
            close_time = str(hours.get("close") or "").strip()
            hours_text = f"{open_time} - {close_time}".strip(" -") if (open_time or close_time) else ""
        else:
            hours_text = str(hours or "").strip()
        is_spa_service = self._is_spa_service_entry(matched_service)
        spa_catalog_query = self._looks_like_spa_catalog_query(msg_lower)
        spa_option_hints = self._extract_spa_option_hints(matched_service)
        details: list[str] = []
        if description:
            details.append(description)
        if hours_text:
            details.append(f"Hours: {hours_text}")
        details_text = " ".join(details).strip()

        if not active:
            alternatives = []
            for service in service_catalog:
                if not isinstance(service, dict):
                    continue
                if not bool(service.get("is_active", True)):
                    continue
                alt_type = str(service.get("type") or "").strip().lower()
                if service_type and alt_type and alt_type != service_type:
                    continue
                alt_name = str(service.get("name") or "").strip()
                if alt_name and alt_name.lower() != service_name.lower():
                    alternatives.append(alt_name)
                if len(alternatives) >= 3:
                    break
            alt_text = ""
            if alternatives:
                alt_text = f" Available alternatives: {', '.join(alternatives)}."
            return {
                "service_id": service_id,
                "service_name": service_name,
                "match_type": "inactive_service",
                "response_text": f"{service_name} is currently unavailable.{alt_text}",
                "suggested_actions": ["Show available services", "Talk to human"],
            }

        # Keep known transactional requests on normal handler path even if the
        # message also contains informational words (for example "book spa treatment").
        if action_query and self._service_action_should_route_to_transaction(matched_service, msg_lower):
            return None
        if action_query and not info_query:
            response_text = f"{service_name} is available."
            if details_text:
                response_text += f" {details_text}"
            response_text += " Please share your preferred time and required details so I can help next."
            return {
                "service_id": service_id,
                "service_name": service_name,
                "match_type": "service_catalog_action",
                "response_text": response_text,
                "suggested_actions": ["Share details", "Cancel"],
            }

        if is_spa_service and spa_catalog_query:
            spa_response = f"{service_name} is available."
            if details_text:
                spa_response += f" {details_text}"
            if spa_option_hints:
                spa_response += f"\n\nAvailable spa options: {', '.join(spa_option_hints[:8])}."
            spa_response += (
                "\n\nYou can ask for spa treatments or packages, or share a direct request like "
                "\"Book Swedish massage at 6 PM\"."
            )
            return {
                "service_id": service_id,
                "service_name": service_name,
                "match_type": "service_catalog_spa_info",
                "response_text": spa_response,
                "suggested_actions": [
                    "Show spa treatments",
                    "Show spa packages",
                    "Book spa at 6 PM",
                    "Talk to human",
                ],
            }
        if details_text:
            response_text = f"{service_name} is available. {details_text}"
        else:
            response_text = f"{service_name} is available."

        if action_query:
            response_text += " Please share your preferred time and any specific requirement so I can help next."

        return {
            "service_id": service_id,
            "service_name": service_name,
            "match_type": "service_catalog_info",
            "response_text": response_text,
            "suggested_actions": ["Show available services", "Ask another question", "Talk to human"],
        }

    @staticmethod
    def _service_aliases_for_matching(service: dict) -> list[str]:
        aliases: list[str] = []
        service_name = str(service.get("name") or "").strip().lower()
        service_id = str(service.get("id") or "").strip().lower().replace("_", " ")

        if service_name and (
            service_name not in _GENERIC_SERVICE_ALIASES or len(service_name.split()) > 1
        ):
            aliases.append(service_name)
        if (
            service_id
            and service_id != service_name
            and service_id not in _GENERIC_SERVICE_ALIASES
        ):
            aliases.append(service_id)

        # Add compact aliases by removing generic suffixes like "booking"/"service".
        compact_source_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", f"{service_name} {service_id}")
            if token
        ]
        removable_tokens = {
            "booking",
            "bookings",
            "service",
            "services",
            "request",
            "requests",
            "support",
            "desk",
            "assistance",
            "help",
        }
        compact_tokens = [token for token in compact_source_tokens if token not in removable_tokens]
        if compact_tokens:
            aliases.append(" ".join(compact_tokens[:3]))
            for token in compact_tokens[:3]:
                if (
                    len(token) >= 3
                    and token not in _GENERIC_SERVICE_ALIASES
                    and token not in _BROAD_FIRST_ALIAS_TOKENS
                    and token not in _SERVICE_DESCRIPTION_STOPWORDS
                ):
                    aliases.append(token)

        seen: set[str] = set()
        deduped: list[str] = []
        for alias in aliases:
            clean = alias.strip()
            if clean and clean not in seen:
                deduped.append(clean)
                seen.add(clean)
        return deduped

    @staticmethod
    def _is_spa_service_entry(service: dict[str, Any]) -> bool:
        text = " ".join(
            [
                str(service.get("id") or ""),
                str(service.get("name") or ""),
                str(service.get("type") or ""),
                str(service.get("description") or service.get("cuisine") or ""),
            ]
        ).strip().lower()
        if not text:
            return False
        return any(marker in text for marker in ("spa", "massage", "wellness", "therapy"))

    @staticmethod
    def _looks_like_spa_catalog_query(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        if not any(marker in text for marker in ("spa", "massage", "wellness", "therapy")):
            return False
        return any(
            marker in text
            for marker in (
                "show",
                "list",
                "treatment",
                "treatments",
                "package",
                "packages",
                "therapist",
                "therapists",
                "menu",
                "options",
            )
        )

    @staticmethod
    def _extract_spa_option_hints(service: dict[str, Any]) -> list[str]:
        description = str(service.get("description") or service.get("cuisine") or "").strip()
        if not description:
            return []
        candidates = re.split(r"[,|;/\n]+", description)
        options: list[str] = []
        blocked_fragments = {
            "spa",
            "booking",
            "service",
            "services",
            "appointment",
            "appointments",
            "available",
            "timing",
            "hours",
            "assistance",
            "support",
        }
        for candidate in candidates:
            cleaned = re.sub(r"\s+", " ", str(candidate or "").strip(" .-"))
            if len(cleaned) < 4 or len(cleaned) > 40:
                continue
            lowered = cleaned.lower()
            if any(fragment in lowered for fragment in blocked_fragments):
                continue
            title = cleaned.title()
            if title not in options:
                options.append(title)
            if len(options) >= 8:
                break
        return options

    def _extract_unconfigured_service_request_label(
        self,
        msg_lower: str,
        service_catalog: list[dict[str, Any]],
    ) -> str:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return ""

        action_markers = ("book", "reserve", "order", "arrange", "need", "want", "request", "schedule")
        if not any(marker in text for marker in action_markers):
            return ""

        known_tokens: set[str] = set()
        known_aliases: set[str] = set()
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            for alias in self._service_aliases_for_matching(service):
                alias_text = str(alias or "").strip().lower()
                if alias_text:
                    known_aliases.add(alias_text)
                known_tokens.update(re.findall(r"[a-z0-9]+", alias.lower()))

        match = re.search(
            r"\b(?:book|reserve|order|arrange|need|want|get|request|schedule)\b\s+(?:a|an|the|for)?\s*([a-z][a-z0-9&'/ -]{2,48})",
            text,
        )
        if not match:
            return ""

        candidate = re.split(r"\b(?:for|at|on|to|from|in|by|with|tomorrow|today|tonight)\b", match.group(1), maxsplit=1)[0]
        candidate = re.sub(r"\s+", " ", candidate).strip(" .,!?:;")
        if not candidate:
            return ""

        generic_tokens = {
            "booking",
            "reservation",
            "service",
            "services",
            "help",
            "assistance",
            "something",
            "anything",
        }
        supported_core_tokens = {
            "room",
            "table",
            "food",
            "menu",
            "spa",
            "massage",
            "restaurant",
            "dining",
        }
        candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate))
        candidate_tokens -= generic_tokens
        if not candidate_tokens:
            return ""
        if candidate_tokens & supported_core_tokens:
            return ""
        if candidate_tokens & known_tokens:
            return ""

        return candidate.title()

    @staticmethod
    def _service_entry_supports_request_label(service: dict[str, Any], requested_label: str) -> bool:
        """
        Validate whether a matched service actually covers the requested label.
        Prevents broad aliases (for example generic transport) from auto-mapping
        to a different sub-service.
        """
        if not isinstance(service, dict):
            return False
        label_text = str(requested_label or "").strip().lower()
        if not label_text:
            return False

        service_text = " ".join(
            [
                str(service.get("id") or ""),
                str(service.get("name") or ""),
                str(service.get("type") or ""),
                str(service.get("description") or service.get("cuisine") or ""),
            ]
        ).strip().lower()
        if not service_text:
            return False

        ignored_tokens = {
            "booking",
            "service",
            "services",
            "request",
            "requests",
            "support",
            "assistance",
            "help",
            "instant",
            "manual",
        }
        label_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", label_text)
            if len(token) >= 3 and token not in ignored_tokens
        ]
        if not label_tokens:
            return False

        service_tokens = re.findall(r"[a-z0-9]+", service_text)
        matched_tokens = 0
        for token in label_tokens:
            if re.search(rf"\b{re.escape(token)}\b", service_text):
                matched_tokens += 1
                continue
            if len(token) >= 4 and any(
                (
                    len(service_token) >= 4
                    and (
                        service_token.startswith(token[:4])
                        or token.startswith(service_token[:4])
                    )
                )
                for service_token in service_tokens
            ):
                matched_tokens += 1

        required_matches = len(label_tokens) if len(label_tokens) <= 2 else max(2, len(label_tokens) - 1)
        return matched_tokens >= required_matches

    @staticmethod
    def _build_service_catalog_unavailable_match(requested_service_label: str) -> dict[str, Any]:
        label = str(requested_service_label or "").strip() or "This request"
        return {
            "service_id": "",
            "service_name": label,
            "match_type": "service_catalog_unavailable_handoff",
            "response_text": (
                f"{label} is not currently configured for instant bot fulfillment. "
                "I can connect you with our staff team to assist manually."
            ),
            "suggested_actions": ["Connect to staff", "Show available services", "Ask another question"],
        }

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

    @classmethod
    def _phase_label(cls, phase_id: str) -> str:
        normalized = cls._normalize_phase_identifier(phase_id)
        if not normalized:
            return "Current"
        try:
            phases = config_service.get_journey_phases()
        except Exception:
            phases = []
        if isinstance(phases, list):
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                candidate_id = cls._normalize_phase_identifier(phase.get("id"))
                if candidate_id == normalized:
                    name = str(phase.get("name") or "").strip()
                    if name:
                        return name
        return normalized.replace("_", " ").title()

    @classmethod
    def _phase_transition_timing_hint(
        cls,
        *,
        current_phase_id: str,
        service_phase_id: str,
    ) -> str:
        current_norm = cls._normalize_phase_identifier(current_phase_id)
        service_norm = cls._normalize_phase_identifier(service_phase_id)
        if not current_norm or not service_norm or current_norm == service_norm:
            return ""

        if current_norm == "pre_booking":
            if service_norm == "pre_checkin":
                return "after your booking is confirmed"
            if service_norm in {"during_stay", "post_checkout"}:
                return "after check-in"
        if current_norm == "pre_checkin":
            if service_norm == "during_stay":
                return "once you check in"
            if service_norm == "post_checkout":
                return "after checkout"
        if current_norm == "during_stay" and service_norm == "post_checkout":
            return "after checkout"
        return ""

    @classmethod
    def _build_phase_gate_sales_response(
        cls,
        *,
        service_name: str,
        current_phase_id: str,
        current_phase_name: str,
        service_phase_id: str = "",
        service_phase_name: str = "",
        available_names: list[str] | None = None,
    ) -> str:
        service_label = str(service_name or "").strip() or "This service"
        response_parts = [f"{service_label} is not available for {current_phase_name} phase."]

        if service_phase_name:
            response_parts.append(f"It is available in {service_phase_name} phase.")
            timing_hint = cls._phase_transition_timing_hint(
                current_phase_id=current_phase_id,
                service_phase_id=service_phase_id,
            )
            if timing_hint:
                response_parts.append(
                    f"Great news: we do offer {service_label} at the hotel, and you can request it {timing_hint}."
                )
            else:
                response_parts.append(f"Great news: we do offer {service_label} at the hotel.")

        curated_names = [str(name).strip() for name in (available_names or []) if str(name).strip()]
        curated_names = curated_names[:4]
        if curated_names:
            response_parts.append(f"Right now, I can help you with {', '.join(curated_names)}.")
        return " ".join(response_parts).strip()

    def _resolve_current_ticketing_phase(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        integration = ticketing_service.get_integration_context(context)
        candidates = (
            entity_map.get("phase"),
            pending.get("phase"),
            integration.get("phase"),
        )
        for candidate in candidates:
            normalized = self._normalize_phase_identifier(candidate)
            if normalized:
                return normalized

        flow = str(integration.get("flow") or integration.get("bot_mode") or "").strip().lower()
        if flow in {"engage", "booking", "booking_bot"}:
            return "pre_booking"
        return "during_stay"

    def _extract_phase_from_pending_data(self, pending_data: dict[str, Any] | None) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        integration = pending.get("_integration", {})
        integration_phase = ""
        if isinstance(integration, dict):
            integration_phase = self._normalize_phase_identifier(integration.get("phase"))
        if integration_phase:
            return integration_phase
        return self._normalize_phase_identifier(pending.get("phase"))

    def _get_selected_phase_context(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        if not phase_id:
            return {"selected_phase_id": "", "selected_phase_name": ""}
        return {
            "selected_phase_id": phase_id,
            "selected_phase_name": self._phase_label(phase_id),
        }

    @staticmethod
    def _memory_facts_from_context(context: ConversationContext) -> dict[str, Any]:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        memory = pending.get("_memory", {})
        if not isinstance(memory, dict):
            return {}
        facts = memory.get("facts", {})
        return facts if isinstance(facts, dict) else {}

    @staticmethod
    def _parse_compact_calendar_date(value: Any, *, reference_year: int | None = None) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()

        iso_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if iso_match:
            try:
                return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
            except ValueError:
                return None

        month_first_match = re.fullmatch(
            r"([A-Za-z]{3,9})\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?",
            text,
            flags=re.IGNORECASE,
        )
        day_first_match = re.fullmatch(
            r"(\d{1,2})\s+([A-Za-z]{3,9})(?:\s*,?\s*(\d{4}))?",
            text,
            flags=re.IGNORECASE,
        )

        parsed: date | None = None
        if month_first_match:
            month_token = month_first_match.group(1)
            day_token = month_first_match.group(2)
            year_token = month_first_match.group(3)
            fmt = "%b %d %Y"
            fmt_long = "%B %d %Y"
            year = int(year_token) if year_token else int(reference_year or date.today().year)
            compact = f"{month_token} {day_token} {year}"
            for parser in (fmt, fmt_long):
                try:
                    parsed = datetime.strptime(compact, parser).date()
                    break
                except ValueError:
                    continue
        elif day_first_match:
            day_token = day_first_match.group(1)
            month_token = day_first_match.group(2)
            year_token = day_first_match.group(3)
            fmt = "%d %b %Y"
            fmt_long = "%d %B %Y"
            year = int(year_token) if year_token else int(reference_year or date.today().year)
            compact = f"{day_token} {month_token} {year}"
            for parser in (fmt, fmt_long):
                try:
                    parsed = datetime.strptime(compact, parser).date()
                    break
                except ValueError:
                    continue

        if parsed is None:
            return None

        if reference_year is None and not re.search(r"\b\d{4}\b", text):
            today_local = date.today()
            if parsed < (today_local - timedelta(days=183)):
                try:
                    parsed = parsed.replace(year=parsed.year + 1)
                except ValueError:
                    pass
        return parsed

    @classmethod
    def _extract_explicit_dates_from_message(cls, message: str) -> list[date]:
        text = str(message or "").strip().lower()
        if not text:
            return []

        dates: list[date] = []
        seen: set[str] = set()
        month_token = (
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        )

        patterns = (
            rf"\b({month_token})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?\b",
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_token})(?:\s*,?\s*(\d{{4}}))?\b",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                chunk = " ".join(part for part in match.groups() if part)
                parsed = cls._parse_compact_calendar_date(chunk)
                if parsed is None:
                    continue
                key = parsed.isoformat()
                if key in seen:
                    continue
                seen.add(key)
                dates.append(parsed)

        today_local = date.today()
        relative_markers = {
            "today": today_local,
            "tomorrow": today_local + timedelta(days=1),
            "day after tomorrow": today_local + timedelta(days=2),
        }
        for marker, parsed in relative_markers.items():
            if marker in text:
                key = parsed.isoformat()
                if key in seen:
                    continue
                seen.add(key)
                dates.append(parsed)

        return dates

    def _resolve_known_stay_window(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> tuple[date, date] | None:
        pending = pending_data if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        integration = ticketing_service.get_integration_context(context)
        facts = self._memory_facts_from_context(context)

        check_in_text = self._first_non_empty(
            entity_map.get("check_in"),
            entity_map.get("stay_checkin_date"),
            pending.get("check_in"),
            pending.get("stay_checkin_date"),
            pending.get("checkin_date"),
            integration.get("check_in"),
            integration.get("stay_checkin_date"),
            facts.get("check_in"),
            facts.get("stay_checkin_date"),
            facts.get("checkin_date"),
        )
        check_out_text = self._first_non_empty(
            entity_map.get("check_out"),
            entity_map.get("stay_checkout_date"),
            pending.get("check_out"),
            pending.get("stay_checkout_date"),
            pending.get("checkout_date"),
            integration.get("check_out"),
            integration.get("stay_checkout_date"),
            facts.get("check_out"),
            facts.get("stay_checkout_date"),
            facts.get("checkout_date"),
        )
        if not check_in_text or not check_out_text:
            return None

        current_year = date.today().year
        parsed_check_in = self._parse_compact_calendar_date(check_in_text, reference_year=current_year)
        parsed_check_out = self._parse_compact_calendar_date(check_out_text, reference_year=current_year)
        if parsed_check_in is None or parsed_check_out is None:
            return None

        if parsed_check_out < parsed_check_in:
            try:
                parsed_check_out = parsed_check_out.replace(year=parsed_check_out.year + 1)
            except ValueError:
                return None
        if parsed_check_out < parsed_check_in:
            return None
        return (parsed_check_in, parsed_check_out)

    @classmethod
    def _format_calendar_date_label(cls, value: date) -> str:
        try:
            return value.strftime("%b %d")
        except Exception:
            return str(value)

    @staticmethod
    def _looks_like_booking_change_request(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        change_markers = ("cancel", "cancellation", "modify", "change", "postpone", "reschedule", "update")
        booking_markers = (
            "booking",
            "reservation",
            "check in",
            "check-in",
            "check out",
            "check-out",
            "stay",
            "pickup",
            "drop",
            "transfer",
            "room",
            "table",
            "spa",
            "appointment",
        )
        return any(marker in text for marker in change_markers) and any(marker in text for marker in booking_markers)

    @staticmethod
    def _resolve_booking_change_target(message: str) -> str:
        text = str(message or "").strip().lower()
        if not text:
            return ""
        if any(marker in text for marker in ("pickup", "drop", "transfer", "airport", "cab", "taxi", "transport")):
            return "transport"
        if any(marker in text for marker in ("spa", "massage", "treatment", "wellness", "appointment")):
            return "spa_booking"
        if any(marker in text for marker in ("table", "restaurant", "dining", "reservation")):
            return "table_booking"
        if any(marker in text for marker in ("room", "stay", "check in", "check-in", "check out", "check-out")):
            return "room_booking"
        return ""

    def _phase_service_names_for_context(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
        limit: int = 5,
    ) -> list[str]:
        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            return []
        current_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        names: list[str] = []
        seen_names: set[str] = set()
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            if self._normalize_phase_identifier(service.get("phase_id")) != current_phase:
                continue
            name = str(service.get("name") or service.get("id") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            names.append(name)
            if len(names) >= max(1, int(limit)):
                break
        return names

    def _detect_contextual_sanity_issue(
        self,
        *,
        message: str,
        intent: IntentType,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        pending = pending_data if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        text = str(message or "").strip()
        msg_lower = text.lower()
        if not text:
            return None

        actionable_intents = {
            IntentType.ORDER_FOOD,
            IntentType.TABLE_BOOKING,
            IntentType.ROOM_SERVICE,
            IntentType.COMPLAINT,
            IntentType.HUMAN_REQUEST,
        }
        is_action_like = (
            intent in actionable_intents
            or self._is_action_request_text(text)
            or self._looks_like_operational_issue_for_ticketing(msg_lower)
            or self._looks_like_booking_change_request(text)
        )

        if is_action_like:
            stay_window = self._resolve_known_stay_window(
                context=context,
                pending_data=pending,
                entities=entity_map,
            )
            requested_dates = self._extract_explicit_dates_from_message(text)
            if stay_window is not None and requested_dates:
                check_in, check_out = stay_window
                out_of_window = [day for day in requested_dates if day < check_in or day > check_out]
                if out_of_window:
                    check_in_label = self._format_calendar_date_label(check_in)
                    check_out_label = self._format_calendar_date_label(check_out)
                    return {
                        "code": "request_date_outside_stay_window",
                        "response_text": (
                            f"That date is outside your current stay window ({check_in_label} to {check_out_label}). "
                            "Please share a date within your stay, or tell me if you want to modify the stay dates first."
                        ),
                        "suggested_actions": [
                            "Share date within stay",
                            "Modify stay dates",
                            "Show available services",
                        ],
                    }

        complaint_like_intent = intent in {
            IntentType.COMPLAINT,
            IntentType.HUMAN_REQUEST,
            IntentType.FAQ,
            IntentType.MENU_REQUEST,
        }
        if complaint_like_intent and self._looks_like_operational_issue_for_ticketing(msg_lower):
            phase_context = self._get_selected_phase_context(
                context=context,
                pending_data=pending,
                entities=entity_map,
            )
            phase_id = self._normalize_phase_identifier(phase_context.get("selected_phase_id"))
            if phase_id in {"pre_booking", "pre_checkin"}:
                phase_name = str(phase_context.get("selected_phase_name") or self._phase_label(phase_id)).strip()
                return {
                    "code": "operational_issue_outside_stay_context",
                    "response_text": (
                        f"This sounds like an in-stay room support issue, but your current phase is {phase_name}. "
                        "If this is for an upcoming stay, please share booking details and room number (if assigned), "
                        "or tell me if you want pre-arrival support."
                    ),
                    "suggested_actions": [
                        "Share booking details",
                        "Need pre-arrival support",
                        "Talk to human",
                    ],
                }

        if self._looks_like_booking_change_request(text):
            target = self._resolve_booking_change_target(text)
            if not target:
                phase_context = self._get_selected_phase_context(
                    context=context,
                    pending_data=pending,
                    entities=entity_map,
                )
                phase_services = self._phase_service_names_for_context(
                    context=context,
                    pending_data=pending,
                    entities=entity_map,
                    limit=5,
                )
                response_text = (
                    "I can help with cancellation or modification. "
                    "Please tell me what you want to change: room booking, airport transfer, table reservation, or spa appointment."
                )
                if phase_services:
                    response_text = (
                        f"{response_text} In {phase_context.get('selected_phase_name') or 'this phase'}, "
                        f"available services include {', '.join(phase_services)}."
                    )
                return {
                    "code": "booking_change_target_ambiguous",
                    "response_text": response_text,
                    "suggested_actions": [
                        "Room booking",
                        "Airport transfer",
                        "Table reservation",
                        "Spa appointment",
                    ],
                }

        return None

    @classmethod
    def _is_action_request_text(cls, message: str) -> bool:
        text = cls._normalize_message_for_routing(message)
        if not text:
            return False
        action_markers = (
            "book",
            "reserve",
            "order",
            "arrange",
            "need",
            "request",
            "cancel",
            "modify",
            "status",
            "wanna",
            "want",
            "confirm",
            "schedule",
        )
        return any(marker in text for marker in action_markers)

    def _match_phase_service_from_rows(
        self,
        *,
        message: str,
        services: list[dict[str, Any]],
        min_score: float = 0.72,
    ) -> dict[str, Any] | None:
        text = self._normalize_message_for_routing(message)
        if not text or not isinstance(services, list):
            return None
        tokens = re.findall(r"[a-z0-9]+", text)
        if not tokens:
            return None

        best_service: dict[str, Any] | None = None
        best_score = 0.0
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if not phase_id:
                continue
            score = 0.0
            for alias in self._service_aliases_for_matching(service):
                score = max(score, self._service_alias_match_score(text, tokens, alias))
            if score > best_score:
                best_score = score
                best_service = service

        if best_service is None or best_score < float(min_score):
            return None

        result = {
            "service_id": str(best_service.get("id") or "").strip(),
            "service_name": str(best_service.get("name") or best_service.get("id") or "This service").strip(),
            "phase_id": self._normalize_phase_identifier(best_service.get("phase_id")),
            "score": best_score,
        }
        if "ticketing_enabled" in best_service:
            result["ticketing_enabled"] = best_service.get("ticketing_enabled")
        return result

    @staticmethod
    def _extract_requested_service_label_for_phase_gate(message: str) -> str:
        text = str(message or "").strip().lower()
        if not text:
            return ""

        at_match = re.search(r"\b(?:table|booking|reservation|book)\b[^.?!]{0,40}\bat\s+([a-z][a-z0-9&' -]{2,40})", text)
        if at_match:
            candidate = at_match.group(1)
        else:
            candidate = ""

        if not candidate:
            request_match = re.search(
                r"\b(?:book|reserve|order|arrange|need|want|request|schedule)\b\s+(?:a|an|the|for)?\s*([a-z][a-z0-9&'/-]{2,48})",
                text,
            )
            if request_match:
                candidate = request_match.group(1)

        if not candidate:
            return ""

        candidate = re.split(
            r"\b(?:for|on|to|from|in|by|with|today|tomorrow|tonight|pm|am)\b",
            candidate,
            maxsplit=1,
        )[0]
        candidate = re.sub(r"\s+", " ", candidate).strip(" .,!?:;")
        if not candidate:
            return ""

        generic = {
            "table",
            "booking",
            "reservation",
            "service",
            "services",
            "food",
            "room",
            "restaurant",
            "transport",
        }
        tokens = [token for token in re.findall(r"[a-z0-9]+", candidate) if token]
        tokens = [token for token in tokens if token not in generic]
        if not tokens:
            return ""

        return " ".join(tokens).title()

    @staticmethod
    def _phase_gate_intent_label(intent: IntentType) -> str:
        mapping = {
            IntentType.ORDER_FOOD: "Food ordering",
            IntentType.TABLE_BOOKING: "Booking",
            IntentType.ROOM_SERVICE: "Room service",
        }
        return mapping.get(intent, "This service")

    @staticmethod
    def _has_spa_booking_marker(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        markers = ("spa", "massage", "wellness", "therapy", "treatment", "recreation", "pool")
        return any(re.search(rf"\b{re.escape(marker)}\b", text) for marker in markers)

    @staticmethod
    def _has_transport_booking_marker(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        markers = (
            "transport",
            "cab",
            "taxi",
            "pickup",
            "pick up",
            "drop",
            "airport transfer",
            "ride",
            "shuttle",
        )
        return any(re.search(rf"\b{re.escape(marker)}\b", text) for marker in markers)

    @staticmethod
    def _infer_phase_gate_transactional_intent(message: str) -> IntentType | None:
        text = str(message or "").strip().lower()
        if not text:
            return None

        def _has_marker(markers: tuple[str, ...]) -> bool:
            for marker in markers:
                pattern = rf"\b{re.escape(marker)}\b"
                if re.search(pattern, text):
                    return True
            return False

        if ChatService._looks_like_room_stay_booking_request(text):
            return IntentType.TABLE_BOOKING

        room_markers = ("room service", "housekeeping", "maintenance", "laundry", "amenity")
        if _has_marker(room_markers):
            return IntentType.ROOM_SERVICE

        food_markers = ("hungry", "food", "dining", "menu", "meal", "eat", "in-room dining")
        if _has_marker(food_markers):
            return IntentType.ORDER_FOOD

        booking_markers = (
            "new booking",
            "pre booking",
            "pre-booking",
            "spa",
            "treatment",
            "table",
            "reservation",
            "restaurant",
            "recreation",
            "room booking",
            "room discovery",
            "stay booking",
            "book a room",
            "book room",
            "transport",
            "cab",
            "taxi",
            "pickup",
            "drop",
            "airport transfer",
            "ride",
            "shuttle",
        )
        if _has_marker(booking_markers):
            return IntentType.TABLE_BOOKING
        return None

    @staticmethod
    def _infer_all_phase_gate_transactional_intents(message: str) -> list[IntentType]:
        text = str(message or "").strip().lower()
        if not text:
            return []

        def _has_marker(markers: tuple[str, ...]) -> bool:
            for marker in markers:
                pattern = rf"\b{re.escape(marker)}\b"
                if re.search(pattern, text):
                    return True
            return False

        intents: list[IntentType] = []
        if ChatService._looks_like_room_stay_booking_request(text):
            intents.append(IntentType.TABLE_BOOKING)
        room_markers = ("room service", "housekeeping", "maintenance", "laundry", "amenity")
        food_markers = ("hungry", "food", "dining", "menu", "meal", "eat", "in-room dining")
        booking_markers = (
            "new booking",
            "pre booking",
            "pre-booking",
            "spa",
            "treatment",
            "table",
            "reservation",
            "restaurant",
            "recreation",
            "room booking",
            "room discovery",
            "stay booking",
            "book a room",
            "book room",
            "transport",
            "cab",
            "taxi",
            "pickup",
            "drop",
            "airport transfer",
            "ride",
            "shuttle",
        )

        if _has_marker(booking_markers):
            intents.append(IntentType.TABLE_BOOKING)
        if _has_marker(food_markers):
            intents.append(IntentType.ORDER_FOOD)
        if _has_marker(room_markers):
            intents.append(IntentType.ROOM_SERVICE)
        return intents

    def _infer_phase_gate_ticketing_intent(
        self,
        *,
        message: str,
        intent: IntentType,
    ) -> IntentType | None:
        if intent not in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
            return None
        inferred = self._infer_phase_gate_transactional_intent(message)
        if inferred is not None:
            return inferred
        if self._looks_like_operational_issue_for_ticketing(message):
            return IntentType.ROOM_SERVICE
        return None

    @staticmethod
    def _service_phase_gate_text_blob(service: dict[str, Any]) -> str:
        if not isinstance(service, dict):
            return ""
        fields = (
            service.get("id"),
            service.get("name"),
            service.get("type"),
            service.get("description"),
            service.get("cuisine"),
        )
        return " ".join(str(field or "").strip().lower() for field in fields if str(field or "").strip())

    def _phase_has_intent_compatible_service(
        self,
        *,
        intent: IntentType,
        phase_services: list[dict[str, Any]],
        strict: bool = False,
    ) -> bool:
        marker_map = _PHASE_INTENT_SERVICE_MARKERS_STRICT if strict else _PHASE_INTENT_SERVICE_MARKERS
        markers = marker_map.get(intent, ())
        if not markers:
            return False
        for service in phase_services:
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            for marker in markers:
                if re.search(rf"\b{re.escape(marker)}\b", text):
                    return True
        return False

    def _phase_has_room_booking_support(self, phase_services: list[dict[str, Any]]) -> bool:
        room_markers = (
            "room booking",
            "room discovery",
            "stay booking",
            "room",
            "suite",
            "check in",
            "check-out",
            "check out",
            "checkin",
            "checkout",
        )
        for service in phase_services:
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            for marker in room_markers:
                if re.search(rf"\b{re.escape(marker)}\b", text):
                    return True
        return False

    def _phase_has_booking_subtype_support(
        self,
        *,
        phase_services: list[dict[str, Any]],
        subtype: str,
    ) -> bool:
        subtype_key = str(subtype or "").strip().lower()
        marker_map = {
            "spa": ("spa", "massage", "wellness", "therapy", "treatment", "recreation", "pool"),
            "transport": (
                "transport",
                "cab",
                "taxi",
                "pickup",
                "pick up",
                "drop",
                "airport transfer",
                "ride",
                "shuttle",
            ),
        }
        markers = marker_map.get(subtype_key, ())
        if not markers:
            return False
        for service in phase_services:
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            for marker in markers:
                if re.search(rf"\b{re.escape(marker)}\b", text):
                    return True
        return False

    def _phase_services_include_requested_label(
        self,
        *,
        requested_label: str,
        phase_services: list[dict[str, Any]],
    ) -> bool:
        label = str(requested_label or "").strip().lower()
        if not label:
            return False
        label_tokens = [token for token in re.findall(r"[a-z0-9]+", label) if len(token) >= 3]
        for service in phase_services:
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            if label in text:
                return True
            if label_tokens and any(token in text for token in label_tokens):
                return True
        return False

    def _find_intent_compatible_service(
        self,
        *,
        intent: IntentType,
        services: list[dict[str, Any]],
        exclude_phase_id: str = "",
    ) -> dict[str, Any] | None:
        marker_map = _PHASE_INTENT_SERVICE_MARKERS
        markers = marker_map.get(intent, ())
        if not markers:
            return None
        excluded = self._normalize_phase_identifier(exclude_phase_id)
        best: dict[str, Any] | None = None
        best_score = 0

        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if excluded and phase_id == excluded:
                continue
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            score = 0
            for marker in markers:
                if re.search(rf"\b{re.escape(marker)}\b", text):
                    score += 1
            if score > best_score:
                best_score = score
                best = service
        if best is None or best_score <= 0:
            return None
        return best

    def _phase_gate_additional_unavailable_lines(
        self,
        *,
        message: str,
        current_phase_id: str,
        all_phase_services: list[dict[str, Any]],
        phase_services: list[dict[str, Any]],
        primary_service_name: str = "",
    ) -> list[str]:
        inferred_intents = self._infer_all_phase_gate_transactional_intents(message)
        if not inferred_intents:
            return []

        current_phase_name = self._phase_label(current_phase_id)
        primary_norm = str(primary_service_name or "").strip().lower()
        has_spa_marker = self._has_spa_booking_marker(message)
        has_transport_marker = self._has_transport_booking_marker(message)
        room_only_booking = (
            self._looks_like_room_stay_booking_request(message)
            and not has_spa_marker
            and not has_transport_marker
        )
        lines: list[str] = []
        seen_keys: set[str] = set()
        for inferred_intent in inferred_intents:
            if inferred_intent == IntentType.TABLE_BOOKING:
                if has_spa_marker:
                    if self._phase_has_booking_subtype_support(
                        phase_services=phase_services,
                        subtype="spa",
                    ):
                        continue
                elif has_transport_marker:
                    if self._phase_has_booking_subtype_support(
                        phase_services=phase_services,
                        subtype="transport",
                    ):
                        continue
                elif room_only_booking:
                    if self._phase_has_room_booking_support(phase_services):
                        continue
                elif self._phase_has_intent_compatible_service(
                    intent=inferred_intent,
                    phase_services=phase_services,
                ):
                    continue
            elif self._phase_has_intent_compatible_service(
                intent=inferred_intent,
                phase_services=phase_services,
            ):
                continue
            fallback_service = self._find_intent_compatible_service(
                intent=inferred_intent,
                services=all_phase_services,
                exclude_phase_id=current_phase_id,
            )
            label = self._phase_gate_intent_label(inferred_intent)
            if fallback_service is None:
                key = f"{label.lower()}::{current_phase_id}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                lines.append(f"{label} is not available for {current_phase_name} phase.")
                continue

            service_name = str(fallback_service.get("name") or fallback_service.get("id") or label).strip() or label
            if primary_norm and service_name.lower() == primary_norm:
                continue
            service_phase_id = self._normalize_phase_identifier(fallback_service.get("phase_id"))
            service_phase_name = self._phase_label(service_phase_id)
            timing_hint = self._phase_transition_timing_hint(
                current_phase_id=current_phase_id,
                service_phase_id=service_phase_id,
            )
            unavailable_key = f"{service_name.lower()}::not_available::{current_phase_id}"
            if unavailable_key not in seen_keys:
                seen_keys.add(unavailable_key)
                lines.append(f"{service_name} is not available for {current_phase_name} phase.")
            key = f"{service_name.lower()}::{service_phase_id}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if timing_hint:
                lines.append(
                    f"{service_name} is available in {service_phase_name} phase, and you can request it {timing_hint}."
                )
            else:
                lines.append(f"{service_name} is available in {service_phase_name} phase.")
        return lines

    def _match_phase_managed_service_request(self, message: str) -> dict[str, Any] | None:
        text = self._normalize_message_for_routing(message)
        if not text:
            return None

        query_markers = (
            "book",
            "reserve",
            "order",
            "arrange",
            "need",
            "request",
            "want",
            "wanna",
            "cancel",
            "modify",
            "status",
            "what",
            "when",
            "where",
            "show",
            "list",
            "details",
            "available",
            "have",
            "provide",
            "offer",
            "services",
            "support",
            "help",
        )
        if not any(marker in text for marker in query_markers):
            return None

        tokens = re.findall(r"[a-z0-9]+", text)
        if not tokens:
            return None

        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            return None

        return self._match_phase_service_from_rows(
            message=message,
            services=services,
            min_score=0.82,
        )

    def _detect_ticketing_phase_service_mismatch(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            services = []

        matched_service = self._resolve_service_from_phase_signals(
            services=services,
            entities=entities if isinstance(entities, dict) else {},
            pending_data=pending_data if isinstance(pending_data, dict) else {},
        )
        llm_only_phase_gate = bool(getattr(settings, "chat_phase_gate_llm_only", True))
        if matched_service is None:
            if llm_only_phase_gate:
                # LLM-first mode still needs a semantic safety-net when service_id is omitted.
                matched_service = self._match_phase_service_from_rows(
                    message=message,
                    services=services,
                    min_score=0.72,
                )
            else:
                matched_service = self._match_phase_managed_service_request(message)
        if matched_service is None:
            return None

        current_phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        service_phase_id = self._normalize_phase_identifier(matched_service.get("phase_id"))
        if not current_phase_id or not service_phase_id or service_phase_id == current_phase_id:
            return None

        service_name = str(matched_service.get("service_name") or "This service").strip()
        current_phase_name = self._phase_label(current_phase_id)
        service_phase_name = self._phase_label(service_phase_id)
        current_phase_services = [
            service
            for service in services
            if isinstance(service, dict)
            and bool(service.get("is_active", True))
            and self._normalize_phase_identifier(service.get("phase_id")) == current_phase_id
        ]
        available_names = [
            str(service.get("name") or service.get("id") or "").strip()
            for service in current_phase_services
            if str(service.get("name") or service.get("id") or "").strip()
        ]
        response_text = self._build_phase_gate_sales_response(
            service_name=service_name,
            current_phase_id=current_phase_id,
            current_phase_name=current_phase_name,
            service_phase_id=service_phase_id,
            service_phase_name=service_phase_name,
            available_names=available_names,
        )
        additional_lines = self._phase_gate_additional_unavailable_lines(
            message=message,
            current_phase_id=current_phase_id,
            all_phase_services=[
                service
                for service in services
                if isinstance(service, dict)
                and bool(service.get("is_active", True))
                and self._normalize_phase_identifier(service.get("phase_id"))
            ],
            phase_services=current_phase_services,
            primary_service_name=service_name,
        )
        if additional_lines:
            response_text = f"{response_text} {' '.join(additional_lines)}".strip()
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()
        return {
            "service_id": str(matched_service.get("service_id") or "").strip(),
            "service_name": service_name,
            "current_phase_id": current_phase_id,
            "current_phase_name": current_phase_name,
            "service_phase_id": service_phase_id,
            "service_phase_name": service_phase_name,
            "response_text": response_text,
        }

    def _detect_phase_service_unavailable_for_intent(
        self,
        *,
        message: str,
        intent: IntentType,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Enforce phase-specific service availability for transactional requests.
        If no matching active service exists in current phase, decline politely.
        """
        transactional_intents = {
            IntentType.TABLE_BOOKING,
            IntentType.ORDER_FOOD,
            IntentType.ROOM_SERVICE,
        }
        is_transactional = intent in transactional_intents
        is_action_request = self._is_action_request_text(message)
        is_faq_action = intent in {IntentType.FAQ, IntentType.MENU_REQUEST} and is_action_request
        is_faq_operational_issue = (
            intent in {IntentType.FAQ, IntentType.MENU_REQUEST}
            and self._looks_like_operational_issue_for_ticketing(message)
        )
        is_ticketing_like = intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}
        ticketing_intent_hint = self._infer_phase_gate_ticketing_intent(
            message=message,
            intent=intent,
        )
        if not is_transactional and not is_faq_action and not is_faq_operational_issue and not (
            is_ticketing_like and ticketing_intent_hint is not None
        ):
            return None

        if context.pending_action in {
            "confirm_order",
            "confirm_booking",
            "confirm_room_booking",
            "confirm_room_availability_check",
            "confirm_service_request",
            "select_service",
            "select_restaurant",
            "collect_booking_party_size",
            "collect_booking_time",
            "collect_room_booking_details",
            "select_room_type",
            "collect_transport_details",
        }:
            return None

        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            services = []

        all_phase_services = [
            service
            for service in services
            if isinstance(service, dict)
            and bool(service.get("is_active", True))
            and self._normalize_phase_identifier(service.get("phase_id"))
        ]
        if not all_phase_services:
            return None

        current_phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        if not current_phase_id:
            return None

        phase_services = [
            service
            for service in all_phase_services
            if self._normalize_phase_identifier(service.get("phase_id")) == current_phase_id
        ]
        is_room_booking_request = self._looks_like_room_stay_booking_request(message)
        has_spa_marker = self._has_spa_booking_marker(message)
        has_transport_marker = self._has_transport_booking_marker(message)
        room_only_booking = (
            is_room_booking_request
            and not has_spa_marker
            and not has_transport_marker
        )
        phase_supports_room_booking = self._phase_has_room_booking_support(phase_services)

        resolved_signal_service = self._resolve_service_from_phase_signals(
            services=all_phase_services,
            entities=entities if isinstance(entities, dict) else {},
            pending_data=pending_data if isinstance(pending_data, dict) else {},
        )
        llm_only_phase_gate = bool(getattr(settings, "chat_phase_gate_llm_only", True))
        if llm_only_phase_gate and resolved_signal_service is not None:
            signal_service_phase = self._normalize_phase_identifier(resolved_signal_service.get("phase_id"))
            if not signal_service_phase:
                return None
            if signal_service_phase == current_phase_id:
                additional_lines = self._phase_gate_additional_unavailable_lines(
                    message=message,
                    current_phase_id=current_phase_id,
                    all_phase_services=all_phase_services,
                    phase_services=phase_services,
                    primary_service_name=str(resolved_signal_service.get("service_name") or ""),
                )
                if not additional_lines:
                    return None
                current_phase_name = self._phase_label(current_phase_id)
                available_names = [
                    str(service.get("name") or "").strip()
                    for service in phase_services
                    if isinstance(service, dict) and str(service.get("name") or "").strip()
                ]
                response_parts = list(additional_lines)
                curated_names = [name for name in available_names if name][:4]
                if curated_names:
                    response_parts.append(f"Right now, I can help you with {', '.join(curated_names)}.")
                response_text = self._dedupe_response_sentences(
                    " ".join(part for part in response_parts if str(part or "").strip()).strip()
                )
                return {
                    "service_id": str(resolved_signal_service.get("service_id") or "").strip(),
                    "service_name": str(resolved_signal_service.get("service_name") or "").strip(),
                    "current_phase_id": current_phase_id,
                    "current_phase_name": current_phase_name,
                    "service_phase_id": current_phase_id,
                    "service_phase_name": current_phase_name,
                    "response_text": response_text,
                    "suggested_actions": list(curated_names[:3]) + ["Show available services", "Ask another question"],
                    "phase_service_unavailable": True,
                }
            if intent == IntentType.TABLE_BOOKING and room_only_booking and phase_supports_room_booking:
                return None
            service_name = str(resolved_signal_service.get("service_name") or "This service").strip()
            current_phase_name = self._phase_label(current_phase_id)
            service_phase_name = self._phase_label(signal_service_phase)
            available_names = [
                str(service.get("name") or "").strip()
                for service in phase_services
                if isinstance(service, dict) and str(service.get("name") or "").strip()
            ]
            return {
                "service_id": str(resolved_signal_service.get("service_id") or "").strip(),
                "service_name": service_name,
                "current_phase_id": current_phase_id,
                "current_phase_name": current_phase_name,
                "service_phase_id": signal_service_phase,
                "service_phase_name": service_phase_name,
                "response_text": self._build_phase_gate_sales_response(
                    service_name=service_name,
                    current_phase_id=current_phase_id,
                    current_phase_name=current_phase_name,
                    service_phase_id=signal_service_phase,
                    service_phase_name=service_phase_name,
                    available_names=available_names,
                ),
                "suggested_actions": available_names[:3] + ["Show available services", "Ask another question"],
                "phase_service_unavailable": True,
            }
        if resolved_signal_service is not None:
            signal_service_phase = self._normalize_phase_identifier(resolved_signal_service.get("phase_id"))
            if signal_service_phase == current_phase_id:
                return None
            if intent == IntentType.TABLE_BOOKING and room_only_booking and phase_supports_room_booking:
                return None
            service_name = str(resolved_signal_service.get("service_name") or "This service").strip()
            current_phase_name = self._phase_label(current_phase_id)
            service_phase_name = self._phase_label(signal_service_phase)
            available_names = [
                str(service.get("name") or "").strip()
                for service in phase_services
                if isinstance(service, dict) and str(service.get("name") or "").strip()
            ]
            result = {
                "service_id": str(resolved_signal_service.get("service_id") or "").strip(),
                "service_name": service_name,
                "current_phase_id": current_phase_id,
                "current_phase_name": current_phase_name,
                "service_phase_id": signal_service_phase,
                "service_phase_name": service_phase_name,
                "response_text": self._build_phase_gate_sales_response(
                    service_name=service_name,
                    current_phase_id=current_phase_id,
                    current_phase_name=current_phase_name,
                    service_phase_id=signal_service_phase,
                    service_phase_name=service_phase_name,
                    available_names=available_names,
                ),
                "suggested_actions": available_names[:3] + ["Show available services", "Ask another question"],
            }
            return result

        if (
            intent == IntentType.TABLE_BOOKING
            and self._looks_like_room_booking_detail_followup(message)
            and self._recent_user_turn_mentions_room_booking(context, message)
            and self._phase_has_room_booking_support(phase_services)
        ):
            return None

        matched_current_phase_service = None
        if not (is_ticketing_like and ticketing_intent_hint is not None):
            matched_current_phase_service = self._match_phase_service_from_rows(
                message=message,
                services=phase_services,
                min_score=0.70,
            )
        if matched_current_phase_service is not None:
            additional_lines = self._phase_gate_additional_unavailable_lines(
                message=message,
                current_phase_id=current_phase_id,
                all_phase_services=all_phase_services,
                phase_services=phase_services,
                primary_service_name=str(matched_current_phase_service.get("service_name") or ""),
            )
            if additional_lines:
                current_phase_name = self._phase_label(current_phase_id)
                available_names = [
                    str(service.get("name") or "").strip()
                    for service in phase_services
                    if isinstance(service, dict) and str(service.get("name") or "").strip()
                ]
                response_parts = list(additional_lines)
                curated_names = [name for name in available_names if name][:4]
                if curated_names:
                    response_parts.append(f"Right now, I can help you with {', '.join(curated_names)}.")
                return {
                    "service_id": str(matched_current_phase_service.get("service_id") or "").strip(),
                    "service_name": str(matched_current_phase_service.get("service_name") or "").strip(),
                    "current_phase_id": current_phase_id,
                    "current_phase_name": current_phase_name,
                    "service_phase_id": current_phase_id,
                    "service_phase_name": current_phase_name,
                    "response_text": self._dedupe_response_sentences(
                        " ".join(part for part in response_parts if str(part or "").strip()).strip()
                    ),
                    "suggested_actions": list(curated_names[:3]) + ["Show available services", "Ask another question"],
                    "phase_service_unavailable": True,
                }
            return None

        matched_any_phase_service = None
        if not (is_ticketing_like and ticketing_intent_hint is not None):
            matched_any_phase_service = self._match_phase_service_from_rows(
                message=message,
                services=all_phase_services,
                min_score=0.70,
            )
        if (
            matched_any_phase_service is not None
            and self._normalize_phase_identifier(matched_any_phase_service.get("phase_id")) != current_phase_id
        ):
            if intent == IntentType.TABLE_BOOKING and room_only_booking and phase_supports_room_booking:
                return None
            service_name = str(matched_any_phase_service.get("service_name") or "This service").strip()
            current_phase_name = self._phase_label(current_phase_id)
            service_phase_id = self._normalize_phase_identifier(matched_any_phase_service.get("phase_id"))
            service_phase_name = self._phase_label(service_phase_id)
            available_names = [
                str(service.get("name") or "").strip()
                for service in phase_services
                if isinstance(service, dict) and str(service.get("name") or "").strip()
            ]
            result = {
                "service_id": str(matched_any_phase_service.get("service_id") or "").strip(),
                "service_name": service_name,
                "current_phase_id": current_phase_id,
                "current_phase_name": current_phase_name,
                "service_phase_id": service_phase_id,
                "service_phase_name": service_phase_name,
                "response_text": self._build_phase_gate_sales_response(
                    service_name=service_name,
                    current_phase_id=current_phase_id,
                    current_phase_name=current_phase_name,
                    service_phase_id=service_phase_id,
                    service_phase_name=service_phase_name,
                    available_names=available_names,
                ),
                "suggested_actions": [
                    str(service.get("name") or "").strip()
                    for service in phase_services
                    if isinstance(service, dict) and str(service.get("name") or "").strip()
                ][:3] + ["Show available services", "Ask another question"],
            }
            additional_lines = self._phase_gate_additional_unavailable_lines(
                message=message,
                current_phase_id=current_phase_id,
                all_phase_services=all_phase_services,
                phase_services=phase_services,
                primary_service_name=service_name,
            )
            if additional_lines:
                result["response_text"] = (
                    f"{str(result.get('response_text') or '').strip()} {' '.join(additional_lines)}"
                ).strip()
            result["response_text"] = self._dedupe_response_sentences(
                str(result.get("response_text") or "").strip()
            )
            return result

        inferred_transactional_intent = ticketing_intent_hint or intent
        requested_label = self._extract_requested_service_label_for_phase_gate(message)
        if not is_transactional:
            inferred = ticketing_intent_hint or self._infer_phase_gate_transactional_intent(message)
            if inferred is None:
                # For FAQ/MENU action-like text, if no known transactional service intent can
                # be inferred, let normal capability/validator flows handle unsupported requests.
                return None
            inferred_transactional_intent = inferred
            if (
                inferred_transactional_intent == IntentType.TABLE_BOOKING
                and room_only_booking
                and phase_supports_room_booking
            ):
                return None
            if not requested_label and self._phase_has_intent_compatible_service(
                intent=inferred_transactional_intent,
                phase_services=phase_services,
                strict=True,
            ):
                strict_match = self._match_phase_service_from_rows(
                    message=message,
                    services=phase_services,
                    min_score=0.72,
                )
                if strict_match is not None:
                    return None

        if requested_label and self._phase_services_include_requested_label(
            requested_label=requested_label,
            phase_services=phase_services,
        ):
            return None
        if is_transactional:
            if (
                intent == IntentType.TABLE_BOOKING
                and room_only_booking
                and phase_supports_room_booking
            ):
                return None
            has_compatible_service = self._phase_has_intent_compatible_service(
                intent=intent,
                phase_services=phase_services,
            )
            if (
                has_compatible_service
                and not requested_label
                and intent in {IntentType.ORDER_FOOD, IntentType.ROOM_SERVICE}
            ):
                return None

        current_phase_name = self._phase_label(current_phase_id)
        service_label = requested_label or self._phase_gate_intent_label(inferred_transactional_intent)
        available_names = [
            str(service.get("name") or "").strip()
            for service in phase_services
            if isinstance(service, dict) and str(service.get("name") or "").strip()
        ]
        available_names = [name for name in available_names if name][:4]
        fallback_service = self._find_intent_compatible_service(
            intent=inferred_transactional_intent,
            services=all_phase_services,
            exclude_phase_id=current_phase_id,
        )
        fallback_phase_id = self._normalize_phase_identifier(fallback_service.get("phase_id")) if isinstance(fallback_service, dict) else ""
        fallback_phase_name = self._phase_label(fallback_phase_id) if fallback_phase_id else ""
        response_text = self._build_phase_gate_sales_response(
            service_name=service_label,
            current_phase_id=current_phase_id,
            current_phase_name=current_phase_name,
            service_phase_id=fallback_phase_id,
            service_phase_name=fallback_phase_name,
            available_names=available_names,
        )
        additional_lines = self._phase_gate_additional_unavailable_lines(
            message=message,
            current_phase_id=current_phase_id,
            all_phase_services=all_phase_services,
            phase_services=phase_services,
            primary_service_name=service_label,
        )
        if additional_lines:
            response_text = f"{response_text} {' '.join(additional_lines)}".strip()
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        suggestions = list(available_names[:3])
        suggestions.extend(["Show available services", "Ask another question"])
        return {
            "service_id": "",
            "service_name": service_label,
            "current_phase_id": current_phase_id,
            "current_phase_name": current_phase_name,
            "service_phase_id": "",
            "service_phase_name": "",
            "response_text": response_text,
            "suggested_actions": suggestions,
            "phase_service_unavailable": True,
        }

    @staticmethod
    def _is_phase_service_ticketing_enabled(service: dict[str, Any]) -> bool:
        if not isinstance(service, dict):
            return True
        if "ticketing_enabled" not in service:
            return True
        value = service.get("ticketing_enabled")
        if value is None:
            return True
        return bool(value)

    def _detect_ticketing_phase_service_ticketing_disabled(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            services = []

        matched_service = self._resolve_service_from_phase_signals(
            services=services,
            entities=entities if isinstance(entities, dict) else {},
            pending_data=pending_data if isinstance(pending_data, dict) else {},
        )
        llm_only_phase_gate = bool(getattr(settings, "chat_phase_gate_llm_only", True))
        if matched_service is None:
            if llm_only_phase_gate:
                matched_service = self._match_phase_service_from_rows(
                    message=message,
                    services=services,
                    min_score=0.72,
                )
            else:
                matched_service = self._match_phase_managed_service_request(message)
        if matched_service is None:
            return None

        current_phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        service_phase_id = self._normalize_phase_identifier(matched_service.get("phase_id"))
        if not current_phase_id or not service_phase_id:
            return None
        if current_phase_id != service_phase_id:
            return None
        if self._is_phase_service_ticketing_enabled(matched_service):
            return None

        service_name = str(matched_service.get("service_name") or "This service").strip()
        current_phase_name = self._phase_label(current_phase_id)
        response_text = (
            f"Ticketing is not enabled for {service_name} in {current_phase_name} phase."
        )
        return {
            "service_id": str(matched_service.get("service_id") or "").strip(),
            "service_name": service_name,
            "current_phase_id": current_phase_id,
            "current_phase_name": current_phase_name,
            "service_phase_id": service_phase_id,
            "service_phase_name": self._phase_label(service_phase_id),
            "response_text": response_text,
        }

    def _phase_services_for_id(
        self,
        *,
        capabilities_summary: dict[str, Any],
        phase_id: str,
        limit: int = 6,
    ) -> list[str]:
        normalized_phase = self._normalize_phase_identifier(phase_id)
        if not normalized_phase:
            return []
        services = capabilities_summary.get("service_catalog", [])
        if not isinstance(services, list):
            return []
        names: list[str] = []
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if service_phase_id != normalized_phase:
                continue
            service_name = str(service.get("name") or service.get("id") or "").strip()
            if not service_name:
                continue
            if service_name in names:
                continue
            names.append(service_name)
            if len(names) >= max(1, int(limit or 6)):
                break
        return names

    async def _compose_policy_guardrail_response(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        intent: IntentType,
        gate_payload: dict[str, Any] | None = None,
        fallback_text: str = "",
        response_source: str = "policy_guardrail",
    ) -> str:
        gate = gate_payload if isinstance(gate_payload, dict) else {}
        fallback = self._dedupe_response_sentences(fallback_text) or str(fallback_text or "").strip()
        if not fallback:
            fallback = "I can help with services available in your current phase."

        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        current_phase_id = self._normalize_phase_identifier(
            gate.get("current_phase_id") or selected_phase.get("selected_phase_id")
        )
        current_phase_name = str(gate.get("current_phase_name") or selected_phase.get("selected_phase_name") or "").strip()
        if not current_phase_name and current_phase_id:
            current_phase_name = self._phase_label(current_phase_id)

        service_phase_id = self._normalize_phase_identifier(gate.get("service_phase_id"))
        service_phase_name = str(gate.get("service_phase_name") or "").strip()
        if service_phase_id and not service_phase_name:
            service_phase_name = self._phase_label(service_phase_id)
        service_name = str(gate.get("service_name") or gate.get("requested_service") or "This service").strip()
        timing_hint = self._phase_transition_timing_hint(
            current_phase_id=current_phase_id,
            service_phase_id=service_phase_id,
        )

        available_now = self._phase_services_for_id(
            capabilities_summary=capabilities_summary,
            phase_id=current_phase_id,
            limit=6,
        )
        raw_actions = gate.get("suggested_actions")
        suggested_actions: list[str] = []
        if isinstance(raw_actions, list):
            for action in raw_actions:
                label = str(action or "").strip()
                if not label:
                    continue
                if label.lower() in {"show available services", "ask another question"}:
                    continue
                if label not in suggested_actions:
                    suggested_actions.append(label)
                if len(suggested_actions) >= 4:
                    break

        service_catalog_context: list[dict[str, Any]] = []
        catalog_rows = capabilities_summary.get("service_catalog", [])
        if isinstance(catalog_rows, list):
            for service in catalog_rows:
                if not isinstance(service, dict):
                    continue
                if not bool(service.get("is_active", True)):
                    continue
                service_id = str(service.get("id") or "").strip()
                service_name = str(service.get("name") or "").strip()
                if not service_id and not service_name:
                    continue
                phase_row_id = self._normalize_phase_identifier(service.get("phase_id"))
                phase_row_name = self._phase_label(phase_row_id) if phase_row_id else ""
                prompt_pack = service.get("service_prompt_pack")
                if not isinstance(prompt_pack, dict):
                    prompt_pack = {}
                knowledge_hint = str(prompt_pack.get("extracted_knowledge") or "").strip()
                if not knowledge_hint:
                    knowledge_hint = str(service.get("description") or service.get("cuisine") or "").strip()
                service_catalog_context.append(
                    {
                        "id": service_id,
                        "name": service_name or service_id,
                        "phase_id": phase_row_id,
                        "phase_name": phase_row_name,
                        "description": str(service.get("description") or "").strip(),
                        "ticketing_enabled": bool(service.get("ticketing_enabled", True)),
                        "knowledge_hint": knowledge_hint[:500],
                    }
                )
                if len(service_catalog_context) >= 80:
                    break

        prompt_payload = {
            "intent": intent.value if isinstance(intent, IntentType) else str(intent),
            "user_message": str(user_message or "").strip(),
            "policy_reason_code": str(
                gate.get("policy_blocked_reason")
                or gate.get("ticket_skip_reason")
                or gate.get("code")
                or ""
            ).strip(),
            "current_phase": {
                "id": current_phase_id,
                "name": current_phase_name,
            },
            "requested_service": {
                "id": str(gate.get("service_id") or "").strip(),
                "name": service_name,
                "target_phase_id": service_phase_id,
                "target_phase_name": service_phase_name,
                "timing_hint": timing_hint,
            },
            "phase_service_unavailable": bool(gate.get("phase_service_unavailable")),
            "available_services_now": available_now,
            "suggested_services_now": suggested_actions,
            "service_catalog_context": service_catalog_context,
        }

        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return fallback

        system_prompt = (
            "You are a hotel concierge assistant composing a policy-guarded reply.\n"
            "Return plain text only.\n"
            "Use only the supplied facts.\n"
            "Do not promise immediate booking/order/ticket actions when blocked.\n"
            "If service is out of phase, mention current phase and when it becomes available.\n"
            "If requested_service.id is empty, infer likely service from user_message using service_catalog_context.\n"
            "When a likely match exists in another phase, include brief details from that service's description/knowledge_hint.\n"
            "For informational topics where execution is blocked, provide info-only response and clearly avoid transactional promises.\n"
            "Keep response concise, natural, and guest-friendly (2-4 short sentences).\n"
        )
        user_prompt = (
            "Generate the final user response from this JSON policy payload:\n"
            f"{json.dumps(prompt_payload, ensure_ascii=False)}"
        )
        model = str(getattr(settings, "chat_llm_response_surface_model", "") or "").strip() or None
        base_temperature = float(getattr(settings, "chat_llm_response_surface_temperature", 0.35) or 0.35)
        temperature = max(0.2, min(0.8, base_temperature + 0.1))
        max_tokens = max(120, int(getattr(settings, "chat_llm_response_surface_max_tokens", 420) or 420))

        try:
            rendered = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=temperature,
                max_tokens=min(max_tokens, 260),
            )
        except Exception:
            return fallback

        candidate = self._dedupe_response_sentences(rendered) or str(rendered or "").strip()
        if not candidate:
            return fallback

        validation_intent = intent if isinstance(intent, IntentType) else IntentType.FAQ
        validation = response_validator.validate(
            response_text=candidate,
            intent_result=IntentResult(intent=validation_intent, confidence=0.82, entities={}),
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=True,
            capability_reason="",
        )
        if validation.valid:
            return candidate

        if validation.action == "replace" and validation.replacement_response:
            safe = self._dedupe_response_sentences(validation.replacement_response) or str(
                validation.replacement_response or ""
            ).strip()
            if safe and bool(getattr(settings, "chat_no_template_response_mode", False)):
                rewritten_safe, _ = await self._maybe_llm_rewrite_response(
                    response_text=safe,
                    user_message=user_message,
                    intent_result=IntentResult(intent=validation_intent, confidence=0.82, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source=response_source,
                    validator_replaced=True,
                )
                safe = self._dedupe_response_sentences(rewritten_safe) or str(rewritten_safe or "").strip() or safe
            return safe or fallback

        return fallback

    async def _build_ticketing_phase_gate_handler_result(
        self,
        phase_gate: dict[str, Any],
        *,
        user_message: str,
        intent: IntentType,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
    ) -> HandlerResult:
        response_text = await self._compose_policy_guardrail_response(
            user_message=user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            intent=intent,
            gate_payload=phase_gate,
            fallback_text="",
            response_source="ticketing_phase_gate",
        )
        if not response_text:
            response_text = "I can help with services available in your current phase."
        response_text = ChatService._dedupe_response_sentences(response_text) or response_text
        raw_actions = phase_gate.get("suggested_actions")
        suggestions: list[str] = []
        if isinstance(raw_actions, list):
            suggestions = [str(item).strip() for item in raw_actions if str(item).strip()]
        if not suggestions:
            suggestions = ["Show available services", "Ask another question"]
        return HandlerResult(
            response_text=response_text,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=suggestions[:4],
            metadata={
                "response_source": "ticketing_phase_gate",
                "ticketing_phase_gate": True,
                "phase_gate_service_id": str(phase_gate.get("service_id") or ""),
                "phase_gate_service_name": str(phase_gate.get("service_name") or ""),
                "phase_gate_current_phase_id": str(phase_gate.get("current_phase_id") or ""),
                "phase_gate_current_phase_name": str(phase_gate.get("current_phase_name") or ""),
                "phase_gate_service_phase_id": str(phase_gate.get("service_phase_id") or ""),
                "phase_gate_service_phase_name": str(phase_gate.get("service_phase_name") or ""),
                "phase_service_unavailable": bool(phase_gate.get("phase_service_unavailable")),
            },
        )

    @staticmethod
    def _build_contextual_sanity_handler_result(sanity_issue: dict[str, Any]) -> HandlerResult:
        response_text = str(sanity_issue.get("response_text") or "").strip()
        if not response_text:
            response_text = "I need one clarification before I can proceed."
        response_text = ChatService._dedupe_response_sentences(response_text) or response_text
        raw_actions = sanity_issue.get("suggested_actions")
        suggestions: list[str] = []
        if isinstance(raw_actions, list):
            suggestions = [str(item).strip() for item in raw_actions if str(item).strip()]
        if not suggestions:
            suggestions = ["Show available services", "Ask another question"]
        return HandlerResult(
            response_text=response_text,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=suggestions[:4],
            metadata={
                "response_source": "contextual_sanity_gate",
                "contextual_sanity_gate": True,
                "contextual_sanity_code": str(sanity_issue.get("code") or ""),
            },
        )

    @staticmethod
    def _service_description_keywords(service: dict) -> list[str]:
        description = str(service.get("description") or service.get("cuisine") or "").strip().lower()
        if not description:
            return []
        tokens = re.findall(r"[a-z0-9]+", description)
        keywords: list[str] = []
        for token in tokens:
            if len(token) < 4:
                continue
            if token in _SERVICE_DESCRIPTION_STOPWORDS:
                continue
            if token not in keywords:
                keywords.append(token)
        return keywords[:8]

    @staticmethod
    def _token_matches_message(token: str, msg_tokens: list[str]) -> bool:
        if not token:
            return False
        for msg_token in msg_tokens:
            if msg_token == token:
                return True
            if (
                len(token) >= 6
                and len(msg_token) >= 6
                and (msg_token.startswith(token[:4]) or token.startswith(msg_token[:4]))
            ):
                return True
            if len(token) >= 5 and len(msg_token) >= 4 and SequenceMatcher(a=token, b=msg_token).ratio() >= 0.86:
                return True
        return False

    def _service_alias_match_score(self, msg_lower: str, msg_tokens: list[str], alias: str) -> float:
        alias_clean = str(alias or "").strip().lower()
        if not alias_clean:
            return 0.0

        alias_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", alias_clean)
            if len(token) >= 3 and token not in _SERVICE_DESCRIPTION_STOPWORDS
        ]
        if not alias_tokens:
            return 0.0

        if len(alias_tokens) == 1:
            if re.search(rf"\b{re.escape(alias_tokens[0])}\b", msg_lower):
                return 0.95
        else:
            alias_phrase = " ".join(alias_tokens)
            if alias_phrase and re.search(rf"\b{re.escape(alias_phrase)}\b", msg_lower):
                return 0.98

        hits = sum(1 for token in alias_tokens if self._token_matches_message(token, msg_tokens))
        if hits == len(alias_tokens):
            return 0.9
        if len(alias_tokens) >= 3 and hits >= len(alias_tokens) - 1:
            return 0.8
        if len(alias_tokens) == 1 and hits == 1:
            return 0.84
        return 0.0

    @staticmethod
    def _service_action_should_route_to_transaction(service: dict, msg_lower: str) -> bool:
        """
        Keep only clearly-known transactional flows on dedicated handlers.
        """
        service_type = str(service.get("type") or "").strip().lower()
        service_id = str(service.get("id") or "").strip().lower()
        service_name = str(service.get("name") or "").strip().lower()
        combined = f"{service_type} {service_id} {service_name}"

        action_like = any(token in msg_lower for token in ("book", "reserve", "schedule", "order", "confirm", "arrange"))
        if not action_like:
            return False

        transport_markers = ("transport", "airport_transfer", "cab", "taxi", "shuttle")
        appointment_markers = ("appointment", "spa", "massage", "doctor")
        room_service_markers = ("room_service", "housekeeping", "laundry", "cleaning")
        restaurant_markers = ("restaurant", "outlet", "dining", "ird", "food")
        booking_markers = ("book", "reserve", "table", "guests", "party")

        if any(marker in combined for marker in transport_markers):
            return True
        if any(marker in combined for marker in appointment_markers):
            return True
        if any(marker in combined for marker in room_service_markers):
            return True
        if any(marker in combined for marker in restaurant_markers) and any(marker in msg_lower for marker in booking_markers):
            return True
        return False

    def _match_memory_information_response(
        self,
        message: str,
        context: ConversationContext,
        memory_snapshot: dict,
    ) -> Optional[dict]:
        """
        Deterministic personal-memory lookup for "my booking/order/departure" questions.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        facts = memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {}
        if not isinstance(facts, dict) or not facts:
            return None

        asks_personal_booking = (
            "my" in msg_lower
            and any(token in msg_lower for token in ("reservation", "booking", "table"))
        ) or any(
            marker in msg_lower
            for marker in (
                "reservation time",
                "booking time",
                "what time is my reservation",
                "what time is my booking",
            )
        )
        if asks_personal_booking:
            latest_booking = facts.get("latest_booking", {})
            if isinstance(latest_booking, dict) and latest_booking:
                restaurant = str(latest_booking.get("restaurant") or "the restaurant").strip()
                booking_time = str(latest_booking.get("time") or "").strip()
                booking_date = str(latest_booking.get("date") or "").strip()
                reference = str(latest_booking.get("reference") or "").strip()

                details = [f"Your latest reservation is for {restaurant}"]
                if booking_time:
                    details.append(f"at {booking_time}")
                if booking_date and booking_date.lower() not in {"today", ""}:
                    details.append(f"on {booking_date}")
                details_text = " ".join(details).strip() + "."
                if reference:
                    details_text += f" Reference: {reference}."

                return {
                    "match_type": "memory_booking_lookup",
                    "response_text": details_text,
                }
            return {
                "match_type": "memory_booking_lookup_missing",
                "response_text": "I don't see a confirmed reservation in this chat yet. Would you like me to book one now?",
            }

        asks_departure = (
            any(marker in msg_lower for marker in ("what time am i leaving", "when am i leaving", "my departure"))
            or ("my checkout" in msg_lower)
            or (("leave" in msg_lower or "leaving" in msg_lower or "checkout" in msg_lower) and "my" in msg_lower)
        )
        if asks_departure:
            departure_time = str(facts.get("departure_time") or "").strip()
            departure_day = str(facts.get("departure_day") or "").strip()
            if departure_time:
                day_text = f" {departure_day}" if departure_day else ""
                return {
                    "match_type": "memory_departure_lookup",
                    "response_text": f"You told me your departure is at {departure_time}{day_text}.",
                }

        asks_order = (
            "my order" in msg_lower
            and any(token in msg_lower for token in ("id", "status", "details", "what", "which"))
        ) or ("order id" in msg_lower)
        if asks_order:
            latest_order = facts.get("latest_order", {})
            if isinstance(latest_order, dict) and latest_order:
                order_id = str(latest_order.get("id") or "").strip()
                items = latest_order.get("items") if isinstance(latest_order.get("items"), list) else []
                total = latest_order.get("total")

                parts = []
                if order_id:
                    parts.append(f"Your latest order ID is {order_id}.")
                if items:
                    parts.append(f"Items: {', '.join(str(i) for i in items[:5])}.")
                if total is not None and str(total) != "":
                    try:
                        parts.append(f"Total: Rs.{float(total):.0f}.")
                    except (TypeError, ValueError):
                        pass

                if parts:
                    return {
                        "match_type": "memory_order_lookup",
                        "response_text": " ".join(parts),
                    }
                return {
                    "match_type": "memory_order_lookup_missing",
                    "response_text": "I can see your recent order, but I don't have full details. I can connect you with staff for exact status.",
                }

        return None

    @staticmethod
    def _is_return_to_bot_request(message: str) -> bool:
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False
        if msg_lower in _RETURN_TO_BOT_MARKERS:
            return True
        return msg_lower.startswith("return to bot")

    @staticmethod
    def _is_personal_reservation_query(msg_lower: str) -> bool:
        if "my" in msg_lower and ("reservation" in msg_lower or "booking" in msg_lower):
            return True
        return any(
            marker in msg_lower
            for marker in (
                "what time am i leaving",
                "when am i leaving",
                "my checkout",
                "my check out",
                "my departure",
            )
        )

    @staticmethod
    def _intent_ids_for_enablement(intent: IntentType) -> list[str]:
        """
        Intent aliases used across industry templates.
        Allows core intents to map onto industry-specific naming.
        """
        mapping = {
            IntentType.GREETING: ["greeting"],
            IntentType.MENU_REQUEST: ["menu_request", "faq", "product_search", "department_info", "doctor_info"],
            IntentType.ORDER_FOOD: ["order_food", "order_placement", "add_to_cart", "checkout"],
            IntentType.ORDER_STATUS: ["order_status", "refund_status", "report_request"],
            IntentType.TABLE_BOOKING: ["table_booking", "book_appointment", "appointment_booking"],
            IntentType.ROOM_SERVICE: ["room_service", "housekeeping", "prescription"],
            IntentType.COMPLAINT: ["complaint", "feedback", "support"],
            IntentType.FAQ: ["faq"],
            IntentType.HUMAN_REQUEST: ["human_request"],
        }
        return mapping.get(intent, [intent.value])

    @staticmethod
    def _detect_confirmation_intent(msg_lower: str) -> Optional[IntentType]:
        strict_mode = bool(getattr(settings, "chat_require_strict_confirmation_phrase", True))
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm")).strip().lower()
        compact_msg = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        yes_tokens = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "proceed", "y", "ys", "ye", "yess"}
        no_tokens = {"no", "nope", "cancel", "n", "stop", "dont", "don't", "nah"}
        # Always treat the configured phrase as a valid confirmation,
        # even when strict mode is disabled.
        if compact_msg == confirmation_phrase:
            return IntentType.CONFIRMATION_YES
        if strict_mode:
            if compact_msg in no_tokens:
                return IntentType.CONFIRMATION_NO
            return None

        if compact_msg in yes_tokens:
            return IntentType.CONFIRMATION_YES
        if compact_msg in no_tokens:
            return IntentType.CONFIRMATION_NO

        compact = re.sub(r"[^a-z]", "", compact_msg)
        if not compact:
            return None

        for token in yes_tokens:
            if SequenceMatcher(a=compact, b=token).ratio() >= 0.84:
                return IntentType.CONFIRMATION_YES
        for token in no_tokens:
            if SequenceMatcher(a=compact, b=token).ratio() >= 0.84:
                return IntentType.CONFIRMATION_NO
        return None

    @staticmethod
    def _is_reasonable_message_preprocess_rewrite(original: str, rewritten: str) -> bool:
        base = re.sub(r"\s+", " ", str(original or "").strip())
        candidate = re.sub(r"\s+", " ", str(rewritten or "").strip())
        if not base or not candidate:
            return False
        if candidate == base:
            return True
        if len(candidate) > max(2600, len(base) * 3):
            return False

        base_tokens = set(re.findall(r"[a-z0-9]+", base.lower()))
        candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate.lower()))
        if not base_tokens or not candidate_tokens:
            return False

        if len(base_tokens) == 1 and len(candidate_tokens) == 1:
            base_token = next(iter(base_tokens))
            candidate_token = next(iter(candidate_tokens))
            return SequenceMatcher(a=base_token, b=candidate_token).ratio() >= 0.45

        overlap = len(base_tokens & candidate_tokens) / max(1, len(base_tokens))
        return overlap >= 0.35

    async def _preprocess_user_message_with_llm(
        self,
        message: str,
        *,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> str:
        """
        LLM-first user-message preprocessing layer.
        Correct typos/spelling and minor grammar while preserving intent.
        """
        original = re.sub(r"\s+", " ", str(message or "").strip())
        if not original:
            return ""
        if not bool(getattr(settings, "chat_llm_preprocess_enabled", True)):
            return original
        if not str(settings.openai_api_key or "").strip():
            return original

        phase_label = str(selected_phase_name or "").strip() or (
            str(selected_phase_id or "").replace("_", " ").title()
        )
        phase_id = str(selected_phase_id or "").strip() or "unknown"

        prompt = (
            "You normalize chat user input before intent routing.\n"
            f"Selected user journey phase: {phase_label} ({phase_id}).\n"
            "Task: fix spelling/typos and minor grammar only.\n"
            "Do not add/remove intent, entities, requests, dates, times, quantities, room numbers, names, or polarity.\n"
            "Keep language and tone as-is.\n"
            "Return exactly one rewritten user message line and nothing else."
        )
        preprocess_model = str(getattr(settings, "chat_llm_preprocess_model", "") or "").strip() or None
        preprocess_temp = float(getattr(settings, "chat_llm_preprocess_temperature", 0.0))
        preprocess_tokens = max(24, int(getattr(settings, "chat_llm_preprocess_max_tokens", 80)))

        try:
            rewritten = await llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": original},
                ],
                model=preprocess_model,
                temperature=preprocess_temp,
                max_tokens=preprocess_tokens,
            )
        except Exception:
            return original

        candidate = re.sub(r"\s+", " ", str(rewritten or "").strip().strip('"').strip("'"))
        if not candidate:
            return original
        if self._is_reasonable_message_preprocess_rewrite(original, candidate):
            return candidate
        return original

    @staticmethod
    def _normalize_message_for_routing(message: str) -> str:
        return re.sub(r"\s+", " ", str(message or "").strip().lower())

    @staticmethod
    def _normalize_alias_text(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()

    def _match_restaurant_alias_menu_phrase(self, msg_lower: str, context: dict) -> Optional[str]:
        """
        Detect bare restaurant/IRD alias phrases and treat them as menu intent.
        Example: "in room dining", "ird", "ird menu".
        """
        if not isinstance(context, dict):
            return None
        normalized_msg = self._normalize_alias_text(msg_lower)
        if not normalized_msg:
            return None

        transactional_markers = ("book ", "reserve ", "order ", "deliver ", "send ", "confirm ", "cancel ")
        if any(marker in f"{normalized_msg} " for marker in transactional_markers):
            return None

        service_catalog = context.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return None

        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            service_type = str(service.get("type") or "").strip().lower()
            if service_type and not any(token in service_type for token in ("restaurant", "dining", "food", "outlet")):
                continue

            aliases = []
            service_name = str(service.get("name") or "").strip()
            service_id = str(service.get("id") or "").strip()
            if service_name:
                aliases.append(service_name)
            if service_id:
                aliases.append(service_id)

            for alias in aliases:
                alias_norm = self._normalize_alias_text(alias)
                if not alias_norm:
                    continue
                if normalized_msg in {alias_norm, f"{alias_norm} menu", f"menu {alias_norm}"}:
                    return service_name or alias

        return None

    @staticmethod
    def _has_hunger_signal(msg_lower: str) -> bool:
        """Detect hunger expressions with typo tolerance (e.g., 'hungfry')."""
        if not msg_lower:
            return False

        tokens = re.findall(r"[a-z]+", msg_lower.lower())
        if not tokens:
            return False

        explicit_markers = {"hungry", "starving", "famished"}
        for token in tokens:
            if token in explicit_markers:
                return True
            if len(token) >= 5 and SequenceMatcher(a=token, b="hungry").ratio() >= 0.72:
                return True
        return False

    @staticmethod
    def _looks_like_food_catalog_info_query(msg_lower: str) -> bool:
        """
        Detect natural-language food/menu information asks so they route to RAG FAQ
        instead of transactional handlers.
        """
        if not msg_lower:
            return False

        catalog_markers = (
            "menu",
            "menus",
            "catalog",
            "food",
            "dining",
            "ird",
            "in room dining",
            "in-room dining",
            "hungry",
            "eat",
        )
        info_markers = (
            "what",
            "which",
            "show",
            "list",
            "tell",
            "do you",
            "can you",
            "is there",
            "are there",
            "serve",
            "available",
            "options",
        )
        direct_order_markers = (
            "i want ",
            "order ",
            "book ",
            "confirm ",
            "place order",
            "send ",
        )

        has_hunger_signal = ChatService._has_hunger_signal(msg_lower)
        has_catalog_signal = any(marker in msg_lower for marker in catalog_markers) or has_hunger_signal
        has_info_signal = any(marker in msg_lower for marker in info_markers)
        if not has_catalog_signal:
            return False

        if has_info_signal:
            return True

        if has_hunger_signal and not any(marker in msg_lower for marker in direct_order_markers):
            return True

        return False

    @staticmethod
    def _looks_like_food_delivery_order_request(msg_lower: str) -> bool:
        """
        Deterministically catch explicit food-order delivery asks so they don't
        get misrouted to room-service just because a room is mentioned.
        """
        if not msg_lower:
            return False

        order_markers = (
            "order",
            "deliver",
            "delivery",
            "send",
            "to my room",
            "room delivery",
        )
        food_markers = (
            "food",
            "menu",
            "pizza",
            "pasta",
            "biryani",
            "burger",
            "snack",
            "drink",
            "wine",
            "dessert",
            "soup",
            "meal",
            "chicken",
            "veg",
            "nonveg",
            "non-veg",
        )

        has_order_signal = any(marker in msg_lower for marker in order_markers)
        has_hunger_signal = ChatService._has_hunger_signal(msg_lower)
        if not has_order_signal and has_hunger_signal and "room" in msg_lower:
            has_order_signal = True

        has_food_signal = any(marker in msg_lower for marker in food_markers) or has_hunger_signal
        if not (has_order_signal and has_food_signal):
            return False

        # Explicit room-service operational asks should remain room_service.
        operational_markers = ("cleaning", "towel", "laundry", "amenities", "maintenance", "housekeeping")
        if any(marker in msg_lower for marker in operational_markers):
            return False
        return True

    @staticmethod
    def _looks_like_strong_menu_request(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if any(token in msg_lower for token in ("menu", "catalog", "items list", "offerings")):
            return True
        compact_tokens = [re.sub(r"[^a-z]", "", token) for token in msg_lower.split()]
        compact_tokens = [token for token in compact_tokens if token]
        for token in compact_tokens:
            if SequenceMatcher(a=token, b="menu").ratio() >= 0.8:
                return True
            if SequenceMatcher(a=token, b="catalog").ratio() >= 0.82:
                return True
        return False

    @staticmethod
    def _looks_like_policy_or_timing_query(msg_lower: str) -> bool:
        """
        Detect policy/timing information asks and route to FAQ/RAG deterministically.
        """
        if not msg_lower:
            return False

        normalized = re.sub(r"\s+", " ", msg_lower.strip().lower())
        if not normalized:
            return False

        transactional_prefixes = (
            "book ",
            "reserve ",
            "order ",
            "arrange ",
            "send ",
            "cancel ",
            "confirm ",
            "i want ",
            "i need ",
            "please book",
            "please reserve",
            "please order",
        )
        if any(normalized.startswith(prefix) for prefix in transactional_prefixes):
            return False

        transactional_markers = (
            "talk to human",
            "connect me",
            "human agent",
            "manager",
            "my order",
            "my booking",
            "my reservation",
            "order status",
            "track my",
        )
        if any(marker in normalized for marker in transactional_markers):
            return False

        is_question_like = (
            "?" in normalized
            or normalized.startswith("what ")
            or normalized.startswith("when ")
            or normalized.startswith("where ")
            or normalized.startswith("which ")
            or normalized.startswith("who ")
            or normalized.startswith("how ")
            or normalized.startswith("is ")
            or normalized.startswith("are ")
            or normalized.startswith("do ")
            or normalized.startswith("can ")
            or normalized.startswith("what's ")
        )
        if not is_question_like:
            return False

        policy_markers = (
            "check in",
            "check-in",
            "checkin",
            "check out",
            "check-out",
            "checkout",
            "timing",
            "timings",
            "hours",
            "policy",
            "arrival",
            "departure",
            "late checkout",
            "early checkin",
        )
        return any(marker in normalized for marker in policy_markers)

    @staticmethod
    def _looks_like_ticketing_request(msg_lower: str) -> bool:
        """Detect ticket status/update/create asks and route to complaint workflow."""
        if not msg_lower:
            return False

        status_markers = (
            "ticket status",
            "status of ticket",
            "check ticket",
            "my ticket",
            "complaint status",
        )
        update_markers = (
            "update ticket",
            "ticket update",
            "add note",
            "append note",
            "update my complaint",
            "follow up ticket",
            "ticket follow up",
        )
        create_markers = (
            "raise complaint",
            "file complaint",
            "create ticket",
            "raise ticket",
            "log complaint",
        )
        return any(marker in msg_lower for marker in (status_markers + update_markers + create_markers))

    def _build_capability_denial_response(self, capability_check: CapabilityCheck) -> str:
        """Build a helpful response when capability is not available."""
        response = capability_check.reason

        if capability_check.alternatives:
            alternatives_text = ", ".join(capability_check.alternatives)
            response += f"\n\nAlternatively, I can help you with: {alternatives_text}"

        return response

    async def _apply_full_kb_response_validation(
        self,
        *,
        response_text: str,
        effective_intent: IntentType,
        effective_confidence: float,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        request_message: str,
    ) -> tuple[str, bool, list[str], bool]:
        """
        Run the same runtime validator used by classic flow for full-KB responses.
        Returns: (final_response_text, replaced, issue_codes, capability_allowed)
        """
        validation_intent = IntentResult(
            intent=effective_intent,
            confidence=max(0.0, min(1.0, float(effective_confidence or 0.0))),
            entities={},
        )
        intent_enabled_check = self._check_intent_enabled(effective_intent)
        capability_check = (
            intent_enabled_check
            if not intent_enabled_check.allowed
            else self._check_capability_for_intent(
                context.hotel_code,
                validation_intent,
                request_message,
            )
        )
        validation = response_validator.validate(
            response_text=response_text,
            intent_result=validation_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
        )
        issue_codes = [str(issue.code) for issue in (validation.issues or []) if getattr(issue, "code", None)]
        validator_replaced = False
        final_response_text = str(response_text or "").strip()
        if not validation.valid and validation.action == "replace" and validation.replacement_response:
            final_response_text = str(validation.replacement_response or "").strip()
            validator_replaced = True
        final_response_text, rewrite_meta = await self._maybe_llm_rewrite_response(
            response_text=final_response_text,
            user_message=request_message,
            intent_result=validation_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
            response_source="full_kb_llm",
            validator_replaced=validator_replaced,
        )
        if bool(rewrite_meta.get("llm_surface_rewrite_validator_replaced")):
            validator_replaced = True
        rewrite_issue_codes = [
            str(code).strip()
            for code in (rewrite_meta.get("llm_surface_rewrite_validation_issues") or [])
            if str(code).strip()
        ]
        combined_issue_codes: list[str] = []
        for code in [*issue_codes, *rewrite_issue_codes]:
            if code and code not in combined_issue_codes:
                combined_issue_codes.append(code)
        return final_response_text, validator_replaced, combined_issue_codes, capability_check.allowed

    async def _classify_intent(
        self,
        message: str,
        conversation_history: list[dict],
        context: dict,
    ) -> IntentResult:
        """Classify user intent using LLM."""
        try:
            classification_message = re.sub(r"\s+", " ", str(message or "").strip())
            msg_lower = classification_message.lower()

            # Check for simple confirmation first (state-aware)
            if context.get("state") == "awaiting_confirmation":
                if context.get("pending_action") == "show_menu" and msg_lower in {"s", "show", "menu", "show menu", "full menu"}:
                    return IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.9)
                confirmation_intent = self._detect_confirmation_intent(msg_lower)
                if confirmation_intent == IntentType.CONFIRMATION_YES:
                    return IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95)
                if confirmation_intent == IntentType.CONFIRMATION_NO:
                    return IntentResult(intent=IntentType.CONFIRMATION_NO, confidence=0.95)

            restaurant_alias = self._match_restaurant_alias_menu_phrase(msg_lower, context)
            if restaurant_alias:
                return IntentResult(
                    intent=IntentType.MENU_REQUEST,
                    confidence=0.9,
                    entities={"restaurant": restaurant_alias},
                )

            if self._looks_like_spa_booking_action(msg_lower):
                return IntentResult(
                    intent=IntentType.TABLE_BOOKING,
                    confidence=0.92,
                    entities={
                        "service_name": "Spa",
                        "booking_sub_category": "spa_booking",
                        "booking_type": "spa_booking",
                    },
                )

            if self._looks_like_room_stay_booking_request(classification_message):
                return IntentResult(
                    intent=IntentType.TABLE_BOOKING,
                    confidence=0.92,
                    entities={
                        "service_name": "Room Booking",
                        "booking_sub_category": "room_booking",
                        "booking_type": "room_booking",
                    },
                )

            if self._looks_like_food_delivery_order_request(msg_lower):
                return IntentResult(
                    intent=IntentType.ORDER_FOOD,
                    confidence=0.92,
                    entities={},
                )

            if self._looks_like_food_catalog_info_query(msg_lower):
                return IntentResult(
                    intent=IntentType.FAQ,
                    confidence=0.9,
                    entities={"requested_topic": "food_catalog"},
                )

            if self._looks_like_ticketing_request(msg_lower):
                return IntentResult(
                    intent=IntentType.COMPLAINT,
                    confidence=0.9,
                    entities={"ticketing_flow": True},
                )

            if self._looks_like_strong_menu_request(msg_lower):
                # In RAG-first mode, treat catalog/menu asks as FAQ-like retrieval.
                return IntentResult(
                    intent=IntentType.FAQ,
                    confidence=0.9,
                    entities={"requested_topic": "catalog"},
                )

            if self._looks_like_policy_or_timing_query(msg_lower):
                return IntentResult(
                    intent=IntentType.FAQ,
                    confidence=0.9,
                    entities={"requested_topic": "policy_timing"},
                )

            # Use LLM for classification
            result = await llm_client.classify_intent(classification_message, conversation_history, context)

            # Parse result
            raw_intent = result.get("intent", "unclear")
            intent_str = str(raw_intent).lower().replace(" ", "_")
            resolved_intent = config_service.resolve_intent_to_core(intent_str) or intent_str
            confidence = float(result.get("confidence", 0.5))
            entities = result.get("entities", {})
            if not isinstance(entities, dict):
                entities = {}

            if resolved_intent != intent_str:
                entities = dict(entities)
                entities.setdefault("custom_intent", intent_str)
                entities.setdefault("resolved_intent", resolved_intent)

            # Map to IntentType enum
            try:
                intent = IntentType(resolved_intent)
            except ValueError:
                intent = IntentType.UNCLEAR
                confidence = 0.3

            if intent == IntentType.MENU_REQUEST and not config_service.is_menu_runtime_enabled():
                intent = IntentType.FAQ
                confidence = max(confidence, 0.82)
                entities = dict(entities)
                entities.setdefault("requested_topic", "catalog")

            return IntentResult(
                intent=intent,
                confidence=confidence,
                entities=entities,
                requires_confirmation=intent in [IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING],
            )

        except Exception as e:
            print(f"Intent classification error: {e}")
            # Fallback to unclear
            return IntentResult(intent=IntentType.UNCLEAR, confidence=0.3)

    async def _generate_response(
        self,
        user_message: str,
        intent_result: IntentResult,
        conversation_history: list[dict],
        context: dict,
        capability_check: Optional[CapabilityCheck] = None,
    ) -> str:
        """Generate response using LLM (fallback when no handler matched)."""
        try:
            if intent_result.intent == IntentType.CONFIRMATION_YES:
                return self._handle_confirmation_yes(context)

            if intent_result.intent == IntentType.CONFIRMATION_NO:
                return "No problem, I've cancelled that. Is there anything else I can help you with?"

            # Use LLM for response generation
            response = await llm_client.generate_response(
                user_message,
                intent_result.intent.value,
                intent_result.entities,
                conversation_history,
                context,
            )

            # Add confirmation prompt for orders
            if intent_result.intent == IntentType.ORDER_FOOD and "confirm" not in response.lower():
                if intent_result.entities.get("items"):
                    response += "\n\nShall I confirm this order? (Yes/No)"

            return response

        except Exception as e:
            print(f"Response generation error: {e}")
            return "I apologize, but I'm having trouble processing your request right now. Could you please try again or let me connect you with our staff?"

    def _handle_confirmation_yes(self, context: dict) -> str:
        """Handle yes confirmation based on pending action (LLM fallback path)."""
        action = context.get("pending_action")
        pending_data = context.get("pending_data", {})
        if not isinstance(pending_data, dict):
            pending_data = {}

        if action == "confirm_order":
            items = pending_data.get("items", [])
            if not items:
                return "I don't have the order items yet. Please tell me what you'd like to order first."
            return f"Your order has been confirmed!\n\nOrder ID: ORD-{hash(str(items)) % 10000:04d}\nEstimated delivery: 25-30 minutes\n\nI'll notify you when it's on its way. Is there anything else you need?"

        if action == "confirm_booking":
            return "Your table has been reserved! You'll receive a confirmation shortly. Is there anything else I can help with?"

        if action == "show_menu":
            return "Knowledge browsing is handled through the retrieval layer now. Ask for a specific service/topic and I will help."

        return "Great! How can I help you further?"

    def _handle_unexpected_intent(
        self,
        context: ConversationContext,
        intent_result: IntentResult,
        error_msg: Optional[str],
    ) -> str:
        """Handle when intent doesn't match expected state."""
        if context.state == ConversationState.AWAITING_CONFIRMATION:
            return f"I'm waiting for your confirmation on the previous request. Please say 'Yes' to confirm or 'No' to cancel."
        return "I didn't quite understand. Could you please clarify what you'd like to do?"

    def _determine_next_state(
        self,
        context: ConversationContext,
        intent_result: IntentResult,
        response: str,
    ) -> ConversationState:
        """Determine the next conversation state (LLM fallback path)."""
        intent = intent_result.intent

        # Human escalation
        if intent == IntentType.HUMAN_REQUEST:
            return ConversationState.ESCALATED

        # Confirmation handling
        if intent == IntentType.CONFIRMATION_YES:
            pending_action = getattr(context, "pending_action", None)
            pending_data = getattr(context, "pending_data", {})
            if not isinstance(pending_data, dict):
                pending_data = {}
            if pending_action == "confirm_order" and not pending_data.get("items"):
                return ConversationState.AWAITING_INFO
            return ConversationState.COMPLETED

        if intent == IntentType.CONFIRMATION_NO:
            return ConversationState.IDLE

        # Order/booking flow - check if asking for confirmation
        if intent in [IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING]:
            if "confirm" in response.lower() or "yes/no" in response.lower():
                internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                context.pending_action = "confirm_order" if intent == IntentType.ORDER_FOOD else "confirm_booking"
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {"items": intent_result.entities.get("items", [])},
                    internal_pending_entries,
                )
                return ConversationState.AWAITING_CONFIRMATION

        # Stay in current state if awaiting something
        if context.state in [ConversationState.AWAITING_CONFIRMATION, ConversationState.AWAITING_INFO]:
            return context.state

        return ConversationState.IDLE

    def _get_suggested_actions(
        self,
        state: ConversationState,
        intent: IntentType,
        capabilities_summary: dict,
    ) -> list[str]:
        """Get suggested quick actions for the user."""
        if state == ConversationState.AWAITING_CONFIRMATION:
            confirmation_phrase = str(
                getattr(settings, "chat_confirmation_phrase", "yes confirm")
            ).strip() or "yes confirm"
            return [confirmation_phrase, "cancel"]

        if state == ConversationState.ESCALATED:
            return ["Return to bot"]

        if state == ConversationState.COMPLETED:
            return config_service.get_quick_actions(limit=4)

        business_type = str(capabilities_summary.get("business_type", "hotel")).lower()
        quick_actions = config_service.get_quick_actions(limit=4)

        if intent == IntentType.MENU_REQUEST and business_type == "hotel":
            return ["Ask specific question", "View services", "Talk to human"]

        if intent == IntentType.GREETING:
            return quick_actions

        return quick_actions[:2]

    def _build_contextual_suggested_actions(
        self,
        state: ConversationState,
        intent: IntentType,
        pending_action: str | None,
        capabilities_summary: dict,
        pending_data: dict[str, Any] | None = None,
    ) -> list[str]:
        """
        Build context-aware quick actions for UI chips.
        Uses current state + pending action + enabled services, with no outlet-specific hardcoding.
        """
        confirmation_phrase = str(
            getattr(settings, "chat_confirmation_phrase", "yes confirm")
        ).strip() or "yes confirm"

        if state == ConversationState.AWAITING_CONFIRMATION:
            return [confirmation_phrase, "cancel"]
        if state == ConversationState.ESCALATED:
            return ["Return to bot"]

        actions: list[str] = []
        if pending_action:
            if pending_action in {
                "confirm_order",
                "confirm_booking",
                "confirm_service_request",
                "confirm_ticket_creation",
                "confirm_ticket_escalation",
            }:
                actions.extend([confirmation_phrase, "cancel"])
            elif pending_action in {"select_service", "select_restaurant"}:
                actions.extend(["Show available services", "cancel"])
            elif pending_action in {
                "collect_booking_party_size",
                "collect_booking_time",
                "collect_ticket_room_number",
                "collect_ticket_issue_details",
                "collect_ticket_identity_details",
                "collect_ticket_update_note",
            }:
                actions.extend(["Share details", "cancel"])
            else:
                actions.extend(["Continue", "cancel"])

        service_catalog = capabilities_summary.get("service_catalog", [])
        current_phase_id = self._extract_phase_from_pending_data(pending_data)
        if isinstance(service_catalog, list):
            for service in service_catalog:
                if not isinstance(service, dict):
                    continue
                if not bool(service.get("is_active", True)):
                    continue
                if current_phase_id:
                    service_phase = self._normalize_phase_identifier(service.get("phase_id"))
                    if service_phase != current_phase_id:
                        continue
                name = str(service.get("name") or "").strip()
                if name:
                    actions.append(name)
                if len(actions) >= 6:
                    break

        if intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING}:
            actions.extend(["Show options", "Talk to human"])
        elif intent == IntentType.FAQ:
            actions.extend(["Ask another question", "Talk to human"])

        fallback = self._get_suggested_actions(state, intent, capabilities_summary)
        actions.extend(fallback)

        deduped: list[str] = []
        seen: set[str] = set()
        for action in actions:
            text = str(action or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(text)
            if len(deduped) >= 4:
                break
        return deduped or ["Ask another question", "Talk to human"]

    def _ensure_internal_task_state(self, context: ConversationContext) -> dict[str, Any]:
        """Ensure diversion task-state payload exists in internal pending data."""
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        state = context.pending_data.get(_INTERNAL_TASK_STATE_KEY)
        if not isinstance(state, dict):
            state = {}

        parked_tasks = state.get("parked_tasks")
        if not isinstance(parked_tasks, list):
            parked_tasks = []
        normalized_parked: list[dict[str, Any]] = []
        for item in parked_tasks:
            if isinstance(item, dict):
                normalized_parked.append(dict(item))

        active_task = state.get("active_task")
        if not isinstance(active_task, dict):
            active_task = None

        normalized = {
            "version": int(state.get("version") or 1),
            "active_task": dict(active_task) if isinstance(active_task, dict) else None,
            "parked_tasks": normalized_parked[:_MAX_PARKED_TASKS],
            "updated_at": str(state.get("updated_at") or datetime.now(UTC).isoformat()),
        }
        context.pending_data[_INTERNAL_TASK_STATE_KEY] = normalized
        return normalized

    @staticmethod
    def _public_pending_data(context: ConversationContext) -> dict[str, Any]:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        return {
            key: value
            for key, value in pending.items()
            if isinstance(key, str) and not key.startswith("_")
        }

    def _active_task_summary_text(
        self,
        *,
        pending_action: str | None,
        pending_data: dict[str, Any],
    ) -> str:
        service_name = str(
            pending_data.get("service_name")
            or pending_data.get("restaurant_name")
            or pending_data.get("service")
            or ""
        ).strip()
        issue_text = str(
            pending_data.get("issue")
            or pending_data.get("ticket_issue")
            or pending_data.get("request")
            or ""
        ).strip()
        item_name = str(
            pending_data.get("order_item")
            or pending_data.get("item_name")
            or ""
        ).strip()
        if service_name:
            return f"your {service_name} request"
        if issue_text:
            return issue_text[:120]
        if item_name:
            return f"your {item_name} request"
        action_label = str(pending_action or "").strip().replace("_", " ")
        if action_label:
            return f"your {action_label} flow"
        return "your previous request"

    def _sync_active_task_snapshot(
        self,
        *,
        context: ConversationContext,
        selected_phase_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep internal active_task aligned with current pending flow."""
        task_state = self._ensure_internal_task_state(context)
        if not str(context.pending_action or "").strip():
            task_state["active_task"] = None
            task_state["updated_at"] = datetime.now(UTC).isoformat()
            context.pending_data[_INTERNAL_TASK_STATE_KEY] = task_state
            return task_state

        pending_public = self._public_pending_data(context)
        phase_ctx = selected_phase_context if isinstance(selected_phase_context, dict) else {}
        task_state["active_task"] = {
            "pending_action": str(context.pending_action or "").strip(),
            "pending_data": dict(pending_public),
            "state": context.state.value,
            "phase_id": str(phase_ctx.get("selected_phase_id") or "").strip(),
            "phase_name": str(phase_ctx.get("selected_phase_name") or "").strip(),
            "summary": self._active_task_summary_text(
                pending_action=context.pending_action,
                pending_data=pending_public,
            ),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        task_state["updated_at"] = datetime.now(UTC).isoformat()
        context.pending_data[_INTERNAL_TASK_STATE_KEY] = task_state
        return task_state

    def _park_active_task(
        self,
        *,
        context: ConversationContext,
        selected_phase_context: dict[str, Any] | None = None,
        reason: str = "topic_diversion",
        user_message: str = "",
    ) -> dict[str, Any] | None:
        """Suspend current pending flow into parked task queue."""
        task_state = self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context or {},
        )
        active_task = task_state.get("active_task")
        if not isinstance(active_task, dict):
            return None

        parked_task = dict(active_task)
        parked_task["reason"] = str(reason or "topic_diversion")
        parked_task["diversion_message"] = str(user_message or "").strip()[:280]
        parked_task["parked_at"] = datetime.now(UTC).isoformat()

        parked_tasks = [
            dict(item)
            for item in (task_state.get("parked_tasks") or [])
            if isinstance(item, dict)
        ]
        parked_tasks.insert(0, parked_task)
        deduped: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for item in parked_tasks:
            key = json.dumps(
                {
                    "pending_action": str(item.get("pending_action") or ""),
                    "pending_data": item.get("pending_data") if isinstance(item.get("pending_data"), dict) else {},
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(item)
            if len(deduped) >= _MAX_PARKED_TASKS:
                break

        task_state["active_task"] = None
        task_state["parked_tasks"] = deduped
        task_state["updated_at"] = datetime.now(UTC).isoformat()

        internal_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        internal_entries[_INTERNAL_TASK_STATE_KEY] = task_state
        context.pending_action = None
        context.pending_data = conversation_memory_service.merge_with_internal({}, internal_entries)
        context.pending_data.pop("_clarification_attempts", None)
        context.state = ConversationState.IDLE
        return parked_task

    def _peek_parked_task(self, context: ConversationContext) -> dict[str, Any] | None:
        task_state = self._ensure_internal_task_state(context)
        for item in task_state.get("parked_tasks", []):
            if isinstance(item, dict):
                return dict(item)
        return None

    def _restore_parked_task(self, context: ConversationContext) -> dict[str, Any] | None:
        """Resume the most recently parked task."""
        task_state = self._ensure_internal_task_state(context)
        parked_tasks = [
            dict(item)
            for item in (task_state.get("parked_tasks") or [])
            if isinstance(item, dict)
        ]
        if not parked_tasks:
            return None

        task = parked_tasks.pop(0)
        pending_action = str(task.get("pending_action") or "").strip()
        pending_public = task.get("pending_data")
        if not isinstance(pending_public, dict):
            pending_public = {}

        internal_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        task_state["parked_tasks"] = parked_tasks
        task_state["active_task"] = dict(task)
        task_state["updated_at"] = datetime.now(UTC).isoformat()
        internal_entries[_INTERNAL_TASK_STATE_KEY] = task_state

        context.pending_action = pending_action or None
        context.pending_data = conversation_memory_service.merge_with_internal(
            dict(pending_public),
            internal_entries,
        )

        restored_state_raw = str(task.get("state") or "").strip().lower().replace("-", "_")
        try:
            restored_state = ConversationState(restored_state_raw)
        except Exception:
            restored_state = ConversationState.AWAITING_INFO
        context.state = restored_state
        return task

    def _cancel_parked_task(self, context: ConversationContext) -> dict[str, Any] | None:
        """Discard the most recently parked task."""
        task_state = self._ensure_internal_task_state(context)
        parked_tasks = [
            dict(item)
            for item in (task_state.get("parked_tasks") or [])
            if isinstance(item, dict)
        ]
        if not parked_tasks:
            return None
        task = parked_tasks.pop(0)
        task_state["parked_tasks"] = parked_tasks
        task_state["updated_at"] = datetime.now(UTC).isoformat()
        context.pending_data[_INTERNAL_TASK_STATE_KEY] = task_state
        return task

    @staticmethod
    def _is_resume_task_request(message: str) -> bool:
        compact = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not compact:
            return False
        if compact in _RESUME_TASK_MARKERS:
            return True
        return compact.startswith("resume ")

    @staticmethod
    def _is_cancel_task_request(message: str) -> bool:
        compact = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not compact:
            return False
        if compact in _CANCEL_TASK_MARKERS:
            return True
        return compact in {"cancel pending", "cancel pending task", "cancel"}

    def _should_auto_resume_parked_task(
        self,
        *,
        message: str,
        parked_task: dict[str, Any],
        capabilities_summary: dict[str, Any],
    ) -> bool:
        """
        Resume a parked task when user naturally returns with task-specific details.
        """
        msg_lower = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg_lower:
            return False
        if self._is_resume_task_request(msg_lower) or self._is_cancel_task_request(msg_lower):
            return False
        if self._looks_like_information_query(msg_lower):
            return False
        if self._looks_like_problem_report(msg_lower):
            return False

        pending_action = str(parked_task.get("pending_action") or "").strip().lower()
        if not pending_action:
            return False
        pending_data = parked_task.get("pending_data")
        if not isinstance(pending_data, dict):
            pending_data = {}

        if self._is_room_booking_pending_action(pending_action):
            if self._looks_like_room_booking_detail_followup(msg_lower):
                return True
            if self._looks_like_room_type_preference_reply(msg_lower):
                return True
            if self._looks_like_room_stay_booking_request(msg_lower):
                return True
            return bool(
                "?" not in msg_lower
                and re.search(r"\b(room|suite|stay|check in|check-in|check out|check-out|guest|guests)\b", msg_lower)
            )

        if pending_action in {"select_service", "select_restaurant"}:
            if self._looks_like_service_selection(msg_lower, capabilities_summary):
                return True
            known_service = str(
                pending_data.get("service_name")
                or pending_data.get("restaurant_name")
                or pending_data.get("service")
                or ""
            ).strip().lower()
            if known_service and (known_service in msg_lower or msg_lower in known_service):
                return True
            return False

        if pending_action == "collect_booking_party_size":
            return self._looks_like_party_size_reply(msg_lower)
        if pending_action == "collect_booking_time":
            return self._looks_like_time_reply(msg_lower)
        if pending_action in {"collect_ticket_room_number", "awaiting_room_number"}:
            return self._looks_like_room_number_reply(msg_lower)
        if pending_action in {"collect_ticket_issue_details", "collect_ticket_update_note"}:
            return self._looks_like_ticket_update_note(msg_lower)
        if pending_action == "collect_ticket_identity_details":
            return self._looks_like_ticket_identity_details_reply(msg_lower)
        if pending_action == "awaiting_request_detail":
            return self._looks_like_room_service_detail(msg_lower)
        if pending_action == "collect_transport_details":
            return self._looks_like_transport_detail(msg_lower)
        if pending_action == "collect_service_details":
            return self._looks_like_service_detail_reply(msg_lower)

        if pending_action.startswith("confirm_"):
            return msg_lower in {"yes", "yes confirm", "confirm", "no", "cancel"}

        return False

    def _append_resume_checkpoint(
        self,
        *,
        response_text: str,
        context: ConversationContext,
        pending_interrupted: bool,
    ) -> tuple[str, list[str]]:
        """Add resume/cancel checkpoint after a diversion answer."""
        if not pending_interrupted:
            return response_text, []
        parked_task = self._peek_parked_task(context)
        if parked_task is None:
            return response_text, []
        summary = str(
            parked_task.get("summary")
            or self._active_task_summary_text(
                pending_action=parked_task.get("pending_action"),
                pending_data=parked_task.get("pending_data")
                if isinstance(parked_task.get("pending_data"), dict)
                else {},
            )
        ).strip()
        checkpoint_line = (
            f"Shall we continue with {summary}? "
            "Reply 'resume' to continue or 'cancel pending' to drop it."
        )
        current_text = str(response_text or "").strip()
        if checkpoint_line.lower() in current_text.lower():
            return current_text, ["resume", "cancel pending"]
        if current_text:
            current_text = f"{current_text}\n\n{checkpoint_line}"
        else:
            current_text = checkpoint_line
        return current_text, ["resume", "cancel pending"]

    def _infer_context_missing_slots(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
    ) -> list[str]:
        pending_public = self._public_pending_data(context)
        missing: list[str] = []
        if context.state == ConversationState.AWAITING_INFO and context.pending_action:
            missing.append(str(context.pending_action))

        target_service_id = self._normalize_service_identifier(
            pending_public.get("service_id")
            or pending_public.get("target_service_id")
            or pending_public.get("resolved_service_id")
        )
        if not target_service_id:
            return missing

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return missing
        target_service = next(
            (
                service
                for service in service_catalog
                if isinstance(service, dict)
                and self._normalize_service_identifier(service.get("id")) == target_service_id
            ),
            None,
        )
        if not isinstance(target_service, dict):
            return missing
        prompt_pack = target_service.get("service_prompt_pack")
        if not isinstance(prompt_pack, dict):
            return missing
        required_slots = prompt_pack.get("required_slots")
        if not isinstance(required_slots, list):
            return missing
        for slot in required_slots:
            if not isinstance(slot, dict):
                continue
            if not bool(slot.get("required", True)):
                continue
            slot_id = str(slot.get("id") or "").strip()
            if not slot_id:
                continue
            if str(pending_public.get(slot_id) or "").strip():
                continue
            missing.append(slot_id)
        deduped: list[str] = []
        seen: set[str] = set()
        for item in missing:
            key = str(item or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(str(item))
        return deduped[:8]

    def _build_llm_context_pack(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        selected_phase_context: dict[str, Any],
        memory_snapshot: dict[str, Any],
        user_message: str,
    ) -> dict[str, Any]:
        phase_id = self._normalize_phase_identifier(selected_phase_context.get("selected_phase_id"))
        phase_name = str(selected_phase_context.get("selected_phase_name") or "").strip()

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            service_catalog = []
        phase_services: list[dict[str, Any]] = []
        ticketing_enabled_by_service: dict[str, bool] = {}
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_id = self._normalize_service_identifier(service.get("id"))
            if not service_id:
                continue
            ticketing_enabled = bool(service.get("ticketing_enabled", True))
            ticketing_enabled_by_service[service_id] = ticketing_enabled
            service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if phase_id and service_phase_id and service_phase_id != phase_id:
                continue
            phase_services.append(
                {
                    "id": service_id,
                    "name": str(service.get("name") or service_id).strip(),
                    "type": str(service.get("type") or "service").strip(),
                    "ticketing_enabled": ticketing_enabled,
                }
            )

        stay_window = self._resolve_known_stay_window(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        stay_window_payload: dict[str, Any] = {"available": False}
        if stay_window is not None:
            stay_window_payload = {
                "available": True,
                "check_in": self._format_calendar_date_label(stay_window[0]),
                "check_out": self._format_calendar_date_label(stay_window[1]),
            }

        memory_facts = memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {}
        if not isinstance(memory_facts, dict):
            memory_facts = {}
        recent_goal = str(
            memory_facts.get("last_user_topic")
            or memory_facts.get("latest_user_request")
            or user_message
            or ""
        ).strip()
        flow_type = "information"
        if context.pending_action or context.state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
            ConversationState.PROCESSING_ORDER,
        }:
            flow_type = "transactional"

        return {
            "current_phase": {"id": phase_id, "name": phase_name},
            "active_flow": {
                "state": context.state.value,
                "type": flow_type,
            },
            "pending_action": str(context.pending_action or "").strip(),
            "missing_slots": self._infer_context_missing_slots(
                context=context,
                capabilities_summary=capabilities_summary,
            ),
            "recent_user_goal": recent_goal[:180],
            "phase_services": phase_services[:30],
            "ticketing_enabled_by_service": ticketing_enabled_by_service,
            "stay_window": stay_window_payload,
            "current_time": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _apply_ticket_metadata_contract(metadata: dict[str, Any]) -> dict[str, Any]:
        """Normalize ticket metadata keys across runtime paths."""
        normalized = dict(metadata or {})
        ticket_skip_reason = str(normalized.get("ticket_skip_reason") or "").strip()
        if not ticket_skip_reason:
            ticket_skip_reason = str(normalized.get("ticket_create_skip_reason") or "").strip()
        if not ticket_skip_reason:
            ticket_skip_reason = str(normalized.get("ticketing_skip_reason") or "").strip()
        if ticket_skip_reason:
            normalized["ticket_skip_reason"] = ticket_skip_reason
            normalized.setdefault("ticket_created", False)
        return normalized

    def _ingest_request_metadata(self, context: ConversationContext, request: ChatRequest) -> None:
        """
        Persist integration identifiers from ChatRequest metadata into context.
        Stored under pending_data['_integration'] so handlers can build external payloads.
        """
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        existing = context.pending_data.get("_integration", {})
        integration: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        metadata = request.metadata if isinstance(request.metadata, dict) else {}

        canonical_map: dict[str, tuple[str, ...]] = {
            "guest_id": ("guest_id", "user_id"),
            "guest_name": ("guest_name", "name", "user_name"),
            "room_number": ("room_number", "room"),
            "organisation_id": ("organisation_id", "organization_id", "org_id", "entity_id"),
            "entity_id": ("entity_id",),
            "group_id": ("group_id",),
            "conversation_id": ("conversation_id",),
            "agent_id": ("agent_id", "staff_id"),
            "phase": ("phase",),
            "message_id": ("message_id",),
            "ticket_source": ("ticket_source", "source"),
            "flow": ("flow", "bot_mode"),
        }

        for canonical, aliases in canonical_map.items():
            for alias in aliases:
                value = metadata.get(alias)
                if value in (None, ""):
                    continue
                integration[canonical] = value
                break

        if request.guest_phone:
            integration["guest_phone"] = request.guest_phone
        if context.channel:
            integration["channel"] = context.channel
        if context.hotel_code:
            integration.setdefault("organisation_id", context.hotel_code)

        if not context.guest_name:
            guest_name = str(integration.get("guest_name") or "").strip()
            if guest_name:
                context.guest_name = guest_name
        if not context.room_number:
            room_number = str(integration.get("room_number") or "").strip()
            if room_number:
                context.room_number = room_number

        context.pending_data["_integration"] = integration

    async def _augment_capabilities_from_db(self, capabilities_summary: dict, db_session=None) -> dict:
        """
        Merge runtime DB service rows into capability summary for backward compatibility
        with legacy schemas that still store service outlets in `new_bot_restaurants`.
        """
        if db_session is None:
            return capabilities_summary

        from sqlalchemy import select
        from models.database import Hotel, Restaurant

        merged = dict(capabilities_summary or {})
        service_catalog = list(merged.get("service_catalog", []) or [])

        existing_service_ids = {
            str(service.get("id") or "").strip().lower()
            for service in service_catalog
            if isinstance(service, dict)
        }

        hotel_id = merged.get("hotel_id")
        if hotel_id is None:
            hotel_name = str(merged.get("hotel_name") or "").strip()
            if hotel_name:
                hotel_row = (
                    await db_session.execute(select(Hotel).where(Hotel.name.ilike(f"%{hotel_name}%")))
                ).scalar_one_or_none()
                if hotel_row is not None:
                    hotel_id = hotel_row.id
        if hotel_id is None:
            hotel_row = (
                await db_session.execute(select(Hotel).where(Hotel.is_active == True).limit(1))  # noqa: E712
            ).scalar_one_or_none()
            if hotel_row is not None:
                hotel_id = hotel_row.id
        if hotel_id is None:
            return merged

        rows = (
            await db_session.execute(
                select(Restaurant).where(Restaurant.hotel_id == hotel_id)
            )
        ).scalars().all()

        for row in rows:
            service_id = str(row.code or "").strip().lower()
            if not service_id:
                continue
            if service_id not in existing_service_ids and not config_service.is_menu_runtime_enabled():
                # In RAG-first mode, keep JSON/admin services as authoritative.
                continue

            hours_open = row.opens_at.strftime("%H:%M:%S") if row.opens_at else "00:00:00"
            hours_close = row.closes_at.strftime("%H:%M:%S") if row.closes_at else "23:59:00"
            delivery_zones = ["room"] if row.delivers_to_room else ["dine_in_only"]

            if service_id not in existing_service_ids:
                service_catalog.append(
                    {
                        "id": service_id,
                        "name": row.name,
                        "type": "service",
                        "description": row.cuisine or "",
                        "cuisine": row.cuisine or "",
                        "hours": {"open": hours_open, "close": hours_close},
                        "delivery_zones": delivery_zones,
                        "is_active": bool(row.is_active),
                    }
                )
                existing_service_ids.add(service_id)

        merged["hotel_id"] = hotel_id
        merged["service_catalog"] = service_catalog
        merged["restaurants"] = []
        return merged


# Global instance
chat_service = ChatService()
