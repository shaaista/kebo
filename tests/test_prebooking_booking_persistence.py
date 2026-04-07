import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.routes import chat as chat_routes
from schemas.chat import ConversationContext


def test_extract_prebooking_payload_maps_common_fields() -> None:
    context = ConversationContext(session_id="sess-prebooking-1", hotel_code="TEST")
    payload = chat_routes._extract_prebooking_booking_payload(
        service_name="Room Booking",
        form_data={
            "guest_phone": "+91 98765 43210",
            "guest_name": "Rahul Sharma",
            "property_name": "Shimla Orchid",
            "room_type": "Deluxe",
            "check_in_date": "2026-05-10",
            "check_out_date": "2026-05-12",
            "num_guests": "2",
        },
        req_meta={"phase": "pre_booking", "channel": "web"},
        context=context,
    )

    assert payload
    assert payload["guest_phone"] == "+919876543210"
    assert payload["guest_name"] == "Rahul Sharma"
    assert payload["property_name"] == "Shimla Orchid"
    assert payload["room_type"] == "Deluxe"
    assert payload["num_guests"] == 2
    assert payload["check_in_date"].isoformat() == "2026-05-10"
    assert payload["check_out_date"].isoformat() == "2026-05-12"


def test_extract_prebooking_payload_uses_user_id_phone_and_single_date_fallback() -> None:
    context = ConversationContext(session_id="sess-prebooking-2", hotel_code="TEST")
    payload = chat_routes._extract_prebooking_booking_payload(
        service_name="Room Booking",
        form_data={
            "check_in": "2026-06-01",
            "property_name": "Goa Property",
        },
        req_meta={"phase": "pre_booking", "user_id": "+91-9988776655"},
        context=context,
    )

    assert payload
    assert payload["guest_phone"] == "+919988776655"
    assert payload["check_in_date"].isoformat() == "2026-06-01"
    assert payload["check_out_date"].isoformat() == "2026-06-02"


def test_extract_prebooking_payload_allows_booking_flow_without_explicit_phase() -> None:
    context = ConversationContext(session_id="sess-prebooking-3", hotel_code="TEST")
    payload = chat_routes._extract_prebooking_booking_payload(
        service_name="Room Booking",
        form_data={"property_name": "Manali Property"},
        req_meta={
            "flow": "booking_bot",
            "guest_phone": "+1 (202) 555-0182",
            "booking_check_in_date": "2026-07-10",
            "booking_check_out_date": "2026-07-14",
        },
        context=context,
    )

    assert payload
    assert payload["guest_phone"] == "+12025550182"
    assert payload["check_in_date"].isoformat() == "2026-07-10"
    assert payload["check_out_date"].isoformat() == "2026-07-14"


def test_extract_prebooking_payload_skips_non_prebooking_phase() -> None:
    context = ConversationContext(session_id="sess-prebooking-4", hotel_code="TEST")
    payload = chat_routes._extract_prebooking_booking_payload(
        service_name="Room Booking",
        form_data={
            "guest_phone": "+919876543210",
            "check_in_date": "2026-05-10",
            "check_out_date": "2026-05-12",
        },
        req_meta={"phase": "during_stay"},
        context=context,
    )

    assert payload == {}

