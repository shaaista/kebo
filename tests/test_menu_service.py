import pytest
from sqlalchemy import select, func

from models.database import Hotel, Restaurant, MenuItem
from schemas.admin_schemas import MenuItemCreate, MenuItemUpdate
from services.menu_service import MenuService


@pytest.mark.asyncio
async def test_create_menu_item_upserts_duplicate_identity(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    restaurant = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining")
    async_session.add(restaurant)
    await async_session.commit()
    await async_session.refresh(restaurant)

    service = MenuService(async_session)
    first = await service.create_menu_item(
        MenuItemCreate(
            restaurant_id=restaurant.id,
            name="Margherita Pizza",
            category="Pizza",
            price=450,
            currency="INR",
            is_vegetarian=True,
        )
    )
    second = await service.create_menu_item(
        MenuItemCreate(
            restaurant_id=restaurant.id,
            name="margherita pizza",
            category="pizza",
            price=470,
            currency="INR",
            is_vegetarian=True,
        )
    )

    assert first.id == second.id
    assert float(second.price) == 470.0

    count_stmt = select(func.count(MenuItem.id)).where(MenuItem.restaurant_id == restaurant.id)
    total_rows = int((await async_session.execute(count_stmt)).scalar() or 0)
    assert total_rows == 1


@pytest.mark.asyncio
async def test_bulk_create_menu_items_deduplicates_payload(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    restaurant = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining")
    async_session.add(restaurant)
    await async_session.commit()
    await async_session.refresh(restaurant)

    service = MenuService(async_session)
    created = await service.bulk_create_menu_items(
        [
            MenuItemCreate(
                restaurant_id=restaurant.id,
                name="Caesar Salad",
                category="Salads",
                price=320,
                currency="INR",
                is_vegetarian=True,
            ),
            MenuItemCreate(
                restaurant_id=restaurant.id,
                name="caesar salad",
                category="salads",
                price=340,
                currency="INR",
                is_vegetarian=True,
            ),
            MenuItemCreate(
                restaurant_id=restaurant.id,
                name="Butter Chicken",
                category="Indian Main",
                price=520,
                currency="INR",
                is_vegetarian=False,
            ),
        ]
    )

    assert len(created) == 2

    rows = (
        await async_session.execute(
            select(MenuItem).where(MenuItem.restaurant_id == restaurant.id).order_by(MenuItem.id.asc())
        )
    ).scalars().all()
    assert len(rows) == 2
    names = {row.name.lower() for row in rows}
    assert "caesar salad" in names
    assert "butter chicken" in names


@pytest.mark.asyncio
async def test_update_menu_item_rejects_conflicting_identity(async_session):
    hotel = Hotel(code="DEFAULT", name="Demo", city="Mumbai")
    async_session.add(hotel)
    await async_session.flush()

    restaurant = Restaurant(hotel_id=hotel.id, code="ird", name="In-Room Dining")
    async_session.add(restaurant)
    await async_session.commit()
    await async_session.refresh(restaurant)

    service = MenuService(async_session)
    pasta = await service.create_menu_item(
        MenuItemCreate(
            restaurant_id=restaurant.id,
            name="Pasta Alfredo",
            category="Pasta",
            price=450,
            currency="INR",
            is_vegetarian=False,
        )
    )
    _pizza = await service.create_menu_item(
        MenuItemCreate(
            restaurant_id=restaurant.id,
            name="Margherita Pizza",
            category="Pizza",
            price=420,
            currency="INR",
            is_vegetarian=True,
        )
    )

    with pytest.raises(ValueError):
        await service.update_menu_item(
            pasta.id,
            MenuItemUpdate(name="margherita pizza", category="pizza"),
        )
