"""
Async database layer for NexOria (MySQL).

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
        return {"timeout": 30, "server_settings": {"search_path": "guest_chatbot"}}
    if backend == "sqlite":
        # timeout=30 makes SQLite wait up to 30s for a write lock instead of
        # immediately raising "database is locked" under concurrent load.
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
    bot_services = relationship(
        "BotService",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    kb_files = relationship(
        "KBFile",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    image_assets = relationship(
        "ImageAsset",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    bookings = relationship(
        "Booking",
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
    bookings = relationship(
        "Booking",
        back_populates="guest",
        cascade="all, delete-orphan",
    )
    orders = relationship(
        "Order",
        back_populates="guest",
        cascade="all, delete-orphan",
    )
    conversations = relationship("Conversation", back_populates="guest")


class Booking(Base, TimestampMixin):
    """Stores individual guest bookings/stays. Separate from Guest to support
    repeat visits and multi-property stays."""
    __tablename__ = "new_bot_bookings"
    __table_args__ = (
        UniqueConstraint("confirmation_code", name="uq_booking_code"),
        Index("idx_bookings_hotel_id", "hotel_id"),
        Index("idx_bookings_guest_id", "guest_id"),
        Index("idx_bookings_hotel_status", "hotel_id", "status"),
        Index("idx_bookings_dates", "hotel_id", "check_in_date", "check_out_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    guest_id = Column(
        Integer,
        ForeignKey("new_bot_guests.id", ondelete="CASCADE"),
        nullable=False,
    )
    confirmation_code = Column(String(30), nullable=False)
    property_name = Column(String(255), nullable=True)
    room_number = Column(String(20), nullable=True)
    room_type = Column(String(100), nullable=True)
    check_in_date = Column(Date, nullable=False)
    check_out_date = Column(Date, nullable=False)
    num_guests = Column(Integer, nullable=True, server_default=text("1"))
    status = Column(String(20), nullable=False, server_default=text("'reserved'"))
    source_channel = Column(String(20), nullable=True)
    special_requests = Column(Text, nullable=True)

    hotel = relationship("Hotel", back_populates="bookings")
    guest = relationship("Guest", back_populates="bookings")
    conversations = relationship("Conversation", back_populates="booking")


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
    booking_id = Column(
        Integer,
        ForeignKey("new_bot_bookings.id", ondelete="SET NULL"),
        nullable=True,
    )
    state = Column(String(50), nullable=False, server_default=text("'idle'"))
    pending_action = Column(String(100), nullable=True)
    pending_data = Column(JSON, nullable=True)
    channel = Column(String(20), nullable=False, server_default=text("'web'"))

    hotel = relationship("Hotel", back_populates="conversations")
    guest = relationship("Guest", back_populates="conversations")
    booking = relationship("Booking", back_populates="conversations")
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
    config_value = Column(Text(length=4294967295), nullable=True)  # LONGTEXT JSON payload

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


class BotService(Base, TimestampMixin):
    """Stores all service configuration per hotel. Primary persistent store."""
    __tablename__ = "new_bot_services"
    __table_args__ = (
        UniqueConstraint("hotel_id", "service_id", name="uq_bot_service"),
        Index("idx_bot_services_hotel_id", "hotel_id"),
        Index("idx_bot_services_phase_id", "hotel_id", "phase_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    service_id = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)
    type = Column(String(50), nullable=False, server_default=text("'service'"))
    description = Column(Text, nullable=True)
    phase_id = Column(String(50), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("1"))
    is_builtin = Column(Boolean, nullable=False, server_default=text("0"))
    ticketing_enabled = Column(Boolean, nullable=False, server_default=text("1"))
    ticketing_mode = Column(String(20), nullable=True)
    ticketing_policy = Column(Text, nullable=True)
    form_config = Column(JSON, nullable=True)
    service_prompt_pack = Column(JSON, nullable=True)
    generated_system_prompt = Column(Text, nullable=True)
    generated_system_prompt_override = Column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )

    hotel = relationship("Hotel", back_populates="bot_services")


class PromptRegistry(Base, TimestampMixin):
    """
    DB-backed registry for every customizable prompt.

    Scope:
      - industry-default row: industry set, hotel_id NULL  (one per supported industry)
      - hotel override row:   hotel_id set                  (industry may be NULL)

    Resolution order (see PromptRegistryService.get):
      hotel override  ->  industry default  ->  raise PromptMissingError
    """
    __tablename__ = "new_bot_prompt_registry"
    __table_args__ = (
        UniqueConstraint("prompt_key", "industry", "hotel_id", name="uq_prompt_scope"),
        Index("idx_prompt_registry_key", "prompt_key"),
        Index("idx_prompt_registry_hotel", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    prompt_key = Column(String(128), nullable=False)
    industry = Column(String(32), nullable=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=True,
    )
    content = Column(Text(length=4294967295), nullable=False)  # LONGTEXT
    variables = Column(JSON, nullable=True)
    version = Column(Integer, nullable=False, server_default=text("1"))
    description = Column(Text, nullable=True)
    updated_by = Column(String(64), nullable=True)
    seeded_from_file_hash = Column(String(64), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("1"))


class KBFile(Base, TimestampMixin):
    """Stores uploaded knowledge base file content in the database for persistence."""
    __tablename__ = "new_bot_kb_files"
    __table_args__ = (
        UniqueConstraint("hotel_id", "stored_name", name="uq_kb_file"),
        Index("idx_kb_files_hotel_id", "hotel_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    original_name = Column(String(255), nullable=False)
    stored_name = Column(String(255), nullable=False)
    content = Column(Text(length=4294967295), nullable=False)  # LONGTEXT
    content_hash = Column(String(64), nullable=True)

    hotel = relationship("Hotel", back_populates="kb_files")


class ImageAsset(Base, TimestampMixin):
    """Stores curated image assets per hotel for LLM-selected media responses."""
    __tablename__ = "bot_hotel_images_scraper"
    __table_args__ = (
        Index("idx_image_assets_hotel_id", "hotel_id"),
        Index("idx_image_assets_hotel_active", "hotel_id", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel_id = Column(
        Integer,
        ForeignKey("new_bot_hotels.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    image_url = Column(Text, nullable=False)
    category = Column(String(120), nullable=True)
    tags = Column(JSON, nullable=True)
    source_label = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("1"))
    priority = Column(Integer, nullable=False, server_default=text("0"))

    hotel = relationship("Hotel", back_populates="image_assets")


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

# Enable WAL mode for SQLite so concurrent reads don't block writes.
try:
    _parsed_url = make_url(ACTIVE_DATABASE_URL)
    if str(_parsed_url.get_backend_name() or "").lower() == "sqlite":
        from sqlalchemy import event as _sa_event

        @_sa_event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_wal(dbapi_conn, connection_record):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=30000")
except Exception:
    pass

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides an async DB session."""
    async with AsyncSessionLocal() as session:
        yield session


