import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from services.ticketing_service import TicketingService


_DEPARTMENTS = [
    {"department_id": "1", "department_name": "HOUSEKEEPING"},
    {"department_id": "2", "department_name": "IN-ROOM DINING"},
    {"department_id": "3", "department_name": "RESTAURANT"},
    {"department_id": "4", "department_name": "SPA"},
]


class _DummyAsyncSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_department_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    ltr_module = importlib.import_module("integrations.lumira_ticketing_repository")

    async def _fake_fetch_departments_of_entity(_db_session, entity_id=None):
        assert str(entity_id) == "5703"
        return list(_DEPARTMENTS)

    monkeypatch.setattr(
        ltr_module.lumira_ticketing_repository,
        "fetch_departments_of_entity",
        _fake_fetch_departments_of_entity,
    )
    monkeypatch.setattr(
        "models.database.AsyncSessionLocal",
        lambda: _DummyAsyncSessionContext(),
    )


@pytest.mark.asyncio
async def test_apply_department_mapping_falls_back_to_existing_department_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ticketing_debug_log_enabled", False, raising=False)
    _patch_department_lookup(monkeypatch)

    service = TicketingService()

    async def _llm_none(*, payload, departments):
        return None

    monkeypatch.setattr(service, "_llm_resolve_department_for_ticket_payload", _llm_none)

    payload = {
        "issue": "Need replacement towels",
        "message": "Guest requested extra towels.",
        "organisation_id": "5703",
        "department_id": "2",
        "department_allocated": "2",
        "categorization": "custom service request",
    }
    resolved = await service._apply_department_mapping(payload)

    assert resolved["department_id"] == "2"
    assert resolved["department_allocated"] == "2"
    assert resolved["department_manager"] == "IN-ROOM DINING"
    assert resolved["categorization"] == "IN-ROOM DINING"


@pytest.mark.asyncio
async def test_apply_department_mapping_ignores_invalid_existing_department_and_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ticketing_debug_log_enabled", False, raising=False)
    _patch_department_lookup(monkeypatch)

    service = TicketingService()

    async def _llm_none(*, payload, departments):
        return None

    monkeypatch.setattr(service, "_llm_resolve_department_for_ticket_payload", _llm_none)

    payload = {
        "issue": "Need a spa booking",
        "message": "Please schedule massage at 6 PM.",
        "organisation_id": "5703",
        "department_id": "999",
        "department_allocated": "999",
        "categorization": "spa booking",
    }
    resolved = await service._apply_department_mapping(payload)

    assert resolved["department_id"] == "4"
    assert resolved["department_allocated"] == "4"
    assert resolved["department_manager"] == "SPA"


@pytest.mark.asyncio
async def test_apply_department_mapping_prefers_llm_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ticketing_debug_log_enabled", False, raising=False)
    _patch_department_lookup(monkeypatch)

    service = TicketingService()

    async def _llm_pick_restaurant(*, payload, departments):
        for item in departments:
            if str(item.get("department_id")) == "3":
                return item
        return None

    monkeypatch.setattr(service, "_llm_resolve_department_for_ticket_payload", _llm_pick_restaurant)

    payload = {
        "issue": "Please send food menu",
        "message": "Guest asked to order dinner.",
        "organisation_id": "5703",
        "department_id": "1",
        "department_allocated": "1",
        "categorization": "food service",
    }
    resolved = await service._apply_department_mapping(payload)

    assert resolved["department_id"] == "3"
    assert resolved["department_allocated"] == "3"
    assert resolved["department_manager"] == "RESTAURANT"
    assert resolved["categorization"] == "RESTAURANT"
