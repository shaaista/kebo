import pytest
from sqlalchemy import select

from core.context_manager import ContextManager
from models.database import Conversation as DBConversation
from models.database import Message as DBMessage
from schemas.chat import ConversationState, MessageRole


@pytest.mark.asyncio
async def test_context_persists_pending_and_channel(async_session):
    manager = ContextManager()

    context = await manager.create_context(
        session_id="session_ctx_1",
        hotel_code="DEFAULT",
        guest_phone="+15550001111",
        channel="whatsapp",
        db_session=async_session,
    )
    context.state = ConversationState.AWAITING_CONFIRMATION
    context.pending_action = "confirm_order"
    context.pending_data = {"items": ["pizza"], "quantity": 1}
    context.add_message(
        MessageRole.USER,
        "I want pizza",
        metadata={"channel": "whatsapp"},
    )
    context.add_message(
        MessageRole.ASSISTANT,
        "Please confirm your order",
        metadata={
            "intent": "order_food",
            "confidence": 0.91,
            "channel": "whatsapp",
        },
    )
    await manager.save_context(context, db_session=async_session)

    # Simulate process restart: load through a new manager instance.
    fresh_manager = ContextManager()
    loaded = await fresh_manager.get_context("session_ctx_1", db_session=async_session)

    assert loaded is not None
    assert loaded.channel == "whatsapp"
    assert loaded.state == ConversationState.AWAITING_CONFIRMATION
    assert loaded.pending_action == "confirm_order"
    assert loaded.pending_data == {"items": ["pizza"], "quantity": 1}
    assert len(loaded.messages) == 2
    assert loaded.messages[1].metadata.get("intent") == "order_food"
    assert loaded.messages[1].metadata.get("channel") == "whatsapp"

    conv_row = (
        await async_session.execute(
            select(DBConversation).where(DBConversation.session_id == "session_ctx_1")
        )
    ).scalar_one()
    assert conv_row.channel == "whatsapp"
    assert conv_row.pending_action == "confirm_order"
    assert conv_row.pending_data == {"items": ["pizza"], "quantity": 1}

    msg_rows = (
        await async_session.execute(
            select(DBMessage)
            .where(DBMessage.conversation_id == conv_row.id)
            .order_by(DBMessage.id.asc())
        )
    ).scalars().all()
    assert len(msg_rows) == 2
    assert msg_rows[0].channel == "whatsapp"
    assert msg_rows[1].channel == "whatsapp"


@pytest.mark.asyncio
async def test_update_state_allows_clearing_pending_data(async_session):
    manager = ContextManager()
    await manager.create_context(
        session_id="session_ctx_2",
        hotel_code="DEFAULT",
        db_session=async_session,
    )

    await manager.update_state(
        session_id="session_ctx_2",
        state=ConversationState.AWAITING_INFO,
        pending_action="collect_more",
        pending_data={"field": "room_number"},
        db_session=async_session,
    )

    cleared = await manager.update_state(
        session_id="session_ctx_2",
        state=ConversationState.IDLE,
        pending_action=None,
        pending_data={},
        db_session=async_session,
    )

    assert cleared is not None
    assert cleared.pending_data == {}
