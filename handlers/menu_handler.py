"""
Menu Handler

Builds and returns a formatted menu response.

If a database session is available the handler queries the ``new_bot_restaurants``
and ``new_bot_menu_items`` tables so the menu always reflects the latest data.
Otherwise it falls back to the capability summary built from the JSON config file.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import re
from difflib import SequenceMatcher
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.base_handler import BaseHandler, HandlerResult
from models.database import Restaurant, MenuItem, Hotel
from schemas.chat import ConversationState, IntentResult, ConversationContext, IntentType
from services.config_service import config_service


class MenuHandler(BaseHandler):
    """Handle menu_request intents by displaying restaurant menus."""

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session: Optional[AsyncSession] = None,
    ) -> HandlerResult:
        if context.pending_action == "show_menu":
            if intent_result.intent == IntentType.CONFIRMATION_NO:
                return HandlerResult(
                    response_text="No problem. I won't show the menu right now. Let me know what you'd like next.",
                    next_state=ConversationState.IDLE,
                    suggested_actions=["Order food", "Need help"],
                    pending_action=None,
                    pending_data={},
                )
            # Confirmation (including typo-yes normalized upstream) should show full menu.
            requested_restaurant = None
            requested_item = None
        else:
            requested_restaurant = self._resolve_requested_restaurant(
                message=message,
                entities=intent_result.entities,
                capabilities=capabilities,
            )
            requested_item = self._extract_requested_item_keyword(
                message,
                intent_result.entities,
            )
            if requested_restaurant and "menu" in str(message or "").lower():
                requested_item = None

        # Check if the user asked for a specific restaurant by name
        if db_session is not None:
            menu_text = await self._build_menu_from_db(
                db_session, capabilities, requested_restaurant, requested_item
            )
        else:
            menu_text = self._build_menu_from_config(
                capabilities, requested_restaurant, requested_item
            )

        if not menu_text:
            if requested_item:
                return HandlerResult(
                    response_text=(
                    f"Sorry, I couldn't find items matching '{requested_item}' in our active menus. "
                    "Would you like to see the full menu?"
                    ),
                    next_state=ConversationState.AWAITING_CONFIRMATION,
                    suggested_actions=["Yes, show full menu", "No, cancel"],
                    pending_action="show_menu",
                    pending_data={"requested_item": requested_item},
                )
            return HandlerResult(
                response_text="Sorry, there are no menus available at the moment.",
                next_state=ConversationState.IDLE,
                suggested_actions=["Need help", "Talk to human"],
                pending_action=None,
                pending_data={},
            )

        return HandlerResult(
            response_text=menu_text,
            next_state=ConversationState.IDLE,
            suggested_actions=["Order from this menu", "Show another menu"],
            pending_action=None,
            pending_data={},
        )

    # ------------------------------------------------------------------
    # Database-backed menu builder
    # ------------------------------------------------------------------

    async def _build_menu_from_db(
        self,
        db_session: AsyncSession,
        capabilities: dict[str, Any],
        requested_restaurant: Optional[str],
        requested_item: Optional[str],
    ) -> str:
        """Query the database and format the full menu."""

        hotel_id: Optional[int] = capabilities.get("hotel_id")

        # Look up hotel_id from DB by hotel name if not in capabilities
        if hotel_id is None:
            hotel_name = capabilities.get("hotel_name", "")
            if hotel_name:
                result = await db_session.execute(
                    select(Hotel).where(Hotel.name.ilike(f"%{hotel_name}%"))
                )
                hotel = result.scalar_one_or_none()
                if hotel:
                    hotel_id = hotel.id

        # If still None, try fetching the first active hotel
        if hotel_id is None:
            result = await db_session.execute(
                select(Hotel).where(Hotel.is_active == True).limit(1)  # noqa: E712
            )
            hotel = result.scalar_one_or_none()
            if hotel:
                hotel_id = hotel.id

        if hotel_id is None:
            return ""

        # Fetch active restaurants for this hotel
        stmt = select(Restaurant).where(
            Restaurant.hotel_id == hotel_id,
            Restaurant.is_active == True,  # noqa: E712
        )
        result = await db_session.execute(stmt)
        restaurants: list[Restaurant] = list(result.scalars().all())

        if not restaurants:
            return ""

        # If a specific restaurant was requested, filter to that one
        if requested_restaurant:
            restaurants = self._filter_db_restaurants(restaurants, requested_restaurant)
            if not restaurants:
                return (
                    f"Sorry, I couldn't find a restaurant matching "
                    f"'{requested_restaurant}'. Please try another name."
                )
        else:
            # Avoid duplicate sections if multiple rows resolve to same outlet.
            unique_by_name: dict[str, Restaurant] = {}
            for restaurant in restaurants:
                key = restaurant.name.strip().lower()
                unique_by_name.setdefault(key, restaurant)
            restaurants = list(unique_by_name.values())

        sections: list[str] = []

        for restaurant in restaurants:
            section = await self._format_restaurant_section(
                db_session, restaurant, requested_item
            )
            if section:
                sections.append(section)

        if not sections and requested_item:
            return ""
        return "\n\n".join(sections)

    async def _format_restaurant_section(
        self,
        db_session: AsyncSession,
        restaurant: Restaurant,
        requested_item: Optional[str] = None,
    ) -> str:
        """Format a single restaurant with its menu items grouped by category."""

        # Fetch available menu items for this restaurant
        stmt = select(MenuItem).where(
            MenuItem.restaurant_id == restaurant.id,
            MenuItem.is_available == True,  # noqa: E712
        )
        result = await db_session.execute(stmt)
        items: list[MenuItem] = list(result.scalars().all())

        if not items:
            return ""

        # Runtime dedup so duplicate DB rows do not appear twice in chat output.
        deduped_items: list[MenuItem] = []
        seen_signatures: set[tuple] = set()
        for item in items:
            signature = (
                str(item.name or "").strip().lower(),
                str(item.category or "").strip().lower(),
                str(item.price),
                str(item.currency or "").strip().upper(),
                bool(item.is_vegetarian),
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            deduped_items.append(item)
        items = deduped_items

        if requested_item:
            needle = requested_item.strip().lower()
            filtered: list[MenuItem] = []
            for item in items:
                combined = " ".join(
                    [
                        str(item.name or ""),
                        str(item.description or ""),
                        str(item.category or ""),
                    ]
                ).lower()
                if needle in combined:
                    filtered.append(item)
            items = filtered

        # Build header
        header_parts: list[str] = [f"**{restaurant.name}**"]
        if restaurant.cuisine:
            header_parts[0] += f" ({restaurant.cuisine})"

        # Hours
        if restaurant.opens_at and restaurant.closes_at:
            opens = restaurant.opens_at.strftime("%H:%M")
            closes = restaurant.closes_at.strftime("%H:%M")
            header_parts.append(f"{opens}-{closes}")
        else:
            header_parts.append("24/7")

        # Delivery info
        if restaurant.delivers_to_room:
            header_parts.append("Delivers to room")
        else:
            header_parts.append("Dine-in only")

        header = " - ".join(header_parts)

        # Group items by category
        by_category: dict[str, list[MenuItem]] = defaultdict(list)
        for item in items:
            category = item.category or "Other"
            by_category[category].append(item)

        # Build lines
        lines: list[str] = [header, ""]
        for category in sorted(by_category.keys(), key=lambda value: str(value).lower()):
            cat_items = sorted(by_category[category], key=lambda item: str(item.name).lower())
            lines.append(f"{category}:")
            for item in cat_items:
                price = self._format_price(item.price, item.currency)
                veg_tag = " (Veg)" if item.is_vegetarian else ""
                lines.append(f"- {item.name} - {price}{veg_tag}")
            lines.append("")  # blank line between categories

        return "\n".join(lines).rstrip()

    # ------------------------------------------------------------------
    # Config-based fallback menu builder
    # ------------------------------------------------------------------

    def _build_menu_from_config(
        self,
        capabilities: dict[str, Any],
        requested_restaurant: Optional[str],
        requested_item: Optional[str],
    ) -> str:
        """Build a menu summary from the config capabilities dict."""

        restaurants: list[dict[str, Any]] = capabilities.get("restaurants", [])
        if not restaurants:
            return ""

        # Filter to active only
        restaurants = [r for r in restaurants if r.get("is_active", True)]

        # Filter to specific restaurant if requested
        if requested_restaurant:
            restaurants = self._filter_config_restaurants(restaurants, requested_restaurant)
            if not restaurants:
                return (
                    f"Sorry, I couldn't find a restaurant matching "
                    f"'{requested_restaurant}'. Please try another name."
                )
        else:
            unique_by_name: dict[str, dict[str, Any]] = {}
            for restaurant in restaurants:
                key = str(restaurant.get("name") or "").strip().lower()
                if not key:
                    continue
                unique_by_name.setdefault(key, restaurant)
            restaurants = list(unique_by_name.values())

        sections: list[str] = []
        for restaurant in restaurants:
            section = self._format_config_restaurant(restaurant)
            if section:
                sections.append(section)

        if requested_item:
            # Config fallback has no item-level data to filter reliably.
            return ""
        return "\n\n".join(sections)

    @staticmethod
    def _extract_requested_item_keyword(message: str, entities: dict[str, Any]) -> Optional[str]:
        item = entities.get("item")
        if isinstance(item, str) and item.strip():
            return item.strip()
        items = entities.get("items")
        if isinstance(items, list) and items:
            first = str(items[0]).strip()
            if first:
                return first

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None
        if "menu" in msg_lower and "food" not in msg_lower and "show menu" in msg_lower:
            return None
        if "options" in msg_lower:
            option_match = re.search(r"\b([a-z][a-z\s]{1,30})\s+options?\b", msg_lower)
            if option_match:
                value = option_match.group(1).strip()
                value_tokens = [
                    token
                    for token in value.split()
                    if token not in {"show", "all", "list", "give", "me", "i", "want", "need", "please", "for", "the", "a", "an"}
                ]
                cleaned = " ".join(value_tokens).strip()
                if not cleaned:
                    return None
                if cleaned not in {"menu", "food", "room", "in room"} and not MenuHandler._is_generic_restaurant_term(cleaned):
                    return cleaned
        if "menu" in msg_lower:
            menu_match = re.search(r"\b([a-z][a-z\s]{1,30})\s+menu\b", msg_lower)
            if menu_match:
                value = menu_match.group(1).strip()
                value_tokens = [
                    token
                    for token in value.split()
                    if token not in {"show", "all", "list", "give", "me", "i", "want", "need", "please", "for", "the", "a", "an"}
                ]
                cleaned = " ".join(value_tokens).strip()
                if not cleaned:
                    return None
                if cleaned not in {"show", "food", "full", "room", "in room"} and not MenuHandler._is_generic_restaurant_term(cleaned):
                    return cleaned
        return None

    def _resolve_requested_restaurant(
        self,
        message: str,
        entities: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> Optional[str]:
        entity_candidate = str(
            entities.get("restaurant")
            or entities.get("restaurant_name")
            or ""
        ).strip()
        candidate = entity_candidate

        msg_lower = str(message or "").strip().lower()
        if entity_candidate and not self._entity_explicitly_mentioned_in_message(message, entity_candidate):
            candidate = ""
        if not candidate:
            menu_for_match = re.search(r"\bmenu\s+(?:for|of)\s+([a-z0-9\s]{2,40})\b", msg_lower)
            if menu_for_match:
                candidate = menu_for_match.group(1).strip()
        if not candidate:
            inferred = self._infer_restaurant_from_message(msg_lower, capabilities)
            if inferred:
                candidate = inferred

        if not candidate:
            return None
        if self._is_generic_restaurant_term(candidate):
            return None

        restaurants = capabilities.get("restaurants", [])
        names = [str(row.get("name") or "").strip() for row in restaurants if str(row.get("name") or "").strip()]
        if not names:
            return candidate

        best_name = ""
        best_score = 0.0
        candidate_norm = re.sub(r"[^a-z0-9]+", " ", candidate.lower()).strip()
        candidate_tokens = [token for token in candidate_norm.split() if token]
        for name in names:
            name_norm = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            if not name_norm:
                continue
            if candidate_norm in name_norm or name_norm in candidate_norm:
                return name
            name_tokens = [token for token in name_norm.split() if token]
            if candidate_tokens and all(token in name_tokens for token in candidate_tokens):
                return name
            score = SequenceMatcher(a=candidate_norm, b=name_norm).ratio()
            if score > best_score:
                best_score = score
                best_name = name
        if best_score >= 0.72:
            return best_name
        return candidate

    def _infer_restaurant_from_message(
        self,
        msg_lower: str,
        capabilities: dict[str, Any],
    ) -> Optional[str]:
        restaurants = capabilities.get("restaurants", [])
        names = [str(row.get("name") or "").strip() for row in restaurants if str(row.get("name") or "").strip()]
        if not names:
            return None

        # Common shorthand aliases used by guests.
        if any(token in msg_lower for token in ("in room", "in-room", "ird")):
            for name in names:
                compact = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
                if "in room" in compact:
                    return name

        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", msg_lower)
            if token not in {"menu", "food", "show", "for", "of", "the", "a", "an", "please"}
        ]
        if not tokens:
            return None
        query = " ".join(tokens).strip()
        if not query:
            return None

        best_name = ""
        best_score = 0.0
        for name in names:
            name_norm = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            if not name_norm:
                continue
            if query in name_norm or name_norm in query:
                return name
            query_tokens = [token for token in query.split() if token]
            name_tokens = [token for token in name_norm.split() if token]
            if query_tokens and all(token in name_tokens for token in query_tokens):
                return name

            score = SequenceMatcher(a=query, b=name_norm).ratio()
            if score > best_score:
                best_score = score
                best_name = name

        if best_score >= 0.74:
            return best_name
        return None

    @staticmethod
    def _entity_explicitly_mentioned_in_message(message: str, entity_value: str) -> bool:
        msg_compact = re.sub(r"[^a-z0-9]+", " ", str(message or "").lower()).strip()
        entity_compact = re.sub(r"[^a-z0-9]+", " ", str(entity_value or "").lower()).strip()
        if not msg_compact or not entity_compact:
            return False
        if entity_compact in msg_compact:
            return True

        entity_tokens = [token for token in entity_compact.split() if token]
        msg_tokens = [token for token in msg_compact.split() if token]
        if not entity_tokens or not msg_tokens:
            return False
        for token in entity_tokens:
            if any(
                msg_token == token
                or SequenceMatcher(a=msg_token, b=token).ratio() >= 0.86
                for msg_token in msg_tokens
            ):
                continue
            return False
        return True

    @staticmethod
    def _is_generic_restaurant_term(value: str) -> bool:
        compact = re.sub(r"[^a-z]", "", str(value or "").lower())
        if not compact:
            return True
        generic_tokens = {"restaurant", "restaurants", "restuarant", "retarant", "food", "dining", "outlet", "menu"}
        if compact in generic_tokens:
            return True
        return any(SequenceMatcher(a=compact, b=token).ratio() >= 0.82 for token in generic_tokens)

    @staticmethod
    def _filter_db_restaurants(restaurants: list[Restaurant], requested_restaurant: str) -> list[Restaurant]:
        needle = re.sub(r"[^a-z0-9]+", " ", str(requested_restaurant or "").lower()).strip()
        if not needle:
            return restaurants

        exact = [row for row in restaurants if needle in re.sub(r"[^a-z0-9]+", " ", str(row.name or "").lower()).strip()]
        if exact:
            return exact

        best_score = 0.0
        best_matches: list[Restaurant] = []
        for row in restaurants:
            name_norm = re.sub(r"[^a-z0-9]+", " ", str(row.name or "").lower()).strip()
            if not name_norm:
                continue
            score = SequenceMatcher(a=needle, b=name_norm).ratio()
            if score > best_score:
                best_score = score
                best_matches = [row]
            elif abs(score - best_score) < 0.01:
                best_matches.append(row)
        return best_matches if best_score >= 0.72 else []

    @staticmethod
    def _filter_config_restaurants(restaurants: list[dict[str, Any]], requested_restaurant: str) -> list[dict[str, Any]]:
        needle = re.sub(r"[^a-z0-9]+", " ", str(requested_restaurant or "").lower()).strip()
        if not needle:
            return restaurants

        exact = []
        for row in restaurants:
            name_norm = re.sub(r"[^a-z0-9]+", " ", str(row.get("name") or "").lower()).strip()
            if needle in name_norm:
                exact.append(row)
        if exact:
            return exact

        best_score = 0.0
        best_matches: list[dict[str, Any]] = []
        for row in restaurants:
            name_norm = re.sub(r"[^a-z0-9]+", " ", str(row.get("name") or "").lower()).strip()
            if not name_norm:
                continue
            score = SequenceMatcher(a=needle, b=name_norm).ratio()
            if score > best_score:
                best_score = score
                best_matches = [row]
            elif abs(score - best_score) < 0.01:
                best_matches.append(row)
        return best_matches if best_score >= 0.72 else []

    def _format_config_restaurant(self, restaurant: dict[str, Any]) -> str:
        """Format a single restaurant entry from config (no item-level detail)."""

        name = restaurant.get("name", "Restaurant")
        cuisine = restaurant.get("cuisine", "")
        hours = restaurant.get("hours", "24/7")
        delivers = restaurant.get("delivers_to_room", False)
        dine_in_only = restaurant.get("dine_in_only", False)

        header = f"**{name}**"
        if cuisine:
            header += f" ({cuisine})"
        header += f" - {hours}"

        if delivers:
            header += ", Delivers to room"
        elif dine_in_only:
            header += ", Dine-in only"

        return header

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_price(price: Decimal | float, currency: str = "INR") -> str:
        """Format a price value for display."""
        symbol_map = {"INR": "Rs.", "USD": "$", "EUR": "E", "GBP": "P"}
        symbol = symbol_map.get(currency, currency)
        # Convert Decimal to float for formatting
        numeric = float(price)
        if numeric == int(numeric):
            return f"{symbol}{int(numeric)}"
        return f"{symbol}{numeric:.2f}"
