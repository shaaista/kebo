"""
Database-backed Configuration Service

Stores all business configuration in MySQL database.
Falls back to JSON file if database is unavailable.
"""

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Optional, Dict, Any, List
from pathlib import Path

from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Hotel, Restaurant, MenuItem, BusinessConfig, Capability, Intent, BotService,
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
    - new_bot_services: Service config (primary persistent store)
    - new_bot_restaurants: Legacy restaurant rows (food ordering only)
    - new_bot_menu_items: Menu items per restaurant
    """

    def __init__(self):
        self._current_hotel_id: Optional[int] = None
        self._hotel_id_by_code: Dict[str, int] = {}
        self._hotel_code_ctx: ContextVar[Optional[str]] = ContextVar(
            "db_config_hotel_code",
            default=None,
        )
        self._json_config: Optional[Dict] = None

    _SERVICE_DELETE_TOMBSTONES_KEY = "service_delete_tombstones"
    _JSON_SECTION_PREFIX = "json_section."

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        """Normalize IDs to stable lowercase snake-style identifiers."""
        return str(value or "").strip().lower().replace(" ", "_")

    @classmethod
    def _normalize_hotel_code(cls, value: Any) -> str:
        """
        Normalize hotel/property code used for DB scoping.
        Uses stable uppercase so DB lookups stay consistent across callers.
        """
        raw = str(value or "").strip()
        if not raw:
            return "DEFAULT"
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_")
        if not normalized:
            return "DEFAULT"
        return normalized.upper()

    def get_current_hotel_code(self) -> str:
        scoped = self._hotel_code_ctx.get()
        if scoped:
            return self._normalize_hotel_code(scoped)
        try:
            if BUSINESS_CONFIG_FILE.exists():
                with open(BUSINESS_CONFIG_FILE, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                business = payload.get("business", {}) if isinstance(payload, dict) else {}
                business_id = self._normalize_hotel_code(business.get("id"))
                if business_id and business_id != "DEFAULT":
                    return business_id
        except Exception:
            pass
        return "DEFAULT"

    def set_hotel_context(self, hotel_code: Any) -> Token:
        normalized = self._normalize_hotel_code(hotel_code)
        return self._hotel_code_ctx.set(normalized)

    def reset_hotel_context(self, token: Token) -> None:
        try:
            self._hotel_code_ctx.reset(token)
        except Exception:
            pass

    @contextmanager
    def scoped_hotel(self, hotel_code: Any):
        token = self.set_hotel_context(hotel_code)
        try:
            yield
        finally:
            self.reset_hotel_context(token)

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

    @classmethod
    def _normalize_service_prompt_pack_payload(cls, pack: Any) -> Optional[Dict[str, Any]]:
        """
        Normalize service_prompt_pack before persisting to DB so runtime prompt inputs
        remain durable and admin-managed across JSON sync/normalization cycles.
        """
        payload = pack
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return None
        if not isinstance(payload, dict):
            return None

        normalized: Dict[str, Any] = dict(payload)
        source = str(normalized.get("source") or "").strip().lower()
        if not source:
            normalized["source"] = "manual_override"
        if "version" not in normalized:
            normalized["version"] = 1
        if not str(normalized.get("generator") or "").strip():
            normalized["generator"] = "admin_ui"

        return normalized

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

    @classmethod
    def _json_section_config_key(cls, section: str) -> str:
        section_id = cls._normalize_identifier(section)
        if not section_id:
            raise ValueError("section is required")
        return f"{cls._JSON_SECTION_PREFIX}{section_id}"

    async def _load_json_section_from_db(self, section: str) -> Any:
        """Load one JSON section snapshot from BusinessConfig for current hotel."""
        hotel_id = await self.get_current_hotel_id()
        config_key = self._json_section_config_key(section)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BusinessConfig).where(
                    BusinessConfig.hotel_id == hotel_id,
                    BusinessConfig.config_key == config_key,
                )
            )
            row = result.scalar_one_or_none()

        if row is None:
            return None

        raw = str(row.config_value or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def _save_json_section_to_db(self, section: str, payload: Any) -> bool:
        """Persist one JSON section snapshot into BusinessConfig for current hotel."""
        hotel_id = await self.get_current_hotel_id()
        config_key = self._json_section_config_key(section)
        serialized = json.dumps(payload, ensure_ascii=False)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BusinessConfig).where(
                    BusinessConfig.hotel_id == hotel_id,
                    BusinessConfig.config_key == config_key,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.config_value = serialized
            else:
                session.add(
                    BusinessConfig(
                        hotel_id=hotel_id,
                        config_key=config_key,
                        config_value=serialized,
                    )
                )
            await session.commit()
        return True

    async def get_json_section(self, section: str, default: Any = None) -> Any:
        """
        Public accessor for scoped JSON-style sections with DB-first lookup.
        Falls back to scoped JSON config file section when DB snapshot is absent.
        """
        section_id = self._normalize_identifier(section)
        if not section_id:
            return default
        try:
            payload = await self._load_json_section_from_db(section_id)
            if payload is not None:
                return payload
        except Exception:
            pass
        try:
            config = self._load_json_config()
            if isinstance(config, dict) and section_id in config:
                return config.get(section_id)
        except Exception:
            pass
        return default

    async def save_json_section(self, section: str, payload: Any) -> bool:
        """
        Public mutator for scoped JSON-style sections.
        Saves section to scoped JSON config and DB snapshot.
        """
        section_id = self._normalize_identifier(section)
        if not section_id:
            return False
        config = self._load_json_config()
        if not isinstance(config, dict):
            config = {}
        config[section_id] = payload
        self._save_json_config(config)
        await self._save_json_section_to_db(section_id, payload)
        return True

    async def get_session(self) -> AsyncSession:
        """Get a database session."""
        return AsyncSessionLocal()

    # ==================== HOTEL MANAGEMENT ====================

    async def get_or_create_hotel(self, code: str = "DEFAULT") -> int:
        """Get or create the current hotel, return hotel_id (int)."""
        normalized_code = self._normalize_hotel_code(code or self.get_current_hotel_code())
        cached_id = self._hotel_id_by_code.get(normalized_code)
        if cached_id:
            self._current_hotel_id = cached_id
            return cached_id

        async with AsyncSessionLocal() as session:
            # Check if hotel exists
            result = await session.execute(
                select(Hotel).where(Hotel.code.ilike(normalized_code))
            )
            hotel = result.scalar_one_or_none()

            if hotel:
                self._current_hotel_id = hotel.id
                self._hotel_id_by_code[normalized_code] = hotel.id
                return hotel.id

            # Create new hotel from JSON config
            json_config = self._load_json_config()
            business = json_config.get("business", {})

            new_hotel = Hotel(
                code=normalized_code,
                name=business.get("name", "My Business"),
                city=business.get("city", "City"),
                timezone=business.get("timezone", "Asia/Kolkata"),
                is_active=True,
            )
            session.add(new_hotel)
            await session.commit()
            await session.refresh(new_hotel)

            self._current_hotel_id = new_hotel.id
            self._hotel_id_by_code[normalized_code] = new_hotel.id

            # Sync rest of config to DB
            await self._sync_json_to_db(new_hotel.id, json_config)

            return new_hotel.id

    async def get_current_hotel_id(self) -> int:
        """Get current hotel ID, creating if needed."""
        current_code = self.get_current_hotel_code()
        cached_id = self._hotel_id_by_code.get(current_code)
        if cached_id:
            self._current_hotel_id = cached_id
            return cached_id
        return await self.get_or_create_hotel(current_code)

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

            payload = {
                "id": str(hotel.code or self.get_current_hotel_code()),
                "code": str(hotel.code or self.get_current_hotel_code()),
                "hotel_id": str(hotel.id),
                "name": hotel.name,
                "city": hotel.city,
                "timezone": hotel.timezone,
                "type": config_dict.get("business.type", "hotel"),
                "bot_name": config_dict.get("business.bot_name", "Assistant"),
                "welcome_message": config_dict.get("business.welcome_message", "Hello! How can I help you?"),
                "currency": config_dict.get("business.currency", "INR"),
                "language": config_dict.get("business.language", "en"),
            }
            # Overlay onboarding-only business fields from scoped JSON as fallback.
            try:
                business_json = self._load_json_config().get("business", {})
                if isinstance(business_json, dict):
                    for key, value in business_json.items():
                        if key in {"id", "code", "hotel_id"}:
                            continue
                        if value is not None:
                            payload[key] = value
            except Exception:
                pass

            # Overlay onboarding-only business fields from DB JSON snapshot if present.
            try:
                business_snapshot = await self._load_json_section_from_db("business")
                if isinstance(business_snapshot, dict):
                    for key, value in business_snapshot.items():
                        if key in {"id", "code", "hotel_id"}:
                            continue
                        if value is not None:
                            payload[key] = value
            except Exception:
                pass
            return payload

    async def update_business_info(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update business info in database AND JSON file."""

        # ALWAYS update JSON file first (this is the reliable backup)
        self._update_json_config("business", updates)
        try:
            current_business = self._load_json_config().get("business", {})
            if isinstance(current_business, dict):
                await self._save_json_section_to_db("business", current_business)
        except Exception as e:
            print(f"[DB] business snapshot save failed: {e}")

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

    # ==================== SERVICES (new_bot_services - DB primary) ====================

    def _service_to_dict(self, row: "BotService") -> Dict[str, Any]:
        """Convert a BotService ORM row to the dict format used everywhere."""
        result: Dict[str, Any] = {
            "id": row.service_id,
            "name": row.name,
            "type": row.type or "service",
            "description": row.description or "",
            "is_active": bool(row.is_active),
            "is_builtin": bool(row.is_builtin),
            "ticketing_enabled": bool(row.ticketing_enabled),
            "ticketing_policy": row.ticketing_policy or "",
        }
        # ticketing_mode & form_config
        tm = getattr(row, "ticketing_mode", None)
        if tm:
            result["ticketing_mode"] = str(tm).strip().lower()
        fc = getattr(row, "form_config", None)
        if isinstance(fc, dict):
            result["form_config"] = fc
        if row.phase_id:
            result["phase_id"] = row.phase_id
        if row.service_prompt_pack:
            prompt_pack = self._normalize_service_prompt_pack_payload(row.service_prompt_pack)
            if isinstance(prompt_pack, dict):
                result["service_prompt_pack"] = prompt_pack
                source = str(prompt_pack.get("source") or "").strip().lower()
                result["service_prompt_pack_custom"] = bool(
                    source in {"manual_override", "admin_ui", "admin_override", "db"}
                    or str(prompt_pack.get("ticketing_conditions") or "").strip()
                    or str(prompt_pack.get("extracted_knowledge") or "").strip()
                    or bool(prompt_pack.get("required_slots"))
                )
        if row.generated_system_prompt:
            result["generated_system_prompt"] = row.generated_system_prompt
        return result

    def _read_service_delete_tombstones(
        self,
        config: Optional[Dict[str, Any]] = None,
    ) -> tuple[set[str], bool]:
        """
        Read service delete tombstones from JSON config.
        Returns (deleted_service_ids, suppress_all_services_flag).
        """
        source = config if isinstance(config, dict) else self._load_json_config()
        raw = source.get(self._SERVICE_DELETE_TOMBSTONES_KEY)

        deleted_ids: set[str] = set()
        suppress_all = False
        if isinstance(raw, dict):
            suppress_all = bool(raw.get("suppress_all", False))
            raw_ids = raw.get("ids", [])
        elif isinstance(raw, list):
            # Backward-compatible legacy list-only format.
            raw_ids = raw
        else:
            raw_ids = []

        if isinstance(raw_ids, list):
            for item in raw_ids:
                normalized = self._normalize_identifier(item)
                if normalized:
                    deleted_ids.add(normalized)
        return deleted_ids, suppress_all

    def _write_service_delete_tombstones(
        self,
        config: Dict[str, Any],
        deleted_ids: set[str],
        suppress_all: bool,
    ) -> bool:
        """Persist service delete tombstones to JSON config."""
        normalized_ids = sorted(
            {
                self._normalize_identifier(item)
                for item in (deleted_ids or set())
                if self._normalize_identifier(item)
            }
        )
        normalized_suppress_all = bool(suppress_all)
        existing_raw = config.get(self._SERVICE_DELETE_TOMBSTONES_KEY)
        existing_payload = (
            existing_raw if isinstance(existing_raw, dict) else {}
        )

        if not normalized_ids and not normalized_suppress_all:
            if self._SERVICE_DELETE_TOMBSTONES_KEY in config:
                config.pop(self._SERVICE_DELETE_TOMBSTONES_KEY, None)
                self._save_json_config(config)
                return True
            return False

        payload = {
            "ids": normalized_ids,
            "suppress_all": normalized_suppress_all,
        }
        if existing_payload == payload:
            return False
        config[self._SERVICE_DELETE_TOMBSTONES_KEY] = payload
        self._save_json_config(config)
        return True

    def mark_service_deleted(self, service_id: str) -> bool:
        """Mark one service ID as deleted until DB is reconciled."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return False
        try:
            config = self._load_json_config()
            deleted_ids, suppress_all = self._read_service_delete_tombstones(config)
            deleted_ids.add(normalized_id)
            self._write_service_delete_tombstones(config, deleted_ids, suppress_all)
            return True
        except Exception as e:
            print(f"[JSON] Failed to mark service tombstone for {normalized_id}: {e}")
            return False

    def unmark_service_deleted(
        self,
        service_id: str,
        *,
        clear_suppress_all: bool = False,
    ) -> bool:
        """Remove one service ID tombstone after DB and JSON are in sync."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id and not clear_suppress_all:
            return False
        try:
            config = self._load_json_config()
            deleted_ids, suppress_all = self._read_service_delete_tombstones(config)
            changed = False
            if normalized_id and normalized_id in deleted_ids:
                deleted_ids.discard(normalized_id)
                changed = True
            if clear_suppress_all and suppress_all:
                suppress_all = False
                changed = True
            if changed:
                self._write_service_delete_tombstones(config, deleted_ids, suppress_all)
            return True
        except Exception as e:
            print(f"[JSON] Failed to clear service tombstone for {normalized_id}: {e}")
            return False

    def mark_all_services_deleted(self) -> bool:
        """Mark 'clear all services' intent until DB rows are fully deleted."""
        try:
            config = self._load_json_config()
            deleted_ids, _ = self._read_service_delete_tombstones(config)
            self._write_service_delete_tombstones(config, deleted_ids, True)
            return True
        except Exception as e:
            print(f"[JSON] Failed to mark clear-all tombstone: {e}")
            return False

    def clear_service_delete_tombstones(self) -> bool:
        """Clear all service delete tombstones."""
        try:
            config = self._load_json_config()
            self._write_service_delete_tombstones(config, set(), False)
            return True
        except Exception as e:
            print(f"[JSON] Failed to clear service tombstones: {e}")
            return False

    def _sync_services_to_json(self, services: List[Dict[str, Any]]) -> None:
        """Write current service list to JSON so config_service / LLM path stays fresh."""
        try:
            json_config = self._load_json_config()
            json_config["services"] = services
            self._save_json_config(json_config)
        except Exception as e:
            print(f"[JSON] Failed to sync services to JSON: {e}")

    def _prune_json_service_artifacts(
        self,
        *,
        service_ids_to_remove: set[str],
        clear_service_kb_records: bool = False,
    ) -> None:
        """
        Remove JSON artifacts linked to deleted services so runtime uses a clean state.
        Prunes:
        - agent_plugins.plugins rows linked by service_id
        - service_kb.records linked by service_id or removed plugin_id
        """
        try:
            normalized_service_ids = {
                self._normalize_identifier(item)
                for item in service_ids_to_remove
                if self._normalize_identifier(item)
            }
            json_config = self._load_json_config()
            changed = False

            removed_plugin_ids: set[str] = set()
            plugins_cfg = json_config.get("agent_plugins")
            if isinstance(plugins_cfg, dict):
                plugin_rows = plugins_cfg.get("plugins")
                if isinstance(plugin_rows, list):
                    kept_plugins: list[Any] = []
                    for plugin in plugin_rows:
                        if not isinstance(plugin, dict):
                            kept_plugins.append(plugin)
                            continue
                        plugin_service_id = self._normalize_identifier(plugin.get("service_id"))
                        plugin_id = self._normalize_identifier(plugin.get("id"))
                        if plugin_service_id and plugin_service_id in normalized_service_ids:
                            if plugin_id:
                                removed_plugin_ids.add(plugin_id)
                            continue
                        kept_plugins.append(plugin)
                    if kept_plugins != plugin_rows:
                        plugins_cfg["plugins"] = kept_plugins
                        changed = True

            service_kb = json_config.get("service_kb")
            if isinstance(service_kb, dict):
                records = service_kb.get("records")
                if clear_service_kb_records:
                    if isinstance(records, list) and records:
                        service_kb["records"] = []
                        changed = True
                elif isinstance(records, list):
                    kept_records = []
                    for record in records:
                        if not isinstance(record, dict):
                            kept_records.append(record)
                            continue
                        record_service_id = self._normalize_identifier(record.get("service_id"))
                        record_plugin_id = self._normalize_identifier(record.get("plugin_id"))
                        if record_service_id and record_service_id in normalized_service_ids:
                            continue
                        if record_plugin_id and record_plugin_id in removed_plugin_ids:
                            continue
                        kept_records.append(record)
                    if kept_records != records:
                        service_kb["records"] = kept_records
                        changed = True

            if changed:
                self._save_json_config(json_config)
                print(
                    "[JSON] Pruned service artifacts "
                    f"(services={sorted(normalized_service_ids)}, plugins={sorted(removed_plugin_ids)})"
                )
        except Exception as e:
            print(f"[JSON] Service artifact prune failed: {e}")

    async def get_services(self) -> List[Dict[str, Any]]:
        """Load services from new_bot_services (DB-primary). Syncs JSON on load."""
        hotel_id = await self.get_current_hotel_id()
        tombstone_ids: set[str] = set()
        suppress_all = False
        try:
            json_config = self._load_json_config()
            tombstone_ids, suppress_all = self._read_service_delete_tombstones(json_config)
        except Exception as e:
            print(f"[JSON] Failed to read service tombstones: {e}")
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BotService)
                    .where(BotService.hotel_id == hotel_id)
                    .order_by(BotService.id)
                )
                rows = result.scalars().all()
                services = [self._service_to_dict(row) for row in rows]
                suppress_all_reconciled = False

                if suppress_all:
                    try:
                        await session.execute(
                            delete(BotService).where(BotService.hotel_id == hotel_id)
                        )
                        await session.commit()
                        suppress_all_reconciled = True
                    except Exception as purge_error:
                        await session.rollback()
                        print(f"[DB] Failed clearing services while suppress_all is active: {purge_error}")
                    services = []
                elif tombstone_ids:
                    stale_ids = {
                        self._normalize_identifier(item.get("id"))
                        for item in services
                        if self._normalize_identifier(item.get("id")) in tombstone_ids
                    }
                    if stale_ids:
                        try:
                            await session.execute(
                                delete(BotService).where(
                                    BotService.hotel_id == hotel_id,
                                    BotService.service_id.in_(sorted(stale_ids)),
                                )
                            )
                            await session.commit()
                            for stale_id in stale_ids:
                                self.unmark_service_deleted(stale_id)
                        except Exception as purge_error:
                            await session.rollback()
                            print(f"[DB] Failed purging tombstoned services {sorted(stale_ids)}: {purge_error}")
                        services = [
                            item
                            for item in services
                            if self._normalize_identifier(item.get("id")) not in stale_ids
                        ]
                    else:
                        services = [
                            item
                            for item in services
                            if self._normalize_identifier(item.get("id")) not in tombstone_ids
                        ]

                if suppress_all_reconciled:
                    self.clear_service_delete_tombstones()
                return services
        except Exception as e:
            print(f"[DB] get_services failed, falling back to JSON: {e}")
            return self._load_json_config().get("services", [])

    async def add_service(self, service: Dict[str, Any]) -> bool:
        """Upsert a service into new_bot_services."""
        service_id = self._normalize_identifier(service.get("id"))
        if not service_id:
            return False

        hotel_id = await self.get_current_hotel_id()
        phase_id = self._normalize_phase_identifier(service.get("phase_id")) or None
        prompt_pack = self._normalize_service_prompt_pack_payload(service.get("service_prompt_pack"))
        generated_prompt = str(service.get("generated_system_prompt") or "").strip() or None

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BotService).where(
                        BotService.hotel_id == hotel_id,
                        BotService.service_id == service_id,
                    )
                )
                row = result.scalar_one_or_none()
                tm = str(service.get("ticketing_mode") or "").strip().lower() or None
                fc = service.get("form_config")
                fc = fc if isinstance(fc, dict) else None

                if row:
                    row.name = str(service.get("name") or service_id).strip()
                    row.type = self._normalize_identifier(service.get("type") or "service")
                    row.description = str(service.get("description") or "").strip()
                    row.phase_id = phase_id
                    row.is_active = bool(service.get("is_active", True))
                    row.is_builtin = bool(service.get("is_builtin", False))
                    row.ticketing_enabled = bool(service.get("ticketing_enabled", True))
                    row.ticketing_mode = tm
                    row.ticketing_policy = str(service.get("ticketing_policy") or "").strip() or None
                    row.form_config = fc
                    if prompt_pack is not None:
                        row.service_prompt_pack = prompt_pack
                    if "generated_system_prompt" in service:
                        row.generated_system_prompt = generated_prompt
                else:
                    row = BotService(
                        hotel_id=hotel_id,
                        service_id=service_id,
                        name=str(service.get("name") or service_id).strip(),
                        type=self._normalize_identifier(service.get("type") or "service"),
                        description=str(service.get("description") or "").strip(),
                        phase_id=phase_id,
                        is_active=bool(service.get("is_active", True)),
                        is_builtin=bool(service.get("is_builtin", False)),
                        ticketing_enabled=bool(service.get("ticketing_enabled", True)),
                        ticketing_mode=tm,
                        ticketing_policy=str(service.get("ticketing_policy") or "").strip() or None,
                        form_config=fc,
                        service_prompt_pack=prompt_pack,
                        generated_system_prompt=generated_prompt,
                    )
                    session.add(row)
                await session.commit()
                print(f"[DB] Upserted service {service_id}")
                try:
                    from services.flow_logger import log_service_config_save

                    pack = row.service_prompt_pack if isinstance(row.service_prompt_pack, dict) else {}
                    extracted_len = len(str((pack or {}).get("extracted_knowledge") or "").strip())
                    log_service_config_save(
                        action="add_or_upsert",
                        source="db",
                        service_id=service_id,
                        service_name=str(row.name or service_id).strip(),
                        description_len=len(str(row.description or "").strip()),
                        ticketing_policy_len=len(str(row.ticketing_policy or "").strip()),
                        extracted_knowledge_len=extracted_len,
                        generated_prompt_len=len(str(row.generated_system_prompt or "").strip()),
                        success=True,
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[DB] add_service failed for {service_id}: {e}")
            try:
                from services.flow_logger import log_service_config_save

                pack = prompt_pack if isinstance(prompt_pack, dict) else {}
                log_service_config_save(
                    action="add_or_upsert",
                    source="db",
                    service_id=service_id,
                    service_name=str(service.get("name") or service_id).strip(),
                    description_len=len(str(service.get("description") or "").strip()),
                    ticketing_policy_len=len(str(service.get("ticketing_policy") or "").strip()),
                    extracted_knowledge_len=len(str(pack.get("extracted_knowledge") or "").strip()),
                    generated_prompt_len=0,
                    success=False,
                    error=str(e),
                )
            except Exception:
                pass
            return False

        # Re-adding a service should release any prior deletion tombstones.
        self.unmark_service_deleted(service_id, clear_suppress_all=True)
        # Sync JSON so LLM reads fresh data.
        all_services = await self.get_services()
        self._sync_services_to_json(all_services)
        return True

    async def update_service(self, service_id: str, updates: Dict[str, Any]) -> bool:
        """Patch fields on an existing service row."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return False

        hotel_id = await self.get_current_hotel_id()
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BotService).where(
                        BotService.hotel_id == hotel_id,
                        BotService.service_id == normalized_id,
                    )
                )
                row = result.scalar_one_or_none()
                if not row:
                    # Row doesn't exist yet — create it.
                    updates["id"] = normalized_id
                    return await self.add_service(updates)

                if "name" in updates:
                    row.name = str(updates["name"]).strip()
                if "type" in updates:
                    row.type = self._normalize_identifier(updates["type"] or "service")
                if "description" in updates:
                    row.description = str(updates["description"] or "").strip()
                if "phase_id" in updates:
                    row.phase_id = self._normalize_phase_identifier(updates["phase_id"]) or None
                if "is_active" in updates:
                    row.is_active = bool(updates["is_active"])
                if "is_builtin" in updates:
                    row.is_builtin = bool(updates["is_builtin"])
                if "ticketing_enabled" in updates:
                    row.ticketing_enabled = bool(updates["ticketing_enabled"])
                if "ticketing_mode" in updates:
                    row.ticketing_mode = str(updates["ticketing_mode"] or "").strip().lower() or None
                if "ticketing_policy" in updates:
                    row.ticketing_policy = str(updates["ticketing_policy"] or "").strip() or None
                if "form_config" in updates:
                    fc = updates["form_config"]
                    row.form_config = fc if isinstance(fc, dict) else None
                if "service_prompt_pack" in updates:
                    pack = self._normalize_service_prompt_pack_payload(updates["service_prompt_pack"])
                    row.service_prompt_pack = pack
                if "generated_system_prompt" in updates:
                    row.generated_system_prompt = (
                        str(updates["generated_system_prompt"] or "").strip() or None
                    )
                await session.commit()
                print(f"[DB] Updated service {normalized_id}: {list(updates.keys())}")
                try:
                    from services.flow_logger import log_service_config_save

                    pack = row.service_prompt_pack if isinstance(row.service_prompt_pack, dict) else {}
                    log_service_config_save(
                        action="update",
                        source="db",
                        service_id=normalized_id,
                        service_name=str(row.name or normalized_id).strip(),
                        description_len=len(str(row.description or "").strip()),
                        ticketing_policy_len=len(str(row.ticketing_policy or "").strip()),
                        extracted_knowledge_len=len(str((pack or {}).get("extracted_knowledge") or "").strip()),
                        generated_prompt_len=len(str(row.generated_system_prompt or "").strip()),
                        success=True,
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[DB] update_service failed for {normalized_id}: {e}")
            try:
                from services.flow_logger import log_service_config_save

                pack = updates.get("service_prompt_pack", {})
                pack = pack if isinstance(pack, dict) else {}
                log_service_config_save(
                    action="update",
                    source="db",
                    service_id=normalized_id,
                    service_name=str(updates.get("name") or normalized_id).strip(),
                    description_len=len(str(updates.get("description") or "").strip()),
                    ticketing_policy_len=len(str(updates.get("ticketing_policy") or "").strip()),
                    extracted_knowledge_len=len(str(pack.get("extracted_knowledge") or "").strip()),
                    generated_prompt_len=0,
                    success=False,
                    error=str(e),
                )
            except Exception:
                pass
            return False

        self.unmark_service_deleted(normalized_id, clear_suppress_all=True)
        all_services = await self.get_services()
        self._sync_services_to_json(all_services)
        return True

    async def save_generated_prompt(self, service_id: str, prompt: str) -> bool:
        """Store or clear the LLM-generated system prompt for a service."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return False
        hotel_id = await self.get_current_hotel_id()
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BotService).where(
                        BotService.hotel_id == hotel_id,
                        BotService.service_id == normalized_id,
                    )
                )
                row = result.scalar_one_or_none()
                if not row:
                    return False
                row.generated_system_prompt = str(prompt).strip() or None
                await session.commit()
                print(f"[DB] Saved generated prompt for service {normalized_id} ({len(prompt)} chars)")
                try:
                    from services.flow_logger import log_service_config_save

                    pack = row.service_prompt_pack if isinstance(row.service_prompt_pack, dict) else {}
                    log_service_config_save(
                        action="save_generated_prompt",
                        source="db",
                        service_id=normalized_id,
                        service_name=str(row.name or normalized_id).strip(),
                        description_len=len(str(row.description or "").strip()),
                        ticketing_policy_len=len(str(row.ticketing_policy or "").strip()),
                        extracted_knowledge_len=len(str((pack or {}).get("extracted_knowledge") or "").strip()),
                        generated_prompt_len=len(str(row.generated_system_prompt or "").strip()),
                        success=True,
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[DB] save_generated_prompt failed for {normalized_id}: {e}")
            try:
                from services.flow_logger import log_service_config_save

                log_service_config_save(
                    action="save_generated_prompt",
                    source="db",
                    service_id=normalized_id,
                    service_name=normalized_id,
                    description_len=0,
                    ticketing_policy_len=0,
                    extracted_knowledge_len=0,
                    generated_prompt_len=len(str(prompt or "").strip()),
                    success=False,
                    error=str(e),
                )
            except Exception:
                pass
            return False
        all_services = await self.get_services()
        self._sync_services_to_json(all_services)
        return True

    async def delete_service(self, service_id: str) -> bool:
        """Hard-delete a service row from new_bot_services."""
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return False
        self.mark_service_deleted(normalized_id)
        hotel_id = await self.get_current_hotel_id()
        deleted_rows = 0
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    delete(BotService).where(
                        BotService.hotel_id == hotel_id,
                        BotService.service_id == normalized_id,
                    )
                )
                await session.commit()
                deleted_rows = int(result.rowcount or 0)
                print(f"[DB] Deleted service {normalized_id}")
        except Exception as e:
            print(f"[DB] delete_service failed for {normalized_id}: {e}")
            return False

        # DB and JSON are now reconciled for this id.
        self.unmark_service_deleted(normalized_id)
        all_services = await self.get_services()
        self._sync_services_to_json(all_services)
        self._prune_json_service_artifacts(service_ids_to_remove={normalized_id})
        return deleted_rows > 0

    async def clear_services(self) -> bool:
        """Delete all services for this hotel."""
        self.mark_all_services_deleted()
        hotel_id = await self.get_current_hotel_id()
        service_ids_to_remove: set[str] = set()
        try:
            async with AsyncSessionLocal() as session:
                existing_service_ids = await session.execute(
                    select(BotService.service_id).where(BotService.hotel_id == hotel_id)
                )
                service_ids_to_remove = {
                    self._normalize_identifier(item)
                    for item in existing_service_ids.scalars().all()
                    if self._normalize_identifier(item)
                }
                await session.execute(
                    delete(BotService).where(BotService.hotel_id == hotel_id)
                )
                await session.commit()
                print("[DB] Cleared all services")
        except Exception as e:
            print(f"[DB] clear_services failed: {e}")
            return False

        self.clear_service_delete_tombstones()
        self._sync_services_to_json([])
        self._prune_json_service_artifacts(
            service_ids_to_remove=service_ids_to_remove,
            clear_service_kb_records=True,
        )
        return True

    # ==================== KB FILES (DB-PRIMARY) ====================

    async def delete_all_kb_files(self) -> int:
        """Delete all KB file records for this hotel from DB."""
        hotel_id = await self.get_current_hotel_id()
        try:
            from models.database import KBFile
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    delete(KBFile).where(KBFile.hotel_id == hotel_id)
                )
                await session.commit()
                count = int(result.rowcount or 0)
                print(f"[DB] Deleted {count} KB file record(s) for hotel {hotel_id}")
                return count
        except Exception as e:
            print(f"[DB] delete_all_kb_files failed: {e}")
            return 0

    async def save_kb_file(
        self,
        original_name: str,
        stored_name: str,
        content: str,
        content_hash: str,
    ) -> bool:
        """Upsert KB file content into new_bot_kb_files."""
        hotel_id = await self.get_current_hotel_id()
        try:
            from models.database import KBFile
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(KBFile).where(
                        KBFile.hotel_id == hotel_id,
                        KBFile.stored_name == stored_name,
                    )
                )
                row = result.scalar_one_or_none()
                if row:
                    row.content = content
                    row.content_hash = content_hash
                    row.original_name = original_name
                else:
                    session.add(KBFile(
                        hotel_id=hotel_id,
                        original_name=original_name,
                        stored_name=stored_name,
                        content=content,
                        content_hash=content_hash,
                    ))
                await session.commit()
                print(f"[DB] Saved KB file {stored_name}")
                try:
                    from services.flow_logger import log_kb_db_persist

                    log_kb_db_persist(
                        tenant_id=str(self.get_current_hotel_code()),
                        original_name=str(original_name or ""),
                        stored_name=str(stored_name or ""),
                        content_chars=len(str(content or "")),
                        content_hash=str(content_hash or ""),
                        success=True,
                    )
                except Exception:
                    pass
                return True
        except Exception as e:
            print(f"[DB] save_kb_file failed: {e}")
            try:
                from services.flow_logger import log_kb_db_persist

                log_kb_db_persist(
                    tenant_id=str(self.get_current_hotel_code()),
                    original_name=str(original_name or ""),
                    stored_name=str(stored_name or ""),
                    content_chars=len(str(content or "")),
                    content_hash=str(content_hash or ""),
                    success=False,
                    error=str(e),
                )
            except Exception:
                pass
            return False

    async def restore_kb_files(self, kb_dir: str) -> int:
        """
        On startup, restore all KB files for the current hotel from DB to disk.
        Keeps all historical rows intact and updates knowledge_config.sources to
        include all restored file paths in stable order.
        Returns the number of files written/updated on disk.
        """
        hotel_id = await self.get_current_hotel_id()
        try:
            from models.database import KBFile
            from pathlib import Path as _Path
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(KBFile).where(KBFile.hotel_id == hotel_id).order_by(KBFile.id.asc())
                )
                rows = result.scalars().all()

            if not rows:
                return 0

            tenant_folder = self._normalize_identifier(self.get_current_hotel_code()) or "default"
            restored = 0
            source_paths: list[str] = []

            for row in rows:
                stored_name = str(row.stored_name or "").strip()
                if not stored_name:
                    continue

                dest = _Path(kb_dir) / "uploads" / tenant_folder / stored_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                db_content = str(row.content or "")

                write_required = True
                if dest.exists():
                    try:
                        existing_content = dest.read_text(encoding="utf-8", errors="ignore")
                        write_required = existing_content != db_content
                    except Exception:
                        write_required = True

                if write_required:
                    dest.write_text(db_content, encoding="utf-8")
                    restored += 1
                    print(f"[KB] Restored {stored_name} from DB")

                source_paths.append(str(dest.resolve()))

            # Update knowledge_config.sources to all restored files (deduped, stable order).
            deduped_sources: list[str] = []
            seen_sources: set[str] = set()
            for source in source_paths:
                if source in seen_sources:
                    continue
                seen_sources.add(source)
                deduped_sources.append(source)

            from services.config_service import config_service as _cs
            if deduped_sources:
                _cs.update_knowledge_config({"sources": deduped_sources})
            return restored
        except Exception as e:
            print(f"[KB] restore_kb_files failed: {e}")
            return 0

    # ==================== PROMPTS / KNOWLEDGE / UI ====================

    async def get_prompts(self) -> Dict[str, Any]:
        """Get prompts with DB snapshot preference and JSON fallback."""
        try:
            section = await self._load_json_section_from_db("prompts")
            if isinstance(section, dict):
                return section
        except Exception:
            pass

        config = self._load_json_config()
        prompts = config.get("prompts", {})
        return prompts if isinstance(prompts, dict) else {}

    async def update_prompts(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update prompts in scoped JSON config and persist section snapshot to DB."""
        config = self._load_json_config()
        prompts = config.setdefault("prompts", {})
        if not isinstance(prompts, dict):
            prompts = {}
            config["prompts"] = prompts
        if isinstance(updates, dict):
            prompts.update(updates)
        self._save_json_config(config)
        await self._save_json_section_to_db("prompts", prompts)
        return prompts

    async def get_knowledge_config(self) -> Dict[str, Any]:
        """Get knowledge config with DB snapshot preference and JSON fallback."""
        try:
            section = await self._load_json_section_from_db("knowledge_base")
            if isinstance(section, dict):
                return section
        except Exception:
            pass

        config = self._load_json_config()
        knowledge = config.get("knowledge_base", {})
        return knowledge if isinstance(knowledge, dict) else {}

    async def update_knowledge_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update scoped knowledge config using config_service merge logic,
        then persist section snapshot to DB.
        """
        from services.config_service import config_service as _cs

        knowledge = _cs.update_knowledge_config(updates if isinstance(updates, dict) else {})
        if not isinstance(knowledge, dict):
            knowledge = {}

        # Keep DB snapshot aligned with JSON source.
        await self._save_json_section_to_db("knowledge_base", knowledge)
        return knowledge

    async def get_ui_settings(self) -> Dict[str, Any]:
        """Get UI settings with DB snapshot preference and JSON fallback."""
        try:
            section = await self._load_json_section_from_db("ui_settings")
            if isinstance(section, dict):
                return section
        except Exception:
            pass

        config = self._load_json_config()
        ui_settings = config.get("ui_settings", {})
        return ui_settings if isinstance(ui_settings, dict) else {}

    async def update_ui_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update scoped UI settings using config_service merge logic,
        then persist section snapshot to DB.
        """
        from services.config_service import config_service as _cs

        ui_settings = _cs.update_ui_settings(updates if isinstance(updates, dict) else {})
        if not isinstance(ui_settings, dict):
            ui_settings = {}

        # Keep DB snapshot aligned with JSON source.
        await self._save_json_section_to_db("ui_settings", ui_settings)
        return ui_settings

    # ==================== FAQ BANK (JSON-FIRST) ====================

    async def get_faq_bank(self) -> List[Dict[str, Any]]:
        """Get admin FAQ bank entries (JSON source-of-truth)."""
        try:
            section = await self._load_json_section_from_db("faq_bank")
            if isinstance(section, list):
                return [dict(entry) for entry in section if isinstance(entry, dict)]
        except Exception:
            pass

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
        try:
            await self._save_json_section_to_db("faq_bank", config.get("faq_bank", []))
        except Exception as e:
            print(f"[DB] FAQ snapshot save failed: {e}")
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
            try:
                await self._save_json_section_to_db("faq_bank", config.get("faq_bank", []))
            except Exception as e:
                print(f"[DB] FAQ snapshot save failed: {e}")
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
        try:
            await self._save_json_section_to_db("faq_bank", config.get("faq_bank", []))
        except Exception as e:
            print(f"[DB] FAQ snapshot save failed: {e}")
        return True

    # ==================== TOOLS (JSON-FIRST) ====================

    async def get_tools(self) -> List[Dict[str, Any]]:
        """Get admin tools list (JSON source-of-truth)."""
        try:
            section = await self._load_json_section_from_db("tools")
            if isinstance(section, list):
                return [dict(tool) for tool in section if isinstance(tool, dict)]
        except Exception:
            pass

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
        try:
            await self._save_json_section_to_db("tools", config.get("tools", []))
        except Exception as e:
            print(f"[DB] tools snapshot save failed: {e}")
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
            try:
                await self._save_json_section_to_db("tools", config.get("tools", []))
            except Exception as e:
                print(f"[DB] tools snapshot save failed: {e}")
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
        try:
            await self._save_json_section_to_db("tools", config.get("tools", []))
        except Exception as e:
            print(f"[DB] tools snapshot save failed: {e}")
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
        section_types: Dict[str, type] = {
            "business": dict,
            "prompts": dict,
            "knowledge_base": dict,
            "service_kb": dict,
            "ui_settings": dict,
            "faq_bank": list,
            "tools": list,
            "journey_phases": list,
        }
        for section_name, expected_type in section_types.items():
            try:
                section_value = await self._load_json_section_from_db(section_name)
                if isinstance(section_value, expected_type):
                    base_config[section_name] = section_value
            except Exception:
                pass
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

            # Persist JSON-first section snapshots in DB for multi-node resilience.
            for section_name in (
                "business",
                "prompts",
                "knowledge_base",
                "service_kb",
                "ui_settings",
                "faq_bank",
                "tools",
                "journey_phases",
            ):
                if section_name in config:
                    await self._save_json_section_to_db(section_name, config.get(section_name))

            # Update escalation
            if "escalation" in config:
                await self.update_escalation_config(config["escalation"])
            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False

    # ==================== JSON HELPERS ====================

    def _resolve_json_config_file(self) -> Path:
        scoped = self._hotel_code_ctx.get()
        code = self._normalize_identifier(scoped)
        if code and code != "default":
            return CONFIG_DIR / "properties" / f"{code}.json"
        return BUSINESS_CONFIG_FILE

    def _load_json_config(self) -> Dict[str, Any]:
        """Load config from JSON file (fallback)."""
        config_file = self._resolve_json_config_file()
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
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
        config_file = self._resolve_json_config_file()
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w", encoding="utf-8") as f:
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
        try:
            section = await self._load_json_section_from_db("journey_phases")
            if isinstance(section, list):
                phases = section
        except Exception:
            pass
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
            try:
                await self._save_json_section_to_db("journey_phases", rows)
            except Exception as e:
                print(f"[DB] journey phases snapshot save failed: {e}")
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
        try:
            await self._save_json_section_to_db("journey_phases", normalized)
        except Exception as e:
            print(f"[DB] journey phases snapshot save failed: {e}")
        return True


# Singleton instance
db_config_service = DBConfigService()
