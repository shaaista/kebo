import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.routes.chat import (
    _build_form_ticket_description_fallback,
    _generate_form_ticket_description,
    _sanitize_form_ticket_fields,
)
from config.settings import settings


class _DummyContext:
    def __init__(self) -> None:
        self.pending_data = {"_selected_property_scope_name": "Rohl Test Property"}
        self.booking_property_name = "Rohl Test Property"


def test_sanitize_form_ticket_fields_excludes_internal_and_empty() -> None:
    pairs = _sanitize_form_ticket_fields(
        {
            "flight_number": "aa178",
            "_internal": "x",
            "arrival_time": "",
            "number_of_passengers": 2,
        }
    )
    assert ("Flight Number", "aa178") in pairs
    assert ("Number Of Passengers", "2") in pairs
    assert all(label != "_internal" for label, _ in pairs)


def test_build_form_ticket_description_fallback_contains_context() -> None:
    issue, message = _build_form_ticket_description_fallback(
        service_name="airport transfer",
        service_purpose="Pickup from airport to hotel",
        property_name="Rohl Test Property",
        trigger_label="Terminal",
        trigger_value="Terminal 1",
        merged_form_data={
            "flight_number": "aa178",
            "arrival_time": "00:45",
            "number_of_passengers": "2",
            "_internal": "ignore",
        },
    )
    assert "airport transfer request" in issue.lower()
    assert "terminal 1" in issue.lower()
    assert "rohl test property" in message.lower()
    assert "selected terminal: terminal 1" in message.lower()
    assert "flight number: aa178" in message.lower()


@pytest.mark.asyncio
async def test_generate_form_ticket_description_falls_back_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)

    issue, message = await _generate_form_ticket_description(
        service={
            "id": "airport_transfer",
            "name": "airport transfer",
            "description": "Arrange airport pickup",
            "form_config": {
                "trigger_field": {"id": "terminal", "label": "Terminal"},
            },
        },
        service_name="airport transfer",
        service_id="airport_transfer",
        merged_form_data={
            "terminal": "Terminal 1",
            "flight_number": "aa178",
            "arrival_time": "00:45",
            "number_of_passengers": "2",
        },
        req_meta={"booking_property_name": "Rohl Test Property"},
        context=_DummyContext(),
    )

    assert issue
    assert message
    assert "terminal 1" in message.lower()
    assert "flight number: aa178" in message.lower()

