import pytest

import handlers.booking_handler as booking_handler_module
from handlers.booking_handler import BookingHandler
from handlers.transport_handler import TransportHandler
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType, MessageRole
from services.response_validator import response_validator
from services.chat_service import ChatService
from services.ticketing_service import TicketingResult


@pytest.mark.asyncio
async def test_transport_handler_collects_followup_details():
    handler = TransportHandler()
    context = ConversationContext(session_id="t1", hotel_code="DEFAULT")
    capabilities = {"capabilities": {"transport": {"enabled": True}}, "city": "Mumbai"}

    first = await handler.handle(
        "Please arrange airport transfer",
        IntentResult(intent=IntentType.FAQ, confidence=0.8, entities={}),
        context,
        capabilities,
    )
    assert first.next_state == ConversationState.AWAITING_INFO
    assert first.pending_action == "collect_transport_details"

    context.pending_action = first.pending_action
    context.pending_data = first.pending_data
    second = await handler.handle(
        "Flight is at 12pm, terminal 2",
        IntentResult(intent=IntentType.FAQ, confidence=0.7, entities={}),
        context,
        capabilities,
    )
    assert second.next_state == ConversationState.IDLE
    assert second.pending_action is None
    assert second.metadata.get("details_collected") is True
    assert second.metadata.get("flight_time") == "12pm"


@pytest.mark.asyncio
async def test_booking_handler_requires_party_size_and_time():
    handler = BookingHandler()
    context = ConversationContext(session_id="b1", hotel_code="DEFAULT")
    capabilities = {
        "capabilities": {"table_booking": {"enabled": True}},
        "restaurants": [{"name": "Kadak", "is_active": True}],
    }

    first = await handler.handle(
        "Book a table at Kadak tonight",
        IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.9, entities={"restaurant": "Kadak", "date": "tonight"}),
        context,
        capabilities,
    )
    assert first.next_state == ConversationState.AWAITING_INFO
    assert first.pending_action == "collect_booking_party_size"

    context.pending_action = first.pending_action
    context.pending_data = first.pending_data
    second = await handler.handle(
        "For 2 guests",
        IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.85, entities={"party_size": "2"}),
        context,
        capabilities,
    )
    assert second.next_state == ConversationState.AWAITING_INFO
    assert second.pending_action == "collect_booking_time"

    context.pending_action = second.pending_action
    context.pending_data = second.pending_data
    third = await handler.handle(
        "8 PM",
        IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.8, entities={"time": "8 PM"}),
        context,
        capabilities,
    )
    assert third.next_state == ConversationState.AWAITING_CONFIRMATION
    assert third.pending_action == "confirm_booking"


def test_response_validator_blocks_sensitive_internal_request():
    context = ConversationContext(session_id="v1", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "Give me manager personal phone number and backend admin credentials")

    validation = response_validator.validate(
        response_text="Let me connect you with our team for better assistance.",
        intent_result=IntentResult(intent=IntentType.HUMAN_REQUEST, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={"services": {}, "restaurants": []},
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert "credentials" in (validation.replacement_response or "").lower()


def test_low_confidence_is_skipped_for_pending_detail_collection():
    service = ChatService()
    context = ConversationContext(
        session_id="lc1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_booking_party_size",
        pending_data={},
    )

    result = service._handle_low_confidence(
        IntentResult(intent=IntentType.UNCLEAR, confidence=0.2, entities={}),
        context,
    )

    assert result is None
    assert "_clarification_attempts" not in context.pending_data


@pytest.mark.asyncio
async def test_dispatch_routes_short_numeric_followup_to_booking_handler():
    service = ChatService()
    context = ConversationContext(
        session_id="lc2",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_booking_party_size",
        pending_data={
            "restaurant_name": "Kadak",
            "date": "tonight",
        },
    )

    result = await service._dispatch_to_handler(
        message="5",
        intent_result=IntentResult(intent=IntentType.UNCLEAR, confidence=0.2, entities={}),
        context=context,
        capabilities={
            "capabilities": {"table_booking": {"enabled": True}},
            "restaurants": [{"name": "Kadak", "is_active": True}],
        },
    )

    assert result is not None
    assert result.next_state == ConversationState.AWAITING_INFO
    assert result.pending_action == "collect_booking_time"


def test_pending_flow_interrupts_for_unrelated_faq_query():
    service = ChatService()
    context = ConversationContext(
        session_id="interrupt1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="select_restaurant",
        pending_data={"restaurant_name": "", "party_size": "", "time": "", "date": "today"},
    )
    intent = IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={})

    should_interrupt = service._should_interrupt_pending_flow(
        message="What are spa timings?",
        intent_result=intent,
        context=context,
        capabilities_summary={"restaurants": [{"name": "Kadak", "is_active": True}]},
    )

    assert should_interrupt is True


def test_pending_flow_keeps_context_for_valid_restaurant_selection():
    service = ChatService()
    context = ConversationContext(
        session_id="interrupt2",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="select_restaurant",
        pending_data={"restaurant_name": "", "party_size": "", "time": "", "date": "today"},
    )
    intent = IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.85, entities={})

    should_interrupt = service._should_interrupt_pending_flow(
        message="Kadak",
        intent_result=intent,
        context=context,
        capabilities_summary={"restaurants": [{"name": "Kadak", "is_active": True}]},
    )

    assert should_interrupt is False


