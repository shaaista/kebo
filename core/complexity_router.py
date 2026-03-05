"""
Complexity Router

Initial implementation of the hybrid routing concept from ARC docs.
It classifies a message as SIMPLE or COMPLEX using lightweight heuristics.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from schemas.chat import ConversationContext


class ProcessingPath(str, Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"


class ComplexityDecision(BaseModel):
    path: ProcessingPath
    score: float = Field(ge=0.0)
    signals: list[str] = Field(default_factory=list)


class ComplexityRouter:
    """Heuristic complexity router used before intent processing."""

    _FRUSTRATION_TOKENS = {
        "angry",
        "annoyed",
        "frustrated",
        "ridiculous",
        "useless",
        "bro",
        "wtf",
        "mad",
    }

    _RESEARCH_TOKENS = {
        "recommend",
        "best",
        "compare",
        "which is better",
        "options",
    }

    _MULTI_INTENT_JOINERS = {" and ", ",", " also ", " plus ", " then "}

    def route(self, message: str, context: ConversationContext | None = None) -> ComplexityDecision:
        msg = (message or "").strip().lower()
        if not msg:
            return ComplexityDecision(path=ProcessingPath.SIMPLE, score=0.0, signals=[])

        signals: list[str] = []
        score = 0.0

        # Multi-intent / composite request indicator
        if any(joiner in msg for joiner in self._MULTI_INTENT_JOINERS) and len(msg.split()) > 8:
            signals.append("multi_intent")
            score += 0.4

        # User frustration signal
        if any(token in msg for token in self._FRUSTRATION_TOKENS):
            signals.append("frustration")
            score += 0.35

        # Research / recommendation style asks
        if any(token in msg for token in self._RESEARCH_TOKENS):
            signals.append("needs_research")
            score += 0.3

        # Ambiguity signal
        if len(msg.split()) <= 2 and msg not in {"yes", "no", "ok", "hello", "hi"}:
            signals.append("ambiguous")
            score += 0.25

        # Repeated clarification loops are more likely complex
        if context:
            clarifications = int(context.pending_data.get("_clarification_attempts", 0))
            if clarifications >= 1:
                signals.append("clarification_loop")
                score += min(0.25, 0.1 * clarifications)

        path = ProcessingPath.COMPLEX if score >= 0.55 else ProcessingPath.SIMPLE
        return ComplexityDecision(path=path, score=round(score, 2), signals=signals)


# Global instance
complexity_router = ComplexityRouter()

