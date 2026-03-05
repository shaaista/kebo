from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field

from schemas.chat import ConversationContext, ConversationState, IntentResult


class HandlerResult(BaseModel):
    """Result returned by any intent handler."""

    response_text: str
    next_state: ConversationState = ConversationState.IDLE
    suggested_actions: list[str] = Field(default_factory=list)
    pending_action: Optional[str] = None
    pending_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseHandler(ABC):
    """Abstract base class that every intent handler must implement."""

    @abstractmethod
    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict,
        db_session=None,
    ) -> HandlerResult:
        pass

    # ------------------------------------------------------------------
    # Shared helpers available to all handlers
    # ------------------------------------------------------------------

    def _is_capability_enabled(self, capabilities: dict, capability_id: str) -> bool:
        """Check if a capability is enabled in the capabilities dict."""
        caps = capabilities.get("capabilities", {})
        cap = caps.get(capability_id, {})
        return cap.get("enabled", False)

    def _get_business_city(self, capabilities: dict) -> str:
        """Get the business city from capabilities."""
        return capabilities.get("city", "")
