"""
Booking Handler

Handles TABLE_BOOKING intent as a generic booking workflow,
including detail collection, confirmation flow, and reference generation.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from handlers.base_handler import BaseHandler, HandlerResult
from schemas.chat import ConversationContext, ConversationState, IntentResult, MessageRole, IntentType
from services.ticketing_agent_service import ticketing_agent_service
from services.ticketing_service import ticketing_service


class BookingHandler(BaseHandler):
    """Handles generic service booking / reservation requests."""

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        # Confirmation flow: user replied yes/no to a pending booking.
        if context.pending_action == "confirm_booking":
            msg_lower = str(message or "").strip().lower()
            if intent_result.intent in {IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO} or msg_lower in {"yes", "no", "cancel", "stop"}:
                return await self._handle_booking_confirmation(intent_result, context, capabilities)
            # Treat non-confirmation messages as a new/updated booking request.
            context.pending_action = None

        if context.pending_action in {"select_service", "select_restaurant", "collect_booking_party_size", "collect_booking_time"}:
            return self._handle_booking_details_followup(message, intent_result, context, capabilities)

        # Capability check.
        if not self._is_capability_enabled(capabilities, "table_booking"):
            return HandlerResult(
                response_text=(
                    "I'm sorry, booking requests are not available at this time. "
                    "You may contact the front desk or support team."
                ),
                next_state=ConversationState.IDLE,
                suggested_actions=["Contact reception", "Need help"],
            )

        entities = intent_result.entities
        booking_data: dict[str, Any] = {
            "service_name": (
                entities.get("service_name")
                or entities.get("service")
                or entities.get("restaurant_name")
                or entities.get("restaurant")
                or ""
            ).strip(),
            "party_size": str(entities.get("party_size") or "").strip(),
            "time": str(entities.get("time") or "").strip(),
            "date": str(entities.get("date") or self._extract_date_hint(message) or "today").strip(),
        }
        if not booking_data["service_name"]:
            hinted_service = self._extract_service_hint_from_text(message)
            if hinted_service:
                booking_data["service_name"] = hinted_service

        if booking_data["service_name"]:
            service_match = self._find_service(booking_data["service_name"], capabilities)
            if service_match is None:
                if not self._has_service_catalog(capabilities):
                    booking_data["service_name"] = self._normalize_service_label(booking_data["service_name"])
                else:
                    available = self._list_service_names(capabilities)
                    available_text = ", ".join(available) if available else "our available services"
                    return HandlerResult(
                        response_text=(
                            f"I couldn't find a matching service called '{booking_data['service_name']}'. "
                            f"Available options: {available_text}. Which one would you like to book?"
                        ),
                        next_state=ConversationState.AWAITING_INFO,
                        pending_action="select_service",
                        pending_data=booking_data,
                        suggested_actions=available,
                    )
            else:
                booking_data["service_name"] = service_match.get("name", booking_data["service_name"])

        missing = self._next_missing_booking_field(booking_data)
        if missing is not None:
            return self._prompt_for_missing_booking_field(missing, booking_data, capabilities)

        hours_violation = self._validate_booking_window(booking_data, capabilities)
        if hours_violation is not None:
            return hours_violation

        return self._build_booking_confirmation(booking_data)

    def _handle_booking_details_followup(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
    ) -> HandlerResult:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        booking_data = {
            "service_name": str(pending.get("service_name") or pending.get("restaurant_name") or "").strip(),
            "party_size": str(pending.get("party_size") or "").strip(),
            "time": str(pending.get("time") or "").strip(),
            "date": str(pending.get("date") or "today").strip(),
        }
        msg_lower = message.lower().strip()

        from schemas.chat import IntentType

        if intent_result.intent == IntentType.CONFIRMATION_NO or msg_lower in {"cancel", "no", "stop"}:
            return HandlerResult(
                response_text="No problem, I've cancelled this booking request.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["View services", "Need help"],
            )

        action = context.pending_action
        if action in {"select_service", "select_restaurant"}:
            match = self._find_service(message, capabilities)
            if match is not None:
                booking_data["service_name"] = match.get("name", "")
            else:
                fallback_name = self._extract_service_hint_from_text(message)
                if not fallback_name:
                    fallback_name = self._infer_service_from_recent_messages(context, capabilities, message)
                if fallback_name:
                    booking_data["service_name"] = self._normalize_service_label(fallback_name)
                elif self._has_service_catalog(capabilities):
                    available = self._list_service_names(capabilities)
                    available_text = ", ".join(available) if available else "our available services"
                    return HandlerResult(
                        response_text=(
                            f"I couldn't match that service. Available options: {available_text}. "
                            "Please tell me which one you prefer."
                        ),
                        next_state=ConversationState.AWAITING_INFO,
                        pending_action="select_service",
                        pending_data=booking_data,
                        suggested_actions=available,
                    )
                else:
                    return HandlerResult(
                        response_text=(
                            "Please share the service or outlet name you want to book "
                            "(for example: Kadak or In-Room Dining)."
                        ),
                        next_state=ConversationState.AWAITING_INFO,
                        pending_action="select_service",
                        pending_data=booking_data,
                        suggested_actions=["cancel"],
                    )

        if action == "collect_booking_party_size":
            party_size = self._extract_party_size(message) or str(intent_result.entities.get("party_size") or "").strip()
            if not party_size:
                return HandlerResult(
                    response_text="How many guests should I reserve for?",
                    next_state=ConversationState.AWAITING_INFO,
                    pending_action="collect_booking_party_size",
                    pending_data=booking_data,
                    suggested_actions=["cancel"],
                )
            booking_data["party_size"] = party_size

        if action == "collect_booking_time":
            booking_time = self._extract_time(message) or str(intent_result.entities.get("time") or "").strip()
            if not booking_time:
                return HandlerResult(
                    response_text="Please share your preferred booking time (for example: 7:30 PM).",
                    next_state=ConversationState.AWAITING_INFO,
                    pending_action="collect_booking_time",
                    pending_data=booking_data,
                    suggested_actions=["cancel"],
                )
            booking_data["time"] = booking_time
            booking_data["date"] = str(
                intent_result.entities.get("date") or self._extract_date_hint(message) or booking_data.get("date") or "today"
            )

        if not booking_data["service_name"]:
            match = self._find_service(message, capabilities)
            if match is not None:
                booking_data["service_name"] = match.get("name", "")
        if not booking_data["party_size"]:
            booking_data["party_size"] = self._extract_party_size(message)
        if not booking_data["time"]:
            booking_data["time"] = self._extract_time(message)
        if not booking_data["date"]:
            booking_data["date"] = self._extract_date_hint(message) or "today"

        missing = self._next_missing_booking_field(booking_data)
        if missing is not None:
            return self._prompt_for_missing_booking_field(missing, booking_data, capabilities)

        hours_violation = self._validate_booking_window(booking_data, capabilities)
        if hours_violation is not None:
            return hours_violation

        return self._build_booking_confirmation(booking_data)

    def _build_booking_confirmation(self, booking_data: dict[str, Any]) -> HandlerResult:
        service_name = booking_data.get("service_name") or "this service"
        party = booking_data.get("party_size") or ""
        booking_time = booking_data.get("time") or ""
        booking_date = booking_data.get("date") or "today"

        date_display = f" on {booking_date}" if booking_date and booking_date != "today" else ""
        confirmation_msg = (
            f"I'd like to book {service_name} for {party} guests at {booking_time}{date_display}. "
            "Shall I confirm this booking?"
        )
        return HandlerResult(
            response_text=confirmation_msg,
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_booking",
            pending_data={
                "service_name": service_name,
                "restaurant_name": service_name,  # backward-compatible alias
                "party_size": party,
                "time": booking_time,
                "date": booking_date,
            },
            suggested_actions=["Yes, confirm", "No, cancel"],
        )

    def _next_missing_booking_field(self, booking_data: dict[str, Any]) -> str | None:
        if not str(booking_data.get("service_name") or "").strip():
            return "service_name"
        if not str(booking_data.get("party_size") or "").strip():
            return "party_size"
        if not str(booking_data.get("time") or "").strip():
            return "time"
        return None

    def _prompt_for_missing_booking_field(
        self,
        missing_field: str,
        booking_data: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> HandlerResult:
        if missing_field == "service_name":
            available = self._list_service_names(capabilities)
            available_text = ", ".join(available) if available else "our available services"
            return HandlerResult(
                response_text=f"Sure. Which service would you like to book? Available options: {available_text}.",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="select_service",
                pending_data=booking_data,
                suggested_actions=available,
            )

        if missing_field == "party_size":
            return HandlerResult(
                response_text="How many guests should I reserve for?",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_booking_party_size",
                pending_data=booking_data,
                suggested_actions=["2", "4", "6"],
            )

        return HandlerResult(
            response_text="What time should I reserve this booking for?",
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_booking_time",
            pending_data=booking_data,
            suggested_actions=["7 PM", "8 PM", "9 PM"],
        )

    async def _handle_booking_confirmation(
        self,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
    ) -> HandlerResult:
        """Process yes/no reply to a pending booking confirmation."""
        from schemas.chat import IntentType

        if intent_result.intent == IntentType.CONFIRMATION_YES:
            booking_data = context.pending_data if isinstance(context.pending_data, dict) else {}
            hours_violation = self._validate_booking_window(booking_data, capabilities)
            if hours_violation is not None:
                return hours_violation

            booking_ref = f"BK-{uuid.uuid4().hex[:8].upper()}"
            service_name = self._first_non_empty(
                booking_data.get("service_name"),
                booking_data.get("restaurant_name"),
                "the service",
            )
            party = self._first_non_empty(
                booking_data.get("party_size"),
                booking_data.get("guest_count"),
                booking_data.get("guests"),
            )
            time = self._first_non_empty(
                booking_data.get("time"),
                booking_data.get("booking_time"),
            )
            detail_lines, detail_meta = self._build_booking_confirmation_detail_lines(
                booking_data=booking_data,
                context=context,
            )
            details_text = "\n".join(f"- {line}" for line in detail_lines)
            ticket_meta = await self._maybe_create_booking_ticket(
                context=context,
                capabilities=capabilities,
                booking_data=booking_data,
                booking_ref=booking_ref,
            )
            staff_followup_line = self._build_booking_staff_followup_line(
                booking_data=booking_data,
                capabilities=capabilities,
            )
            created_ticket_id = str(ticket_meta.get("ticket_id") or "").strip()
            ticket_reference_line = (
                f"Ticket Reference: #{created_ticket_id}\n" if created_ticket_id else ""
            )

            return HandlerResult(
                response_text=(
                    f"Your booking request has been received successfully.\n\n"
                    f"Booking Reference: {booking_ref}\n"
                    f"Details:\n{details_text}\n\n"
                    f"{staff_followup_line}\n"
                    f"{ticket_reference_line}"
                    "Is there anything else I can help you with?"
                ),
                next_state=ConversationState.COMPLETED,
                pending_action=None,
                pending_data={},
                suggested_actions=["View services", "Room service", "Need help"],
                metadata={
                    "booking_ref": booking_ref,
                    "booking_restaurant": service_name,
                    "booking_service": service_name,
                    "booking_party_size": party,
                    "booking_time": time,
                    "booking_date": booking_data.get("date", ""),
                    "booking_guest_name": detail_meta.get("guest_name", ""),
                    "booking_guest_phone": detail_meta.get("guest_phone", ""),
                    "booking_guest_email": detail_meta.get("guest_email", ""),
                    "booking_room_type": detail_meta.get("room_type", ""),
                    "booking_check_in": detail_meta.get("check_in", ""),
                    "booking_check_out": detail_meta.get("check_out", ""),
                    **ticket_meta,
                },
            )

        if intent_result.intent != IntentType.CONFIRMATION_NO:
            return HandlerResult(
                response_text="Please reply with 'Yes' to confirm this booking or 'No' to cancel.",
                next_state=ConversationState.AWAITING_CONFIRMATION,
                pending_action="confirm_booking",
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                suggested_actions=["Yes, confirm", "No, cancel"],
            )

        return HandlerResult(
            response_text=(
                "No problem, I've cancelled the booking request. "
                "Let me know if you'd like to book later or need anything else."
            ),
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
        )

    def _build_booking_staff_followup_line(
        self,
        *,
        booking_data: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> str:
        service_name = str(
            booking_data.get("service_name")
            or booking_data.get("restaurant_name")
            or ""
        ).strip()
        matched_service = self._find_service(service_name, capabilities) if service_name else None
        service_description = ""
        if isinstance(matched_service, dict):
            service_description = str(
                matched_service.get("description")
                or matched_service.get("service_description")
                or matched_service.get("details")
                or matched_service.get("service_context")
                or ""
            ).strip()
        policy_text = f"{service_name} {service_description}".lower()
        explicit_staff_markers = (
            "staff",
            "team",
            "human",
            "manual",
            "front desk",
            "concierge",
            "forward",
            "sent to",
            "assign",
        )
        if any(marker in policy_text for marker in explicit_staff_markers):
            return "I've forwarded this request to our staff team, and they will follow up shortly."
        return "Our team will follow up with you shortly."

    def _build_booking_confirmation_detail_lines(
        self,
        *,
        booking_data: dict[str, Any],
        context: ConversationContext,
    ) -> tuple[list[str], dict[str, str]]:
        pending = booking_data if isinstance(booking_data, dict) else {}
        integration = ticketing_service.get_integration_context(context)
        sub_category = self._infer_booking_sub_category(pending)

        service_name = self._first_non_empty(
            pending.get("service_name"),
            pending.get("restaurant_name"),
            pending.get("booking_service"),
        )
        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
        )
        guests = self._first_non_empty(
            pending.get("party_size"),
            pending.get("guest_count"),
            pending.get("guests"),
        )
        booking_time = self._first_non_empty(
            pending.get("time"),
            pending.get("booking_time"),
        )
        booking_date = self._first_non_empty(
            pending.get("date"),
            pending.get("booking_date"),
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
        stay_range = self._first_non_empty(
            pending.get("stay_date_range"),
        )
        guest_name = self._first_non_empty(
            pending.get("guest_name"),
            pending.get("name"),
            pending.get("full_name"),
            integration.get("guest_name"),
            context.guest_name,
        )
        guest_phone = self._first_non_empty(
            pending.get("guest_phone"),
            pending.get("phone"),
            pending.get("contact"),
            pending.get("contact_number"),
            pending.get("mobile"),
            integration.get("guest_phone"),
            integration.get("wa_number"),
            context.guest_phone,
        )
        guest_email = self._first_non_empty(
            pending.get("guest_email"),
            pending.get("email"),
            pending.get("contact_email"),
            integration.get("guest_email"),
            integration.get("email"),
        )
        if not guest_email:
            guest_email = self._extract_email_from_user_messages(context)
        if not guest_phone:
            guest_phone = self._extract_phone_from_user_messages(context)

        details: list[str] = []

        if sub_category == "room_booking":
            if room_type:
                details.append(f"Room Type: {room_type}")
            elif service_name:
                details.append(f"Room Type: {service_name}")
            if check_in:
                details.append(f"Check-in: {self._format_booking_date_for_display(check_in)}")
            if check_out:
                details.append(f"Check-out: {self._format_booking_date_for_display(check_out)}")
            if not check_in and not check_out and stay_range:
                details.append(f"Stay Dates: {stay_range}")
        else:
            if service_name:
                details.append(f"Service: {service_name}")
            if booking_date and booking_date.lower() not in {"today", "tonight"}:
                details.append(f"Date: {self._format_booking_date_for_display(booking_date)}")
            if booking_time:
                details.append(f"Time: {booking_time}")

        if guests:
            details.append(f"Guests: {guests}")
        if guest_name:
            details.append(f"Guest Name: {guest_name}")
        if guest_phone:
            details.append(f"Phone: {guest_phone}")
        if guest_email:
            details.append(f"Email: {guest_email}")

        if not details:
            fallback = service_name or "Booking request"
            details.append(f"Service: {fallback}")

        return details, {
            "guest_name": guest_name,
            "guest_phone": guest_phone,
            "guest_email": guest_email,
            "room_type": room_type,
            "check_in": check_in,
            "check_out": check_out,
        }

    @staticmethod
    def _extract_email_from_user_messages(context: ConversationContext) -> str:
        pattern = re.compile(r"\b([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})\b", flags=re.IGNORECASE)
        for msg in reversed(context.messages):
            if msg.role != MessageRole.USER:
                continue
            text = str(msg.content or "").strip()
            if not text:
                continue
            match = pattern.search(text)
            if match:
                return str(match.group(1) or "").strip()
        return ""

    @staticmethod
    def _extract_phone_from_user_messages(context: ConversationContext) -> str:
        for msg in reversed(context.messages):
            if msg.role != MessageRole.USER:
                continue
            text = str(msg.content or "").strip()
            if not text:
                continue
            for match in re.finditer(r"(?<!\d)\+?\d[\d\s\-\(\)]{6,20}\d(?!\d)", text):
                candidate = str(match.group(0) or "").strip()
                digits = re.sub(r"\D", "", candidate)
                if 7 <= len(digits) <= 15:
                    return f"+{digits}" if candidate.startswith("+") else digits
        return ""

    @staticmethod
    def _format_booking_date_for_display(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        iso_candidate = text.split("T", 1)[0]
        try:
            parsed = date.fromisoformat(iso_candidate)
        except ValueError:
            return text
        month = parsed.strftime("%B")
        return f"{month} {parsed.day}, {parsed.year}"

    async def _maybe_create_booking_ticket(
        self,
        *,
        context: ConversationContext,
        capabilities: dict[str, Any],
        booking_data: dict[str, Any],
        booking_ref: str,
    ) -> dict[str, Any]:
        """
        Lumira-style operational ticket for confirmed bookings.
        Non-blocking: booking success is never blocked by ticket API issues.
        """
        if not ticketing_service.is_ticketing_enabled(capabilities):
            return {"ticket_created": False, "ticket_skipped": True}

        configured_cases = ticketing_agent_service.get_configured_cases()
        if not configured_cases:
            return {
                "ticket_created": False,
                "ticket_skipped": True,
                "ticket_skip_reason": "no_configured_ticket_cases",
                "ticket_source": "booking_handler",
            }
        issue = self._build_booking_ticket_issue(
            booking_data=booking_data,
            booking_ref=booking_ref,
            sub_category=self._infer_booking_sub_category(booking_data),
        )
        matched_case = await ticketing_agent_service.match_configured_case_async(
            message=f"{issue} {str(booking_data.get('service_name') or '')}".strip(),
            conversation_excerpt=self._build_ticketing_case_context_text(
                context=context,
                latest_issue=issue,
            ),
            llm_response_text="",
        )
        if configured_cases and not matched_case:
            return {
                "ticket_created": False,
                "ticket_skipped": True,
                "ticket_skip_reason": "no_matching_configured_ticket_case",
                "ticket_source": "booking_handler",
            }

        sub_category = self._infer_booking_sub_category(
            booking_data,
            matched_case=matched_case,
        )
        if not sub_category:
            return {
                "ticket_created": False,
                "ticket_skipped": True,
                "ticket_skip_reason": "unknown_booking_sub_category",
                "ticket_source": "booking_handler",
            }
        issue = self._build_booking_ticket_issue(
            booking_data=booking_data,
            booking_ref=booking_ref,
            sub_category=sub_category,
        )
        integration = ticketing_service.get_integration_context(context)
        phase_value = self._first_non_empty(
            booking_data.get("phase"),
            context.pending_data.get("phase") if isinstance(context.pending_data, dict) else "",
            integration.get("phase"),
        )

        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category="request",
                sub_category=sub_category,
                priority="medium",
                phase=phase_value,
            )
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            return {
                "ticket_created": False,
                "ticket_create_error": str(exc),
                "ticket_source": "booking_handler",
            }

        if not create_result.success:
            return {
                "ticket_created": False,
                "ticket_create_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": "booking_handler",
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        return {
            "ticket_created": True,
            "ticket_id": str(create_result.ticket_id or "").strip(),
            "ticket_status": "open",
            "ticket_category": "request",
            "ticket_sub_category": sub_category,
            "ticket_priority": "medium",
            "ticket_summary": issue[:180],
            "ticket_source": "booking_handler",
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
        }

    @staticmethod
    def _first_non_empty(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _infer_booking_sub_category(self, booking_data: dict[str, Any], matched_case: str = "") -> str:
        pending = booking_data if isinstance(booking_data, dict) else {}
        explicit = self._first_non_empty(
            pending.get("sub_category"),
            pending.get("booking_sub_category"),
            pending.get("booking_type"),
        ).lower().replace(" ", "_")
        if explicit in {"room_booking", "spa_booking", "transport_booking"}:
            return explicit
        explicit_table = explicit == "table_booking"

        service_name = self._first_non_empty(
            pending.get("service_name"),
            pending.get("restaurant_name"),
            pending.get("booking_service"),
        ).lower()
        matched_case_lower = str(matched_case or "").strip().lower()
        primary_text = " ".join(
            [
                service_name,
                str(pending.get("room_type") or "").lower(),
                str(pending.get("check_in") or pending.get("stay_checkin_date") or "").lower(),
                str(pending.get("check_out") or pending.get("stay_checkout_date") or "").lower(),
            ]
        )

        if any(
            marker in primary_text
            for marker in ("room", "suite", "check in", "check-in", "checkin", "checkout", "check-out", "stay")
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
        if any(marker in primary_text for marker in ("table", "restaurant", "reservation", "book table", "dining")):
            return "table_booking"
        if any(
            marker in matched_case_lower
            for marker in (
                "room booking",
                "book room",
                "stay booking",
                "check in",
                "check-out",
            )
        ):
            return "room_booking"
        if any(
            marker in matched_case_lower
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
        if any(marker in matched_case_lower for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(marker in matched_case_lower for marker in ("table", "restaurant", "reservation", "book table", "dining")):
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

    def _build_booking_ticket_issue(
        self,
        *,
        booking_data: dict[str, Any],
        booking_ref: str,
        sub_category: str,
    ) -> str:
        pending = booking_data if isinstance(booking_data, dict) else {}
        service_name = self._first_non_empty(
            pending.get("service_name"),
            pending.get("restaurant_name"),
            pending.get("booking_service"),
        )
        time_value = self._first_non_empty(
            pending.get("time"),
            pending.get("booking_time"),
        )
        date_value = self._first_non_empty(
            pending.get("date"),
            pending.get("booking_date"),
        )
        party_value = self._first_non_empty(
            pending.get("party_size"),
            pending.get("guests"),
            pending.get("guest_count"),
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
            date_range = self._first_non_empty(
                pending.get("stay_date_range"),
                date_value,
            )
            issue_parts = [f"Room booking confirmed ({booking_ref}): {room_type}"]
            if check_in and check_out:
                issue_parts.append(f"from {check_in} to {check_out}")
            elif date_range:
                issue_parts.append(f"for {date_range}")
            if party_value:
                issue_parts.append(f"for {party_value} guests")
            return " ".join(issue_parts).strip()

        if sub_category == "spa_booking":
            treatment = service_name or "spa treatment"
            issue_parts = [f"Spa booking confirmed ({booking_ref}): {treatment}"]
            if time_value:
                issue_parts.append(f"at {time_value}")
            if date_value and date_value.lower() not in {"today", "tonight"}:
                issue_parts.append(f"on {date_value}")
            if party_value:
                issue_parts.append(f"for {party_value} guests")
            return " ".join(issue_parts).strip()

        if sub_category == "transport_booking":
            transport_service = service_name or "transport booking"
            issue_parts = [f"Transport request confirmed ({booking_ref}): {transport_service}"]
            if time_value:
                issue_parts.append(f"at {time_value}")
            if date_value and date_value.lower() not in {"today", "tonight"}:
                issue_parts.append(f"on {date_value}")
            if party_value:
                issue_parts.append(f"for {party_value} guests")
            return " ".join(issue_parts).strip()

        if sub_category == "table_booking":
            table_service = service_name or "table booking"
            issue_parts = [f"Table booking confirmed ({booking_ref}): {table_service}"]
            if party_value:
                issue_parts.append(f"for {party_value} guests")
            if time_value:
                issue_parts.append(f"at {time_value}")
            if date_value and date_value.lower() not in {"today", "tonight"}:
                issue_parts.append(f"on {date_value}")
            return " ".join(issue_parts).strip()

        generic_service = service_name or "service booking"
        issue_parts = [f"Booking confirmed ({booking_ref}): {generic_service}"]
        if party_value:
            issue_parts.append(f"for {party_value} guests")
        if time_value:
            issue_parts.append(f"at {time_value}")
        if date_value and date_value.lower() not in {"today", "tonight"}:
            issue_parts.append(f"on {date_value}")
        return " ".join(issue_parts).strip()

    def _validate_booking_window(
        self,
        booking_data: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> HandlerResult | None:
        """
        Validate requested booking time against selected service operating hours.
        """
        service_name = str(booking_data.get("service_name") or booking_data.get("restaurant_name") or "").strip()
        booking_time = str(booking_data.get("time") or "").strip()
        if not service_name or not booking_time:
            return None

        service = self._find_service(service_name, capabilities)
        if service is None:
            return None

        requested_minutes = self._to_minutes(booking_time)
        hours_text = str(service.get("hours") or "").strip()
        open_close = self._parse_hours_window(hours_text)
        if requested_minutes is None or open_close is None:
            return None
        opens_at, closes_at = open_close

        if self._is_within_window(requested_minutes, opens_at, closes_at):
            return None

        return HandlerResult(
            response_text=(
                f"{service_name} is open from {hours_text}. "
                f"The requested time ({booking_time}) is outside operating hours. "
                "Please share another preferred time."
            ),
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_booking_time",
            pending_data=booking_data,
            suggested_actions=["7 PM", "8 PM", "9 PM"],
        )

    def _find_service(
        self,
        name: str,
        capabilities: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find a service by name using exact, partial, then fuzzy matching."""
        service_catalog = capabilities.get("service_catalog", []) or []
        services = [
            row
            for row in service_catalog
            if isinstance(row, dict)
            and row.get("is_active", True)
            and self._is_bookable_service_entry(row)
        ]
        if not services:
            services = [
                row
                for row in capabilities.get("restaurants", [])
                if isinstance(row, dict)
                and row.get("is_active", True)
                and self._is_bookable_service_entry(row)
            ]
        needle = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
        if not needle:
            return None

        for service in services:
            service_name = re.sub(r"[^a-z0-9]+", " ", str(service.get("name") or "").lower()).strip()
            if not service_name:
                continue
            if needle in service_name or service_name in needle:
                return service

        best_service: dict[str, Any] | None = None
        best_score = 0.0
        for service in services:
            service_name = re.sub(r"[^a-z0-9]+", " ", str(service.get("name") or "").lower()).strip()
            if not service_name:
                continue
            score = SequenceMatcher(a=needle, b=service_name).ratio()
            if score > best_score:
                best_score = score
                best_service = service
        if best_service is not None and best_score >= 0.72:
            return best_service
        return None

    def _list_service_names(self, capabilities: dict[str, Any]) -> list[str]:
        """Return a list of active service names."""
        services = capabilities.get("service_catalog", []) or []
        names = [
            str(service.get("name") or "").strip()
            for service in services
            if isinstance(service, dict)
            and service.get("is_active", True)
            and self._is_bookable_service_entry(service)
        ]
        names = [name for name in names if name]
        if names:
            return names
        legacy = capabilities.get("restaurants", [])
        legacy_names = [
            str(r.get("name") or "").strip()
            for r in legacy
            if isinstance(r, dict)
            and r.get("is_active", True)
            and self._is_bookable_service_entry(r)
        ]
        legacy_names = [name for name in legacy_names if name]
        if legacy_names:
            return legacy_names

        capability_flags = capabilities.get("services", {}) if isinstance(capabilities, dict) else {}
        inferred = []
        if isinstance(capability_flags, dict):
            for capability_id, enabled in capability_flags.items():
                if capability_id.endswith("_hours") or not enabled:
                    continue
                label = str(capability_id).replace("_", " ").strip().title()
                if label and label not in inferred:
                    inferred.append(label)
        return inferred

    def _infer_service_from_recent_messages(
        self,
        context: ConversationContext,
        capabilities: dict[str, Any],
        current_message: str,
    ) -> str:
        """
        When awaiting service selection, recover service mention from
        previous user turn (for replies like "2am 5 guests").
        """
        current_compact = re.sub(r"\s+", " ", str(current_message or "").strip().lower())
        skipped_current = False
        for msg in reversed(context.messages):
            if msg.role != MessageRole.USER:
                continue
            text = str(msg.content or "").strip()
            if not text:
                continue
            text_compact = re.sub(r"\s+", " ", text.lower())
            if not skipped_current and text_compact == current_compact:
                skipped_current = True
                continue
            matched = self._find_service(text, capabilities)
            if matched is not None:
                return str(matched.get("name") or "").strip()
            hinted = self._extract_service_hint_from_text(text)
            if hinted:
                return hinted
        return ""

    @staticmethod
    def _extract_service_hint_from_text(message: str) -> str:
        """
        Extract likely outlet/service name from free-form booking text.
        """
        msg = str(message or "").strip().lower()
        if not msg:
            return ""

        match = re.search(r"\b(?:at|from|in)\s+([a-z][a-z0-9&\-\s]{1,40})\b", msg)
        if not match:
            return ""

        raw = match.group(1)
        raw = re.split(r"\b(?:for|at|on|to|by|with|from)\b", raw, maxsplit=1)[0].strip()
        raw = re.sub(r"\s+", " ", raw).strip()
        if not raw:
            return ""
        if re.fullmatch(r"[0-9:\samp]+", raw):
            return ""
        if raw in {"my room", "room", "table", "booking"}:
            return ""
        return raw

    @staticmethod
    def _normalize_service_label(value: str) -> str:
        normalized = re.sub(r"\s+", " ", str(value or "").strip())
        if not normalized:
            return ""
        return " ".join(part.capitalize() for part in normalized.split())

    @staticmethod
    def _build_ticketing_case_context_text(
        *,
        context: ConversationContext,
        latest_issue: str,
        max_messages: int = 0,
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
        if latest_issue:
            lines.append(f"Ticket Issue Draft: {latest_issue}")
        joined = "\n".join(lines).strip()
        limit = max(1200, int(max_chars or 6000))
        if len(joined) <= limit:
            return joined
        return joined[-limit:]

    @staticmethod
    def _has_service_catalog(capabilities: dict[str, Any]) -> bool:
        service_catalog = capabilities.get("service_catalog", []) if isinstance(capabilities, dict) else []
        if isinstance(service_catalog, list):
            if any(
                isinstance(service, dict)
                and service.get("is_active", True)
                and BookingHandler._is_bookable_service_entry(service)
                for service in service_catalog
            ):
                return True
        restaurants = capabilities.get("restaurants", []) if isinstance(capabilities, dict) else []
        if isinstance(restaurants, list):
            if any(
                isinstance(service, dict)
                and service.get("is_active", True)
                and BookingHandler._is_bookable_service_entry(service)
                for service in restaurants
            ):
                return True
        return False

    @staticmethod
    def _is_bookable_service_entry(service: dict[str, Any]) -> bool:
        service_type = str(service.get("type") or "").strip().lower()
        service_id = str(service.get("id") or "").strip().lower()
        service_name = str(service.get("name") or "").strip().lower()
        blocked_ids = {
            "ticketing_agent",
            "ticketing_plugin",
            "ticketing",
            "human_handoff",
            "live_chat",
            "callback",
            "email_followup",
        }
        blocked_types = {"plugin", "workflow", "handoff", "tool"}
        if service_id in blocked_ids:
            return False
        if service_type in blocked_types:
            return False
        if "ticketing" in service_name:
            return False
        return True

    @staticmethod
    def _extract_party_size(message: str) -> str:
        match = re.search(r"\b(?:for|party of)\s+(\d{1,2})\b", message.lower())
        if match:
            return match.group(1)

        fallback = re.search(r"\b(\d{1,2})\s*(?:people|guests|persons|pax)\b", message.lower())
        if fallback:
            return fallback.group(1)

        plain_numeric = re.fullmatch(r"\s*(\d{1,2})\s*", message or "")
        if plain_numeric:
            return plain_numeric.group(1)
        return ""

    @staticmethod
    def _extract_time(message: str) -> str:
        match = re.search(
            r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
            message.lower(),
        )
        if not match:
            return ""
        return match.group(1).strip()

    @staticmethod
    def _to_minutes(value: str) -> int | None:
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        match = re.search(r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?\b", raw)
        if not match:
            return None

        hour = int(match.group("h"))
        minute = int(match.group("m") or 0)
        ap = match.group("ap")
        if ap:
            hour = hour % 12
            if ap == "pm":
                hour += 12
        if hour > 23 or minute > 59:
            return None
        return (hour * 60) + minute

    def _parse_hours_window(self, hours_text: str) -> tuple[int, int] | None:
        if not hours_text:
            return None
        parts = [part.strip() for part in hours_text.split("-")]
        if len(parts) != 2:
            return None
        opens = self._to_minutes(parts[0])
        closes = self._to_minutes(parts[1])
        if opens is None or closes is None:
            return None
        return opens, closes

    @staticmethod
    def _is_within_window(requested: int, opens_at: int, closes_at: int) -> bool:
        if opens_at == closes_at:
            return True
        if opens_at < closes_at:
            return opens_at <= requested <= closes_at
        return requested >= opens_at or requested <= closes_at

    @staticmethod
    def _extract_date_hint(message: str) -> str:
        lower = message.lower()
        for token in ("today", "tomorrow", "tonight"):
            if token in lower:
                return token
        return ""
