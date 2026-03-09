import pytest

from schemas.chat import IntentType
from services.ticketing_agent_service import ticketing_agent_service


@pytest.fixture(autouse=True)
def _default_ticketing_config(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_services", lambda: [])


def test_order_intent_requires_ticket_true_does_not_activate_ticketing_agent():
    decision = ticketing_agent_service.decide(
        intent=IntentType.ORDER_FOOD,
        message="order margherita pizza",
        llm_response_text="Great choice, how many portions would you like?",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target="collect_order_quantity",
    )
    assert decision.activate is False
    assert decision.route == "none"


def test_room_service_actionable_request_activates_ticketing_agent():
    decision = ticketing_agent_service.decide(
        intent=IntentType.ROOM_SERVICE,
        message="please send two towels to room 305",
        llm_response_text="Sure, I can arrange that for you.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is True
    assert decision.routed_intent == IntentType.COMPLAINT
    assert decision.route == "complaint_handler"


def test_room_service_information_query_does_not_activate_ticketing_agent():
    decision = ticketing_agent_service.decide(
        intent=IntentType.ROOM_SERVICE,
        message="what room service options are available?",
        llm_response_text="Room service is available 24/7.",
        llm_ticketing_preference=False,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.route == "none"


def test_room_service_intent_with_stay_booking_text_does_not_activate_ticketing_agent():
    decision = ticketing_agent_service.decide(
        intent=IntentType.ROOM_SERVICE,
        message="i need a room from march 20 to march 22",
        llm_response_text="Sure, I can help with room booking details.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.route == "none"


def test_complaint_intent_always_activates_ticketing_agent():
    decision = ticketing_agent_service.decide(
        intent=IntentType.COMPLAINT,
        message="there is a cockroach in my room",
        llm_response_text="I am very sorry for this inconvenience.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is True
    assert decision.routed_intent == IntentType.COMPLAINT
    assert decision.route == "complaint_handler"


def test_identity_collection_pending_action_keeps_ticketing_agent_active():
    decision = ticketing_agent_service.decide(
        intent=IntentType.FAQ,
        message="alex +1 555 111 2222",
        llm_response_text="",
        llm_ticketing_preference=None,
        current_pending_action="collect_ticket_identity_details",
        pending_action_target=None,
    )
    assert decision.activate is True
    assert decision.routed_intent == IntentType.COMPLAINT
    assert decision.route == "complaint_handler"


def test_ticketing_plugin_service_toggle_off_blocks_activation(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": False,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": ["guest complaint requiring staff action"],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.COMPLAINT,
        message="there is a cockroach in my room",
        llm_response_text="I am sorry to hear this.",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.reason == "ticketing_plugin_service_disabled"


def test_ticketing_plugin_tool_toggle_off_blocks_activation(monkeypatch):
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_tools",
        lambda: [
            {
                "id": "ticketing",
                "name": "Ticketing",
                "type": "workflow",
                "handler": "ticket_create",
                "enabled": False,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": ["guest complaint requiring staff action"],
            }
        ],
    )
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_services", lambda: [])

    decision = ticketing_agent_service.decide(
        intent=IntentType.COMPLAINT,
        message="there is a cockroach in my room",
        llm_response_text="I am sorry to hear this.",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.reason == "ticketing_plugin_service_disabled"


def test_configured_ticketing_case_match_activates_plugin(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "table booking at restaurant requires staff follow-up",
                    "guest asks for human escalation",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.FAQ,
        message="Please arrange table booking at the restaurant for 4 guests tonight",
        llm_response_text="Sure, I can help with that.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.reason == "configured_transaction_case_deferred_until_confirmation"
    assert decision.source == "service_ticketing_cases"
    assert decision.matched_case == "table booking at restaurant requires staff follow-up"


def test_configured_ticketing_cases_gate_blocks_non_matching_issue(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "table booking at restaurant requires staff follow-up",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.COMPLAINT,
        message="there is a cockroach in my room",
        llm_response_text="I'm very sorry to hear that.",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.reason == "no_matching_configured_ticketing_case"


def test_broad_complaint_case_matches_operational_issue_message(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "Guest reports a complaint or maintenance issue that requires staff action.",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.COMPLAINT,
        message="there is a cockroach in my room",
        llm_response_text="I'm sorry to hear that.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is True
    assert decision.reason == "configured_ticketing_case_match"


def test_configured_cases_match_room_booking_case_not_table_case(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "table booking at a restaurant",
                    "room booking",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.FAQ,
        message="book a room from feb 27 to feb 29 for 2 guests",
        llm_response_text="Sure, I can proceed.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.reason == "configured_transaction_case_deferred_until_confirmation"
    assert decision.matched_case == "room booking"


def test_configured_cases_match_spa_booking_case(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "table booking at a restaurant",
                    "spa booking",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.FAQ,
        message="book a functional massage at 1 pm",
        llm_response_text="Done, would you like to confirm this booking?",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.reason == "configured_transaction_case_deferred_until_confirmation"
    assert decision.matched_case == "spa booking"


def test_configured_spa_case_still_defers_until_confirmation_even_if_response_mentions_staff(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "spa booking",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.FAQ,
        message="book a spa treatment",
        llm_response_text="I've noted your request to book a spa treatment. I'll forward this to our staff for further assistance.",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.routed_intent is None
    assert decision.reason == "configured_transaction_case_deferred_until_confirmation"
    assert decision.matched_case == "spa booking"


def test_non_transactional_configured_case_requires_escalation_signal(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "Pre-booking website booking issue remains unresolved (OTP/login/form/technical error).",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.FAQ,
        message="hi, i need a room",
        llm_response_text="Sure, please share your check-in and check-out dates.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
        configured_case_match_override="Pre-booking website booking issue remains unresolved (OTP/login/form/technical error).",
    )
    assert decision.activate is False
    assert decision.reason == "configured_case_match_missing_escalation_signal"
    assert decision.matched_case.startswith("Pre-booking website booking issue")


def test_non_transactional_configured_case_respects_llm_ticketing_preference_signal(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "During-stay maintenance issue (AC/electrical/plumbing/device) requires engineering intervention.",
                ],
            }
        ],
    )
    decision = ticketing_agent_service.decide(
        intent=IntentType.ROOM_SERVICE,
        message="ac leaking badly, create urgent maintenance ticket",
        llm_response_text="I've created an urgent maintenance ticket for the leaking AC.",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target=None,
        configured_case_match_override="During-stay maintenance issue (AC/electrical/plumbing/device) requires engineering intervention.",
    )

    assert decision.activate is True
    assert decision.route == "complaint_handler"
    assert decision.reason == "configured_ticketing_case_match"


def test_match_configured_case_supports_transport_shorthand(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "transport booking",
                    "room booking",
                ],
            }
        ],
    )

    matched = ticketing_agent_service.match_configured_case(
        "please arrange airport pickup for tomorrow morning"
    )

    assert matched == "transport booking"


@pytest.mark.asyncio
async def test_decide_async_prefers_llm_case_match(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": ["transport booking", "room booking"],
            }
        ],
    )

    async def _fake_llm_case_match(**_kwargs):
        return "transport booking", True

    monkeypatch.setattr(
        ticketing_agent_service,
        "_llm_match_configured_ticketing_case",
        _fake_llm_case_match,
    )

    decision = await ticketing_agent_service.decide_async(
        intent=IntentType.FAQ,
        message="please arrange airport pickup",
        llm_response_text="I can help with that.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
        conversation_excerpt="User asked for airport pickup tomorrow",
    )

    assert decision.activate is False
    assert decision.reason == "configured_transaction_case_deferred_until_confirmation"
    assert decision.matched_case == "transport booking"


@pytest.mark.asyncio
async def test_decide_async_falls_back_to_semantic_match_when_llm_unavailable(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": ["room booking", "table booking"],
            }
        ],
    )

    async def _fake_llm_case_match(**_kwargs):
        return "", False

    monkeypatch.setattr(
        ticketing_agent_service,
        "_llm_match_configured_ticketing_case",
        _fake_llm_case_match,
    )
    monkeypatch.setattr(
        "services.ticketing_agent_service.settings.ticketing_case_match_fallback_enabled",
        True,
    )

    decision = await ticketing_agent_service.decide_async(
        intent=IntentType.FAQ,
        message="book a room from feb 27 to feb 29",
        llm_response_text="Sure, please confirm dates.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
        conversation_excerpt="User wants to reserve a room",
    )

    assert decision.activate is False
    assert decision.reason == "configured_transaction_case_deferred_until_confirmation"
    assert decision.matched_case == "room booking"


@pytest.mark.asyncio
async def test_match_configured_case_async_falls_back_when_llm_returns_no_match(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": ["in room dining food order", "guest complaint requiring staff action"],
            }
        ],
    )
    monkeypatch.setattr(
        "services.ticketing_agent_service.settings.ticketing_case_match_fallback_enabled",
        True,
    )

    async def _fake_llm_case_match(**_kwargs):
        # Simulate model returning "no case", which should now still fall back.
        return "", True

    monkeypatch.setattr(
        ticketing_agent_service,
        "_llm_match_configured_ticketing_case",
        _fake_llm_case_match,
    )

    matched = await ticketing_agent_service.match_configured_case_async(
        message="food order confirmed: 2 x opera",
        conversation_excerpt="User confirmed in-room dining order.",
        llm_response_text="Your order has been confirmed.",
    )

    assert matched == "in room dining food order"


