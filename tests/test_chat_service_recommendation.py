import pytest

from models.database import Hotel, MenuItem, Restaurant
from schemas.chat import ConversationContext, ConversationState
from services.chat_service import ChatService


@pytest.mark.asyncio
async def test_menu_recommendation_returns_nonveg_items(async_session):
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
                name="Butter Chicken",
                price=520,
                category="Indian Main",
                is_vegetarian=False,
                is_available=True,
            ),
            MenuItem(
                restaurant_id=restaurant.id,
                name="Paneer Tikka Masala",
                price=420,
                category="Indian Main",
                is_vegetarian=True,
                is_available=True,
            ),
        ]
    )
    await async_session.commit()

    service = ChatService()
    matched = await service._match_menu_recommendation_response(
        message="recommend something nonveg",
        context=ConversationContext(
            session_id="rec1",
            hotel_code="DEFAULT",
            state=ConversationState.IDLE,
        ),
        capabilities_summary={"hotel_name": "Demo"},
        db_session=async_session,
    )

    assert matched is not None
    assert matched["match_type"] == "menu_recommendation"
    assert "Butter Chicken" in matched["response_text"]
    assert "Paneer Tikka Masala" not in matched["response_text"]
