import pytest

from handlers.base_handler import HandlerResult
from schemas.chat import ChatRequest, ConversationContext, ConversationState, IntentResult, IntentType, MessageRole
from services.chat_service import ChatService
from services.full_kb_llm_service import FullKBLLMResult
from services.ticketing_agent_service import TicketingAgentDecision
from services.ticketing_service import TicketingResult


def test_food_catalog_info_query_detection_handles_natural_phrasing():
    service = ChatService()
    assert service._looks_like_food_catalog_info_query("i am hungry what options are available for me to eat")
    assert service._looks_like_food_catalog_info_query("do you serve food to my room")
    assert service._looks_like_food_catalog_info_query("show me your food menus")
    assert not service._looks_like_food_catalog_info_query("i want margherita pizza")


def test_policy_timing_query_detection_handles_checkin_variants():
    service = ChatService()
    assert service._looks_like_policy_or_timing_query("what is the checkin time")
    assert service._looks_like_policy_or_timing_query("what are the check in time chcek out time")
    assert not service._looks_like_policy_or_timing_query("book a table at 9 pm")


def test_match_greeting_response_returns_welcome_message():
    service = ChatService()
    context = ConversationContext(session_id="greet-1", hotel_code="DEFAULT", state=ConversationState.IDLE)
    matched = service._match_greeting_response(
        "hi",
        {"welcome_message": "Welcome to ICONIQA Demo Hotel. How may I assist you today?"},
        context,
    )
    assert matched is not None
    assert matched["match_type"] == "greeting"
    assert "Welcome to ICONIQA Demo Hotel" in matched["response_text"]


@pytest.mark.asyncio
async def test_classify_intent_routes_policy_timing_query_to_faq_without_llm(monkeypatch):
    service = ChatService()

    async def _should_not_run(*args, **kwargs):
        raise AssertionError("LLM classification should not be invoked for deterministic policy/timing queries")

    monkeypatch.setattr("services.chat_service.llm_client.classify_intent", _should_not_run)

    result = await service._classify_intent(
        "what are the check in time chcek out time",
        [],
        {"state": "idle", "pending_action": None},
    )

    assert result.intent == IntentType.FAQ
    assert result.entities.get("requested_topic") == "policy_timing"


def test_match_faq_bank_answer_returns_match_for_idle_context(monkeypatch):
    service = ChatService()
    context = ConversationContext(session_id="faq-1", hotel_code="DEFAULT", state=ConversationState.IDLE)

    monkeypatch.setattr("services.chat_service.config_service.is_intent_enabled", lambda _intent_id: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.find_faq_entry",
        lambda _message: {
            "id": "medicine_availability",
            "question": "Is medicine available?",
            "answer": "Yes, medicine is available.",
            "match_score": 0.95,
        },
    )

    matched = service._match_faq_bank_answer("is medicine available now?", context)
    assert matched is not None
    assert matched["id"] == "medicine_availability"


def test_match_faq_bank_answer_skips_when_context_is_transactional(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="faq-2",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_booking_party_size",
    )

    monkeypatch.setattr("services.chat_service.config_service.is_intent_enabled", lambda _intent_id: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.find_faq_entry",
        lambda _message: {"id": "dummy", "answer": "dummy", "match_score": 0.99},
    )

    assert service._match_faq_bank_answer("what is check out time?", context) is None


@pytest.mark.asyncio
async def test_process_message_uses_resolved_hotel_code_for_context(monkeypatch):
    service = ChatService()
    captured = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        captured["hotel_code"] = hotel_code
        return ConversationContext(
            session_id=session_id,
            hotel_code=hotel_code,
            state=ConversationState.IDLE,
        )

    async def _fake_save_context(_context, db_session=None):
        return None

    monkeypatch.setattr(
        "services.chat_service.config_service.resolve_hotel_code",
        lambda _requested: "hotel_1771239708469",
    )
    monkeypatch.setattr(
        "services.chat_service.context_manager.get_or_create_context",
        _fake_get_or_create_context,
    )
    monkeypatch.setattr(
        "services.chat_service.context_manager.save_context",
        _fake_save_context,
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"intents": []},
    )
    monkeypatch.setattr(
        service,
        "_match_faq_bank_answer",
        lambda _message, _context: {
            "id": "faq_1",
            "answer": "test answer",
            "match_score": 0.95,
        },
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-tenant",
            message="hi",
            hotel_code="DEFAULT",
        )
    )

    assert captured["hotel_code"] == "hotel_1771239708469"
    assert response.metadata.get("faq_bank_match") is True


@pytest.mark.asyncio
async def test_process_message_deescalates_on_return_to_bot(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-deesc",
        hotel_code="DEFAULT",
        state=ConversationState.ESCALATED,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    monkeypatch.setattr(
        "services.chat_service.config_service.resolve_hotel_code",
        lambda _requested: "DEFAULT",
    )
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr(
        "services.chat_service.context_manager.get_or_create_context",
        _fake_get_or_create_context,
    )
    monkeypatch.setattr(
        "services.chat_service.context_manager.save_context",
        _fake_save_context,
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"intents": []},
    )
    monkeypatch.setattr(
        "services.chat_service.conversation_memory_service.maybe_refresh_summary",
        _no_summary,
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-deesc",
            message="Return to bot",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.IDLE
    assert response.metadata.get("deescalated") is True


@pytest.mark.asyncio
async def test_full_kb_simple_greeting_uses_shortcut_without_llm(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-greeting-shortcut",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError("Full-KB LLM should not be invoked for simple greeting shortcut")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [],
            "restaurants": [],
            "services": {},
            "intents": [],
            "welcome_message": "Welcome to ICONIQA Demo Hotel in Mumbai. How may I assist you today?",
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-greeting-shortcut",
            message="hi",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_shortcut"
    assert response.metadata.get("full_kb_shortcut_type") == "greeting"
    assert "welcome to iconiqa demo hotel" in response.message.lower()


def test_match_identity_response_returns_bot_name():
    service = ChatService()
    context = ConversationContext(session_id="identity-1", hotel_code="DEFAULT", state=ConversationState.IDLE)

    matched = service._match_identity_response(
        "what is ur name",
        {"bot_name": "Kebo", "hotel_name": "ICONIQA Demo Hotel"},
        context,
    )

    assert matched is not None
    assert matched["match_type"] == "bot_name"
    assert "Kebo" in matched["response_text"]


def test_match_identity_response_handles_typo_variant():
    service = ChatService()
    context = ConversationContext(session_id="identity-typo-1", hotel_code="DEFAULT", state=ConversationState.IDLE)

    matched = service._match_identity_response(
        "who arre u",
        {"bot_name": "kebo", "hotel_name": "ICONIQA Demo Hotel", "city": "Mumbai"},
        context,
    )

    assert matched is not None
    assert matched["match_type"] == "bot_name"
    assert "kebo" in matched["response_text"].lower()
    assert "iconiqa demo hotel" in matched["response_text"].lower()


@pytest.mark.asyncio
async def test_full_kb_identity_typo_uses_shortcut_without_llm(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-identity-shortcut",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError("Full-KB LLM should not be invoked for identity typo shortcut")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [],
            "restaurants": [],
            "services": {},
            "intents": [],
            "bot_name": "kebo",
            "hotel_name": "ICONIQA Demo Hotel",
            "city": "Mumbai",
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-identity-shortcut",
            message="who arre u",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_shortcut"
    assert response.metadata.get("full_kb_shortcut_type") == "identity"
    assert "concierge assistant" in response.message.lower()


@pytest.mark.asyncio
async def test_process_message_rechecks_service_shortcut_after_pending_interrupt(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-interrupt-service",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_booking_time",
        pending_data={"restaurant_name": "Kadak", "party_size": "2"},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_classify(_message, _history, _ctx):
        return IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={})

    monkeypatch.setattr(
        "services.chat_service.config_service.resolve_hotel_code",
        lambda _requested: "DEFAULT",
    )
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr(
        "services.chat_service.context_manager.get_or_create_context",
        _fake_get_or_create_context,
    )
    monkeypatch.setattr(
        "services.chat_service.context_manager.save_context",
        _fake_save_context,
    )
    monkeypatch.setattr(
        "services.chat_service.conversation_memory_service.maybe_refresh_summary",
        _no_summary,
    )
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "bot_name": "Kebo",
            "hotel_name": "Demo",
            "intents": [],
            "service_catalog": [
                {
                    "id": "ird",
                    "name": "In-Room Dining",
                    "type": "restaurant",
                    "description": "Multi-cuisine",
                    "hours": {"open": "00:00", "close": "23:59"},
                    "is_active": True,
                }
            ],
        },
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-interrupt-service",
            message="ird timings",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("pending_interrupted") is True
    assert response.metadata.get("service_catalog_match") is True
    assert "00:00" in response.message


@pytest.mark.asyncio
async def test_process_message_sets_service_detail_pending_for_service_action(monkeypatch):
    service = ChatService()

    context_holder = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        ctx = ConversationContext(
            session_id=session_id,
            hotel_code=hotel_code,
            state=ConversationState.IDLE,
        )
        context_holder["ctx"] = ctx
        return ctx

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    monkeypatch.setattr(
        "services.chat_service.config_service.resolve_hotel_code",
        lambda _requested: "DEFAULT",
    )
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr(
        "services.chat_service.context_manager.get_or_create_context",
        _fake_get_or_create_context,
    )
    monkeypatch.setattr(
        "services.chat_service.context_manager.save_context",
        _fake_save_context,
    )
    monkeypatch.setattr(
        "services.chat_service.conversation_memory_service.maybe_refresh_summary",
        _no_summary,
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "bot_name": "Kebo",
            "hotel_name": "Demo",
            "intents": [],
            "service_catalog": [
                {
                    "id": "hotel_booking",
                    "name": "Hotel Booking",
                    "type": "front_desk",
                    "description": "Room booking assistance",
                    "hours": {"open": "00:00", "close": "23:59"},
                    "is_active": True,
                }
            ],
        },
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-service-action",
            message="I want to book hotel stay",
            hotel_code="DEFAULT",
        )
    )

    ctx = context_holder["ctx"]
    assert response.state == ConversationState.AWAITING_INFO
    assert ctx.pending_action == "collect_service_details"
    assert ctx.pending_data.get("service_id") == "hotel_booking"