async def _column_exists(conn, table: str, column: str) -> bool:
    """Check whether a column exists on the active backend."""
    backend = str(getattr(conn.dialect, "name", "") or "").lower()

    if backend == "mysql":
        result = await conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                  AND column_name = :column_name
                LIMIT 1
                """
            ),
            {"table_name": table, "column_name": column},
        )
        return result.scalar_one_or_none() is not None

    if backend in {"postgresql", "postgres"}:
        result = await conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = :table_name
                  AND column_name = :column_name
                LIMIT 1
                """
            ),
            {"table_name": table, "column_name": column},
        )
        return result.scalar_one_or_none() is not None

    if backend == "sqlite":
        # Table/column names come from a static migration list below.
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        for row in result:
            row_name = str(getattr(row, "_mapping", {}).get("name", row[1]) or "")
            if row_name.lower() == column.lower():
                return True
        return False

    try:
        await conn.execute(text(f"SELECT {column} FROM {table} WHERE 1=0"))
        return True
    except Exception:
        return False


async def _mysql_data_type(conn, table: str, column: str) -> str:
    """Return MySQL DATA_TYPE for a given column (empty when unavailable)."""
    result = await conn.execute(
        text(
            """
            SELECT DATA_TYPE
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
              AND column_name = :column_name
            LIMIT 1
            """
        ),
        {"table_name": table, "column_name": column},
    )
    value = result.scalar_one_or_none()
    return str(value or "").strip().lower()


