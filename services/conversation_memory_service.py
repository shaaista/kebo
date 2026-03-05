"""
Conversation Memory Service

Maintains long-term conversational memory per session:
1) rolling summary of the full conversation
2) latest structured facts (last-write-wins)
3) fact change history for traceability/evaluation

Memory is persisted inside context.pending_data["_memory"] so no schema
migration is required.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from typing import Any

from config.settings import settings
from llm.client import llm_client
from schemas.chat import ConversationContext, MessageRole


class ConversationMemoryService:
    """Service for retention beyond a short message window."""

    MEMORY_KEY = "_memory"
    MAX_FACT_HISTORY = 40
    MAX_BOOKING_HISTORY = 20
    MAX_ORDER_HISTORY = 20
    MAX_TICKET_HISTORY = 25
    SUMMARY_REFRESH_INTERVAL = 6
    SUMMARY_MAX_LEN = 900
    MAX_CONTEXTUAL_QUERY_LEN = 420

    _TIME_PATTERN = re.compile(
        r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
        re.IGNORECASE,
    )
    _ROOM_PATTERN = re.compile(
        r"\b(?:my\s+)?room(?:\s*(?:number|no\.?))?\s*(?:is|=|:)?\s*([a-zA-Z0-9-]{2,10})\b",
        re.IGNORECASE,
    )
    _PARTY_PATTERN = re.compile(r"\b(?:for|party of)\s+(\d{1,2})\b", re.IGNORECASE)
    _MONTH_ALIASES = {
        "jan": "Jan",
        "january": "Jan",
        "feb": "Feb",
        "february": "Feb",
        "mar": "Mar",
        "march": "Mar",
        "apr": "Apr",
        "april": "Apr",
        "may": "May",
        "jun": "Jun",
        "june": "Jun",
        "jul": "Jul",
        "july": "Jul",
        "aug": "Aug",
        "august": "Aug",
        "sep": "Sep",
        "sept": "Sep",
        "september": "Sep",
        "oct": "Oct",
        "october": "Oct",
        "nov": "Nov",
        "november": "Nov",
        "dec": "Dec",
        "december": "Dec",
    }
    _MONTH_TOKEN = (
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    )
    _DATE_RANGE_MONTH_FIRST_PATTERN = re.compile(
        rf"\b(?P<month>{_MONTH_TOKEN})\s*(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*"
        rf"(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\b",
        re.IGNORECASE,
    )
    _DATE_RANGE_FROM_TO_PATTERN = re.compile(
        rf"\bfrom\s*(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*(?P<start_month>{_MONTH_TOKEN})?\s*"
        rf"(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\s*(?P<end_month>{_MONTH_TOKEN})\b",
        re.IGNORECASE,
    )
    _SINGLE_DATE_MONTH_FIRST_PATTERN = re.compile(
        rf"\b(?P<month>{_MONTH_TOKEN})\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b",
        re.IGNORECASE,
    )
    _SINGLE_DATE_DAY_FIRST_PATTERN = re.compile(
        rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*(?P<month>{_MONTH_TOKEN})\b",
        re.IGNORECASE,
    )
    _SHORT_FOLLOWUP_PATTERN = re.compile(
        r"^\s*(?:"
        r"how much(?: cost| price| charges?)?|"
        r"what about price|"
        r"price\??|"
        r"cost\??|"
        r"charges?\??|"
        r"and price\??|"
        r"what time\??|"
        r"when\??|"
        r"how long\??|"
        r"availability\??|"
        r"is it available\??"
        r")\s*$",
        re.IGNORECASE,
    )

    def ensure_memory(self, context: ConversationContext) -> dict[str, Any]:
        """Ensure memory structure exists in pending_data and return it."""
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        memory = context.pending_data.get(self.MEMORY_KEY)
        if not isinstance(memory, dict):
            memory = {
                "summary": "",
                "facts": {},
                "fact_history": [],
                "last_summarized_count": 0,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            context.pending_data[self.MEMORY_KEY] = memory
            return memory

        if not isinstance(memory.get("facts"), dict):
            memory["facts"] = {}
        if not isinstance(memory.get("fact_history"), list):
            memory["fact_history"] = []
        if not isinstance(memory.get("summary"), str):
            memory["summary"] = ""
        if not isinstance(memory.get("last_summarized_count"), int):
            memory["last_summarized_count"] = 0
        memory["updated_at"] = datetime.now(UTC).isoformat()
        return memory

    @staticmethod
    def internal_pending_entries(pending_data: Any) -> dict[str, Any]:
        """Get reserved internal keys (prefixed with underscore)."""
        if not isinstance(pending_data, dict):
            return {}
        return {
            key: value
            for key, value in pending_data.items()
            if isinstance(key, str) and key.startswith("_")
        }

    def merge_with_internal(
        self,
        new_pending_data: Any,
        internal_entries: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge handler pending data with preserved internal entries."""
        merged: dict[str, Any] = {}
        if isinstance(new_pending_data, dict):
            merged.update(new_pending_data)
        for key, value in (internal_entries or {}).items():
            merged.setdefault(key, value)
        return merged

    def capture_user_message(self, context: ConversationContext, message: str) -> None:
        """
        Extract memory updates from the user message and store them.
        Deterministic extraction handles common service/booking details.
        """
        memory = self.ensure_memory(context)
        facts = memory.setdefault("facts", {})
        fact_history = memory.setdefault("fact_history", [])

        msg = str(message or "").strip()
        if not msg:
            return
        msg_lower = msg.lower()
        is_correction = self._looks_like_correction(msg_lower)

        extracted = self._extract_facts(msg)
        topic_hint = self._infer_topic_hint(msg_lower)
        if topic_hint:
            extracted["last_user_topic"] = topic_hint
        if is_correction:
            self._apply_correction_inference(extracted, facts)
        for key, value in extracted.items():
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key=key,
                value=value,
                source_message=msg[:300],
                change_type="corrected" if (facts.get(key) is not None and is_correction) else "set",
            )

        if len(fact_history) > self.MAX_FACT_HISTORY:
            memory["fact_history"] = fact_history[-self.MAX_FACT_HISTORY :]

        memory["last_user_message"] = msg[:400]
        memory["updated_at"] = datetime.now(UTC).isoformat()

    def capture_assistant_message(
        self,
        context: ConversationContext,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Capture durable transactional details emitted by handlers/assistant.
        This keeps reservation and order info retrievable in later turns.
        """
        memory = self.ensure_memory(context)
        facts = memory.setdefault("facts", {})
        fact_history = memory.setdefault("fact_history", [])

        meta = metadata if isinstance(metadata, dict) else {}
        msg = str(message or "").strip()
        now_iso = datetime.now(UTC).isoformat()

        booking_ref = str(meta.get("booking_ref") or "").strip()
        if booking_ref:
            booking_record = {
                "reference": booking_ref,
                "restaurant": str(meta.get("booking_restaurant") or meta.get("restaurant_name") or "").strip(),
                "party_size": str(meta.get("booking_party_size") or "").strip(),
                "time": str(meta.get("booking_time") or "").strip(),
                "date": str(meta.get("booking_date") or "").strip(),
                "status": "confirmed",
                "updated_at": now_iso,
            }
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="latest_booking",
                value=booking_record,
                source_message=msg[:300],
            )
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_booking_ref",
                value=booking_ref,
                source_message=msg[:300],
            )
            if booking_record.get("time"):
                self._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key="booking_time",
                    value=booking_record["time"],
                    source_message=msg[:300],
                )
            if booking_record.get("date"):
                self._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key="booking_day",
                    value=booking_record["date"],
                    source_message=msg[:300],
                )
            history = list(facts.get("booking_history") or [])
            history.append(booking_record)
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="booking_history",
                value=history[-self.MAX_BOOKING_HISTORY :],
                source_message=msg[:300],
            )

        order_id = meta.get("order_id")
        if order_id is not None and str(order_id).strip():
            order_record = {
                "id": str(order_id).strip(),
                "items": list(meta.get("order_items") or []),
                "total": meta.get("order_total"),
                "restaurant_id": meta.get("restaurant_id"),
                "status": "confirmed",
                "updated_at": now_iso,
            }
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="latest_order",
                value=order_record,
                source_message=msg[:300],
            )
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_order_id",
                value=order_record["id"],
                source_message=msg[:300],
            )
            history = list(facts.get("order_history") or [])
            history.append(order_record)
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="order_history",
                value=history[-self.MAX_ORDER_HISTORY :],
                source_message=msg[:300],
            )

        ticket_id = meta.get("ticket_id")
        if ticket_id is not None and str(ticket_id).strip():
            ticket_record = {
                "id": str(ticket_id).strip(),
                "status": str(meta.get("ticket_status") or "open").strip().lower() or "open",
                "category": str(meta.get("ticket_category") or "").strip().lower(),
                "sub_category": str(meta.get("ticket_sub_category") or "").strip().lower(),
                "priority": str(meta.get("ticket_priority") or "").strip().lower(),
                "department_id": str(meta.get("ticket_department_id") or "").strip(),
                "assigned_id": str(
                    meta.get("ticket_assigned_id")
                    or meta.get("assigned_id")
                    or meta.get("agent_id")
                    or ""
                ).strip(),
                "room_number": str(
                    meta.get("room_number")
                    or context.room_number
                    or ""
                ).strip(),
                "source": str(meta.get("ticket_source") or "").strip(),
                "updated_at": now_iso,
            }
            if meta.get("ticket_summary"):
                ticket_record["summary"] = str(meta.get("ticket_summary")).strip()

            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="latest_ticket",
                value=ticket_record,
                source_message=msg[:300],
            )
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_ticket_id",
                value=ticket_record["id"],
                source_message=msg[:300],
            )
            history = list(facts.get("ticket_history") or [])
            history.append(ticket_record)
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="ticket_history",
                value=history[-self.MAX_TICKET_HISTORY :],
                source_message=msg[:300],
            )

        if meta.get("room_number"):
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="room_number",
                value=str(meta["room_number"]).strip(),
                source_message=msg[:300],
            )

        guest_preferences_meta = meta.get("guest_preferences")
        if isinstance(guest_preferences_meta, list):
            normalized_preferences: list[str] = []
            for item in guest_preferences_meta:
                text = str(item or "").strip().lower()
                if not text:
                    continue
                text = re.sub(r"[^a-z0-9 /-]+", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) < 3 or text in normalized_preferences:
                    continue
                normalized_preferences.append(text[:80])
                if len(normalized_preferences) >= 8:
                    break
            if normalized_preferences:
                self._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key="guest_preferences",
                    value=normalized_preferences,
                    source_message=msg[:300],
                )

        if meta.get("flight_time"):
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="flight_time",
                value=str(meta["flight_time"]).strip(),
                source_message=msg[:300],
            )

        if meta.get("transport_type"):
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_transport_type",
                value=str(meta["transport_type"]).strip(),
                source_message=msg[:300],
            )

        memory["last_assistant_message"] = msg[:400]
        memory["updated_at"] = now_iso

    def contextualize_follow_up_query(
        self,
        context: ConversationContext,
        user_query: str,
    ) -> dict[str, Any]:
        """
        Rewrite terse follow-up user queries with conversation memory context.
        Returns metadata for observability and downstream routing.
        """
        query = str(user_query or "").strip()
        if not query:
            return {"query": query, "rewritten": False, "reason": "empty"}

        memory = self.ensure_memory(context)
        facts = memory.get("facts", {}) if isinstance(memory.get("facts"), dict) else {}

        should_rewrite = self._should_rewrite_follow_up(query)
        if not should_rewrite:
            return {"query": query, "rewritten": False, "reason": "not_followup"}

        previous_user = self._previous_user_message(context)
        contextual_parts: list[str] = []
        if previous_user:
            contextual_parts.append(f"previous_request={previous_user[:160]}")

        last_topic = str(facts.get("last_user_topic") or "").strip()
        if last_topic:
            contextual_parts.append(f"topic={last_topic}")

        facts_snippet = []
        for key in (
            "route",
            "flight_time",
            "pickup_time",
            "booking_time",
            "booking_day",
            "departure_time",
            "departure_day",
            "room_number",
        ):
            value = facts.get(key)
            if value:
                facts_snippet.append(f"{key}={value}")
        if facts_snippet:
            contextual_parts.append("known_facts=" + ", ".join(facts_snippet[:5]))

        if not contextual_parts:
            return {"query": query, "rewritten": False, "reason": "no_context"}

        rewritten = f"{query} | Context: {'; '.join(contextual_parts)}"
        rewritten = rewritten[: self.MAX_CONTEXTUAL_QUERY_LEN]
        return {
            "query": rewritten,
            "rewritten": True,
            "reason": "followup_context_added",
            "base_query": query,
            "contextual_parts": contextual_parts,
        }

    def get_snapshot(self, context: ConversationContext) -> dict[str, Any]:
        """Return a safe memory snapshot for prompt assembly and debug metadata."""
        memory = self.ensure_memory(context)
        return {
            "summary": str(memory.get("summary") or "").strip(),
            "facts": dict(memory.get("facts") or {}),
            "recent_changes": list(memory.get("fact_history") or [])[-5:],
            "last_summarized_count": int(memory.get("last_summarized_count") or 0),
        }

    async def maybe_refresh_summary(self, context: ConversationContext) -> None:
        """Refresh rolling summary periodically to retain long conversations."""
        memory = self.ensure_memory(context)
        message_count = len(context.messages)
        last = int(memory.get("last_summarized_count") or 0)

        if message_count < 4:
            return
        if (message_count - last) < self.SUMMARY_REFRESH_INTERVAL:
            return

        summary = await self._build_summary(context, memory)
        memory["summary"] = summary[: self.SUMMARY_MAX_LEN]
        memory["last_summarized_count"] = message_count
        memory["updated_at"] = datetime.now(UTC).isoformat()

    async def refresh_summary(self, context: ConversationContext) -> None:
        """Force summary refresh for modes that require maximum continuity."""
        memory = self.ensure_memory(context)
        summary = await self._build_summary(context, memory)
        memory["summary"] = summary[: self.SUMMARY_MAX_LEN]
        memory["last_summarized_count"] = len(context.messages)
        memory["updated_at"] = datetime.now(UTC).isoformat()

    async def _build_summary(self, context: ConversationContext, memory: dict[str, Any]) -> str:
        """Generate summary with LLM when available, otherwise use deterministic fallback."""
        previous_summary = str(memory.get("summary") or "").strip()
        facts = memory.get("facts") if isinstance(memory.get("facts"), dict) else {}
        recent_changes = list(memory.get("fact_history") or [])[-8:]

        recent_turns = []
        for msg in context.messages[-18:]:
            role = getattr(msg.role, "value", str(msg.role))
            recent_turns.append(f"{role.upper()}: {msg.content}")
        transcript = "\n".join(recent_turns) if recent_turns else "No transcript available."

        if settings.openai_api_key:
            prompt = (
                "You maintain long-term conversation memory for a service bot.\n"
                "Write a concise summary (max 120 words) that preserves:\n"
                "1) active user goals\n"
                "2) latest corrected details (new values override old)\n"
                "3) unresolved asks\n"
                "Do not include secrets or internal data.\n\n"
                f"Previous Summary:\n{previous_summary or 'None'}\n\n"
                f"Known Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
                f"Recent Fact Changes JSON:\n{json.dumps(recent_changes, ensure_ascii=False)}\n\n"
                f"Recent Transcript:\n{transcript}\n"
            )
            try:
                summary = await llm_client.chat(
                    messages=[{"role": "system", "content": prompt}],
                    temperature=0.1,
                    max_tokens=220,
                )
                cleaned = str(summary or "").strip()
                if cleaned:
                    return cleaned
            except Exception:
                pass

        return self._fallback_summary(context, facts, recent_changes)

    def _fallback_summary(
        self,
        context: ConversationContext,
        facts: dict[str, Any],
        recent_changes: list[dict[str, Any]],
    ) -> str:
        """Deterministic summary fallback for local/dev mode."""
        user_messages = [
            msg.content.strip()
            for msg in context.messages
            if msg.role == MessageRole.USER and msg.content.strip()
        ]

        parts: list[str] = []
        if user_messages:
            parts.append(f"Initial user request: {user_messages[0][:120]}")
            parts.append(f"Latest user request: {user_messages[-1][:140]}")

        if facts:
            compact_facts = ", ".join(
                f"{key}={value}"
                for key, value in list(facts.items())[:8]
            )
            parts.append(f"Current known details: {compact_facts}")

        if recent_changes:
            compact_changes = "; ".join(
                f"{item.get('key')}->{item.get('new_value')}"
                for item in recent_changes[-3:]
            )
            parts.append(f"Recent updates: {compact_changes}")

        if not parts:
            return "Conversation started. No durable details captured yet."
        return " | ".join(parts)

    @staticmethod
    def _looks_like_correction(msg_lower: str) -> bool:
        correction_markers = (
            "change",
            "changed",
            "update",
            "updated",
            "instead",
            "actually",
            "correction",
            "make it",
            "reschedule",
        )
        return any(marker in msg_lower for marker in correction_markers)

    def _extract_facts(self, message: str) -> dict[str, Any]:
        """Extract commonly useful details from free-form user text."""
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        extracted: dict[str, Any] = {}

        room_match = self._ROOM_PATTERN.search(msg)
        if room_match:
            room_candidate = room_match.group(1).upper()
            if any(ch.isdigit() for ch in room_candidate):
                extracted["room_number"] = room_candidate

        party_match = self._PARTY_PATTERN.search(msg)
        if party_match:
            extracted["party_size"] = party_match.group(1)

        stay_dates = self._extract_stay_dates(msg_lower)
        if stay_dates:
            extracted.update(stay_dates)

        time_match = self._TIME_PATTERN.search(msg_lower)
        if time_match:
            time_value = time_match.group(1).strip().replace("  ", " ")
            if any(token in msg_lower for token in ("leave", "leaving", "checkout", "check out", "check-out", "depart")):
                extracted["departure_time"] = time_value
            elif any(token in msg_lower for token in ("flight", "arrival", "terminal")):
                extracted["flight_time"] = time_value
            elif any(token in msg_lower for token in ("pickup", "pick up", "drop", "transfer", "cab", "taxi")):
                extracted["pickup_time"] = time_value
            elif any(token in msg_lower for token in ("book", "booking", "table", "reservation")):
                extracted["booking_time"] = time_value
            else:
                extracted["mentioned_time"] = time_value

        if "tomorrow" in msg_lower:
            if any(token in msg_lower for token in ("leave", "leaving", "checkout", "check out", "depart")):
                extracted["departure_day"] = "tomorrow"
            elif "book" in msg_lower or "reservation" in msg_lower:
                extracted["booking_day"] = "tomorrow"
            else:
                extracted["mentioned_day"] = "tomorrow"
        elif "today" in msg_lower:
            extracted["mentioned_day"] = "today"
        elif "tonight" in msg_lower:
            extracted["mentioned_day"] = "tonight"

        # Transport route hint: "from X to Y"
        route_match = re.search(r"\bfrom\s+([a-z ]{2,40})\s+to\s+([a-z ]{2,40})\b", msg_lower)
        if route_match:
            source = route_match.group(1).strip()
            destination = route_match.group(2).strip()
            extracted["route"] = f"{source} -> {destination}"

        return extracted

    def _extract_stay_dates(self, msg_lower: str) -> dict[str, Any]:
        """
        Extract stay/booking date hints, including compact ranges like
        'feb 20-23' and correction-style messages.
        """
        extracted: dict[str, Any] = {}
        if not msg_lower:
            return extracted

        stay_context = any(
            token in msg_lower
            for token in (
                "stay",
                "book",
                "booking",
                "reservation",
                "check in",
                "check-in",
                "checkin",
                "check out",
                "check-out",
                "checkout",
                "room",
            )
        )
        if not stay_context:
            return extracted

        range_match = self._DATE_RANGE_MONTH_FIRST_PATTERN.search(msg_lower)
        if range_match:
            month_token = str(range_match.group("month") or "")
            start_day = range_match.group("start")
            end_day = range_match.group("end")
            start = self._format_month_day(month_token, start_day)
            end = self._format_month_day(month_token, end_day)
            if start and end:
                extracted["stay_checkin_date"] = start
                extracted["stay_checkout_date"] = end
                extracted["stay_date_range"] = f"{start} to {end}"
                extracted.setdefault("booking_day", extracted["stay_date_range"])
                return extracted

        from_to_match = self._DATE_RANGE_FROM_TO_PATTERN.search(msg_lower)
        if from_to_match:
            start_day = from_to_match.group("start")
            end_day = from_to_match.group("end")
            start_month = str(from_to_match.group("start_month") or from_to_match.group("end_month") or "")
            end_month = str(from_to_match.group("end_month") or start_month)
            start = self._format_month_day(start_month, start_day)
            end = self._format_month_day(end_month, end_day)
            if start and end:
                extracted["stay_checkin_date"] = start
                extracted["stay_checkout_date"] = end
                extracted["stay_date_range"] = f"{start} to {end}"
                extracted.setdefault("booking_day", extracted["stay_date_range"])
                return extracted

        single_month_first = self._SINGLE_DATE_MONTH_FIRST_PATTERN.search(msg_lower)
        single_day_first = self._SINGLE_DATE_DAY_FIRST_PATTERN.search(msg_lower)
        month_token = ""
        day_token = ""
        if single_month_first:
            month_token = str(single_month_first.group("month") or "")
            day_token = str(single_month_first.group("day") or "")
        elif single_day_first:
            month_token = str(single_day_first.group("month") or "")
            day_token = str(single_day_first.group("day") or "")

        single_date = self._format_month_day(month_token, day_token)
        if not single_date:
            return extracted

        if any(token in msg_lower for token in ("check out", "check-out", "checkout")):
            extracted["stay_checkout_date"] = single_date
        elif any(token in msg_lower for token in ("check in", "check-in", "checkin")):
            extracted["stay_checkin_date"] = single_date
        else:
            extracted["stay_checkin_date"] = single_date
        extracted.setdefault("booking_day", single_date)
        return extracted

    def _format_month_day(self, month_token: str, day_token: str) -> str:
        month_key = str(month_token or "").strip().lower()
        month_norm = self._MONTH_ALIASES.get(month_key)
        if not month_norm:
            return ""
        try:
            day_value = int(str(day_token or "").strip())
        except (TypeError, ValueError):
            return ""
        if day_value < 1 or day_value > 31:
            return ""
        return f"{month_norm} {day_value:02d}"

    @staticmethod
    def _infer_topic_hint(msg_lower: str) -> str:
        if any(token in msg_lower for token in ("airport", "flight", "transfer", "cab", "taxi", "pickup", "drop")):
            return "transport"
        if any(token in msg_lower for token in ("book", "booking", "reservation", "table")):
            return "reservation"
        if any(token in msg_lower for token in ("order", "menu", "food", "dish", "price")):
            return "menu_or_order"
        if any(token in msg_lower for token in ("leave", "leaving", "checkout", "check out", "depart")):
            return "checkout_or_departure"
        if any(token in msg_lower for token in ("room service", "housekeeping", "cleaning", "towel", "amenities")):
            return "room_service"
        return ""

    def _set_fact(
        self,
        facts: dict[str, Any],
        fact_history: list[dict[str, Any]],
        key: str,
        value: Any,
        source_message: str,
        change_type: str = "set",
    ) -> None:
        old_value = facts.get(key)
        if old_value == value:
            return
        facts[key] = value
        fact_history.append(
            {
                "key": key,
                "old_value": old_value,
                "new_value": value,
                "change_type": change_type,
                "source_message": source_message,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def _previous_user_message(self, context: ConversationContext) -> str:
        """
        Return the previous user message before the latest one in the context.
        """
        seen_latest = False
        for msg in reversed(context.messages):
            if msg.role != MessageRole.USER:
                continue
            if not seen_latest:
                seen_latest = True
                continue
            value = str(msg.content or "").strip()
            if value:
                return value
        return ""

    def _should_rewrite_follow_up(self, query: str) -> bool:
        query_clean = str(query or "").strip()
        if not query_clean:
            return False

        tokens = re.findall(r"[a-z0-9]+", query_clean.lower())
        if len(tokens) <= 3:
            followup_tokens = {"price", "cost", "charges", "timing", "time", "when", "how", "much", "available"}
            return any(token in followup_tokens for token in tokens)

        if self._SHORT_FOLLOWUP_PATTERN.match(query_clean):
            return True

        followup_markers = (
            "how much",
            "what about",
            "and price",
            "and timing",
            "and cost",
            "how long",
        )
        return any(marker in query_clean.lower() for marker in followup_markers)

    @staticmethod
    def _apply_correction_inference(extracted: dict[str, Any], existing_facts: dict[str, Any]) -> None:
        """
        If the user sends a correction without repeating the original field name,
        map generic values to the most likely previously-set key.
        Example: "change it to 3pm" -> updates existing departure_time.
        """
        if "mentioned_time" in extracted:
            inferred_targets = ("departure_time", "pickup_time", "flight_time", "booking_time")
            for target in inferred_targets:
                if target in existing_facts:
                    extracted[target] = extracted.pop("mentioned_time")
                    break

        if "mentioned_day" in extracted:
            inferred_targets = ("departure_day", "booking_day")
            for target in inferred_targets:
                if target in existing_facts:
                    extracted[target] = extracted.pop("mentioned_day")
                    break


# Global singleton
conversation_memory_service = ConversationMemoryService()
