"""
Order Handler for KePSLA Bot v2

Handles ORDER_FOOD intent (building order summaries from DB menu items)
and CONFIRMATION_YES when pending_action is "confirm_order" (creating
Order + OrderItem records in the database).
"""

from __future__ import annotations

from difflib import SequenceMatcher
import logging
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select

from handlers.base_handler import BaseHandler, HandlerResult
from models.database import Guest, MenuItem, Order, OrderItem, Restaurant, Hotel
from schemas.chat import ConversationContext, ConversationState, IntentResult, IntentType
from services.config_service import config_service
from services.ticketing_agent_service import ticketing_agent_service
from services.ticketing_service import ticketing_service

logger = logging.getLogger(__name__)


class OrderHandler(BaseHandler):
    """Handles food ordering flow: item lookup, summary, and confirmation."""

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict,
        db_session=None,
    ) -> Optional[HandlerResult]:
        try:
            if intent_result.intent == IntentType.ORDER_FOOD:
                return await self._handle_order_request(
                    message, intent_result, context, capabilities, db_session
                )

            if (
                intent_result.intent == IntentType.CONFIRMATION_YES
                and context.pending_action == "confirm_order"
            ):
                return await self._handle_order_confirmation(
                    message, intent_result, context, capabilities, db_session
                )

            # Not our responsibility (e.g. CONFIRMATION_YES with pending_action="show_menu")
            return None

        except Exception as exc:
            logger.exception("OrderHandler error: %s", exc)
            return HandlerResult(
                response_text="Sorry, something went wrong while processing your order. Please try again.",
                next_state=ConversationState.IDLE,
            )

    # ------------------------------------------------------------------
    # Case 1: ORDER_FOOD -- look up menu items and build a summary
    # ------------------------------------------------------------------
    async def _handle_order_request(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict,
        db_session,
    ) -> Optional[HandlerResult]:
        if db_session is None:
            return None  # fall back to LLM

        entities = intent_result.entities

        # Handle both "items" (list) and "item" (string) from LLM
        raw_items = entities.get("items") or entities.get("item") or []
        if isinstance(raw_items, str):
            # LLM returned a single string - split by comma or wrap in list
            item_names = [name.strip() for name in raw_items.split(",") if name.strip()]
        elif isinstance(raw_items, list):
            item_names = [str(name).strip() for name in raw_items if name]
        else:
            item_names = []

        restaurant_name: str = entities.get("restaurant", "")

        if not item_names:
            return HandlerResult(
                response_text="What would you like to order? Please mention the item names.",
                next_state=ConversationState.AWAITING_INFO,
                suggested_actions=["Try another item", "Cancel"],
            )

        # Resolve restaurant_id if a restaurant name was provided
        restaurant_id: Optional[int] = None
        if restaurant_name:
            rest_result = await db_session.execute(
                select(Restaurant).where(
                    Restaurant.name.ilike(f"%{restaurant_name}%"),
                    Restaurant.is_active == True,
                )
            )
            restaurant_row = rest_result.scalar_one_or_none()
            if restaurant_row:
                restaurant_id = restaurant_row.id

        # Build active candidate pool once; use for exact + fuzzy item matching.
        candidate_stmt = (
            select(MenuItem)
            .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
            .where(
                MenuItem.is_available == True,  # noqa: E712
                Restaurant.is_active == True,  # noqa: E712
            )
        )
        if restaurant_id is not None:
            candidate_stmt = candidate_stmt.where(MenuItem.restaurant_id == restaurant_id)
        candidate_rows = list((await db_session.execute(candidate_stmt)).scalars().all())

        # Search for each requested item in the menu.
        matched_items: list[dict[str, Any]] = []
        not_found: list[str] = []
        seen_menu_item_ids: set[int] = set()

        for item_name in item_names:
            menu_item = self._find_exact_item(item_name, candidate_rows)
            if menu_item is None:
                menu_item = self._find_fuzzy_item(item_name, candidate_rows)

            if menu_item:
                if menu_item.id in seen_menu_item_ids:
                    continue
                seen_menu_item_ids.add(menu_item.id)
                matched_items.append(
                    {
                        "menu_item_id": menu_item.id,
                        "name": menu_item.name,
                        "price": float(menu_item.price),
                        "restaurant_id": menu_item.restaurant_id,
                        "quantity": 1,
                    }
                )
            else:
                not_found.append(item_name)

        if not matched_items:
            category_matches = self._find_items_by_category_keywords(item_names, candidate_rows)
            if category_matches:
                options = category_matches[:8]
                lines = [f"{idx}. {item.name} - Rs.{float(item.price):.0f}" for idx, item in enumerate(options, start=1)]
                option_names = [item.name for item in options[:4]]
                return HandlerResult(
                    response_text=(
                        "I couldn't match an exact item, but these options are available:\n\n"
                        + "\n".join(lines)
                        + "\n\nPlease tell me which item you'd like to order."
                    ),
                    next_state=ConversationState.AWAITING_INFO,
                    suggested_actions=option_names or ["Try another item", "Talk to human"],
                )

            suggestions = self._suggest_similar_items(item_names, candidate_rows)
            suggestion_text = ""
            suggestion_actions = ["Try another item", "Talk to human"]
            if suggestions:
                suggestion_text = f"\n\nDid you mean: {', '.join(suggestions[:5])}?"
                suggestion_actions = suggestions[:4]

            return HandlerResult(
                response_text=(
                    f"Sorry, I couldn't find any of the requested items "
                    f"({', '.join(not_found)}) in the current order catalog. "
                    f"Please try another item name or ask our team for assistance."
                    f"{suggestion_text}"
                ),
                next_state=ConversationState.AWAITING_INFO,
                suggested_actions=suggestion_actions,
            )

        # Build order summary
        total = sum(item["price"] * item["quantity"] for item in matched_items)
        lines = [f"{idx}. {item['name']} - Rs.{item['price']:.0f}"
                 for idx, item in enumerate(matched_items, start=1)]
        summary = "\n".join(lines)

        not_found_note = ""
        if not_found:
            not_found_note = (
                f"\n\nNote: Could not find: {', '.join(not_found)}. "
                f"Please share alternative item names."
            )

        # Use the restaurant_id from the first matched item if not already set
        effective_restaurant_id = restaurant_id or matched_items[0]["restaurant_id"]

        response = (
            f"Here's your order summary:\n\n"
            f"{summary}\n\n"
            f"Total: Rs.{total:.0f}"
            f"{not_found_note}\n\n"
            f"Shall I confirm this order? (Yes/No)"
        )

        return HandlerResult(
            response_text=response,
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_order",
            pending_data={
                "items": matched_items,
                "restaurant_id": effective_restaurant_id,
                "total": total,
            },
            suggested_actions=["Yes, confirm", "No, cancel"],
        )

    # ------------------------------------------------------------------
    # Case 2: CONFIRMATION_YES + pending_action == "confirm_order"
    # ------------------------------------------------------------------
    async def _handle_order_confirmation(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict,
        db_session,
    ) -> HandlerResult:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        raw_items = pending.get("items", [])
        items: list[dict[str, Any]] = self._normalize_pending_items(raw_items)
        if not items:
            items = await self._rebuild_items_from_slot_pending(pending, db_session)
        restaurant_id: int = int(pending.get("restaurant_id", 1) or 1)
        total = self._safe_float(pending.get("total", 0.0), default=0.0)
        if not total:
            total = self._safe_float(pending.get("order_total", 0.0), default=0.0)

        if not items:
            return HandlerResult(
                response_text="I don't have the order items yet. Please tell me which dish you'd like to order first.",
                next_state=ConversationState.AWAITING_INFO,
                pending_action=None,
                pending_data={},
                suggested_actions=["Show menu", "Order food", "Talk to human"],
            )

        # Recalculate total as safety net
        if not total and items:
            total = sum(
                self._safe_float(i.get("price", 0.0), default=0.0)
                * max(1, int(self._safe_float(i.get("quantity", 1), default=1)))
                for i in items
            )

        first_item_restaurant_id = int(
            self._safe_float(items[0].get("restaurant_id", 0), default=0)
        ) if items else 0
        if first_item_restaurant_id > 0:
            restaurant_id = first_item_restaurant_id

        db_ready = all("menu_item_id" in item for item in items)

        # ------ DB path: create real Order + OrderItems ------
        if db_session is not None and db_ready:
            try:
                # Resolve hotel_id (default 1 for single-hotel setup)
                hotel_id = await self._resolve_hotel_id(db_session, capabilities)

                # Get or create guest
                guest = await self._get_or_create_guest(
                    db_session,
                    hotel_id=hotel_id,
                    phone=context.guest_phone,
                    name=context.guest_name,
                )

                # Create Order
                order = Order(
                    guest_id=guest.id,
                    restaurant_id=restaurant_id,
                    status="pending",
                    total_amount=Decimal(str(total)),
                    delivery_location=context.room_number or "lobby",
                )
                db_session.add(order)
                await db_session.flush()  # get order.id

                # Create OrderItems
                for item in items:
                    order_item = OrderItem(
                        order_id=order.id,
                        menu_item_id=int(item["menu_item_id"]),
                        quantity=max(1, int(self._safe_float(item.get("quantity", 1), default=1))),
                        unit_price=Decimal(str(self._safe_float(item.get("price", 0.0), default=0.0))),
                    )
                    db_session.add(order_item)

                await db_session.commit()

                item_list = ", ".join(i.get("name", "Item") for i in items)
                ticket_meta = await self._maybe_create_order_ticket(
                    context=context,
                    capabilities=capabilities,
                    items=items,
                    total=total,
                    order_ref=str(order.id),
                )
                return HandlerResult(
                    response_text=(
                        f"Your order has been confirmed!\n\n"
                        f"Order ID: {order.id}\n"
                        f"Items: {item_list}\n"
                        f"Total: Rs.{total:.0f}\n"
                        f"Estimated delivery: 25-30 minutes"
                    ),
                    next_state=ConversationState.COMPLETED,
                    suggested_actions=["Order more", "Need help", "Talk to human"],
                    metadata={
                        "order_id": order.id,
                        "order_items": [i["name"] for i in items],
                        "order_total": float(total),
                        "restaurant_id": restaurant_id,
                        **ticket_meta,
                    },
                )
            except Exception as exc:
                logger.exception("Failed to create order in DB: %s", exc)
                # Rollback and fall through to fallback
                await db_session.rollback()

        # ------ Fallback path (no db_session or DB error) ------
        fake_order_id = f"ORD-{abs(hash(str(items))) % 10000:04d}"
        item_list = ", ".join(i.get("name", "Item") for i in items)
        total_line = f"Total: Rs.{total:.0f}" if total > 0 else "Total: To be confirmed"
        ticket_meta = await self._maybe_create_order_ticket(
            context=context,
            capabilities=capabilities,
            items=items,
            total=total,
            order_ref=fake_order_id,
        )
        return HandlerResult(
            response_text=(
                f"Your order has been confirmed!\n\n"
                f"Order ID: {fake_order_id}\n"
                f"Items: {item_list}\n"
                f"{total_line}\n"
                f"Estimated delivery: 25-30 minutes"
            ),
            next_state=ConversationState.COMPLETED,
            suggested_actions=["Order more", "Need help", "Talk to human"],
            metadata={
                "order_id": fake_order_id,
                "order_items": [i["name"] for i in items],
                "order_total": float(total),
                "restaurant_id": restaurant_id,
                **ticket_meta,
            },
        )

    async def _rebuild_items_from_slot_pending(
        self,
        pending: dict[str, Any],
        db_session,
    ) -> list[dict[str, Any]]:
        """
        Rebuild normalized pending items from slot-based order fields used by
        full-LLM flows (order_item/order_quantity/etc.), not only items[].
        """
        if not isinstance(pending, dict):
            return []

        item_name = self._first_non_empty(
            pending.get("order_item"),
            pending.get("selected_item"),
            pending.get("item_name"),
            pending.get("dish_name"),
            pending.get("requested_item"),
        )
        if not item_name:
            return []

        quantity = max(
            1,
            int(
                self._safe_float(
                    self._first_non_empty(
                        pending.get("order_quantity"),
                        pending.get("quantity"),
                        1,
                    ),
                    default=1,
                )
            ),
        )

        restaurant_id = int(self._safe_float(pending.get("restaurant_id", 0), default=0))
        price = self._safe_float(
            self._first_non_empty(
                pending.get("price"),
                pending.get("unit_price"),
                pending.get("item_price"),
                0.0,
            ),
            default=0.0,
        )

        reconstructed: dict[str, Any] = {
            "name": str(item_name).strip(),
            "quantity": quantity,
            "price": price,
        }
        if restaurant_id > 0:
            reconstructed["restaurant_id"] = restaurant_id

        if db_session is not None:
            candidate_stmt = (
                select(MenuItem)
                .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
                .where(
                    MenuItem.is_available == True,  # noqa: E712
                    Restaurant.is_active == True,  # noqa: E712
                )
            )
            if restaurant_id > 0:
                candidate_stmt = candidate_stmt.where(MenuItem.restaurant_id == restaurant_id)
            candidates = list((await db_session.execute(candidate_stmt)).scalars().all())
            menu_item = self._find_exact_item(str(item_name), candidates)
            if menu_item is None:
                menu_item = self._find_fuzzy_item(str(item_name), candidates)
            if menu_item is not None:
                reconstructed["menu_item_id"] = int(menu_item.id)
                reconstructed["restaurant_id"] = int(menu_item.restaurant_id)
                reconstructed["name"] = str(menu_item.name or reconstructed["name"]).strip()
                if price <= 0:
                    reconstructed["price"] = self._safe_float(menu_item.price, default=0.0)

        return [reconstructed]

    async def _maybe_create_order_ticket(
        self,
        *,
        context: ConversationContext,
        capabilities: dict[str, Any],
        items: list[dict[str, Any]],
        total: float,
        order_ref: str,
    ) -> dict[str, Any]:
        """
        Lumira-style operational ticket for confirmed orders.
        Non-blocking: order confirmation should succeed even if ticketing fails.
        """
        if not ticketing_service.is_ticketing_enabled(capabilities):
            return {"ticket_created": False, "ticket_skipped": True}

        item_names = [str(i.get("name") or "").strip() for i in items if str(i.get("name") or "").strip()]
        joined_items = ", ".join(item_names) if item_names else "requested food items"
        issue = (
            f"Food order confirmed ({order_ref}): {joined_items}. "
            f"Total amount Rs.{float(total):.0f}."
        ).strip()
        configured_cases = ticketing_agent_service.get_configured_cases()
        if not configured_cases:
            return {
                "ticket_created": False,
                "ticket_skipped": True,
                "ticket_skip_reason": "no_configured_ticket_cases",
                "ticket_source": "order_handler",
            }
        matched_case = await ticketing_agent_service.match_configured_case_async(
            message=issue,
            conversation_excerpt=self._build_ticketing_case_context_text(
                context=context,
                latest_issue=issue,
            ),
            llm_response_text="",
        )
        if not matched_case:
            return {
                "ticket_created": False,
                "ticket_skipped": True,
                "ticket_skip_reason": "no_matching_configured_ticket_case",
                "ticket_source": "order_handler",
            }

        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category="request",
                sub_category="order_food",
                priority="medium",
                phase="during_stay",
            )
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            return {
                "ticket_created": False,
                "ticket_create_error": str(exc),
                "ticket_source": "order_handler",
            }

        if not create_result.success:
            return {
                "ticket_created": False,
                "ticket_create_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": "order_handler",
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        return {
            "ticket_created": True,
            "ticket_id": str(create_result.ticket_id or "").strip(),
            "ticket_status": "open",
            "ticket_category": "request",
            "ticket_sub_category": "order_food",
            "ticket_priority": "medium",
            "ticket_source": "order_handler",
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
        }

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return ""

    @staticmethod
    def _build_ticketing_case_context_text(
        *,
        context: ConversationContext,
        latest_issue: str,
        max_messages: int = 10,
    ) -> str:
        lines: list[str] = []
        for msg in context.get_recent_messages(max_messages):
            role = "User" if msg.role.value == "user" else "Assistant"
            content = str(msg.content or "").strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")
        if latest_issue:
            lines.append(f"Ticket Issue Draft: {latest_issue}")
        joined = "\n".join(lines).strip()
        if len(joined) <= 1500:
            return joined
        return joined[-1500:]

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _normalize_pending_items(self, raw_items: Any) -> list[dict[str, Any]]:
        """
        Normalize pending order items so confirmation remains robust even when
        fallback/LLM paths stored string-based item lists.
        """
        if isinstance(raw_items, dict):
            items_source = [raw_items]
        elif isinstance(raw_items, str):
            items_source = [chunk.strip() for chunk in raw_items.split(",") if chunk.strip()]
        elif isinstance(raw_items, list):
            items_source = raw_items
        else:
            items_source = []

        normalized: list[dict[str, Any]] = []
        for item in items_source:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("item") or "").strip()
                menu_item_id = item.get("menu_item_id")
                quantity = max(1, int(self._safe_float(item.get("quantity", 1), default=1)))
                price = self._safe_float(item.get("price", 0.0), default=0.0)

                normalized_item: dict[str, Any] = {
                    "name": name or (f"Item {menu_item_id}" if menu_item_id is not None else "Item"),
                    "quantity": quantity,
                    "price": price,
                }
                if menu_item_id is not None:
                    normalized_item["menu_item_id"] = menu_item_id
                if item.get("restaurant_id") is not None:
                    normalized_item["restaurant_id"] = item.get("restaurant_id")
                normalized.append(normalized_item)
                continue

            item_name = str(item or "").strip()
            if item_name:
                normalized.append(
                    {
                        "name": item_name,
                        "quantity": 1,
                        "price": 0.0,
                    }
                )

        return normalized

    @staticmethod
    def _find_exact_item(item_name: str, candidates: list[MenuItem]) -> Optional[MenuItem]:
        needle = str(item_name or "").strip().lower()
        if not needle:
            return None
        # Prefer exact full-word containment.
        for candidate in candidates:
            name = str(candidate.name or "").strip().lower()
            if not name:
                continue
            if needle in name or name in needle:
                return candidate
        return None

    @staticmethod
    def _find_fuzzy_item(item_name: str, candidates: list[MenuItem], threshold: float = 0.72) -> Optional[MenuItem]:
        needle = str(item_name or "").strip().lower()
        if not needle:
            return None

        best_ratio = 0.0
        best_candidate: Optional[MenuItem] = None
        for candidate in candidates:
            name = str(candidate.name or "").strip().lower()
            if not name:
                continue
            ratio = SequenceMatcher(a=needle, b=name).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_candidate = candidate

        if best_candidate is None or best_ratio < threshold:
            return None
        return best_candidate

    @staticmethod
    def _find_items_by_category_keywords(item_names: list[str], candidates: list[MenuItem]) -> list[MenuItem]:
        """Return top items by broad category hints (snacks, drinks, desserts, etc.)."""
        if not item_names or not candidates:
            return []

        category_aliases = {
            "snacks": {"snack", "snacks", "small plate", "starters", "starter", "side", "sides", "appetizer"},
            "beverages": {"drink", "drinks", "beverage", "beverages", "wine", "cocktail", "juice", "tea", "coffee"},
            "desserts": {"dessert", "desserts", "sweet", "sweets", "cake", "pastry"},
            "pizza": {"pizza"},
            "pasta": {"pasta"},
            "soups": {"soup", "soups"},
        }

        requested_tokens: set[str] = set()
        for name in item_names:
            normalized = str(name or "").strip().lower()
            if not normalized:
                continue
            requested_tokens.update(token for token in normalized.replace("-", " ").split() if token)
            requested_tokens.add(normalized)

        target_categories: set[str] = set()
        for canonical, aliases in category_aliases.items():
            if any(alias in requested_tokens for alias in aliases):
                target_categories.add(canonical)
        if not target_categories:
            return []

        def _candidate_matches(candidate: MenuItem) -> bool:
            text = " ".join(
                [
                    str(candidate.name or "").lower(),
                    str(candidate.category or "").lower(),
                    str(candidate.description or "").lower(),
                ]
            )
            for canonical in target_categories:
                aliases = category_aliases.get(canonical, {canonical})
                if any(alias in text for alias in aliases):
                    return True
            return False

        matched: list[MenuItem] = []
        for candidate in candidates:
            if _candidate_matches(candidate):
                matched.append(candidate)
            if len(matched) >= 12:
                break
        return matched

    @staticmethod
    def _suggest_similar_items(item_names: list[str], candidates: list[MenuItem]) -> list[str]:
        """Generate human-friendly fallback suggestions for typo/noise inputs."""
        if not item_names or not candidates:
            return []

        scored: list[tuple[float, str]] = []
        seen: set[str] = set()
        for query in item_names:
            query_norm = str(query or "").strip().lower()
            if not query_norm:
                continue
            query_tokens = [token for token in query_norm.replace("-", " ").split() if token]
            for candidate in candidates:
                name = str(candidate.name or "").strip()
                if not name:
                    continue
                name_norm = name.lower()
                if name_norm in seen:
                    continue
                token_overlap = 0.0
                if query_tokens:
                    token_overlap = max(
                        SequenceMatcher(a=token, b=part).ratio()
                        for token in query_tokens
                        for part in name_norm.split()
                    )
                full_ratio = SequenceMatcher(a=query_norm, b=name_norm).ratio()
                score = max(full_ratio, token_overlap)
                if score >= 0.58:
                    scored.append((score, name))

        scored.sort(key=lambda item: item[0], reverse=True)
        suggestions: list[str] = []
        for _, name in scored:
            if name in suggestions:
                continue
            suggestions.append(name)
            if len(suggestions) >= 6:
                break
        return suggestions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _resolve_hotel_id(self, db_session, capabilities: dict) -> int:
        """Resolve hotel_id from capabilities or default to 1."""
        hotel_name = capabilities.get("hotel_name", "")
        if hotel_name:
            result = await db_session.execute(
                select(Hotel.id).where(Hotel.name.ilike(f"%{hotel_name}%"))
            )
            hotel_id = result.scalar_one_or_none()
            if hotel_id:
                return hotel_id
        return 1

    async def _get_or_create_guest(
        self,
        db_session,
        hotel_id: int,
        phone: Optional[str],
        name: Optional[str],
    ) -> Guest:
        """Look up guest by phone in new_bot_guests; create if not found."""
        effective_phone = phone or "walk-in"

        guest_result = await db_session.execute(
            select(Guest).where(
                Guest.hotel_id == hotel_id,
                Guest.phone_number == effective_phone,
            )
        )
        guest = guest_result.scalar_one_or_none()

        if not guest:
            guest = Guest(
                hotel_id=hotel_id,
                phone_number=effective_phone,
                name=name or "Guest",
            )
            db_session.add(guest)
            await db_session.flush()

        return guest
