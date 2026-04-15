import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from schemas.chat import ConversationContext, IntentResult, IntentType, MessageRole
from schemas.orchestration import OrchestrationDecision
from services.orchestration_policy_service import OrchestrationPolicyService
from services.response_validator import response_validator
from services.ticketing_service import ticketing_service


@pytest.fixture
def context() -> ConversationContext:
    ctx = ConversationContext(session_id="sess-1", hotel_code="test")
    ctx.add_message(MessageRole.USER, "Can I extend my stay until the 17th?")
    return ctx


@pytest.fixture
def capabilities_summary() -> dict:
    return {
        "journey_phases": [
            {"id": "during_stay", "name": "During Stay"},
            {"id": "pre_booking", "name": "Pre Booking"},
        ],
        "service_catalog": [],
    }


def test_policy_blocks_unknown_service_ticket_by_default(monkeypatch, context, capabilities_summary):
    monkeypatch.setattr(settings, "ticketing_plugin_enabled", True, raising=False)
    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda *_args, **_kwargs: True)

    decision = OrchestrationDecision(
        action="create_ticket",
        ticket={
            "required": True,
            "ready_to_create": True,
            "issue": "General request",
        },
    )

    result = OrchestrationPolicyService().evaluate(
        decision=decision,
        context=context,
        capabilities_summary=capabilities_summary,
        selected_phase_context={"selected_phase_id": "during_stay", "selected_phase_name": "During Stay"},
    )

    assert result.action_allowed is False
    assert result.blocked_reason == "unknown_target_service"


def test_policy_allows_generic_kb_ticket_with_evidence(monkeypatch, context, capabilities_summary):
    monkeypatch.setattr(settings, "ticketing_plugin_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chat_llm_generic_kb_ticketing_enabled", True, raising=False)
    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda *_args, **_kwargs: True)

    decision = OrchestrationDecision(
        action="create_ticket",
        ticket={
            "required": True,
            "ready_to_create": True,
            "generic_request": True,
            "phase_applicable": True,
            "issue": "Stay extension request until April 17",
            "evidence": [
                "During-stay late checkout or stay extension requires front office approval."
            ],
        },
    )

    result = OrchestrationPolicyService().evaluate(
        decision=decision,
        context=context,
        capabilities_summary=capabilities_summary,
        selected_phase_context={"selected_phase_id": "during_stay", "selected_phase_name": "During Stay"},
    )

    assert result.action_allowed is True
    assert result.ticket_create_allowed is True
    assert result.blocked_reason == ""
    assert result.metadata.get("generic_kb_request") is True


def test_policy_blocks_generic_kb_ticket_without_evidence(monkeypatch, context, capabilities_summary):
    monkeypatch.setattr(settings, "ticketing_plugin_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chat_llm_generic_kb_ticketing_enabled", True, raising=False)
    monkeypatch.setattr(ticketing_service, "is_ticketing_enabled", lambda *_args, **_kwargs: True)

    decision = OrchestrationDecision(
        action="create_ticket",
        ticket={
            "required": True,
            "ready_to_create": True,
            "generic_request": True,
            "phase_applicable": True,
            "issue": "Stay extension request until April 17",
            "evidence": [],
        },
    )

    result = OrchestrationPolicyService().evaluate(
        decision=decision,
        context=context,
        capabilities_summary=capabilities_summary,
        selected_phase_context={"selected_phase_id": "during_stay", "selected_phase_name": "During Stay"},
    )

    assert result.action_allowed is False
    assert result.blocked_reason == "generic_kb_evidence_missing"


def test_response_validator_allows_generic_kb_collection_prompt():
    ctx = ConversationContext(session_id="sess-2", hotel_code="test")
    ctx.add_message(MessageRole.USER, "Can I extend my stay until the 17th?")
    intent_result = IntentResult(
        intent=IntentType.FAQ,
        confidence=0.88,
        entities={"generic_kb_request": True},
    )

    result = response_validator.validate(
        response_text="What date would you like to extend until?",
        intent_result=intent_result,
        context=ctx,
        capabilities_summary={"service_catalog": []},
        capability_check_allowed=True,
        capability_reason="",
    )

    assert result.valid is True
