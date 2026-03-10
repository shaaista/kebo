from schemas.chat import ChatResponse, ConversationState
from services.lumira_compat_adapter import (
    build_engage_chat_request,
    build_engage_response,
    build_guest_journey_chat_request,
    build_guest_journey_response,
    resolve_ticket_summary,
)


def _chat_response(message: str, metadata: dict):
    return ChatResponse(
        session_id="sess-1",
        message=message,
        state=ConversationState.IDLE,
        metadata=metadata,
    )


def test_build_guest_journey_chat_request_maps_lumira_fields():
    req = build_guest_journey_chat_request(
        {
            "message": "AC is not working",
            "waNumber": "+91-9876543210",
            "phase": "During Stay",
            "guest_id": 101,
            "room_number": "305",
            "entity_id": 5703,
        }
    )

    assert req.message == "AC is not working"
    assert req.guest_phone == "+91-9876543210"
    assert req.channel == "whatsapp"
    assert req.metadata["flow"] == "guest_journey"
    assert req.metadata["ticket_source"] == "whatsapp_bot"
    assert req.metadata["phase"] == "During Stay"
    assert req.metadata["guest_id"] == 101
    assert req.metadata["entity_id"] == 5703
    assert req.metadata["room_number"] == "305"


def test_build_engage_chat_request_maps_lumira_fields():
    req = build_engage_chat_request(
        {
            "message": "Need help booking",
            "entity_id": 207,
            "group_id": 301,
            "message_id": 99,
            "conversation_id": "conv-10",
            "city": "Mumbai",
            "country": "India",
        },
        session_id_header="eng-sess-1",
    )

    assert req.session_id == "eng-sess-1"
    assert req.message == "Need help booking"
    assert req.channel == "web_widget"
    assert req.metadata["flow"] == "engage"
    assert req.metadata["ticket_source"] == "booking_bot"
    assert req.metadata["phase"] == "pre_booking"
    assert req.metadata["entity_id"] == 207
    assert req.metadata["group_id"] == 301
    assert req.metadata["message_id"] == 99
    assert req.metadata["conversation_id"] == "conv-10"
    assert req.metadata["city"] == "Mumbai"
    assert req.metadata["country"] == "India"


def test_build_guest_journey_response_for_created_ticket():
    response = _chat_response(
        "Ticket created successfully.",
        {
            "ticket_created": True,
            "ticket_id": "LOCAL-7",
            "ticket_status": "open",
            "ticket_category": "complaint",
            "ticket_sub_category": "maintenance",
            "room_number": "305",
            "ticket_api_response": {"ticket_id": "LOCAL-7", "status": "success"},
        },
    )

    payload = build_guest_journey_response(response)
    assert payload["is_ticket_intent"] is True
    assert payload["response"] == "Ticket created successfully."
    assert payload["category"] == "complaint"
    assert payload["sub_category"] == "maintenance"
    assert payload["room_number"] == "305"
    assert isinstance(payload["ticket_summary"], list)
    assert payload["ticket_summary"][0]["ticket_id"] == "LOCAL-7"


def test_build_guest_journey_response_for_ticket_update_keeps_intent_false():
    response = _chat_response(
        "Update added.",
        {
            "ticket_updated": True,
            "ticket_id": "123",
            "ticket_update_response": {"ok": True, "id": "123"},
        },
    )

    payload = build_guest_journey_response(response)
    assert payload["is_ticket_intent"] is False
    assert payload["ticket_summary"] == [{"ok": True, "id": "123"}]


def test_build_engage_response_maps_ticket_summary_as_object():
    response = _chat_response(
        "I have raised this with the team.",
        {
            "ticket_created": True,
            "ticket_api_response": {"ticket_id": "T-10", "assignedId": "A-1"},
        },
    )

    payload = build_engage_response(
        response,
        source_payload={
            "entity_id": 207,
            "group_id": 301,
            "message_id": 44,
            "city": "Pune",
            "country": "India",
        },
        session_id="eng-sess-9",
    )

    assert payload["is_ticket_intent"] is True
    assert payload["ticket_summary"]["ticket_id"] == "T-10"
    assert payload["message_id"] == 44
    assert payload["city"] == "Pune"
    assert payload["country"] == "India"
    assert payload["additional_data"]["session_id"] == "eng-sess-9"
    assert payload["additional_data"]["entity_id"] == 207
    assert payload["additional_data"]["group_id"] == 301


def test_resolve_ticket_summary_builds_fallback_when_api_response_missing():
    result = resolve_ticket_summary(
        {
            "ticket_id": "LOCAL-21",
            "ticket_status": "open",
            "ticket_category": "request",
            "ticket_sub_category": "transport",
            "ticket_priority": "MEDIUM",
            "ticket_summary": "Airport pickup requested",
        },
        as_list=True,
    )
    assert isinstance(result, list)
    assert result[0]["ticket_id"] == "LOCAL-21"
    assert result[0]["issue"] == "Airport pickup requested"

