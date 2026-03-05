import pytest

from handlers.health_support_handler import HealthSupportHandler
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType, MessageRole
from services.chat_service import ChatService
from services.response_validator import response_validator


@pytest.mark.asyncio
async def test_classify_intent_routes_health_support_without_llm(monkeypatch):
    service = ChatService()

    async def _should_not_run(*args, **kwargs):
        raise AssertionError("LLM classification should not run for deterministic health-support requests")

    monkeypatch.setattr("services.chat_service.llm_client.classify_intent", _should_not_run)

    result = await service._classify_intent(
        "I need medicine for severe headache",
        [],
        {"state": "idle", "pending_action": None},
    )

    assert result.intent == IntentType.HEALTH_SUPPORT
    assert result.entities.get("requested_topic") == "health_support"


@pytest.mark.asyncio
async def test_health_support_handler_emergency_escalates(monkeypatch):
    handler = HealthSupportHandler()
    context = ConversationContext(session_id="health-1", hotel_code="DEFAULT")

    monkeypatch.setattr("handlers.health_support_handler.config_service.is_capability_enabled", lambda _cap: True)

    result = await handler.handle(
        "I have chest pain and can't breathe",
        IntentResult(intent=IntentType.HEALTH_SUPPORT, confidence=0.95, entities={}),
        context,
        capabilities={},
    )

    assert result.next_state == ConversationState.ESCALATED
    assert result.metadata.get("health_support_severity") == "emergency"
    assert result.metadata.get("escalated") is True


@pytest.mark.asyncio
async def test_health_support_handler_confirmation_yes_escalates(monkeypatch):
    handler = HealthSupportHandler()
    context = ConversationContext(
        session_id="health-2",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_health_support",
        pending_data={"health_request": "Need medicine"},
    )

    monkeypatch.setattr("handlers.health_support_handler.config_service.is_capability_enabled", lambda _cap: True)

    result = await handler.handle(
        "yes",
        IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context,
        capabilities={},
    )

    assert result.next_state == ConversationState.ESCALATED
    assert result.pending_action is None
    assert result.metadata.get("escalated") is True


@pytest.mark.asyncio
async def test_health_support_handler_confirmation_no_returns_idle():
    handler = HealthSupportHandler()
    context = ConversationContext(
        session_id="health-3",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_health_support",
        pending_data={"health_request": "Need medicine"},
    )

    result = await handler.handle(
        "no thanks",
        IntentResult(intent=IntentType.CONFIRMATION_NO, confidence=0.95, entities={}),
        context,
        capabilities={},
    )

    assert result.next_state == ConversationState.IDLE
    assert result.pending_action is None
    assert result.metadata.get("escalated") is False


def test_response_validator_blocks_medication_dosage_advice():
    context = ConversationContext(session_id="health-4", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "I need medicine for fever and headache")

    validation = response_validator.validate(
        response_text="You should take 500 mg paracetamol twice daily.",
        intent_result=IntentResult(intent=IntentType.HEALTH_SUPPORT, confidence=0.9, entities={}),
        context=context,
        capabilities_summary={"services": {}, "nlu_policy": {"dos": [], "donts": []}, "restaurants": [], "service_catalog": []},
        capability_check_allowed=True,
        capability_reason="",
    )

    assert validation.valid is False
    assert validation.action == "replace"
    assert any(issue.code == "medical_advice_guardrail" for issue in validation.issues)