@pytest.mark.asyncio
async def test_process_message_interrupts_collect_service_details_for_unrelated_question(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-service-interrupt",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_service_details",
        pending_data={"service_id": "hotel_booking", "service_name": "Hotel Booking"},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_classify(_message, _history, _ctx):
        return IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={})

    monkeypatch.setattr(
        "services.chat_service.config_service.resolve_hotel_code",
        lambda _requested: "DEFAULT",
    )
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr(
        "services.chat_service.context_manager.get_or_create_context",
        _fake_get_or_create_context,
    )
    monkeypatch.setattr(
        "services.chat_service.context_manager.save_context",
        _fake_save_context,
    )
    monkeypatch.setattr(
        "services.chat_service.conversation_memory_service.maybe_refresh_summary",
        _no_summary,
    )
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "bot_name": "Kebo",
            "hotel_name": "Demo",
            "intents": [],
            "service_catalog": [
                {
                    "id": "spa_booking",
                    "name": "Spa Booking",
                    "type": "wellness",
                    "description": "Spa appointments",
                    "hours": {"open": "09:00", "close": "23:00"},
                    "is_active": True,
                }
            ],
        },
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-service-interrupt",
            message="what are spa timings?",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("pending_interrupted") is True
    assert "noted your Hotel Booking request details" not in response.message
    assert context.pending_action is None


@pytest.mark.asyncio
async def test_process_message_resume_restores_parked_task(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-parked-resume",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_action=None,
        pending_data={
            "_task_state": {
                "version": 1,
                "active_task": None,
                "parked_tasks": [
                    {
                        "pending_action": "collect_booking_time",
                        "pending_data": {"restaurant_name": "Kadak", "party_size": "2"},
                        "state": "awaiting_info",
                        "summary": "your Kadak request",
                    }
                ],
            }
        },
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "intents": [], "tools": [], "services": {}},
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-parked-resume",
            message="resume",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("parked_task_resumed") is True
    assert context.pending_action == "collect_booking_time"
    assert context.state == ConversationState.AWAITING_INFO
    assert "Resuming your Kadak request" in response.message


@pytest.mark.asyncio
async def test_process_message_cancel_pending_drops_parked_task(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-parked-cancel",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_action=None,
        pending_data={
            "_task_state": {
                "version": 1,
                "active_task": None,
                "parked_tasks": [
                    {
                        "pending_action": "collect_service_details",
                        "pending_data": {"service_name": "Spa Booking"},
                        "state": "awaiting_info",
                        "summary": "your spa booking request",
                    }
                ],
            }
        },
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "intents": [], "tools": [], "services": {}},
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-parked-cancel",
            message="cancel pending",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("parked_task_cancelled") is True
    assert response.state == ConversationState.IDLE
    assert context.pending_action is None
    assert response.metadata.get("parked_tasks_remaining") == 0


