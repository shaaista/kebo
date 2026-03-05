from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConversationState(str, Enum):
    """Conversation state machine states."""
    IDLE = "idle"                           # Waiting for user input
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # Asked yes/no question
    AWAITING_SELECTION = "awaiting_selection"        # Presented options
    AWAITING_INFO = "awaiting_info"         # Asked for specific info (room number, etc.)
    PROCESSING_ORDER = "processing_order"   # Order in progress
    ESCALATED = "escalated"                 # Handed off to human
    COMPLETED = "completed"                 # Task completed


class IntentType(str, Enum):
    """Recognized intent types."""
    GREETING = "greeting"
    MENU_REQUEST = "menu_request"
    ORDER_FOOD = "order_food"
    ORDER_STATUS = "order_status"
    TABLE_BOOKING = "table_booking"
    ROOM_SERVICE = "room_service"
    HEALTH_SUPPORT = "health_support"
    COMPLAINT = "complaint"
    FAQ = "faq"
    CONFIRMATION_YES = "confirmation_yes"
    CONFIRMATION_NO = "confirmation_no"
    HUMAN_REQUEST = "human_request"
    UNCLEAR = "unclear"
    OUT_OF_SCOPE = "out_of_scope"


class Message(BaseModel):
    """Single message in conversation."""
    id: UUID = Field(default_factory=uuid4)
    role: MessageRole
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """Incoming chat request."""
    session_id: str = Field(..., description="Unique session identifier")
    message: str = Field(..., min_length=1, max_length=2000)
    hotel_code: str = Field(default="DEFAULT")
    guest_phone: str | None = Field(default=None)
    channel: str | None = Field(default=None, description="Message source, e.g. web or whatsapp")
    metadata: dict[str, Any] = Field(default_factory=dict)


class IntentResult(BaseModel):
    """Result of intent classification."""
    intent: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    entities: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False


class ChatResponse(BaseModel):
    """Outgoing chat response."""
    session_id: str
    message: str
    intent: IntentType | None = None
    confidence: float | None = None
    state: ConversationState
    suggested_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationContext(BaseModel):
    """Full conversation context."""
    session_id: str
    hotel_code: str
    guest_phone: str | None = None
    guest_name: str | None = None
    room_number: str | None = None
    channel: str = "web"

    state: ConversationState = ConversationState.IDLE
    pending_action: str | None = None
    pending_data: dict[str, Any] = Field(default_factory=dict)

    messages: list[Message] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def add_message(self, role: MessageRole, content: str, metadata: dict | None = None) -> Message:
        """Add a message to conversation history."""
        msg = Message(role=role, content=content, metadata=metadata or {})
        self.messages.append(msg)
        self.updated_at = datetime.now(UTC)
        return msg

    def get_recent_messages(self, count: int = 10) -> list[Message]:
        """Get recent messages for context."""
        return self.messages[-count:] if self.messages else []

    def to_llm_messages(self, count: int = 10) -> list[dict]:
        """Convert to LLM message format."""
        return [
            {"role": msg.role.value, "content": msg.content}
            for msg in self.get_recent_messages(count)
        ]
