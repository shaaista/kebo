import pytest

from handlers.room_service_handler import RoomServiceHandler
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType
from services.ticketing_service import TicketingResult


@pytest.mark.asyncio
async def test_room_service_auto_resolves_room_from_db_and_creates_ticket(monkeypatch):
    handler = RoomServiceHandler()
    context = ConversationContext(
        session_id="room-service-ticket-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={
            "_integration": {
                "entity_id": 5703,
                "guest_phone": "+91 9876543210",
            }
        },
    )

    capabilities = {
        "capabilities": {
            "room_service": {"enabled": True},
            "housekeeping": {"enabled": True},
        }
    }

    async def _fake_fetch_guest_profile(_db_session, *, entity_id, guest_id=None, guest_phone=None):
        assert entity_id == 5703
        assert guest_phone
        return {"guest_id": "991", "room_number": "305", "guest_name": "Alex"}

    async def _fake_create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-RS-1", status_code=200, response={"ok": True})

    monkeypatch.setattr(
        "handlers.room_service_handler.lumira_ticketing_repository.fetch_guest_profile",
        _fake_fetch_guest_profile,
    )
    monkeypatch.setattr("handlers.room_service_handler.ticketing_service.is_ticketing_enabled", lambda _cap=None: True)
    monkeypatch.setattr(
        "handlers.room_service_handler.ticketing_agent_service.get_configured_cases",
        lambda: ["Guest reports a complaint or maintenance issue that requires staff action."],
    )

    async def _fake_match_case_async(**_kwargs):
        return "Guest reports a complaint or maintenance issue that requires staff action."

    monkeypatch.setattr(
        "handlers.room_service_handler.ticketing_agent_service.match_configured_case_async",
        _fake_match_case_async,
    )
    monkeypatch.setattr("handlers.room_service_handler.ticketing_service.create_ticket", _fake_create_ticket)

    result = await handler.handle(
        message="please send fresh towels",
        intent_result=IntentResult(intent=IntentType.ROOM_SERVICE, confidence=0.95, entities={}),
        context=context,
        capabilities=capabilities,
        db_session=object(),
    )

    assert result.pending_action is None
    assert "forwarded your towels request" in result.response_text.lower()
    assert result.metadata.get("room_number") == "305"
    assert result.metadata.get("ticket_created") is True
    assert result.metadata.get("ticket_id") == "LOCAL-RS-1"
    assert result.metadata.get("ticket_sub_category") == "amenities"


@pytest.mark.asyncio
async def test_room_service_info_query_does_not_create_ticket(monkeypatch):
    handler = RoomServiceHandler()
    context = ConversationContext(
        session_id="room-service-ticket-2",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    capabilities = {
        "capabilities": {
            "room_service": {"enabled": True},
            "housekeeping": {"enabled": True},
        }
    }

    # No actionable request type yet ("room service" generic), should ask for detail.
    result = await handler.handle(
        message="room service",
        intent_result=IntentResult(intent=IntentType.ROOM_SERVICE, confidence=0.9, entities={}),
        context=context,
        capabilities=capabilities,
        db_session=None,
    )

    assert result.pending_action == "awaiting_request_detail"
    assert result.metadata.get("ticket_created") is None


@pytest.mark.asyncio
async def test_room_service_actionable_request_skips_ticket_without_matching_case(monkeypatch):
    handler = RoomServiceHandler()
    context = ConversationContext(
        session_id="room-service-ticket-3",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        room_number="101",
    )
    capabilities = {
        "capabilities": {
            "room_service": {"enabled": True},
            "housekeeping": {"enabled": True},
        }
    }

    monkeypatch.setattr("handlers.room_service_handler.ticketing_service.is_ticketing_enabled", lambda _cap=None: True)
    monkeypatch.setattr(
        "handlers.room_service_handler.ticketing_agent_service.get_configured_cases",
        lambda: ["table booking at a restaurant"],
    )

    async def _fake_match_case_async(**_kwargs):
        return ""

    monkeypatch.setattr(
        "handlers.room_service_handler.ticketing_agent_service.match_configured_case_async",
        _fake_match_case_async,
    )

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Ticket must not be created without a configured-case match")

    monkeypatch.setattr("handlers.room_service_handler.ticketing_service.create_ticket", _should_not_create_ticket)

    result = await handler.handle(
        message="please send soap and towel",
        intent_result=IntentResult(intent=IntentType.ROOM_SERVICE, confidence=0.95, entities={}),
        context=context,
        capabilities=capabilities,
        db_session=None,
    )

    assert result.metadata.get("ticket_created") is False
    assert result.metadata.get("ticket_skip_reason") == "no_matching_configured_ticket_case"
