"""
Async database layer for KePSLA Bot v2 (MySQL).

Defines:
- Async engine + session factory
- Base model class
- ORM models that match manual tables (prefix new_bot_)

NOTE: All tables use INT AUTO_INCREMENT for IDs (not UUID).
This makes debugging easier and matches recreate_tables.py structure.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.mysql import DECIMAL
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship

from config.settings import settings

# Legacy fallback for local development; primary source should be settings.database_url.
LEGACY_FALLBACK_DATABASE_URL = "mysql+aiomysql://root:zapcom123@172.16.5.32:3306/GHN_PROD_BAK"
ACTIVE_DATABASE_URL = str(getattr(settings, "database_url", "") or "").strip() or LEGACY_FALLBACK_DATABASE_URL


def _build_connect_args(database_url: str) -> dict[str, Any]:
    """
    Build driver-safe connect args.

    - MySQL + aiomysql: supports `connect_timeout`
    - PostgreSQL + asyncpg: supports `timeout`
    - SQLite + aiosqlite: should not receive `connect_timeout`
    """
    try:
        parsed = make_url(database_url)
        backend = str(parsed.get_backend_name() or "").lower()
        driver = str(parsed.get_driver_name() or "").lower()
    except Exception:
        return {}

    if backend == "mysql" and "aiomysql" in driver:
        return {"connect_timeout": 30}
    if backend in {"postgresql", "postgres"} and "asyncpg" in driver:
        return {"timeout": 30}
    return {}


Base = declarative_base()


class TimestampMixin:
    """created_at / updated_at columns with server defaults."""

    created_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )


class Hotel(Base, TimestampMixin):
    __tablename__ = "new_bot_hotels"
    __table_args__ = (
        UniqueConstraint("code", name="uq_hotels_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    city = Column(String(100), nullable=False)
    timezone = Column(String(50), server_default=text("'Asia/Kolkata'"))
    is_active = Column(Boolean, nullable=False, server_default=text("1"))

    restaurants = relationship(
        "Restaurant",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    guests = relationship(
        "Guest",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    conversations = relationship(
        "Conversation",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    # Config relationships
    business_configs = relationship(
        "BusinessConfig",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    capabilities = relationship(
        "Capability",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    intents = relationship(
        "Intent",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )


class Restaurant(Base, TimestampMixin):
    __tablename__ = "new_bot_restaurants"
    __table_args__ = (
        UniqueConstraint("hotel_id", "code", name="uq_rest"),
        Index("idx_restaurants_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    code = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    cuisine = Column(String(100), nullable=True)
    opens_at = Column(Time, nullable=True)
    closes_at = Column(Time, nullable=True)
    delivers_to_room = Column(Boolean, nullable=False, server_default=text("0"))
    is_active = Column(Boolean, nullable=False, server_default=text("1"))

    hotel = relationship("Hotel", back_populates="restaurants")
    menu_items = relationship(
        "MenuItem",
        back_populates="restaurant",
        cascade="all, delete-orphan",
    )
    orders = relationship("Order", back_populates="restaurant")


class MenuItem(Base, TimestampMixin):
    __tablename__ = "new_bot_menu_items"
    __table_args__ = (
        Index("idx_menu_items_restaurant_id", "restaurant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    restaurant_id = Column(
        Integer,
        ForeignKey("new_bot_restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(DECIMAL(10, 2, asdecimal=True), nullable=False, server_default=text("0.00"))
    currency = Column(String(3), nullable=False, server_default=text("'INR'"))
    category = Column(String(100), nullable=True)
    is_vegetarian = Column(Boolean, nullable=False, server_default=text("0"))
    is_available = Column(Boolean, nullable=False, server_default=text("1"))

    restaurant = relationship("Restaurant", back_populates="menu_items")
    order_items = relationship("OrderItem", back_populates="menu_item")


class Guest(Base, TimestampMixin):
    __tablename__ = "new_bot_guests"
    __table_args__ = (
        UniqueConstraint("hotel_id", "phone_number", name="uq_guest"),
        Index("idx_guests_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    phone_number = Column(String(30), nullable=False)
    name = Column(String(255), nullable=True)
    room_number = Column(String(20), nullable=True)
    check_in_date = Column(Date, nullable=True)
    check_out_date = Column(Date, nullable=True)

    hotel = relationship("Hotel", back_populates="guests")
    orders = relationship(
        "Order",
        back_populates="guest",
        cascade="all, delete-orphan",
    )
    conversations = relationship("Conversation", back_populates="guest")


class Order(Base, TimestampMixin):
    __tablename__ = "new_bot_orders"
    __table_args__ = (
        Index("idx_orders_guest_id", "guest_id"),
        Index("idx_orders_restaurant_id", "restaurant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    guest_id = Column(
        Integer,
        ForeignKey("new_bot_guests.id"),
        nullable=False,
    )
    restaurant_id = Column(
        Integer,
        ForeignKey("new_bot_restaurants.id"),
        nullable=False,
    )
    status = Column(String(50), nullable=False, server_default=text("'pending'"))
    total_amount = Column(DECIMAL(10, 2, asdecimal=True), nullable=False, server_default=text("0.00"))
    delivery_location = Column(String(100), nullable=True)

    guest = relationship("Guest", back_populates="orders")
    restaurant = relationship("Restaurant", back_populates="orders")
    order_items = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderItem(Base):
    __tablename__ = "new_bot_order_items"
    __table_args__ = (
        Index("idx_order_items_order_id", "order_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        Integer,
        ForeignKey("new_bot_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    menu_item_id = Column(
        Integer,
        ForeignKey("new_bot_menu_items.id"),
        nullable=False,
    )
    quantity = Column(Integer, nullable=False, server_default=text("1"))
    unit_price = Column(DECIMAL(10, 2, asdecimal=True), nullable=False, server_default=text("0.00"))

    order = relationship("Order", back_populates="order_items")
    menu_item = relationship("MenuItem", back_populates="order_items")


class Conversation(Base, TimestampMixin):
    __tablename__ = "new_bot_conversations"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session"),
        Index("idx_conversations_hotel_id", "hotel_id"),
        Index("idx_conversations_guest_id", "guest_id"),
        Index("idx_conversations_hotel_state", "hotel_id", "state"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    guest_id = Column(
        Integer,
        ForeignKey("new_bot_guests.id", ondelete="SET NULL"),
        nullable=True,
    )
    state = Column(String(50), nullable=False, server_default=text("'idle'"))
    pending_action = Column(String(100), nullable=True)
    pending_data = Column(JSON, nullable=True)
    channel = Column(String(20), nullable=False, server_default=text("'web'"))

    hotel = relationship("Hotel", back_populates="conversations")
    guest = relationship("Guest", back_populates="conversations")
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class Message(Base):
    __tablename__ = "new_bot_messages"
    __table_args__ = (
        Index("idx_messages_conversation_id", "conversation_id"),
        Index("idx_messages_conv_created", "conversation_id", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer,
        ForeignKey("new_bot_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    intent = Column(String(100), nullable=True)
    confidence = Column(Float, nullable=True)
    channel = Column(String(20), nullable=False, server_default=text("'web'"))
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    conversation = relationship("Conversation", back_populates="messages")


class BusinessConfig(Base, TimestampMixin):
    """Stores business configuration in database."""
    __tablename__ = "new_bot_business_config"
    __table_args__ = (
        UniqueConstraint("hotel_id", "config_key", name="uq_config"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    config_key = Column(String(100), nullable=False)  # e.g., 'business.name', 'business.welcome_message'
    config_value = Column(Text, nullable=True)  # JSON-encoded value

    hotel = relationship("Hotel", back_populates="business_configs")


class Capability(Base, TimestampMixin):
    """Stores capabilities per hotel in database."""
    __tablename__ = "new_bot_capabilities"
    __table_args__ = (
        UniqueConstraint("hotel_id", "capability_id", name="uq_cap"),
        Index("idx_capabilities_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability_id = Column(String(100), nullable=False)  # e.g., 'food_ordering', 'room_service'
    description = Column(String(255), nullable=True)
    enabled = Column(Boolean, nullable=False, server_default=text("1"))
    hours = Column(String(100), nullable=True)  # e.g., '24/7' or '9 AM - 9 PM'

    hotel = relationship("Hotel", back_populates="capabilities")


class Intent(Base, TimestampMixin):
    """Stores intents per hotel in database."""
    __tablename__ = "new_bot_intents"
    __table_args__ = (
        UniqueConstraint("hotel_id", "intent_id", name="uq_intent"),
        Index("idx_intents_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    intent_id = Column(String(100), nullable=False)  # e.g., 'greeting', 'order_food'
    label = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, server_default=text("1"))

    hotel = relationship("Hotel", back_populates="intents")


# Async engine + session factory
_engine_connect_args = _build_connect_args(ACTIVE_DATABASE_URL)
_engine_kwargs: dict[str, Any] = {
    "echo": settings.database_echo,
    "pool_pre_ping": True,
}
if _engine_connect_args:
    _engine_kwargs["connect_args"] = _engine_connect_args

engine = create_async_engine(
    ACTIVE_DATABASE_URL,
    **_engine_kwargs,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides an async DB session."""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Initialize database tables (safe if tables already exist)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