@pytest.mark.asyncio
async def test_human_request_keeps_matched_case_for_configured_handoff(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": ["Guest requests human escalation or live agent support."],
            }
        ],
    )

    async def _fake_llm_case_match(**_kwargs):
        return "Guest requests human escalation or live agent support.", True

    monkeypatch.setattr(
        ticketing_agent_service,
        "_llm_match_configured_ticketing_case",
        _fake_llm_case_match,
    )

    decision = await ticketing_agent_service.decide_async(
        intent=IntentType.HUMAN_REQUEST,
        message="talk to human",
        llm_response_text="I'll connect you to our staff.",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target=None,
        conversation_excerpt="User asked to speak to a human agent.",
    )

    assert decision.activate is True
    assert decision.routed_intent == IntentType.HUMAN_REQUEST
    assert decision.matched_case == "Guest requests human escalation or live agent support."


@pytest.mark.asyncio
async def test_human_request_with_transactional_case_routes_to_handoff(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_tools", lambda: [])
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_services",
        lambda: [
            {
                "id": "ticketing_agent",
                "name": "Ticketing Agent",
                "type": "plugin",
                "is_active": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": ["spa booking"],
            }
        ],
    )

    async def _fake_llm_case_match(**_kwargs):
        return "spa booking", True

    monkeypatch.setattr(
        ticketing_agent_service,
        "_llm_match_configured_ticketing_case",
        _fake_llm_case_match,
    )

    decision = await ticketing_agent_service.decide_async(
        intent=IntentType.HUMAN_REQUEST,
        message="connect me to spa staff",
        llm_response_text="I can connect you with our spa staff.",
        llm_ticketing_preference=True,
        current_pending_action=None,
        pending_action_target=None,
        conversation_excerpt="User asked to connect with spa staff.",
    )

    assert decision.activate is True
    assert decision.routed_intent == IntentType.HUMAN_REQUEST
    assert decision.route == "escalation_handler"
    assert decision.reason == "configured_transaction_case_human_request"
    assert decision.matched_case == "spa booking"


