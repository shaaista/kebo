"""
Ticketing route decision service.

Decides whether a new complaint message should:
- acknowledge an existing open ticket
- update an existing open ticket
- create a new ticket
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import logging
import re
from typing import Any

from config.settings import settings
from llm.client import llm_client

logger = logging.getLogger(__name__)

_ROUTER_PROMPT = """
You route a new guest message to one of three outcomes against open ticket candidates.

Input:
- conversation: recent chat ending with latest user message
- latest_user_message: latest user message only
- candidates: open/in-progress tickets

Rules:
1) acknowledge:
   - choose when latest user message is effectively the same issue as an existing open ticket
   - no meaningful new details
2) update:
   - choose when message refers to an existing issue but adds/changes useful details
   - include concise manager_notes
3) create:
   - choose only when message is a different issue from all candidates

Return strict JSON only:
{
  "decision": "acknowledge" | "update" | "create",
  "update_ticket_id": "string or null",
  "manager_notes": "string or null",
  "response": "short user-facing reply"
}
""".strip()


@dataclass
class TicketRouteDecision:
    decision: str = "create"
    update_ticket_id: str = ""
    manager_notes: str = ""
    response: str = ""
    source: str = "heuristic"


class TicketingRouterService:
    """LLM-first route decision with safe deterministic fallback."""

    async def decide(
        self,
        *,
        conversation: str,
        latest_user_message: str,
        candidates: list[dict[str, Any]],
    ) -> TicketRouteDecision:
        if not candidates:
            return TicketRouteDecision(
                decision="create",
                response="I will create a new support ticket for this request.",
                source="none",
            )

        use_llm = bool(getattr(settings, "ticketing_smart_routing_use_llm", True))
        if use_llm:
            llm_decision = await self._llm_decide(
                conversation=conversation,
                latest_user_message=latest_user_message,
                candidates=candidates,
            )
            if llm_decision is not None:
                return llm_decision

        return self._heuristic_decide(
            latest_user_message=latest_user_message,
            candidates=candidates,
        )

    async def _llm_decide(
        self,
        *,
        conversation: str,
        latest_user_message: str,
        candidates: list[dict[str, Any]],
    ) -> TicketRouteDecision | None:
        payload = {
            "conversation": str(conversation or "").strip(),
            "latest_user_message": str(latest_user_message or "").strip(),
            "candidates": candidates,
        }
        messages = [
            {"role": "system", "content": _ROUTER_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            model = str(getattr(settings, "ticketing_router_model", "") or "").strip() or None
            parsed = await llm_client.chat_with_json(messages, model=model, temperature=0.0)
        except Exception:
            logger.exception("Ticket router LLM call failed")
            return None

        decision = self._normalize_llm_decision(parsed, candidates, latest_user_message)
        if decision is None:
            return None
        decision.source = "llm"
        return decision

    @staticmethod
    def _normalize_llm_decision(
        parsed: dict[str, Any] | Any,
        candidates: list[dict[str, Any]],
        latest_user_message: str,
    ) -> TicketRouteDecision | None:
        if not isinstance(parsed, dict):
            return None

        valid_ids = {
            str(c.get("id") or "").strip()
            for c in candidates
            if str(c.get("id") or "").strip()
        }
        decision_raw = str(parsed.get("decision") or "").strip().lower()
        response = str(parsed.get("response") or "").strip()
        manager_notes = str(parsed.get("manager_notes") or "").strip()
        update_id = str(parsed.get("update_ticket_id") or "").strip()

        if decision_raw not in {"acknowledge", "update", "create"}:
            return None

        if decision_raw == "create":
            return TicketRouteDecision(
                decision="create",
                response=response or "I will create a new support ticket for this request.",
            )

        if decision_raw == "acknowledge":
            return TicketRouteDecision(
                decision="acknowledge",
                response=response
                or "Thanks for the update. We are already working on your existing ticket.",
            )

        # decision == update
        if update_id and update_id not in valid_ids:
            return None
        if not update_id and valid_ids:
            update_id = next(iter(valid_ids))
        if not update_id:
            return None
        if not manager_notes:
            manager_notes = str(latest_user_message or "").strip()
        return TicketRouteDecision(
            decision="update",
            update_ticket_id=update_id,
            manager_notes=manager_notes,
            response=response or "Thanks for the update. I have added this to your existing ticket.",
        )

    def _heuristic_decide(
        self,
        *,
        latest_user_message: str,
        candidates: list[dict[str, Any]],
    ) -> TicketRouteDecision:
        latest = str(latest_user_message or "").strip()
        if not latest:
            return TicketRouteDecision(
                decision="acknowledge",
                response="We are already reviewing your earlier request.",
            )

        best: dict[str, Any] | None = None
        best_score = -1.0
        for candidate in candidates:
            issue = str(candidate.get("issue") or "").strip().lower()
            notes = str(candidate.get("manager_notes") or "").strip().lower()
            msg = latest.lower()
            score_issue = SequenceMatcher(None, msg, issue).ratio() if issue else 0.0
            score_notes = SequenceMatcher(None, msg, notes).ratio() if notes else 0.0
            score = max(score_issue, score_notes)
            if score > best_score:
                best_score = score
                best = candidate

        if not best:
            return TicketRouteDecision(
                decision="create",
                response="I will create a new support ticket for this request.",
            )

        ack_threshold = float(getattr(settings, "ticketing_router_ack_similarity", 0.88) or 0.88)
        update_threshold = float(getattr(settings, "ticketing_router_update_similarity", 0.55) or 0.55)
        best_issue = str(best.get("issue") or "")
        ticket_id = str(best.get("id") or "").strip()
        has_context_change = self._has_context_change(latest, best_issue)

        if best_score >= ack_threshold and not has_context_change:
            return TicketRouteDecision(
                decision="acknowledge",
                response="Thanks for checking in. We are already working on your earlier ticket.",
            )

        if best_score >= update_threshold and ticket_id:
            return TicketRouteDecision(
                decision="update",
                update_ticket_id=ticket_id,
                manager_notes=latest[:500],
                response="Thanks for the update. I have added this detail to your ticket.",
            )

        return TicketRouteDecision(
            decision="create",
            response="I will create a new support ticket for this request.",
        )

    @staticmethod
    def _has_context_change(latest_user_message: str, reference_issue: str) -> bool:
        latest = str(latest_user_message or "").lower()
        reference = str(reference_issue or "").lower()
        if not latest:
            return False

        latest_markers = TicketingRouterService._extract_context_markers(latest)
        ref_markers = TicketingRouterService._extract_context_markers(reference)
        if latest_markers and latest_markers != ref_markers:
            return True

        latest_numbers = set(re.findall(r"\b\d+\b", latest))
        ref_numbers = set(re.findall(r"\b\d+\b", reference))
        if latest_numbers and latest_numbers != ref_numbers:
            return True

        return False

    @staticmethod
    def _extract_context_markers(text: str) -> set[str]:
        markers: set[str] = set()
        lowered = str(text or "").lower()
        if not lowered:
            return markers

        for word in (
            "today",
            "tomorrow",
            "tonight",
            "morning",
            "afternoon",
            "evening",
            "urgent",
            "asap",
            "immediately",
            "again",
            "still",
            "not resolved",
        ):
            if word in lowered:
                markers.add(word)

        for found in re.findall(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", lowered):
            markers.add(found.strip())

        for found in re.findall(r"\b(?:for|at|by)\s+\d+\b", lowered):
            markers.add(found.strip())

        return markers


ticketing_router_service = TicketingRouterService()

