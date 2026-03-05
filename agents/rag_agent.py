"""
RAG Agent

Dedicated agent boundary for the RAG workstream so it can evolve independently
from core chat orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from services.rag_service import rag_service


@dataclass
class RAGAgentResult:
    handled: bool
    answer: str = ""
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    reason: str = ""
    trace_id: str = ""


class RAGAgent:
    """
    Isolated RAG-facing agent with a stable interface.

    This makes it easy to:
    - keep RAG development in a separate workstream
    - swap retrieval backend later (Qdrant/LlamaIndex/etc.)
    - use the same agent for website widget + WhatsApp channels
    """

    name = "rag_agent"
    version = "0.1.0"

    async def answer(
        self,
        query: str,
        hotel_name: str,
        city: str,
        tenant_id: str = "default",
        business_type: str = "generic",
        min_confidence: float = 0.35,
    ) -> RAGAgentResult:
        rag_answer = await rag_service.answer_question(
            question=query,
            hotel_name=hotel_name,
            city=city,
            tenant_id=tenant_id,
            business_type=business_type,
        )
        if rag_answer is None:
            return RAGAgentResult(
                handled=False,
                reason="no_retrieval_match",
            )

        if rag_answer.confidence < min_confidence:
            return RAGAgentResult(
                handled=False,
                confidence=rag_answer.confidence,
                sources=rag_answer.sources,
                reason="low_confidence",
            )

        return RAGAgentResult(
            handled=True,
            answer=rag_answer.answer,
            confidence=rag_answer.confidence,
            sources=rag_answer.sources,
            reason="ok",
            trace_id=rag_answer.trace_id,
        )


rag_agent = RAGAgent()
