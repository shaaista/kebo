"""
Menu CRUD service using async SQLAlchemy.
"""

from __future__ import annotations

from typing import List

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Restaurant, MenuItem
from schemas.admin_schemas import (
    RestaurantCreate,
    RestaurantUpdate,
    MenuItemCreate,
    MenuItemUpdate,
)


class MenuService:
    """Service layer for restaurant and menu item operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ===== Restaurant operations =====
    async def get_restaurant(self, restaurant_id: int) -> Restaurant:
        stmt = select(Restaurant).where(Restaurant.id == restaurant_id)
        result = await self.session.execute(stmt)
        restaurant = result.scalar_one_or_none()
        if not restaurant:
            raise ValueError("Restaurant not found")
        return restaurant

    async def get_restaurants_by_hotel(self, hotel_id: int) -> list[Restaurant]:
        stmt = select(Restaurant).where(Restaurant.hotel_id == hotel_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create_restaurant(self, data: RestaurantCreate) -> Restaurant:
        restaurant = Restaurant(**data.model_dump())
        self.session.add(restaurant)
        await self.session.commit()
        await self.session.refresh(restaurant)
        return restaurant

    async def update_restaurant(self, restaurant_id: int, data: RestaurantUpdate) -> Restaurant:
        restaurant = await self.get_restaurant(restaurant_id)
        updates = data.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(restaurant, key, value)
        await self.session.commit()
        await self.session.refresh(restaurant)
        return restaurant

    # ===== Menu item operations =====
    async def get_menu_item(self, item_id: int) -> MenuItem:
        stmt = select(MenuItem).where(MenuItem.id == item_id)
        result = await self.session.execute(stmt)
        item = result.scalar_one_or_none()
        if not item:
            raise ValueError("Menu item not found")
        return item

    async def get_menu_by_restaurant(self, restaurant_id: int) -> list[MenuItem]:
        stmt = select(MenuItem).where(MenuItem.restaurant_id == restaurant_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_menu_items(self, restaurant_id: int, query: str) -> list[MenuItem]:
        like_query = f"%{query.strip()}%"
        stmt = (
            select(MenuItem)
            .where(MenuItem.restaurant_id == restaurant_id)
            .where(
                or_(
                    MenuItem.name.ilike(like_query),
                    MenuItem.description.ilike(like_query),
                )
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create_menu_item(self, data: MenuItemCreate) -> MenuItem:
        payload = data.model_dump()
        name_key, category_key = self._menu_identity(payload.get("name"), payload.get("category"))

        existing = await self._find_menu_item_by_identity(
            restaurant_id=int(payload["restaurant_id"]),
            name_key=name_key,
            category_key=category_key,
        )
        if existing:
            # Upsert behavior to enforce uniqueness by identity.
            for field, value in payload.items():
                if field in {"restaurant_id", "name", "category"}:
                    continue
                setattr(existing, field, value)
            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        item = MenuItem(**payload)
        self.session.add(item)
        await self.session.commit()
        await self.session.refresh(item)
        return item

    async def bulk_create_menu_items(self, items: list[MenuItemCreate]) -> list[MenuItem]:
        # Deduplicate incoming payload by (restaurant_id, normalized_name, normalized_category).
        deduped_payloads: dict[tuple[int, str, str], MenuItemCreate] = {}
        for item in items:
            item_data = item.model_dump()
            name_key, category_key = self._menu_identity(item_data.get("name"), item_data.get("category"))
            key = (int(item_data["restaurant_id"]), name_key, category_key)
            deduped_payloads[key] = item

        created: List[MenuItem] = []
        for item in deduped_payloads.values():
            upserted = await self.create_menu_item(item)
            created.append(upserted)
        return created

    async def update_menu_item(self, item_id: int, data: MenuItemUpdate) -> MenuItem:
        item = await self.get_menu_item(item_id)
        updates = data.model_dump(exclude_unset=True)

        target_restaurant_id = int(updates.get("restaurant_id", item.restaurant_id))
        target_name = updates.get("name", item.name)
        target_category = updates.get("category", item.category)
        name_key, category_key = self._menu_identity(target_name, target_category)
        conflict = await self._find_menu_item_by_identity(
            restaurant_id=target_restaurant_id,
            name_key=name_key,
            category_key=category_key,
            exclude_item_id=item.id,
        )
        if conflict is not None:
            raise ValueError("Menu item with the same name/category already exists for this restaurant")

        for key, value in updates.items():
            setattr(item, key, value)
        await self.session.commit()
        await self.session.refresh(item)
        return item

    async def delete_menu_item(self, item_id: int) -> bool:
        stmt = select(MenuItem).where(MenuItem.id == item_id)
        result = await self.session.execute(stmt)
        item = result.scalar_one_or_none()
        if not item:
            return False
        await self.session.delete(item)
        await self.session.commit()
        return True

    @staticmethod
    def _menu_identity(name: str | None, category: str | None) -> tuple[str, str]:
        normalized_name = str(name or "").strip().lower()
        normalized_category = str(category or "").strip().lower()
        return normalized_name, normalized_category

    async def _find_menu_item_by_identity(
        self,
        restaurant_id: int,
        name_key: str,
        category_key: str,
        exclude_item_id: int | None = None,
    ) -> MenuItem | None:
        stmt = (
            select(MenuItem)
            .where(MenuItem.restaurant_id == restaurant_id)
            .where(func.lower(MenuItem.name) == name_key)
            .where(func.lower(func.coalesce(MenuItem.category, "")) == category_key)
        )
        if exclude_item_id is not None:
            stmt = stmt.where(MenuItem.id != exclude_item_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
