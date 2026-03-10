from services.chat_service import ChatService


def test_extract_order_slot_updates_accepts_bare_item_reply():
    service = ChatService()

    updates = service._extract_order_slot_updates(
        message="rainbow veggie pizza",
        response_text="",
        current_pending_action="collect_order_item",
        pending_data={},
    )

    assert updates.get("order_item", "").lower() == "rainbow veggie pizza"


def test_extract_order_slot_updates_resolves_deictic_reply_from_single_option():
    service = ChatService()

    updates = service._extract_order_slot_updates(
        message="order that only",
        response_text="",
        current_pending_action="collect_order_item",
        pending_data={"order_option_candidates": ["Rainbow Veggie Pizza"]},
    )

    assert updates.get("order_item") == "Rainbow Veggie Pizza"
    assert updates.get("order_item", "").lower() != "that only"


def test_extract_order_slot_updates_does_not_literalize_deictic_without_context():
    service = ChatService()

    updates = service._extract_order_slot_updates(
        message="order that only",
        response_text="",
        current_pending_action="collect_order_item",
        pending_data={},
    )

    assert updates.get("order_item", "").lower() != "that only"