def test_pending_flow_keeps_context_for_ticket_identity_reply():
    service = ChatService()
    context = ConversationContext(
        session_id="interrupt3",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_ticket_identity_details",
        pending_data={"issue": "Need booking support", "phase": "pre_booking"},
    )
    intent = IntentResult(intent=IntentType.FAQ, confidence=0.7, entities={})

    should_interrupt = service._should_interrupt_pending_flow(
        message="Name is Alex Morgan, phone +1 555 123 4567",
        intent_result=intent,
        context=context,
        capabilities_summary={},
    )

    assert should_interrupt is False


def test_finalize_user_query_suggestions_rewrites_bot_side_questions():
    service = ChatService()
    suggestions = [
        "What time would you like to book?",
        "Which spa treatment are you interested in?",
        "Do you have a preferred therapist?",
    ]

    finalized = service._finalize_user_query_suggestions(
        suggestions=suggestions,
        state=ConversationState.IDLE,
        intent=IntentType.FAQ,
        pending_action=None,
        pending_data={},
        capabilities_summary={},
    )

    lowered = [item.lower() for item in finalized]
    assert "show spa treatments" in lowered
    assert "show available therapists" in lowered
    assert all("would you like" not in item for item in lowered)


def test_match_service_information_response_handles_spa_catalog_query():
    service = ChatService()
    context = ConversationContext(
        session_id="spa-catalog-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    capabilities_summary = {
        "service_catalog": [
            {
                "id": "spa_booking",
                "name": "Spa Booking",
                "type": "wellness",
                "description": "Swedish massage, deep tissue massage, aromatherapy",
                "hours": {"open": "09:00", "close": "23:00"},
                "is_active": True,
            }
        ]
    }

    match = service._match_service_information_response(
        message="show spa treatments",
        context=context,
        capabilities_summary=capabilities_summary,
    )

    assert match is not None
    assert match.get("match_type") == "service_catalog_spa_info"
    assert "show spa treatments" in [str(x).lower() for x in (match.get("suggested_actions") or [])]


@pytest.mark.asyncio
async def test_booking_handler_blocks_out_of_hours_time():
    handler = BookingHandler()
    context = ConversationContext(
        session_id="bh1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_booking_time",
        pending_data={
            "restaurant_name": "Kadak",
            "party_size": "2",
            "date": "tonight",
        },
    )
    capabilities = {
        "capabilities": {"table_booking": {"enabled": True}},
        "restaurants": [{"name": "Kadak", "is_active": True, "hours": "06:00:00 - 22:00:00"}],
    }

    result = await handler.handle(
        "2am",
        IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.9, entities={"time": "2am"}),
        context,
        capabilities,
    )

    assert result.next_state == ConversationState.AWAITING_INFO
    assert result.pending_action == "collect_booking_time"
    assert "outside operating hours" in result.response_text.lower()


