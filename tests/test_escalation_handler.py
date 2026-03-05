import pytest

from handlers.escalation_handler import EscalationHandler
from schemas.chat import ConversationContext, IntentResult, IntentType, MessageRole
from services.ticketing_service import TicketingResult


@pytest.mark.asyncio
async def test_escalation_handler_skips_ticket_when_no_configured_case(monkeypatch):
    handler = EscalationHandler()
    context = ConversationContext(session_id="esc-1", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "i need airport transfer")

    monkeypatch.setattr(
        "handlers.escalation_handler.config_service.load_config",
        lambda: {"escalation": {"escalation_message": "Connecting you to support."}},
    )
    monkeypatch.setattr(
        "handlers.escalation_handler.ticketing_agent_service.match_configured_case",
        lambda _message: "",
    )
    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.get_latest_ticket", lambda _context: {})
    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.is_ticketing_enabled", lambda _caps: True)
    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.is_handoff_enabled", lambda _caps: False)

    async def _should_not_create(_payload):
        raise AssertionError("create_ticket should not run without configured ticketing case")

    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.create_ticket", _should_not_create)

    result = await handler.handle(
        "please connect me to staff for transport booking",
        IntentResult(intent=IntentType.HUMAN_REQUEST, confidence=0.9, entities={}),
        context,
        capabilities={},
    )

    assert result is not None
    assert result.metadata.get("ticket_created") is False
    assert result.metadata.get("ticket_create_error") == "ticket_skipped_no_matching_configured_case"
    assert result.metadata.get("ticket_creation_policy") == "configured_cases_only"


@pytest.mark.asyncio
async def test_escalation_handler_creates_ticket_with_conversation_context_when_case_matches(monkeypatch):
    handler = EscalationHandler()
    context = ConversationContext(session_id="esc-2", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "i want transport tomorrow morning")
    context.add_message(MessageRole.ASSISTANT, "Sure, please share pickup time.")
    context.add_message(MessageRole.USER, "pickup from airport at 7 pm")

    monkeypatch.setattr(
        "handlers.escalation_handler.config_service.load_config",
        lambda: {"escalation": {"escalation_message": "Connecting you to support."}},
    )
    monkeypatch.setattr(
        "handlers.escalation_handler.ticketing_agent_service.match_configured_case",
        lambda _message: "transport booking",
    )
    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.get_latest_ticket", lambda _context: {})
    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.is_ticketing_enabled", lambda _caps: True)
    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.is_handoff_enabled", lambda _caps: False)

    captured_issue = {"text": ""}

    def _build_payload(**kwargs):
        captured_issue["text"] = str(kwargs.get("issue") or "")
        return {"issue": captured_issue["text"]}

    async def _create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-HANDOFF-1", status_code=200, response={"ok": True})

    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.build_lumira_ticket_payload", _build_payload)
    monkeypatch.setattr("handlers.escalation_handler.ticketing_service.create_ticket", _create_ticket)

    result = await handler.handle(
        "please connect me to human for transport booking",
        IntentResult(intent=IntentType.HUMAN_REQUEST, confidence=0.9, entities={"requested_service": "Transport Booking"}),
        context,
        capabilities={},
    )

    assert result is not None
    assert result.metadata.get("ticket_created") is True
    assert result.metadata.get("ticket_id") == "LOCAL-HANDOFF-1"
    assert result.metadata.get("ticket_matched_case") == "transport booking"
    assert "recent guest context" in captured_issue["text"].lower()
