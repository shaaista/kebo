"""
Room Service Handler

Handles ROOM_SERVICE intent: housekeeping, amenity requests,
towels, laundry, cleaning, and other in-room service needs.
"""

import re
from typing import Any

from handlers.base_handler import BaseHandler, HandlerResult
from integrations.lumira_ticketing_repository import lumira_ticketing_repository
from schemas.chat import ConversationState, IntentResult, ConversationContext
from services.ticketing_agent_service import ticketing_agent_service
from services.ticketing_service import ticketing_service
# Map of keywords to friendly request type labels
REQUEST_TYPE_KEYWORDS: dict[str, list[str]] = {
    "cleaning": ["clean", "cleaning", "tidy", "vacuum", "mop", "housekeep"],
    "towels": ["towel", "towels", "bath towel", "hand towel"],
    "amenities": ["amenity", "amenities", "toiletries", "shampoo", "soap", "toothbrush"],
    "laundry": ["laundry", "ironing", "iron", "dry clean", "washing", "press"],
    "extra bedding": ["blanket", "pillow", "bedding", "extra bed", "bed sheet"],
    "minibar": ["minibar", "mini bar", "refill", "water bottle"],
    "maintenance": ["repair", "fix", "broken", "not working", "leaking", "ac", "plumbing"],
}