def test_configured_ticketing_case_match_works_from_tools(monkeypatch):
    monkeypatch.setattr(
        "services.ticketing_agent_service.config_service.get_tools",
        lambda: [
            {
                "id": "ticketing",
                "name": "Ticketing",
                "type": "workflow",
                "handler": "ticket_create",
                "enabled": True,
                "ticketing_plugin_enabled": True,
                "ticketing_cases": [
                    "table booking at restaurant requires staff follow-up",
                    "guest asks for human escalation",
                ],
            }
        ],
    )
    monkeypatch.setattr("services.ticketing_agent_service.config_service.get_services", lambda: [])

    decision = ticketing_agent_service.decide(
        intent=IntentType.FAQ,
        message="Please arrange table booking at the restaurant for 4 guests tonight",
        llm_response_text="Sure, I can help with that.",
        llm_ticketing_preference=None,
        current_pending_action=None,
        pending_action_target=None,
    )
    assert decision.activate is False
    assert decision.reason == "configured_transaction_case_deferred_until_confirmation"
    assert decision.matched_case == "table booking at restaurant requires staff follow-up"


@pytest.mark.asyncio
async def test_llm_case_match_rejects_cross_domain_result(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.settings.ticketing_case_match_use_llm", True)
    monkeypatch.setattr("services.ticketing_agent_service.settings.openai_api_key", "test-key")

    async def _fake_chat_with_json(_messages, model=None, temperature=0.0):
        return {
            "should_create_ticket": True,
            "matched_case": "table booking",
            "reason": "booking confirmation",
        }

    monkeypatch.setattr("services.ticketing_agent_service.llm_client.chat_with_json", _fake_chat_with_json)

    matched_case, llm_used = await ticketing_agent_service._llm_match_configured_ticketing_case(
        message="yes confirm",
        conversation_excerpt="User requested airport transfer to terminal 1",
        llm_response_text="Your airport transfer has been successfully booked.",
        configured_cases=["table booking", "room booking"],
    )

    assert llm_used is True
    assert matched_case == ""


@pytest.mark.asyncio
async def test_llm_case_match_allows_excerpt_for_generic_confirmation(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.settings.ticketing_case_match_use_llm", True)
    monkeypatch.setattr("services.ticketing_agent_service.settings.openai_api_key", "test-key")

    async def _fake_chat_with_json(_messages, model=None, temperature=0.0):
        return {
            "should_create_ticket": True,
            "matched_case": "table booking",
            "reason": "table reservation flow",
        }

    monkeypatch.setattr("services.ticketing_agent_service.llm_client.chat_with_json", _fake_chat_with_json)

    matched_case, llm_used = await ticketing_agent_service._llm_match_configured_ticketing_case(
        message="yes confirm",
        conversation_excerpt="User asked to book table at Kadak for 4 guests at 8 PM",
        llm_response_text="Your booking has been confirmed.",
        configured_cases=["table booking", "room booking"],
    )

    assert llm_used is True
    assert matched_case == "table booking"


@pytest.mark.asyncio
async def test_llm_case_match_prompt_includes_selected_phase(monkeypatch):
    monkeypatch.setattr("services.ticketing_agent_service.settings.ticketing_case_match_use_llm", True)
    monkeypatch.setattr("services.ticketing_agent_service.settings.openai_api_key", "test-key")
    captured: dict[str, str] = {}

    async def _fake_chat_with_json(messages, model=None, temperature=0.0):
        _ = model, temperature
        if isinstance(messages, list) and len(messages) >= 2:
            captured["user_prompt"] = str(messages[1].get("content") or "")
        return {
            "should_create_ticket": False,
            "matched_case": "",
            "reason": "phase not eligible",
        }

    monkeypatch.setattr("services.ticketing_agent_service.llm_client.chat_with_json", _fake_chat_with_json)

    matched_case, llm_used = await ticketing_agent_service._llm_match_configured_ticketing_case(
        message="there is a cockroach in my room",
        conversation_excerpt="Guest is in pre-booking phase and reported room hygiene issue.",
        llm_response_text="I understand your concern.",
        configured_cases=["room complaint", "spa booking"],
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
    )

    assert llm_used is True
    assert matched_case == ""
    assert "Selected user journey phase: Pre Booking (pre_booking)" in str(
        captured.get("user_prompt") or ""
    )
