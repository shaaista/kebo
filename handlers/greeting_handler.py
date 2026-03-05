"""
Greeting Handler

Returns the configured welcome message and suggests common actions
the guest can take (view menu, order food, book a table, room service).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from handlers.base_handler import BaseHandler, HandlerResult
from schemas.chat import ConversationState, IntentResult, ConversationContext
from services.config_service import config_service


class GreetingHandler(BaseHandler):
    """Handle greeting intents by returning the hotel's welcome message."""

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Optional[AsyncSession] = None,
    ) -> HandlerResult:
        welcome_message = config_service.get_welcome_message()

        return HandlerResult(
            response_text=welcome_message,
            next_state=ConversationState.IDLE,
            suggested_actions=[
                "Show menu",
                "Order food",
                "Book a table",
                "Room service",
            ],
        )
