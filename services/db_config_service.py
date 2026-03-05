"""
Database-backed Configuration Service

Stores all business configuration in MySQL database.
Falls back to JSON file if database is unavailable.
"""

import json
import re
from typing import Optional, Dict, Any, List
from pathlib import Path

from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Hotel, Restaurant, MenuItem, BusinessConfig, Capability, Intent,
    AsyncSessionLocal, engine
)


# JSON config file path (fallback)
CONFIG_DIR = Path(__file__).parent.parent / "config"
BUSINESS_CONFIG_FILE = CONFIG_DIR / "business_config.json"


class DBConfigService:
    """
    Database-backed configuration service.

    Stores in these tables:
    - new_bot_hotels: Hotel basic info (name, city, timezone)
    - new_bot_business_config: Key-value config (welcome_message, bot_name, etc.)
    - new_bot_capabilities: Capability settings per hotel
    - new_bot_intents: Intent settings per hotel
    - new_bot_restaurants: Services/outlets
    - new_bot_menu_items: Menu items per restaurant
    """

    def __init__(self):
        self._current_hotel_id: Optional[int] = None
        self._json_config: Optional[Dict] = None

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        """Normalize IDs to stable lowercase snake-style identifiers."""
        return str(value or "").strip().lower().replace(" ", "_")

    @classmethod
    def _normalize_phase_identifier(cls, value: Any) -> str:
        """Normalize phase IDs and map legacy aliases to canonical values."""
        normalized = cls._normalize_identifier(value)
        aliases = {
            "prebooking": "pre_booking",
            "booking": "pre_checkin",
            "precheckin": "pre_checkin",
            "duringstay": "during_stay",
            "instay": "during_stay",
            "in_stay": "during_stay",
            "postcheckout": "post_checkout",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _normalize_slug(value: Any) -> str:
        """Normalize free-form text to URL/id-safe slug."""
        lowered = str(value or "").strip().lower()
        return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")

    @staticmethod
    def _normalize_ticketing_cases(raw_cases: Any) -> list[str]:
        """Normalize ticketing cases from strings/object rows."""
        if not isinstance(raw_cases, list):
            return []

        cleaned: list[str] = []
        for item in raw_cases:
            if isinstance(item, dict):
                text = str(item.get("description") or item.get("case") or item.get("label") or "").strip()
            else:
                text = str(item or "").strip()
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text)
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned[:40]

    async def get_session(self) -> AsyncSession:
        """Get a database session."""
        return AsyncSessionLocal()

    # ==================== HOTEL MANAGEMENT ====================

    async def get_or_create_hotel(self, code: str = "DEFAULT") -> int:
        """Get or create the current hotel, return hotel_id (int)."""
        async with AsyncSessionLocal() as session:
            # Check if hotel exists
            result = await session.execute(
                select(Hotel).where(Hotel.code == code)
            )
            hotel = result.scalar_one_or_none()

            if hotel:
                self._current_hotel_id = hotel.id
                return hotel.id

            # Create new hotel from JSON config
            json_config = self._load_json_config()
            business = json_config.get("business", {})

            new_hotel = Hotel(
                code=code,
                name=business.get("name", "My Business"),
                city=business.get("city", "City"),
                timezone=business.get("timezone", "Asia/Kolkata"),
                is_active=True,
            )
            session.add(new_hotel)
            await session.commit()
            await session.refresh(new_hotel)

            self._current_hotel_id = new_hotel.id

            # Sync rest of config to DB
            await self._sync_json_to_db(new_hotel.id, json_config)

            return new_hotel.id

    async def get_current_hotel_id(self) -> int:
        """Get current hotel ID, creating if needed."""
        if self._current_hotel_id:
            return self._current_hotel_id
        return await self.get_or_create_hotel("DEFAULT")

    # ==================== BUSINESS INFO ====================

    async def get_business_info(self) -> Dict[str, Any]:
        """Get business info from database."""
        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            # Get hotel basic info
            result = await session.execute(
                select(Hotel).where(Hotel.id == hotel_id)
            )
            hotel = result.scalar_one_or_none()

            if not hotel:
                return self._load_json_config().get("business", {})

            # Get additional config values
            result = await session.execute(
                select(BusinessConfig).where(BusinessConfig.hotel_id == hotel_id)
            )
            configs = result.scalars().all()
            config_dict = {c.config_key: c.config_value for c in configs}

            return {
                "id": str(hotel.id),
                "name": hotel.name,
                "city": hotel.city,
                "timezone": hotel.timezone,
                "type": config_dict.get("business.type", "hotel"),
                "bot_name": config_dict.get("business.bot_name", "Assistant"),
                "welcome_message": config_dict.get("business.welcome_message", "Hello! How can I help you?"),
                "currency": config_dict.get("business.currency", "INR"),
                "language": config_dict.get("business.language", "en"),
            }

    async def update_business_info(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update business info in database AND JSON file."""

        # ALWAYS update JSON file first (this is the reliable backup)
        self._update_json_config("business", updates)

        # Then try to update database
        try:
            hotel_id = await self.get_current_hotel_id()

            async with AsyncSessionLocal() as session:
                # Update hotel table for name, city, timezone
                if any(k in updates for k in ["name", "city", "timezone"]):
                    hotel_updates = {}
                    if "name" in updates:
                        hotel_updates["name"] = updates["name"]
                    if "city" in updates:
                        hotel_updates["city"] = updates["city"]
                    if "timezone" in updates:
                        hotel_updates["timezone"] = updates["timezone"]

                    if hotel_updates:
                        await session.execute(
                            update(Hotel).where(Hotel.id == hotel_id).values(**hotel_updates)
                        )

                # Update business_config table for other fields
                config_fields = ["type", "bot_name", "welcome_message", "currency", "language"]
                for field in config_fields:
                    if field in updates:
                        config_key = f"business.{field}"

                        # Check if exists
                        result = await session.execute(
                            select(BusinessConfig).where(
                                BusinessConfig.hotel_id == hotel_id,
                                BusinessConfig.config_key == config_key
                            )
                        )
                        existing = result.scalar_one_or_none()

                        if existing:
                            existing.config_value = updates[field]
                        else:
                            new_config = BusinessConfig(
                                hotel_id=hotel_id,
                                config_key=config_key,
                                config_value=updates[field]
                            )
                            session.add(new_config)

                await session.commit()
                print(f"[DB] Updated business info: {list(updates.keys())}")

        except Exception as e:
            print(f"[DB] Error updating (JSON still saved): {e}")

        return await self.get_business_info()

    # ==================== CAPABILITIES ====================

    async def get_capabilities(self) -> Dict[str, Any]:
        """Get all capabilities from database."""
        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Capability).where(Capability.hotel_id == hotel_id)
            )
            capabilities = result.scalars().all()

            if not capabilities:
                # Return from JSON if DB empty
                return self._load_json_config().get("capabilities", {})

            return {
                cap.capability_id: {
                    "enabled": cap.enabled,
                    "description": cap.description or "",
                    "hours": cap.hours,
                }
                for cap in capabilities
            }

    async def update_capability(self, capability_id: str, updates: Dict[str, Any]) -> bool:
        """Update a capability in database AND JSON file."""

        # ALWAYS update JSON first
        json_config = self._load_json_config()
        if "capabilities" not in json_config:
            json_config["capabilities"] = {}
        if capability_id not in json_config["capabilities"]:
            json_config["capabilities"][capability_id] = {}
        json_config["capabilities"][capability_id].update(updates)
        self._save_json_config(json_config)
        print(f"[JSON] Updated capability {capability_id}: {updates}")

        # Then try database
        try:
            hotel_id = await self.get_current_hotel_id()

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Capability).where(
                        Capability.hotel_id == hotel_id,
                        Capability.capability_id == capability_id
                    )
                )
                cap = result.scalar_one_or_none()

                if cap:
                    if "enabled" in updates:
                        cap.enabled = updates["enabled"]
                    if "description" in updates:
                        cap.description = updates["description"]
                    if "hours" in updates:
                        cap.hours = updates["hours"]
                    await session.commit()
                    print(f"[DB] Updated capability {capability_id}")
                    return True

                # Create new if doesn't exist
                new_cap = Capability(
                    hotel_id=hotel_id,
                    capability_id=capability_id,
                    enabled=updates.get("enabled", True),
                    description=updates.get("description", ""),
                    hours=updates.get("hours"),
                )
                session.add(new_cap)
                await session.commit()
                print(f"[DB] Created capability {capability_id}")
                return True

        except Exception as e:
            print(f"[DB] Error updating capability (JSON still saved): {e}")
            return True  # JSON was saved, so return True

    async def add_capability(self, capability_id: str, data: Dict[str, Any]) -> bool:
        """Add a new capability."""
        return await self.update_capability(capability_id, data)

    async def delete_capability(self, capability_id: str) -> bool:
        """Delete a capability."""
        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(Capability).where(
                    Capability.hotel_id == hotel_id,
                    Capability.capability_id == capability_id
                )
            )
            await session.commit()
            return True

    # ==================== SERVICES/RESTAURANTS ====================

    async def get_services(self) -> List[Dict[str, Any]]:
        """Get all services (restaurants) from database."""
        hotel_id = await self.get_current_hotel_id()
        json_services = self._load_json_config().get("services", [])

        normalized_json: List[Dict[str, Any]] = []
        json_by_id: Dict[str, Dict[str, Any]] = {}
        for service in json_services:
            if not isinstance(service, dict):
                continue
            service_id = self._normalize_identifier(service.get("id"))
            if not service_id:
                continue
            normalized = dict(service)
            normalized["id"] = service_id
            normalized["name"] = str(service.get("name") or service_id).strip()
            normalized["type"] = self._normalize_identifier(service.get("type") or "service")
            normalized["description"] = str(service.get("description") or "").strip()
            normalized["is_active"] = bool(service.get("is_active", True))
            phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if phase_id:
                normalized["phase_id"] = phase_id
            elif "phase_id" in normalized:
                normalized.pop("phase_id", None)
            normalized_json.append(normalized)
            json_by_id[service_id] = normalized

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Restaurant).where(Restaurant.hotel_id == hotel_id)
            )
            restaurants = result.scalars().all()

            if not restaurants:
                return normalized_json
            if not normalized_json:
                # JSON is the authoritative service list. If it is empty, treat
                # DB-only rows as legacy/orphaned and do not expose them in admin.
                return []

            for restaurant in restaurants:
                service_id = self._normalize_identifier(restaurant.code)
                if service_id not in json_by_id:
                    # Do not reintroduce DB-only services that were removed from JSON.
                    continue
                base = dict(json_by_id.get(service_id, {}))
                hours = {
                    "open": str(restaurant.opens_at) if restaurant.opens_at else "00:00",
                    "close": str(restaurant.closes_at) if restaurant.closes_at else "23:59",
                }
                db_service = dict(base)
                db_service.update(
                    {
                    "id": service_id,
                    "name": restaurant.name,
                    "type": base.get("type", "service"),
                    "description": base.get("description") or restaurant.cuisine or "",
                    "cuisine": restaurant.cuisine or base.get("cuisine"),
                    "hours": hours,
                    "delivery_zones": ["room"] if restaurant.delivers_to_room else ["dine_in_only"],
                    "is_active": restaurant.is_active,
                    }
                )
                json_by_id[service_id] = db_service

            ordered: List[Dict[str, Any]] = []
            seen_ids: set[str] = set()
            for service in normalized_json:
                service_id = self._normalize_identifier(service.get("id"))
                if service_id in seen_ids:
                    continue
                ordered.append(json_by_id.get(service_id, service))
                seen_ids.add(service_id)

            return ordered

    async def add_service(self, service: Dict[str, Any]) -> bool:
        """Add a new service/restaurant."""
        service_id = self._normalize_identifier(service.get("id"))
        if not service_id:
            return False

        normalized = dict(service)
        normalized["id"] = service_id
        normalized["name"] = str(service.get("name") or service_id).strip()
        normalized["type"] = self._normalize_identifier(service.get("type") or "service")
        normalized["description"] = str(service.get("description") or "").strip()
        normalized["is_active"] = bool(service.get("is_active", True))
        phase_id = self._normalize_phase_identifier(normalized.get("phase_id"))
        if phase_id:
            normalized["phase_id"] = phase_id
        elif "phase_id" in normalized:
            normalized.pop("phase_id", None)

        # Always save JSON first (source of truth for richer service metadata).
        json_config = self._load_json_config()
        json_services = json_config.setdefault("services", [])
        replaced = False
        for idx, existing in enumerate(json_services):
            if self._normalize_identifier(existing.get("id")) == service_id:
                merged = dict(existing)
                merged.update(normalized)
                json_services[idx] = merged
                replaced = True
                break
        if not replaced:
            json_services.append(normalized)
        self._save_json_config(json_config)
        print(f"[JSON] Upserted service {service_id}")

        if normalized["type"] != "restaurant":
            return True

        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            from datetime import time

            hours = normalized.get("hours", {})
            opens_at = None
            closes_at = None

            if hours.get("open"):
                try:
                    parts = hours["open"].split(":")
                    opens_at = time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
                except:
                    pass

            if hours.get("close"):
                try:
                    parts = hours["close"].split(":")
                    closes_at = time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
                except:
                    pass

            delivery_zones = normalized.get("delivery_zones", [])
            delivers_to_room = "room" in delivery_zones

            result = await session.execute(
                select(Restaurant).where(
                    Restaurant.hotel_id == hotel_id,
                    Restaurant.code == service_id,
                )
            )
            restaurant = result.scalar_one_or_none()

            if restaurant:
                restaurant.name = normalized.get("name", restaurant.name)
                restaurant.cuisine = normalized.get("cuisine") or normalized.get("description") or restaurant.cuisine
                restaurant.opens_at = opens_at
                restaurant.closes_at = closes_at
                restaurant.delivers_to_room = delivers_to_room
                restaurant.is_active = normalized.get("is_active", True)
            else:
                new_restaurant = Restaurant(
                    hotel_id=hotel_id,
                    code=service_id,
                    name=normalized.get("name", "New Service"),
                    cuisine=normalized.get("cuisine") or normalized.get("description"),
                    opens_at=opens_at,
                    closes_at=closes_at,
                    delivers_to_room=delivers_to_room,
                    is_active=normalized.get("is_active", True),
                )
                session.add(new_restaurant)
            await session.commit()
            print(f"[DB] Upserted service {service_id} as restaurant")
            return True

    async def update_service(self, service_id: str, updates: Dict[str, Any]) -> bool:
        """Update a service/restaurant."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return False

        # Always update JSON first for full metadata retention.
        json_config = self._load_json_config()
        json_services = json_config.setdefault("services", [])
        current: Dict[str, Any] = {"id": normalized_id, "name": normalized_id, "type": "service", "is_active": True}
        found = False
        for existing in json_services:
            if self._normalize_identifier(existing.get("id")) == normalized_id:
                current = dict(existing)
                found = True
                break
        current.update(updates)
        current["id"] = normalized_id
        phase_id = self._normalize_phase_identifier(current.get("phase_id"))
        if phase_id:
            current["phase_id"] = phase_id
        elif "phase_id" in current:
            current.pop("phase_id", None)

        if found:
            for idx, existing in enumerate(json_services):
                if self._normalize_identifier(existing.get("id")) == normalized_id:
                    json_services[idx] = current
                    break
        else:
            json_services.append(current)
        self._save_json_config(json_config)
        print(f"[JSON] Updated service {normalized_id}: {list(updates.keys())}")

        # For non-restaurant service types, JSON persistence is enough for now.
        service_type = self._normalize_identifier(current.get("type") or "service")
        if service_type != "restaurant":
            return True

        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Restaurant).where(
                    Restaurant.hotel_id == hotel_id,
                    Restaurant.code == normalized_id
                )
            )
            restaurant = result.scalar_one_or_none()

            if not restaurant:
                # If DB row doesn't exist yet, create it from merged JSON state.
                return await self.add_service(current)

            if "name" in current:
                restaurant.name = current["name"]
            if "cuisine" in current or "description" in current:
                restaurant.cuisine = current.get("cuisine") or current.get("description")
            if "is_active" in current:
                restaurant.is_active = bool(current["is_active"])
            if "delivery_zones" in current:
                restaurant.delivers_to_room = "room" in (current.get("delivery_zones") or [])
            if "hours" in current and isinstance(current.get("hours"), dict):
                from datetime import time

                open_val = str(current["hours"].get("open") or "").strip()
                close_val = str(current["hours"].get("close") or "").strip()

                if open_val:
                    try:
                        hh, mm = open_val.split(":")[:2]
                        restaurant.opens_at = time(int(hh), int(mm))
                    except Exception:
                        pass
                if close_val:
                    try:
                        hh, mm = close_val.split(":")[:2]
                        restaurant.closes_at = time(int(hh), int(mm))
                    except Exception:
                        pass

            await session.commit()
            print(f"[DB] Updated service {normalized_id}")
            return True

    async def delete_service(self, service_id: str) -> bool:
        """Delete a service/restaurant."""
        normalized_id = self._normalize_identifier(service_id)

        # Delete from JSON first.
        json_config = self._load_json_config()
        json_config["services"] = [
            service
            for service in json_config.get("services", [])
            if self._normalize_identifier(service.get("id")) != normalized_id
        ]
        self._save_json_config(json_config)
        print(f"[JSON] Deleted service {normalized_id}")

        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            try:
                restaurant_ids = list(
                    (
                        await session.execute(
                            select(Restaurant.id).where(
                                Restaurant.hotel_id == hotel_id,
                                Restaurant.code == normalized_id,
                            )
                        )
                    ).scalars().all()
                )
                if restaurant_ids:
                    # Best-effort hard delete for menu rows first.
                    await session.execute(
                        delete(MenuItem).where(MenuItem.restaurant_id.in_(restaurant_ids))
                    )
                    await session.execute(
                        delete(Restaurant).where(
                            Restaurant.hotel_id == hotel_id,
                            Restaurant.code == normalized_id,
                        )
                    )
                await session.commit()
                print(f"[DB] Deleted service {normalized_id}")
            except IntegrityError:
                # Existing orders/history can block hard delete. Soft-disable instead.
                await session.rollback()
                await session.execute(
                    update(Restaurant)
                    .where(
                        Restaurant.hotel_id == hotel_id,
                        Restaurant.code == normalized_id,
                    )
                    .values(is_active=False)
                )
                await session.commit()
                print(f"[DB] Soft-disabled service {normalized_id} (FK protected)")
            except Exception as e:
                # Any driver-specific FK/constraint error should not break admin delete UX.
                await session.rollback()
                await session.execute(
                    update(Restaurant)
                    .where(
                        Restaurant.hotel_id == hotel_id,
                        Restaurant.code == normalized_id,
                    )
                    .values(is_active=False)
                )
                await session.commit()
                print(f"[DB] Soft-disabled service {normalized_id} after delete error: {e}")
            return True

    async def clear_services(self) -> bool:
        """Delete all services/restaurants."""
        # Delete all service entries from JSON first.
        json_config = self._load_json_config()
        json_config["services"] = []
        self._save_json_config(json_config)
        print("[JSON] Cleared all services")

        hotel_id = await self.get_current_hotel_id()
        async with AsyncSessionLocal() as session:
            try:
                restaurant_ids = list(
                    (
                        await session.execute(
                            select(Restaurant.id).where(Restaurant.hotel_id == hotel_id)
                        )
                    ).scalars().all()
                )
                if restaurant_ids:
                    await session.execute(
                        delete(MenuItem).where(MenuItem.restaurant_id.in_(restaurant_ids))
                    )
                await session.execute(
                    delete(Restaurant).where(Restaurant.hotel_id == hotel_id)
                )
                await session.commit()
                print("[DB] Cleared all services/restaurants")
            except IntegrityError:
                # Keep historical rows intact and hide services operationally.
                await session.rollback()
                await session.execute(
                    update(Restaurant)
                    .where(Restaurant.hotel_id == hotel_id)
                    .values(is_active=False)
                )
                await session.commit()
                print("[DB] Soft-disabled all services/restaurants (FK protected)")
            except Exception as e:
                await session.rollback()
                await session.execute(
                    update(Restaurant)
                    .where(Restaurant.hotel_id == hotel_id)
                    .values(is_active=False)
                )
                await session.commit()
                print(f"[DB] Soft-disabled all services/restaurants after clear error: {e}")
            return True

    # ==================== FAQ BANK (JSON-FIRST) ====================

    async def get_faq_bank(self) -> List[Dict[str, Any]]:
        """Get admin FAQ bank entries (JSON source-of-truth)."""
        config = self._load_json_config()
        faq_bank = config.get("faq_bank", [])
        if not isinstance(faq_bank, list):
            return []
        return [dict(entry) for entry in faq_bank if isinstance(entry, dict)]

    async def add_faq_entry(self, faq: Dict[str, Any]) -> bool:
        """Add or upsert a FAQ entry by ID."""
        question = str(faq.get("question") or "").strip()
        answer = str(faq.get("answer") or "").strip()
        if not question or not answer:
            return False

        faq_id = self._normalize_identifier(faq.get("id")) or self._normalize_slug(question)
        if not faq_id:
            return False

        normalized = {
            "id": faq_id,
            "question": question,
            "answer": answer,
            "description": str(faq.get("description") or "").strip(),
            "tags": [self._normalize_slug(tag) for tag in faq.get("tags", []) if self._normalize_slug(tag)],
            "enabled": bool(faq.get("enabled", True)),
        }

        config = self._load_json_config()
        faq_bank = config.setdefault("faq_bank", [])
        replaced = False
        for idx, existing in enumerate(faq_bank):
            if self._normalize_identifier(existing.get("id")) == faq_id:
                merged = dict(existing)
                merged.update(normalized)
                faq_bank[idx] = merged
                replaced = True
                break
        if not replaced:
            faq_bank.append(normalized)

        self._save_json_config(config)
        return True

    async def update_faq_entry(self, faq_id: str, updates: Dict[str, Any]) -> bool:
        """Update a FAQ entry by ID."""
        normalized_id = self._normalize_identifier(faq_id)
        if not normalized_id:
            return False

        config = self._load_json_config()
        faq_bank = config.setdefault("faq_bank", [])
        for idx, existing in enumerate(faq_bank):
            if self._normalize_identifier(existing.get("id")) != normalized_id:
                continue
            merged = dict(existing)
            merged.update(updates)
            merged["id"] = normalized_id
            if "question" in merged:
                merged["question"] = str(merged.get("question") or "").strip()
            if "answer" in merged:
                merged["answer"] = str(merged.get("answer") or "").strip()
            if not merged.get("question") or not merged.get("answer"):
                return False
            faq_bank[idx] = merged
            self._save_json_config(config)
            return True
        return False

    async def delete_faq_entry(self, faq_id: str) -> bool:
        """Delete a FAQ entry by ID."""
        normalized_id = self._normalize_identifier(faq_id)
        config = self._load_json_config()
        config["faq_bank"] = [
            entry
            for entry in config.get("faq_bank", [])
            if self._normalize_identifier(entry.get("id")) != normalized_id
        ]
        self._save_json_config(config)
        return True

    # ==================== TOOLS (JSON-FIRST) ====================

    async def get_tools(self) -> List[Dict[str, Any]]:
        """Get admin tools list (JSON source-of-truth)."""
        config = self._load_json_config()
        tools = config.get("tools", [])
        if not isinstance(tools, list):
            return []
        return [dict(tool) for tool in tools if isinstance(tool, dict)]

    async def add_tool(self, tool: Dict[str, Any]) -> bool:
        """Add or upsert a tool by ID."""
        tool_id = self._normalize_identifier(tool.get("id")) or self._normalize_slug(tool.get("name"))
        if not tool_id:
            return False

        normalized = {
            "id": tool_id,
            "name": str(tool.get("name") or tool_id.replace("_", " ").title()).strip(),
            "description": str(tool.get("description") or "").strip(),
            "type": self._normalize_identifier(tool.get("type") or "workflow"),
            "handler": str(tool.get("handler") or "").strip() or None,
            "channels": [self._normalize_identifier(ch) for ch in tool.get("channels", []) if self._normalize_identifier(ch)],
            "enabled": bool(tool.get("enabled", True)),
            "requires_confirmation": bool(tool.get("requires_confirmation", False)),
        }
        if "ticketing_plugin_enabled" in tool:
            normalized["ticketing_plugin_enabled"] = bool(tool.get("ticketing_plugin_enabled", True))
        ticketing_cases = self._normalize_ticketing_cases(tool.get("ticketing_cases"))
        if ticketing_cases:
            normalized["ticketing_cases"] = ticketing_cases

        config = self._load_json_config()
        tools = config.setdefault("tools", [])
        replaced = False
        for idx, existing in enumerate(tools):
            if self._normalize_identifier(existing.get("id")) == tool_id:
                merged = dict(existing)
                merged.update(normalized)
                tools[idx] = merged
                replaced = True
                break
        if not replaced:
            tools.append(normalized)

        self._save_json_config(config)
        return True

    async def update_tool(self, tool_id: str, updates: Dict[str, Any]) -> bool:
        """Update a tool by ID."""
        normalized_id = self._normalize_identifier(tool_id)
        if not normalized_id:
            return False

        config = self._load_json_config()
        tools = config.setdefault("tools", [])
        for idx, existing in enumerate(tools):
            if self._normalize_identifier(existing.get("id")) != normalized_id:
                continue
            merged = dict(existing)
            merged.update(updates)
            merged["id"] = normalized_id
            if "name" in merged:
                merged["name"] = str(merged.get("name") or normalized_id.replace("_", " ").title()).strip()
            tools[idx] = merged
            self._save_json_config(config)
            return True
        return False

    async def delete_tool(self, tool_id: str) -> bool:
        """Delete a tool by ID."""
        normalized_id = self._normalize_identifier(tool_id)
        config = self._load_json_config()
        config["tools"] = [
            tool
            for tool in config.get("tools", [])
            if self._normalize_identifier(tool.get("id")) != normalized_id
        ]
        self._save_json_config(config)
        return True

    # ==================== INTENTS ====================

    async def get_intents(self) -> List[Dict[str, Any]]:
        """Get all intents from database."""
        hotel_id = await self.get_current_hotel_id()
        json_intents = self._load_json_config().get("intents", [])

        normalized_json: List[Dict[str, Any]] = []
        json_by_id: Dict[str, Dict[str, Any]] = {}
        for intent in json_intents:
            if not isinstance(intent, dict):
                continue
            intent_id = self._normalize_identifier(intent.get("id"))
            if not intent_id:
                continue
            normalized = dict(intent)
            normalized["id"] = intent_id
            normalized["label"] = str(intent.get("label") or intent_id.replace("_", " ").title()).strip()
            normalized["enabled"] = bool(intent.get("enabled", True))
            maps_to = self._normalize_identifier(intent.get("maps_to"))
            if maps_to and maps_to != intent_id:
                normalized["maps_to"] = maps_to
            normalized_json.append(normalized)
            json_by_id[intent_id] = normalized

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Intent).where(Intent.hotel_id == hotel_id)
            )
            intents = result.scalars().all()

            mapping_rows = await session.execute(
                select(BusinessConfig).where(
                    BusinessConfig.hotel_id == hotel_id,
                    BusinessConfig.config_key.like("intent_map.%"),
                )
            )
            mapping_entries = mapping_rows.scalars().all()
            mapping_by_id = {
                self._normalize_identifier(entry.config_key.replace("intent_map.", "", 1)): self._normalize_identifier(entry.config_value)
                for entry in mapping_entries
            }

            if not intents:
                return normalized_json

            merged: List[Dict[str, Any]] = []
            seen_ids: set[str] = set()
            for row in intents:
                row_id = self._normalize_identifier(row.intent_id)
                base = dict(json_by_id.get(row_id, {}))
                item = {
                    "id": row_id,
                    "label": row.label or base.get("label") or row_id.replace("_", " ").title(),
                    "enabled": bool(row.enabled),
                }
                maps_to = mapping_by_id.get(row_id) or self._normalize_identifier(base.get("maps_to"))
                if maps_to and maps_to != row_id:
                    item["maps_to"] = maps_to
                merged.append(item)
                seen_ids.add(row_id)

            for intent in normalized_json:
                intent_id = self._normalize_identifier(intent.get("id"))
                if intent_id in seen_ids:
                    continue
                merged.append(intent)
                seen_ids.add(intent_id)

            return merged

    async def add_intent(self, intent: Dict[str, Any]) -> bool:
        """Add a new intent (or upsert existing one)."""
        intent_id = self._normalize_identifier(intent.get("id"))
        if not intent_id:
            return False
        payload = dict(intent)
        payload["id"] = intent_id
        return await self.update_intent(intent_id, payload)

    async def update_intent(self, intent_id: str, enabled: Any) -> bool:
        """Update an intent's enabled/label/mapping fields."""
        normalized_id = self._normalize_identifier(intent_id)
        if not normalized_id:
            return False

        updates = enabled if isinstance(enabled, dict) else {"enabled": bool(enabled)}
        updates = dict(updates)
        updates["id"] = normalized_id

        # Always persist in JSON first.
        json_config = self._load_json_config()
        json_intents = json_config.setdefault("intents", [])
        found = False
        for idx, existing in enumerate(json_intents):
            if self._normalize_identifier(existing.get("id")) == normalized_id:
                merged = dict(existing)
                merged.update(updates)
                merged["id"] = normalized_id
                merged["label"] = str(merged.get("label") or normalized_id.replace("_", " ").title()).strip()
                merged["enabled"] = bool(merged.get("enabled", True))
                maps_to = self._normalize_identifier(merged.get("maps_to"))
                if maps_to and maps_to != normalized_id:
                    merged["maps_to"] = maps_to
                elif "maps_to" in merged:
                    merged.pop("maps_to", None)
                json_intents[idx] = merged
                found = True
                break
        if not found:
            new_intent = {
                "id": normalized_id,
                "label": str(updates.get("label") or normalized_id.replace("_", " ").title()).strip(),
                "enabled": bool(updates.get("enabled", True)),
            }
            maps_to = self._normalize_identifier(updates.get("maps_to"))
            if maps_to and maps_to != normalized_id:
                new_intent["maps_to"] = maps_to
            json_intents.append(new_intent)
        self._save_json_config(json_config)
        print(f"[JSON] Updated intent {normalized_id}: {list(updates.keys())}")

        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Intent).where(
                    Intent.hotel_id == hotel_id,
                    Intent.intent_id == normalized_id
                )
            )
            intent = result.scalar_one_or_none()

            if intent:
                if "enabled" in updates:
                    intent.enabled = bool(updates["enabled"])
                if "label" in updates and updates.get("label") is not None:
                    intent.label = str(updates.get("label")).strip() or intent.label
            else:
                intent = Intent(
                    hotel_id=hotel_id,
                    intent_id=normalized_id,
                    label=str(updates.get("label") or normalized_id.replace("_", " ").title()).strip(),
                    enabled=bool(updates.get("enabled", True)),
                )
                session.add(intent)

            if "maps_to" in updates:
                maps_to = self._normalize_identifier(updates.get("maps_to"))
                config_key = f"intent_map.{normalized_id}"
                existing_map = await session.execute(
                    select(BusinessConfig).where(
                        BusinessConfig.hotel_id == hotel_id,
                        BusinessConfig.config_key == config_key,
                    )
                )
                mapping_row = existing_map.scalar_one_or_none()
                if maps_to and maps_to != normalized_id:
                    if mapping_row:
                        mapping_row.config_value = maps_to
                    else:
                        session.add(
                            BusinessConfig(
                                hotel_id=hotel_id,
                                config_key=config_key,
                                config_value=maps_to,
                            )
                        )
                elif mapping_row:
                    await session.delete(mapping_row)

            await session.commit()
            print(f"[DB] Updated intent {normalized_id}")
            return True

    async def delete_intent(self, intent_id: str) -> bool:
        """Delete an intent."""
        normalized_id = self._normalize_identifier(intent_id)

        # Delete from JSON first.
        json_config = self._load_json_config()
        json_config["intents"] = [
            intent
            for intent in json_config.get("intents", [])
            if self._normalize_identifier(intent.get("id")) != normalized_id
        ]
        self._save_json_config(json_config)
        print(f"[JSON] Deleted intent {normalized_id}")

        hotel_id = await self.get_current_hotel_id()
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(Intent).where(
                    Intent.hotel_id == hotel_id,
                    Intent.intent_id == normalized_id,
                )
            )
            await session.execute(
                delete(BusinessConfig).where(
                    BusinessConfig.hotel_id == hotel_id,
                    BusinessConfig.config_key == f"intent_map.{normalized_id}",
                )
            )
            await session.commit()
            print(f"[DB] Deleted intent {normalized_id}")
            return True

    # ==================== ESCALATION ====================

    async def get_escalation_config(self) -> Dict[str, Any]:
        """Get escalation config from database."""
        hotel_id = await self.get_current_hotel_id()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BusinessConfig).where(
                    BusinessConfig.hotel_id == hotel_id,
                    BusinessConfig.config_key.like("escalation.%")
                )
            )
            configs = result.scalars().all()

            if not configs:
                return self._load_json_config().get("escalation", {})

            config_dict = {c.config_key.replace("escalation.", ""): c.config_value for c in configs}

            return {
                "confidence_threshold": float(config_dict.get("confidence_threshold", 0.4)),
                "max_clarification_attempts": int(config_dict.get("max_clarification_attempts", 3)),
                "escalation_message": config_dict.get("escalation_message", "Let me connect you with our team."),
                "modes": json.loads(config_dict.get("modes", '["live_chat", "ticket"]')),
            }

    async def update_escalation_config(self, updates: Dict[str, Any]) -> bool:
        """Update escalation config in database AND JSON file."""

        # ALWAYS update JSON first
        json_config = self._load_json_config()
        if "escalation" not in json_config:
            json_config["escalation"] = {}
        json_config["escalation"].update(updates)
        self._save_json_config(json_config)
        print(f"[JSON] Updated escalation: {list(updates.keys())}")

        # Then try database
        try:
            hotel_id = await self.get_current_hotel_id()

            async with AsyncSessionLocal() as session:
                for key, value in updates.items():
                    config_key = f"escalation.{key}"

                    # Convert to string for storage
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value)
                    else:
                        value = str(value)

                    result = await session.execute(
                        select(BusinessConfig).where(
                            BusinessConfig.hotel_id == hotel_id,
                            BusinessConfig.config_key == config_key
                        )
                    )
                    existing = result.scalar_one_or_none()

                    if existing:
                        existing.config_value = value
                    else:
                        new_config = BusinessConfig(
                            hotel_id=hotel_id,
                            config_key=config_key,
                            config_value=value
                        )
                        session.add(new_config)

                await session.commit()
                print(f"[DB] Updated escalation config")
                return True

        except Exception as e:
            print(f"[DB] Error updating escalation (JSON still saved): {e}")
            return True  # JSON was saved

    # ==================== FULL CONFIG ====================

    async def get_full_config(self) -> Dict[str, Any]:
        """Get full configuration."""
        # Start with JSON to preserve sections that are not yet normalized in DB
        # (e.g., prompts, knowledge_base, ui_settings).
        base_config = dict(self._load_json_config())
        db_business = await self.get_business_info()
        merged_business = dict(base_config.get("business", {}))
        merged_business.update({k: v for k, v in db_business.items() if v is not None})
        base_config["business"] = merged_business
        base_config["capabilities"] = await self.get_capabilities()
        base_config["services"] = await self.get_services()
        base_config["faq_bank"] = await self.get_faq_bank()
        base_config["tools"] = await self.get_tools()
        base_config["intents"] = await self.get_intents()
        base_config["escalation"] = await self.get_escalation_config()
        base_config["journey_phases"] = await self.get_journey_phases()
        return base_config

    async def save_full_config(self, config: Dict[str, Any]) -> bool:
        """Save full configuration to database."""
        try:
            # Save JSON first so onboarding-only sections are never lost.
            self._save_json_config(config)

            # Update business info
            if "business" in config:
                await self.update_business_info(config["business"])

            # Update capabilities
            if "capabilities" in config:
                for cap_id, cap_data in config["capabilities"].items():
                    await self.update_capability(cap_id, cap_data)

            # Update services
            if "services" in config and isinstance(config["services"], list):
                desired_service_ids = {
                    self._normalize_identifier(service.get("id"))
                    for service in config["services"]
                    if isinstance(service, dict) and self._normalize_identifier(service.get("id"))
                }
                existing_services = await self.get_services()
                for existing in existing_services:
                    existing_id = self._normalize_identifier(existing.get("id"))
                    if existing_id and existing_id not in desired_service_ids:
                        await self.delete_service(existing_id)
                for service in config["services"]:
                    if isinstance(service, dict):
                        await self.add_service(service)

            # Update FAQ bank (JSON-first section)
            if "faq_bank" in config and isinstance(config["faq_bank"], list):
                desired_faq_ids = {
                    self._normalize_identifier(item.get("id"))
                    for item in config["faq_bank"]
                    if isinstance(item, dict) and self._normalize_identifier(item.get("id"))
                }
                existing_faq = await self.get_faq_bank()
                for existing in existing_faq:
                    existing_id = self._normalize_identifier(existing.get("id"))
                    if existing_id and existing_id not in desired_faq_ids:
                        await self.delete_faq_entry(existing_id)
                for item in config["faq_bank"]:
                    if isinstance(item, dict):
                        await self.add_faq_entry(item)

            # Update tools (JSON-first section)
            if "tools" in config and isinstance(config["tools"], list):
                desired_tool_ids = {
                    self._normalize_identifier(item.get("id"))
                    for item in config["tools"]
                    if isinstance(item, dict) and self._normalize_identifier(item.get("id"))
                }
                existing_tools = await self.get_tools()
                for existing in existing_tools:
                    existing_id = self._normalize_identifier(existing.get("id"))
                    if existing_id and existing_id not in desired_tool_ids:
                        await self.delete_tool(existing_id)
                for item in config["tools"]:
                    if isinstance(item, dict):
                        await self.add_tool(item)

            # Update intents
            if "intents" in config and isinstance(config["intents"], list):
                desired_intent_ids = {
                    self._normalize_identifier(intent.get("id"))
                    for intent in config["intents"]
                    if isinstance(intent, dict) and self._normalize_identifier(intent.get("id"))
                }
                existing_intents = await self.get_intents()
                for existing in existing_intents:
                    existing_id = self._normalize_identifier(existing.get("id"))
                    if existing_id and existing_id not in desired_intent_ids:
                        await self.delete_intent(existing_id)
                for intent in config["intents"]:
                    if isinstance(intent, dict):
                        await self.add_intent(intent)

            # Update journey phases (JSON-first section)
            if "journey_phases" in config and isinstance(config["journey_phases"], list):
                await self.update_journey_phases(config["journey_phases"])

            # Update escalation
            if "escalation" in config:
                await self.update_escalation_config(config["escalation"])
            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False

    # ==================== JSON HELPERS ====================

    def _load_json_config(self) -> Dict[str, Any]:
        """Load config from JSON file (fallback)."""
        if BUSINESS_CONFIG_FILE.exists():
            with open(BUSINESS_CONFIG_FILE, "r", encoding="utf-8") as f:
                self._json_config = json.load(f)
        else:
            self._json_config = {
                "business": {},
                "capabilities": {},
                "services": [],
                "faq_bank": [],
                "tools": [],
                "intents": [],
                "escalation": {},
                "prompts": {},
                "knowledge_base": {},
                "ui_settings": {},
            }

        return self._json_config

    def _save_json_config(self, config: Dict[str, Any]):
        """Save config to JSON file (backup)."""
        CONFIG_DIR.mkdir(exist_ok=True)
        with open(BUSINESS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        self._json_config = config

    def _update_json_config(self, section: str, updates: Dict[str, Any]):
        """Update a section of JSON config and save immediately."""
        # Force reload from file (clear cache)
        self._json_config = None
        config = self._load_json_config()
        if section not in config:
            config[section] = {}
        config[section].update(updates)
        self._save_json_config(config)
        print(f"[JSON] Updated {section}: {list(updates.keys())}")

    async def _sync_json_to_db(self, hotel_id: int, config: Dict[str, Any]):
        """Sync JSON config to database."""
        async with AsyncSessionLocal() as session:
            from datetime import time

            # Sync business config
            business = config.get("business", {})
            for key in ["type", "bot_name", "welcome_message", "currency", "language"]:
                if key in business:
                    new_config = BusinessConfig(
                        hotel_id=hotel_id,
                        config_key=f"business.{key}",
                        config_value=business[key]
                    )
                    session.add(new_config)

            # Sync capabilities
            for cap_id, cap_data in config.get("capabilities", {}).items():
                new_cap = Capability(
                    hotel_id=hotel_id,
                    capability_id=cap_id,
                    enabled=cap_data.get("enabled", True),
                    description=cap_data.get("description", ""),
                    hours=cap_data.get("hours"),
                )
                session.add(new_cap)

            # Sync services (restaurant rows only)
            for service in config.get("services", []):
                if not isinstance(service, dict):
                    continue
                service_id = self._normalize_identifier(service.get("id"))
                if not service_id:
                    continue
                if self._normalize_identifier(service.get("type") or "service") != "restaurant":
                    continue

                hours = service.get("hours", {})
                opens_at = None
                closes_at = None
                if isinstance(hours, dict):
                    open_val = str(hours.get("open") or "").strip()
                    close_val = str(hours.get("close") or "").strip()
                    if open_val:
                        try:
                            hh, mm = open_val.split(":")[:2]
                            opens_at = time(int(hh), int(mm))
                        except Exception:
                            opens_at = None
                    if close_val:
                        try:
                            hh, mm = close_val.split(":")[:2]
                            closes_at = time(int(hh), int(mm))
                        except Exception:
                            closes_at = None

                delivery_zones = service.get("delivery_zones", [])
                delivers_to_room = "room" in (delivery_zones or [])

                session.add(
                    Restaurant(
                        hotel_id=hotel_id,
                        code=service_id,
                        name=str(service.get("name") or service_id).strip(),
                        cuisine=service.get("cuisine") or service.get("description"),
                        opens_at=opens_at,
                        closes_at=closes_at,
                        delivers_to_room=delivers_to_room,
                        is_active=bool(service.get("is_active", True)),
                    )
                )

            # Sync intents
            for intent in config.get("intents", []):
                intent_id = self._normalize_identifier(intent.get("id"))
                if not intent_id:
                    continue
                new_intent = Intent(
                    hotel_id=hotel_id,
                    intent_id=intent_id,
                    label=intent.get("label", intent_id),
                    enabled=intent.get("enabled", True),
                )
                session.add(new_intent)
                maps_to = self._normalize_identifier(intent.get("maps_to"))
                if maps_to and maps_to != intent_id:
                    session.add(
                        BusinessConfig(
                            hotel_id=hotel_id,
                            config_key=f"intent_map.{intent_id}",
                            config_value=maps_to,
                        )
                    )

            # Sync escalation config
            escalation = config.get("escalation", {})
            for key, value in escalation.items():
                if isinstance(value, (list, dict)):
                    value = json.dumps(value)
                new_config = BusinessConfig(
                    hotel_id=hotel_id,
                    config_key=f"escalation.{key}",
                    config_value=str(value)
                )
                session.add(new_config)

            await session.commit()

    # ==================== JOURNEY PHASES (JSON-FIRST) ====================

    async def get_journey_phases(self) -> List[Dict[str, Any]]:
        """Get configured journey phases (JSON source-of-truth)."""
        config = self._load_json_config()
        phases = config.get("journey_phases", [])
        if not isinstance(phases, list):
            return []
        rows: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        changed = False
        for index, phase in enumerate(phases, start=1):
            if not isinstance(phase, dict):
                changed = True
                continue
            raw_phase_id = self._normalize_identifier(phase.get("id"))
            phase_id = self._normalize_phase_identifier(phase.get("id"))
            if not phase_id or phase_id in seen_ids:
                changed = True
                continue
            seen_ids.add(phase_id)
            try:
                order_value = int(phase.get("order", index))
            except Exception:
                order_value = index
            phase_name = str(phase.get("name") or phase_id.replace("_", " ").title()).strip()
            phase_description = str(phase.get("description") or "").strip()
            if raw_phase_id == "booking":
                if not str(phase.get("name") or "").strip() or str(phase.get("name") or "").strip().lower() == "booking":
                    phase_name = "Pre Checkin"
                if not phase_description or "reservation/payment/modify/cancel" in phase_description.lower():
                    phase_description = "Guest booking is confirmed and needs support before arrival."
            normalized_row = {
                "id": phase_id,
                "name": phase_name,
                "description": phase_description,
                "is_active": bool(phase.get("is_active", True)),
                "order": order_value,
            }
            if normalized_row != phase:
                changed = True
            rows.append(normalized_row)

        rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("name") or "")))
        if changed:
            config["journey_phases"] = rows
            self._save_json_config(config)
        return rows

    async def update_journey_phases(self, phases: List[Dict[str, Any]]) -> bool:
        """Replace journey phases in JSON config."""
        if not isinstance(phases, list):
            return False

        normalized: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, phase in enumerate(phases, start=1):
            if not isinstance(phase, dict):
                continue
            raw_phase_id = self._normalize_identifier(phase.get("id"))
            phase_id = self._normalize_phase_identifier(phase.get("id"))
            if not phase_id or phase_id in seen_ids:
                continue
            seen_ids.add(phase_id)
            try:
                order_value = int(phase.get("order", index))
            except Exception:
                order_value = index
            phase_name = str(phase.get("name") or phase_id.replace("_", " ").title()).strip()
            phase_description = str(phase.get("description") or "").strip()
            if raw_phase_id == "booking":
                if not str(phase.get("name") or "").strip() or str(phase.get("name") or "").strip().lower() == "booking":
                    phase_name = "Pre Checkin"
                if not phase_description or "reservation/payment/modify/cancel" in phase_description.lower():
                    phase_description = "Guest booking is confirmed and needs support before arrival."
            normalized.append(
                {
                    "id": phase_id,
                    "name": phase_name,
                    "description": phase_description,
                    "is_active": bool(phase.get("is_active", True)),
                    "order": order_value,
                }
            )

        if not normalized:
            return False
        normalized.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("name") or "")))

        config = self._load_json_config()
        config["journey_phases"] = normalized
        self._save_json_config(config)
        return True


# Singleton instance
db_config_service = DBConfigService()
