import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.routes import chat as chat_routes
from schemas.chat import ChatRequest, ConversationContext
from services.chat_service import ChatService
from services.llm_orchestration_service import LLMOrchestrationService
from services.ticketing_service import ticketing_service


@pytest.mark.asyncio
async def test_ingest_metadata_keeps_guest_and_user_ids_separate() -> None:
    service = ChatService()
    context = ConversationContext(session_id="sess-guest-user", hotel_code="TEST")
    request = ChatRequest(
        session_id="sess-guest-user",
        message="hello",
        hotel_code="TEST",
        metadata={
            "guest_id": "921346",
            "user_id": "+911234567890",
            "entity_id": "5703",
        },
    )

    await service._ingest_request_metadata(context, request, db_session=None)
    integration = context.pending_data.get("_integration", {})

    assert integration.get("guest_id") == "921346"
    assert integration.get("user_id") == "+911234567890"
    assert context.pending_data.get("guest_id") == "921346"


@pytest.mark.asyncio
async def test_ingest_metadata_does_not_promote_user_id_to_guest_id() -> None:
    service = ChatService()
    context = ConversationContext(session_id="sess-user-only", hotel_code="TEST")
    request = ChatRequest(
        session_id="sess-user-only",
        message="hello",
        hotel_code="TEST",
        metadata={
            "user_id": "+911234567890",
            "entity_id": "5703",
        },
    )

    await service._ingest_request_metadata(context, request, db_session=None)
    integration = context.pending_data.get("_integration", {})

    assert integration.get("user_id") == "+911234567890"
    assert not str(integration.get("guest_id") or "").strip()
    assert "guest_id" not in context.pending_data


@pytest.mark.asyncio
async def test_ingest_metadata_enriches_guest_id_from_phone_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ChatService()
    context = ConversationContext(session_id="sess-phone-lookup", hotel_code="TEST")
    request = ChatRequest(
        session_id="sess-phone-lookup",
        message="hello",
        hotel_code="TEST",
        guest_phone="+91 9988776655",
        metadata={
            "entity_id": "5703",
        },
    )

    async def fake_fetch_guest_profile(_db_session, *, entity_id=None, guest_id=None, guest_phone=None):
        assert str(entity_id) == "5703"
        assert "9988776655" in str(guest_phone)
        return {
            "guest_id": "777001",
            "guest_name": "Rahul",
            "room_number": "1204",
            "entity_id": "5703",
        }

    monkeypatch.setattr(
        "services.chat_service.lumira_ticketing_repository.fetch_guest_profile",
        fake_fetch_guest_profile,
    )

    await service._ingest_request_metadata(context, request, db_session=object())
    integration = context.pending_data.get("_integration", {})

    assert integration.get("guest_id") == "777001"
    assert context.pending_data.get("guest_id") == "777001"
    assert context.guest_name == "Rahul"
    assert context.room_number == "1204"


def test_ticket_payload_prefers_real_guest_id_when_present(caplog: pytest.LogCaptureFixture) -> None:
    context = ConversationContext(
        session_id="sess-real-guest",
        hotel_code="5703",
        pending_data={
            "_integration": {
                "guest_id": "921348",
                "user_id": "+911234567890",
                "entity_id": "5703",
            }
        },
    )

    with caplog.at_level("WARNING"):
        payload = ticketing_service.build_lumira_ticket_payload(
            context=context,
            issue="Need help",
            message="Need help",
            phase="During Stay",
        )

    assert payload["guest_id"] == "921348"
    assert "identity mismatch" in caplog.text.lower()


def test_ticket_payload_with_only_user_id_uses_synthetic_guest_id(caplog: pytest.LogCaptureFixture) -> None:
    context = ConversationContext(
        session_id="sess-user-only-ticket",
        hotel_code="5703",
        pending_data={
            "_integration": {
                "user_id": "+911234567890",
                "entity_id": "5703",
            }
        },
    )

    with caplog.at_level("WARNING"):
        payload = ticketing_service.build_lumira_ticket_payload(
            context=context,
            issue="Need callback",
            message="Need callback",
            phase="Pre Booking",
        )

    assert payload["guest_id"]
    assert payload["guest_id"].isdigit()
    assert payload["guest_id"] != "+911234567890"
    assert "synthetic fallback" in caplog.text.lower()


def test_ticket_payload_with_only_phone_uses_synthetic_guest_id() -> None:
    context = ConversationContext(
        session_id="sess-phone-only-ticket",
        hotel_code="5703",
        guest_phone="+91 9988776655",
        pending_data={
            "_integration": {
                "entity_id": "5703",
                "guest_phone": "+91 9988776655",
            }
        },
    )

    payload = ticketing_service.build_lumira_ticket_payload(
        context=context,
        issue="Need follow-up",
        message="Need follow-up",
        phase="During Stay",
    )

    assert payload["guest_id"]
    assert payload["guest_id"].isdigit()