@pytest.mark.asyncio
async def test_process_message_auto_resumes_parked_task_from_detail_reply(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-parked-auto-resume",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_action=None,
        pending_data={
            "_task_state": {
                "version": 1,
                "active_task": None,
                "parked_tasks": [
                    {
                        "pending_action": "collect_booking_party_size",
                        "pending_data": {"restaurant_name": "Kadak", "date": "today"},
                        "state": "awaiting_info",
                        "summary": "your Kadak request",
                    }
                ],
            }
        },
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_classify(_message, _history, _ctx):
        return IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.95, entities={})

    async def _fake_dispatch(message, intent_result, ctx, capabilities, db_session=None):
        assert ctx.pending_action == "collect_booking_party_size"
        assert (ctx.pending_data or {}).get("restaurant_name") == "Kadak"
        return HandlerResult(
            response_text="Noted 3 guests. What time would you prefer?",
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_booking_time",
            pending_data={"restaurant_name": "Kadak", "party_size": "3", "date": "today"},
            suggested_actions=["7 PM", "8 PM"],
            metadata={"auto_resumed_from_parked_task": True},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_pure_llm_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "intents": [], "tools": [], "services": {}},
    )

    response = await service.process_message(
        ChatRequest(
            session_id="s-parked-auto-resume",
            message="for 3 guests",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.AWAITING_INFO
    assert context.pending_action == "collect_booking_time"
    assert response.metadata.get("auto_resumed_from_parked_task") is True
    assert response.metadata.get("parked_task_available") is False


def test_build_llm_context_pack_includes_required_runtime_fields():
    service = ChatService()
    context = ConversationContext(
        session_id="s-context-pack",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_service_details",
        pending_data={"service_id": "spa_booking"},
    )

    context_pack = service._build_llm_context_pack(
        context=context,
        capabilities_summary={
            "service_catalog": [
                {
                    "id": "spa_booking",
                    "name": "Spa Booking",
                    "type": "service",
                    "phase_id": "during_stay",
                    "ticketing_enabled": False,
                    "service_prompt_pack": {
                        "required_slots": [{"id": "preferred_time", "required": True}]
                    },
                }
            ]
        },
        selected_phase_context={"selected_phase_id": "during_stay", "selected_phase_name": "During Stay"},
        memory_snapshot={"facts": {"last_user_topic": "book spa"}},
        user_message="book spa",
    )

    assert isinstance(context_pack, dict)
    for key in (
        "current_phase",
        "active_flow",
        "pending_action",
        "missing_slots",
        "recent_user_goal",
        "phase_services",
        "ticketing_enabled_by_service",
        "stay_window",
        "current_time",
    ):
        assert key in context_pack
    assert context_pack.get("pending_action") == "collect_service_details"
    assert context_pack.get("ticketing_enabled_by_service", {}).get("spa_booking") is False
    assert "preferred_time" in context_pack.get("missing_slots", [])


@pytest.mark.asyncio
async def test_kb_only_mode_applies_response_validator_guardrails(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-kb-validator",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_dispatch(*args, **kwargs):
        return HandlerResult(
            response_text="You should take 500 mg twice daily.",
            next_state=ConversationState.IDLE,
            suggested_actions=["Ask another question"],
            metadata={},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [],
            "restaurants": [],
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "intents": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.handler_registry.dispatch", _fake_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="s-kb-validator",
            message="what medicine should i take for fever",
            hotel_code="DEFAULT",
        )
    )

    assert "not able to provide medical diagnosis or dosage guidance" in response.message.lower()
    assert response.metadata.get("response_validator_applied") is True
    assert response.metadata.get("response_validator_replaced") is True
    assert "medical_advice_guardrail" in (response.metadata.get("response_validator_issues") or [])


@pytest.mark.asyncio
async def test_full_kb_mode_applies_response_validator_guardrails(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-validator",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="Take 500 mg every 6 hours for pain.",
            normalized_query="what medicine should i take for pain",
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Talk to human"],
            trace_id="fullkb-test-guardrail",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [],
            "restaurants": [],
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "intents": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-validator",
            message="which medicine should i take for pain",
            hotel_code="DEFAULT",
        )
    )

    assert "not able to provide medical diagnosis or dosage guidance" in response.message.lower()
    assert response.metadata.get("response_validator_applied") is True
    assert response.metadata.get("response_validator_replaced") is True
    assert "medical_advice_guardrail" in (response.metadata.get("response_validator_issues") or [])


@pytest.mark.asyncio
async def test_full_kb_passthrough_routes_ticketing_intent_to_handler(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-ticket-route",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    captured: dict[str, object] = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I have noted your complaint.",
            normalized_query="ac not cooling",
            intent=IntentType.COMPLAINT,
            raw_intent="complaint",
            confidence=0.92,
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_ticket_creation",
            pending_data={
                "issue": "AC not cooling in room 305",
                "category": "complaint",
                "priority": "high",
            },
            room_number="305",
            suggested_actions=["Yes, create ticket", "No, cancel"],
            trace_id="fullkb-ticketing-test",
            llm_output={
                "entities": {
                    "category": "complaint",
                    "priority": "high",
                }
            },
            clear_pending_data=False,
            status="success",
        )

    async def _fake_dispatch_to_handler(message, intent_result, _context, _capabilities, _db_session=None):
        captured["message"] = message
        captured["intent"] = intent_result.intent
        captured["entities"] = dict(intent_result.entities or {})
        return HandlerResult(
            response_text=(
                "Your complaint ticket is created successfully.\n\n"
                "Ticket ID: LOCAL-1"
            ),
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_ticket_escalation",
            pending_data={"ticket_id": "LOCAL-1", "issue": "AC not cooling in room 305"},
            suggested_actions=["Yes, connect me", "No, continue with bot"],
            metadata={"ticket_created": True, "ticket_id": "LOCAL-1"},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [],
            "restaurants": [],
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "intents": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch_to_handler)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-ticket-route",
            message="AC is not cooling in my room",
            hotel_code="DEFAULT",
        )
    )

    assert captured.get("intent") == IntentType.COMPLAINT
    assert response.metadata.get("response_source") == "full_kb_llm_handler"
    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-1"
    assert context.pending_action == "confirm_ticket_escalation"
    assert "LOCAL-1" in response.message


@pytest.mark.asyncio
async def test_full_kb_faq_escalation_text_routes_to_complaint_handler(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-faq-to-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    captured: dict[str, object] = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I'm very sorry for this inconvenience. I will escalate this to our staff immediately.",
            normalized_query="there is cockroach in my room",
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.95,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number="305",
            suggested_actions=["Talk to human"],
            trace_id="fullkb-faq-ticket-test",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    async def _fake_dispatch_to_handler(message, intent_result, _context, _capabilities, _db_session=None):
        captured["message"] = message
        captured["intent"] = intent_result.intent
        return HandlerResult(
            response_text="Your complaint ticket is created successfully.\n\nTicket ID: LOCAL-1",
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_ticket_escalation",
            pending_data={"ticket_id": "LOCAL-1"},
            suggested_actions=["Yes, connect me", "No, continue with bot"],
            metadata={"ticket_created": True, "ticket_id": "LOCAL-1"},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch_to_handler)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-faq-to-ticket",
            message="theres a cockroach in my room",
            hotel_code="DEFAULT",
        )
    )

    assert captured.get("intent") == IntentType.COMPLAINT
    assert response.metadata.get("full_kb_ticketing_handler") is True
    assert response.metadata.get("ticket_id") == "LOCAL-1"


@pytest.mark.asyncio
async def test_full_kb_room_service_request_routes_to_complaint_handler(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-roomservice-to-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    captured: dict[str, object] = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I've noted your request for a towel and will ensure it is addressed promptly.",
            normalized_query="i need a towel",
            intent=IntentType.ROOM_SERVICE,
            raw_intent="room_service",
            confidence=0.95,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number="305",
            suggested_actions=["Ask another question"],
            trace_id="fullkb-roomservice-ticket-test",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    async def _fake_dispatch_to_handler(message, intent_result, _context, _capabilities, _db_session=None):
        captured["message"] = message
        captured["intent"] = intent_result.intent
        return HandlerResult(
            response_text="Your complaint ticket is created successfully.\n\nTicket ID: LOCAL-2",
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_ticket_escalation",
            pending_data={"ticket_id": "LOCAL-2"},
            suggested_actions=["Yes, connect me", "No, continue with bot"],
            metadata={"ticket_created": True, "ticket_id": "LOCAL-2"},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch_to_handler)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-roomservice-to-ticket",
            message="i need a towel",
            hotel_code="DEFAULT",
        )
    )

    assert captured.get("intent") == IntentType.COMPLAINT
    assert response.metadata.get("full_kb_ticketing_handler") is True
    assert response.metadata.get("ticket_id") == "LOCAL-2"


