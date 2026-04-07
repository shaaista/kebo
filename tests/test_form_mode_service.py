import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.routes.chat import _inject_form_trigger
from schemas.chat import ChatResponse, ConversationState
from services.form_mode_service import (
    canonicalize_trigger_pending_data,
    infer_trigger_value_from_message,
    strip_form_confirmation_instructions,
)


def test_canonicalize_trigger_pending_data_maps_alias_to_canonical_key() -> None:
    cleaned, matched_key, matched_value = canonicalize_trigger_pending_data(
        {"terminal_choice": "Terminal 2", "service_id": "airport_transfer"},
        "terminal",
        "Terminal",
    )

    assert matched_key == "terminal_choice"
    assert matched_value == "Terminal 2"
    assert cleaned["terminal"] == "Terminal 2"
    assert "terminal_choice" not in cleaned
    assert cleaned["service_id"] == "airport_transfer"


def test_strip_form_confirmation_instructions_keeps_real_clarification_prompt() -> None:
    original = (
        "Please confirm which terminal you will be arriving at: Terminal 1 or Terminal 2. "
        "Please reply 'yes confirm' to confirm."
    )

    cleaned = strip_form_confirmation_instructions(original)

    assert "please confirm which terminal" in cleaned.lower()
    assert "yes confirm" not in cleaned.lower()


def test_infer_trigger_value_from_message_uses_candidates() -> None:
    key, value = infer_trigger_value_from_message(
        {
            "room_type_candidates": [
                "Premier King Room",
                "Ultimate Suite",
            ]
        },
        "room_type",
        "Room Type",
        "Can I book the ultimate suite?",
    )

    assert key == "room_type"
    assert value == "Ultimate Suite"


@pytest.mark.asyncio
async def test_inject_form_trigger_accepts_alias_trigger_key_and_strips_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    config_service_module = importlib.import_module("services.config_service")
    form_fields_service_module = importlib.import_module("services.form_fields_service")

    service = {
        "id": "airport_transfer",
        "ticketing_enabled": True,
        "ticketing_mode": "form",
        "form_config": {
            "trigger_field": {"id": "terminal", "label": "Terminal"},
            "fields": [
                {"id": "flight_number", "label": "Flight Number", "type": "text", "required": True},
                {"id": "arrival_time", "label": "Arrival Time", "type": "time", "required": True},
            ],
        },
    }

    async def fake_extract_form_fields(_service: dict) -> list[dict]:
        return [
            {"id": "flight_number", "label": "Flight Number", "type": "text", "required": True},
            {"id": "arrival_time", "label": "Arrival Time", "type": "time", "required": True},
        ]

    monkeypatch.setattr(config_service_module.config_service, "get_service", lambda _service_id: service)
    monkeypatch.setattr(form_fields_service_module, "extract_form_fields", fake_extract_form_fields)

    response = ChatResponse(
        session_id="session-1",
        message=(
            "Great choice! The Toyota Innova Hycross is perfect for a comfortable ride. "
            "Please fill in the booking details below. Please reply 'yes confirm' to confirm."
        ),
        state=ConversationState.AWAITING_INFO,
        metadata={
            "pending_action": "collect_form_details",
            "entities": {
                "target_service_id": "airport_transfer",
                "orchestration_action": "collect_info",
                "pending_action": "collect_form_details",
                "pending_data": {
                    "terminal_choice": "Terminal 2",
                    "service_id": "airport_transfer",
                },
                "missing_fields": ["terminal_choice"],
            },
        },
    )

    await _inject_form_trigger(response)

    assert response.metadata["form_trigger"] is True
    assert response.metadata["form_service_id"] == "airport_transfer"
    assert [field["id"] for field in response.metadata["form_fields"]] == [
        "flight_number",
        "arrival_time",
    ]
    assert "yes confirm" not in response.message.lower()
    assert "please fill in the details below" in response.message.lower()
    assert response.metadata["entities"]["pending_data"]["terminal"] == "Terminal 2"
    assert "terminal_choice" not in response.metadata["entities"]["pending_data"]
    assert response.metadata["entities"]["missing_fields"] == []


@pytest.mark.asyncio
async def test_inject_form_trigger_forces_form_when_trigger_resolved_even_if_respond_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_service_module = importlib.import_module("services.config_service")
    form_fields_service_module = importlib.import_module("services.form_fields_service")

    service = {
        "id": "room_booking_request",
        "ticketing_enabled": True,
        "ticketing_mode": "form",
        "form_config": {
            "trigger_field": {"id": "room_type", "label": "Room Type"},
            "fields": [
                {"id": "full_name", "label": "Full Name", "type": "text", "required": True},
                {"id": "phone", "label": "Phone", "type": "tel", "required": True},
            ],
        },
    }

    async def fake_extract_form_fields(_service: dict) -> list[dict]:
        return list(service["form_config"]["fields"])

    monkeypatch.setattr(config_service_module.config_service, "get_service", lambda _service_id: service)
    monkeypatch.setattr(form_fields_service_module, "extract_form_fields", fake_extract_form_fields)

    response = ChatResponse(
        session_id="session-respond-only",
        message="Yes, you can book the Ultimate Suite.",
        state=ConversationState.IDLE,
        metadata={
            "entities": {
                "target_service_id": "room_booking_request",
                "orchestration_action": "respond_only",
                "pending_action": "",
                "pending_data": {
                    "room_type_candidates": ["Premier King Room", "Ultimate Suite"],
                },
                "missing_fields": [],
            },
        },
    )

    await _inject_form_trigger(response, user_message="Can I book the Ultimate Suite?")

    assert response.metadata["form_trigger"] is True
    assert response.metadata["form_service_id"] == "room_booking_request"
    assert response.metadata["entities"]["pending_data"]["room_type"] == "Ultimate Suite"
    assert response.metadata["entities"]["orchestration_action"] == "collect_info"
    assert response.metadata["entities"]["pending_action"] == "collect_form_details"


@pytest.mark.asyncio
async def test_inject_form_trigger_skips_text_mode_services(monkeypatch: pytest.MonkeyPatch) -> None:
    config_service_module = importlib.import_module("services.config_service")
    form_fields_service_module = importlib.import_module("services.form_fields_service")

    service = {
        "id": "restaurant_booking",
        "ticketing_enabled": True,
        "ticketing_mode": "text",
        "form_config": {
            "trigger_field": {"id": "table_type", "label": "Table Type"},
            "fields": [
                {"id": "date", "label": "Date", "type": "date", "required": True},
            ],
        },
    }

    async def fake_extract_form_fields(_service: dict) -> list[dict]:
        return [{"id": "date", "label": "Date", "type": "date", "required": True}]

    monkeypatch.setattr(config_service_module.config_service, "get_service", lambda _service_id: service)
    monkeypatch.setattr(form_fields_service_module, "extract_form_fields", fake_extract_form_fields)

    response = ChatResponse(
        session_id="session-2",
        message="Please reply 'yes confirm' to confirm.",
        state=ConversationState.AWAITING_INFO,
        metadata={
            "entities": {
                "target_service_id": "restaurant_booking",
                "orchestration_action": "collect_info",
                "pending_action": "confirm_booking",
                "pending_data": {"table_type": "Window"},
                "missing_fields": [],
            },
        },
    )

    await _inject_form_trigger(response)

    assert "form_trigger" not in response.metadata
