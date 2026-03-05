from schemas.chat import ConversationContext
from services.ticketing_service import ticketing_service


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