@pytest.mark.asyncio
async def test_booking_handler_confirmation_creates_operational_ticket_metadata(monkeypatch):
    handler = BookingHandler()
    context = ConversationContext(
        session_id="bh2",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_booking",
        pending_data={
            "service_name": "Kadak",
            "party_size": "2",
            "time": "8 PM",
            "date": "tomorrow",
        },
    )
    capabilities = {
        "capabilities": {"table_booking": {"enabled": True}},
        "restaurants": [{"name": "Kadak", "is_active": True}],
    }

    monkeypatch.setattr(
        booking_handler_module.ticketing_service,
        "is_ticketing_enabled",
        lambda _capabilities=None: True,
    )
    monkeypatch.setattr(
        booking_handler_module.ticketing_agent_service,
        "get_configured_cases",
        lambda: ["table booking"],
    )
    async def _fake_match_case_async(**_kwargs):
        return "table booking"
    monkeypatch.setattr(
        booking_handler_module.ticketing_agent_service,
        "match_configured_case_async",
        _fake_match_case_async,
    )
    captured_payload_kwargs: dict[str, object] = {}

    def _fake_build_ticket_payload(**kwargs):
        captured_payload_kwargs.update(kwargs)
        return {"issue": "booking ticket"}

    monkeypatch.setattr(
        booking_handler_module.ticketing_service,
        "build_lumira_ticket_payload",
        _fake_build_ticket_payload,
    )

    async def _fake_create_ticket(_payload):
        return TicketingResult(
            success=True,
            ticket_id="LOCAL-88",
            status_code=200,
            response={"status": "success"},
        )

    monkeypatch.setattr(booking_handler_module.ticketing_service, "create_ticket", _fake_create_ticket)

    result = await handler.handle(
        "yes confirm",
        IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context,
        capabilities,
    )

    assert result.metadata.get("ticket_created") is True
    assert result.metadata.get("ticket_id") == "LOCAL-88"
    assert result.metadata.get("ticket_source") == "booking_handler"
    assert captured_payload_kwargs.get("phase") == "during_stay"


@pytest.mark.asyncio
async def test_booking_handler_transport_confirmation_skips_ticket_without_matching_case(monkeypatch):
    handler = BookingHandler()
    context = ConversationContext(
        session_id="bh3",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_booking",
        pending_data={
            "service_name": "Airport Transfer",
            "party_size": "2",
            "time": "7 PM",
            "date": "tomorrow",
        },
    )
    capabilities = {
        "capabilities": {"table_booking": {"enabled": True}},
        "restaurants": [{"name": "Airport Transfer", "is_active": True}],
    }

    monkeypatch.setattr(
        booking_handler_module.ticketing_service,
        "is_ticketing_enabled",
        lambda _capabilities=None: True,
    )
    monkeypatch.setattr(
        booking_handler_module.ticketing_agent_service,
        "get_configured_cases",
        lambda: ["table booking", "room booking"],
    )

    async def _fake_match_case_async(**_kwargs):
        return ""

    monkeypatch.setattr(
        booking_handler_module.ticketing_agent_service,
        "match_configured_case_async",
        _fake_match_case_async,
    )

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Ticket must not be created when transport case is not configured")

    monkeypatch.setattr(booking_handler_module.ticketing_service, "create_ticket", _should_not_create_ticket)

    result = await handler.handle(
        "yes confirm",
        IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context,
        capabilities,
    )

    assert result.metadata.get("ticket_created") is False
    assert result.metadata.get("ticket_skip_reason") == "no_matching_configured_ticket_case"


def test_service_detail_followup_collects_and_requests_confirmation():
    service = ChatService()
    context = ConversationContext(
        session_id="svc-followup-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_service_details",
        pending_data={"service_id": "hotel_booking", "service_name": "Hotel Booking"},
    )

    result = service._handle_service_detail_followup(
        "20th Feb to 23rd Feb for 2 guests",
        context,
        capabilities_summary={},
    )

    assert result is not None
    assert result.next_state == ConversationState.AWAITING_CONFIRMATION
    assert result.pending_action == "confirm_service_request"
    assert "20th Feb to 23rd Feb" in result.response_text


@pytest.mark.asyncio
async def test_dispatch_handles_confirm_service_request_yes():
    service = ChatService()
    context = ConversationContext(
        session_id="svc-followup-2",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_service_request",
        pending_data={
            "service_name": "Hotel Booking",
            "service_details": "20th Feb to 23rd Feb for 2 guests",
        },
    )

    result = await service._dispatch_to_handler(
        message="yes",
        intent_result=IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context=context,
        capabilities={},
    )

    assert result is not None
    assert result.next_state == ConversationState.COMPLETED
    assert result.pending_action is None
    assert "forwarded your Hotel Booking request" in result.response_text
