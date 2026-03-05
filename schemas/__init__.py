# Pydantic schemas for validation
from .chat import ChatRequest, ChatResponse, IntentResult
from .admin_schemas import (
    HotelCreate,
    HotelUpdate,
    HotelResponse,
    RestaurantCreate,
    RestaurantUpdate,
    RestaurantResponse,
    MenuItemCreate,
    MenuItemUpdate,
    MenuItemResponse,
    GuestCreate,
    GuestUpdate,
    GuestResponse,
    OrderCreate,
    OrderUpdate,
    OrderResponse,
    OrderItemCreate,
    OrderItemResponse,
    ConversationCreate,
    ConversationUpdate,
    ConversationResponse,
    MessageCreate,
    MessageResponse,
    BulkMenuItemCreate,
    BulkMenuItemResponse,
)

__all__ = [
    # Chat
    "ChatRequest",
    "ChatResponse",
    "IntentResult",
    # Hotel
    "HotelCreate",
    "HotelUpdate",
    "HotelResponse",
    # Restaurant
    "RestaurantCreate",
    "RestaurantUpdate",
    "RestaurantResponse",
    # MenuItem
    "MenuItemCreate",
    "MenuItemUpdate",
    "MenuItemResponse",
    # Guest
    "GuestCreate",
    "GuestUpdate",
    "GuestResponse",
    # Order
    "OrderCreate",
    "OrderUpdate",
    "OrderResponse",
    "OrderItemCreate",
    "OrderItemResponse",
    # Conversation
    "ConversationCreate",
    "ConversationUpdate",
    "ConversationResponse",
    "MessageCreate",
    "MessageResponse",
    # Bulk
    "BulkMenuItemCreate",
    "BulkMenuItemResponse",
]
