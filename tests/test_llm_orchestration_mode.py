import pytest

from handlers.base_handler import HandlerResult
from schemas.chat import ChatRequest, ConversationContext, ConversationState, IntentType
from schemas.orchestration import OrchestrationDecision, TicketDecision
from services.chat_service import ChatService
from services.ticketing_service import TicketingResult


@pytest.fixture(autouse=True)
def _disable_no_template_mode(monkeypatch):
    monkeypatch.setattr("services.chat_service.settings.chat_no_template_response_mode", False)


@pytest.mark.asyncio
async def test_orchestration_mode_out_of_phase_overrides_response(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="orch-out-of-phase-1",
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

    async def _fake_orchestrate(**kwargs):
        return OrchestrationDecision(
            normalized_query="i need food",
            intent="order_food",
            confidence=0.92,
            action="collect_info",
            target_service_id="in_room_dining_support",
            response_text="Sure, I can help with that.",
            pending_action="collect_order_items",
            pending_data_updates={"service_id": "in_room_dining_support"},
            missing_fields=["item_name"],
            use_handler=False,
            ticket=TicketDecision(required=False),
        )

    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_dispatch_handlers", False)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "hotel_name": "ICONIQA Demo Hotel",
            "business_type": "hotel",
            "service_catalog": [
                {
                    "id": "in_room_dining_support",
                    "name": "In Room Dining",
                    "type": "service",
                    "phase_id": "during_stay",
                    "is_active": True,
                    "ticketing_enabled": True,
                },
                {
                    "id": "room_discovery",
                    "name": "Room Discovery",
                    "type": "service",
                    "phase_id": "pre_booking",
                    "is_active": True,
                    "ticketing_enabled": False,
                },
            ],
            "journey_phases": [
                {"id": "pre_booking", "name": "Pre Booking", "is_active": True, "order": 1},
                {"id": "during_stay", "name": "During Stay", "is_active": True, "order": 2},
            ],
            "services": {},
            "intents": [],
            "tools": [],
            "faq_bank": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.llm_orchestration_service.orchestrate_turn", _fake_orchestrate)

    response = await service.process_message(
        ChatRequest(
            session_id="orch-out-of-phase-1",
            message="i need food",
            hotel_code="DEFAULT",
        )
    )

    lowered = str(response.message or "").lower()
    assert ("during stay" in lowered) or ("during your stay" in lowered) or ("after you check in" in lowered)
    assert (
        ("room discovery" in lowered)
        or ("pre booking" in lowered)
        or ("for now" in lowered)
        or ("right now" in lowered)
    )
    assert response.metadata.get("orchestration_policy_out_of_phase") is True
    assert response.metadata.get("response_source") == "llm_orchestration"


@pytest.mark.asyncio
async def test_orchestration_mode_creates_ticket_when_allowed(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="orch-ticket-create-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "during_stay"}, "room_number": "405"},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_orchestrate(**kwargs):
        return OrchestrationDecision(
            normalized_query="ac not working in room 405",
            intent="complaint",
            confidence=0.93,
            action="create_ticket",
            target_service_id="maintenance_support",
            response_text="I have logged this issue and shared it with our team.",
            pending_action=None,
            pending_data_updates={"service_id": "maintenance_support", "room_number": "405"},
            missing_fields=[],
            use_handler=False,
            ticket=TicketDecision(
                required=True,
                ready_to_create=True,
                reason="Maintenance intervention required",
                issue="AC not cooling in room 405",
                category="complaint",
                sub_category="maintenance",
                priority="high",
            ),
        )

    async def _fake_create_ticket(_payload):
        return TicketingResult(
            success=True,
            ticket_id="LOCAL-ORCH-1",
            status_code=200,
            response={"ok": True},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_dispatch_handlers", False)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "hotel_name": "ICONIQA Demo Hotel",
            "business_type": "hotel",
            "service_catalog": [
                {
                    "id": "maintenance_support",
                    "name": "Maintenance Support",
                    "type": "service",
                    "phase_id": "during_stay",
                    "is_active": True,
                    "ticketing_enabled": True,
                }
            ],
            "journey_phases": [
                {"id": "during_stay", "name": "During Stay", "is_active": True, "order": 1},
            ],
            "services": {},
            "intents": [],
            "tools": [{"id": "ticketing", "type": "workflow", "enabled": True}],
            "faq_bank": [],
        },
    )
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _cap=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _fake_create_ticket)
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.llm_orchestration_service.orchestrate_turn", _fake_orchestrate)

    response = await service.process_message(
        ChatRequest(
            session_id="orch-ticket-create-1",
            message="ac not working in room 405",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-ORCH-1"
    assert response.metadata.get("response_source") == "llm_orchestration"


@pytest.mark.asyncio
async def test_orchestration_mode_dispatches_handler_when_requested(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="orch-handler-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "during_stay"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_orchestrate(**kwargs):
        return OrchestrationDecision(
            normalized_query="book a table for 2 at 8 pm",
            intent="table_booking",
            confidence=0.9,
            action="dispatch_handler",
            target_service_id="restaurant_reservation",
            response_text="",
            use_handler=True,
            handler_intent="table_booking",
            pending_action=None,
            missing_fields=[],
            ticket=TicketDecision(required=False),
        )

    async def _fake_dispatch(*args, **kwargs):
        return HandlerResult(
            response_text="Sure, table booking started. For how many guests?",
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_booking_party_size",
            pending_data={"service_name": "Restaurant Reservation"},
            suggested_actions=["2 guests", "4 guests", "Cancel"],
            metadata={"response_source": "handler"},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_dispatch_handlers", True)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "hotel_name": "ICONIQA Demo Hotel",
            "business_type": "hotel",
            "service_catalog": [
                {
                    "id": "restaurant_reservation",
                    "name": "Restaurant Reservation",
                    "type": "service",
                    "phase_id": "during_stay",
                    "is_active": True,
                    "ticketing_enabled": True,
                }
            ],
            "journey_phases": [
                {"id": "during_stay", "name": "During Stay", "is_active": True, "order": 1},
            ],
            "services": {},
            "intents": [],
            "tools": [],
            "faq_bank": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.llm_orchestration_service.orchestrate_turn", _fake_orchestrate)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="orch-handler-1",
            message="book a table for 2 at 8 pm",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == "Sure, table booking started. For how many guests?"
    assert response.state == ConversationState.AWAITING_INFO
    assert context.pending_action == "collect_booking_party_size"
    assert response.metadata.get("response_source") == "llm_orchestration"


@pytest.mark.asyncio
async def test_orchestration_confirmation_yes_keeps_pending_service_and_dispatches(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="orch-confirm-continue-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_room_booking",
        pending_data={
            "_integration": {"phase": "pre_booking"},
            "service_id": "room_discovery",
            "room_type": "Deluxe Room",
            "check_in": "2026-03-12",
            "check_out": "2026-03-13",
            "guest_count": "2",
        },
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_orchestrate(**kwargs):
        return OrchestrationDecision(
            normalized_query="yes confirm",
            intent="faq",
            confidence=0.8,
            action="respond_only",
            target_service_id="hotel_enquiry_sales",
            response_text="I can assist with room discovery and hotel enquiries.",
            pending_action=None,
            pending_data_updates={},
            missing_fields=[],
            use_handler=False,
            ticket=TicketDecision(required=False),
        )

    dispatch_capture = {"intent": None}

    async def _fake_dispatch(message, intent_result, _context, capabilities, db_session):
        dispatch_capture["intent"] = intent_result.intent
        return HandlerResult(
            response_text="Confirmed. Your room booking request has been sent.",
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data={},
            suggested_actions=["Ask another question"],
            metadata={"response_source": "handler"},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_orchestration_dispatch_handlers", True)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "hotel_name": "ICONIQA Demo Hotel",
            "business_type": "hotel",
            "service_catalog": [
                {
                    "id": "room_discovery",
                    "name": "Room Discovery",
                    "type": "service",
                    "phase_id": "pre_booking",
                    "is_active": True,
                    "ticketing_enabled": True,
                },
                {
                    "id": "hotel_enquiry_sales",
                    "name": "Hotel Enquiry",
                    "type": "service",
                    "phase_id": "pre_booking",
                    "is_active": True,
                    "ticketing_enabled": False,
                },
            ],
            "journey_phases": [
                {"id": "pre_booking", "name": "Pre Booking", "is_active": True, "order": 1},
            ],
            "services": {},
            "intents": [],
            "tools": [],
            "faq_bank": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.llm_orchestration_service.orchestrate_turn", _fake_orchestrate)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="orch-confirm-continue-1",
            message="yes confirm",
            hotel_code="DEFAULT",
        )
    )

    assert dispatch_capture["intent"] == IntentType.CONFIRMATION_YES
    assert response.message == "Confirmed. Your room booking request has been sent."
    assert response.state == ConversationState.COMPLETED
    assert context.pending_action is None
    assert response.metadata.get("response_source") == "llm_orchestration"