def test_synthetic_guest_id_stable_across_phase_changes_same_session() -> None:
    base_integration = {
        "user_id": "+911234567890",
        "entity_id": "5703",
        "conversation_id": "conv-abc",
    }
    context_pre = ConversationContext(
        session_id="sess-phase-stable",
        hotel_code="5703",
        pending_data={"_integration": dict(base_integration)},
    )
    context_during = ConversationContext(
        session_id="sess-phase-stable",
        hotel_code="5703",
        pending_data={"_integration": dict(base_integration)},
    )

    payload_pre = ticketing_service.build_lumira_ticket_payload(
        context=context_pre,
        issue="Need booking help",
        message="Need booking help",
        phase="Pre Booking",
    )
    payload_during = ticketing_service.build_lumira_ticket_payload(
        context=context_during,
        issue="Need in-stay help",
        message="Need in-stay help",
        phase="During Stay",
    )

    assert payload_pre["guest_id"]
    assert payload_pre["guest_id"] == payload_during["guest_id"]


def test_phase_test_profiles_still_merge_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    custom_json = json.dumps(
        {
            "pre_checkin": {
                "guest_id": "100001",
                "entity_id": "5703",
                "organisation_id": "5703",
                "ticket_source": "whatsapp_bot",
            }
        }
    )
    monkeypatch.setattr(chat_routes.settings, "chat_test_phase_profiles_json", custom_json, raising=False)

    profiles = chat_routes._load_chat_test_phase_profiles()
    assert profiles["pre_checkin"]["guest_id"] == "100001"
    assert "during_stay" in profiles
    assert "post_checkout" in profiles


@pytest.mark.asyncio
async def test_booking_property_resolution_sets_stay_pin_for_stay_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService()
    context = ConversationContext(session_id="sess-stay-pin", hotel_code="TEST")
    request = ChatRequest(
        session_id="sess-stay-pin",
        message="hello",
        hotel_code="TEST",
        metadata={
            "booking_phase": "during_stay",
            "booking_property_name": "Shimla Property",
        },
    )

    async def fake_resolve_property_scope_ids(*, property_hints, max_sources=50, limit=4):
        assert property_hints == ["Shimla Property"]
        return {
            "resolved": [
                {
                    "id": "shimla_property",
                    "name": "Shimla Property",
                    "input": "Shimla Property",
                    "match_type": "label_match",
                }
            ],
            "unresolved_hints": [],
            "property_manifest": [],
        }

    monkeypatch.setattr(
        "services.chat_service.config_service.resolve_property_scope_ids",
        fake_resolve_property_scope_ids,
    )

    await service._ingest_request_metadata(context, request, db_session=None)

    assert context.pending_data.get("_stay_property_id") == "shimla_property"
    assert context.pending_data.get("_stay_property_name") == "Shimla Property"
    assert context.pending_data.get("_active_property_ids") == ["shimla_property"]
    assert context.pending_data.get("_stay_property_pin_enabled") is True


@pytest.mark.asyncio
async def test_pre_booking_property_behavior_remains_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService()
    context = ConversationContext(session_id="sess-prebooking-stable", hotel_code="TEST")
    request = ChatRequest(
        session_id="sess-prebooking-stable",
        message="hello",
        hotel_code="TEST",
        metadata={
            "booking_phase": "pre_booking",
            "booking_property_name": "Shimla Property",
        },
    )

    async def fake_resolve_property_scope_ids(*, property_hints, max_sources=50, limit=4):
        return {
            "resolved": [
                {
                    "id": "shimla_property",
                    "name": "Shimla Property",
                    "input": "Shimla Property",
                    "match_type": "label_match",
                }
            ],
            "unresolved_hints": [],
            "property_manifest": [],
        }

    monkeypatch.setattr(
        "services.chat_service.config_service.resolve_property_scope_ids",
        fake_resolve_property_scope_ids,
    )

    await service._ingest_request_metadata(context, request, db_session=None)

    # Pre-booking behavior stays as before: active scope uses raw property label.
    assert context.pending_data.get("_active_property_ids") == ["Shimla Property"]
    assert context.pending_data.get("_selected_property_scope_name") == "Shimla Property"
    assert context.pending_data.get("_stay_property_id") == "shimla_property"
    assert context.pending_data.get("_stay_property_pin_enabled") is False


def test_stay_property_pin_reapplies_when_llm_switches_to_other_property() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-orch-pin",
        hotel_code="TEST",
        pending_data={
            "_stay_property_id": "shimla_property",
            "_stay_property_name": "Shimla Property",
        },
    )

    pinned_ids, applied = service._apply_stay_property_pin(
        context=context,
        selected_phase_id="during_stay",
        property_ids=["goa_property"],
        source="unit_test",
    )

    assert applied is True
    assert pinned_ids[0] == "shimla_property"
    assert "goa_property" in pinned_ids


def test_stay_property_pin_not_forced_in_pre_booking() -> None:
    service = LLMOrchestrationService()
    context = ConversationContext(
        session_id="sess-no-pin-prebooking",
        hotel_code="TEST",
        pending_data={
            "_stay_property_id": "shimla_property",
            "_stay_property_name": "Shimla Property",
        },
    )

    pinned_ids, applied = service._apply_stay_property_pin(
        context=context,
        selected_phase_id="pre_booking",
        property_ids=["goa_property"],
        source="unit_test",
    )

    assert applied is False
    assert pinned_ids == ["goa_property"]
