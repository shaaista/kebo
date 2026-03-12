import json

import pytest

from schemas.chat import ChatRequest, ChatResponse, ConversationContext, ConversationState, IntentType
from services.chat_service import ChatService
from services.full_kb_llm_service import full_kb_llm_service
from services.llm_orchestration_service import llm_orchestration_service


@pytest.mark.asyncio
async def test_orchestrator_prompt_contains_phase_policy_snapshot(monkeypatch):
    context = ConversationContext(
        session_id="phase-prompt-orch-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    capabilities = {
        "service_catalog": [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "phase_id": "pre_booking",
                "is_active": True,
                "ticketing_enabled": False,
            },
            {
                "id": "in_room_dining_support",
                "name": "In Room Dining",
                "type": "service",
                "phase_id": "during_stay",
                "is_active": True,
                "ticketing_enabled": True,
            },
        ],
        "journey_phases": [
            {"id": "pre_booking", "name": "Pre Booking", "is_active": True},
            {"id": "during_stay", "name": "During Stay", "is_active": True},
        ],
        "service_kb_records": [],
    }
    memory_snapshot = {"summary": "", "facts": {}}
    captured: dict[str, object] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=None):
        captured["messages"] = messages
        return {
            "normalized_query": "i need food",
            "intent": "order_food",
            "confidence": 0.9,
            "action": "respond_only",
            "target_service_id": "in_room_dining_support",
            "response_text": "In-room dining is available in During Stay phase.",
            "pending_action": None,
            "pending_data_updates": {},
            "missing_fields": [],
            "suggested_actions": ["Room Discovery"],
            "use_handler": False,
            "handler_intent": "",
            "interrupt_pending": False,
            "resume_pending": False,
            "cancel_pending": False,
            "requires_human_handoff": False,
            "ticket": {"required": False, "ready_to_create": False},
            "metadata": {},
        }

    monkeypatch.setattr("services.llm_orchestration_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_orchestration_mode", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_no_template_response_mode", True)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_service_agent_enabled", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_answer_first_guard_enabled", False)
    monkeypatch.setattr("services.llm_orchestration_service.llm_client.chat_with_json", _fake_chat_with_json)

    decision = await llm_orchestration_service.orchestrate_turn(
        user_message="i need food",
        context=context,
        capabilities_summary=capabilities,
        memory_snapshot=memory_snapshot,
        selected_phase_context={"selected_phase_id": "pre_booking", "selected_phase_name": "Pre Booking"},
    )

    assert decision is not None
    messages = captured.get("messages")
    assert isinstance(messages, list) and len(messages) == 2
    system_prompt = str(messages[0]["content"])
    payload = json.loads(str(messages[1]["content"]))
    phase_policy = payload["policy_snapshot"]["phase_service_policy"]

    assert "phase_service_policy" in system_prompt
    assert phase_policy["current_phase_id"] == "pre_booking"
    allowed_ids = {str(item.get("id")) for item in phase_policy["allowed_services"]}
    blocked_ids = {str(item.get("id")) for item in phase_policy["blocked_out_of_phase_services"]}
    assert "room_discovery" in allowed_ids
    assert "in_room_dining_support" in blocked_ids


@pytest.mark.asyncio
async def test_service_agent_forces_source_and_confirmation_pending_action(monkeypatch):
    context = ConversationContext(
        session_id="phase-prompt-service-agent-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="guest_count",
        pending_data={
            "_integration": {"phase": "pre_booking"},
            "request_details": "room booking",
            "check_in_date": "2026-03-12",
            "check_out_date": "2026-03-13",
            "guest_count": "1",
        },
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
                "service_prompt_pack": {
                    "profile": "room_booking",
                    "role": "You are the Room Discovery service assistant.",
                    "professional_behavior": "Collect details clearly and confirm before execution.",
                    "required_slots": [
                        {"id": "request_details", "required": True},
                        {"id": "check_in_date", "required": True},
                        {"id": "check_out_date", "required": True},
                        {"id": "guest_count", "required": True},
                    ],
                    "execution_guard": {"require_required_slots_before_confirm": True},
                },
            },
        ],
        "journey_phases": [
            {"id": "pre_booking", "name": "Pre Booking", "is_active": True},
        ],
        "service_kb_records": [],
    }
    memory_snapshot = {"summary": "", "facts": {}}
    call_count = {"value": 0}

    async def _fake_chat_with_json(messages, model=None, temperature=None):
        call_count["value"] += 1
        if call_count["value"] == 1:
            return {
                "normalized_query": "room booking for one guest",
                "intent": "table_booking",
                "confidence": 0.95,
                "action": "respond_only",
                "target_service_id": "room_discovery",
                "response_text": "Thanks, I noted one guest.",
                "pending_action": None,
                "pending_data_updates": {"guest_count": "1"},
                "missing_fields": [],
                "suggested_actions": [],
                "use_handler": False,
                "handler_intent": "",
                "interrupt_pending": False,
                "resume_pending": False,
                "cancel_pending": False,
                "requires_human_handoff": False,
                "ticket": {"required": False, "ready_to_create": False},
                "metadata": {"source": "orchestrator"},
            }
        if call_count["value"] == 2:
            return {
                "normalized_query": "room booking for one guest",
                "intent": "table_booking",
                "confidence": 0.95,
                "action": "respond_only",
                "target_service_id": "room_discovery",
                "response_text": "Thanks, I noted one guest.",
                "pending_action": None,
                "pending_data_updates": {"guest_count": "1"},
                "missing_fields": [],
                "suggested_actions": [],
                "use_handler": False,
                "handler_intent": "",
                "interrupt_pending": False,
                "resume_pending": False,
                "cancel_pending": False,
                "requires_human_handoff": False,
                "ticket": {"required": False, "ready_to_create": False},
                "metadata": {"source": "orchestrator"},
            }
        raise AssertionError("Unexpected extra LLM call")

    monkeypatch.setattr("services.llm_orchestration_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_orchestration_mode", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_no_template_response_mode", True)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_service_agent_enabled", True)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_answer_first_guard_enabled", False)
    async def _no_suggestions(**kwargs):
        return []
    monkeypatch.setattr(llm_orchestration_service, "_run_next_action_suggestion_agent", _no_suggestions)
    monkeypatch.setattr("services.llm_orchestration_service.llm_client.chat_with_json", _fake_chat_with_json)

    decision = await llm_orchestration_service.orchestrate_turn(
        user_message="only me",
        context=context,
        capabilities_summary=capabilities,
        memory_snapshot=memory_snapshot,
        selected_phase_context={"selected_phase_id": "pre_booking", "selected_phase_name": "Pre Booking"},
    )

    assert decision is not None
    assert call_count["value"] == 2
    assert decision.metadata.get("source") == "service_agent"
    assert decision.metadata.get("service_agent_model_source") == "orchestrator"
    assert decision.pending_action == "confirm_room_booking"
    assert "yes confirm" in str(decision.response_text or "").lower()