@pytest.mark.asyncio
async def test_full_kb_ticketing_takeover_declines_out_of_phase_service_request(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-phase-gate-decline",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I can help you with that request.",
            normalized_query="book spa at 7 pm",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="table_booking",
            confidence=0.92,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-phase-gate-decline",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    async def _should_not_dispatch(*_args, **_kwargs):
        raise AssertionError("Handler dispatch should not run when phase gate declines request")

    async def _should_not_decide(**_kwargs):
        raise AssertionError("Ticketing decision should not run when phase gate declines request")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa booking requests",
                "phase_id": "during_stay",
                "is_active": True,
            },
            {
                "id": "booking_modification",
                "name": "Booking Modification",
                "type": "service",
                "description": "Modify booking details",
                "phase_id": "pre_checkin",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_checkin", "name": "Pre Checkin"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _should_not_decide)
    monkeypatch.setattr(service, "_dispatch_to_handler", _should_not_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-phase-gate-decline",
            message="book spa at 7 pm",
            hotel_code="DEFAULT",
            metadata={"phase": "pre_checkin"},
        )
    )

    assert response.state == ConversationState.IDLE
    assert "not available for Pre Checkin phase" in response.message
    assert response.metadata.get("full_kb_ticketing_handler") is True
    assert response.metadata.get("full_kb_ticketing_phase_gate") is True
    assert response.metadata.get("phase_gate_service_name") == "Spa & Recreation Booking"


@pytest.mark.asyncio
async def test_full_kb_phase_gate_declines_out_of_phase_request_without_takeover(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-phase-gate-no-takeover",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I can help you with that request.",
            normalized_query="book spa at 7 pm",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="table_booking",
            confidence=0.92,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-phase-gate-no-takeover",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    async def _should_not_dispatch(*_args, **_kwargs):
        raise AssertionError("Handler dispatch should not run when phase gate declines request")

    async def _should_not_decide(**_kwargs):
        raise AssertionError("Ticketing decision should not run when phase gate declines request")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa booking requests",
                "phase_id": "during_stay",
                "is_active": True,
            },
            {
                "id": "booking_modification",
                "name": "Booking Modification",
                "type": "service",
                "description": "Modify booking details",
                "phase_id": "pre_checkin",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_checkin", "name": "Pre Checkin"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _should_not_decide)
    monkeypatch.setattr(service, "_dispatch_to_handler", _should_not_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-phase-gate-no-takeover",
            message="book spa at 7 pm",
            hotel_code="DEFAULT",
            metadata={"phase": "pre_checkin"},
        )
    )

    assert response.state == ConversationState.IDLE
    assert "not available for Pre Checkin phase" in response.message
    assert response.metadata.get("full_kb_ticketing_phase_gate") is True
    assert response.metadata.get("phase_gate_service_name") == "Spa & Recreation Booking"


@pytest.mark.asyncio
async def test_full_kb_plugin_ticketing_creates_backend_ticket_without_changing_response(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-plugin-ticket-no-takeover",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    expected_response = "Thanks. I have created a complaint ticket for this issue."
    captured: dict[str, object] = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="there is a cockroach in my room",
            intent=IntentType.COMPLAINT,
            raw_intent="complaint",
            confidence=0.94,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number="305",
            suggested_actions=["Need help"],
            trace_id="fullkb-plugin-ticket-no-takeover",
            llm_output={"requires_ticket": True, "ticket_reason": "operational staff action required"},
            clear_pending_data=False,
            status="success",
        )

    async def _fake_dispatch_to_handler(message, intent_result, _context, _capabilities, _db_session=None):
        captured["message"] = message
        captured["intent"] = intent_result.intent
        return HandlerResult(
            response_text=expected_response,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=["Need help"],
            metadata={"ticket_created": True, "ticket_id": "LOCAL-PLUGIN-1"},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    async def _fake_ticketing_decide_async(**_kwargs):
        return TicketingAgentDecision(
            activate=True,
            routed_intent=IntentType.COMPLAINT,
            route="complaint_handler",
            reason="complaint_intent",
            source="intent",
            matched_case="Guest reports a complaint or maintenance issue that requires staff action.",
        )
    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.decide_async",
        _fake_ticketing_decide_async,
    )
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch_to_handler)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-plugin-ticket-no-takeover",
            message="theres a roach in my room",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert captured.get("intent") == IntentType.COMPLAINT
    assert response.metadata.get("response_source") == "full_kb_llm_handler"
    assert response.metadata.get("full_kb_ticketing_handler") is True
    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-PLUGIN-1"


@pytest.mark.asyncio
async def test_full_kb_plugin_ticketing_creates_complaint_without_room_number(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-plugin-complaint-no-room",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        guest_phone="+919999999999",
        pending_data={"_integration": {"phase": "pre_booking", "flow": "booking"}},
    )
    expected_response = "I captured this issue. Please share your room number."
    captured: dict[str, object] = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="cockroach in room",
            intent=IntentType.HUMAN_REQUEST,
            raw_intent="human_request",
            confidence=0.93,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-plugin-complaint-no-room",
            llm_output={"requires_ticket": True, "ticket_reason": "operational staff action required"},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    async def _fake_ticketing_decide_async(**_kwargs):
        return TicketingAgentDecision(
            activate=True,
            routed_intent=IntentType.COMPLAINT,
            route="complaint_handler",
            reason="configured_ticketing_case_match",
            source="service_ticketing_cases",
            matched_case="Guest reports a complaint or maintenance issue that requires staff action.",
        )

    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.decide_async",
        _fake_ticketing_decide_async,
    )
    async def _fake_dispatch_to_handler(message, intent_result, _context, _capabilities, _db_session=None):
        captured["message"] = message
        captured["intent"] = intent_result.intent
        return HandlerResult(
            response_text=expected_response,
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_ticket_room_number",
            pending_data={"issue": "cockroach in room"},
            suggested_actions=["101", "202"],
            metadata={},
        )

    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch_to_handler)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-plugin-complaint-no-room",
            message="there is cockroach in my room",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert captured.get("intent") == IntentType.COMPLAINT
    assert response.metadata.get("full_kb_ticketing_handler") is True
    assert response.state == ConversationState.AWAITING_INFO
    assert context.pending_action == "collect_ticket_room_number"


@pytest.mark.asyncio
async def test_full_kb_plugin_ticketing_evaluates_agent_for_greeting_turn(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-plugin-greeting-eval",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    expected_response = "Welcome to ICONIQA Demo Hotel in Mumbai. How may I assist you today?"
    decide_calls = {"count": 0}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="hi",
            intent=IntentType.GREETING,
            raw_intent="greeting",
            confidence=0.95,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-plugin-greeting-eval",
            llm_output={"requires_ticket": False},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    async def _fake_ticketing_decide_async(**_kwargs):
        decide_calls["count"] += 1
        return TicketingAgentDecision(
            activate=False,
            routed_intent=None,
            route="none",
            reason="no_ticketing_trigger",
            source="default",
            matched_case="",
        )

    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.decide_async",
        _fake_ticketing_decide_async,
    )

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Greeting turn must not create ticket")

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _should_not_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-plugin-greeting-eval",
            message="hi",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    # One decision call comes from turn-level status evaluation and one from
    # plugin ticketing evaluation.
    assert decide_calls["count"] == 2
    assert response.metadata.get("ticket_created") is None


@pytest.mark.asyncio
async def test_full_kb_plugin_ticketing_skips_human_request_without_matching_case(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-plugin-human-no-case",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    expected_response = "I can connect you to our staff team right away."

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="connect me to human",
            intent=IntentType.HUMAN_REQUEST,
            raw_intent="human_request",
            confidence=0.95,
            next_state=ConversationState.ESCALATED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Return to bot"],
            trace_id="fullkb-plugin-human-no-case",
            llm_output={"requires_ticket": True},
            clear_pending_data=False,
            status="success",
        )

    async def _dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("Handler takeover should not run in plugin non-takeover mode")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _dispatch_should_not_run)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    async def _fake_ticketing_decide_async(**_kwargs):
        return TicketingAgentDecision(
            activate=True,
            routed_intent=IntentType.HUMAN_REQUEST,
            route="escalation_handler",
            reason="human_request",
            source="intent",
            matched_case="",
        )

    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.decide_async",
        _fake_ticketing_decide_async,
    )

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Ticket should be skipped when no configured ticketing case matched")

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _should_not_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-plugin-human-no-case",
            message="connect me to human staff",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("ticket_created") is None
    assert response.metadata.get("ticket_create_skip_reason") == "human_request_without_matching_configured_case"


