"""
Admin/API schemas for NexOria.

Pydantic v2 compatible with SQLAlchemy ORM objects.
All IDs are int (matching INT AUTO_INCREMENT in MySQL).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, Literal

from pydantic import BaseModel, Field, ConfigDict


class ORMBase(BaseModel):
    """Base schema with ORM support."""

    model_config = ConfigDict(from_attributes=True)


# ===== Hotel Schemas =====

class HotelCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    city: str = Field(..., min_length=1, max_length=128)
    timezone: Optional[str] = Field(default=None, max_length=64)
    is_active: bool = True


class HotelUpdate(BaseModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    city: Optional[str] = Field(default=None, min_length=1, max_length=128)
    timezone: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None


class HotelResponse(ORMBase):
    id: int
    code: str
    name: str
    city: str
    timezone: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ===== Restaurant Schemas =====

class RestaurantCreate(BaseModel):
    hotel_id: int
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    cuisine: Optional[str] = Field(default=None, max_length=128)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    delivers_to_room: bool = False
    is_active: bool = True


class RestaurantUpdate(BaseModel):
    hotel_id: Optional[int] = None
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    cuisine: Optional[str] = Field(default=None, max_length=128)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    delivers_to_room: Optional[bool] = None
    is_active: Optional[bool] = None


class RestaurantResponse(ORMBase):
    id: int
    hotel_id: int
    code: str
    name: str
    cuisine: Optional[str] = None
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    delivers_to_room: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ===== Menu Item Schemas =====

class MenuItemCreate(BaseModel):
    restaurant_id: int
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    price: float = Field(..., ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    category: Optional[str] = Field(default=None, max_length=128)
    is_vegetarian: bool = False
    is_available: bool = True


class MenuItemUpdate(BaseModel):
    restaurant_id: Optional[int] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    category: Optional[str] = Field(default=None, max_length=128)
    is_vegetarian: Optional[bool] = None
    is_available: Optional[bool] = None


class MenuItemResponse(ORMBase):
    id: int
    restaurant_id: int
    name: str
    description: Optional[str] = None
    price: float
    currency: str
    category: Optional[str] = None
    is_vegetarian: bool
    is_available: bool
    created_at: datetime
    updated_at: datetime


# ===== Guest Schemas =====

class GuestCreate(BaseModel):
    hotel_id: int
    phone_number: Optional[str] = Field(default=None, max_length=32)
    name: Optional[str] = Field(default=None, max_length=255)
    room_number: Optional[str] = Field(default=None, max_length=32)
    check_in_date: Optional[date] = None
    check_out_date: Optional[date] = None


class GuestUpdate(BaseModel):
    hotel_id: Optional[int] = None
    phone_number: Optional[str] = Field(default=None, max_length=32)
    name: Optional[str] = Field(default=None, max_length=255)
    room_number: Optional[str] = Field(default=None, max_length=32)
    check_in_date: Optional[date] = None
    check_out_date: Optional[date] = None


class GuestResponse(ORMBase):
    id: int
    hotel_id: int
    phone_number: Optional[str] = None
    name: Optional[str] = None
    room_number: Optional[str] = None
    check_in_date: Optional[date] = None
    check_out_date: Optional[date] = None
    created_at: datetime
    updated_at: datetime


# ===== Order Schemas =====

OrderStatus = Literal["pending", "confirmed", "in_progress", "delivered", "cancelled"]


class OrderCreate(BaseModel):
    guest_id: int
    restaurant_id: int
    status: OrderStatus = "pending"
    total_amount: float = Field(..., ge=0)
    delivery_location: Optional[str] = Field(default=None, max_length=64)


class OrderUpdate(BaseModel):
    guest_id: Optional[int] = None
    restaurant_id: Optional[int] = None
    status: Optional[OrderStatus] = None
    total_amount: Optional[float] = Field(default=None, ge=0)
    delivery_location: Optional[str] = Field(default=None, max_length=64)


class OrderResponse(ORMBase):
    id: int
    guest_id: int
    restaurant_id: int
    status: Optional[OrderStatus] = None
    total_amount: float
    delivery_location: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ===== Order Item Schemas =====

class OrderItemCreate(BaseModel):
    order_id: int
    menu_item_id: int
    quantity: int = Field(..., ge=1)
    unit_price: float = Field(..., ge=0)


class OrderItemUpdate(BaseModel):
    order_id: Optional[int] = None
    menu_item_id: Optional[int] = None
    quantity: Optional[int] = Field(default=None, ge=1)
    unit_price: Optional[float] = Field(default=None, ge=0)


class OrderItemResponse(ORMBase):
    id: int
    order_id: int
    menu_item_id: int
    quantity: int
    unit_price: float


# ===== Conversation Schemas =====

ConversationState = Literal[
    "idle",
    "awaiting_confirmation",
    "awaiting_selection",
    "awaiting_info",
    "processing_order",
    "escalated",
    "completed",
]


class ConversationCreate(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=255)
    hotel_id: int
    guest_id: Optional[int] = None
    state: ConversationState = "idle"


class ConversationUpdate(BaseModel):
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=255)
    hotel_id: Optional[int] = None
    guest_id: Optional[int] = None
    state: Optional[ConversationState] = None


class ConversationResponse(ORMBase):
    id: int
    session_id: str
    hotel_id: int
    guest_id: Optional[int] = None
    state: Optional[ConversationState] = None
    created_at: datetime
    updated_at: datetime


# ===== Message Schemas =====

MessageRole = Literal["user", "assistant", "system"]


class MessageCreate(BaseModel):
    conversation_id: int
    role: MessageRole
    content: str = Field(..., min_length=1)
    intent: Optional[str] = Field(default=None, max_length=64)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)


class MessageUpdate(BaseModel):
    conversation_id: Optional[int] = None
    role: Optional[MessageRole] = None
    content: Optional[str] = Field(default=None, min_length=1)
    intent: Optional[str] = Field(default=None, max_length=64)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)


class MessageResponse(ORMBase):
    id: int
    conversation_id: int
    role: MessageRole
    content: str
    intent: Optional[str] = None
    confidence: Optional[float] = None
    created_at: datetime


# ===== Bulk Operation Schemas =====

class BulkMenuItemEntry(BaseModel):
    """Single menu item entry for bulk creation (no restaurant_id needed)."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    price: float = Field(..., ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    category: Optional[str] = Field(default=None, max_length=128)
    is_vegetarian: bool = False
    is_available: bool = True


class BulkMenuItemCreate(BaseModel):
    """Schema for creating multiple menu items at once."""
    restaurant_id: int
    items: list[BulkMenuItemEntry]


class BulkMenuItemResponse(BaseModel):
    """Response for bulk menu item creation."""
    created_count: int
    items: list[MenuItemResponse]
