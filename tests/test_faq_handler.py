from types import SimpleNamespace

import pytest

from handlers.faq_handler import FAQHandler
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType


@pytest.mark.asyncio
async def test_faq_handler_returns_polite_message_when_rag_has_no_match(monkeypatch):
    handler = FAQHandler()
    monkeypatch.setattr("handlers.faq_handler.settings.kb_direct_lookup_enabled", False)
    context = ConversationContext(
        session_id="faq-no-match",
        hotel_code="tenant_a",
        state=ConversationState.IDLE,
    )

    async def _fake_rag_answer(**kwargs):
        return SimpleNamespace(
            handled=False,
            answer="",
            confidence=0.0,
            sources=[],
            reason="no_retrieval_match",
        )

    monkeypatch.setattr("handlers.faq_handler.rag_agent.answer", _fake_rag_answer)

    result = await handler.handle(
        message="show me your menu",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities={"hotel_name": "Demo", "city": "Mumbai", "business_type": "hotel"},
    )

    assert result is not None
    assert "could not find this in the current knowledge base" in result.response_text.lower()
    assert result.metadata.get("rag_no_match") is True


@pytest.mark.asyncio
async def test_faq_handler_uses_query_expansion_for_hungry_phrasing(monkeypatch):
    handler = FAQHandler()
    monkeypatch.setattr("handlers.faq_handler.settings.kb_direct_lookup_enabled", False)
    context = ConversationContext(
        session_id="faq-hungry",
        hotel_code="tenant_b",
        state=ConversationState.IDLE,
    )

    seen_queries: list[str] = []

    async def _fake_rag_answer(**kwargs):
        seen_queries.append(str(kwargs.get("query") or ""))
        # Only a catalog-expanded query should match.
        if "menu sections with item names and prices" in seen_queries[-1]:
            return SimpleNamespace(
                handled=True,
                answer="Menu sections available.",
                confidence=0.92,
                sources=["doc#1"],
                reason="ok",
            )
        return SimpleNamespace(
            handled=False,
            answer="",
            confidence=0.0,
            sources=[],
            reason="no_retrieval_match",
        )

    monkeypatch.setattr("handlers.faq_handler.rag_agent.answer", _fake_rag_answer)

    result = await handler.handle(
        message="i am hungry what are options to eat",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities={"hotel_name": "Demo", "city": "Mumbai", "business_type": "hotel"},
    )

    assert result is not None
    assert result.response_text == "Menu sections available."
    assert any("menu sections with item names and prices" in query for query in seen_queries)


@pytest.mark.asyncio
async def test_faq_handler_uses_query_expansion_for_spa_treatments(monkeypatch):
    handler = FAQHandler()
    monkeypatch.setattr("handlers.faq_handler.settings.kb_direct_lookup_enabled", False)
    context = ConversationContext(
        session_id="faq-spa-treatments",
        hotel_code="tenant_spa",
        state=ConversationState.IDLE,
    )

    seen_queries: list[str] = []

    async def _fake_rag_answer(**kwargs):
        seen_queries.append(str(kwargs.get("query") or ""))
        if "show spa treatments and therapies with details" in seen_queries[-1]:
            return SimpleNamespace(
                handled=True,
                answer="Spa treatments: Swedish Massage, Deep Tissue Massage.",
                confidence=0.91,
                sources=["doc#spa"],
                reason="ok",
            )
        return SimpleNamespace(
            handled=False,
            answer="",
            confidence=0.0,
            sources=[],
            reason="no_retrieval_match",
        )

    monkeypatch.setattr("handlers.faq_handler.rag_agent.answer", _fake_rag_answer)

    result = await handler.handle(
        message="show spa treatments",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities={"hotel_name": "Demo", "city": "Mumbai", "business_type": "hotel"},
    )

    assert result is not None
    assert "swedish massage" in result.response_text.lower()
    assert any("show spa treatments and therapies with details" in query for query in seen_queries)