@pytest.mark.asyncio
async def test_full_kb_plugin_ticketing_creates_human_handoff_ticket_with_matching_case(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-plugin-human-with-case",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    expected_response = "I can connect you to our staff team right away."

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="connect me to human",
            intent=IntentType.HUMAN_REQUEST,
            raw_intent="human_request",
            confidence=0.95,
            next_state=ConversationState.ESCALATED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Return to bot"],
            trace_id="fullkb-plugin-human-with-case",
            llm_output={"requires_ticket": True},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.get_configured_cases",
        lambda: ["Guest requests human escalation or live agent support."],
    )

    async def _fake_match_case_async(**_kwargs):
        return "Guest requests human escalation or live agent support."

    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.match_configured_case_async",
        _fake_match_case_async,
    )
    monkeypatch.setattr(
        "services.chat_service.ticketing_service.build_lumira_ticket_payload",
        lambda **_kwargs: {"issue": "human handoff ticket"},
    )

    async def _fake_create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-HUMAN-1", status_code=200, response={"ok": True})

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _fake_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-plugin-human-with-case",
            message="talk to human",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-HUMAN-1"
    assert response.metadata.get("ticket_sub_category") == "human_handoff"


@pytest.mark.asyncio
async def test_full_kb_plugin_ticketing_defers_spa_case_until_confirmation(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-plugin-spa-defer",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    expected_response = (
        "ICONIQA Demo Hotel offers spa treatments. Please share your preferred treatment and time."
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="spa treatment i want",
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.92,
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_booking_time",
            pending_data={},
            room_number=None,
            suggested_actions=["Book spa at 6 PM"],
            trace_id="fullkb-plugin-spa-defer",
            llm_output={"requires_ticket": True},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.get_configured_cases", lambda: ["spa booking"])

    async def _fake_match_case_async(**_kwargs):
        return "spa booking"

    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.match_configured_case_async",
        _fake_match_case_async,
    )

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Spa request before explicit confirmation should not create ticket")

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _should_not_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-plugin-spa-defer",
            message="spa treatment i want",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("ticket_created") is None


@pytest.mark.asyncio
async def test_full_kb_plugin_ticketing_defers_spa_ticket_even_if_response_mentions_staff(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-plugin-spa-forwarded",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    expected_response = (
        "I've noted your request to book a spa treatment. I'll forward this to our staff for further assistance."
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="book a spa treatment",
            intent=IntentType.FAQ,
            raw_intent="spa_booking",
            confidence=0.92,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-plugin-spa-forwarded",
            llm_output={"requires_ticket": True, "ticket_reason": "manual staff follow-up required"},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.get_configured_cases", lambda: ["spa booking"])

    async def _fake_match_case_async(**_kwargs):
        return "spa booking"

    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.match_configured_case_async",
        _fake_match_case_async,
    )
    async def _should_not_create_ticket(_payload):
        raise AssertionError("Spa booking should not create ticket before explicit confirmation")

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _should_not_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-plugin-spa-forwarded",
            message="Book a spa treatment",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("ticket_created") is None


@pytest.mark.asyncio
async def test_full_kb_validator_blocks_unconfigured_local_cab_promise(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-local-cab-guard",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="Sure, I can arrange your local cab booking for 2 PM.",
            normalized_query="book local cab at 2 pm",
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Talk to human"],
            trace_id="fullkb-local-cab-guard",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr("services.chat_service.config_service.is_intent_enabled", lambda _intent_id: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [
                {
                    "id": "transport",
                    "name": "Transport",
                    "type": "service",
                    "description": "Airport transfer service",
                    "is_active": True,
                }
            ],
            "restaurants": [],
            "services": {"transport": True, "local_transport": False, "local_cab": False},
            "nlu_policy": {"dos": [], "donts": []},
            "intents": [],
            "tools": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-local-cab-guard",
            message="book local cab from hotel to bandra at 2 pm",
            hotel_code="DEFAULT",
        )
    )

    response_text = str(response.message or "").lower()
    assert "connect" in response_text
    assert ("staff" in response_text) or ("team" in response_text)
    assert "local_cab_not_configured" not in (response.metadata.get("response_validator_issues") or [])


@pytest.mark.asyncio
async def test_full_kb_validator_replaces_unconfigured_local_cab_clarification(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-local-cab-clarification-guard",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="Could you please share your pickup and drop-off locations?",
            normalized_query="book local cab at 2 pm",
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Talk to human"],
            trace_id="fullkb-local-cab-clarification-guard",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_takeover_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr("services.chat_service.config_service.is_intent_enabled", lambda _intent_id: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {
            "service_catalog": [
                {
                    "id": "transport",
                    "name": "Transport",
                    "type": "service",
                    "description": "Airport transfer service",
                    "is_active": True,
                }
            ],
            "restaurants": [],
            "services": {"transport": True, "local_transport": False, "local_cab": False},
            "nlu_policy": {"dos": [], "donts": []},
            "intents": [],
            "tools": [],
        },
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-local-cab-clarification-guard",
            message="book local cab from hotel to bandra at 2 pm",
            hotel_code="DEFAULT",
        )
    )

    response_text = str(response.message or "").lower()
    assert "connect" in response_text
    assert ("staff" in response_text) or ("team" in response_text)
    assert "clarification_for_unconfigured_service_request" in (response.metadata.get("response_validator_issues") or [])


@pytest.mark.asyncio
async def test_full_kb_requires_ticket_true_does_not_hijack_order_flow(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-requires-ticket-true",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I will get the team to arrange this order request.",
            normalized_query="please arrange dinner in room 305",
            intent=IntentType.ORDER_FOOD,
            raw_intent="order_food",
            confidence=0.9,
            next_state=ConversationState.AWAITING_INFO,
            pending_action=None,
            pending_data={"issue": "Dinner order request for room 305"},
            room_number="305",
            suggested_actions=["Yes, create ticket", "No, cancel"],
            trace_id="fullkb-requires-ticket-true",
            llm_output={"requires_ticket": True, "ticket_reason": "manual order fulfillment needed"},
            clear_pending_data=False,
            status="success",
        )

    async def _dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("Ticket handler should not be called for normal order flow")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _dispatch_should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-requires-ticket-true",
            message="please arrange dinner in room 305",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_llm"
    assert response.metadata.get("full_kb_ticketing_handler") is None


@pytest.mark.asyncio
async def test_full_kb_room_service_info_query_with_requires_ticket_false_skips_ticketing_agent(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-roomservice-info-no-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="Room service is available 24/7 at the property.",
            normalized_query="check room service availability",
            intent=IntentType.ROOM_SERVICE,
            raw_intent="room_service",
            confidence=0.94,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Order food", "Need towels", "Book a table"],
            trace_id="fullkb-roomservice-info-no-ticket",
            llm_output={"requires_ticket": False},
            clear_pending_data=False,
            status="success",
        )

    async def _dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("Ticket handler should not be called for room-service informational query")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _dispatch_should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-roomservice-info-no-ticket",
            message="check room service",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_llm"
    assert response.metadata.get("full_kb_ticketing_handler") is None


