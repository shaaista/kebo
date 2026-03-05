import pytest

import handlers.order_handler as order_handler_module
from handlers.menu_handler import MenuHandler
from handlers.order_handler import OrderHandler
from models.database import Hotel, MenuItem, Restaurant
from schemas.chat import ConversationContext, IntentResult, IntentType, ConversationState
from services.ticketing_service import TicketingResult


@pytest.mark.asyncio
async def test_order_handler_ignores_inactive_restaurant_items(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    active_restaurant = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining", is_active=True)
    inactive_restaurant = Restaurant(hotel_id=hotel.id, code="old", name="Old Outlet", is_active=False)
    async_session.add_all([active_restaurant, inactive_restaurant])
    await async_session.flush()

    async_session.add(
        MenuItem(
            restaurant_id=inactive_restaurant.id,
            name="Classic Burger",
            price=350,
            category="Burgers",
            is_available=True,
        )
    )
    await async_session.commit()

    handler = OrderHandler()
    result = await handler.handle(
        "burger",
        IntentResult(intent=IntentType.ORDER_FOOD, confidence=0.9, entities={"items": ["burger"]}),
        ConversationContext(session_id="oc1", hotel_code="DEFAULT"),
        capabilities={"hotel_name": "Demo"},
        db_session=async_session,
    )

    assert result is not None
    assert "couldn't find any of the requested items" in result.response_text.lower()
    assert result.pending_action is None
    assert result.next_state == ConversationState.AWAITING_INFO


@pytest.mark.asyncio
async def test_order_handler_fuzzy_matches_typo_item_name(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    restaurant = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining", is_active=True)
    async_session.add(restaurant)
    await async_session.flush()

    async_session.add(
        MenuItem(
            restaurant_id=restaurant.id,
            name="Chicken Pizza",
            price=550,
            category="Pizza",
            is_available=True,
        )
    )
    await async_session.commit()

    handler = OrderHandler()
    result = await handler.handle(
        "i want hcicken pizza",
        IntentResult(intent=IntentType.ORDER_FOOD, confidence=0.9, entities={"items": ["hcicken pizza"]}),
        ConversationContext(session_id="oc2", hotel_code="DEFAULT"),
        capabilities={"hotel_name": "Demo"},
        db_session=async_session,
    )

    assert result is not None
    assert "order summary" in result.response_text.lower()
    assert "Chicken Pizza" in result.response_text


@pytest.mark.asyncio
async def test_order_handler_confirmation_handles_string_pending_items_without_crash():
    handler = OrderHandler()
    context = ConversationContext(
        session_id="oc3",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_order",
        pending_data={"items": "rainbow veggie pizza"},
    )

    result = await handler.handle(
        "yes",
        IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context,
        capabilities={"hotel_name": "Demo"},
        db_session=None,
    )

    assert result is not None
    assert "order has been confirmed" in result.response_text.lower()
    assert "rainbow veggie pizza" in result.response_text.lower()
    assert "sorry, something went wrong" not in result.response_text.lower()


@pytest.mark.asyncio
async def test_order_handler_confirmation_rebuilds_slot_based_pending_items():
    handler = OrderHandler()
    context = ConversationContext(
        session_id="oc4",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_order",
        pending_data={
            "order_item": "Low Carb Pizza Bowl",
            "order_quantity": 2,
            "order_total": 1300,
        },
    )

    result = await handler.handle(
        "yes confirm",
        IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context,
        capabilities={"hotel_name": "Demo"},
        db_session=None,
    )

    assert result is not None
    assert "order has been confirmed" in result.response_text.lower()
    assert "low carb pizza bowl" in result.response_text.lower()
    assert "total: rs.1300" in result.response_text.lower()


@pytest.mark.asyncio
async def test_order_handler_confirmation_creates_operational_ticket_metadata(monkeypatch):
    handler = OrderHandler()
    context = ConversationContext(
        session_id="oc5",
        hotel_code="DEFAULT",
        state=ConversationState.AWAITING_CONFIRMATION,
        pending_action="confirm_order",
        pending_data={
            "order_item": "Shrimp Katsu",
            "order_quantity": 3,
            "order_total": 1875,
        },
    )

    monkeypatch.setattr(
        order_handler_module.ticketing_service,
        "is_ticketing_enabled",
        lambda _capabilities=None: True,
    )
    monkeypatch.setattr(
        order_handler_module.ticketing_agent_service,
        "get_configured_cases",
        lambda: ["food order"],
    )
    async def _fake_match_case_async(**_kwargs):
        return "food order"
    monkeypatch.setattr(
        order_handler_module.ticketing_agent_service,
        "match_configured_case_async",
        _fake_match_case_async,
    )
    monkeypatch.setattr(
        order_handler_module.ticketing_service,
        "build_lumira_ticket_payload",
        lambda **_kwargs: {"issue": "order ticket"},
    )

    async def _fake_create_ticket(_payload):
        return TicketingResult(
            success=True,
            ticket_id="LOCAL-77",
            status_code=200,
            response={"status": "success"},
        )

    monkeypatch.setattr(order_handler_module.ticketing_service, "create_ticket", _fake_create_ticket)

    result = await handler.handle(
        "yes confirm",
        IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context,
        capabilities={"hotel_name": "Demo"},
        db_session=None,
    )

    assert result is not None
    assert result.metadata.get("ticket_created") is True
    assert result.metadata.get("ticket_id") == "LOCAL-77"
    assert result.metadata.get("ticket_source") == "order_handler"


@pytest.mark.asyncio
async def test_menu_handler_filters_requested_item_options(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    restaurant = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining", is_active=True)
    async_session.add(restaurant)
    await async_session.flush()

    async_session.add_all(
        [
            MenuItem(
                restaurant_id=restaurant.id,
                name="Classic Burger",
                price=350,
                category="Burgers",
                is_available=True,
            ),
            MenuItem(
                restaurant_id=restaurant.id,
                name="Margherita Pizza",
                price=450,
                category="Pizza",
                is_available=True,
            ),
        ]
    )
    await async_session.commit()

    handler = MenuHandler()
    result = await handler.handle(
        "burger options",
        IntentResult(intent=IntentType.MENU_REQUEST, confidence=0.9, entities={}),
        ConversationContext(session_id="mc1", hotel_code="DEFAULT"),
        capabilities={"hotel_name": "Demo"},
        db_session=async_session,
    )

    assert "Classic Burger" in result.response_text
    assert "Margherita Pizza" not in result.response_text


@pytest.mark.asyncio
async def test_menu_handler_show_menu_pending_yes_returns_full_menu(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    restaurant = Restaurant(
        hotel_id=hotel.id,
        code="ird",
        name="In-Room Dining",
        is_active=True,
        delivers_to_room=True,
    )
    async_session.add(restaurant)
    await async_session.flush()

    async_session.add(
        MenuItem(
            restaurant_id=restaurant.id,
            name="Paneer Tikka",
            price=420,
            category="Indian",
            is_available=True,
            is_vegetarian=True,
        )
    )
    await async_session.commit()

    handler = MenuHandler()
    context = ConversationContext(
        session_id="mc2",
        hotel_code="DEFAULT",
        pending_action="show_menu",
        state=ConversationState.AWAITING_CONFIRMATION,
    )
    result = await handler.handle(
        "yes",
        IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95, entities={}),
        context,
        capabilities={"hotel_name": "Demo"},
        db_session=async_session,
    )

    assert "In-Room Dining" in result.response_text
    assert result.next_state == ConversationState.IDLE


@pytest.mark.asyncio
async def test_menu_handler_ignores_generic_restaurant_term(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    restaurant = Restaurant(hotel_id=hotel.id, code="kadak", name="Kadak", is_active=True)
    async_session.add(restaurant)
    await async_session.flush()

    async_session.add(
        MenuItem(
            restaurant_id=restaurant.id,
            name="Masala Chai",
            price=120,
            category="Beverages",
            is_available=True,
            is_vegetarian=True,
        )
    )
    await async_session.commit()

    handler = MenuHandler()
    result = await handler.handle(
        "restaurant menu",
        IntentResult(
            intent=IntentType.MENU_REQUEST,
            confidence=0.9,
            entities={"restaurant": "restaurant"},
        ),
        ConversationContext(session_id="mc3", hotel_code="DEFAULT"),
        capabilities={"hotel_name": "Demo", "restaurants": [{"name": "Kadak", "is_active": True}]},
        db_session=async_session,
    )

    assert "Kadak" in result.response_text
    assert "couldn't find a restaurant matching" not in result.response_text.lower()


@pytest.mark.asyncio
async def test_menu_handler_ignores_stale_restaurant_entity_not_in_user_message(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    kadak = Restaurant(hotel_id=hotel.id, code="kadak", name="Kadak", is_active=True)
    ird = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining", is_active=True, delivers_to_room=True)
    async_session.add_all([kadak, ird])
    await async_session.flush()

    async_session.add_all(
        [
            MenuItem(restaurant_id=kadak.id, name="Rogan Josh", price=620, category="Main", is_available=True),
            MenuItem(restaurant_id=ird.id, name="Margherita Pizza", price=450, category="Pizza", is_available=True),
        ]
    )
    await async_session.commit()

    handler = MenuHandler()
    result = await handler.handle(
        "menu",
        IntentResult(intent=IntentType.MENU_REQUEST, confidence=0.9, entities={"restaurant": "kadak"}),
        ConversationContext(session_id="mc4", hotel_code="DEFAULT"),
        capabilities={
            "hotel_name": "Demo",
            "restaurants": [
                {"name": "Kadak", "is_active": True},
                {"name": "In-Room Dining", "is_active": True},
            ],
        },
        db_session=async_session,
    )

    assert "Kadak" in result.response_text
    assert "In-Room Dining" in result.response_text


@pytest.mark.asyncio
async def test_menu_handler_resolves_in_room_menu_to_in_room_dining(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    ird = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining", is_active=True, delivers_to_room=True)
    kadak = Restaurant(hotel_id=hotel.id, code="kadak", name="Kadak", is_active=True)
    async_session.add_all([ird, kadak])
    await async_session.flush()

    async_session.add_all(
        [
            MenuItem(restaurant_id=ird.id, name="Chicken Pizza", price=550, category="Pizza", is_available=True),
            MenuItem(restaurant_id=kadak.id, name="Dal Makhani", price=380, category="Main", is_available=True),
        ]
    )
    await async_session.commit()

    handler = MenuHandler()
    result = await handler.handle(
        "in room menu",
        IntentResult(intent=IntentType.MENU_REQUEST, confidence=0.9, entities={}),
        ConversationContext(session_id="mc5", hotel_code="DEFAULT"),
        capabilities={
            "hotel_name": "Demo",
            "restaurants": [
                {"name": "In-Room Dining", "is_active": True},
                {"name": "Kadak", "is_active": True},
            ],
        },
        db_session=async_session,
    )

    assert "In-Room Dining" in result.response_text
    assert "Chicken Pizza" in result.response_text
