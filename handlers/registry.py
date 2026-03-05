from typing import Optional

from schemas.chat import ConversationContext, IntentResult, IntentType

from handlers.base_handler import BaseHandler, HandlerResult


class HandlerRegistry:
    """Maps IntentType values to concrete handler instances."""

    def __init__(self):
        self._handlers: dict[IntentType, BaseHandler] = {}

    def register(self, intent: IntentType, handler: BaseHandler):
        """Register a handler for a specific intent type."""
        self._handlers[intent] = handler

    def get_handler(self, intent: IntentType) -> Optional[BaseHandler]:
        """Look up the handler for an intent type, or None if unregistered."""
        return self._handlers.get(intent)

    async def dispatch(
        self,
        intent_result: IntentResult,
        message: str,
        context: ConversationContext,
        capabilities: dict,
        db_session=None,
    ) -> Optional[HandlerResult]:
        """Route a classified intent to its registered handler."""
        handler = self.get_handler(intent_result.intent)
        if handler:
            return await handler.handle(
                message, intent_result, context, capabilities, db_session
            )
        return None


# Global instance - handlers are registered after import
handler_registry = HandlerRegistry()