@pytest.mark.asyncio
async def test_full_kb_requires_ticket_false_skips_ticketing_route(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-requires-ticket-false",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I have noted your towel preference in profile context.",
            normalized_query="i need a towel",
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number="305",
            suggested_actions=["Ask another question"],
            trace_id="fullkb-requires-ticket-false",
            llm_output={"requires_ticket": False},
            clear_pending_data=False,
            status="success",
        )

    async def _dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("Ticket handler should not be called when requires_ticket=false")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _dispatch_should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-requires-ticket-false",
            message="i need a towel",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_llm"
    assert response.metadata.get("full_kb_ticketing_handler") is None


@pytest.mark.asyncio
async def test_full_kb_order_without_requires_ticket_keeps_order_flow(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-order-no-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="You selected Opera. How many portions would you like?",
            normalized_query="opera",
            intent=IntentType.ORDER_FOOD,
            raw_intent="order_food",
            confidence=0.9,
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_order_quantity",
            pending_data={"item_name": "Opera"},
            room_number="305",
            suggested_actions=["1", "2", "3"],
            trace_id="fullkb-order-no-ticket",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    async def _dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("Ticket handler should not be called for order flow without requires_ticket")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _dispatch_should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-order-no-ticket",
            message="opera",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_llm"
    assert response.metadata.get("full_kb_ticketing_handler") is None
    assert "how many portions" in response.message.lower()


@pytest.mark.asyncio
async def test_full_kb_table_booking_with_requires_ticket_keeps_booking_flow(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-booking-no-ticket-hijack",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={"booking_type": "room"},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=(
                "Perfect, 2 guests for Feb 25 to Feb 26. "
                "Please share your preferred room type so I can continue."
            ),
            normalized_query="2 feb 25-26",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="room_booking",
            confidence=0.9,
            next_state=ConversationState.AWAITING_INFO,
            pending_action="collect_room_booking_details",
            pending_data={"guest_count": 2, "check_in": "2026-02-25", "check_out": "2026-02-26"},
            room_number=None,
            suggested_actions=["Deluxe", "Suite"],
            trace_id="fullkb-booking-no-ticket-hijack",
            llm_output={"requires_ticket": True, "ticket_reason": "manual booking support needed"},
            clear_pending_data=False,
            status="success",
        )

    async def _dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("Ticket handler should not be called for normal booking detail collection")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _dispatch_should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-booking-no-ticket-hijack",
            message="2, feb 25-26",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_llm"
    assert response.metadata.get("full_kb_ticketing_handler") is None
    assert response.state == ConversationState.AWAITING_INFO


@pytest.mark.asyncio
async def test_full_kb_order_quantity_reply_ignores_spurious_complaint_ticket_route(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-order-qty-no-ticket-hijack",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_order_quantity",
        pending_data={"order_item": "Shrimp Katsu"},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text="I can create a ticket for this issue.",
            normalized_query="3",
            intent=IntentType.COMPLAINT,
            raw_intent="complaint",
            confidence=0.95,
            next_state=ConversationState.AWAITING_INFO,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Yes, create ticket", "No, cancel"],
            trace_id="fullkb-order-qty-no-ticket-hijack",
            llm_output={"requires_ticket": True, "ticket_reason": "model misfire"},
            clear_pending_data=False,
            status="success",
        )

    async def _dispatch_should_not_run(*_args, **_kwargs):
        raise AssertionError("Ticket handler should not be called for quantity reply in order flow")

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr(service, "_dispatch_to_handler", _dispatch_should_not_run)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-order-qty-no-ticket-hijack",
            message="3",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("response_source") == "full_kb_llm"
    assert response.metadata.get("full_kb_ticketing_handler") is None
    assert response.state == ConversationState.AWAITING_INFO
    assert response.metadata.get("full_kb_ticketing_requested") is None
    assert "would you like to add anything else" in response.message.lower()


@pytest.mark.asyncio
async def test_full_kb_confirm_booking_creates_backend_ticket_without_changing_response(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-confirm-booking-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_booking",
        pending_data={
            "service_name": "Kadak Restaurant",
            "party_size": "4",
            "time": "2 PM",
            "date": "today",
        },
    )
    expected_response = (
        "Your table for 4 at Kadak Restaurant is confirmed for 2 PM. "
        "We look forward to serving you. If you need anything else, feel free to ask!"
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="yes confirm",
            intent=IntentType.CONFIRMATION_YES,
            raw_intent="confirmation_yes",
            confidence=0.95,
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-confirm-booking-ticket",
            llm_output={},
            clear_pending_data=True,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.get_configured_cases", lambda: ["room booking"])
    async def _fake_match_case_async(**_kwargs):
        return "room booking"
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.match_configured_case_async", _fake_match_case_async)
    captured_payload_kwargs: dict[str, object] = {}

    def _fake_build_ticket_payload(**kwargs):
        captured_payload_kwargs.update(kwargs)
        return {"issue": "booking ticket"}

    monkeypatch.setattr(
        "services.chat_service.ticketing_service.build_lumira_ticket_payload",
        _fake_build_ticket_payload,
    )

    async def _fake_create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-321", status_code=200, response={"ok": True})

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _fake_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-confirm-booking-ticket",
            message="yes confirm",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-321"
    assert response.metadata.get("ticket_sub_category") == "table_booking"
    assert captured_payload_kwargs.get("phase") == "during_stay"


@pytest.mark.asyncio
async def test_full_kb_room_booking_details_turn_forces_review_and_confirmation(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-room-booking-review",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={"room_type": "Ultimate Suite"},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=(
                "Thank you for sharing your dates and party size. "
                "I'll forward your request for the Ultimate Suite from March 22-24 for 5 people to our staff."
            ),
            normalized_query="march 22-24 for 5 people",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="room_booking",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-room-booking-review",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.config_service.get_services", lambda: [])
    monkeypatch.setattr("services.chat_service.config_service.get_journey_phases", lambda: [])
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-room-booking-review",
            message="march 22-24 for 5 people",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.AWAITING_CONFIRMATION
    assert "review your room booking request" in str(response.message or "").lower()
    assert "ultimate suite" in str(response.message or "").lower()
    assert "for 5 guests" in str(response.message or "").lower()
    assert "type \"yes confirm\"" in str(response.message or "").lower()
    assert response.metadata.get("ticket_created") is None


@pytest.mark.asyncio
async def test_full_kb_room_booking_details_without_room_type_prompts_for_room_type(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-room-booking-missing-roomtype",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=(
                "Thank you for sharing your dates and party size. "
                "I'll forward your request to our staff for further assistance."
            ),
            normalized_query="march 10-15 for 5 people",
            intent=IntentType.TABLE_BOOKING,
            raw_intent="room_booking",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-room-booking-missing-roomtype",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.config_service.get_services", lambda: [])
    monkeypatch.setattr("services.chat_service.config_service.get_journey_phases", lambda: [])
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-room-booking-missing-roomtype",
            message="march 10-15 for 5 people",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.AWAITING_INFO
    assert "preferred room type" in str(response.message or "").lower()
    assert response.metadata.get("ticket_created") is None


@pytest.mark.asyncio
async def test_full_kb_room_booking_flow_answers_room_options_query_and_keeps_context(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-room-booking-options-query",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={
            "stay_checkin_date": "Mar 20",
            "stay_checkout_date": "Mar 22",
            "guest_count": 2,
        },
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=(
                "We have Premier King Room, Premier Twin Room, Lux Suite, Reserve Suite, "
                "Prestige Suite, and Ultimate Suite."
            ),
            normalized_query="what rooms do you have",
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-room-booking-options-query",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.config_service.get_services", lambda: [])
    monkeypatch.setattr("services.chat_service.config_service.get_journey_phases", lambda: [])
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-room-booking-options-query",
            message="what rooms do u have",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.AWAITING_INFO
    assert context.pending_action == "collect_room_booking_details"
    assert "premier king room" in str(response.message or "").lower()
    assert "preferred room type" not in str(response.message or "").lower()
    assert response.metadata.get("ticket_created") is None


