"""
Complex Query Orchestrator

Hybrid "agent team" for COMPLEX-routed requests:
1) Orchestrator step (plan)
2) Research agent (RAG)
3) Planner + Executor (deterministic for now)
4) Response synthesizer (LLM)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.rag_agent import rag_agent
from llm.client import llm_client
from schemas.chat import ConversationContext, IntentResult, IntentType
from services.conversation_memory_service import conversation_memory_service


@dataclass
class ComplexOrchestratorResult:
    handled: bool
    response_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class ResearchOutput:
    attempted: bool = False
    handled: bool = False
    answer: str = ""
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    reason: str = ""
    trace_id: str = ""
    query_used: str = ""
    query_rewritten: bool = False
    rewrite_reason: str = ""


@dataclass
class PlannerOutput:
    execution_mode: str = "answer_only"
    priority: str = "normal"
    plan_steps: list[str] = field(default_factory=list)
    requires_human: bool = False


@dataclass
class ExecutionOutput:
    action_taken: str = "none"
    used_executor: bool = False
    execution_message: str = ""


class ComplexQueryOrchestrator:
    """
    Agent-team orchestration for high-complexity user messages.
    """

    name = "complex_query_orchestrator"
    version = "0.2.0"

    def _build_orchestrator_plan(
        self,
        intent_result: IntentResult,
        routing_signals: list[str],
    ) -> dict[str, Any]:
        intent = intent_result.intent
        signals = set(routing_signals or [])

        should_try_rag = (
            intent in {IntentType.FAQ, IntentType.MENU_REQUEST}
            or "needs_research" in signals
            or "multi_intent" in signals
        )

        force_synthesis = bool("multi_intent" in signals or "frustration" in signals)
        requires_human = intent in {IntentType.HUMAN_REQUEST, IntentType.HEALTH_SUPPORT}
        if "clarification_loop" in signals:
            requires_human = True

        return {
            "should_try_rag": should_try_rag,
            "force_synthesis": force_synthesis,
            "requires_human": requires_human,
            "routing_signals": list(routing_signals or []),
        }

    async def _run_research_agent(
        self,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_context: dict[str, Any],
        should_try_rag: bool,
    ) -> ResearchOutput:
        if not should_try_rag:
            return ResearchOutput(attempted=False, reason="not_required")

        rewrite = conversation_memory_service.contextualize_follow_up_query(context, message)
        candidate_queries: list[str] = []
        rewritten_query = str(rewrite.get("query") or "").strip()
        if rewritten_query:
            candidate_queries.append(rewritten_query)
        original_query = str(message or "").strip()
        if original_query and original_query not in candidate_queries:
            candidate_queries.append(original_query)

        rag_result = None
        query_used = original_query
        for query in candidate_queries:
            query_used = query
            rag_result = await rag_agent.answer(
                query=query,
                hotel_name=capabilities_summary.get("hotel_name", llm_context.get("hotel_name", "Business")),
                city=capabilities_summary.get("city", llm_context.get("city", "")),
                tenant_id=context.hotel_code,
                business_type=capabilities_summary.get("business_type", llm_context.get("business_type", "generic")),
                min_confidence=0.3,
            )
            if rag_result.handled:
                break

        if rag_result is None:
            rag_result = await rag_agent.answer(
                query=original_query,
                hotel_name=capabilities_summary.get("hotel_name", llm_context.get("hotel_name", "Business")),
                city=capabilities_summary.get("city", llm_context.get("city", "")),
                tenant_id=context.hotel_code,
                business_type=capabilities_summary.get("business_type", llm_context.get("business_type", "generic")),
                min_confidence=0.3,
            )

        return ResearchOutput(
            attempted=True,
            handled=bool(rag_result.handled),
            answer=str(getattr(rag_result, "answer", "") or ""),
            confidence=float(getattr(rag_result, "confidence", 0.0) or 0.0),
            sources=list(getattr(rag_result, "sources", []) or []),
            reason=str(getattr(rag_result, "reason", "") or ""),
            trace_id=str(getattr(rag_result, "trace_id", "") or ""),
            query_used=query_used,
            query_rewritten=query_used != original_query,
            rewrite_reason=str(rewrite.get("reason") or ""),
        )

    def _run_planner_agent(
        self,
        intent_result: IntentResult,
        routing_signals: list[str],
        orchestrator_plan: dict[str, Any],
    ) -> PlannerOutput:
        signals = set(routing_signals or [])
        intent = intent_result.intent
        plan_steps: list[str] = ["analyze_user_need"]
        priority = "normal"
        execution_mode = "answer_only"

        if "frustration" in signals:
            priority = "high"
            plan_steps.append("apply_deescalation_tone")

        if intent in {IntentType.HUMAN_REQUEST, IntentType.HEALTH_SUPPORT} or orchestrator_plan.get("requires_human"):
            execution_mode = "offer_human_handoff"
            plan_steps.append("offer_human_handoff")
        elif intent == IntentType.UNCLEAR or "ambiguous" in signals:
            execution_mode = "ask_clarification"
            plan_steps.append("ask_targeted_clarification")

        if orchestrator_plan.get("should_try_rag"):
            plan_steps.append("ground_with_retrieval")
        plan_steps.append("generate_safe_response")

        return PlannerOutput(
            execution_mode=execution_mode,
            priority=priority,
            plan_steps=plan_steps,
            requires_human=bool(orchestrator_plan.get("requires_human")),
        )

    def _run_executor_agent(
        self,
        planner_output: PlannerOutput,
        intent_result: IntentResult,
    ) -> ExecutionOutput:
        # External tool actions (ticketing/OCR/etc.) are intentionally out-of-scope
        # for this orchestrator stage. Executor currently performs safe inline actions.
        if planner_output.execution_mode == "offer_human_handoff":
            return ExecutionOutput(
                action_taken="prepare_human_handoff_offer",
                used_executor=True,
                execution_message=(
                    "I can connect you with our team right away if you want a human specialist to continue."
                ),
            )
        if planner_output.execution_mode == "ask_clarification":
            return ExecutionOutput(
                action_taken="clarification_prompt",
                used_executor=True,
                execution_message="Could you share a little more detail so I can answer this accurately?",
            )
        return ExecutionOutput(
            action_taken="none",
            used_executor=False,
            execution_message="",
        )

    async def _run_response_synthesizer(
        self,
        message: str,
        intent_result: IntentResult,
        llm_context: dict[str, Any],
        research_output: ResearchOutput,
        planner_output: PlannerOutput,
        execution_output: ExecutionOutput,
    ) -> str:
        prompt = (
            "You are the response synthesizer for a complex business-assistant workflow.\n"
            "Use available research findings and follow the plan. Keep response concise, factual, and safe.\n"
            "If capability is uncertain, offer human support and avoid overpromising.\n\n"
            f"Business: {llm_context.get('hotel_name', 'Business')}\n"
            f"Business Type: {llm_context.get('business_type', 'generic')}\n"
            f"City: {llm_context.get('city', '')}\n"
            f"Intent: {intent_result.intent.value}\n"
            f"Confidence: {intent_result.confidence:.2f}\n"
            f"Entities: {intent_result.entities}\n"
            f"Current State: {llm_context.get('state', 'idle')}\n"
            f"Pending Action: {llm_context.get('pending_action')}\n"
            f"Enabled Intents: {llm_context.get('enabled_intents', [])}\n"
            f"Service Catalog: {llm_context.get('service_catalog', [])}\n"
            f"Capabilities: {llm_context.get('capabilities', {})}\n"
            f"NLU Policy: {llm_context.get('nlu_policy', {})}\n\n"
            f"Research Attempted: {research_output.attempted}\n"
            f"Research Handled: {research_output.handled}\n"
            f"Research Confidence: {research_output.confidence:.2f}\n"
            f"Research Sources: {research_output.sources}\n"
            f"Research Answer: {research_output.answer}\n"
            f"Planner Mode: {planner_output.execution_mode}\n"
            f"Planner Steps: {planner_output.plan_steps}\n"
            f"Executor Action: {execution_output.action_taken}\n"
            f"Executor Message: {execution_output.execution_message}\n\n"
            f"User message: {message}\n"
        )

        try:
            response = await llm_client.chat(
                messages=[{"role": "system", "content": prompt}],
                temperature=0.3,
                max_tokens=340,
            )
        except Exception:
            response = ""
        return str(response or "").strip()

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_context: dict[str, Any],
        routing_signals: list[str],
    ) -> ComplexOrchestratorResult:
        metadata: dict[str, Any] = {
            "agents_used": [],
            "routing_signals": routing_signals,
            "agent_team": {},
        }

        orchestrator_plan = self._build_orchestrator_plan(intent_result, routing_signals)
        metadata["agents_used"].append("orchestrator_agent")
        metadata["agent_team"]["orchestrator"] = orchestrator_plan

        research_output = await self._run_research_agent(
            message=message,
            context=context,
            capabilities_summary=capabilities_summary,
            llm_context=llm_context,
            should_try_rag=bool(orchestrator_plan.get("should_try_rag")),
        )
        if research_output.attempted:
            metadata["agents_used"].append("rag_agent")
        metadata["agent_team"]["research"] = {
            "attempted": research_output.attempted,
            "handled": research_output.handled,
            "reason": research_output.reason,
            "confidence": research_output.confidence,
            "sources": research_output.sources,
            "trace_id": research_output.trace_id,
            "query_used": research_output.query_used,
            "query_rewritten": research_output.query_rewritten,
            "rewrite_reason": research_output.rewrite_reason,
        }

        # Backward-compatible RAG metadata keys used by existing dashboards/tests.
        metadata["rag_reason"] = research_output.reason
        metadata["rag_confidence"] = research_output.confidence
        metadata["rag_sources"] = research_output.sources
        metadata["rag_trace_id"] = research_output.trace_id
        metadata["rag_query"] = research_output.query_used
        metadata["rag_query_rewritten"] = research_output.query_rewritten
        metadata["rag_rewrite_reason"] = research_output.rewrite_reason

        if research_output.handled and not bool(orchestrator_plan.get("force_synthesis")):
            return ComplexOrchestratorResult(
                handled=True,
                response_text=research_output.answer,
                metadata=metadata,
                reason="rag_answered",
            )

        planner_output = self._run_planner_agent(
            intent_result=intent_result,
            routing_signals=routing_signals,
            orchestrator_plan=orchestrator_plan,
        )
        metadata["agents_used"].append("planner_agent")
        metadata["agent_team"]["planner"] = {
            "execution_mode": planner_output.execution_mode,
            "priority": planner_output.priority,
            "plan_steps": planner_output.plan_steps,
            "requires_human": planner_output.requires_human,
        }

        execution_output = self._run_executor_agent(
            planner_output=planner_output,
            intent_result=intent_result,
        )
        if execution_output.used_executor:
            metadata["agents_used"].append("executor_agent")
        metadata["agent_team"]["executor"] = {
            "action_taken": execution_output.action_taken,
            "used_executor": execution_output.used_executor,
        }

        synthesized = await self._run_response_synthesizer(
            message=message,
            intent_result=intent_result,
            llm_context=llm_context,
            research_output=research_output,
            planner_output=planner_output,
            execution_output=execution_output,
        )
        metadata["agents_used"].append("complex_response_agent")
        metadata["agent_team"]["synthesizer"] = {
            "used_llm": True,
            "response_chars": len(synthesized),
        }

        if synthesized:
            return ComplexOrchestratorResult(
                handled=True,
                response_text=synthesized,
                metadata=metadata,
                reason="complex_response_generated",
            )

        if research_output.handled:
            return ComplexOrchestratorResult(
                handled=True,
                response_text=research_output.answer,
                metadata=metadata,
                reason="rag_fallback_after_synth_failure",
            )

        if execution_output.execution_message:
            return ComplexOrchestratorResult(
                handled=True,
                response_text=execution_output.execution_message,
                metadata=metadata,
                reason="executor_fallback",
            )

        return ComplexOrchestratorResult(
            handled=False,
            metadata=metadata,
            reason="agent_team_no_answer",
        )


complex_query_orchestrator = ComplexQueryOrchestrator()