@pytest.mark.asyncio
async def test_faq_handler_kb_only_mode_skips_rag_when_kb_has_no_match(monkeypatch):
    handler = FAQHandler()
    monkeypatch.setattr("handlers.faq_handler.settings.kb_direct_lookup_enabled", True)
    monkeypatch.setattr("handlers.faq_handler.settings.kb_direct_disable_rag_fallback", True)
    context = ConversationContext(
        session_id="faq-kb-only-no-match",
        hotel_code="tenant_c",
        state=ConversationState.IDLE,
    )

    async def _fake_kb_answer(**kwargs):
        return SimpleNamespace(
            handled=False,
            answer="",
            confidence=0.0,
            reason="score_below_threshold",
            matched_field="",
            source_file="",
            trace_id="kb-test-1",
        )

    async def _rag_should_not_run(**kwargs):
        raise AssertionError("RAG should not run in KB-only mode when KB has no match")

    monkeypatch.setattr("handlers.faq_handler.kb_direct_lookup_service.answer_question_async", _fake_kb_answer)
    monkeypatch.setattr("handlers.faq_handler.rag_agent.answer", _rag_should_not_run)

    result = await handler.handle(
        message="some unknown faq question",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities={"hotel_name": "Demo", "city": "Mumbai", "business_type": "hotel"},
    )

    assert result is not None
    assert "could not find this in the current knowledge base" in result.response_text.lower()
    assert result.metadata.get("kb_only_mode") is True
    assert result.metadata.get("rag_used") is False
    assert result.metadata.get("rag_skipped") is True


@pytest.mark.asyncio
async def test_faq_handler_overrides_inactive_service_availability_claim(monkeypatch):
    handler = FAQHandler()
    monkeypatch.setattr("handlers.faq_handler.settings.kb_direct_lookup_enabled", False)
    context = ConversationContext(
        session_id="faq-inactive-service-override",
        hotel_code="tenant_d",
        state=ConversationState.IDLE,
    )

    async def _fake_rag_answer(**kwargs):
        return SimpleNamespace(
            handled=True,
            answer="Yes, Kadak is available right now and we can help immediately.",
            confidence=0.9,
            sources=["doc#inactive"],
            reason="ok",
        )

    monkeypatch.setattr("handlers.faq_handler.rag_agent.answer", _fake_rag_answer)

    result = await handler.handle(
        message="is kadak available now",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities={
            "hotel_name": "Demo",
            "city": "Mumbai",
            "business_type": "hotel",
            "service_catalog": [
                {
                    "id": "kadak",
                    "name": "Kadak",
                    "type": "restaurant",
                    "is_active": False,
                    "delivery_zones": ["dine_in_only"],
                }
            ],
        },
    )

    assert result is not None
    assert "currently unavailable" in result.response_text.lower()
    assert result.metadata.get("service_context_blended") is True
    assert result.metadata.get("service_context_reason") == "inactive_service_override"


@pytest.mark.asyncio
async def test_faq_handler_overrides_room_delivery_for_dine_in_only_service(monkeypatch):
    handler = FAQHandler()
    monkeypatch.setattr("handlers.faq_handler.settings.kb_direct_lookup_enabled", False)
    context = ConversationContext(
        session_id="faq-delivery-constraint-override",
        hotel_code="tenant_e",
        state=ConversationState.IDLE,
    )

    async def _fake_rag_answer(**kwargs):
        return SimpleNamespace(
            handled=True,
            answer="Yes, Kadak can be delivered to your room.",
            confidence=0.91,
            sources=["doc#delivery"],
            reason="ok",
        )

    monkeypatch.setattr("handlers.faq_handler.rag_agent.answer", _fake_rag_answer)

    result = await handler.handle(
        message="can kadak deliver to room",
        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
        context=context,
        capabilities={
            "hotel_name": "Demo",
            "city": "Mumbai",
            "business_type": "hotel",
            "service_catalog": [
                {
                    "id": "kadak",
                    "name": "Kadak",
                    "type": "restaurant",
                    "is_active": True,
                    "delivery_zones": ["dine_in_only"],
                }
            ],
        },
    )

    assert result is not None
    assert "dine-in only" in result.response_text.lower()
    assert result.metadata.get("service_context_blended") is True
    assert result.metadata.get("service_context_reason") == "delivery_constraint_override"
