"""
Intent Handlers Package

Registers all handlers with the handler_registry on import.
"""

from handlers.base_handler import BaseHandler, HandlerResult
from handlers.registry import handler_registry
from handlers.greeting_handler import GreetingHandler
from handlers.faq_handler import FAQHandler
from handlers.order_handler import OrderHandler
from handlers.escalation_handler import EscalationHandler
from handlers.booking_handler import BookingHandler
from handlers.complaint_handler import ComplaintHandler
from handlers.transport_handler import TransportHandler
from handlers.room_service_handler import RoomServiceHandler
from handlers.health_support_handler import HealthSupportHandler

from schemas.chat import IntentType

# Register all handlers with the registry
handler_registry.register(IntentType.GREETING, GreetingHandler())
# Knowledge/catalog-style questions are handled through FAQ/RAG flow.
handler_registry.register(IntentType.MENU_REQUEST, FAQHandler())
handler_registry.register(IntentType.ORDER_FOOD, OrderHandler())
handler_registry.register(IntentType.TABLE_BOOKING, BookingHandler())
handler_registry.register(IntentType.ROOM_SERVICE, RoomServiceHandler())
handler_registry.register(IntentType.HEALTH_SUPPORT, HealthSupportHandler())
handler_registry.register(IntentType.COMPLAINT, ComplaintHandler())
handler_registry.register(IntentType.HUMAN_REQUEST, EscalationHandler())
handler_registry.register(IntentType.FAQ, FAQHandler())
# CONFIRMATION_YES/NO are handled contextually by the pending_action's handler
# ORDER_STATUS, UNCLEAR, OUT_OF_SCOPE fall through to LLM generation

__all__ = [
    "BaseHandler",
    "HandlerResult",
    "handler_registry",
    "GreetingHandler",
    "FAQHandler",
    "OrderHandler",
    "EscalationHandler",
    "BookingHandler",
    "ComplaintHandler",
    "TransportHandler",
    "RoomServiceHandler",
    "HealthSupportHandler",
]