class RoomServiceHandler(BaseHandler):
    """Handles in-room service requests (housekeeping, amenities, laundry, etc.)."""

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        if context.pending_action == "awaiting_request_detail":
            result = self._handle_request_detail_reply(message, context)
            return await self._with_ticket_if_applicable(
                result=result,
                message=message,
                context=context,
                capabilities=capabilities,
                db_session=db_session,
            )

        # --- Handle room number follow-up ---
        if context.pending_action == "awaiting_room_number":
            result = self._handle_room_number_reply(message, context)
            return await self._with_ticket_if_applicable(
                result=result,
                message=message,
                context=context,
                capabilities=capabilities,
                db_session=db_session,
            )

        # --- Capability check ---
        room_service_enabled = self._is_capability_enabled(capabilities, "room_service")
        housekeeping_enabled = self._is_capability_enabled(capabilities, "housekeeping")

        if not room_service_enabled and not housekeeping_enabled:
            return HandlerResult(
                response_text=(
                    "I'm sorry, room service requests are not available through the bot at this time. "
                    "Please contact the front desk directly for assistance."
                ),
                next_state=ConversationState.IDLE,
                suggested_actions=["Contact reception", "Need help"],
            )

        # --- Detect request type ---
        entities = intent_result.entities
        request_type = entities.get("request_type") or self._detect_request_type(message)

        if request_type == "room service":
            return HandlerResult(
                response_text=(
                    "Sure, I can help with that. What do you need from room service "
                    "(for example: towels, amenities, cleaning, laundry, or maintenance)?"
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="awaiting_request_detail",
                pending_data={"original_message": message},
                suggested_actions=["Fresh towels", "Room cleaning", "Laundry", "AC not working"],
            )

        # --- Check if room number is available ---
        room_number = context.room_number
        if not room_number:
            room_number = await self._resolve_room_number_from_db(context, db_session)
            if room_number:
                context.room_number = room_number

        if not room_number:
            return HandlerResult(
                response_text="Could you please share your room number so I can forward your request?",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="awaiting_room_number",
                pending_data={
                    "request_type": request_type,
                    "original_message": message,
                },
                suggested_actions=["101", "202", "305"],
            )

        # --- Build success response ---
        result = self._build_service_response(request_type, room_number)
        return await self._with_ticket_if_applicable(
            result=result,
            message=message,
            context=context,
            capabilities=capabilities,
            db_session=db_session,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _handle_room_number_reply(
        self, message: str, context: ConversationContext
    ) -> HandlerResult:
        """Process the room number the guest provided."""
        room_number = message.strip()

        # Basic validation: room numbers are typically numeric or alphanumeric
        if not room_number or len(room_number) > 10 or not re.fullmatch(r"[A-Za-z0-9-]{2,10}", room_number):
            return HandlerResult(
                response_text="That doesn't look like a valid room number. Could you please try again?",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="awaiting_room_number",
                pending_data=context.pending_data,
            )

        request_type = context.pending_data.get("request_type", "general assistance")

        # Update the room number on the context (caller should persist this)
        return HandlerResult(
            **self._build_service_response(request_type, room_number).model_dump(),
            # Caller can read metadata to update context.room_number
        )

    def _handle_request_detail_reply(self, message: str, context: ConversationContext) -> HandlerResult:
        """Collect missing room-service request detail and continue slot filling."""
        request_type = self._detect_request_type(message)
        if request_type == "room service":
            return HandlerResult(
                response_text=(
                    "I can help with housekeeping requests like towels, amenities, cleaning, "
                    "laundry, or maintenance. What exactly should I arrange?"
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="awaiting_request_detail",
                pending_data=context.pending_data,
                suggested_actions=["Fresh towels", "Room cleaning", "Laundry", "AC not working"],
            )

        room_number = context.room_number
        if not room_number:
            return HandlerResult(
                response_text="Thanks. Could you also share your room number?",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="awaiting_room_number",
                pending_data={"request_type": request_type},
                suggested_actions=["101", "202", "305"],
            )

        return self._build_service_response(request_type, room_number)

    def _build_service_response(
        self, request_type: str, room_number: str
    ) -> HandlerResult:
        """Create the final response once we have all info."""
        return HandlerResult(
            response_text=(
                f"I've forwarded your {request_type} request to our service team. "
                f"They'll attend to room {room_number} shortly. "
                "Is there anything else I can help you with?"
            ),
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["View menu", "Book a table", "Need help"],
            metadata={
                "request_type": request_type,
                "room_number": room_number,
            },
        )

    async def _with_ticket_if_applicable(
        self,
        *,
        result: HandlerResult,
        message: str,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        """
        Attach operational ticket creation for actionable room-service outcomes
        without changing conversational response text.
        """
        if not isinstance(result.metadata, dict):
            result.metadata = {}
        if result.metadata.get("ticket_created") is True:
            return result

        if result.next_state not in {ConversationState.IDLE, ConversationState.COMPLETED}:
            return result

        request_type = str(result.metadata.get("request_type") or "").strip().lower()
        room_number = str(result.metadata.get("room_number") or context.room_number or "").strip()
        if not request_type:
            return result

        if not room_number:
            room_number = await self._resolve_room_number_from_db(context, db_session)
            if room_number:
                context.room_number = room_number
                result.metadata["room_number"] = room_number

        if not room_number:
            return result

        if not ticketing_service.is_ticketing_enabled(capabilities):
            result.metadata.setdefault("ticket_created", False)
            result.metadata.setdefault("ticket_skipped", True)
            return result

        configured_cases = ticketing_agent_service.get_configured_cases()
        if not configured_cases:
            result.metadata.update(
                {
                    "ticket_created": False,
                    "ticket_skipped": True,
                    "ticket_skip_reason": "no_configured_ticket_cases",
                    "ticket_source": "room_service_handler",
                }
            )
            return result

        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        original_message = str(pending.get("original_message") or "").strip()
        if not original_message:
            original_message = str(message or "").strip()

        sub_category = self._sub_category_from_request_type(request_type)
        issue = f"{request_type.title()} request for room {room_number}: {original_message}".strip()
        matched_case = await ticketing_agent_service.match_configured_case_async(
            message=f"{issue} {request_type}".strip(),
            conversation_excerpt=self._build_ticketing_case_context_text(
                context=context,
                latest_issue=issue,
            ),
            llm_response_text="",
        )
        if not matched_case:
            result.metadata.update(
                {
                    "ticket_created": False,
                    "ticket_skipped": True,
                    "ticket_skip_reason": "no_matching_configured_ticket_case",
                    "ticket_source": "room_service_handler",
                }
            )
            return result

        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category="request",
                sub_category=sub_category,
                priority="medium",
                phase="during_stay",
            )
            payload["room_number"] = room_number
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            result.metadata.update(
                {
                    "ticket_created": False,
                    "ticket_create_error": str(exc),
                    "ticket_source": "room_service_handler",
                }
            )
            return result

        if not create_result.success:
            result.metadata.update(
                {
                    "ticket_created": False,
                    "ticket_create_error": str(create_result.error or "ticket_create_failed"),
                    "ticket_source": "room_service_handler",
                    "ticket_api_status_code": create_result.status_code,
                    "ticket_api_response": create_result.response,
                }
            )
            return result

        result.metadata.update(
            {
                "ticket_created": True,
                "ticket_id": str(create_result.ticket_id or "").strip(),
                "ticket_status": "open",
                "ticket_category": "request",
                "ticket_sub_category": sub_category,
                "ticket_priority": "medium",
                "ticket_source": "room_service_handler",
                "ticket_matched_case": matched_case,
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }
        )
        return result

    async def _resolve_room_number_from_db(
        self,
        context: ConversationContext,
        db_session: Any = None,
    ) -> str:
        if db_session is None:
            return ""

        integration = ticketing_service.get_integration_context(context)
        entity_id = integration.get("entity_id") or integration.get("organisation_id")
        guest_id = integration.get("guest_id")
        guest_phone = (
            integration.get("guest_phone")
            or context.guest_phone
            or ""
        )
        if entity_id in (None, ""):
            return ""
        if guest_id in (None, "") and not str(guest_phone or "").strip():
            return ""

        profile = await lumira_ticketing_repository.fetch_guest_profile(
            db_session,
            entity_id=entity_id,
            guest_id=guest_id,
            guest_phone=str(guest_phone or "").strip(),
        )
        if not profile:
            return ""

        room_number = str(profile.get("room_number") or "").strip()
        if not room_number:
            return ""

        integration["room_number"] = room_number
        if str(profile.get("guest_id") or "").strip():
            integration["guest_id"] = str(profile.get("guest_id") or "").strip()
        if isinstance(context.pending_data, dict):
            context.pending_data["_integration"] = integration
        return room_number

    @staticmethod
    def _sub_category_from_request_type(request_type: str) -> str:
        normalized = str(request_type or "").strip().lower()
        mapping = {
            "cleaning": "housekeeping",
            "towels": "amenities",
            "amenities": "amenities",
            "laundry": "laundry",
            "extra bedding": "amenities",
            "minibar": "order_food",
            "maintenance": "maintenance",
        }
        return mapping.get(normalized, "room_service")

    @staticmethod
    def _build_ticketing_case_context_text(
        *,
        context: ConversationContext,
        latest_issue: str,
        max_messages: int = 10,
    ) -> str:
        lines: list[str] = []
        for msg in context.get_recent_messages(max_messages):
            role = "User" if str(getattr(msg.role, "value", msg.role)) == "user" else "Assistant"
            content = str(msg.content or "").strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")
        if latest_issue:
            lines.append(f"Ticket Issue Draft: {latest_issue}")
        joined = "\n".join(lines).strip()
        if len(joined) <= 1500:
            return joined
        return joined[-1500:]

    def _detect_request_type(self, message: str) -> str:
        """Detect the room-service request type from message keywords."""
        msg_lower = message.lower()
        for label, keywords in REQUEST_TYPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in msg_lower:
                    return label
        return "room service"
