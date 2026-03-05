from datetime import UTC, datetime, timedelta

import handlers.complaint_handler as complaint_handler_module
from handlers.complaint_handler import ComplaintHandler
import pytest
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType
from schemas.chat import MessageRole
from services.ticketing_service import ticketing_service
from services.ticketing_service import TicketingResult


def test_resolve_department_prefers_housekeeping_for_towels():
    handler = ComplaintHandler()
    match = handler._resolve_department_from_context(
        pending_data={
            "issue": "Need extra towels in my room please",
            "message": "Need extra towels in my room please",
            "category": "request",
            "sub_category": "",
        },
        entities={},
        departments=[
            {"department_id": "1", "department_name": "Housekeeping"},
            {"department_id": "2", "department_name": "Maintenance"},
        ],
    )
    assert match is not None
    assert str(match["department_id"]) == "1"


def test_resolve_outlet_matches_by_outlet_name_mention():
    handler = ComplaintHandler()
    match = handler._resolve_outlet_from_context(
        pending_data={
            "issue": "Table booking issue at Aviator restaurant",
            "message": "Table booking issue at Aviator restaurant",
        },
        outlets=[
            {"outlet_id": "10", "outlet_name": "Aviator"},
            {"outlet_id": "11", "outlet_name": "Pool Bar"},
        ],
    )
    assert match is not None
    assert str(match["outlet_id"]) == "10"


@pytest.mark.asyncio
async def test_confirm_ticket_creation_allows_switch_to_new_issue(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-switch-1",
        hotel_code="DEFAULT",
        room_number="101",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_ticket_creation",
        pending_data={"issue": "opera", "category": "complaint", "priority": "medium"},
    )

    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_auto_create_on_actionable", False)
    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda _capabilities=None: True)

    result = await handler._handle_ticket_creation_confirmation(
        message="i need a towel",
        intent_result=IntentResult(intent=IntentType.COMPLAINT, confidence=0.9, entities={}),
        context=context,
        capabilities={},
        db_session=None,
    )

    assert result.pending_action == "confirm_ticket_creation"
    assert "should i create this ticket now" in result.response_text.lower()
    assert str((result.pending_data or {}).get("issue") or "").lower() == "i need a towel"


@pytest.mark.asyncio
async def test_collect_ticket_room_number_auto_creates_ticket_when_enabled(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-auto-create-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_ticket_room_number",
        pending_data={
            "issue": "Hairdryer not available in room",
            "message": "its not there in my room",
            "category": "complaint",
            "priority": "medium",
        },
    )

    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_auto_create_on_actionable", True)
    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda _capabilities=None: True)

    async def _fake_create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-99", status_code=200, response={"ok": True})

    monkeypatch.setattr(ticketing_service, "create_ticket", _fake_create_ticket)

    result = await handler._handle_room_number_collection(
        message="101",
        context=context,
        capabilities={},
        db_session=None,
    )

    assert result.metadata.get("ticket_created") is True
    assert result.metadata.get("ticket_id") == "LOCAL-99"
    assert result.pending_action is None
    assert "address it shortly" in result.response_text.lower()


def test_issue_resolution_uses_previous_user_context_for_generic_issue():
    handler = ComplaintHandler()
    context = ConversationContext(session_id="ticket-issue-context-1", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "hairdryer")
    context.add_message(MessageRole.USER, "its not there in my room")

    resolved = handler._resolve_issue_text(
        issue="its not there in my room",
        message="its not there in my room",
        context=context,
    )

    assert "hairdryer" in resolved.lower()
    assert "not available in room" in resolved.lower()


@pytest.mark.asyncio
async def test_enrich_ticket_context_hydrates_room_number_from_guest_profile(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-enrich-guest-profile-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"entity_id": 5703, "guest_phone": "+91 9876543210"}},
    )

    async def _fake_fetch_ri_entity_id(_db_session, fms_entity_id):
        assert fms_entity_id == 5703
        return ""

    async def _fake_fetch_departments(_db_session, entity_id):
        assert entity_id == 5703
        return []

    async def _fake_fetch_outlets(_db_session, entity_id):
        assert entity_id == 5703
        return []

    async def _fake_fetch_guest_profile(_db_session, *, entity_id, guest_id=None, guest_phone=None):
        assert entity_id == 5703
        return {"guest_id": "8877", "guest_name": "Jordan", "room_number": "409"}

    monkeypatch.setattr(
        "handlers.complaint_handler.lumira_ticketing_repository.fetch_ri_entity_id_from_mapping",
        _fake_fetch_ri_entity_id,
    )
    monkeypatch.setattr(
        "handlers.complaint_handler.lumira_ticketing_repository.fetch_departments_of_entity",
        _fake_fetch_departments,
    )
    monkeypatch.setattr(
        "handlers.complaint_handler.lumira_ticketing_repository.fetch_outlets_of_entity",
        _fake_fetch_outlets,
    )
    monkeypatch.setattr(
        "handlers.complaint_handler.lumira_ticketing_repository.fetch_guest_profile",
        _fake_fetch_guest_profile,
    )

    enriched = await handler._enrich_ticket_context(
        context=context,
        pending_data={"issue": "need towel", "category": "request", "sub_category": "amenities"},
        db_session=object(),
        entities={},
    )

    assert enriched.get("room_number") == "409"
    assert context.room_number == "409"
    integration = context.pending_data.get("_integration", {})
    assert str(integration.get("guest_id") or "") == "8877"


