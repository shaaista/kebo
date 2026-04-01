"""
Complaint Handler

End-to-end complaint lifecycle with Lumira-style ticketing:
- collect details
- create ticket
- update ticket notes
- optional human handoff
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from config.settings import settings
from handlers.base_handler import BaseHandler, HandlerResult
from integrations.lumira_ticketing_repository import lumira_ticketing_repository
from schemas.chat import (
    ConversationContext,
    ConversationState,
    IntentResult,
    IntentType,
    MessageRole,
)
from services.config_service import config_service
from services.ticketing_llm_service import ticketing_llm_service
from services.ticketing_router_service import ticketing_router_service
from services.ticketing_service import ticketing_service

logger = logging.getLogger(__name__)


class ComplaintHandler(BaseHandler):
    """Handles complaints with external ticket creation/update + optional escalation."""

    _ALLOWED_SUB_CATEGORIES = (
        "amenities",
        "housekeeping",
        "laundry",
        "maintenance",
        "order_food",
        "table_booking",
        "room_booking",
        "billing",
        "transport",
        "spa",
        "room_service",
    )
    _PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s\-()]{6,}\d)")

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        msg = str(message or "").strip()
        msg_lower = msg.lower()

        if context.pending_action == "collect_ticket_room_number":
            return await self._handle_room_number_collection(
                msg,
                context,
                capabilities,
                db_session,
            )

        if context.pending_action == "collect_ticket_issue_details":
            return await self._start_ticket_creation_flow(
                msg,
                intent_result,
                context,
                capabilities,
                db_session,
            )

        if context.pending_action == "collect_ticket_identity_details":
            return await self._handle_ticket_identity_details(
                msg,
                context,
                capabilities,
                db_session,
            )

        if context.pending_action == "confirm_ticket_creation":
            return await self._handle_ticket_creation_confirmation(
                msg,
                intent_result,
                context,
                capabilities,
                db_session,
            )

        if context.pending_action == "collect_ticket_update_note":
            return await self._handle_ticket_update_note(
                msg,
                intent_result,
                context,
                capabilities,
            )

        if context.pending_action == "confirm_ticket_escalation":
            return await self._handle_ticket_escalation_confirmation(
                msg,
                intent_result,
                context,
                capabilities,
                db_session,
            )

        # Backward compatibility for old pending action values.
        if context.pending_action == "escalate_complaint":
            return self._handle_legacy_escalation_confirmation(intent_result)

        if self._is_ticket_status_request(msg_lower):
            return await self._handle_ticket_status_query(context, db_session)

        if self._is_ticket_update_request(msg_lower):
            return await self._start_ticket_update_flow(
                msg,
                intent_result,
                context,
                capabilities,
                db_session,
            )

        # ── Smart routing: direct contact-info requests ──────────────────────
        if self._is_human_contact_request(msg_lower):
            return self._handle_human_contact_request()

        # ── Empathy-first: emotional complaints ──────────────────────────────
        if self._is_emotional_complaint(msg_lower):
            return self._handle_emotional_complaint(msg, context)

        return await self._start_ticket_creation_flow(
            msg,
            intent_result,
            context,
            capabilities,
            db_session,
        )

    async def _start_ticket_creation_flow(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        entities = intent_result.entities if isinstance(intent_result.entities, dict) else {}
        issue = self._resolve_issue_text(
            issue=str(entities.get("issue") or message).strip(),
            message=message,
            context=context,
        )
        if len(issue) < 5:
            return HandlerResult(
                response_text=(
                    "I'd like to help with this. Could you share a little more detail "
                    "about what happened so I can make sure the right team assists you?"
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_issue_details",
                pending_data={"issue_hint": issue},
                suggested_actions=["Room AC not cooling", "Housekeeping delay", "Billing issue"],
            )

        category = self._detect_category(issue, entities)
        sub_category = await self._resolve_sub_category(
            issue=issue,
            message=message,
            context=context,
            entities=entities,
        )
        priority = self._detect_priority(issue, entities)
        room_number = context.room_number or self._extract_room_number(message)

        pending_data = {
            "issue": issue,
            "message": message,
            "category": category,
            "sub_category": sub_category,
            "priority": priority,
            "department_id": str(entities.get("department_id") or "").strip(),
            "department_head": str(entities.get("department_head") or "").strip(),
            "phase": str(entities.get("phase") or "in_stay").strip(),
            "sla_due_time": str(entities.get("sla_due_time") or entities.get("slaDue") or "").strip(),
            "outlet_id": str(entities.get("outlet_id") or "").strip(),
            "room_number": room_number or "",
        }
        guest_preferences = await self._resolve_guest_preferences(
            message=message,
            context=context,
            pending_data=pending_data,
        )
        if guest_preferences:
            pending_data["guest_preferences"] = guest_preferences

        if not ticketing_service.is_ticketing_enabled(capabilities):
            return self._fallback_without_ticketing()

        pending_data = await self._enrich_ticket_context(
            context=context,
            pending_data=pending_data,
            db_session=db_session,
            entities=entities,
        )

        route_result = await self._maybe_route_to_existing_ticket(
            message=message,
            context=context,
            pending_data=pending_data,
            db_session=db_session,
        )
        if route_result is not None:
            return route_result

        identity_gate = self._identity_gate_result_if_needed(context=context, pending=pending_data)
        if identity_gate is not None:
            return identity_gate

        room_number = str(pending_data.get("room_number") or "").strip()
        if not room_number and self._room_number_required_for_ticket(context=context, pending=pending_data):
            return HandlerResult(
                response_text=self._build_room_number_request_message(pending_data),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_room_number",
                pending_data=pending_data,
                suggested_actions=["cancel"],
            )

        if self._should_auto_create_ticket():
            return await self._create_ticket_from_pending(
                context=context,
                pending=pending_data,
                capabilities=capabilities,
                db_session=db_session,
                skip_existing_route=True,
            )

        return self._build_create_confirmation_result(pending_data)

    async def _handle_room_number_collection(
        self,
        message: str,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        room_number = self._extract_room_number(message)
        if not room_number:
            return HandlerResult(
                response_text=(
                    "That doesn't look like a valid room number. "
                    "Please share it in 2-10 letters/numbers (for example: 305 or A-12)."
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_room_number",
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                suggested_actions=["cancel"],
            )

        pending = dict(context.pending_data or {})
        pending["room_number"] = room_number
        identity_gate = self._identity_gate_result_if_needed(context=context, pending=pending)
        if identity_gate is not None:
            return identity_gate
        if self._should_auto_create_ticket():
            return await self._create_ticket_from_pending(
                context=context,
                pending=pending,
                capabilities=capabilities,
                db_session=db_session,
            )
        return self._build_create_confirmation_result(pending, room_number_override=room_number)

    async def _handle_ticket_identity_details(
        self,
        message: str,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        text = str(message or "").strip()
        if not text:
            return HandlerResult(
                response_text="Please share the requested details so I can continue.",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_identity_details",
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                suggested_actions=["Name + phone", "Phone only", "Cancel"],
            )
        if text.lower() in {"cancel", "stop", "no", "not now"}:
            return HandlerResult(
                response_text="Understood, I won't create a ticket right now.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Need help", "Talk to human", "Ask another question"],
            )

        pending = dict(context.pending_data or {})
        name, phone = self._extract_identity_details(text)
        if name and not str(pending.get("guest_name") or "").strip():
            pending["guest_name"] = name
        if phone and not str(pending.get("guest_phone") or "").strip():
            pending["guest_phone"] = phone

        missing_fields = self._missing_identity_fields(context=context, pending=pending)
        if missing_fields:
            return HandlerResult(
                response_text=self._build_identity_prompt(missing_fields),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_identity_details",
                pending_data=pending,
                suggested_actions=["Name: Alex, Phone: +1 555 123 4567", "Phone: +1 555 123 4567", "Cancel"],
            )

        integration = ticketing_service.get_integration_context(context)
        resolved_name = str(pending.get("guest_name") or integration.get("guest_name") or context.guest_name or "").strip()
        resolved_phone = str(
            pending.get("guest_phone")
            or integration.get("guest_phone")
            or integration.get("wa_number")
            or context.guest_phone
            or ""
        ).strip()

        if resolved_name:
            pending["guest_name"] = resolved_name
            context.guest_name = resolved_name
            integration["guest_name"] = resolved_name
        if resolved_phone:
            pending["guest_phone"] = resolved_phone
            context.guest_phone = resolved_phone
            integration["guest_phone"] = resolved_phone
        if isinstance(context.pending_data, dict):
            context.pending_data["_integration"] = integration

        if self._should_auto_create_ticket():
            return await self._create_ticket_from_pending(
                context=context,
                pending=pending,
                capabilities=capabilities,
                db_session=db_session,
            )
        return self._build_create_confirmation_result(pending)

    async def _handle_ticket_creation_confirmation(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        if self._is_no(intent_result, message):
            return HandlerResult(
                response_text=(
                    "No problem at all. "
                    "If you'd like, I can still connect you with someone from our team."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Speak with someone", "Need help", "Ask another question"],
            )

        is_yes = self._is_yes(intent_result, message)
        if is_yes and self._should_stale_reconfirm(context):
            pending = context.pending_data if isinstance(context.pending_data, dict) else {}
            return HandlerResult(
                response_text=self._build_stale_reconfirm_prompt(pending, flow_label="request"),
                next_state=ConversationState.AWAITING_CONFIRMATION,
                pending_action="confirm_ticket_creation",
                pending_data=pending,
                suggested_actions=["Yes, go ahead", "No, cancel"],
                metadata={"ticket_stale_reconfirm": True},
            )

        if not is_yes:
            if self._looks_like_new_issue_message(message):
                # User started describing a new issue instead of confirming old ticket.
                # Restart ticket-intake flow with latest message.
                return await self._start_ticket_creation_flow(
                    message,
                    intent_result,
                    context,
                    capabilities,
                    db_session,
                )
            return HandlerResult(
                response_text="Would you like me to go ahead and raise this with our team? Just say Yes or No.",
                next_state=ConversationState.AWAITING_CONFIRMATION,
                pending_action="confirm_ticket_creation",
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                suggested_actions=["Yes, go ahead", "No, cancel"],
            )

        if not ticketing_service.is_ticketing_enabled(capabilities):
            return self._fallback_without_ticketing()

        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        return await self._create_ticket_from_pending(
            context=context,
            pending=pending,
            capabilities=capabilities,
            db_session=db_session,
        )

    async def _handle_ticket_escalation_confirmation(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        if self._is_no(intent_result, message):
            return HandlerResult(
                response_text=(
                    "Understood. Our team has been notified and will follow up shortly. "
                    "You can ask me anything else in the meantime."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Ask another question", "Check status", "Need help"],
            )

        is_yes = self._is_yes(intent_result, message)
        if is_yes and self._should_stale_reconfirm(context):
            pending = context.pending_data if isinstance(context.pending_data, dict) else {}
            return HandlerResult(
                response_text=self._build_stale_reconfirm_prompt(
                    pending,
                    flow_label="human handoff",
                ),
                next_state=ConversationState.AWAITING_CONFIRMATION,
                pending_action="confirm_ticket_escalation",
                pending_data=pending,
                suggested_actions=["Yes, connect me", "No, continue with bot"],
                metadata={"ticket_stale_reconfirm": True},
            )

        if not is_yes:
            return HandlerResult(
                response_text="Please reply 'Yes' to connect a human agent, or 'No' to continue with the bot.",
                next_state=ConversationState.AWAITING_CONFIRMATION,
                pending_action="confirm_ticket_escalation",
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                suggested_actions=["Yes, connect me", "No, continue with bot"],
            )

        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        ticket_id = str(pending.get("ticket_id") or "").strip()
        issue = str(pending.get("issue") or "Guest requested human assistance").strip()

        if not ticketing_service.is_handoff_enabled(capabilities):
            return HandlerResult(
                response_text=(
                    "I've made sure this is flagged as urgent. "
                    "Someone from our team will reach out to you shortly."
                ),
                next_state=ConversationState.ESCALATED,
                pending_action=None,
                pending_data={},
                suggested_actions=["Return to bot"],
                metadata={
                    "escalated": True,
                    "escalation_reason": "guest_requested_after_ticket",
                    "ticket_id": ticket_id,
                    "handoff_skipped": True,
                },
            )

        integration = ticketing_service.get_integration_context(context)
        conversation_id = str(
            integration.get("conversation_id")
            or context.session_id
            or ""
        ).strip()
        agent_id = str(
            pending.get("agent_id")
            or integration.get("agent_id")
            or ""
        ).strip()
        if not agent_id and db_session is not None:
            agent_id = await lumira_ticketing_repository.fetch_agent_for_handover(
                db_session,
                department_id=(
                    pending.get("ticket_department_id")
                    or pending.get("department_id")
                    or ""
                ),
                entity_id=integration.get("entity_id") or integration.get("organisation_id"),
                group_id=integration.get("group_id"),
            )

        handoff_result = await ticketing_service.handoff_to_agent(
            conversation_id=conversation_id,
            session_id=context.session_id,
            reason=issue,
            agent_id=agent_id,
        )

        if handoff_result.success:
            return HandlerResult(
                response_text=(
                    "You're now connected with our team. "
                    "Someone will be with you shortly."
                ),
                next_state=ConversationState.ESCALATED,
                pending_action=None,
                pending_data={},
                suggested_actions=["Return to bot"],
                metadata={
                    "escalated": True,
                    "escalation_reason": "guest_requested_after_ticket",
                    "ticket_id": ticket_id,
                    "handoff_completed": True,
                    "handoff_response": handoff_result.response,
                },
            )

        return HandlerResult(
            response_text=(
                "I wasn't able to connect you right now, but I've made sure this is marked as a priority. "
                "Our team will get back to you shortly."
            ),
            next_state=ConversationState.ESCALATED,
            pending_action=None,
            pending_data={},
            suggested_actions=["Return to bot"],
            metadata={
                "escalated": True,
                "escalation_reason": "handoff_api_error",
                "ticket_id": ticket_id,
                "handoff_completed": False,
                "handoff_error": handoff_result.error,
            },
        )

    async def _start_ticket_update_flow(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult:
        if not ticketing_service.is_ticketing_enabled(capabilities):
            return self._fallback_without_ticketing()

        latest_ticket = ticketing_service.get_latest_ticket(context)
        ticket_id = str(latest_ticket.get("id") or "").strip()
        if not ticket_id and db_session is not None:
            integration = ticketing_service.get_integration_context(context)
            candidates = await lumira_ticketing_repository.fetch_candidate_tickets(
                db_session,
                guest_id=integration.get("guest_id"),
                room_number=(
                    context.room_number
                    or integration.get("room_number")
                    or ""
                ),
            )
            if candidates:
                ticket_id = str(candidates[0].get("id") or "").strip()

        if not ticket_id:
            return HandlerResult(
                response_text=(
                    "I don't see any open requests from this conversation yet. "
                    "Would you like me to raise a new one for you?"
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Yes, raise it", "Speak with someone", "Cancel"],
            )

        window_check = self._check_ticket_update_window(
            context=context,
            latest_ticket=latest_ticket,
            ticket_id=ticket_id,
        )
        note = self._extract_update_note(message)
        if not window_check["within_window"]:
            if note:
                return await self._handle_expired_ticket_update_note(
                    ticket_id=ticket_id,
                    note=note,
                    context=context,
                    capabilities=capabilities,
                    db_session=db_session,
                    window_check=window_check,
                )
            window_minutes = int(window_check.get("window_minutes") or 0)
            return HandlerResult(
                response_text=(
                    f"Ticket {ticket_id} can be updated only within {window_minutes} minutes of creation. "
                    "If this is now urgent or needs human escalation, share what changed and I will raise "
                    "a priority follow-up ticket."
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_update_note",
                pending_data={
                    "ticket_id": ticket_id,
                    "update_window_expired": True,
                    "ticket_update_window_minutes": window_minutes,
                    "ticket_created_at": str(window_check.get("created_at_raw") or "").strip(),
                },
                suggested_actions=[
                    "Still unresolved and urgent",
                    "Safety concern, need human now",
                    "No urgent update",
                ],
                metadata={
                    "ticket_update_window_enforced": True,
                    "ticket_update_window_expired": True,
                    "ticket_id": ticket_id,
                },
            )

        if not note:
            return HandlerResult(
                response_text=f"Sure. What update should I add to ticket {ticket_id}?",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_update_note",
                pending_data={"ticket_id": ticket_id},
                suggested_actions=["Issue still unresolved", "Please prioritize this", "Need immediate support"],
            )

        return await self._execute_ticket_update(ticket_id, note)

    async def _handle_ticket_update_note(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
    ) -> HandlerResult:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        if not pending.get("ticket_id"):
            return HandlerResult(
                response_text=(
                    "I couldn't find which ticket to update in this conversation. "
                    "Please ask for ticket status first or share your issue to create a new ticket."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Ticket status", "Create ticket", "Talk to human"],
            )

        if self._is_no(intent_result, message):
            return HandlerResult(
                response_text="No problem. I won't add any update to the ticket right now.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Ticket status", "Talk to human", "Ask another question"],
            )

        note = str(message or "").strip()
        if len(note) < 4:
            return HandlerResult(
                response_text="Please share a little more detail for the ticket update.",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_ticket_update_note",
                pending_data=pending,
                suggested_actions=["Issue still unresolved", "Please escalate", "Need urgent callback"],
            )

        ticket_id = str(pending.get("ticket_id") or "").strip()
        latest_ticket = ticketing_service.get_latest_ticket(context)
        window_check = self._check_ticket_update_window(
            context=context,
            latest_ticket=latest_ticket,
            ticket_id=ticket_id,
        )
        if bool(pending.get("update_window_expired")) or not bool(window_check.get("within_window")):
            return await self._handle_expired_ticket_update_note(
                ticket_id=ticket_id,
                note=note,
                context=context,
                capabilities=capabilities,
                window_check={
                    "window_minutes": pending.get("ticket_update_window_minutes")
                    or window_check.get("window_minutes"),
                    "created_at_raw": pending.get("ticket_created_at")
                    or window_check.get("created_at_raw"),
                    "within_window": False,
                },
            )
        return await self._execute_ticket_update(ticket_id, note)

    async def _execute_ticket_update(self, ticket_id: str, note: str) -> HandlerResult:
        update_result = await ticketing_service.update_ticket(ticket_id=ticket_id, manager_notes=note)
        if update_result.success:
            return HandlerResult(
                response_text=(
                    f"Update added successfully to ticket {ticket_id}. "
                    "Our team will review it shortly."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Ticket status", "Talk to human", "Need help"],
                metadata={
                    "ticket_updated": True,
                    "ticket_update": True,
                    "ticket_id": ticket_id,
                    "ticket_status": "open",
                    "ticket_assigned_id": str(update_result.assigned_id or "").strip(),
                    "ticket_summary": note[:180],
                    "ticket_source": "complaint_handler",
                    "ticket_update_response": update_result.response,
                },
            )

        return HandlerResult(
            response_text=(
                f"I couldn't update ticket {ticket_id} right now. "
                "Please try again in a moment, or ask me to connect a human agent."
            ),
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["Talk to human", "Try ticket update again", "Need help"],
            metadata={
                "ticket_update_failed": True,
                "ticket_id": ticket_id,
                "ticket_error": update_result.error,
            },
        )

    def _check_ticket_update_window(
        self,
        *,
        context: ConversationContext,
        latest_ticket: dict[str, Any],
        ticket_id: str,
    ) -> dict[str, Any]:
        window_minutes = int(getattr(settings, "ticketing_update_window_minutes", 2) or 0)
        if window_minutes <= 0:
            return {
                "within_window": True,
                "window_minutes": 0,
                "created_at_raw": "",
                "created_at_utc": "",
            }

        created_at_dt, created_at_raw = self._resolve_ticket_created_at(
            context=context,
            latest_ticket=latest_ticket,
            ticket_id=ticket_id,
        )
        if created_at_dt is None:
            # Unknown creation time should not hard-block updates.
            return {
                "within_window": True,
                "window_minutes": window_minutes,
                "created_at_raw": str(created_at_raw or "").strip(),
                "created_at_utc": "",
            }

        deadline = created_at_dt + timedelta(minutes=window_minutes)
        within_window = datetime.now(UTC) <= deadline
        return {
            "within_window": within_window,
            "window_minutes": window_minutes,
            "created_at_raw": str(created_at_raw or "").strip(),
            "created_at_utc": created_at_dt.isoformat(),
        }

    def _resolve_ticket_created_at(
        self,
        *,
        context: ConversationContext,
        latest_ticket: dict[str, Any],
        ticket_id: str,
    ) -> tuple[datetime | None, str]:
        candidates: list[tuple[datetime, str]] = []
        ticket = latest_ticket if isinstance(latest_ticket, dict) else {}
        for key in ("created_at", "createdAt", "createdDate", "created_date"):
            raw = ticket.get(key)
            parsed = self._parse_ticket_timestamp(raw)
            if parsed is not None:
                candidates.append((parsed, str(raw)))

        memory_root = context.pending_data if isinstance(context.pending_data, dict) else {}
        memory = memory_root.get("_memory", {})
        facts = memory.get("facts", {}) if isinstance(memory, dict) else {}
        history = facts.get("ticket_history", []) if isinstance(facts, dict) else []
        if isinstance(history, list):
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(
                    entry.get("id")
                    or entry.get("ticket_id")
                    or entry.get("ticketId")
                    or ""
                ).strip()
                if ticket_id and entry_id and entry_id != ticket_id:
                    continue
                for key in ("created_at", "createdAt", "createdDate", "updated_at", "updatedAt"):
                    raw = entry.get(key)
                    parsed = self._parse_ticket_timestamp(raw)
                    if parsed is not None:
                        candidates.append((parsed, str(raw)))

        if not candidates:
            return None, ""
        earliest_dt, earliest_raw = min(candidates, key=lambda item: item[0])
        return earliest_dt, earliest_raw

    @staticmethod
    def _parse_ticket_timestamp(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)

        if isinstance(value, (int, float)):
            stamp = float(value)
            if stamp > 1_000_000_000_000:
                stamp = stamp / 1000.0
            try:
                return datetime.fromtimestamp(stamp, tz=UTC)
            except Exception:
                return None

        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            try:
                stamp = float(text)
                if stamp > 1_000_000_000_000:
                    stamp = stamp / 1000.0
                return datetime.fromtimestamp(stamp, tz=UTC)
            except Exception:
                return None

        iso_candidate = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except Exception:
            pass

        for fmt in ("%H:%M:%S %d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except Exception:
                continue
        return None

    async def _handle_expired_ticket_update_note(
        self,
        *,
        ticket_id: str,
        note: str,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Any = None,
        window_check: dict[str, Any] | None = None,
    ) -> HandlerResult:
        decision = await ticketing_llm_service.assess_expired_ticket_update_followup(
            note=note,
            conversation=self._build_conversation_text(context),
            ticket_id=ticket_id,
        )
        should_create_followup = bool(decision.get("create_new_ticket"))
        if not should_create_followup and str(decision.get("source") or "").strip().lower() == "heuristic":
            fallback_priority = self._detect_priority(note, {})
            if fallback_priority in {"high", "critical"}:
                should_create_followup = True
                decision = dict(decision or {})
                decision["create_new_ticket"] = True
                decision["priority"] = fallback_priority
                decision["reason"] = "priority_based_fallback"

        if not should_create_followup:
            window_minutes = int(
                (window_check or {}).get("window_minutes")
                or getattr(settings, "ticketing_update_window_minutes", 2)
                or 2
            )
            return HandlerResult(
                response_text=(
                    f"I can no longer update ticket {ticket_id} because the {window_minutes}-minute update window has passed. "
                    "If this has become urgent or needs immediate human support, tell me what changed and I can raise a new priority ticket."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=[
                    "Need urgent human support",
                    "Create new complaint ticket",
                    "Ticket status",
                ],
                metadata={
                    "ticket_update_window_enforced": True,
                    "ticket_update_window_expired": True,
                    "ticket_update_outside_window": True,
                    "ticket_followup_created": False,
                    "ticket_followup_decision_source": str(decision.get("source") or ""),
                    "ticket_followup_decision_reason": str(decision.get("reason") or ""),
                },
            )

        pending = await self._build_expired_update_followup_pending(
            ticket_id=ticket_id,
            note=note,
            decision=decision,
            context=context,
            db_session=db_session,
        )

        if self._should_auto_create_ticket():
            result = await self._create_ticket_from_pending(
                context=context,
                pending=pending,
                capabilities=capabilities,
                db_session=db_session,
                skip_existing_route=True,
            )
            result.metadata = dict(result.metadata or {})
            result.metadata["ticket_update_window_expired"] = True
            result.metadata["ticket_followup_created_from_expired_update"] = True
            result.metadata["ticket_followup_decision_source"] = str(decision.get("source") or "")
            result.metadata["ticket_followup_decision_reason"] = str(decision.get("reason") or "")
            return result

        result = self._build_create_confirmation_result(pending)
        result.metadata = dict(result.metadata or {})
        result.metadata["ticket_update_window_expired"] = True
        result.metadata["ticket_followup_pending_confirmation"] = True
        result.metadata["ticket_followup_decision_source"] = str(decision.get("source") or "")
        result.metadata["ticket_followup_decision_reason"] = str(decision.get("reason") or "")
        return result

    async def _build_expired_update_followup_pending(
        self,
        *,
        ticket_id: str,
        note: str,
        decision: dict[str, Any],
        context: ConversationContext,
        db_session: Any = None,
    ) -> dict[str, Any]:
        integration = ticketing_service.get_integration_context(context)
        latest_ticket = ticketing_service.get_latest_ticket(context)
        issue = f"Urgent follow-up after ticket {ticket_id}: {note}"
        fallback_sub_category = self._detect_sub_category(note, {})
        sub_category = await self._resolve_sub_category(
            issue=issue,
            message=note,
            context=context,
            entities={},
        )
        if not sub_category:
            sub_category = fallback_sub_category
        priority = str(decision.get("priority") or self._detect_priority(note, {})).strip().lower()
        if priority not in {"low", "medium", "high", "critical"}:
            priority = "high"
        if priority in {"low", "medium"}:
            priority = "high"

        phase = str(
            integration.get("phase")
            or (context.pending_data or {}).get("phase")
            or "in_stay"
        ).strip()
        room_number = str(
            context.room_number
            or latest_ticket.get("room_number")
            or integration.get("room_number")
            or self._extract_room_number(note)
            or ""
        ).strip()
        pending: dict[str, Any] = {
            "issue": issue,
            "message": note,
            "category": "complaint",
            "sub_category": sub_category,
            "priority": priority,
            "department_id": "",
            "department_head": "",
            "phase": phase,
            "sla_due_time": "",
            "outlet_id": "",
            "room_number": room_number,
            "ticket_source": str(integration.get("ticket_source") or "").strip(),
            "related_ticket_id": ticket_id,
        }
        pending = await self._enrich_ticket_context(
            context=context,
            pending_data=pending,
            db_session=db_session,
            entities={},
        )
        guest_preferences = await self._resolve_guest_preferences(
            message=note,
            context=context,
            pending_data=pending,
        )
        if guest_preferences:
            pending["guest_preferences"] = guest_preferences
        return pending

    async def _handle_ticket_status_query(
        self,
        context: ConversationContext,
        db_session: Any = None,
    ) -> HandlerResult:
        latest_ticket = ticketing_service.get_latest_ticket(context)
        ticket_id = str(latest_ticket.get("id") or "").strip()
        if not ticket_id and db_session is not None:
            integration = ticketing_service.get_integration_context(context)
            candidates = await lumira_ticketing_repository.fetch_candidate_tickets(
                db_session,
                guest_id=integration.get("guest_id"),
                room_number=(
                    context.room_number
                    or integration.get("room_number")
                    or ""
                ),
            )
            if candidates:
                latest_ticket = dict(candidates[0])
                ticket_id = str(latest_ticket.get("id") or "").strip()
        if not ticket_id:
            return HandlerResult(
                response_text=(
                    "I don't see a ticket in this chat yet. "
                    "If you share the issue, I can create one now."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Create ticket", "Talk to human"],
            )

        status = str(latest_ticket.get("status") or "open").strip().lower() or "open"
        priority = str(latest_ticket.get("priority") or "medium").strip().lower() or "medium"
        return HandlerResult(
            response_text=(
                f"Latest ticket status:\n\n"
                f"Ticket ID: {ticket_id}\n"
                f"Status: {status.title()}\n"
                f"Priority: {priority.title()}\n\n"
                "If you want, I can add an update note to this ticket."
            ),
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["Add update note", "Talk to human", "Ask another question"],
        )

    async def _enrich_ticket_context(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any],
        db_session: Any = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        enriched = dict(pending_data or {})
        entities = entities or {}
        integration = ticketing_service.get_integration_context(context)

        # Carry integration metadata into pending payload for future confirmation step.
        for key in (
            "group_id",
            "message_id",
            "ticket_source",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost",
        ):
            if enriched.get(key) in (None, "") and integration.get(key) not in (None, ""):
                enriched[key] = integration.get(key)

        if not str(enriched.get("phase") or "").strip():
            enriched["phase"] = str(integration.get("phase") or "in_stay").strip()

        if not bool(getattr(settings, "ticketing_enrichment_enabled", True)) or db_session is None:
            return enriched

        entity_id = integration.get("entity_id") or integration.get("organisation_id")
        if entity_id in (None, ""):
            return enriched

        ri_org_id = await lumira_ticketing_repository.fetch_ri_entity_id_from_mapping(
            db_session,
            fms_entity_id=entity_id,
        )
        if ri_org_id:
            integration["organisation_id"] = ri_org_id
            if isinstance(context.pending_data, dict):
                context.pending_data["_integration"] = integration

        departments = await lumira_ticketing_repository.fetch_departments_of_entity(
            db_session,
            entity_id=entity_id,
        )
        outlets = await lumira_ticketing_repository.fetch_outlets_of_entity(
            db_session,
            entity_id=entity_id,
        )

        outlet_match = self._resolve_outlet_from_context(enriched, outlets)
        if outlet_match and not str(enriched.get("outlet_id") or "").strip():
            enriched["outlet_id"] = str(outlet_match.get("outlet_id") or "").strip()

        department_match = self._resolve_department_from_context(
            pending_data=enriched,
            entities=entities,
            departments=departments,
        )
        if department_match:
            if not str(enriched.get("department_id") or "").strip():
                enriched["department_id"] = str(department_match.get("department_id") or "").strip()
            if not str(enriched.get("department_head") or "").strip():
                department_head = (
                    str(department_match.get("department_head") or "").strip()
                    or str(department_match.get("department_name") or "").strip()
                )
                if department_head:
                    enriched["department_head"] = department_head
            if (
                not str(enriched.get("department_head_phone") or "").strip()
                and str(department_match.get("department_head_phone") or "").strip()
            ):
                enriched["department_head_phone"] = str(
                    department_match.get("department_head_phone") or ""
                ).strip()

            if not str(integration.get("agent_id") or "").strip():
                inferred_agent = str(department_match.get("agent_id") or "").strip()
                if inferred_agent:
                    integration["agent_id"] = inferred_agent
                    if isinstance(context.pending_data, dict):
                        context.pending_data["_integration"] = integration

        if not str(enriched.get("room_number") or "").strip():
            enriched["room_number"] = str(
                context.room_number
                or integration.get("room_number")
                or ""
            ).strip()

        if db_session is not None:
            enriched = await self._hydrate_guest_details_from_db(
                context=context,
                pending_data=enriched,
                db_session=db_session,
            )

        return enriched

    async def _hydrate_guest_details_from_db(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any],
        db_session: Any,
    ) -> dict[str, Any]:
        """
        Hydrate guest details (room_number/guest_name/guest_id) from FF guest info
        so ticket intake is more natural and avoids unnecessary room-number prompts.
        """
        enriched = dict(pending_data or {})
        integration = ticketing_service.get_integration_context(context)

        # If room is already known, no need to fetch.
        if str(enriched.get("room_number") or "").strip():
            return enriched

        entity_id = integration.get("entity_id") or integration.get("organisation_id")
        guest_id = integration.get("guest_id")
        guest_phone = (
            integration.get("guest_phone")
            or context.guest_phone
            or ""
        )

        profile = await lumira_ticketing_repository.fetch_guest_profile(
            db_session,
            entity_id=entity_id,
            guest_id=guest_id,
            guest_phone=str(guest_phone or "").strip(),
        )
        if not profile:
            return enriched

        room_number = str(profile.get("room_number") or "").strip()
        guest_name = str(profile.get("guest_name") or "").strip()
        resolved_guest_id = str(profile.get("guest_id") or "").strip()
        resolved_entity_id = str(profile.get("entity_id") or "").strip()

        if room_number:
            enriched["room_number"] = room_number
            if not context.room_number:
                context.room_number = room_number
            integration["room_number"] = room_number
        if guest_name:
            if not context.guest_name:
                context.guest_name = guest_name
            integration.setdefault("guest_name", guest_name)
            enriched.setdefault("guest_name", guest_name)
        if resolved_guest_id:
            integration["guest_id"] = resolved_guest_id
        if resolved_entity_id and not str(integration.get("entity_id") or "").strip():
            integration["entity_id"] = resolved_entity_id

        if isinstance(context.pending_data, dict):
            context.pending_data["_integration"] = integration
        return enriched

    async def _maybe_route_to_existing_ticket(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any],
        db_session: Any = None,
    ) -> HandlerResult | None:
        if not bool(getattr(settings, "ticketing_smart_routing_enabled", True)):
            return None
        if db_session is None:
            return None

        integration = ticketing_service.get_integration_context(context)
        candidates = await lumira_ticketing_repository.fetch_candidate_tickets(
            db_session,
            guest_id=integration.get("guest_id"),
            room_number=(
                str(pending_data.get("room_number") or "").strip()
                or context.room_number
                or integration.get("room_number")
                or ""
            ),
        )
        if not candidates:
            return None

        conversation = self._build_conversation_text(context)
        decision = await ticketing_router_service.decide(
            conversation=conversation,
            latest_user_message=message,
            candidates=candidates,
        )
        if decision.decision == "acknowledge":
            return HandlerResult(
                response_text=decision.response or "We are already working on your earlier ticket.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Ticket status", "Talk to human", "Need help"],
                metadata={
                    "ticket_route_decision": "acknowledge",
                    "ticket_route_source": decision.source,
                    "ticket_candidates_checked": len(candidates),
                },
            )

        if decision.decision == "update" and decision.update_ticket_id:
            update_note = decision.manager_notes or str(message or "").strip()
            update_result = await self._execute_ticket_update(decision.update_ticket_id, update_note)
            update_result.metadata["ticket_route_decision"] = "update"
            update_result.metadata["ticket_route_source"] = decision.source
            update_result.metadata["ticket_candidates_checked"] = len(candidates)
            if update_result.metadata.get("ticket_updated") and decision.response:
                update_result.response_text = decision.response
            return update_result

        return None

    @staticmethod
    def _build_conversation_text(context: ConversationContext, max_messages: int = 12) -> str:
        lines: list[str] = []
        for msg in context.get_recent_messages(max_messages):
            role = "User" if msg.role == MessageRole.USER else "AI"
            content = str(msg.content or "").strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _resolve_outlet_from_context(
        pending_data: dict[str, Any],
        outlets: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not outlets:
            return None
        text = " ".join(
            [
                str(pending_data.get("issue") or ""),
                str(pending_data.get("message") or ""),
                str(pending_data.get("sub_category") or ""),
            ]
        ).lower()
        if not text:
            return None
        for outlet in sorted(
            outlets,
            key=lambda item: len(str(item.get("outlet_name") or "")),
            reverse=True,
        ):
            name = str(outlet.get("outlet_name") or "").strip().lower()
            if name and name in text:
                return outlet
        return None

    def _resolve_department_from_context(
        self,
        *,
        pending_data: dict[str, Any],
        entities: dict[str, Any],
        departments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not departments:
            return None

        explicit_id = str(
            pending_data.get("department_id")
            or entities.get("department_id")
            or ""
        ).strip()
        if explicit_id:
            for dept in departments:
                if str(dept.get("department_id") or "").strip() == explicit_id:
                    return dept

        text = " ".join(
            [
                str(pending_data.get("issue") or ""),
                str(pending_data.get("message") or ""),
                str(pending_data.get("category") or ""),
                str(pending_data.get("sub_category") or ""),
            ]
        ).lower()
        category = str(pending_data.get("category") or "").strip().lower()

        # Direct department-name mention match.
        for dept in sorted(
            departments,
            key=lambda item: len(str(item.get("department_name") or "")),
            reverse=True,
        ):
            dept_name = str(dept.get("department_name") or "").strip().lower()
            if dept_name and dept_name in text:
                return dept

        keyword_groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
            (("housekeeping", "cleaning", "towel", "linen", "bed"), ("housekeeping",)),
            (("ac", "air conditioner", "maintenance", "plumbing", "electrical", "repair"), ("maintenance", "engineering")),
            (("food", "meal", "room service", "breakfast", "dinner", "restaurant"), ("room service", "f&b", "food", "dining", "ird")),
            (("billing", "bill", "invoice", "payment", "refund"), ("front desk", "finance", "accounts", "guest relations")),
            (("spa", "wellness", "massage"), ("spa", "wellness")),
            (("taxi", "airport", "pickup", "drop", "transport"), ("transport", "concierge", "travel")),
            (("security", "unsafe", "emergency"), ("security",)),
        ]
        if category == "complaint":
            keyword_groups.append((("complaint", "issue"), ("guest relations", "front desk")))

        for issue_keywords, dept_keywords in keyword_groups:
            if not any(token in text for token in issue_keywords):
                continue
            for dept in departments:
                dept_name = str(dept.get("department_name") or "").strip().lower()
                if any(token in dept_name for token in dept_keywords):
                    return dept

        return departments[0]

    def _build_create_confirmation_result(
        self,
        pending_data: dict[str, Any],
        room_number_override: str = "",
    ) -> HandlerResult:
        room_number = room_number_override or str(pending_data.get("room_number") or "").strip()
        issue = str(pending_data.get("issue") or "").strip()
        category = str(pending_data.get("category") or "complaint").strip()
        priority = str(pending_data.get("priority") or "medium").strip()
        guest_name = str(pending_data.get("guest_name") or "").strip()
        guest_phone = str(pending_data.get("guest_phone") or "").strip()
        details_lines = [
            f"Issue: {issue}",
            f"Category: {category.title()}",
            f"Priority: {priority.title()}",
        ]
        if room_number:
            details_lines.append(f"Room: {room_number}")
        if guest_name:
            details_lines.append(f"Guest: {guest_name}")
        if guest_phone:
            details_lines.append(f"Phone: {guest_phone}")
        details_text = "\n".join(details_lines)

        return HandlerResult(
            response_text=(
                "I have the following details to share with our team:\n\n"
                f"{details_text}\n\n"
                "Shall I go ahead and raise this?"
            ),
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_ticket_creation",
            pending_data=pending_data,
            suggested_actions=["Yes, go ahead", "No, cancel"],
            metadata={"room_number": room_number} if room_number else {},
        )

    def _should_auto_create_ticket(self) -> bool:
        return bool(getattr(settings, "ticketing_auto_create_on_actionable", True))

    def _build_room_number_request_message(self, pending_data: dict[str, Any]) -> str:
        """
        Ask for room number in a service-first tone (no explicit ticketing jargon).
        """
        category = str(pending_data.get("category") or "").strip().lower()
        sub_category = str(pending_data.get("sub_category") or "").strip().lower()
        request_like_sub_categories = {
            "amenities",
            "housekeeping",
            "laundry",
            "maintenance",
            "room_service",
            "order_food",
            "table_booking",
            "room_booking",
            "transport",
            "billing",
        }
        if category == "request" or sub_category in request_like_sub_categories:
            return (
                "Certainly. Please share your room number so I can route this to the right team immediately."
            )
        return (
            "I'm sorry this happened. Could you share your room number so I can get this to the right team right away?"
        )

    @staticmethod
    def _team_label_for_sub_category(sub_category: str) -> str:
        normalized = str(sub_category or "").strip().lower()
        mapping = {
            "amenities": "our housekeeping team",
            "housekeeping": "our housekeeping team",
            "laundry": "our housekeeping team",
            "maintenance": "our engineering team",
            "order_food": "our in-room dining team",
            "table_booking": "our restaurant team",
            "room_booking": "our front desk team",
            "billing": "our front desk team",
            "transport": "our concierge team",
            "spa": "our spa team",
        }
        return mapping.get(normalized, "our team")

    def _build_post_ticket_message(
        self,
        *,
        pending: dict[str, Any],
        room_number: str,
    ) -> str:
        """
        Human-facing acknowledgement after backend ticket creation.
        """
        category = str(pending.get("category") or "complaint").strip().lower()
        sub_category = str(pending.get("sub_category") or "").strip().lower()
        team_label = self._team_label_for_sub_category(sub_category)
        issue_text = str(pending.get("issue") or pending.get("message") or "").strip().lower()
        if any(marker in issue_text for marker in ("cockroach", "roach", "pest", "bed bug", "insect")):
            team_label = "our housekeeping team"
        room_phrase = f" for room {room_number}" if room_number else ""

        if category == "request" or sub_category in {
            "amenities",
            "housekeeping",
            "laundry",
            "maintenance",
            "order_food",
            "table_booking",
            "room_booking",
            "transport",
            "billing",
        }:
            return (
                f"Certainly. I've informed {team_label}{room_phrase}, and they will assist you shortly."
            )

        return (
            f"I'm sorry for the inconvenience. I've raised this with {team_label}{room_phrase}, and they will address it shortly."
        )

    async def _create_ticket_from_pending(
        self,
        *,
        context: ConversationContext,
        pending: dict[str, Any],
        capabilities: dict[str, Any],
        db_session: Any = None,
        skip_existing_route: bool = False,
    ) -> HandlerResult:
        if not ticketing_service.is_ticketing_enabled(capabilities):
            return self._fallback_without_ticketing()

        issue = str(pending.get("issue") or "").strip()
        if not issue:
            issue = "Guest complaint raised via chatbot"
        integration = ticketing_service.get_integration_context(context)
        resolved_guest_name = str(
            pending.get("guest_name")
            or integration.get("guest_name")
            or context.guest_name
            or ""
        ).strip()
        resolved_guest_phone = self._normalize_phone(
            str(
                pending.get("guest_phone")
                or integration.get("guest_phone")
                or integration.get("wa_number")
                or context.guest_phone
                or ""
            ).strip()
        )
        if resolved_guest_name:
            context.guest_name = resolved_guest_name
            integration["guest_name"] = resolved_guest_name
            pending["guest_name"] = resolved_guest_name
        if resolved_guest_phone:
            context.guest_phone = resolved_guest_phone
            integration["guest_phone"] = resolved_guest_phone
            pending["guest_phone"] = resolved_guest_phone
        if isinstance(context.pending_data, dict):
            context.pending_data["_integration"] = integration
        identity_gate = self._identity_gate_result_if_needed(context=context, pending=pending)
        if identity_gate is not None:
            return identity_gate

        if not skip_existing_route:
            route_result = await self._maybe_route_to_existing_ticket(
                message=str(pending.get("message") or issue),
                context=context,
                pending_data=pending,
                db_session=db_session,
            )
            if route_result is not None:
                return route_result

        payload = ticketing_service.build_lumira_ticket_payload(
            context=context,
            issue=issue,
            message=str(pending.get("message") or issue),
            category=str(pending.get("category") or "complaint"),
            sub_category=str(pending.get("sub_category") or ""),
            priority=str(pending.get("priority") or "medium"),
            department_id=str(pending.get("department_id") or ""),
            department_manager=str(pending.get("department_head") or ""),
            phase=str(pending.get("phase") or "in_stay"),
            sla_due_time=str(pending.get("sla_due_time") or ""),
            outlet_id=str(pending.get("outlet_id") or ""),
            source=str(
                pending.get("ticket_source")
                or integration.get("ticket_source")
                or ""
            ),
            manager_notes=self._build_ticket_manager_notes(pending),
            group_id=pending.get("group_id") or integration.get("group_id"),
            message_id=pending.get("message_id") or integration.get("message_id"),
            input_tokens=pending.get("input_tokens"),
            output_tokens=pending.get("output_tokens"),
            total_tokens=pending.get("total_tokens"),
            cost=pending.get("cost"),
        )
        if pending.get("room_number"):
            payload["room_number"] = str(pending["room_number"]).strip()
        guest_name = str(
            pending.get("guest_name")
            or integration.get("guest_name")
            or context.guest_name
            or ""
        ).strip()
        if guest_name:
            payload["guest_name"] = guest_name
        guest_phone = resolved_guest_phone or str(
            pending.get("guest_phone")
            or integration.get("guest_phone")
            or integration.get("wa_number")
            or context.guest_phone
            or ""
        ).strip()
        if guest_phone:
            payload["guest_phone"] = guest_phone

        create_result = await ticketing_service.create_ticket(payload)
        if not create_result.success:
            logger.warning("Ticket create failed: %s", create_result.error)
            return HandlerResult(
                response_text=(
                    "I wasn't able to process this right now, but I can connect you with our team directly."
                ),
                next_state=ConversationState.ESCALATED,
                pending_action=None,
                pending_data={},
                suggested_actions=["Return to bot"],
                metadata={
                    "ticket_create_failed": True,
                    "ticket_error": create_result.error,
                    "escalated": True,
                    "escalation_reason": "ticket_api_error",
                },
            )

        ticket_id = str(create_result.ticket_id or "").strip() or "N/A"
        room_number = str(payload.get("room_number") or "").strip()
        success_message = self._build_post_ticket_message(
            pending=pending,
            room_number=room_number,
        )

        return HandlerResult(
            response_text=success_message,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["Need help", "Check status", "Ask another question"],
            metadata={
                "ticket_created": True,
                "ticket_id": ticket_id,
                "ticket_status": "open",
                "ticket_category": str(pending.get("category") or "complaint"),
                "ticket_sub_category": str(pending.get("sub_category") or ""),
                "ticket_priority": str(pending.get("priority") or "medium"),
                "ticket_department_id": str(pending.get("department_id") or ""),
                "ticket_assigned_id": str(create_result.assigned_id or "").strip(),
                "ticket_source": "complaint_handler",
                "room_number": room_number,
                "ticket_summary": issue[:180],
                "guest_name": guest_name,
                "guest_phone": guest_phone,
                "guest_preferences": list(pending.get("guest_preferences") or []),
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            },
        )

    def _resolve_issue_text(
        self,
        *,
        issue: str,
        message: str,
        context: ConversationContext,
    ) -> str:
        normalized_issue = str(issue or "").strip()
        if not normalized_issue:
            return ""

        lowered = normalized_issue.lower()
        generic_patterns = (
            "it's not there",
            "its not there",
            "not there in my room",
            "not in my room",
            "this is not there",
            "that is not there",
        )
        if not any(pattern in lowered for pattern in generic_patterns):
            return normalized_issue

        previous_user_text = self._get_previous_user_message(context, current_message=message)
        if not previous_user_text:
            return normalized_issue

        if "room" in lowered:
            return f"{previous_user_text} not available in room"
        return f"{previous_user_text} - {normalized_issue}"

    @staticmethod
    def _get_previous_user_message(
        context: ConversationContext,
        *,
        current_message: str,
    ) -> str:
        current = str(current_message or "").strip().lower()
        skipped_current = False
        for msg in reversed(context.messages):
            if msg.role != MessageRole.USER:
                continue
            text = str(msg.content or "").strip()
            if not text:
                continue
            if not skipped_current and text.lower() == current:
                skipped_current = True
                continue
            if len(text) >= 2:
                return text
        return ""

    async def _resolve_sub_category(
        self,
        *,
        issue: str,
        message: str,
        context: ConversationContext,
        entities: dict[str, Any],
    ) -> str:
        explicit = str(entities.get("sub_category") or entities.get("sub_categorization") or "").strip().lower()
        if explicit:
            return explicit
        fallback = self._detect_sub_category(issue, entities)
        return await ticketing_llm_service.classify_sub_category(
            issue=issue,
            latest_user_message=message,
            conversation=self._build_conversation_text(context),
            fallback_sub_category=fallback,
            allowed_sub_categories=list(self._ALLOWED_SUB_CATEGORIES),
        )

    async def _resolve_guest_preferences(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any],
    ) -> list[str]:
        existing: list[str] = []
        existing.extend(list(pending_data.get("guest_preferences") or []))

        pending_root = context.pending_data if isinstance(context.pending_data, dict) else {}
        memory = pending_root.get("_memory", {})
        if isinstance(memory, dict):
            facts = memory.get("facts", {})
            if isinstance(facts, dict):
                existing.extend(list(facts.get("guest_preferences") or []))

        extracted = await ticketing_llm_service.extract_guest_preferences(
            latest_user_message=message,
            conversation=self._build_conversation_text(context),
        )
        return self._normalize_guest_preferences(existing + extracted)

    @staticmethod
    def _normalize_guest_preferences(values: list[Any]) -> list[str]:
        deduped: list[str] = []
        for value in values:
            text = str(value or "").strip().lower()
            if not text:
                continue
            text = re.sub(r"[^a-z0-9 /-]+", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 3:
                continue
            if text in deduped:
                continue
            deduped.append(text[:80])
            if len(deduped) >= 8:
                break
        return deduped

    def _should_stale_reconfirm(self, context: ConversationContext) -> bool:
        if not bool(getattr(settings, "ticketing_stale_reconfirm_enabled", False)):
            return False
        threshold_minutes = int(getattr(settings, "ticketing_stale_reconfirm_minutes", 30) or 30)
        threshold_minutes = max(1, threshold_minutes)
        if len(context.messages) < 2:
            return False
        latest = context.messages[-1]
        previous = context.messages[-2]
        try:
            gap_seconds = (latest.timestamp - previous.timestamp).total_seconds()
        except Exception:
            return False
        return gap_seconds >= float(threshold_minutes * 60)

    def _build_stale_reconfirm_prompt(self, pending: dict[str, Any], flow_label: str) -> str:
        issue = str(pending.get("issue") or pending.get("message") or "your earlier request").strip()
        issue_preview = issue[:160]
        room = str(pending.get("room_number") or "").strip()
        room_text = f" for room {room}" if room else ""
        return (
            f"Before I continue with {flow_label}, please reconfirm this request: "
            f"\"{issue_preview}\"{room_text}. Reply 'Yes' to proceed or 'No' to cancel."
        )

    def _identity_gate_result_if_needed(
        self,
        *,
        context: ConversationContext,
        pending: dict[str, Any],
    ) -> HandlerResult | None:
        if not bool(getattr(settings, "ticketing_identity_gate_enabled", False)):
            return None
        if bool(getattr(settings, "ticketing_identity_gate_prebooking_only", True)) and not self._is_prebooking_ticket(
            context=context,
            pending=pending,
        ):
            return None

        missing_fields = self._missing_identity_fields(context=context, pending=pending)
        if not missing_fields:
            return None

        pending_next = dict(pending or {})
        pending_next["_identity_required_fields"] = missing_fields
        return HandlerResult(
            response_text=self._build_identity_prompt(missing_fields),
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_ticket_identity_details",
            pending_data=pending_next,
            suggested_actions=["Name: Alex, Phone: +1 555 123 4567", "Phone: +1 555 123 4567", "Cancel"],
            metadata={"ticket_identity_required": True},
        )

    def _room_number_required_for_ticket(
        self,
        *,
        context: ConversationContext,
        pending: dict[str, Any],
    ) -> bool:
        if self._is_prebooking_ticket(context=context, pending=pending):
            return False
        return True

    def _is_prebooking_ticket(
        self,
        *,
        context: ConversationContext,
        pending: dict[str, Any],
    ) -> bool:
        integration = ticketing_service.get_integration_context(context)
        phase = str(pending.get("phase") or integration.get("phase") or "").strip().lower().replace(" ", "_")
        if phase in {"pre_booking", "booking", "pre_checkin"}:
            return True

        source = str(
            pending.get("ticket_source")
            or integration.get("ticket_source")
            or integration.get("source")
            or ""
        ).strip().lower().replace("-", "_")
        if source in {"booking_bot", "engage", "booking"}:
            return True

        flow = str(integration.get("flow") or integration.get("bot_mode") or "").strip().lower().replace("-", "_")
        return flow in {"engage", "booking", "booking_bot", "prebooking", "pre_booking"}

    def _missing_identity_fields(
        self,
        *,
        context: ConversationContext,
        pending: dict[str, Any],
    ) -> list[str]:
        integration = ticketing_service.get_integration_context(context)
        missing: list[str] = []

        if bool(getattr(settings, "ticketing_identity_require_name", True)):
            guest_name = str(
                pending.get("guest_name")
                or integration.get("guest_name")
                or context.guest_name
                or ""
            ).strip()
            if not guest_name:
                missing.append("name")

        if bool(getattr(settings, "ticketing_identity_require_phone", True)):
            raw_phone = str(
                pending.get("guest_phone")
                or integration.get("guest_phone")
                or integration.get("wa_number")
                or context.guest_phone
                or ""
            ).strip()
            if not self._normalize_phone(raw_phone):
                missing.append("phone")

        return missing

    @staticmethod
    def _build_identity_prompt(missing_fields: list[str]) -> str:
        missing = set(missing_fields or [])
        if {"name", "phone"} <= missing:
            return (
                "Before I raise this ticket, please share guest name and contact phone number "
                "(for example: Name: Alex, Phone: +1 555 123 4567)."
            )
        if "name" in missing:
            return "Before I raise this ticket, please share guest name."
        return "Before I raise this ticket, please share a contact phone number."

    def _extract_identity_details(self, text: str) -> tuple[str, str]:
        message = str(text or "").strip()
        if not message:
            return "", ""
        phone = self._normalize_phone(message)
        name = self._extract_identity_name(message, phone)
        return name, phone

    def _normalize_phone(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        match = self._PHONE_PATTERN.search(raw)
        if not match:
            return ""
        candidate = match.group(0)
        cleaned = re.sub(r"[^\d+]", "", candidate)
        if cleaned.startswith("00"):
            cleaned = f"+{cleaned[2:]}"
        has_plus = cleaned.startswith("+")
        digits = cleaned[1:] if has_plus else cleaned
        if not digits.isdigit() or not (7 <= len(digits) <= 15):
            return ""
        return f"+{digits}" if has_plus else digits

    @staticmethod
    def _extract_identity_name(message: str, phone: str = "") -> str:
        text = str(message or "").strip()
        if not text:
            return ""
        sanitized = text
        if phone:
            sanitized = sanitized.replace(phone, " ")
        sanitized = re.sub(r"\b(?:phone|mobile|contact|number)\b[:\s-]*", " ", sanitized, flags=re.IGNORECASE)

        explicit_patterns = (
            r"\bmy\s+name\s+is\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bname\s+is\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bi\s+am\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bthis\s+is\s+([A-Za-z][A-Za-z .'-]{1,60})",
        )
        for pattern in explicit_patterns:
            match = re.search(pattern, sanitized, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ,.-")
            if candidate:
                return candidate[:60]

        first_segment = sanitized.split(",", 1)[0]
        tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", first_segment)
        if not tokens:
            return ""
        lowered = {token.lower() for token in tokens}
        blocked = {"yes", "no", "cancel", "ticket", "create", "raise", "please", "thanks", "thank"}
        if lowered & blocked and len(tokens) <= 2:
            return ""
        if len(tokens) >= 2:
            return " ".join(tokens[:3])[:60]
        if len(tokens[0]) >= 3:
            return tokens[0][:60]
        return ""

    def _build_ticket_manager_notes(self, pending: dict[str, Any]) -> str:
        notes = str(pending.get("manager_notes") or "").strip()
        preferences = self._normalize_guest_preferences(list(pending.get("guest_preferences") or []))
        if preferences:
            preference_note = "Guest preferences: " + "; ".join(preferences[:5])
            notes = f"{notes}\n{preference_note}".strip() if notes else preference_note
        return notes[:600]

    def _fallback_without_ticketing(self) -> HandlerResult:
        escalation_message = config_service.get_escalation_config().get(
            "escalation_message",
            "Let me connect you with our team for better assistance.",
        )
        return HandlerResult(
            response_text=(
                "I've noted your concern. "
                f"{escalation_message}"
            ),
            next_state=ConversationState.ESCALATED,
            pending_action=None,
            pending_data={},
            suggested_actions=["Return to bot"],
            metadata={
                "escalated": True,
                "escalation_reason": "ticketing_disabled",
            },
        )

    def _handle_legacy_escalation_confirmation(self, intent_result: IntentResult) -> HandlerResult:
        if intent_result.intent == IntentType.CONFIRMATION_YES:
            return HandlerResult(
                response_text=(
                    "I'm connecting you with a manager now. "
                    "Someone from our team will be with you shortly."
                ),
                next_state=ConversationState.ESCALATED,
                pending_action=None,
                pending_data={},
                suggested_actions=["Return to bot"],
                metadata={"escalated": True, "escalation_reason": "guest_complaint"},
            )

        return HandlerResult(
            response_text="Understood. I have recorded your feedback.",
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["Need help", "Ask another question"],
        )

    # ── Smart routing helpers ────────────────────────────────────────────────

    @staticmethod
    def _is_human_contact_request(msg_lower: str) -> bool:
        """Detect when the user just wants to talk to a human / get contact info."""
        patterns = (
            "talk to staff", "speak to staff", "talk to someone", "speak to someone",
            "speak with someone", "talk to a person", "speak to a person",
            "talk to a human", "speak to a human", "connect me with",
            "want to talk to", "want to speak to", "can i call",
            "give me the number", "phone number please", "contact number",
            "website is down", "website not working", "site is down",
            "call the hotel", "call your hotel", "call reception",
            "i want to call", "how can i reach", "how do i contact",
        )
        return any(p in msg_lower for p in patterns)

    def _handle_human_contact_request(self) -> HandlerResult:
        """Provide hotel contact details directly instead of creating a ticket."""
        contact = config_service.get_escalation_config()
        phone = str(contact.get("phone") or contact.get("escalation_phone") or "").strip()
        email = str(contact.get("email") or contact.get("escalation_email") or "").strip()

        contact_lines = []
        if phone:
            contact_lines.append(f"- Phone: {phone}")
        if email:
            contact_lines.append(f"- Email: {email}")

        if contact_lines:
            contact_text = "\n".join(contact_lines)
            response = (
                "Of course! You can reach our team directly:\n"
                f"{contact_text}\n\n"
                "They'll be happy to help you. Is there anything else I can assist with?"
            )
        else:
            response = (
                "I'd be happy to connect you with our team. "
                "Let me have someone reach out to you shortly. "
                "Is there anything else I can help with in the meantime?"
            )
        return HandlerResult(
            response_text=response,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["Ask another question", "Need help"],
        )

    @staticmethod
    def _is_emotional_complaint(msg_lower: str) -> bool:
        """Detect emotionally charged complaints that need empathy before action."""
        patterns = (
            "rude", "disrespectful", "unprofessional", "yelled", "shouted",
            "insulted", "worst experience", "terrible experience", "horrible experience",
            "disgusting", "never coming back", "never visit again",
            "worst hotel", "worst staff", "so rude", "very rude",
            "treated badly", "treated poorly", "mistreated",
            "discriminat", "harass", "threatened",
        )
        return any(p in msg_lower for p in patterns)

    def _handle_emotional_complaint(self, message: str, context: ConversationContext) -> HandlerResult:
        """Lead with empathy for emotional complaints, then offer follow-up."""
        return HandlerResult(
            response_text=(
                "I'm truly sorry to hear about your experience. "
                "That is absolutely not the standard we hold ourselves to, and I understand how frustrating this must be.\n\n"
                "Would you like me to have our manager follow up with you personally about this?"
            ),
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_ticket_creation",
            pending_data={
                "issue": message,
                "message": message,
                "category": "complaint",
                "sub_category": "",
                "priority": "high",
                "room_number": context.room_number or "",
            },
            suggested_actions=["Yes, please", "No, that's okay"],
        )

    @staticmethod
    def _extract_room_number(message: str) -> str:
        if not message:
            return ""
        direct = str(message).strip()
        if re.fullmatch(r"[A-Za-z0-9-]{2,10}", direct) and any(ch.isdigit() for ch in direct):
            return direct.upper()

        match = re.search(
            r"\b(?:room(?:\s*(?:number|no\.?))?\s*(?:is|=|:)?\s*)?([A-Za-z0-9-]{2,10})\b",
            message,
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

    @staticmethod
    def _detect_category(issue: str, entities: dict[str, Any]) -> str:
        explicit = str(entities.get("category") or entities.get("categorization") or "").strip().lower()
        if explicit:
            return explicit

        text = str(issue or "").lower()
        if any(marker in text for marker in ("broken", "not working", "dirty", "noise", "delay", "bad")):
            return "complaint"
        if any(marker in text for marker in ("need", "request", "please arrange", "please send")):
            return "request"
        return "complaint"

    @staticmethod
    def _detect_priority(issue: str, entities: dict[str, Any]) -> str:
        explicit = str(entities.get("priority") or "").strip().lower()
        if explicit in {"low", "medium", "high", "critical"}:
            return explicit

        text = str(issue or "").lower()
        if any(
            marker in text
            for marker in (
                "fire",
                "smoke",
                "medical emergency",
                "bleeding",
                "unsafe",
                "security threat",
            )
        ):
            return "critical"
        if any(marker in text for marker in ("urgent", "asap", "immediately", "critical", "emergency")):
            return "high"
        if any(marker in text for marker in ("cockroach", "roach", "pest", "bed bug", "insect infestation")):
            return "high"
        if any(marker in text for marker in ("whenever", "not urgent", "later")):
            return "low"
        return "medium"

    @staticmethod
    def _detect_sub_category(issue: str, entities: dict[str, Any]) -> str:
        explicit = str(
            entities.get("sub_category")
            or entities.get("sub_categorization")
            or ""
        ).strip().lower()
        if explicit:
            return explicit

        text = " ".join(
            [
                str(issue or ""),
                str(entities.get("service_name") or entities.get("service") or ""),
                str(entities.get("restaurant_name") or entities.get("restaurant") or ""),
            ]
        ).lower()
        if not text:
            return ""

        mapping: list[tuple[tuple[str, ...], str]] = [
            (("table", "reservation", "book table"), "table_booking"),
            (("room booking", "book room", "stay booking"), "room_booking"),
            (("food", "order", "meal", "menu", "dining", "in-room dining"), "order_food"),
            (("cockroach", "roach", "pest", "bed bug", "insect"), "housekeeping"),
            (("housekeeping", "cleaning", "clean room"), "housekeeping"),
            (("towel", "blanket", "pillow", "amenities", "toiletries", "hairdryer"), "amenities"),
            (("laundry", "iron", "dry clean"), "laundry"),
            (("ac", "air conditioner", "repair", "broken", "not working", "maintenance", "leak"), "maintenance"),
            (("billing", "invoice", "refund", "wrong charge", "payment"), "billing"),
            (("taxi", "airport", "pickup", "drop", "transport"), "transport"),
            (("spa", "massage", "wellness"), "spa"),
        ]
        for keywords, sub_category in mapping:
            if any(keyword in text for keyword in keywords):
                return sub_category
        return ""

    @staticmethod
    def _is_yes(intent_result: IntentResult, message: str) -> bool:
        if intent_result.intent == IntentType.CONFIRMATION_YES:
            return True
        msg = str(message or "").strip().lower()
        return msg in {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "confirm"}

    @staticmethod
    def _is_no(intent_result: IntentResult, message: str) -> bool:
        if intent_result.intent == IntentType.CONFIRMATION_NO:
            return True
        msg = str(message or "").strip().lower()
        return msg in {"no", "n", "nope", "cancel", "stop", "not now"}

    @staticmethod
    def _is_ticket_status_request(msg_lower: str) -> bool:
        return any(
            marker in msg_lower
            for marker in (
                "ticket status",
                "status of ticket",
                "status update",
                "check ticket",
                "my ticket",
            )
        )

    @staticmethod
    def _is_ticket_update_request(msg_lower: str) -> bool:
        return any(
            marker in msg_lower
            for marker in (
                "update ticket",
                "add note",
                "ticket update",
                "append note",
                "update my complaint",
            )
        )

    @staticmethod
    def _extract_update_note(message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return ""

        lowered = text.lower()
        prefixes = (
            "update ticket",
            "add note",
            "ticket update",
            "append note",
            "update my complaint",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                remainder = text[len(prefix) :].strip(" :.-")
                return remainder
        return text

    @staticmethod
    def _looks_like_new_issue_message(message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        lowered = text.lower()
        if lowered in {"yes", "no", "cancel", "stop", "ok", "okay"}:
            return False
        if lowered in {"hi", "hello", "hey", "thanks", "thank you"}:
            return False
        if len(text.split()) < 3:
            return False
        return True
