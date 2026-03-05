"""
FAQ Handler (Stub)

Uses RAG retrieval first. If no evidence is found, returns a polite
knowledge-unavailable response instead of falling back to generic LLM.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from agents.rag_agent import rag_agent
from config.settings import settings
from handlers.base_handler import BaseHandler, HandlerResult
from schemas.chat import ConversationState, IntentResult, ConversationContext
from services.conversation_memory_service import conversation_memory_service
from services.kb_direct_lookup_service import kb_direct_lookup_service


class FAQHandler(BaseHandler):
    """Handle FAQ intents via retrieval-grounded responses."""

    @staticmethod
    def _expand_query_variants(query: str) -> list[str]:
        """
        Expand user phrasing for noisy catalog/menu queries to improve retrieval recall.
        """
        base = str(query or "").strip()
        if not base:
            return []

        variants = [base]
        lowered = base.lower()
        catalog_markers = (
            "menu",
            "menus",
            "food",
            "dining",
            "ird",
            "in room",
            "in-room",
            "catalog",
            "hungry",
            "eat",
            "eating",
            "options",
        )
        if any(marker in lowered for marker in catalog_markers):
            variants.append("show in room dining menu sections with items and prices")
            variants.append("list all menu sections with item names and prices")
            variants.append("what food options are available to eat")
            variants.append("do you serve food to room and what are menu options")
            if "ird" in lowered or "in room" in lowered or "in-room" in lowered:
                variants.append("in room dining ird menu with item_name and price_inr")

        spa_markers = (
            "spa",
            "massage",
            "wellness",
            "therapy",
            "therapist",
            "treatment",
            "package",
        )
        if any(marker in lowered for marker in spa_markers):
            variants.append("show spa treatments and therapies with details")
            variants.append("list spa packages and treatments")
            variants.append("what spa treatments are available")
            variants.append("spa treatment options and package details")
            variants.append("spa therapist availability and timings")

        deduped: list[str] = []
        seen: set[str] = set()
        for item in variants:
            normalized = item.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(item.strip())
        return deduped

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[HandlerResult]:
        entities = intent_result.entities if isinstance(intent_result.entities, dict) else {}
        force_rag_only = bool(entities.get("kb_only_llm_mode", False))
        rewrite = conversation_memory_service.contextualize_follow_up_query(context, message)
        candidate_queries: list[str] = []
        rewritten_query = str(rewrite.get("query") or "").strip()
        if rewritten_query:
            candidate_queries.extend(self._expand_query_variants(rewritten_query))
        original_query = str(message or "").strip()
        if original_query and original_query not in candidate_queries:
            candidate_queries.extend(self._expand_query_variants(original_query))

        rag_result = None
        query_used = original_query
        kb_result = None
        kb_query_used = original_query
        kb_lookup_attempted = False
        kb_only_mode = bool(getattr(settings, "kb_direct_disable_rag_fallback", True)) and not force_rag_only

        if bool(getattr(settings, "kb_direct_lookup_enabled", True)) and not force_rag_only:
            kb_lookup_attempted = True
            for query in candidate_queries:
                kb_query_used = query
                kb_result = await kb_direct_lookup_service.answer_question_async(
                    query=query,
                    tenant_id=context.hotel_code,
                )
                if kb_result.handled:
                    return HandlerResult(
                        response_text=kb_result.answer,
                        next_state=ConversationState.IDLE,
                        suggested_actions=["Need help", "Talk to human"],
                        metadata={
                            "kb_direct_lookup_used": True,
                            "kb_direct_lookup_attempted": True,
                            "kb_direct_lookup_confidence": round(float(kb_result.confidence or 0.0), 2),
                            "kb_direct_lookup_reason": str(kb_result.reason or ""),
                            "kb_direct_lookup_trace_id": str(kb_result.trace_id or ""),
                            "kb_direct_lookup_field": str(kb_result.matched_field or ""),
                            "kb_direct_lookup_source": str(kb_result.source_file or ""),
                            "kb_query": kb_query_used,
                            "kb_query_rewritten": kb_query_used != original_query,
                            "kb_rewrite_reason": rewrite.get("reason"),
                            "rag_used": False,
                            "rag_skipped": True,
                            "kb_only_llm_mode": force_rag_only,
                        },
                    )

            if kb_only_mode:
                return HandlerResult(
                    response_text=(
                        "I could not find this in the current knowledge base for this property. "
                        "If you want, I can connect you with our team."
                    ),
                    next_state=ConversationState.IDLE,
                    suggested_actions=["Talk to human", "Ask another question"],
                    metadata={
                        "kb_direct_lookup_used": False,
                        "kb_direct_lookup_attempted": kb_lookup_attempted,
                        "kb_direct_lookup_confidence": round(float(getattr(kb_result, "confidence", 0.0) or 0.0), 2),
                        "kb_direct_lookup_reason": str(getattr(kb_result, "reason", "no_match")),
                        "kb_direct_lookup_trace_id": str(getattr(kb_result, "trace_id", "") or ""),
                        "kb_query": kb_query_used,
                        "kb_query_rewritten": kb_query_used != original_query,
                        "kb_rewrite_reason": rewrite.get("reason"),
                        "kb_only_mode": True,
                        "rag_used": False,
                        "rag_skipped": True,
                        "rag_no_match": True,
                        "kb_only_llm_mode": force_rag_only,
                    },
                )

        for query in candidate_queries:
            query_used = query
            rag_result = await rag_agent.answer(
                query=query,
                hotel_name=capabilities.get("hotel_name", "Hotel"),
                city=capabilities.get("city", ""),
                tenant_id=context.hotel_code,
                business_type=capabilities.get("business_type", "generic"),
                min_confidence=0.0,
            )
            if rag_result.handled:
                break

        if rag_result is None or not rag_result.handled:
            # Keep FAQ/menu knowledge flow RAG-only: no generic LLM fallback here.
            return HandlerResult(
                response_text=(
                    "I could not find this in the current knowledge base for this property. "
                    "If you want, I can connect you with our team."
                ),
                next_state=ConversationState.IDLE,
                suggested_actions=["Talk to human", "Ask another question"],
                metadata={
                    "kb_direct_lookup_used": False,
                    "kb_direct_lookup_attempted": kb_lookup_attempted,
                    "kb_direct_lookup_confidence": round(float(getattr(kb_result, "confidence", 0.0) or 0.0), 2),
                    "kb_direct_lookup_reason": str(getattr(kb_result, "reason", "not_attempted")),
                    "kb_direct_lookup_trace_id": str(getattr(kb_result, "trace_id", "") or ""),
                    "kb_query": kb_query_used,
                    "kb_query_rewritten": kb_query_used != original_query,
                    "kb_rewrite_reason": rewrite.get("reason"),
                    "kb_only_mode": kb_only_mode,
                    "rag_used": True,
                    "rag_confidence": round(float(getattr(rag_result, "confidence", 0.0) or 0.0), 2),
                    "rag_sources": list(getattr(rag_result, "sources", []) or []),
                    "rag_reason": str(getattr(rag_result, "reason", "no_retrieval_match")),
                    "rag_trace_id": str(getattr(rag_result, "trace_id", "") or ""),
                    "rag_query": query_used,
                    "rag_query_rewritten": query_used != original_query,
                    "rag_rewrite_reason": rewrite.get("reason"),
                    "rag_no_match": True,
                    "kb_only_llm_mode": force_rag_only,
                },
            )

        return HandlerResult(
            response_text=rag_result.answer,
            next_state=ConversationState.IDLE,
            suggested_actions=["Need help", "Talk to human"],
            metadata={
                "kb_direct_lookup_used": False,
                "kb_direct_lookup_attempted": kb_lookup_attempted,
                "kb_direct_lookup_confidence": round(float(getattr(kb_result, "confidence", 0.0) or 0.0), 2),
                "kb_direct_lookup_reason": str(getattr(kb_result, "reason", "not_attempted")),
                "kb_direct_lookup_trace_id": str(getattr(kb_result, "trace_id", "") or ""),
                "kb_query": kb_query_used,
                "kb_query_rewritten": kb_query_used != original_query,
                "kb_rewrite_reason": rewrite.get("reason"),
                "kb_only_mode": kb_only_mode,
                "rag_used": True,
                "rag_confidence": round(rag_result.confidence, 2),
                "rag_sources": rag_result.sources,
                "rag_reason": rag_result.reason,
                "rag_trace_id": str(getattr(rag_result, "trace_id", "") or ""),
                "rag_query": query_used,
                "rag_query_rewritten": query_used != original_query,
                "rag_rewrite_reason": rewrite.get("reason"),
                "kb_only_llm_mode": force_rag_only,
            },
        )
