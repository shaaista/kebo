import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.routes import chat as chat_routes


def test_validate_service_date_boundaries_ignores_alias_time_key_when_schema_present() -> None:
    errors = chat_routes._validate_service_date_boundaries(
        phase_id="pre_checkin",
        form_fields=[
            {"id": "flight", "label": "Flight Number", "type": "text", "required": True},
            {"id": "arrival", "label": "Arrival Time", "type": "time", "required": True},
            {"id": "number", "label": "Number of Passengers", "type": "text", "required": True},
        ],
        merged_form_data={
            # Alias key can appear when frontend/runtime IDs drift.
            "arrival_time": "00:45",
            "stay_checkin_date": "2026-04-10",
            "stay_checkout_date": "2026-04-15",
        },
        req_meta={"phase": "pre_checkin"},
        context=None,
    )

    assert errors == []


def test_validate_service_date_boundaries_still_validates_declared_date_fields() -> None:
    errors = chat_routes._validate_service_date_boundaries(
        phase_id="pre_checkin",
        form_fields=[
            {"id": "pickup_date", "label": "Pickup Date", "type": "date", "required": True},
        ],
        merged_form_data={
            "pickup_date": "00:45",
            "stay_checkin_date": "2026-04-10",
            "stay_checkout_date": "2026-04-15",
        },
        req_meta={"phase": "pre_checkin"},
        context=None,
    )

    assert errors == [
        {
            "field_id": "pickup_date",
            "message": "Please enter a valid date for Pickup Date (YYYY-MM-DD).",
        }
    ]


def test_collect_service_date_field_ids_keeps_fallback_for_schema_less_payloads() -> None:
    ids = chat_routes._collect_service_date_field_ids(
        form_fields=[],
        merged_form_data={
            "arrival_time": "2026-04-10",
        },
    )

    assert ids == ["arrival_time"]