@pytest.mark.asyncio
async def test_start_ticket_creation_flow_uses_llm_sub_category(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-subcategory-1",
        hotel_code="DEFAULT",
        room_number="305",
    )

    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_auto_create_on_actionable", False)

    async def _fake_classify_sub_category(**kwargs):
        assert "ac not cooling" in str(kwargs.get("issue") or "").lower()
        return "maintenance"

    async def _fake_extract_preferences(**kwargs):
        return []

    monkeypatch.setattr(
        "handlers.complaint_handler.ticketing_llm_service.classify_sub_category",
        _fake_classify_sub_category,
    )
    monkeypatch.setattr(
        "handlers.complaint_handler.ticketing_llm_service.extract_guest_preferences",
        _fake_extract_preferences,
    )

    result = await handler._start_ticket_creation_flow(
        message="AC not cooling in room 305",
        intent_result=IntentResult(intent=IntentType.COMPLAINT, confidence=0.93, entities={}),
        context=context,
        capabilities={},
        db_session=None,
    )

    assert result.pending_action == "confirm_ticket_creation"
    assert str((result.pending_data or {}).get("sub_category") or "") == "maintenance"


@pytest.mark.asyncio
async def test_confirm_ticket_creation_requests_reconfirm_on_stale_pending(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-stale-confirm-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_ticket_creation",
        pending_data={
            "issue": "AC not cooling",
            "category": "complaint",
            "priority": "high",
            "room_number": "305",
        },
    )
    assistant_prompt = context.add_message(MessageRole.ASSISTANT, "Should I create this ticket now?")
    assistant_prompt.timestamp = datetime.now(UTC) - timedelta(minutes=45)
    context.add_message(MessageRole.USER, "yes")

    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_stale_reconfirm_enabled", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_stale_reconfirm_minutes", 30)

    result = await handler._handle_ticket_creation_confirmation(
        message="yes",
        intent_result=IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.99, entities={}),
        context=context,
        capabilities={},
        db_session=None,
    )

    assert result.pending_action == "confirm_ticket_creation"
    assert result.metadata.get("ticket_stale_reconfirm") is True
    assert "before i continue with ticket" in result.response_text.lower()


@pytest.mark.asyncio
async def test_start_ticket_creation_flow_collects_identity_for_prebooking(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-identity-gate-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"flow": "engage", "ticket_source": "booking_bot"}},
    )

    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_auto_create_on_actionable", False)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_gate_enabled", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_gate_prebooking_only", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_require_name", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_require_phone", True)

    async def _fake_extract_preferences(**kwargs):
        return []

    monkeypatch.setattr(
        "handlers.complaint_handler.ticketing_llm_service.extract_guest_preferences",
        _fake_extract_preferences,
    )

    result = await handler._start_ticket_creation_flow(
        message="Please help with booking assistance",
        intent_result=IntentResult(
            intent=IntentType.COMPLAINT,
            confidence=0.9,
            entities={"phase": "pre_booking"},
        ),
        context=context,
        capabilities={},
        db_session=None,
    )

    assert result.pending_action == "collect_ticket_identity_details"
    assert "guest name and contact phone number" in result.response_text.lower()


@pytest.mark.asyncio
async def test_collect_ticket_identity_details_auto_creates_ticket(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-identity-create-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_ticket_identity_details",
        pending_data={
            "issue": "Need pre-arrival assistance",
            "message": "Need pre-arrival assistance",
            "category": "request",
            "priority": "medium",
            "phase": "pre_booking",
            "_integration": {"flow": "engage", "ticket_source": "booking_bot"},
        },
    )

    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_auto_create_on_actionable", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_gate_enabled", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_gate_prebooking_only", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_require_name", True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_require_phone", True)

    captured_payload: dict[str, object] = {}

    async def _fake_create_ticket(payload):
        captured_payload.update(payload)
        return TicketingResult(success=True, ticket_id="LOCAL-404", status_code=200, response={"ok": True})

    monkeypatch.setattr(ticketing_service, "create_ticket", _fake_create_ticket)

    result = await handler._handle_ticket_identity_details(
        message="Name is Alex Morgan, phone +1 555 111 2222",
        context=context,
        capabilities={},
        db_session=None,
    )

    assert result.metadata.get("ticket_created") is True
    assert result.metadata.get("ticket_id") == "LOCAL-404"
    assert str(captured_payload.get("guest_name") or "") == "Alex Morgan"
    assert str(captured_payload.get("guest_phone") or "").startswith("+1555")


@pytest.mark.asyncio
async def test_create_ticket_from_pending_adds_guest_preferences_to_metadata_and_notes(monkeypatch):
    handler = ComplaintHandler()
    context = ConversationContext(
        session_id="ticket-preferences-1",
        hotel_code="DEFAULT",
        room_number="305",
    )
    pending = {
        "issue": "Please avoid spicy food",
        "message": "Please avoid spicy food for my meals",
        "category": "request",
        "sub_category": "order_food",
        "priority": "medium",
        "room_number": "305",
        "guest_preferences": ["non-spicy food preference", "vegetarian diet"],
    }

    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr(complaint_handler_module.settings, "ticketing_identity_gate_enabled", False)

    captured_payload: dict[str, object] = {}

    async def _fake_create_ticket(payload):
        captured_payload.update(payload)
        return TicketingResult(success=True, ticket_id="LOCAL-701", status_code=200, response={"ok": True})

    monkeypatch.setattr(ticketing_service, "create_ticket", _fake_create_ticket)

    result = await handler._create_ticket_from_pending(
        context=context,
        pending=pending,
        capabilities={},
        db_session=None,
        skip_existing_route=True,
    )

    assert result.metadata.get("ticket_created") is True
    assert result.metadata.get("guest_preferences") == ["non-spicy food preference", "vegetarian diet"]
    assert "guest preferences:" in str(captured_payload.get("manager_notes") or "").lower()