def test_missing_room_booking_fields_accepts_guest_count_key():
    service = ChatService()
    missing = service._missing_room_booking_fields(
        pending_data={
            "room_type": "Ultimate Suite",
            "stay_checkin_date": "Mar 22",
            "stay_checkout_date": "Mar 24",
            "guest_count": 5,
        },
        memory_facts={},
    )
    assert missing == []


def test_missing_room_booking_fields_requires_room_type():
    service = ChatService()
    missing = service._missing_room_booking_fields(
        pending_data={
            "stay_checkin_date": "Mar 22",
            "stay_checkout_date": "Mar 24",
            "guest_count": 5,
        },
        memory_facts={},
    )
    assert "room_type" in missing


def test_missing_room_booking_fields_treats_generic_room_phrase_as_missing():
    service = ChatService()
    missing = service._missing_room_booking_fields(
        pending_data={
            "room_type": "Hotel offers a variety of room",
            "stay_checkin_date": "Mar 22",
            "stay_checkout_date": "Mar 24",
            "guest_count": 2,
        },
        memory_facts={},
    )
    assert "room_type" in missing


def test_extract_room_type_candidates_filters_generic_phrases():
    service = ChatService()
    context = ConversationContext(
        session_id="room-candidates-filter-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
    )
    context.add_message(
        MessageRole.ASSISTANT,
        (
            "ICONIQA Demo Hotel offers a variety of room types.\n"
            "1. **Premier King Room**: details\n"
            "2. **Premier Twin Room**: details\n"
            "3. **Ultimate Suite**: details"
        ),
    )

    candidates = service._extract_room_type_candidates_from_context_messages(context)

    assert "Hotel offers a variety of room" not in candidates
    assert "Premier King Room" in candidates
    assert "Premier Twin Room" in candidates
    assert "Ultimate Suite" in candidates


def test_generic_room_reference_rejects_conversational_prompt_fragments():
    service = ChatService()

    assert service._is_generic_room_reference("I can help with your room")
    assert service._is_generic_room_reference("Please share your preferred room")
    assert not service._is_generic_room_reference("Ultimate Suite")


def test_merge_room_type_candidates_filters_prompt_like_entries():
    service = ChatService()

    merged = service._merge_room_type_candidates(
        ["I can help with your room", "Please share your preferred room"],
        ["Premier King Room", "Ultimate Suite"],
    )

    assert merged == ["Premier King Room", "Ultimate Suite"]


def test_extract_room_booking_slot_updates_luxury_ignores_prompt_like_candidates():
    service = ChatService()
    context = ConversationContext(
        session_id="room-slots-luxury-prompt-filter-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={
            "stay_checkin_date": "Mar 02",
            "stay_checkout_date": "Mar 03",
            "room_type_candidates": [
                "I can help with your room",
                "Premier King Room",
                "Ultimate Suite",
            ],
        },
    )

    updates = service._extract_room_booking_slot_updates(
        message="i need the most luxurious one",
        current_pending_action="collect_room_booking_details",
        state=ConversationState.AWAITING_INFO,
        pending_data=context.pending_data,
        memory_facts={},
        conversation_context=context,
    )

    assert updates.get("room_type_preference") == "luxury"
    assert updates.get("room_type") == "Ultimate Suite"
    assert "I can help with your room" not in (updates.get("room_type_candidates") or [])


@pytest.mark.asyncio
async def test_handle_room_booking_flow_rejects_unknown_room_type_when_options_are_known():
    service = ChatService()
    context = ConversationContext(
        session_id="room-booking-unknown-type-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={
            "stay_checkin_date": "Mar 03",
            "stay_checkout_date": "Mar 04",
            "guest_count": 5,
        },
    )
    context.add_message(
        MessageRole.ASSISTANT,
        (
            "We have Premier King Room, Premier Twin Room, Lux Suite, "
            "Reserve Suite, Prestige Suite, and Ultimate Suite."
        ),
    )

    result = await service._handle_room_booking_intent_flow(
        message="book the premier suite for me",
        intent_result=IntentResult(
            intent=IntentType.TABLE_BOOKING,
            confidence=0.9,
            entities={"room_type": "Premier Suite"},
        ),
        context=context,
        capabilities={},
        db_session=None,
    )

    assert result.next_state == ConversationState.AWAITING_INFO
    assert result.pending_action == "collect_room_booking_details"
    assert "could not match" in str(result.response_text or "").lower()
    assert "preferred room type" in str(result.response_text or "").lower()
    assert not str((result.pending_data or {}).get("room_type") or "").strip()
    assert str((result.pending_data or {}).get("requested_room_type") or "").lower() == "premier suite"


def test_should_not_interrupt_pending_room_booking_for_room_options_query():
    service = ChatService()
    context = ConversationContext(
        session_id="room-pending-interrupt-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={"stay_checkin_date": "Mar 10", "stay_checkout_date": "Mar 12"},
    )
    intent_result = IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={})

    should_interrupt = service._should_interrupt_pending_flow(
        message="what rooms are there",
        intent_result=intent_result,
        context=context,
        capabilities_summary={"service_catalog": []},
    )

    assert should_interrupt is False


def test_extract_room_booking_slot_updates_infers_cheapest_from_recent_room_list():
    service = ChatService()
    context = ConversationContext(
        session_id="room-slots-cheapest-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={"stay_checkin_date": "Mar 10", "stay_checkout_date": "Mar 12"},
    )
    context.add_message(
        MessageRole.ASSISTANT,
        (
            "We have Premier King Room, Premier Twin Room, Lux Suite, "
            "Reserve Suite, Prestige Suite, and Ultimate Suite."
        ),
    )

    updates = service._extract_room_booking_slot_updates(
        message="i need the cheapest one",
        current_pending_action="collect_room_booking_details",
        state=ConversationState.AWAITING_INFO,
        pending_data=context.pending_data,
        memory_facts={},
        conversation_context=context,
    )

    assert updates.get("room_type_preference") == "cheapest"
    assert updates.get("room_type") == "Premier King Room"


@pytest.mark.asyncio
async def test_dispatch_room_booking_pending_handles_luxury_preference_without_time_prompt():
    service = ChatService()
    context = ConversationContext(
        session_id="room-dispatch-luxury-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={"stay_checkin_date": "Mar 02", "stay_checkout_date": "Mar 03"},
    )
    context.add_message(
        MessageRole.ASSISTANT,
        (
            "We have Premier King Room, Premier Twin Room, Lux Suite, "
            "Reserve Suite, Prestige Suite, and Ultimate Suite."
        ),
    )

    result = await service._dispatch_to_handler(
        "i need the most luxurious one",
        IntentResult(intent=IntentType.FAQ, confidence=0.8, entities={}),
        context,
        {"service_catalog": []},
        None,
    )

    assert result is not None
    assert result.next_state == ConversationState.AWAITING_INFO
    assert result.pending_action == "collect_room_booking_details"
    assert "preferred time" not in str(result.response_text or "").lower()
    assert "number of guests" in str(result.response_text or "").lower()
    assert str((result.pending_data or {}).get("room_type") or "").lower() == "ultimate suite"


