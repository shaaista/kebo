import pytest
import importlib

from agents.complex_query_orchestrator import complex_query_orchestrator
from schemas.chat import ConversationContext, IntentResult, IntentType

orchestrator_module = importlib.import_module("agents.complex_query_orchestrator")


@pytest.mark.asyncio
async def test_complex_orchestrator_prefers_rag_when_available(monkeypatch):
    class DummyRagResult:
        handled = True
        answer = "RAG grounded answer"
        confidence = 0.81
        sources = ["faq.md#faq.md:0"]
        reason = "ok"

    async def fake_rag_answer(*args, **kwargs):
        return DummyRagResult()

    async def fake_chat(*args, **kwargs):
        return "LLM answer should not be used"

    monkeypatch.setattr(orchestrator_module.rag_agent, "answer", fake_rag_answer)
    monkeypatch.setattr(orchestrator_module.llm_client, "chat", fake_chat)

    context = ConversationContext(session_id="s1", hotel_code="default")
    result = await complex_query_orchestrator.handle(
        message="what are checkout rules and late checkout fees?",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.76, entities={}),
        context=context,
        capabilities_summary={"hotel_name": "Demo Hotel", "city": "Bengaluru", "business_type": "hotel"},
        llm_context={"enabled_intents": ["faq"], "service_catalog": [], "capabilities": {}, "nlu_policy": {}},
        routing_signals=["needs_research"],
    )

    assert result.handled is True
    assert "RAG grounded answer" in result.response_text
    assert "rag_agent" in result.metadata.get("agents_used", [])
    assert "complex_response_agent" not in result.metadata.get("agents_used", [])


@pytest.mark.asyncio
async def test_complex_orchestrator_falls_back_to_complex_response_agent(monkeypatch):
    class DummyRagResult:
        handled = False
        answer = ""
        confidence = 0.2
        sources = []
        reason = "no_retrieval_match"

    async def fake_rag_answer(*args, **kwargs):
        return DummyRagResult()

    async def fake_chat(*args, **kwargs):
        return "Complex synthesized answer"

    monkeypatch.setattr(orchestrator_module.rag_agent, "answer", fake_rag_answer)
    monkeypatch.setattr(orchestrator_module.llm_client, "chat", fake_chat)

    context = ConversationContext(session_id="s2", hotel_code="default")
    result = await complex_query_orchestrator.handle(
        message="compare transport options and best time to travel",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.72, entities={}),
        context=context,
        capabilities_summary={"hotel_name": "Demo Hotel", "city": "Bengaluru", "business_type": "hotel"},
        llm_context={"enabled_intents": ["faq"], "service_catalog": [], "capabilities": {}, "nlu_policy": {}},
        routing_signals=["needs_research", "multi_intent"],
    )

    assert result.handled is True
    assert result.response_text == "Complex synthesized answer"
    assert "rag_agent" in result.metadata.get("agents_used", [])
    assert "complex_response_agent" in result.metadata.get("agents_used", [])
