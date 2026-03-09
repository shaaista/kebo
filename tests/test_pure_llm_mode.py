import pytest

from schemas.chat import (
    ChatRequest,
    ConversationContext,
    ConversationState,
    IntentType,
)
from services.chat_service import ChatService
from services.full_kb_llm_service import FullKBLLMResult
from services.ticketing_service import TicketingResult


@pytest.mark.asyncio
async def test_pure_llm_mode_uses_llm_pending_data_without_deterministic_overrides(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="pure-llm-mode-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="Proceeding with the room selection you requested.",
            normalized_query="book premier suite march 2-3 for 2",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="room_booking",
            confidence=0.91,
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_room_booking_details",
            pending_data={
                "service_id": "room_booking",
                "room_type": "Premier Suite",
                "stay_checkin_date": "Mar 02",
                "stay_checkout_date": "Mar 03",
            },
            room_number=None,
            suggested_actions=["Share guest count", "Show room options"],
            trace_id="pure-llm-mode-1",
            llm_output={"service_id": "room_booking", "requires_ticket": False},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_force_summary_refresh", False)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [],
            "restaurants": [],
            "services": {},
            "intents": [],
            "tools": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    response = await service.process_message(
        ChatRequest(
            session_id="pure-llm-mode-1",
            message="book premier suite march 2-3 for 2",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.AWAITING_INFO
    assert response.intent == IntentType.TABLE_BOOKING
    assert response.message == "Proceeding with the room selection you requested."
    assert response.metadata.get("pure_llm_mode") is True
    assert context.pending_action == "collect_room_booking_details"
    # Pure mode should preserve raw LLM slot value without deterministic normalization.
    assert str((context.pending_data or {}).get("room_type") or "") == "Premier Suite"


@pytest.mark.asyncio
async def test_pure_llm_ticket_creation_skips_when_service_ticketing_disabled(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="pure-llm-ticket-skip-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I have forwarded this request to our team.",
            normalized_query="please process room booking follow-up",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="room_booking",
            confidence=0.92,
            next_state=ConversationState.PROCESSING_ORDER,
            pending_action=None,
            pending_data={"service_id": "room_booking"},
            room_number=None,
            suggested_actions=["Ask another question"],
            trace_id="pure-llm-ticket-skip-1",
            llm_output={
                "service_id": "room_booking",
                "requires_ticket": True,
                "ticket_reason": "Staff follow-up needed for this booking request",
                "ticket_sub_category": "room_booking",
                "ticket_issue": "Room booking follow-up request",
            },
            clear_pending_data=False,
            status="success",
        )

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Ticket creation must be skipped when service ticketing is disabled")

    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_force_summary_refresh", False)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [
                {
                    "id": "room_booking",
                    "name": "Room Booking",
                    "phase_id": "pre_booking",
                    "is_active": True,
                    "ticketing_enabled": False,
                }
            ],
            "restaurants": [],
            "services": {},
            "intents": [],
            "tools": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _should_not_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="pure-llm-ticket-skip-1",
            message="please process room booking follow-up",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("ticket_create_skipped") is True
    assert response.metadata.get("ticket_create_skip_reason") == "phase_service_ticketing_disabled"
    assert response.metadata.get("ticket_created") is None


@pytest.mark.asyncio
async def test_pure_llm_ticket_creation_creates_ticket_when_enabled(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="pure-llm-ticket-create-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="Your booking request has been shared with our team.",
            normalized_query="confirm room booking follow-up",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="room_booking",
            confidence=0.95,
            next_state=ConversationState.PROCESSING_ORDER,
            pending_action=None,
            pending_data={"service_id": "room_booking"},
            room_number=None,
            suggested_actions=["Need help with anything else?"],
            trace_id="pure-llm-ticket-create-1",
            llm_output={
                "service_id": "room_booking",
                "requires_ticket": True,
                "ticket_reason": "Final booking confirmation needs staff action",
                "ticket_sub_category": "room_booking",
                "ticket_issue": "Room booking confirmation request",
                "ticket_priority": "HIGH",
                "ticket_category": "request",
            },
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_force_summary_refresh", False)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [
                {
                    "id": "room_booking",
                    "name": "Room Booking",
                    "phase_id": "pre_booking",
                    "is_active": True,
                    "ticketing_enabled": True,
                }
            ],
            "restaurants": [],
            "services": {},
            "intents": [],
            "tools": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    async def _fake_create_ticket(_payload):
        return TicketingResult(
            success=True,
            ticket_id="LOCAL-PURE-1",
            status_code=200,
            response={"ok": True},
        )

    monkeypatch.setattr(
        "services.chat_service.ticketing_service.create_ticket",
        _fake_create_ticket,
    )

    response = await service.process_message(
        ChatRequest(
            session_id="pure-llm-ticket-create-1",
            message="confirm room booking follow-up",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-PURE-1"
    assert response.metadata.get("ticket_sub_category") == "room_booking"
    assert response.metadata.get("ticket_service_id") == "room_booking"
