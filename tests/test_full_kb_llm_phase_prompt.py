import json

import pytest

from schemas.chat import ConversationContext, ConversationState, MessageRole
from services.full_kb_llm_service import FullKBLLMService


@pytest.mark.asyncio
async def test_full_kb_prompt_and_payload_include_selected_phase(monkeypatch):
    service = FullKBLLMService()
    context = ConversationContext(
        session_id="fullkb-phase-prompt-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    captured: dict[str, str] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=0.0):
        _ = model, temperature
        if isinstance(messages, list) and len(messages) >= 2:
            captured["system_prompt"] = str(messages[0].get("content") or "")
            captured["user_payload"] = str(messages[1].get("content") or "")
        return {
            "normalized_query": "do you have a pool",
            "intent": "faq",
            "confidence": 0.9,
            "next_state": "idle",
            "pending_action": None,
            "pending_data": {},
            "room_number": None,
            "requires_ticket": False,
            "ticket_reason": "",
            "suggested_actions": ["Ask another question"],
            "assistant_response": "Yes, we have a pool.",
        }

    monkeypatch.setattr("services.full_kb_llm_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr(
        service,
        "_load_full_kb_text",
        lambda tenant_id: ("Pool details are available.", [f"{tenant_id}/kb.txt"]),
    )
    monkeypatch.setattr(
        "services.full_kb_llm_service.llm_client.chat_with_json",
        _fake_chat_with_json,
    )

    result = await service.run_turn(
        user_message="do u have pool",
        context=context,
        capabilities_summary={
            "hotel_name": "ICONIQA Demo Hotel",
            "bot_name": "Kebo",
            "city": "Mumbai",
            "business_type": "hotel",
            "intents": [],
            "service_catalog": [],
            "faq_bank": [],
            "tools": [],
            "journey_phases": [{"id": "pre_booking", "name": "Pre Booking"}],
        },
        memory_snapshot={},
    )

    assert result.status == "success"
    assert "Current selected user journey phase: 'Pre Booking' (pre_booking)." in str(
        captured.get("system_prompt") or ""
    )
    assert "Do not use your own pretrained/world knowledge when answering." in str(
        captured.get("system_prompt") or ""
    )
    assert "If assistant_response says a ticket was created/raised/escalated/forwarded" in str(
        captured.get("system_prompt") or ""
    )
    payload = json.loads(str(captured.get("user_payload") or "{}"))
    assert payload.get("selected_phase_id") == "pre_booking"
    assert payload.get("selected_phase_name") == "Pre Booking"


@pytest.mark.asyncio
async def test_full_kb_payload_includes_full_available_history(monkeypatch):
    service = FullKBLLMService()
    context = ConversationContext(
        session_id="fullkb-history-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    for index in range(1, 13):
        role = MessageRole.USER if index % 2 else MessageRole.ASSISTANT
        context.add_message(role=role, content=f"message {index}")

    captured: dict[str, str] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=0.0):
        _ = model, temperature
        if isinstance(messages, list) and len(messages) >= 2:
            captured["user_payload"] = str(messages[1].get("content") or "")
        return {
            "normalized_query": "test",
            "intent": "faq",
            "confidence": 0.9,
            "next_state": "idle",
            "pending_action": None,
            "pending_data": {},
            "room_number": None,
            "requires_ticket": False,
            "ticket_reason": "",
            "suggested_actions": ["Ask another question"],
            "assistant_response": "ok",
        }

    monkeypatch.setattr("services.full_kb_llm_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.full_kb_llm_service.settings.full_kb_llm_max_history_messages", 0)
    service.max_history_messages = 0
    monkeypatch.setattr(
        service,
        "_load_full_kb_text",
        lambda tenant_id: ("Pool details are available.", [f"{tenant_id}/kb.txt"]),
    )
    monkeypatch.setattr(
        "services.full_kb_llm_service.llm_client.chat_with_json",
        _fake_chat_with_json,
    )

    result = await service.run_turn(
        user_message="do u have pool",
        context=context,
        capabilities_summary={
            "hotel_name": "ICONIQA Demo Hotel",
            "bot_name": "Kebo",
            "city": "Mumbai",
            "business_type": "hotel",
            "intents": [],
            "service_catalog": [],
            "faq_bank": [],
            "tools": [],
            "journey_phases": [{"id": "pre_booking", "name": "Pre Booking"}],
        },
        memory_snapshot={},
    )

    assert result.status == "success"
    payload = json.loads(str(captured.get("user_payload") or "{}"))
    history = payload.get("recent_history") or []
    assert isinstance(history, list)
    assert len(history) >= 12


@pytest.mark.asyncio
async def test_full_kb_payload_includes_service_knowledge_packs(monkeypatch):
    service = FullKBLLMService()
    context = ConversationContext(
        session_id="fullkb-service-pack-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "during_stay"}},
    )
    captured: dict[str, str] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=0.0):
        _ = model, temperature
        if isinstance(messages, list) and len(messages) >= 2:
            captured["system_prompt"] = str(messages[0].get("content") or "")
            captured["user_payload"] = str(messages[1].get("content") or "")
        return {
            "normalized_query": "tell me spa timings",
            "intent": "faq",
            "confidence": 0.9,
            "next_state": "idle",
            "pending_action": None,
            "pending_data": {},
            "room_number": None,
            "requires_ticket": False,
            "ticket_reason": "",
            "suggested_actions": ["Ask another question"],
            "assistant_response": "Spa timings are 9 AM to 11 PM.",
        }

    monkeypatch.setattr("services.full_kb_llm_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr(
        service,
        "_load_full_kb_text",
        lambda tenant_id: ("Spa details are available.", [f"{tenant_id}/kb.txt"]),
    )
    monkeypatch.setattr("services.full_kb_llm_service.llm_client.chat_with_json", _fake_chat_with_json)

    result = await service.run_turn(
        user_message="tell me spa timings",
        context=context,
        capabilities_summary={
            "hotel_name": "ICONIQA Demo Hotel",
            "bot_name": "Kebo",
            "city": "Mumbai",
            "business_type": "hotel",
            "intents": [],
            "service_catalog": [
                {
                    "id": "spa_booking",
                    "name": "Spa Booking",
                    "type": "service",
                    "description": "Book spa sessions",
                    "phase_id": "during_stay",
                    "ticketing_enabled": True,
                }
            ],
            "service_kb_records": [
                {
                    "id": "spa_booking_kb",
                    "service_id": "spa_booking",
                    "plugin_id": "spa_booking_agent",
                    "strict_mode": True,
                    "version": 3,
                    "facts": [
                        {
                            "id": "fact_1",
                            "text": "Spa timings are 9 AM to 11 PM daily.",
                            "status": "approved",
                            "origin": "auto",
                            "source": "kb_source:hotel_faq.md",
                        }
                    ],
                }
            ],
            "faq_bank": [],
            "tools": [],
            "journey_phases": [{"id": "during_stay", "name": "During Stay"}],
        },
        memory_snapshot={},
    )

    assert result.status == "success"
    system_prompt = str(captured.get("system_prompt") or "")
    assert "service_knowledge_packs facts for that service first" in system_prompt
    payload = json.loads(str(captured.get("user_payload") or "{}"))
    admin_config = payload.get("admin_config") or {}
    packs = admin_config.get("service_knowledge_packs") or []
    assert isinstance(packs, list)
    assert any(str(pack.get("service_id") or "") == "spa_booking" for pack in packs)
    first_pack = next(pack for pack in packs if str(pack.get("service_id") or "") == "spa_booking")
    assert any("spa timings" in str(text).lower() for text in (first_pack.get("facts") or []))
