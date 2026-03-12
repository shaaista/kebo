from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


_ALLOWED_ACTIONS = {
    "respond_only",
    "collect_info",
    "dispatch_handler",
    "create_ticket",
    "resume_pending",
    "cancel_pending",
}


class TicketDecision(BaseModel):
    """Ticket intent emitted by LLM orchestration."""

    required: bool = False
    ready_to_create: bool = False
    reason: str = ""
    issue: str = ""
    category: str = ""
    sub_category: str = ""
    priority: str = ""

    @field_validator("category", "sub_category", "priority", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")


class OrchestrationDecision(BaseModel):
    """
    Validated contract for orchestration output.
    This is used by both the top-level orchestration LLM and service-level LLM.
    """

    normalized_query: str = ""
    intent: str = "faq"
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    action: Literal[
        "respond_only",
        "collect_info",
        "dispatch_handler",
        "create_ticket",
        "resume_pending",
        "cancel_pending",
    ] = "respond_only"
    target_service_id: str = ""
    response_text: str = ""
    pending_action: str | None = None
    pending_data_updates: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    answered_current_query: bool = True
    blocking_fields: list[str] = Field(default_factory=list)
    deferrable_fields: list[str] = Field(default_factory=list)
    followup_question: str = ""
    suggested_actions: list[str] = Field(default_factory=list)
    use_handler: bool = False
    handler_intent: str = ""
    interrupt_pending: bool = False
    resume_pending: bool = False
    cancel_pending: bool = False
    requires_human_handoff: bool = False
    ticket: TicketDecision = Field(default_factory=TicketDecision)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("intent", "handler_intent", "target_service_id", mode="before")
    @classmethod
    def _normalize_identifier(cls, value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")

    @field_validator("pending_action", mode="before")
    @classmethod
    def _normalize_pending_action(cls, value: Any) -> str | None:
        text = str(value or "").strip().lower().replace(" ", "_")
        return text or None

    @field_validator("missing_fields", mode="before")
    @classmethod
    def _normalize_missing_fields(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            key = str(item or "").strip().lower().replace(" ", "_")
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        return normalized[:12]

    @field_validator("blocking_fields", "deferrable_fields", mode="before")
    @classmethod
    def _normalize_field_groups(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            key = str(item or "").strip().lower().replace(" ", "_")
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        return normalized[:12]

    @field_validator("followup_question", mode="before")
    @classmethod
    def _normalize_followup_question(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("suggested_actions", mode="before")
    @classmethod
    def _normalize_suggested_actions(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
            if len(result) >= 6:
                break
        return result

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: Any) -> str:
        action = str(value or "").strip().lower().replace(" ", "_")
        return action if action in _ALLOWED_ACTIONS else "respond_only"