@pytest.mark.asyncio
async def test_orchestrator_falls_back_to_next_action_suggestion_agent(monkeypatch):
    context = ConversationContext(
        session_id="phase-prompt-suggestions-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
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
    memory_snapshot = {"summary": "", "facts": {}}
    call_count = {"value": 0}

    async def _fake_chat_with_json(messages, model=None, temperature=None):
        call_count["value"] += 1
        if call_count["value"] == 1:
                return {
                    "normalized_query": "tell me about rooms",
                    "intent": "faq",
                    "confidence": 0.9,
                    "action": "respond_only",
                    "target_service_id": "room_discovery",
                    "response_text": "We have multiple room categories available for your stay.",
                "pending_action": None,
                "pending_data_updates": {},
                "missing_fields": [],
                "suggested_actions": [],
                "use_handler": False,
                "handler_intent": "",
                "interrupt_pending": False,
                "resume_pending": False,
                "cancel_pending": False,
                "requires_human_handoff": False,
                "ticket": {"required": False, "ready_to_create": False},
                "metadata": {"source": "orchestrator"},
            }
        if call_count["value"] == 2:
            return {
                "suggested_actions": [
                    "Show all room types",
                    "Compare room amenities",
                    "Check room availability",
                ]
            }
        raise AssertionError("Unexpected extra LLM call")

    monkeypatch.setattr("services.llm_orchestration_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_orchestration_mode", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_no_template_response_mode", True)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_service_agent_enabled", False)
    monkeypatch.setattr("services.llm_orchestration_service.settings.chat_llm_answer_first_guard_enabled", False)
    monkeypatch.setattr("services.llm_orchestration_service.llm_client.chat_with_json", _fake_chat_with_json)

    decision = await llm_orchestration_service.orchestrate_turn(
        user_message="tell me about rooms",
        context=context,
        capabilities_summary=capabilities,
        memory_snapshot=memory_snapshot,
        selected_phase_context={"selected_phase_id": "pre_booking", "selected_phase_name": "Pre Booking"},
    )

    assert decision is not None
    assert call_count["value"] == 2
    assert decision.suggested_actions == [
        "Show all room types",
        "Compare room amenities",
        "Check room availability",
    ]
    assert decision.metadata.get("suggested_actions_source") == "next_action_agent"


@pytest.mark.asyncio
async def test_full_kb_prompt_contains_phase_service_policy(monkeypatch):
    context = ConversationContext(
        session_id="phase-prompt-fullkb-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    capabilities = {
        "hotel_name": "ICONIQA Demo Hotel",
        "bot_name": "Kai",
        "business_type": "hotel",
        "service_catalog": [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "phase_id": "pre_booking",
                "is_active": True,
                "ticketing_enabled": False,
            },
            {
                "id": "in_room_dining_support",
                "name": "In Room Dining",
                "type": "service",
                "phase_id": "during_stay",
                "is_active": True,
                "ticketing_enabled": True,
            },
        ],
        "journey_phases": [
            {"id": "pre_booking", "name": "Pre Booking", "is_active": True},
            {"id": "during_stay", "name": "During Stay", "is_active": True},
        ],
        "service_kb_records": [],
        "intents": [],
        "faq_bank": [],
        "tools": [],
    }
    captured: dict[str, object] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=None):
        captured["messages"] = messages
        return {
            "normalized_query": "i need food",
            "intent": "faq",
            "confidence": 0.92,
            "next_state": "idle",
            "pending_action": None,
            "pending_data": {},
            "pending_data_updates": {},
            "clear_pending_data": False,
            "room_number": None,
            "service_id": "in_room_dining_support",
            "requires_ticket": False,
            "ticket_reason": "",
            "ticket_category": "",
            "ticket_sub_category": "",
            "ticket_priority": "",
            "ticket_issue": "",
            "suggested_actions": ["Room Discovery", "Ask another question"],
            "assistant_response": "In-room dining is available in During Stay phase.",
        }

    monkeypatch.setattr("services.full_kb_llm_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr(
        full_kb_llm_service,
        "_load_full_kb_text",
        lambda tenant_id: ("Sample KB text for testing.", ["sample.txt"]),
    )
    monkeypatch.setattr("services.full_kb_llm_service.llm_client.chat_with_json", _fake_chat_with_json)

    result = await full_kb_llm_service.run_turn(
        user_message="i need food",
        context=context,
        capabilities_summary=capabilities,
        memory_snapshot={"summary": "", "facts": {}, "recent_changes": []},
    )

    assert result.intent.value == "faq"
    messages = captured.get("messages")
    assert isinstance(messages, list) and len(messages) == 2
    system_prompt = str(messages[0]["content"])
    llm_input = json.loads(str(messages[1]["content"]))
    phase_policy = llm_input["admin_config"]["phase_service_policy"]

    assert "phase_service_policy.allowed_services" in system_prompt
    assert phase_policy["current_phase_id"] == "pre_booking"
    allowed_ids = {str(item.get("id")) for item in phase_policy["allowed_services"]}
    blocked_ids = {str(item.get("id")) for item in phase_policy["blocked_out_of_phase_services"]}
    assert "room_discovery" in allowed_ids
    assert "in_room_dining_support" in blocked_ids


@pytest.mark.asyncio
async def test_no_template_mode_precedes_full_kb_runtime(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-no-template-priority-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_no_template(**kwargs):
        request = kwargs["request"]
        return ChatResponse(
            session_id=request.session_id,
            message="LLM orchestration path used.",
            intent=IntentType.FAQ,
            confidence=0.9,
            state=ConversationState.IDLE,
            suggested_actions=[],
            metadata={"response_source": "llm_orchestration"},
        )

    async def _should_not_call_full_kb(**kwargs):
        raise AssertionError("full_kb deterministic runtime should be bypassed in no-template mode")

    monkeypatch.setattr("services.chat_service.settings.chat_no_template_response_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(service, "_process_policy_verified_llm_message", _fake_no_template)
    monkeypatch.setattr(service, "_process_full_kb_llm_message", _should_not_call_full_kb)

    response = await service.process_message(
        ChatRequest(
            session_id="phase-no-template-priority-1",
            message="i need food",
            hotel_code="DEFAULT",
        )
    )
    assert response.message == "LLM orchestration path used."
    assert response.metadata.get("response_source") == "llm_orchestration"
