"""
Conversation Context Manager

Handles storing, retrieving, and managing conversation context.
Uses in-memory storage plus optional DB persistence.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from config.settings import settings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation as DBConversation
from models.database import Guest, Hotel, Message as DBMessage
from schemas.chat import (
    ConversationContext,
    ConversationState,
    Message as ChatMessage,
    MessageRole,
)

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages conversation contexts across sessions."""

    def __init__(
        self,
        ttl_hours: int = 24,
        local_store_enabled: Optional[bool] = None,
        local_store_file: Optional[str] = None,
    ):
        self.ttl_hours = ttl_hours
        # In-memory storage for development
        # Replace with Redis in production
        self._storage: Dict[str, dict] = {}
        self._expiry: Dict[str, datetime] = {}
        configured_local_enabled = bool(
            getattr(settings, "conversation_local_store_enabled", True)
        )
        self._local_store_enabled = (
            configured_local_enabled
            if local_store_enabled is None
            else bool(local_store_enabled)
        )
        configured_local_file = str(
            getattr(settings, "conversation_local_store_file", "") or ""
        ).strip()
        effective_local_file = str(local_store_file or "").strip() or configured_local_file
        if not effective_local_file:
            effective_local_file = "./data/runtime/local_contexts.json"
        self._local_store_path = Path(effective_local_file)

    async def get_context(
        self,
        session_id: str,
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[ConversationContext]:
        """Retrieve context for a session."""
        self._cleanup_expired()

        if session_id in self._storage:
            return ConversationContext(**self._storage[session_id])

        if db_session is None:
            local_context = self._load_context_from_local_store(session_id)
            if local_context is not None:
                self._save_to_memory(local_context, persist_local=False)
            return local_context

        context = await self._get_context_from_db(session_id, db_session)
        if context:
            self._save_to_memory(context)
        return context

    async def save_context(
        self,
        context: ConversationContext,
        db_session: Optional[AsyncSession] = None,
    ) -> None:
        """Save context for a session."""
        self._save_to_memory(context)
        if db_session is not None:
            await self._save_context_to_db(context, db_session)

    async def create_context(
        self,
        session_id: str,
        hotel_code: str,
        guest_phone: Optional[str] = None,
        channel: Optional[str] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> ConversationContext:
        """Create a new conversation context."""
        context = ConversationContext(
            session_id=session_id,
            hotel_code=hotel_code,
            guest_phone=guest_phone,
            channel=self._normalize_channel(channel),
        )
        await self.save_context(context, db_session=db_session)
        return context

    async def get_or_create_context(
        self,
        session_id: str,
        hotel_code: str,
        guest_phone: Optional[str] = None,
        channel: Optional[str] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> ConversationContext:
        """Get existing context or create new one."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            context = await self.create_context(
                session_id=session_id,
                hotel_code=hotel_code,
                guest_phone=guest_phone,
                channel=channel,
                db_session=db_session,
            )
        elif channel:
            context.channel = self._normalize_channel(channel)
        return context

    async def update_state(
        self,
        session_id: str,
        state: ConversationState,
        pending_action: Optional[str] = None,
        pending_data: Optional[dict] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[ConversationContext]:
        """Update conversation state."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            return None

        context.state = state
        context.pending_action = pending_action
        if pending_data is not None:
            context.pending_data = pending_data

        await self.save_context(context, db_session=db_session)
        return context

    async def add_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Optional[dict] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[ConversationContext]:
        """Add a message to conversation history."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            return None

        context.add_message(role, content, metadata)
        await self.save_context(context, db_session=db_session)
        return context

    async def delete_context(
        self,
        session_id: str,
        db_session: Optional[AsyncSession] = None,
    ) -> bool:
        """Delete a context."""
        deleted_memory = False
        if session_id in self._storage:
            del self._storage[session_id]
            if session_id in self._expiry:
                del self._expiry[session_id]
            deleted_memory = True

        deleted_db = False
        if db_session is not None:
            result = await db_session.execute(
                select(DBConversation).where(DBConversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()
            if conversation is not None:
                await db_session.delete(conversation)
                await db_session.commit()
                deleted_db = True

        deleted_local = self._remove_local_contexts([session_id])

        return deleted_memory or deleted_db or deleted_local

    async def list_sessions(self, db_session: Optional[AsyncSession] = None) -> list[str]:
        """List all active session IDs."""
        self._cleanup_expired()
        session_ids = set(self._storage.keys())

        if db_session is not None:
            result = await db_session.execute(select(DBConversation.session_id))
            session_ids.update(result.scalars().all())
        else:
            session_ids.update(self._list_local_store_session_ids())

        return sorted(session_ids)

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = datetime.now(UTC)
        expired = [
            sid for sid, exp_time in self._expiry.items()
            if exp_time < now
        ]
        for sid in expired:
            self._storage.pop(sid, None)
            self._expiry.pop(sid, None)
        if expired:
            self._remove_local_contexts(expired)

    async def get_conversation_summary(
        self,
        session_id: str,
        db_session: Optional[AsyncSession] = None,
    ) -> dict:
        """Get a summary of the conversation for debugging."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            return {"error": "Session not found"}

        return {
            "session_id": session_id,
            "state": context.state.value,
            "message_count": len(context.messages),
            "guest_phone": context.guest_phone,
            "room_number": context.room_number,
            "channel": context.channel,
            "pending_action": context.pending_action,
            "pending_data": context.pending_data,
            "created_at": context.created_at.isoformat(),
            "updated_at": context.updated_at.isoformat(),
        }

    @staticmethod
    def _normalize_channel(channel: Optional[str]) -> str:
        """Normalize channel IDs to a compact set used in storage."""
        if not channel:
            return "web"

        normalized = channel.strip().lower().replace("-", "_")
        if normalized in {"web", "web_widget", "widget", "chat_widget"}:
            return "web"
        if normalized in {"wa", "whatsapp", "whats_app"}:
            return "whatsapp"
        return normalized[:20]

    def _save_to_memory(
        self,
        context: ConversationContext,
        *,
        persist_local: bool = True,
    ) -> None:
        """Write current context snapshot to in-memory store."""
        context.updated_at = datetime.now(UTC)
        self._storage[context.session_id] = context.model_dump(mode="json")
        self._expiry[context.session_id] = datetime.now(UTC) + timedelta(hours=self.ttl_hours)
        if persist_local:
            self._upsert_local_context(context)

    def _is_local_store_enabled(self) -> bool:
        return bool(self._local_store_enabled)

    def _read_local_store_map(self) -> dict[str, dict[str, Any]]:
        if not self._is_local_store_enabled():
            return {}

        path = self._local_store_path
        if not path.exists():
            return {}

        try:
            raw_text = path.read_text(encoding="utf-8")
            if not str(raw_text).strip():
                return {}
            parsed = json.loads(raw_text)
        except Exception as exc:
            logger.warning("Failed to read local conversation store at %s: %s", path, exc)
            return {}

        if not isinstance(parsed, dict):
            return {}

        contexts = parsed.get("contexts") if isinstance(parsed.get("contexts"), dict) else parsed
        if not isinstance(contexts, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for session_id, payload in contexts.items():
            if not isinstance(session_id, str) or not isinstance(payload, dict):
                continue
            normalized[session_id] = payload
        return normalized

    def _write_local_store_map(self, contexts: dict[str, dict[str, Any]]) -> bool:
        if not self._is_local_store_enabled():
            return False

        path = self._local_store_path
        payload = {
            "version": 1,
            "saved_at": datetime.now(UTC).isoformat(),
            "contexts": contexts,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
            return True
        except Exception as exc:
            logger.warning("Failed to write local conversation store at %s: %s", path, exc)
            return False

    def _upsert_local_context(self, context: ConversationContext) -> None:
        if not self._is_local_store_enabled():
            return
        contexts = self._read_local_store_map()
        contexts[context.session_id] = context.model_dump(mode="json")
        self._write_local_store_map(contexts)

    def _remove_local_contexts(self, session_ids: list[str]) -> bool:
        if not self._is_local_store_enabled() or not session_ids:
            return False

        contexts = self._read_local_store_map()
        changed = False
        for session_id in session_ids:
            if session_id in contexts:
                contexts.pop(session_id, None)
                changed = True
        if not changed:
            return False
        return self._write_local_store_map(contexts)

    def _load_context_from_local_store(self, session_id: str) -> Optional[ConversationContext]:
        contexts = self._read_local_store_map()
        payload = contexts.get(session_id)
        if not isinstance(payload, dict):
            return None
        try:
            return ConversationContext(**payload)
        except Exception as exc:
            logger.warning(
                "Invalid local context payload for session %s at %s: %s",
                session_id,
                self._local_store_path,
                exc,
            )
            return None

    def _list_local_store_session_ids(self) -> list[str]:
        contexts = self._read_local_store_map()
        return sorted(contexts.keys())

    async def _resolve_hotel(self, db_session: AsyncSession, hotel_code: str) -> Hotel:
        """Resolve hotel by code, creating a minimal row when missing."""
        code = (hotel_code or "").strip()
        if not code:
            code = "DEFAULT"

        result = await db_session.execute(select(Hotel).where(Hotel.code == code))
        hotel = result.scalar_one_or_none()
        if hotel is not None:
            return hotel

        # Avoid creating noisy test rows; prefer canonical DEFAULT when available.
        if code.upper() in {"TEST_HOTEL", "DEFAULT"}:
            fallback = await db_session.execute(select(Hotel).where(Hotel.code == "DEFAULT"))
            default_hotel = fallback.scalar_one_or_none()
            if default_hotel is not None:
                return default_hotel

        hotel = Hotel(
            code=code,
            name=code.replace("_", " ").title(),
            city="Unknown",
            is_active=True,
        )
        db_session.add(hotel)
        await db_session.flush()
        return hotel

    async def _resolve_guest(
        self,
        db_session: AsyncSession,
        hotel_id: int,
        context: ConversationContext,
    ) -> Optional[Guest]:
        """Resolve guest by phone; creates a guest record if phone is available."""
        if not context.guest_phone:
            return None

        result = await db_session.execute(
            select(Guest).where(
                Guest.hotel_id == hotel_id,
                Guest.phone_number == context.guest_phone,
            )
        )
        guest = result.scalar_one_or_none()
        if guest is None:
            guest = Guest(
                hotel_id=hotel_id,
                phone_number=context.guest_phone,
                name=context.guest_name,
                room_number=context.room_number,
            )
            db_session.add(guest)
            await db_session.flush()
            return guest

        if context.guest_name:
            guest.name = context.guest_name
        if context.room_number:
            guest.room_number = context.room_number
        return guest

    async def _get_context_from_db(
        self,
        session_id: str,
        db_session: AsyncSession,
    ) -> Optional[ConversationContext]:
        """Reconstruct a ConversationContext from DB rows."""
        conv_result = await db_session.execute(
            select(DBConversation).where(DBConversation.session_id == session_id)
        )
        conversation = conv_result.scalar_one_or_none()
        if conversation is None:
            return None

        hotel_result = await db_session.execute(select(Hotel).where(Hotel.id == conversation.hotel_id))
        hotel = hotel_result.scalar_one_or_none()

        guest = None
        if conversation.guest_id:
            guest_result = await db_session.execute(select(Guest).where(Guest.id == conversation.guest_id))
            guest = guest_result.scalar_one_or_none()

        msg_result = await db_session.execute(
            select(DBMessage)
            .where(DBMessage.conversation_id == conversation.id)
            .order_by(DBMessage.created_at.asc(), DBMessage.id.asc())
        )
        db_messages = list(msg_result.scalars().all())

        chat_messages: list[ChatMessage] = []
        for msg in db_messages:
            try:
                role = MessageRole(msg.role)
            except ValueError:
                role = MessageRole.ASSISTANT

            metadata: dict[str, Any] = {}
            if msg.intent:
                metadata["intent"] = msg.intent
            if msg.confidence is not None:
                metadata["confidence"] = float(msg.confidence)
            metadata["channel"] = self._normalize_channel(
                getattr(msg, "channel", None) or getattr(conversation, "channel", None)
            )

            chat_messages.append(
                ChatMessage(
                    role=role,
                    content=msg.content,
                    timestamp=msg.created_at,
                    metadata=metadata,
                )
            )

        try:
            state = ConversationState(conversation.state)
        except ValueError:
            state = ConversationState.IDLE

        pending_data = getattr(conversation, "pending_data", None) or {}
        if isinstance(pending_data, str):
            try:
                pending_data = json.loads(pending_data)
            except json.JSONDecodeError:
                pending_data = {}
        if not isinstance(pending_data, dict):
            pending_data = {}

        return ConversationContext(
            session_id=conversation.session_id,
            hotel_code=hotel.code if hotel else "DEFAULT",
            guest_phone=guest.phone_number if guest else None,
            guest_name=guest.name if guest else None,
            room_number=guest.room_number if guest else None,
            channel=self._normalize_channel(getattr(conversation, "channel", None)),
            state=state,
            pending_action=getattr(conversation, "pending_action", None),
            pending_data=pending_data,
            messages=chat_messages,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    async def _save_context_to_db(
        self,
        context: ConversationContext,
        db_session: AsyncSession,
    ) -> None:
        """Persist context snapshot to conversation/message tables."""
        try:
            context.channel = self._normalize_channel(context.channel)
            if not isinstance(context.pending_data, dict):
                context.pending_data = {}

            hotel = await self._resolve_hotel(db_session, context.hotel_code)
            guest = await self._resolve_guest(db_session, hotel.id, context)

            conv_result = await db_session.execute(
                select(DBConversation).where(DBConversation.session_id == context.session_id)
            )
            conversation = conv_result.scalar_one_or_none()

            if conversation is None:
                conversation = DBConversation(
                    session_id=context.session_id,
                    hotel_id=hotel.id,
                    guest_id=guest.id if guest else None,
                    state=context.state.value,
                    pending_action=context.pending_action,
                    pending_data=context.pending_data or {},
                    channel=context.channel,
                )
                db_session.add(conversation)
                await db_session.flush()
            else:
                conversation.hotel_id = hotel.id
                conversation.guest_id = guest.id if guest else conversation.guest_id
                conversation.state = context.state.value
                conversation.pending_action = context.pending_action
                conversation.pending_data = context.pending_data or {}
                conversation.channel = context.channel

            count_result = await db_session.execute(
                select(func.count(DBMessage.id)).where(DBMessage.conversation_id == conversation.id)
            )
            existing_count = int(count_result.scalar() or 0)

            # Insert only messages that are not already persisted.
            if existing_count < len(context.messages):
                for msg in context.messages[existing_count:]:
                    meta = msg.metadata or {}
                    db_session.add(
                        DBMessage(
                            conversation_id=conversation.id,
                            role=msg.role.value,
                            content=msg.content,
                            intent=meta.get("intent"),
                            confidence=meta.get("confidence"),
                            channel=self._normalize_channel(meta.get("channel") or context.channel),
                            created_at=msg.timestamp,
                        )
                    )

            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise


# Global instance
context_manager = ContextManager()
