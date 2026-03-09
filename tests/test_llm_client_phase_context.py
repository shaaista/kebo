import pytest

from llm.client import LLMClient


@pytest.mark.asyncio
async def test_classify_intent_prompt_includes_selected_phase(monkeypatch):
    client = LLMClient()
    captured: dict[str, str] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=0.1):
        _ = model, temperature
        if isinstance(messages, list) and messages:
            captured["system_prompt"] = str(messages[0].get("content") or "")
        return {"intent": "faq", "confidence": 0.9, "entities": {}}

    monkeypatch.setattr(client, "chat_with_json", _fake_chat_with_json)
    monkeypatch.setattr("llm.client.config_service.get_prompts", lambda: {})
    monkeypatch.setattr("llm.client.config_service.get_nlu_policy", lambda: {"dos": [], "donts": []})

    _ = await client.classify_intent(
        "hi there",
        [],
        {
            "hotel_name": "ICONIQA Demo Hotel",
            "business_type": "hotel",
            "guest_name": "Guest",
            "state": "idle",
            "pending_action": None,
            "enabled_intents": ["faq"],
            "selected_phase_id": "pre_booking",
            "selected_phase_name": "Pre Booking",
            "intent_catalog": [],
            "service_catalog": [],
            "faq_bank": [],
            "tools": [],
        },
    )

    assert "Selected Phase: Pre Booking (pre_booking)" in str(captured.get("system_prompt") or "")


@pytest.mark.asyncio
async def test_generate_response_prompt_includes_selected_phase(monkeypatch):
    client = LLMClient()
    captured: dict[str, str] = {}

    async def _fake_chat(messages, model=None, temperature=0.7, max_tokens=None):
        _ = model, temperature, max_tokens
        if isinstance(messages, list) and messages:
            captured["system_prompt"] = str(messages[0].get("content") or "")
        return "Hello!"

    monkeypatch.setattr(client, "chat", _fake_chat)
    monkeypatch.setattr("llm.client.config_service.get_prompts", lambda: {})
    monkeypatch.setattr("llm.client.config_service.get_nlu_policy", lambda: {"dos": [], "donts": []})

    response = await client.generate_response(
        user_message="hello",
        intent="faq",
        entities={},
        conversation_history=[],
        context={
            "hotel_name": "ICONIQA Demo Hotel",
            "hotel_code": "DEFAULT",
            "bot_name": "Kebo",
            "city": "Mumbai",
            "business_type": "hotel",
            "guest_name": "Guest",
            "room_number": "",
            "state": "idle",
            "pending_action": None,
            "selected_phase_id": "pre_booking",
            "selected_phase_name": "Pre Booking",
            "capabilities": {
                "services": {},
                "capabilities": {},
                "service_catalog": [],
                "faq_bank": [],
                "tools": [],
            },
        },
    )

    assert response == "Hello!"
    assert "Selected Phase: Pre Booking (pre_booking)" in str(captured.get("system_prompt") or "")


@pytest.mark.asyncio
async def test_classify_intent_prompt_uses_full_history_not_last_five(monkeypatch):
    client = LLMClient()
    captured: dict[str, str] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=0.1):
        _ = model, temperature
        if isinstance(messages, list) and messages:
            captured["system_prompt"] = str(messages[0].get("content") or "")
        return {"intent": "faq", "confidence": 0.9, "entities": {}}

    monkeypatch.setattr(client, "chat_with_json", _fake_chat_with_json)
    monkeypatch.setattr("llm.client.config_service.get_prompts", lambda: {})
    monkeypatch.setattr("llm.client.config_service.get_nlu_policy", lambda: {"dos": [], "donts": []})

    history = [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": "turn two"},
        {"role": "user", "content": "turn three"},
        {"role": "assistant", "content": "turn four"},
        {"role": "user", "content": "turn five"},
        {"role": "assistant", "content": "turn six"},
        {"role": "user", "content": "turn seven"},
    ]

    _ = await client.classify_intent(
        "latest turn",
        history,
        {
            "hotel_name": "ICONIQA Demo Hotel",
            "business_type": "hotel",
            "guest_name": "Guest",
            "state": "idle",
            "pending_action": None,
            "enabled_intents": ["faq"],
            "selected_phase_id": "pre_booking",
            "selected_phase_name": "Pre Booking",
            "intent_catalog": [],
            "service_catalog": [],
            "faq_bank": [],
            "tools": [],
        },
    )

    prompt = str(captured.get("system_prompt") or "")
    assert "USER: turn one" in prompt
    assert "USER: turn seven" in prompt


@pytest.mark.asyncio
async def test_generate_response_prompt_uses_full_history_not_last_six(monkeypatch):
    client = LLMClient()
    captured: dict[str, str] = {}

    async def _fake_chat(messages, model=None, temperature=0.7, max_tokens=None):
        _ = model, temperature, max_tokens
        if isinstance(messages, list) and messages:
            captured["system_prompt"] = str(messages[0].get("content") or "")
        return "Hello!"

    monkeypatch.setattr(client, "chat", _fake_chat)
    monkeypatch.setattr("llm.client.config_service.get_prompts", lambda: {})
    monkeypatch.setattr("llm.client.config_service.get_nlu_policy", lambda: {"dos": [], "donts": []})

    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "fourth"},
        {"role": "user", "content": "fifth"},
        {"role": "assistant", "content": "sixth"},
        {"role": "user", "content": "seventh"},
    ]

    response = await client.generate_response(
        user_message="hello",
        intent="faq",
        entities={},
        conversation_history=history,
        context={
            "hotel_name": "ICONIQA Demo Hotel",
            "hotel_code": "DEFAULT",
            "bot_name": "Kebo",
            "city": "Mumbai",
            "business_type": "hotel",
            "guest_name": "Guest",
            "room_number": "",
            "state": "idle",
            "pending_action": None,
            "selected_phase_id": "pre_booking",
            "selected_phase_name": "Pre Booking",
            "capabilities": {
                "services": {},
                "capabilities": {},
                "service_catalog": [],
                "faq_bank": [],
                "tools": [],
            },
        },
    )

    assert response == "Hello!"
    prompt = str(captured.get("system_prompt") or "")
    assert "USER: first" in prompt
    assert "USER: seventh" in prompt