@pytest.mark.asyncio
async def test_full_kb_confirm_room_booking_creates_room_booking_ticket(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-confirm-room-booking-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_room_booking",
        pending_data={
            "room_type": "Premier Twin Room",
            "stay_checkin_date": "Feb 27",
            "stay_checkout_date": "Feb 29",
            "guest_count": 2,
        },
    )
    expected_response = (
        "Your booking for a Premier Twin Room from Feb 27 to Feb 29 for 2 guests has been confirmed."
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="yes confirm",
            intent=IntentType.CONFIRMATION_YES,
            raw_intent="confirmation_yes",
            confidence=0.95,
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-confirm-room-booking-ticket",
            llm_output={},
            clear_pending_data=True,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.get_configured_cases", lambda: ["spa booking"])
    async def _fake_match_case_async(**_kwargs):
        return "spa booking"
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.match_configured_case_async", _fake_match_case_async)
    captured_payload_kwargs: dict[str, object] = {}

    def _fake_build_ticket_payload(**kwargs):
        captured_payload_kwargs.update(kwargs)
        return {"issue": "room booking ticket"}

    monkeypatch.setattr(
        "services.chat_service.ticketing_service.build_lumira_ticket_payload",
        _fake_build_ticket_payload,
    )

    async def _fake_create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-ROOM-1", status_code=200, response={"ok": True})

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _fake_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-confirm-room-booking-ticket",
            message="yes confirm",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-ROOM-1"
    assert response.metadata.get("ticket_sub_category") == "room_booking"
    assert captured_payload_kwargs.get("sub_category") == "room_booking"
    assert "room booking confirmed" in str(captured_payload_kwargs.get("issue", "")).lower()


@pytest.mark.asyncio
async def test_full_kb_confirm_room_booking_overrides_phase_denial_wording(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-confirm-room-booking-phase-denial",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_room_booking",
        pending_data={
            "room_type": "Lux Suite",
            "stay_checkin_date": "Mar 02",
            "stay_checkout_date": "Mar 03",
            "guest_count": 5,
            "_integration": {"phase": "pre_booking"},
        },
    )
    denial_text = (
        "Lux Suite is not available for Pre Booking phase. "
        "It is available in During Stay phase."
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=denial_text,
            normalized_query="yes confirm",
            intent=IntentType.CONFIRMATION_YES,
            raw_intent="confirmation_yes",
            confidence=0.95,
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-confirm-room-booking-phase-denial",
            llm_output={},
            clear_pending_data=True,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: False)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-confirm-room-booking-phase-denial",
            message="yes confirm",
            hotel_code="DEFAULT",
        )
    )

    text = str(response.message or "").lower()
    assert "room booking has been confirmed" in text
    assert "not available for pre booking phase" not in text
    assert response.state == ConversationState.COMPLETED


@pytest.mark.asyncio
async def test_full_kb_booking_confirmation_text_still_creates_spa_ticket(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-spa-booking-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_action=None,
        pending_data={},
    )
    expected_response = (
        "Your functional massage at 1 PM has been successfully booked."
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="yes confirm",
            intent=IntentType.CONFIRMATION_YES,
            raw_intent="confirmation_yes",
            confidence=0.95,
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-spa-booking-ticket",
            llm_output={},
            clear_pending_data=True,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.get_configured_cases", lambda: ["food order"])
    async def _fake_match_case_async(**_kwargs):
        return "food order"
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.match_configured_case_async", _fake_match_case_async)
    captured_payload_kwargs: dict[str, object] = {}

    def _fake_build_ticket_payload(**kwargs):
        captured_payload_kwargs.update(kwargs)
        return {"issue": "spa booking ticket"}

    monkeypatch.setattr(
        "services.chat_service.ticketing_service.build_lumira_ticket_payload",
        _fake_build_ticket_payload,
    )

    async def _fake_create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-SPA-1", status_code=200, response={"ok": True})

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _fake_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-spa-booking-ticket",
            message="yes confirm",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-SPA-1"
    assert response.metadata.get("ticket_sub_category") == "spa_booking"
    assert captured_payload_kwargs.get("sub_category") == "spa_booking"


@pytest.mark.asyncio
async def test_full_kb_confirm_order_creates_backend_ticket_without_changing_response(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-confirm-order-ticket",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_order",
        pending_data={
            "order_item": "Margherita Pizza",
            "order_quantity": 2,
            "order_total": 900,
        },
    )
    expected_response = (
        "Your order for 2 portions of Margherita Pizza has been confirmed. "
        "Our team will prepare it shortly. If you need anything else, feel free to ask!"
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="yes confirm",
            intent=IntentType.CONFIRMATION_YES,
            raw_intent="confirmation_yes",
            confidence=0.95,
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-confirm-order-ticket",
            llm_output={},
            clear_pending_data=True,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.get_configured_cases", lambda: [])
    monkeypatch.setattr(
        "services.chat_service.ticketing_service.build_lumira_ticket_payload",
        lambda **_kwargs: {"issue": "order ticket"},
    )

    async def _fake_create_ticket(_payload):
        return TicketingResult(success=True, ticket_id="LOCAL-322", status_code=200, response={"ok": True})

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _fake_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-confirm-order-ticket",
            message="yes confirm",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert response.metadata.get("ticket_created") is True
    assert response.metadata.get("ticket_id") == "LOCAL-322"
    assert response.metadata.get("ticket_sub_category") == "order_food"


@pytest.mark.asyncio
async def test_full_kb_confirm_transport_like_booking_skips_ticket_without_matching_case(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="s-fullkb-confirm-transport-skip",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_booking",
        pending_data={
            "service_name": "Airport Transfer",
            "party_size": "2",
            "time": "7 PM",
            "date": "tomorrow",
        },
    )
    expected_response = "Your airport transfer request has been confirmed."

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*args, **kwargs):
        return FullKBLLMResult(
            response_text=expected_response,
            normalized_query="yes confirm",
            intent=IntentType.CONFIRMATION_YES,
            raw_intent="confirmation_yes",
            confidence=0.95,
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-confirm-transport-skip",
            llm_output={},
            clear_pending_data=True,
            status="success",
        )

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", True)
    monkeypatch.setattr("services.chat_service.settings.full_kb_llm_passthrough_mode", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": [], "tools": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.refresh_summary", _fake_refresh_summary)
    monkeypatch.setattr("services.chat_service.full_kb_llm_service.run_turn", _fake_run_turn)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _capabilities=None: True)
    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.get_configured_cases",
        lambda: ["table booking", "room booking", "spa booking"],
    )

    async def _fake_match_case_async(**_kwargs):
        return ""

    monkeypatch.setattr(
        "services.chat_service.ticketing_agent_service.match_configured_case_async",
        _fake_match_case_async,
    )

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Transport-like booking should not create ticket without configured case match")

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _should_not_create_ticket)

    response = await service.process_message(
        ChatRequest(
            session_id="s-fullkb-confirm-transport-skip",
            message="yes confirm",
            hotel_code="DEFAULT",
        )
    )

    assert response.message == expected_response
    assert response.metadata.get("ticket_created") is None
    assert response.metadata.get("ticket_create_skip_reason") == "no_matching_configured_ticket_case"


def test_infer_transaction_booking_sub_category_prefers_transport_over_generic_table_intent():
    service = ChatService()
    sub_category = service._infer_transaction_booking_sub_category(
        pending_data={
            "booking_type": "table_booking",
            "service_name": "Airport Transfer",
        },
        response_text="Your airport transfer to Terminal 1 has been successfully booked.",
        matched_case="table booking at a restaurant",
    )

    assert sub_category == "transport_booking"
