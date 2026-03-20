"""
Lumira compatibility adapter.

Maps Lumira-style request/response contracts to the internal ChatRequest/ChatResponse
without changing the bot's core ticketing logic.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from schemas.chat import ChatRequest, ChatResponse


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean_str(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return False


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(str(value).strip())
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).strip())
    except Exception:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _fallback_session_id(prefix: str, seed: str = "") -> str:
    normalized_seed = _clean_str(seed)
    if normalized_seed:
        return f"{prefix}_{normalized_seed}"
    return f"{prefix}_{uuid4().hex[:16]}"


def build_guest_journey_chat_request(payload: dict[str, Any]) -> ChatRequest:
    body = _as_dict(payload)
    incoming_meta = _as_dict(body.get("metadata"))

    message = _clean_str(body.get("message"))
    wa_number = _clean_str(
        body.get("waNumber")
        or body.get("wa_number")
        or body.get("guest_phone")
        or body.get("guestPhone")
    )
    session_id = _clean_str(body.get("session_id") or body.get("sessionId"))
    if not session_id:
        session_id = _fallback_session_id("gj", wa_number)

    phase = _clean_str(body.get("phase"))
    entity_id = body.get("entity_id") if body.get("entity_id") is not None else body.get("entityId")
    group_id = body.get("group_id") if body.get("group_id") is not None else body.get("groupId")
    guest_id = body.get("guest_id") if body.get("guest_id") is not None else body.get("guestId")
    room_number = _clean_str(body.get("room_number") or body.get("roomNumber"))
    guest_name = _clean_str(body.get("guest_name") or body.get("guestName"))
    interactive_id = body.get("interactiveId")

    hotel_code = _clean_str(
        body.get("hotel_code")
        or body.get("hotelCode")
        or entity_id
        or group_id
        or "DEFAULT"
    )
    channel = _clean_str(body.get("channel") or incoming_meta.get("channel") or "whatsapp")

    metadata: dict[str, Any] = dict(incoming_meta)
    metadata.setdefault("flow", "guest_journey")
    metadata.setdefault("channel", channel)
    metadata.setdefault("ticket_source", "whatsapp_bot")
    if phase:
        metadata["phase"] = phase
    if entity_id not in (None, ""):
        metadata["entity_id"] = entity_id
        metadata.setdefault("organisation_id", entity_id)
    if group_id not in (None, ""):
        metadata["group_id"] = group_id
    if guest_id not in (None, ""):
        metadata["guest_id"] = guest_id
    if room_number:
        metadata["room_number"] = room_number
    if guest_name:
        metadata["guest_name"] = guest_name
    if wa_number:
        metadata["wa_number"] = wa_number
        metadata.setdefault("guest_phone", wa_number)
    if interactive_id not in (None, ""):
        metadata["interactive_id"] = interactive_id
        # Lumira often uses this as the latest message marker.
        metadata.setdefault("message_id", interactive_id)

    return ChatRequest(
        session_id=session_id,
        message=message,
        hotel_code=hotel_code,
        guest_phone=wa_number or None,
        channel=channel,
        metadata=metadata,
    )


def build_engage_chat_request(payload: dict[str, Any], session_id_header: str = "") -> ChatRequest:
    body = _as_dict(payload)
    incoming_meta = _as_dict(body.get("metadata"))

    message = _clean_str(body.get("message"))
    session_id = _clean_str(session_id_header or body.get("session_id") or body.get("sessionId"))
    if not session_id:
        session_id = _fallback_session_id(
            "eng",
            _clean_str(body.get("conversation_id") or body.get("conversationId")),
        )

    entity_id = body.get("entity_id") if body.get("entity_id") is not None else body.get("entityId")
    group_id = body.get("group_id") if body.get("group_id") is not None else body.get("groupId")
    message_id = body.get("message_id") if body.get("message_id") is not None else body.get("messageId")
    conversation_id = _clean_str(body.get("conversation_id") or body.get("conversationId"))
    widget_id = body.get("widget_id") if body.get("widget_id") is not None else body.get("widgetId")
    city = _clean_str(body.get("city"))
    country = _clean_str(body.get("country"))

    hotel_code = _clean_str(
        body.get("hotel_code")
        or body.get("hotelCode")
        or entity_id
        or group_id
        or "DEFAULT"
    )
    channel = _clean_str(body.get("channel") or incoming_meta.get("channel") or "web_widget")

    metadata: dict[str, Any] = dict(incoming_meta)
    metadata.setdefault("flow", "engage")
    metadata.setdefault("phase", "pre_booking")
    metadata.setdefault("ticket_source", "booking_bot")
    metadata.setdefault("channel", channel)
    if entity_id not in (None, ""):
        metadata["entity_id"] = entity_id
        metadata.setdefault("organisation_id", entity_id)
    if group_id not in (None, ""):
        metadata["group_id"] = group_id
    if message_id not in (None, ""):
        metadata["message_id"] = message_id
    if conversation_id:
        metadata["conversation_id"] = conversation_id
    if widget_id not in (None, ""):
        metadata["widget_id"] = widget_id
    if city:
        metadata["city"] = city
    if country:
        metadata["country"] = country

    return ChatRequest(
        session_id=session_id,
        message=message,
        hotel_code=hotel_code,
        channel=channel,
        metadata=metadata,
    )


def _fallback_ticket_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    fallback: dict[str, Any] = {}
    ticket_id = _clean_str(metadata.get("ticket_id"))
    if ticket_id:
        fallback["ticket_id"] = ticket_id
    status = _clean_str(metadata.get("ticket_status"))
    if status:
        fallback["ticket_status"] = status
    category = _clean_str(metadata.get("ticket_category"))
    if category:
        fallback["category"] = category
    sub_category = _clean_str(metadata.get("ticket_sub_category"))
    if sub_category:
        fallback["sub_category"] = sub_category
    priority = _clean_str(metadata.get("ticket_priority"))
    if priority:
        fallback["priority"] = priority
    issue_summary = metadata.get("ticket_summary")
    if isinstance(issue_summary, str) and issue_summary.strip():
        fallback["issue"] = issue_summary.strip()
    return fallback


def resolve_ticket_summary(
    metadata: dict[str, Any],
    *,
    as_list: bool,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    candidates = (
        metadata.get("ticket_api_response"),
        metadata.get("ticket_update_response"),
        metadata.get("ticket_record"),
        metadata.get("ticket_summary"),
    )
    for value in candidates:
        if isinstance(value, list):
            normalized = [item for item in value if isinstance(item, dict)]
            if not normalized:
                continue
            return normalized if as_list else normalized[0]
        if isinstance(value, dict):
            return [value] if as_list else value

    fallback = _fallback_ticket_summary(metadata)
    if fallback:
        return [fallback] if as_list else fallback
    return [] if as_list else None


def build_guest_journey_response(
    chat_response: ChatResponse,
    *,
    source_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = _as_dict(source_payload)
    metadata = _as_dict(chat_response.metadata)

    ticket_created = _to_bool(metadata.get("ticket_created"))
    ticket_summary = resolve_ticket_summary(metadata, as_list=True)

    room_number = _clean_str(
        metadata.get("room_number")
        or source.get("room_number")
        or source.get("roomNumber")
    )
    outlet_id = (
        metadata.get("outlet_id")
        if metadata.get("outlet_id") is not None
        else source.get("outlet_id")
    )

    category = _clean_str(metadata.get("ticket_category"))
    if not category:
        category = "conversation" if not ticket_created else "request"
    sub_category = _clean_str(
        metadata.get("ticket_sub_category")
        or metadata.get("ticketing_matched_case")
    )
    display_text = _clean_str(chat_response.display_message or chat_response.message)

    payload: dict[str, Any] = {
        "is_ticket_intent": ticket_created,
        "response": display_text,
        "category": category,
        "sub_category": sub_category,
        "room_number": room_number,
    }
    if ticket_summary:
        payload["ticket_summary"] = ticket_summary
    if outlet_id not in (None, ""):
        payload["outlet_id"] = outlet_id

    # Preserve optional usage telemetry when present.
    input_tokens = _to_int(metadata.get("input_tokens"))
    output_tokens = _to_int(metadata.get("output_tokens"))
    total_tokens = _to_int(metadata.get("total_tokens"))
    cost = _to_float(metadata.get("cost"))
    if input_tokens is not None:
        payload["input_tokens"] = input_tokens
    if output_tokens is not None:
        payload["output_tokens"] = output_tokens
    if total_tokens is not None:
        payload["total_tokens"] = total_tokens
    if cost is not None:
        payload["cost"] = round(cost, 4)

    media = metadata.get("media")
    url = metadata.get("url")
    if media not in (None, ""):
        payload["media"] = media
    if url not in (None, ""):
        payload["url"] = url

    return payload


def build_engage_response(
    chat_response: ChatResponse,
    *,
    source_payload: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    source = _as_dict(source_payload)
    metadata = _as_dict(chat_response.metadata)

    ticket_created = _to_bool(metadata.get("ticket_created"))
    ticket_summary = resolve_ticket_summary(metadata, as_list=False)

    message_id = source.get("message_id")
    if message_id is None:
        message_id = source.get("messageId")
    if message_id is None:
        message_id = metadata.get("message_id")

    total_cost = _to_float(metadata.get("total_cost") or metadata.get("cost")) or 0.0
    total_tokens = _to_int(metadata.get("total_tokens")) or 0
    display_text = _clean_str(chat_response.display_message or chat_response.message)

    payload: dict[str, Any] = {
        "message": display_text,
        "render": metadata.get("render"),
        "city": _clean_str(source.get("city") or metadata.get("city")),
        "country": _clean_str(source.get("country") or metadata.get("country")),
        "is_ticket_intent": ticket_created,
        "render_type": metadata.get("render_type"),
        "message_id": message_id,
        "additional_data": {
            "total_cost": round(total_cost, 4),
            "total_tokens": int(total_tokens),
            "session_id": _clean_str(session_id or chat_response.session_id),
            "message_id": message_id,
            "entity_id": source.get("entity_id")
            if source.get("entity_id") is not None
            else source.get("entityId"),
            "group_id": source.get("group_id")
            if source.get("group_id") is not None
            else source.get("groupId"),
            "sender": "bot",
        },
    }
    if ticket_summary:
        payload["ticket_summary"] = ticket_summary
    return payload