async def init_db() -> None:
    """Initialize database tables (safe if tables already exist)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Add columns that may be missing on older deployments.
    # This is intentionally best-effort and safe to run repeatedly.
    _new_columns = [
        ("new_bot_hotels", "timezone", "VARCHAR(50) NULL DEFAULT 'Asia/Kolkata'"),
        ("new_bot_hotels", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
        ("new_bot_services", "type", "VARCHAR(50) NOT NULL DEFAULT 'service'"),
        ("new_bot_services", "description", "TEXT NULL"),
        ("new_bot_services", "phase_id", "VARCHAR(50) NULL"),
        ("new_bot_services", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
        ("new_bot_services", "is_builtin", "BOOLEAN NOT NULL DEFAULT 0"),
        ("new_bot_services", "ticketing_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
        ("new_bot_services", "ticketing_policy", "TEXT NULL"),
        ("new_bot_services", "service_prompt_pack", "JSON NULL"),
        ("new_bot_services", "generated_system_prompt", "TEXT NULL"),
        ("new_bot_services", "ticketing_mode", "VARCHAR(20) NULL"),
        ("new_bot_services", "form_config", "JSON NULL"),
        ("new_bot_services", "generated_system_prompt_override", "BOOLEAN NOT NULL DEFAULT 0"),
        ("new_bot_kb_files", "content_hash", "VARCHAR(64) NULL"),
        ("new_bot_business_config", "created_at", "DATETIME NULL DEFAULT CURRENT_TIMESTAMP"),
        (
            "new_bot_business_config",
            "updated_at",
            "DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
        ("new_bot_conversations", "booking_id", "INT NULL"),
        ("bot_hotel_images_scraper", "description", "TEXT NULL"),
    ]
    async with engine.begin() as conn:
        for table, col, col_type in _new_columns:
            try:
                if await _column_exists(conn, table, col):
                    continue
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except Exception:
                # Best effort: skip when backend doesn't support the DDL or migration races.
                pass

    # MySQL-only longtext widening (skip when already LONGTEXT).
    try:
        parsed = make_url(ACTIVE_DATABASE_URL)
        backend = str(parsed.get_backend_name() or "").lower()
    except Exception:
        backend = ""

    if backend == "mysql":
        try:
            async with engine.begin() as conn:
                if await _mysql_data_type(conn, "new_bot_business_config", "config_value") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_business_config MODIFY COLUMN config_value LONGTEXT NULL")
                    )
        except Exception:
            pass

        try:
            async with engine.begin() as conn:
                if await _mysql_data_type(conn, "new_bot_kb_files", "content") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_kb_files MODIFY COLUMN content LONGTEXT NOT NULL")
                    )
                if await _mysql_data_type(conn, "new_bot_services", "generated_system_prompt") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_services MODIFY COLUMN generated_system_prompt LONGTEXT NULL")
                    )
                if await _mysql_data_type(conn, "new_bot_services", "ticketing_policy") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_services MODIFY COLUMN ticketing_policy LONGTEXT NULL")
                    )
                if await _mysql_data_type(conn, "new_bot_prompt_registry", "content") != "longtext":
                    await conn.execute(
                        text("ALTER TABLE new_bot_prompt_registry MODIFY COLUMN content LONGTEXT NOT NULL")
                    )
        except Exception:
            pass
