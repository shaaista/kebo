import pytest

from config.settings import settings
from schemas.chat import ConversationContext, MessageRole
from services.conversation_memory_service import ConversationMemoryService


def test_memory_service_applies_last_write_wins_for_corrections():
    service = ConversationMemoryService()
    context = ConversationContext(session_id="s1", hotel_code="DEFAULT")

    service.capture_user_message(context, "I will leave at 12:30 tomorrow.")
    service.capture_user_message(context, "Actually change it to 3pm tomorrow.")

    snapshot = service.get_snapshot(context)
    facts = snapshot["facts"]

    assert facts.get("departure_time") == "3pm"
    assert facts.get("departure_day") == "tomorrow"
    assert any(
        item.get("key") == "departure_time" and item.get("new_value") == "3pm"
        for item in snapshot["recent_changes"]
    )


@pytest.mark.asyncio
async def test_memory_service_refreshes_summary_without_llm(monkeypatch):
    service = ConversationMemoryService()
    context = ConversationContext(session_id="s2", hotel_code="DEFAULT")

    monkeypatch.setattr(settings, "openai_api_key", "")

    for idx in range(8):
        context.add_message(MessageRole.USER, f"User message {idx}")
        context.add_message(MessageRole.ASSISTANT, f"Assistant reply {idx}")

    await service.maybe_refresh_summary(context)
    snapshot = service.get_snapshot(context)

    assert snapshot["summary"]
    assert snapshot["last_summarized_count"] == len(context.messages)


def test_merge_with_internal_keeps_reserved_memory():
    service = ConversationMemoryService()
    merged = service.merge_with_internal(
        {"request_type": "room service"},
        {"_memory": {"summary": "hello"}, "_clarification_attempts": 2},
    )

    assert merged["request_type"] == "room service"
    assert "_memory" in merged
    assert merged["_memory"]["summary"] == "hello"


def test_capture_assistant_message_stores_booking_and_order_facts():
    service = ConversationMemoryService()
    context = ConversationContext(session_id="s3", hotel_code="DEFAULT")

    service.capture_assistant_message(
        context,
        "Booking confirmed",
        metadata={
            "booking_ref": "BK-ABCD1234",
            "booking_restaurant": "Kadak",
            "booking_party_size": "4",
            "booking_time": "10 PM",
            "booking_date": "tomorrow",
        },
    )
    service.capture_assistant_message(
        context,
        "Order confirmed",
        metadata={
            "order_id": "ORD-7788",
            "order_items": ["Margherita Pizza"],
            "order_total": 450,
            "restaurant_id": 12,
        },
    )

    snapshot = service.get_snapshot(context)
    facts = snapshot["facts"]

    assert facts["last_booking_ref"] == "BK-ABCD1234"
    assert facts["latest_booking"]["restaurant"] == "Kadak"
    assert facts["latest_order"]["id"] == "ORD-7788"
    assert facts["latest_order"]["items"] == ["Margherita Pizza"]


def test_contextualize_follow_up_query_uses_previous_message_and_facts():
    service = ConversationMemoryService()
    context = ConversationContext(session_id="s4", hotel_code="DEFAULT")
    context.add_message(MessageRole.USER, "Do you provide airport transfer and what is the price?")
    context.add_message(MessageRole.ASSISTANT, "Yes, we provide airport transfer.")
    context.add_message(MessageRole.USER, "how much cost")

    service.capture_user_message(context, "Do you provide airport transfer and what is the price?")
    rewritten = service.contextualize_follow_up_query(context, "how much cost")

    assert rewritten["rewritten"] is True
    assert "Context:" in rewritten["query"]
    assert "previous_request=" in rewritten["query"]


def test_capture_assistant_message_stores_guest_preferences():
    service = ConversationMemoryService()
    context = ConversationContext(session_id="s5", hotel_code="DEFAULT")

    service.capture_assistant_message(
        context,
        "Ticket created",
        metadata={
            "ticket_id": "LOCAL-9",
            "guest_preferences": ["Non-spicy food preference", "Vegetarian diet"],
        },
    )

    snapshot = service.get_snapshot(context)
    facts = snapshot["facts"]
    assert facts.get("guest_preferences") == ["non-spicy food preference", "vegetarian diet"]
