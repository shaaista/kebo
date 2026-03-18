import pytest

from schemas.chat import ConversationContext, MessageRole
from services.ticketing_service import TicketingResult, TicketingService, ticketing_service


def test_build_payload_includes_engage_compat_fields_from_integration():
    context = ConversationContext(
        session_id="sess-1",
        hotel_code="DEFAULT",
        channel="web",
        pending_data={
            "_integration": {
                "flow": "engage",
                "organisation_id": "123",
                "group_id": "44",
                "message_id": "901",
                "phase": "pre_booking",
                "room_number": "305",
            }
        },
    )

    payload = ticketing_service.build_lumira_ticket_payload(
        context=context,
        issue="Need support",
        message="Need support on booking details",
        category="request",
        sub_category="booking_help",
        priority="high",
        department_id="9",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cost=0.42,
    )

    assert payload["source"] == "booking_bot"
    assert payload["group_id"] == "44"
    assert payload["message_id"] == "901"
    assert payload["phase"] == "Pre Booking"
    assert payload["priority"] == "HIGH"
    assert payload["categorization"] == "request"
    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 50
    assert payload["total_tokens"] == 150
    assert payload["cost"] == 0.42


def test_get_latest_ticket_normalizes_common_keys():
    context = ConversationContext(
        session_id="sess-2",
        hotel_code="DEFAULT",
        pending_data={
            "latest_ticket": {
                "ticket_id": "T-10",
                "assignedId": "A-1",
                "department_allocated": "88",
            }
        },
    )
    latest = ticketing_service.get_latest_ticket(context)
    assert latest["id"] == "T-10"
    assert latest["assigned_id"] == "A-1"
    assert latest["department_id"] == "88"


def test_build_payload_normalizes_phase_priority_category_and_source_defaults():
    context = ConversationContext(
        session_id="sess-3",
        hotel_code="DEFAULT",
        channel="web",
        pending_data={
            "_integration": {
                "flow": "guest_journey",
                "organisation_id": "5703",
                "phase": "in_stay",
            }
        },
    )

    payload = ticketing_service.build_lumira_ticket_payload(
        context=context,
        issue="Need AC repair",
        message="AC not cooling",
        category="Food Order",
        priority="urgent",
    )

    assert payload["phase"] == "During Stay"
    assert payload["priority"] == "CRITICAL"
    assert payload["categorization"] == "request"
    assert payload["source"] == "manual"


@pytest.mark.asyncio
async def test_create_ticket_deduplicates_same_issue_within_window(monkeypatch):
    service = TicketingService()
    context = ConversationContext(
        session_id="sess-dedupe",
        hotel_code="DEFAULT",
        channel="web_widget",
        pending_data={"_integration": {"phase": "during_stay", "guest_id": "921348", "organisation_id": "5703"}},
    )
    payload = service.build_lumira_ticket_payload(
        context=context,
        issue="Order for Veggie Sliders and Mac 'n' Cheese to be delivered to room 1205 at 10:15 PM.",
        message="Deliver at 10:15 PM.",
        category="request",
        sub_category="inroom_dining",
        phase="during_stay",
    )

    call_count = {"count": 0}

    async def _fake_request_json(_method, _url, _payload, *, debug_context=None):  # noqa: ARG001
        call_count["count"] += 1
        return TicketingResult(
            success=True,
            ticket_id="T-123",
            assigned_id="",
            status_code=200,
            payload=dict(_payload or {}),
            response={"id": "T-123"},
        )

    monkeypatch.setattr(service, "_use_local_mode", lambda: False)
    monkeypatch.setattr(service, "_ticketing_base_url", lambda: "https://example.test")
    monkeypatch.setattr(service, "_request_json", _fake_request_json)

    first = await service.create_ticket(dict(payload))
    second_payload = dict(payload)
    second_payload["message"] = "Yes, confirm my order."
    second = await service.create_ticket(second_payload)

    assert first.success is True
    assert second.success is True
    assert first.ticket_id == "T-123"
    assert second.ticket_id == "T-123"
    assert second.response.get("duplicate_suppressed") is True
    assert call_count["count"] == 1


def test_build_payload_includes_recent_conversation_in_message():
    context = ConversationContext(
        session_id="sess-conv-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"organisation_id": "5703", "phase": "during_stay"}},
    )
    context.add_message(MessageRole.USER, "hi i need food")
    context.add_message(MessageRole.ASSISTANT, "Sure, what would you like from in-room dining?")
    context.add_message(MessageRole.USER, "veg sliders and mac n cheese at 10:15 pm")

    payload = ticketing_service.build_lumira_ticket_payload(
        context=context,
        issue="Order for Veg Sliders and Mac n Cheese at 10:15 PM",
        message="confirm order",
        category="request",
        sub_category="inroom_dining",
        priority="medium",
    )

    assert "User: hi i need food" in payload["message"]
    assert "AI: Sure, what would you like from in-room dining?" in payload["message"]
    assert "User: veg sliders and mac n cheese at 10:15 pm" in payload["message"]
    assert "Issue Summary: Order for Veg Sliders and Mac n Cheese at 10:15 PM" in payload["message"]


def test_build_payload_falls_back_fields_from_context_when_not_explicit():
    context = ConversationContext(
        session_id="sess-fallback-1",
        hotel_code="DEFAULT",
        pending_data={
            "room_number": "1205",
            "department_id": "24",
            "sentiment_score": "0.82",
            "_integration": {"organisation_id": "5703", "phase": "during_stay"},
        },
    )

    payload = ticketing_service.build_lumira_ticket_payload(
        context=context,
        issue="Please send housekeeping to room 1205",
        message="need cleaning help",
        category="request",
        sub_category="housekeeping",
        priority="medium",
    )

    assert payload["room_number"] == "1205"
    assert payload["department_allocated"] == "24"
    assert payload["department_id"] == "24"
    assert payload["sentiment_score"] == "0.82"
