import pytest
from types import SimpleNamespace

from handlers.base_handler import HandlerResult
from schemas.chat import ChatRequest, ConversationContext, ConversationState, IntentResult, IntentType, MessageRole
from services.chat_service import ChatService
from services.response_validator import response_validator


def test_response_validator_enforces_dont_rule_for_intercity_commit():
    context = ConversationContext(session_id="p1", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "Book intercity cab from Mumbai to Pune now")

    validation = response_validator.validate(
        response_text="I will arrange an intercity cab for you right away.",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.8, entities={}),
        context=context,
        capabilities_summary={
            "services": {"intercity_cab": False},
            "nlu_policy": {"dos": [], "donts": ["Do not commit intercity transfers unless explicitly enabled."]},
            "restaurants": [],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    issue_codes = {issue.code for issue in validation.issues}
    assert ("policy_dont_intercity_commit" in issue_codes) or ("intercity_contradiction" in issue_codes)


def test_response_validator_enforces_do_rule_for_missing_service_details():
    context = ConversationContext(session_id="p2", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "Book room service")

    validation = response_validator.validate(
        response_text="Done, your request is confirmed.",
        intent_result=IntentResult(intent=IntentType.ROOM_SERVICE, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": ["Confirm timing, location, and count for service requests."], "donts": []},
            "restaurants": [],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert any(issue.code == "policy_do_missing_detail_confirmation" for issue in validation.issues)
    assert "preferred time" in (validation.replacement_response or "").lower()


def test_response_validator_does_not_force_table_time_prompt_for_room_booking_flow():
    context = ConversationContext(
        session_id="p2-room-booking-do-rule",
        hotel_code="DEFAULT",
        pending_action="collect_room_booking_details",
        pending_data={"stay_checkin_date": "Mar 02", "stay_checkout_date": "Mar 03"},
    )
    context.add_message(MessageRole.USER, "i need the most luxurious one")

    validation = response_validator.validate(
        response_text="Please share the number of guests for your stay.",
        intent_result=IntentResult(
            intent=IntentType.TABLE_BOOKING,
            confidence=0.9,
            entities={"booking_sub_category": "room_booking", "booking_type": "room_booking"},
        ),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": ["Confirm timing, location, and count for service requests."], "donts": []},
            "restaurants": [],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is True


def test_response_validator_blocks_unconfigured_local_cab_promise():
    context = ConversationContext(session_id="p2b", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "book local cab from hotel to bandra at 2 pm")

    validation = response_validator.validate(
        response_text="Sure, I will arrange your local cab for 2 PM.",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "services": {"transport": True, "local_transport": False, "local_cab": False},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [],
            "service_catalog": [
                {
                    "id": "transport",
                    "name": "Transport",
                    "type": "service",
                    "description": "Airport transfer assistance",
                    "is_active": True,
                }
            ],
        },
        capability_check_allowed=False,
        capability_reason=(
            "That specific transport request is not configured for instant booking here yet. "
            "I can connect you with our staff team to assist manually."
        ),
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert any(issue.code == "capability_violation" for issue in validation.issues)


def test_response_validator_replaces_unconfigured_local_cab_clarification_loop():
    context = ConversationContext(session_id="p2c", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "book local cab from hotel to bandra at 2 pm")

    validation = response_validator.validate(
        response_text="Could you please share your pickup and drop-off locations?",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "services": {"transport": True},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [],
            "service_catalog": [
                {
                    "id": "transport",
                    "name": "Transport",
                    "type": "service",
                    "description": "Airport transfer assistance",
                    "is_active": True,
                }
            ],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert any(issue.code == "clarification_for_unconfigured_service_request" for issue in validation.issues)
    assert "connect" in str(validation.replacement_response or "").lower()
    assert "staff" in str(validation.replacement_response or "").lower()


def test_response_validator_rewrites_out_of_phase_transaction_cta_to_info_only():
    context = ConversationContext(
        session_id="p2d",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    context.add_message(MessageRole.USER, "do u have spa")

    validation = response_validator.validate(
        response_text=(
            "Yes, we have a spa with multiple treatments. "
            "If you need more details or wish to book a treatment, feel free to ask!"
        ),
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [],
            "service_catalog": [
                {
                    "id": "hotel_enquiry_sales",
                    "name": "Hotel Enquiry & Sales",
                    "type": "service",
                    "description": "Answer hotel questions and guide guests toward booking decisions.",
                    "phase_id": "pre_booking",
                    "is_active": True,
                },
                {
                    "id": "spa_recreation_booking",
                    "name": "Spa & Recreation Booking",
                    "type": "service",
                    "description": "Handle spa and recreation bookings during stay.",
                    "phase_id": "during_stay",
                    "is_active": True,
                },
            ],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert any(issue.code == "phase_unavailable_transaction_cta" for issue in validation.issues)
    replacement = str(validation.replacement_response or "").lower()
    assert "wish to book" not in replacement
    assert "available in during stay phase" in replacement
    assert "pre booking phase" in replacement


def test_response_validator_keeps_transaction_cta_when_phase_service_matches():
    context = ConversationContext(
        session_id="p2e",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "during_stay"}},
    )
    context.add_message(MessageRole.USER, "do u have spa")

    validation = response_validator.validate(
        response_text=(
            "Yes, we have a spa with multiple treatments. "
            "If you need more details or wish to book a treatment, feel free to ask!"
        ),
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [],
            "service_catalog": [
                {
                    "id": "spa_recreation_booking",
                    "name": "Spa & Recreation Booking",
                    "type": "service",
                    "description": "Handle spa and recreation bookings during stay.",
                    "phase_id": "during_stay",
                    "is_active": True,
                }
            ],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is True


def test_response_validator_scopes_external_hotel_recommendation_to_current_property():
    context = ConversationContext(
        session_id="p2f",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    context.add_message(
        MessageRole.USER,
        "im looking for a hotel near beach in mumbai, can you recommend options",
    )

    validation = response_validator.validate(
        response_text=(
            "I'm sorry, but I don't have specific information about hotels near the beach right now. "
            "Would you like me to connect you with our staff for personalized recommendations?"
        ),
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "hotel_name": "ICONIQA Demo Hotel",
            "city": "Mumbai",
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert any(issue.code == "external_hotel_scope_enforcement" for issue in validation.issues)
    replacement = str(validation.replacement_response or "").lower()
    assert "iconiqa demo hotel" in replacement
    assert "nearby attractions" in replacement


def test_response_validator_rewrites_unhelpful_nearby_area_unavailable_reply(monkeypatch):
    context = ConversationContext(
        session_id="p2g",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    context.add_message(MessageRole.USER, "is there a beach near ur hotel")

    class _LookupResult:
        handled = True
        answer = (
            "Our concierge will help curate an experience, including shopping in Chor Bazaar, "
            "an Art-Deco tour, and a visit to Mahakali Caves."
        )

    monkeypatch.setattr(
        "services.response_validator.kb_direct_lookup_service.answer_question",
        lambda *args, **kwargs: _LookupResult(),
    )

    validation = response_validator.validate(
        response_text=(
            "I'm sorry, but I don't have specific information about beaches near ICONIQA Demo Hotel in Mumbai at the moment. "
            "Would you like me to connect you with our staff for personalized recommendations?"
        ),
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "hotel_name": "ICONIQA Demo Hotel",
            "city": "Mumbai",
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert any(issue.code == "nearby_area_info_rewrite" for issue in validation.issues)
    replacement = str(validation.replacement_response or "").lower()
    assert "nearby sightseeing around iconiqa demo hotel" in replacement
    assert "chor bazaar" in replacement
    assert "beach plans specifically" in replacement


def test_service_catalog_shortcut_returns_info_for_service_query():
    service = ChatService()
    context = ConversationContext(session_id="p3", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "What are Kadak timings?",
        context,
        {
            "service_catalog": [
                {
                    "id": "kadak",
                    "name": "Kadak",
                    "type": "restaurant",
                    "description": "Indian snacks and chai",
                    "hours": {"open": "09:00", "close": "23:00"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is not None
    assert matched["service_id"] == "kadak"
    assert matched["match_type"] == "service_catalog_info"
    assert "09:00" in matched["response_text"]


def test_service_catalog_shortcut_supports_generic_service_info_queries():
    service = ChatService()
    context = ConversationContext(session_id="p3b", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "Do you provide hotel booking and what are the timings?",
        context,
        {
            "service_catalog": [
                {
                    "id": "hotel_booking",
                    "name": "Hotel Booking",
                    "type": "front_desk",
                    "description": "Room booking assistance",
                    "hours": {"open": "00:00", "close": "23:59"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is not None
    assert matched["service_id"] == "hotel_booking"
    assert matched["match_type"] == "service_catalog_info"
    assert "00:00" in matched["response_text"]


def test_service_catalog_shortcut_supports_generic_service_action_queries():
    service = ChatService()
    context = ConversationContext(session_id="p3c", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "I need hotel booking help",
        context,
        {
            "service_catalog": [
                {
                    "id": "hotel_booking",
                    "name": "Hotel Booking",
                    "type": "front_desk",
                    "description": "Room booking assistance",
                    "hours": {"open": "00:00", "close": "23:59"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is not None
    assert matched["service_id"] == "hotel_booking"
    assert matched["match_type"] == "service_catalog_action"


def test_service_catalog_shortcut_handoffs_unconfigured_local_cab_request():
    service = ChatService()
    context = ConversationContext(session_id="p3d", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "book local cab from hotel to bandra at 2 pm",
        context,
        {
            "services": {"transport": True, "local_transport": False, "local_cab": False},
            "service_catalog": [
                {
                    "id": "transport",
                    "name": "Transport",
                    "type": "service",
                    "description": "Airport transfer assistance",
                    "is_active": True,
                }
            ],
        },
    )

    assert matched is not None
    assert matched["match_type"] == "service_catalog_unavailable_handoff"
    assert "local cab" in str(matched["service_name"]).lower()
    assert "connect you with our staff team" in str(matched["response_text"]).lower()


def test_ticketing_phase_mismatch_detects_out_of_phase_service_request(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-mismatch-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa bookings",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_checkin", "name": "Pre Checkin"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    mismatch = service._detect_ticketing_phase_service_mismatch(
        message="book spa at 6 pm",
        context=context,
        pending_data={},
        entities={},
    )

    assert mismatch is not None
    assert mismatch["service_name"] == "Spa & Recreation Booking"
    assert mismatch["current_phase_id"] == "pre_checkin"
    assert mismatch["service_phase_id"] == "during_stay"
    assert "not available for Pre Checkin phase" in mismatch["response_text"]


def test_ticketing_phase_mismatch_skips_same_phase_service_request(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-mismatch-2",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "booking_modification",
                "name": "Booking Modification",
                "type": "service",
                "description": "Modify booking",
                "phase_id": "pre_checkin",
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [{"id": "pre_checkin", "name": "Pre Checkin"}],
    )

    mismatch = service._detect_ticketing_phase_service_mismatch(
        message="modify booking dates",
        context=context,
        pending_data={},
        entities={},
    )

    assert mismatch is None


def test_ticketing_phase_mismatch_detects_info_query_for_out_of_phase_service(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-mismatch-3",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa bookings and treatments",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_checkin", "name": "Pre Checkin"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    mismatch = service._detect_ticketing_phase_service_mismatch(
        message="what spa services are available?",
        context=context,
        pending_data={},
        entities={},
    )

    assert mismatch is not None
    assert mismatch["service_name"] == "Spa & Recreation Booking"
    assert mismatch["current_phase_id"] == "pre_checkin"
    assert mismatch["service_phase_id"] == "during_stay"


def test_phase_service_unavailable_detects_unconfigured_request_in_current_phase(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-unavailable-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [{"id": "pre_booking", "name": "Pre Booking"}],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="book a table at kadak",
        intent=IntentType.TABLE_BOOKING,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is not None
    assert unavailable["current_phase_id"] == "pre_booking"
    assert unavailable["phase_service_unavailable"] is True
    assert "not available for Pre Booking phase" in unavailable["response_text"]
    lowered_actions = [str(item).lower() for item in unavailable.get("suggested_actions") or []]
    assert "hotel enquiry & sales" in lowered_actions
    assert "room discovery" in lowered_actions
    assert "talk to human" not in lowered_actions


def test_phase_service_unavailable_blocks_generic_transaction_intent_without_phase_support(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-unavailable-2",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Support in-room dining and meal orders.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="im hungry",
        intent=IntentType.ORDER_FOOD,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is not None
    assert unavailable["phase_service_unavailable"] is True
    assert "not available for Pre Booking phase" in unavailable["response_text"]
    assert "Food ordering" in unavailable["response_text"]


def test_phase_service_unavailable_allows_generic_transaction_intent_with_phase_support(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-unavailable-3",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "during_stay"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Support in-room dining and meal orders.",
                "phase_id": "during_stay",
                "is_active": True,
            },
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="im hungry",
        intent=IntentType.ORDER_FOOD,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is None


def test_phase_service_unavailable_allows_same_phase_faq_action_label_match(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-unavailable-4",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "post_checkout"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "postcheckout_invoice_support",
                "name": "Billing & Invoice Support",
                "type": "service",
                "description": "Handle post-stay invoice and billing clarification requests.",
                "phase_id": "post_checkout",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Support in-room dining and meal orders.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "post_checkout", "name": "Post Checkout"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="need invoice copy",
        intent=IntentType.FAQ,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is None


def test_phase_service_unavailable_detects_faq_action_for_known_out_of_phase_service(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-unavailable-5",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="book a spa treatment",
        intent=IntentType.FAQ,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is not None
    assert "not available for Pre Booking phase" in unavailable["response_text"]


def test_phase_service_unavailable_blocks_transport_faq_action_when_not_in_phase(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-unavailable-6",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "transport_on_demand",
                "name": "Transport On Demand",
                "type": "service",
                "description": "Arrange local transport, pickup, and drop requests.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="book local cab from hotel to bandra",
        intent=IntentType.FAQ,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is not None
    assert unavailable.get("phase_service_unavailable") is True
    assert "not available for pre booking phase" in str(unavailable.get("response_text") or "").lower()
    assert "during stay phase" in str(unavailable.get("response_text") or "").lower()
    lowered_actions = [str(item).lower() for item in unavailable.get("suggested_actions") or []]
    assert "talk to human" not in lowered_actions


def test_service_alias_match_ignores_short_substring_alias():
    service = ChatService()
    matched = service._match_phase_service_from_rows(
        message="need invoice copy",
        services=[
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Support in-room dining and meal orders.",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
        min_score=0.70,
    )

    assert matched is None


def test_phase_gate_intent_inference_uses_word_boundaries():
    assert ChatService._infer_phase_gate_transactional_intent("book a spa treatment") == IntentType.TABLE_BOOKING
    assert ChatService._infer_phase_gate_transactional_intent("im hungry") == IntentType.ORDER_FOOD
    assert ChatService._infer_phase_gate_transactional_intent("book a cab to airport") == IntentType.TABLE_BOOKING
    assert ChatService._infer_phase_gate_transactional_intent("this is spacious") is None


def test_ticketing_phase_mismatch_multi_intent_mentions_additional_out_of_phase_service(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-mismatch-multi-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Support in-room dining and meal orders during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    mismatch = service._detect_ticketing_phase_service_mismatch(
        message="hi who are you i need spa and i am hungry",
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert mismatch is not None
    text = str(mismatch["response_text"]).lower()
    assert "spa & recreation booking is not available for pre booking phase" in text
    assert "in-room dining support is available in during stay phase" in text


def test_phase_service_unavailable_handles_multi_intent_with_sales_wording(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-unavailable-multi-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Support in-room dining and meal orders during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="im hungry and i need spa",
        intent=IntentType.ORDER_FOOD,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is not None
    text = str(unavailable["response_text"]).lower()
    assert "not available for pre booking phase" in text
    assert "available in during stay phase" in text
    assert "after check-in" in text
    assert ("food ordering" in text) or ("in-room dining support" in text) or ("spa & recreation booking" in text)


def test_phase_managed_service_request_detects_do_you_have_query(monkeypatch):
    service = ChatService()
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
    )

    matched = service._match_phase_managed_service_request("do u have spa")
    assert matched is not None
    assert matched["service_name"] == "Spa & Recreation Booking"


@pytest.mark.asyncio
async def test_llm_preprocessor_rewrites_typo_message(monkeypatch):
    service = ChatService()

    async def _fake_chat(*_args, **_kwargs):
        return "do you have a pool in your hotel"

    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.chat_service.llm_client.chat", _fake_chat)

    rewritten = await service._preprocess_user_message_with_llm("do u have ap oool in ur hotel")
    assert rewritten == "do you have a pool in your hotel"


@pytest.mark.asyncio
async def test_llm_preprocessor_prompt_includes_selected_phase(monkeypatch):
    service = ChatService()
    captured: dict[str, str] = {}

    async def _fake_chat(*args, **kwargs):
        messages = kwargs.get("messages")
        if messages is None and args:
            messages = args[0]
        if isinstance(messages, list) and messages:
            captured["system_prompt"] = str(messages[0].get("content") or "")
        return "do you have a pool in your hotel"

    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.chat_service.llm_client.chat", _fake_chat)

    rewritten = await service._preprocess_user_message_with_llm(
        "do u have ap oool in ur hotel",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
    )

    assert rewritten == "do you have a pool in your hotel"
    system_prompt = str(captured.get("system_prompt") or "")
    assert "Selected user journey phase: Pre Booking (pre_booking)." in system_prompt


@pytest.mark.asyncio
async def test_process_message_routes_with_llm_preprocessed_text(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="preprocess-routing-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    captured: dict[str, str] = {}

    async def _fake_chat(*_args, **_kwargs):
        return "do you have a pool in your hotel"

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_classify(message, _history, _ctx):
        captured["message"] = message
        return IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={})

    async def _fake_dispatch(_message, _intent_result, _context, _capabilities, _db_session=None):
        return HandlerResult(
            response_text="Pool details available.",
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            metadata={},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", True)
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.llm_client.chat", _fake_chat)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr("services.chat_service.config_service.is_intent_enabled", lambda _intent_id: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="preprocess-routing-1",
            message="do u have ap oool in ur hotel",
            hotel_code="DEFAULT",
        )
    )

    assert captured.get("message") == "do you have a pool in your hotel"
    assert response.message == "Pool details available."


@pytest.mark.asyncio
async def test_process_message_passes_selected_phase_context_to_classifier(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-context-classifier-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )
    captured: dict[str, str] = {}

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_classify(message, _history, llm_context):
        captured["message"] = message
        captured["selected_phase_id"] = str(llm_context.get("selected_phase_id") or "")
        captured["selected_phase_name"] = str(llm_context.get("selected_phase_name") or "")
        return IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={})

    async def _fake_dispatch(_message, _intent_result, _context, _capabilities, _db_session=None):
        return HandlerResult(
            response_text="Sure, here are the room details.",
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            metadata={},
        )

    monkeypatch.setattr("services.chat_service.settings.chat_llm_preprocess_enabled", False)
    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr("services.chat_service.config_service.is_intent_enabled", lambda _intent_id: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(service, "_dispatch_to_handler", _fake_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="phase-context-classifier-1",
            message="i need room details",
            hotel_code="DEFAULT",
            metadata={"phase": "pre_booking"},
        )
    )

    assert response.message == "Sure, here are the room details."
    assert captured.get("selected_phase_id") == "pre_booking"
    assert captured.get("selected_phase_name") == "Pre Booking"


@pytest.mark.asyncio
async def test_full_kb_plugin_ticket_skips_out_of_phase_service_request(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-plugin-skip-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )
    request = ChatRequest(
        session_id="phase-plugin-skip-1",
        message="book spa at 6 pm",
        hotel_code="DEFAULT",
    )
    llm_result = SimpleNamespace(
        normalized_query="book spa at 6 pm",
        pending_action=None,
        pending_data={},
        room_number="",
        response_text="",
        llm_output={},
    )

    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps: True)
    async def _fake_decide_async(**_kwargs):
        return SimpleNamespace(
            activate=True,
            routed_intent=IntentType.COMPLAINT,
            route="complaint_handler",
            reason="configured_ticketing_case_match",
            source="test",
            matched_case="During-stay spa/recreation booking request requires staff confirmation.",
        )

    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _fake_decide_async)
    monkeypatch.setattr(
        service,
        "_extract_full_kb_entities_for_handler",
        lambda _llm: {},
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa bookings",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_checkin", "name": "Pre Checkin"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    meta = await service._maybe_create_full_kb_plugin_ticket(
        request=request,
        context=context,
        capabilities_summary={},
        llm_result=llm_result,
        effective_intent=IntentType.FAQ,
        next_state=ConversationState.IDLE,
        current_pending_action=None,
        pending_data_snapshot={},
        response_text="Let me check this for you.",
    )

    assert meta.get("ticket_create_skipped") is True
    assert meta.get("ticket_create_skip_reason") == "phase_service_mismatch"
    assert meta.get("full_kb_ticketing_phase_gate") is True
    assert meta.get("phase_gate_current_phase_id") == "pre_checkin"
    assert meta.get("phase_gate_service_phase_id") == "during_stay"


@pytest.mark.asyncio
async def test_full_kb_plugin_ticket_defers_transactional_request_without_escalation_signal(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-plugin-transactional-defer-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    request = ChatRequest(
        session_id="phase-plugin-transactional-defer-1",
        message="hi i need a room",
        hotel_code="DEFAULT",
    )
    llm_result = SimpleNamespace(
        normalized_query="hi i need a room",
        pending_action=None,
        pending_data={},
        room_number="",
        response_text="",
        llm_output={},
    )

    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps: True)

    async def _fake_decide_async(**_kwargs):
        return SimpleNamespace(
            activate=True,
            routed_intent=IntentType.COMPLAINT,
            route="complaint_handler",
            reason="configured_ticketing_case_match",
            source="test",
            matched_case="Pre-booking website booking issue remains unresolved (OTP/login/form/technical error).",
        )

    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _fake_decide_async)
    monkeypatch.setattr(service, "_extract_full_kb_entities_for_handler", lambda _llm: {})

    async def _should_not_create_ticket(_payload):
        raise AssertionError("Transactional room booking request should defer ticket creation until confirmation")

    monkeypatch.setattr("services.chat_service.ticketing_service.create_ticket", _should_not_create_ticket)

    meta = await service._maybe_create_full_kb_plugin_ticket(
        request=request,
        context=context,
        capabilities_summary={},
        llm_result=llm_result,
        effective_intent=IntentType.TABLE_BOOKING,
        next_state=ConversationState.IDLE,
        current_pending_action=None,
        pending_data_snapshot={},
        response_text="Sure, please share check-in/check-out dates.",
    )

    assert meta == {}


@pytest.mark.asyncio
async def test_full_kb_plugin_ticket_skips_when_phase_service_ticketing_disabled(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-plugin-ticketing-disabled-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )
    request = ChatRequest(
        session_id="phase-plugin-ticketing-disabled-1",
        message="please modify my booking dates",
        hotel_code="DEFAULT",
    )
    llm_result = SimpleNamespace(
        normalized_query="please modify my booking dates",
        pending_action=None,
        pending_data={},
        room_number="",
        response_text="",
        llm_output={},
    )

    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps: True)

    async def _fake_decide_async(**_kwargs):
        return SimpleNamespace(
            activate=True,
            routed_intent=IntentType.COMPLAINT,
            route="complaint_handler",
            reason="configured_ticketing_case_match",
            source="test",
            matched_case="Pre-checkin booking modification needs staff action.",
        )

    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _fake_decide_async)
    monkeypatch.setattr(
        service,
        "_extract_full_kb_entities_for_handler",
        lambda _llm: {},
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "booking_modification",
                "name": "Booking Modification",
                "type": "service",
                "description": "Modify confirmed bookings",
                "phase_id": "pre_checkin",
                "ticketing_enabled": False,
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [{"id": "pre_checkin", "name": "Pre Checkin"}],
    )

    meta = await service._maybe_create_full_kb_plugin_ticket(
        request=request,
        context=context,
        capabilities_summary={},
        llm_result=llm_result,
        effective_intent=IntentType.FAQ,
        next_state=ConversationState.IDLE,
        current_pending_action=None,
        pending_data_snapshot={},
        response_text="Let me help with that.",
    )

    assert meta.get("ticket_create_skipped") is True
    assert meta.get("ticket_create_skip_reason") == "phase_service_ticketing_disabled"
    assert meta.get("phase_gate_current_phase_id") == "pre_checkin"
    assert meta.get("phase_gate_service_phase_id") == "pre_checkin"


@pytest.mark.asyncio
async def test_full_kb_transaction_ticket_skips_out_of_phase_service_request(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-transaction-skip-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "order_food_support",
                "name": "Order Food",
                "type": "service",
                "description": "Handle in-room dining orders",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_checkin", "name": "Pre Checkin"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    meta = await service._maybe_create_full_kb_transaction_ticket(
        context=context,
        capabilities_summary={},
        current_pending_action="confirm_order",
        effective_intent=IntentType.CONFIRMATION_YES,
        next_state=ConversationState.COMPLETED,
        pending_data_snapshot={
            "items": [{"name": "Pasta", "quantity": 1}],
            "restaurant_name": "In-Room Dining",
        },
        response_text="Your order has been confirmed.",
    )

    assert meta.get("ticket_create_skipped") is True
    assert meta.get("ticket_create_skip_reason") == "phase_service_mismatch"
    assert meta.get("full_kb_ticketing_phase_gate") is True
    assert meta.get("phase_gate_current_phase_id") == "pre_checkin"
    assert meta.get("phase_gate_service_phase_id") == "during_stay"


@pytest.mark.asyncio
async def test_full_kb_transaction_ticket_skips_when_phase_service_ticketing_disabled(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-transaction-ticketing-disabled-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps: True)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "booking_modification",
                "name": "Booking Modification",
                "type": "service",
                "description": "Modify confirmed bookings",
                "phase_id": "pre_checkin",
                "ticketing_enabled": False,
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [{"id": "pre_checkin", "name": "Pre Checkin"}],
    )

    meta = await service._maybe_create_full_kb_transaction_ticket(
        context=context,
        capabilities_summary={},
        current_pending_action="confirm_booking",
        effective_intent=IntentType.CONFIRMATION_YES,
        next_state=ConversationState.COMPLETED,
        pending_data_snapshot={
            "booking_service": "Booking Modification",
            "date": "tomorrow",
        },
        response_text="Your booking update is confirmed.",
    )

    assert meta.get("ticket_create_skipped") is True
    assert meta.get("ticket_create_skip_reason") == "phase_service_ticketing_disabled"
    assert meta.get("phase_gate_current_phase_id") == "pre_checkin"
    assert meta.get("phase_gate_service_phase_id") == "pre_checkin"


@pytest.mark.asyncio
async def test_process_message_declines_out_of_phase_service_request_non_full_kb(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-nonfullkb-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _fake_classify(*_args, **_kwargs):
        return IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.9, entities={})

    async def _should_not_dispatch(*_args, **_kwargs):
        raise AssertionError("Handler dispatch should not run when phase gate declines request")

    async def _no_summary(_context):
        return None

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
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
                "description": "Handle spa bookings",
                "phase_id": "during_stay",
                "is_active": True,
            }
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
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(service, "_dispatch_to_handler", _should_not_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="phase-nonfullkb-1",
            message="book spa at 7 pm",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.IDLE
    assert "not available for Pre Checkin phase" in response.message
    assert response.metadata.get("ticketing_phase_gate") is True


@pytest.mark.asyncio
async def test_process_message_declines_unconfigured_service_request_for_current_phase_non_full_kb(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-nonfullkb-2",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _fake_classify(*_args, **_kwargs):
        return IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.9, entities={})

    async def _should_not_dispatch(*_args, **_kwargs):
        raise AssertionError("Handler dispatch should not run when phase gate declines request")

    async def _no_summary(_context):
        return None

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [{"id": "pre_booking", "name": "Pre Booking"}],
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(service, "_dispatch_to_handler", _should_not_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="phase-nonfullkb-2",
            message="book a table at kadak",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.IDLE
    assert "not available for Pre Booking phase" in response.message
    assert response.metadata.get("ticketing_phase_gate") is True


@pytest.mark.asyncio
async def test_process_message_declines_generic_transaction_intent_outside_phase_non_full_kb(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-nonfullkb-3",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _fake_classify(*_args, **_kwargs):
        return IntentResult(intent=IntentType.ORDER_FOOD, confidence=0.9, entities={})

    async def _should_not_dispatch(*_args, **_kwargs):
        raise AssertionError("Handler dispatch should not run when phase gate declines request")

    async def _no_summary(_context):
        return None

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "hotel_enquiry_sales",
                "name": "Hotel Enquiry & Sales",
                "type": "service",
                "description": "Answer hotel questions and guide guests toward booking decisions.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Support in-room dining and meal orders.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(service, "_dispatch_to_handler", _should_not_dispatch)

    response = await service.process_message(
        ChatRequest(
            session_id="phase-nonfullkb-3",
            message="im hungry",
            hotel_code="DEFAULT",
        )
    )

    assert response.state == ConversationState.IDLE
    assert "not available for Pre Booking phase" in response.message
    assert response.metadata.get("ticketing_phase_gate") is True


def test_contextual_suggested_actions_filter_services_by_phase():
    service = ChatService()
    actions = service._build_contextual_suggested_actions(
        ConversationState.IDLE,
        IntentType.FAQ,
        None,
        {
            "service_catalog": [
                {
                    "id": "hotel_enquiry_sales",
                    "name": "Hotel Enquiry & Sales",
                    "phase_id": "pre_booking",
                    "is_active": True,
                },
                {
                    "id": "room_discovery",
                    "name": "Room Discovery",
                    "phase_id": "pre_booking",
                    "is_active": True,
                },
                {
                    "id": "spa_recreation_booking",
                    "name": "Spa & Recreation Booking",
                    "phase_id": "during_stay",
                    "is_active": True,
                },
            ]
        },
        {"_integration": {"phase": "pre_booking"}},
    )

    lowered = [item.lower() for item in actions]
    assert "hotel enquiry & sales" in lowered
    assert "room discovery" in lowered
    assert "spa & recreation booking" not in lowered


def test_finalize_user_query_suggestions_drops_out_of_phase_transaction_chips():
    service = ChatService()
    finalized = service._finalize_user_query_suggestions(
        suggestions=["Book a spa treatment", "Ask about spa timings", "Ask another question"],
        state=ConversationState.IDLE,
        intent=IntentType.FAQ,
        pending_action=None,
        pending_data={"_integration": {"phase": "pre_booking"}},
        capabilities_summary={
            "service_catalog": [
                {
                    "id": "hotel_enquiry_sales",
                    "name": "Hotel Enquiry & Sales",
                    "phase_id": "pre_booking",
                    "is_active": True,
                },
                {
                    "id": "room_discovery",
                    "name": "Room Discovery",
                    "phase_id": "pre_booking",
                    "is_active": True,
                },
                {
                    "id": "spa_recreation_booking",
                    "name": "Spa & Recreation Booking",
                    "phase_id": "during_stay",
                    "is_active": True,
                },
            ]
        },
    )

    lowered = [item.lower() for item in finalized]
    assert "book a spa treatment" not in lowered
    assert "ask another question" in lowered


def test_service_catalog_shortcut_handles_inactive_service_action():
    service = ChatService()
    context = ConversationContext(session_id="p4", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "Book Kadak for me now",
        context,
        {
            "service_catalog": [
                {
                    "id": "kadak",
                    "name": "Kadak",
                    "type": "restaurant",
                    "description": "Indian snacks and chai",
                    "hours": {"open": "09:00", "close": "23:00"},
                    "is_active": False,
                },
                {
                    "id": "aviator",
                    "name": "Aviator Lounge",
                    "type": "restaurant",
                    "description": "Bar and lounge",
                    "hours": {"open": "10:00", "close": "22:00"},
                    "is_active": True,
                },
            ]
        },
    )

    assert matched is not None
    assert matched["match_type"] == "inactive_service"
    assert "unavailable" in matched["response_text"].lower()
    assert "aviator lounge" in matched["response_text"].lower()


def test_service_catalog_shortcut_skips_personal_reservation_queries():
    service = ChatService()
    context = ConversationContext(session_id="p6", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "What time is my Kadak reservation?",
        context,
        {
            "service_catalog": [
                {
                    "id": "kadak",
                    "name": "Kadak",
                    "type": "restaurant",
                    "description": "Indian snacks and chai",
                    "hours": {"open": "09:00", "close": "23:00"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is None


def test_service_catalog_shortcut_does_not_override_restaurant_booking_actions():
    service = ChatService()
    context = ConversationContext(session_id="p6b", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "Book table at Kadak for 2",
        context,
        {
            "service_catalog": [
                {
                    "id": "kadak",
                    "name": "Kadak",
                    "type": "restaurant",
                    "description": "Indian snacks and chai",
                    "hours": {"open": "09:00", "close": "23:00"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is None


def test_service_catalog_shortcut_matches_hotel_booking_via_description_keywords():
    service = ChatService()
    context = ConversationContext(session_id="p6c", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "i wanna book a room in hotel for 3 nights",
        context,
        {
            "service_catalog": [
                {
                    "id": "hotel_booking",
                    "name": "Hotel Booking",
                    "type": "front_desk",
                    "description": "Room booking assistance and stay-date support",
                    "hours": {"open": "00:00", "close": "23:59"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is not None
    assert matched["service_id"] == "hotel_booking"
    assert matched["match_type"] in {"service_catalog_action", "service_catalog_info"}


def test_service_catalog_shortcut_ignores_generic_service_id_alias():
    service = ChatService()
    context = ConversationContext(session_id="p6d", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "restaurant menu",
        context,
        {
            "service_catalog": [
                {
                    "id": "restaurant",
                    "name": "Aviator",
                    "type": "restaurant",
                    "description": "",
                    "hours": {"open": "00:00", "close": "23:59"},
                    "is_active": False,
                }
            ]
        },
    )

    assert matched is None


@pytest.mark.asyncio
async def test_classify_intent_accepts_typo_yes_in_awaiting_confirmation():
    service = ChatService()
    result = await service._classify_intent(
        "ys",
        [],
        {"state": "awaiting_confirmation"},
    )

    assert result.intent == IntentType.CONFIRMATION_YES
    assert result.confidence >= 0.9


@pytest.mark.asyncio
async def test_classify_intent_accepts_single_char_s_for_show_menu_confirmation():
    service = ChatService()
    result = await service._classify_intent(
        "s",
        [],
        {"state": "awaiting_confirmation", "pending_action": "show_menu"},
    )

    assert result.intent == IntentType.CONFIRMATION_YES
    assert result.confidence >= 0.85


@pytest.mark.asyncio
async def test_classify_intent_prefers_menu_for_restaurant_alias_phrase():
    service = ChatService()
    result = await service._classify_intent(
        "in room dining",
        [],
        {
            "state": "idle",
            "pending_action": None,
            "service_catalog": [
                {
                    "id": "ird",
                    "name": "In-Room Dining",
                    "type": "restaurant",
                    "description": "Multi-cuisine",
                    "is_active": True,
                }
            ],
        },
    )

    assert result.intent == IntentType.MENU_REQUEST
    assert result.entities.get("restaurant") == "In-Room Dining"


def test_service_catalog_shortcut_skips_restaurant_menu_display_queries():
    service = ChatService()
    context = ConversationContext(session_id="p6e", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "in room dining menu",
        context,
        {
            "service_catalog": [
                {
                    "id": "ird",
                    "name": "In-Room Dining",
                    "type": "restaurant",
                    "description": "Multi-cuisine",
                    "hours": {"open": "00:00", "close": "23:59"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is None


def test_service_catalog_shortcut_returns_handoff_for_unconfigured_transport_request():
    service = ChatService()
    context = ConversationContext(session_id="p6f", hotel_code="DEFAULT")

    matched = service._match_service_information_response(
        "Please arrange airport transfer at 7 PM",
        context,
        {
            "service_catalog": [
                {
                    "id": "spa_booking",
                    "name": "Spa Booking",
                    "type": "service",
                    "description": "Spa treatments and massage",
                    "hours": {"open": "09:00", "close": "23:00"},
                    "is_active": True,
                }
            ]
        },
    )

    assert matched is not None
    assert matched["match_type"] == "service_catalog_unavailable_handoff"
    assert "not currently configured" in matched["response_text"].lower()
    assert "staff" in matched["response_text"].lower()


def test_order_capability_check_ignores_stale_restaurant_entity_without_explicit_mention(monkeypatch):
    service = ChatService()
    intent_result = IntentResult(
        intent=IntentType.ORDER_FOOD,
        confidence=0.9,
        entities={"restaurant": "Kadak"},
    )

    monkeypatch.setattr("services.chat_service.config_service.is_capability_enabled", lambda _cap: True)
    monkeypatch.setattr("services.chat_service.config_service.can_deliver_to_room", lambda _svc: False)

    check = service._check_capability_for_intent(
        "DEFAULT",
        intent_result,
        "deliver burger to my room now",
    )

    assert check.allowed is True


def test_order_capability_check_blocks_explicit_non_delivery_restaurant(monkeypatch):
    service = ChatService()
    intent_result = IntentResult(
        intent=IntentType.ORDER_FOOD,
        confidence=0.9,
        entities={"restaurant": "Kadak"},
    )

    monkeypatch.setattr("services.chat_service.config_service.is_capability_enabled", lambda _cap: True)
    monkeypatch.setattr("services.chat_service.config_service.can_deliver_to_room", lambda _svc: False)

    check = service._check_capability_for_intent(
        "DEFAULT",
        intent_result,
        "deliver burger from kadak to my room now",
    )

    assert check.allowed is False
    assert "dine-in only" in check.reason.lower()


def test_table_booking_capability_check_allows_room_stay_booking_language(monkeypatch):
    service = ChatService()
    intent_result = IntentResult(
        intent=IntentType.TABLE_BOOKING,
        confidence=0.85,
        entities={},
    )

    monkeypatch.setattr("services.chat_service.config_service.is_capability_enabled", lambda _cap: True)

    check = service._check_capability_for_intent(
        "DEFAULT",
        intent_result,
        "I want a room from feb 20 to feb 23",
    )

    assert check.allowed is True
    assert "available" in check.reason.lower()


def test_table_booking_capability_check_allows_room_stay_lookup_language(monkeypatch):
    service = ChatService()
    intent_result = IntentResult(
        intent=IntentType.TABLE_BOOKING,
        confidence=0.85,
        entities={},
    )

    monkeypatch.setattr("services.chat_service.config_service.is_capability_enabled", lambda _cap: True)

    check = service._check_capability_for_intent(
        "DEFAULT",
        intent_result,
        "im looking for a room for 2",
    )

    assert check.allowed is True
    assert "available" in check.reason.lower()


def test_service_overview_shortcut_skips_specific_medical_service_query():
    service = ChatService()
    context = ConversationContext(session_id="svc-overview-medical", hotel_code="DEFAULT")

    matched = service._match_service_overview_response(
        "do you provide medical services",
        {
            "services": {
                "food_ordering": True,
                "room_service": True,
                "table_booking": True,
            }
        },
        context,
    )

    assert matched is None


def test_response_validator_does_not_flag_generic_inactive_service_id_in_booking_confirmation():
    context = ConversationContext(session_id="p5", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "Yes, confirm")

    validation = response_validator.validate(
        response_text=(
            "Your table has been booked successfully!\n\n"
            "Booking Reference: BK-12345678\n"
            "Restaurant: Kadak\n"
            "Time: tonight"
        ),
        intent_result=IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [],
            "service_catalog": [
                {"id": "restaurant", "name": "", "is_active": False},
            ],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is True


def test_response_validator_does_not_require_room_for_table_booking_do_rule():
    context = ConversationContext(session_id="p8", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "Reserve table for 2 at 8pm")

    validation = response_validator.validate(
        response_text="I'd like to book a table at Kadak for 2 guests at 8pm. Shall I confirm this reservation?",
        intent_result=IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.92, entities={}),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": ["Confirm timing, location, and count for service requests."], "donts": []},
            "restaurants": [],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is True


def test_response_validator_does_not_overwrite_order_summary_with_generic_do_prompt():
    context = ConversationContext(session_id="p8b", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "send over pasta to my room")

    validation = response_validator.validate(
        response_text=(
            "Here's your order summary:\n\n"
            "1. Chicken Alfredo Pasta - Rs.480\n\n"
            "Total: Rs.480\n\n"
            "Shall I confirm this order? (Yes/No)"
        ),
        intent_result=IntentResult(intent=IntentType.ORDER_FOOD, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": ["Confirm timing, location, and count for service requests."], "donts": []},
            "restaurants": [],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is True


def test_response_validator_scopes_delivery_contradiction_to_same_alias_context():
    context = ConversationContext(session_id="p8c", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "show menu")

    validation = response_validator.validate(
        response_text=(
            "**Kadak** - Dine-in only\n\n"
            "**In-Room Dining** - Delivers to room"
        ),
        intent_result=IntentResult(intent=IntentType.MENU_REQUEST, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={
            "services": {},
            "nlu_policy": {"dos": [], "donts": []},
            "restaurants": [
                {"name": "Kadak", "dine_in_only": True},
                {"name": "In-Room Dining", "dine_in_only": False},
            ],
            "service_catalog": [],
        },
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is True


def test_memory_shortcut_returns_latest_booking_details():
    service = ChatService()
    context = ConversationContext(session_id="p7", hotel_code="DEFAULT")

    matched = service._match_memory_information_response(
        "what time is my reservation?",
        context,
        {
            "facts": {
                "latest_booking": {
                    "reference": "BK-1",
                    "restaurant": "Kadak",
                    "time": "10 PM",
                    "date": "tonight",
                }
            }
        },
    )

    assert matched is not None
    assert matched["match_type"] == "memory_booking_lookup"
    assert "10 PM" in matched["response_text"]


def test_return_to_bot_request_detection():
    assert ChatService._is_return_to_bot_request("Return to bot") is True
    assert ChatService._is_return_to_bot_request("back to bot") is True
    assert ChatService._is_return_to_bot_request("hello") is False


def test_menu_capability_check_allows_rag_mode_when_menu_runtime_disabled(monkeypatch):
    service = ChatService()
    intent_result = IntentResult(intent=IntentType.MENU_REQUEST, confidence=0.9, entities={})

    monkeypatch.setattr("services.chat_service.config_service.is_menu_runtime_enabled", lambda: False)

    check = service._check_capability_for_intent(
        "DEFAULT",
        intent_result,
        "show menu",
    )

    assert check.allowed is True
    assert "knowledge-base" in check.reason.lower()


@pytest.mark.asyncio
async def test_augment_capabilities_skips_db_orphans_when_menu_runtime_disabled(async_session, monkeypatch):
    from models.database import Hotel, Restaurant

    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()
    async_session.add(
        Restaurant(
            hotel_id=hotel.id,
            code="legacy_only",
            name="Legacy Outlet",
            is_active=True,
        )
    )
    await async_session.commit()

    service = ChatService()
    monkeypatch.setattr("services.chat_service.config_service.is_menu_runtime_enabled", lambda: False)

    merged = await service._augment_capabilities_from_db(
        {
            "hotel_name": "Demo",
            "service_catalog": [],
            "restaurants": [],
        },
        async_session,
    )

    assert merged["service_catalog"] == []
    assert merged["restaurants"] == []


@pytest.mark.asyncio
async def test_augment_capabilities_keeps_db_orphans_when_menu_runtime_enabled(async_session, monkeypatch):
    from models.database import Hotel, Restaurant

    hotel = Hotel(code="DEFAULT", name="Demo2", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()
    async_session.add(
        Restaurant(
            hotel_id=hotel.id,
            code="legacy_visible",
            name="Legacy Visible",
            is_active=True,
        )
    )
    await async_session.commit()

    service = ChatService()
    monkeypatch.setattr("services.chat_service.config_service.is_menu_runtime_enabled", lambda: True)

    merged = await service._augment_capabilities_from_db(
        {
            "hotel_name": "Demo2",
            "service_catalog": [],
            "restaurants": [],
        },
        async_session,
    )

    service_ids = {row.get("id") for row in merged.get("service_catalog", [])}
    assert "legacy_visible" in service_ids


@pytest.mark.asyncio
async def test_run_multi_ask_subquery_agent_applies_phase_gate_before_kb(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="multi-ask-phase-gate-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    async def _fake_classify(*_args, **_kwargs):
        return IntentResult(intent=IntentType.TABLE_BOOKING, confidence=0.9, entities={})

    async def _should_not_run_kb(*_args, **_kwargs):
        raise AssertionError("KB agent should not run when phase gate blocks sub-query")

    async def _fake_ticketing_status(**_kwargs):
        return {
            "ticketing_status_checked": True,
            "ticketing_required": True,
            "ticketing_create_allowed": False,
            "ticketing_skip_reason": "phase_service_mismatch",
        }

    monkeypatch.setattr(service, "_classify_intent", _fake_classify)
    monkeypatch.setattr(service, "_run_multi_ask_kb_answer", _should_not_run_kb)
    monkeypatch.setattr(service, "_evaluate_ticketing_status_for_turn", _fake_ticketing_status)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa bookings",
                "phase_id": "during_stay",
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_checkin", "name": "Pre Checkin"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    sub_result = await service._run_multi_ask_subquery_agent(
        query="book spa for tomorrow evening",
        context=context,
        capabilities_summary={"service_catalog": [], "intents": []},
        llm_context={"state": "idle", "pending_action": None, "pending_data": {}},
        history_window=6,
        db_session=None,
    )

    assert sub_result["response_source"] == "multi_ask_phase_gate"
    assert "not available for Pre Checkin phase" in sub_result["response_text"]
    assert sub_result["ticketing"]["ticketing_skip_reason"] == "phase_service_mismatch"


@pytest.mark.asyncio
async def test_process_message_multi_ask_orchestrator_combines_subanswers(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="multi-ask-process-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_ticketing_status(**_kwargs):
        return {
            "ticketing_status_checked": True,
            "ticketing_required": False,
            "ticketing_create_allowed": False,
            "ticketing_skip_reason": "",
        }

    async def _fake_subquery_agent(**kwargs):
        query = str(kwargs.get("query") or "")
        if "food" in query.lower():
            return {
                "query": query,
                "response_text": "Food ordering is not available for Pre Booking phase.",
                "response_source": "multi_ask_phase_gate",
                "intent": IntentType.ORDER_FOOD.value,
                "confidence": 0.9,
                "entities": {},
                "ticketing": {
                    "ticketing_status_checked": True,
                    "ticketing_required": False,
                    "ticketing_create_allowed": False,
                    "ticketing_skip_reason": "phase_service_mismatch",
                },
            }
        return {
            "query": query,
            "response_text": "Spa is available in During Stay phase.",
            "response_source": "multi_ask_phase_gate",
            "intent": IntentType.TABLE_BOOKING.value,
            "confidence": 0.9,
            "entities": {},
            "ticketing": {
                "ticketing_status_checked": True,
                "ticketing_required": True,
                "ticketing_create_allowed": False,
                "ticketing_skip_reason": "phase_service_ticketing_disabled",
            },
        }

    async def _fake_compose(**_kwargs):
        return "Food ordering is not available for Pre Booking phase. Spa is available in During Stay phase."

    async def _fake_decompose(_msg, **_kwargs):
        return [
            "i need food",
            "book spa appointment",
        ]

    monkeypatch.setattr("services.chat_service.settings.chat_full_kb_llm_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_kb_only_mode", False)
    monkeypatch.setattr("services.chat_service.settings.chat_multi_ask_orchestration_enabled", True)
    monkeypatch.setattr("services.chat_service.config_service.resolve_hotel_code", lambda _requested: "DEFAULT")
    monkeypatch.setattr(
        "services.chat_service.config_service.get_capability_summary",
        lambda _hotel_code=None: {"service_catalog": [], "restaurants": [], "services": {}, "intents": []},
    )
    monkeypatch.setattr("services.chat_service.context_manager.get_or_create_context", _fake_get_or_create_context)
    monkeypatch.setattr("services.chat_service.context_manager.save_context", _fake_save_context)
    monkeypatch.setattr("services.chat_service.conversation_memory_service.maybe_refresh_summary", _no_summary)
    monkeypatch.setattr(service, "_evaluate_ticketing_status_for_turn", _fake_ticketing_status)
    monkeypatch.setattr(service, "_decompose_multi_ask_queries", _fake_decompose)
    monkeypatch.setattr(service, "_run_multi_ask_subquery_agent", _fake_subquery_agent)
    monkeypatch.setattr(service, "_compose_multi_ask_response_with_llm", _fake_compose)

    response = await service.process_message(
        ChatRequest(
            session_id="multi-ask-process-1",
            message="i need food and book spa appointment",
            hotel_code="DEFAULT",
        )
    )

    assert response.metadata.get("multi_ask_orchestrated") is True
    assert response.metadata.get("response_source") == "multi_ask_orchestrator"
    assert response.metadata.get("ticketing_required") is True
    assert response.metadata.get("ticketing_create_allowed") is False
    assert len(response.metadata.get("ticketing_statuses", [])) == 2
    assert "Pre Booking phase" in response.message or "pre booking phase" in response.message.lower()


@pytest.mark.asyncio
async def test_ticketing_status_marks_service_toggle_disabled(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="ticketing-status-toggle-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_checkin"}},
    )

    async def _fake_decide_async(**_kwargs):
        return SimpleNamespace(
            activate=True,
            route="complaint_handler",
            reason="configured_ticketing_case_match",
            source="service_ticketing_cases",
            matched_case="Pre-checkin booking modification needs staff action.",
        )

    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _fake_decide_async)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "booking_modification",
                "name": "Booking Modification",
                "type": "service",
                "description": "Modify confirmed bookings",
                "phase_id": "pre_checkin",
                "ticketing_enabled": False,
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [{"id": "pre_checkin", "name": "Pre Checkin"}],
    )

    status = await service._evaluate_ticketing_status_for_turn(
        message="please modify my booking dates",
        context=context,
        intent=IntentType.TABLE_BOOKING,
        response_text="Sure, I can help.",
        current_pending_action=None,
        pending_action_target=None,
        capabilities_summary={},
    )

    assert status.get("ticketing_status_checked") is True
    assert status.get("ticketing_required") is True
    assert status.get("ticketing_create_allowed") is False
    assert status.get("ticketing_skip_reason") == "phase_service_ticketing_disabled"


@pytest.mark.asyncio
async def test_ticketing_status_passes_selected_phase_to_ticketing_agent(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="ticketing-status-phase-context-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    captured: dict[str, str] = {}

    async def _fake_decide_async(**kwargs):
        captured["selected_phase_id"] = str(kwargs.get("selected_phase_id") or "")
        captured["selected_phase_name"] = str(kwargs.get("selected_phase_name") or "")
        return SimpleNamespace(
            activate=False,
            route="none",
            reason="not_required",
            source="test",
            matched_case="",
        )

    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _fake_decide_async)

    status = await service._evaluate_ticketing_status_for_turn(
        message="hello",
        context=context,
        intent=IntentType.FAQ,
        response_text="Hi there.",
        current_pending_action=None,
        pending_action_target=None,
        capabilities_summary={},
    )

    assert status.get("ticketing_required") is False
    assert captured.get("selected_phase_id") == "pre_booking"
    assert captured.get("selected_phase_name") == "Pre Booking"


def test_phase_unavailable_detects_mixed_request_with_allowed_and_disallowed_services(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-mixed-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "sightseeing_around_hotel",
                "name": "sightseeing around hotel",
                "type": "service",
                "description": "Nearby places and attraction guidance",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Food ordering and dining support",
                "phase_id": "during_stay",
                "is_active": True,
            },
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message=(
            "do you have a pool, how can i book a room, "
            "i need food and also book spa for me"
        ),
        intent=IntentType.FAQ,
        context=context,
        pending_data={},
        entities={},
    )

    assert unavailable is not None
    assert unavailable.get("phase_service_unavailable") is True
    response_text = str(unavailable.get("response_text") or "").lower()
    assert "not available for pre booking phase" in response_text
    assert ("dining" in response_text) or ("food" in response_text)
    assert ("spa" in response_text) or ("recreation" in response_text)


def test_phase_service_unavailable_blocks_room_issue_complaint_out_of_phase(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-complaint-room-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "housekeeping_support",
                "name": "Housekeeping Support",
                "type": "service",
                "description": "Room service, housekeeping, and maintenance support during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="there is a cockroach in my room",
        intent=IntentType.COMPLAINT,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is not None
    assert unavailable.get("phase_service_unavailable") is True
    response_text = str(unavailable.get("response_text") or "").lower()
    assert "room service is not available for pre booking phase" in response_text


def test_phase_service_unavailable_does_not_treat_room_stay_request_as_room_service(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-room-stay-request-1",
        hotel_code="DEFAULT",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "housekeeping_support",
                "name": "Housekeeping Support",
                "type": "service",
                "description": "Room service, housekeeping, and maintenance support during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="i need a room but i'm on a budget",
        intent=IntentType.HUMAN_REQUEST,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is None


@pytest.mark.asyncio
async def test_ticketing_status_skips_room_issue_complaint_out_of_phase(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-complaint-room-2",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    async def _fake_decide_async(**_kwargs):
        return SimpleNamespace(
            activate=True,
            route="complaint_handler",
            reason="configured_ticketing_case_match",
            source="service_ticketing_cases",
            matched_case="Room complaint requires staff action.",
        )

    monkeypatch.setattr("services.chat_service.settings.ticketing_plugin_enabled", True)
    monkeypatch.setattr("services.chat_service.ticketing_service.is_ticketing_enabled", lambda _caps=None: True)
    monkeypatch.setattr("services.chat_service.ticketing_agent_service.decide_async", _fake_decide_async)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "housekeeping_support",
                "name": "Housekeeping Support",
                "type": "service",
                "description": "Room service, housekeeping, and maintenance support during stay.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    status = await service._evaluate_ticketing_status_for_turn(
        message="there is a cockroach in my room",
        context=context,
        intent=IntentType.COMPLAINT,
        response_text="I understand and will help.",
        current_pending_action=None,
        pending_action_target=None,
        capabilities_summary={},
    )

    assert status.get("ticketing_required") is True
    assert status.get("ticketing_create_allowed") is False
    assert status.get("ticketing_skip_reason") == "phase_service_unavailable"
    assert status.get("phase_gate_current_phase_id") == "pre_booking"


@pytest.mark.asyncio
async def test_full_kb_prebooking_mixed_query_blocks_food_and_spa(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="fullkb-prebooking-mixed-1",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    async def _fake_get_or_create_context(session_id, hotel_code, guest_phone, channel, db_session):
        return context

    async def _fake_save_context(_context, db_session=None):
        return None

    async def _no_summary(_context):
        return None

    async def _fake_refresh_summary(_context):
        return None

    async def _fake_run_turn(*_args, **_kwargs):
        return SimpleNamespace(
            response_text=(
                "We have food service, spa booking, room booking, pool, and sightseeing options."
            ),
            normalized_query=(
                "hi who are you i need food and do u have a pool how to book a room and book spa and sightseeing nearby"
            ),
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.9,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Need help"],
            trace_id="fullkb-prebooking-mixed-1",
            llm_output={},
            clear_pending_data=False,
            status="success",
        )

    async def _fake_ticketing_status(**_kwargs):
        return {
            "ticketing_status_checked": True,
            "ticketing_required": False,
            "ticketing_create_allowed": False,
            "ticketing_skip_reason": "",
        }

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
    monkeypatch.setattr(service, "_evaluate_ticketing_status_for_turn", _fake_ticketing_status)
    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "sightseeing_around_hotel",
                "name": "sightseeing around hotel",
                "type": "service",
                "description": "Nearby attraction guidance",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Food ordering and dining support",
                "phase_id": "during_stay",
                "is_active": True,
            },
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    response = await service.process_message(
        ChatRequest(
            session_id="fullkb-prebooking-mixed-1",
            message=(
                "hi who are you i need food and do u have a pool "
                "how to book a room and book spa and sightseeing nearby"
            ),
            hotel_code="DEFAULT",
            metadata={"phase": "pre_booking"},
        )
    )

    assert response.metadata.get("response_source") == "full_kb_ticketing_phase_gate"
    text = str(response.message or "").lower()
    assert "not available for pre booking phase" in text
    assert ("dining" in text) or ("food" in text)
    assert ("spa" in text) or ("recreation" in text)


def test_phase_service_unavailable_skips_room_booking_detail_collection_pending_action(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-room-followup-1",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_room_booking_details",
        pending_data={"_integration": {"phase": "pre_booking"}},
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="2 guests from march 10 to march 12",
        intent=IntentType.TABLE_BOOKING,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is None


def test_phase_service_unavailable_allows_room_detail_followup_without_pending_action(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-room-followup-2",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_action=None,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    context.add_message(MessageRole.USER, "book a room")
    context.add_message(
        MessageRole.ASSISTANT,
        "Please share check-in, check-out, and guest count.",
    )
    context.add_message(MessageRole.USER, "i want the cheapest room available")

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "spa_recreation_booking",
                "name": "Spa & Recreation Booking",
                "type": "service",
                "description": "Handle spa and recreation bookings.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="march 10-12 for 2 guests",
        intent=IntentType.TABLE_BOOKING,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is None


def test_phase_service_unavailable_allows_room_detail_followup_with_typo_history(monkeypatch):
    service = ChatService()
    context = ConversationContext(
        session_id="phase-room-followup-3",
        hotel_code="DEFAULT",
        state=ConversationState.IDLE,
        pending_action=None,
        pending_data={"_integration": {"phase": "pre_booking"}},
    )
    context.add_message(MessageRole.USER, "hi i want a rom")
    context.add_message(
        MessageRole.ASSISTANT,
        "Certainly! Could you please provide the check-in and check-out dates, as well as the number of guests for your stay?",
    )

    monkeypatch.setattr(
        "services.chat_service.config_service.get_services",
        lambda: [
            {
                "id": "room_discovery",
                "name": "Room Discovery",
                "type": "service",
                "description": "Help guests compare room types and amenities.",
                "phase_id": "pre_booking",
                "is_active": True,
            },
            {
                "id": "in_room_dining_support",
                "name": "In-Room Dining Support",
                "type": "service",
                "description": "Food ordering and dining support.",
                "phase_id": "during_stay",
                "is_active": True,
            },
        ],
    )
    monkeypatch.setattr(
        "services.chat_service.config_service.get_journey_phases",
        lambda: [
            {"id": "pre_booking", "name": "Pre Booking"},
            {"id": "during_stay", "name": "During Stay"},
        ],
    )

    unavailable = service._detect_phase_service_unavailable_for_intent(
        message="21st march foe 2",
        intent=IntentType.TABLE_BOOKING,
        context=context,
        pending_data=context.pending_data,
        entities={},
    )

    assert unavailable is None


@pytest.mark.asyncio
async def test_decompose_multi_ask_uses_fallback_when_llm_returns_single(monkeypatch):
    service = ChatService()
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")

    async def _fake_chat_with_json(*_args, **_kwargs):
        return {"is_multi_ask": False, "sub_requests": []}

    monkeypatch.setattr("services.chat_service.llm_client.chat_with_json", _fake_chat_with_json)

    queries = await service._decompose_multi_ask_queries(
        "hi who are you and i need food and do u have a pool how to book a room"
    )

    assert len(queries) >= 2


@pytest.mark.asyncio
async def test_decompose_multi_ask_prompt_includes_selected_phase(monkeypatch):
    service = ChatService()
    captured: dict[str, str] = {}
    monkeypatch.setattr("services.chat_service.settings.openai_api_key", "test-key")

    async def _fake_chat_with_json(messages, model=None, temperature=0.0):
        _ = model, temperature
        captured["system_prompt"] = str(messages[0].get("content") or "")
        return {"is_multi_ask": False, "sub_requests": []}

    monkeypatch.setattr("services.chat_service.llm_client.chat_with_json", _fake_chat_with_json)

    _ = await service._decompose_multi_ask_queries(
        "i need food and do you have a pool",
        selected_phase_id="pre_booking",
        selected_phase_name="Pre Booking",
    )

    assert "Selected user journey phase: Pre Booking (pre_booking)." in str(
        captured.get("system_prompt") or ""
    )


def test_dedupe_response_sentences_removes_repeated_unavailable_phrase():
    text = ChatService._dedupe_response_sentences(
        "Food ordering is not available for Pre Booking phase. "
        "Right now, I can help you with Hotel Enquiry & Sales, Room Discovery. "
        "Food ordering is not available for Pre Booking phase."
    )

    lowered = text.lower()
    assert lowered.count("food ordering is not available for pre booking phase") == 1
