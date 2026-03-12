import pytest

from schemas.chat import ConversationContext
from services.llm_orchestration_service import llm_orchestration_service


@pytest.mark.asyncio
async def test_answer_first_policy_defers_non_blocking_fields(monkeypatch):
    context = ConversationContext(
        session_id="answer-first-1",
        hotel_code="DEFAULT",
    )
    capabilities = {
        "service_catalog": [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "phase_id": "pre_booking",
                "is_active": True,
                "ticketing_enabled": True,
            },
        ],
        "journey_phases": [
            {"id": "pre_booking", "name": "Pre Booking", "is_active": True},
        ],
        "service_kb_records": [
            {
                "service_id": "room_discovery",
                "facts": [
                    {"text": "We offer Deluxe, Premier, and Suite room categories.", "status": "approved"},
                ],
            }
        ],
    }

    calls = {"count": 0}

    async def _fake_chat_with_json(messages, model=None, temperature=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "normalized_query": "what rooms are available",
                "intent": "room_booking",
                "confidence": 0.9,
                "action": "collect_info",
                "target_service_id": "room_discovery",
                "response_text": "Could you share your budget range?",
                "pending_action": "collect_budget_range",
                "pending_data_updates": {},
                "missing_fields": ["budget_range"],
                "suggested_actions": ["Room types"],
                "use_handler": False,
                "handler_intent": "",
                "interrupt_pending": False,
                "resume_pending": False,
                "cancel_pending": False,
                "requires_human_handoff": False,
                "ticket": {"required": False, "ready_to_create": False},
                "metadata": {},
            }
        return {
            "answers_current_query": True,
            "can_answer_from_context": True,
            "revised_response_text": "Available room categories include Deluxe, Premier, and Suite.",
            "recommended_action": "respond_only",
            "blocking_fields": [],
            "deferrable_fields": ["budget_range"],
            "followup_question": "If you share your budget range, I can narrow down the best match.",
            "reason": "Budget is useful for ranking but not required to answer available categories.",
        }

    monkeypatch.setattr("services.llm_orchestration_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_orchestration_mode", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_no_template_response_mode", True)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_service_agent_enabled", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_answer_first_guard_enabled", True)
    monkeypatch.setattr("services.llm_orchestration_service.llm_client.chat_with_json", _fake_chat_with_json)

    decision = await llm_orchestration_service.orchestrate_turn(
        user_message="what rooms are available",
        context=context,
        capabilities_summary=capabilities,
        memory_snapshot={"summary": "", "facts": {}},
        selected_phase_context={"selected_phase_id": "pre_booking", "selected_phase_name": "Pre Booking"},
    )

    assert decision is not None
    assert decision.action == "respond_only"
    assert decision.answered_current_query is True
    assert decision.missing_fields == []
    assert decision.blocking_fields == []
    assert decision.deferrable_fields == ["budget_range"]
    assert "Available room categories include Deluxe, Premier, and Suite." in decision.response_text


@pytest.mark.asyncio
async def test_answer_first_policy_keeps_collect_info_for_blocking_fields(monkeypatch):
    context = ConversationContext(
        session_id="answer-first-2",
        hotel_code="DEFAULT",
    )
    capabilities = {
        "service_catalog": [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "phase_id": "pre_booking",
                "is_active": True,
                "ticketing_enabled": True,
            },
        ],
        "journey_phases": [
            {"id": "pre_booking", "name": "Pre Booking", "is_active": True},
        ],
        "service_kb_records": [],
    }

    calls = {"count": 0}

    async def _fake_chat_with_json(messages, model=None, temperature=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "normalized_query": "book a room",
                "intent": "room_booking",
                "confidence": 0.9,
                "action": "respond_only",
                "target_service_id": "room_discovery",
                "response_text": "I can help with that.",
                "pending_action": None,
                "pending_data_updates": {},
                "missing_fields": [],
                "suggested_actions": ["Share dates"],
                "use_handler": False,
                "handler_intent": "",
                "interrupt_pending": False,
                "resume_pending": False,
                "cancel_pending": False,
                "requires_human_handoff": False,
                "ticket": {"required": False, "ready_to_create": False},
                "metadata": {},
            }
        return {
            "answers_current_query": False,
            "can_answer_from_context": False,
            "revised_response_text": "Please share your check-in date so I can proceed.",
            "recommended_action": "collect_info",
            "recommended_pending_action": "collect_check_in_date",
            "blocking_fields": ["check_in_date"],
            "deferrable_fields": [],
            "followup_question": "",
            "reason": "Check-in date is required for this booking ask.",
        }

    monkeypatch.setattr("services.llm_orchestration_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_orchestration_mode", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_no_template_response_mode", True)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_service_agent_enabled", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_answer_first_guard_enabled", True)
    monkeypatch.setattr("services.llm_orchestration_service.llm_client.chat_with_json", _fake_chat_with_json)

    decision = await llm_orchestration_service.orchestrate_turn(
        user_message="book a room",
        context=context,
        capabilities_summary=capabilities,
        memory_snapshot={"summary": "", "facts": {}},
        selected_phase_context={"selected_phase_id": "pre_booking", "selected_phase_name": "Pre Booking"},
    )

    assert decision is not None
    assert decision.action == "collect_info"
    assert decision.answered_current_query is False
    assert decision.missing_fields == ["check_in_date"]
    assert decision.blocking_fields == ["check_in_date"]
    assert decision.pending_action == "collect_check_in_date"
