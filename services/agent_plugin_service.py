"""
Service Agent Plugin Runtime

Deterministic runtime for config-driven service plugins created from the
Admin Agent Builder. It runs before LLM classification to keep latency low.
"""

from __future__ import annotations

from datetime import UTC, datetime
from difflib import SequenceMatcher
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from schemas.chat import ConversationContext, ConversationState
from services.config_service import config_service


class AgentPluginRuntimeResult(BaseModel):
    """Result for one plugin-runtime evaluation."""

    handled: bool = False
    response_text: str = ""
    next_state: ConversationState = ConversationState.IDLE
    pending_action: Optional[str] = None
    pending_data: dict[str, Any] = Field(default_factory=dict)
    suggested_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentPluginService:
    """Deterministic service-agent runtime."""

    RUNTIME_KEY = "_agent_runtime"
    PENDING_PREFIX = "agent_plugin:"

    _CANCEL_MARKERS = {
        "cancel",
        "stop",
        "exit",
        "quit",
        "never mind",
        "nevermind",
        "back",
        "back to bot",
        "return to bot",
    }
    _YES_MARKERS = {"yes", "y", "yes confirm", "confirm", "ok", "okay", "proceed"}
    _NO_MARKERS = {"no", "n", "change", "edit", "not now"}
    _INFORMATION_QUERY_PREFIXES = {
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "is",
        "are",
        "do",
        "does",
        "can",
        "could",
    }
    _INFORMATION_QUERY_VERBS = {"show", "list", "tell", "give", "share", "display", "write", "explain", "plan", "compose", "draft"}
    _INFORMATION_QUERY_TERMS = {
        "timing",
        "timings",
        "hours",
        "price",
        "prices",
        "cost",
        "policy",
        "rules",
        "menu",
        "menus",
        "available",
        "availability",
        "details",
        "items",
        "item",
        "dish",
        "dishes",
        "dessert",
        "desserts",
        "breakfast",
        "lunch",
        "dinner",
        "support",
        "escalation",
        "therapist",
        "unavailable",
    }
    _LISTING_QUERY_MARKERS = {
        "show",
        "list",
        "available",
        "options",
        "what do you have",
        "what's available",
        "what is available",
        "menu",
        "menus",
    }
    _TRANSACTIONAL_MARKERS = {
        "order",
        "book",
        "reserve",
        "booking",
        "appointment",
        "appoint",
        "confirm",
        "cancel",
        "deliver",
        "send",
        "place",
        "buy",
        "pay",
    }
    _TOKEN_STOPWORDS = {
        "a",
        "an",
        "the",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "is",
        "are",
        "be",
        "i",
        "me",
        "my",
        "we",
        "you",
        "your",
        "our",
        "with",
        "from",
        "this",
        "that",
        "it",
        "do",
        "does",
        "can",
        "could",
        "would",
        "should",
        "please",
        "want",
        "need",
        "have",
        "has",
        "am",
        "im",
        "hi",
        "hello",
    }
    _MENU_FACT_TEXT_LIMIT = 800
    _DOMAIN_TOKENS = {
        "spa",
        "pool",
        "swim",
        "swimming",
        "restaurant",
        "dining",
        "housekeeping",
        "concierge",
        "transport",
        "airport",
        "massage",
        "wellness",
        "laundry",
        "cab",
        "shuttle",
    }
    _GENERIC_PLUGIN_TOKENS = {
        "service",
        "services",
        "room",
        "rooms",
        "menu",
        "menus",
        "info",
        "information",
        "request",
        "requests",
        "booking",
        "bookings",
        "agent",
        "help",
        "support",
        "details",
        "available",
        "availability",
        "timing",
        "timings",
        "price",
        "prices",
        "cost",
        "clean",
        "full",
        "hotel",
        "guest",
    }
    _GENERIC_QUERY_TOKENS = {
        "timing",
        "timings",
        "timng",
        "timngs",
        "time",
        "rate",
        "rates",
        "price",
        "prices",
        "cost",
        "costs",
        "available",
        "availability",
        "detail",
        "details",
        "info",
        "information",
    }

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")

    def _is_plugin_pending(self, pending_action: Optional[str]) -> bool:
        return str(pending_action or "").strip().lower().startswith(self.PENDING_PREFIX)

    @staticmethod
    def _normalize_marker_text(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
        return re.sub(r"\s+", " ", normalized)

    def _get_runtime_state(self, context: ConversationContext) -> dict[str, Any]:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        runtime = pending.get(self.RUNTIME_KEY)
        if isinstance(runtime, dict):
            return dict(runtime)
        return {}

    def _build_pending_data(
        self,
        context: ConversationContext,
        runtime_state: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        pending = dict(context.pending_data) if isinstance(context.pending_data, dict) else {}
        pending.pop(self.RUNTIME_KEY, None)
        if runtime_state:
            pending[self.RUNTIME_KEY] = runtime_state
        return pending

    @staticmethod
    def _first_missing_slot(plugin: dict[str, Any], collected_slots: dict[str, Any]) -> Optional[dict[str, Any]]:
        slots = plugin.get("slot_schema", [])
        if not isinstance(slots, list):
            return None
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            if not bool(slot.get("required", True)):
                continue
            slot_id = str(slot.get("id") or "").strip()
            if not slot_id:
                continue
            slot_value = str(collected_slots.get(slot_id) or "").strip()
            if not slot_value:
                return slot
        return None

    @staticmethod
    def _extract_pending_slot_id(pending_action: Optional[str]) -> Optional[str]:
        pending = str(pending_action or "").strip()
        match = re.match(r"^agent_plugin:[a-z0-9_]+:collect:([a-z0-9_]+)$", pending)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _contains_phrase(message_lower: str, phrase: str) -> bool:
        normalized_phrase = str(phrase or "").strip().lower()
        if not normalized_phrase:
            return False

        # Prefer bounded matching for alphanumeric phrases to reduce false positives
        # (e.g., "spa" should not match "space").
        if re.fullmatch(r"[a-z0-9 ]+", normalized_phrase):
            phrase_tokens = [re.escape(token) for token in normalized_phrase.strip().split() if token]
            phrase_pattern = r"\s+".join(phrase_tokens)
            pattern = rf"(?<![a-z0-9]){phrase_pattern}(?![a-z0-9])"
            return re.search(pattern, message_lower) is not None

        return normalized_phrase in message_lower

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        raw_tokens = [token for token in re.findall(r"[a-z0-9]+", str(text or "").lower()) if token]
        tokens: set[str] = set()
        for token in raw_tokens:
            if token in AgentPluginService._TOKEN_STOPWORDS:
                continue
            if len(token) <= 1:
                continue
            tokens.add(token)
            if token.endswith("s") and len(token) > 3:
                singular = token[:-1]
                if singular and singular not in AgentPluginService._TOKEN_STOPWORDS:
                    tokens.add(singular)
        return tokens

    @staticmethod
    def _is_similar_token(left: str, right: str) -> bool:
        a = str(left or "").strip().lower()
        b = str(right or "").strip().lower()
        if not a or not b:
            return False
        if a == b:
            return True
        if len(a) < 3 or len(b) < 3:
            return False
        if abs(len(a) - len(b)) > 2:
            return False
        return SequenceMatcher(None, a, b).ratio() >= 0.84

    def _fuzzy_overlap_count(
        self,
        message_tokens: set[str],
        known_tokens: set[str],
    ) -> int:
        if not message_tokens or not known_tokens:
            return 0
        count = 0
        for mt in message_tokens:
            if mt in known_tokens:
                continue
            if any(self._is_similar_token(mt, kt) for kt in known_tokens):
                count += 1
        return count

    @staticmethod
    def _looks_like_listing_query(message: str) -> bool:
        message_lower = str(message or "").strip().lower()
        if not message_lower:
            return False
        if any(marker in message_lower for marker in AgentPluginService._LISTING_QUERY_MARKERS):
            return True
        return False

    @staticmethod
    def _looks_like_transaction_request(message: str) -> bool:
        message_lower = str(message or "").strip().lower()
        if not message_lower:
            return False
        tokens = re.findall(r"[a-z0-9]+", message_lower)
        return any(token in AgentPluginService._TRANSACTIONAL_MARKERS for token in tokens)

    @staticmethod
    def _looks_like_time_reply(message: str) -> bool:
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False
        if any(
            token in msg_lower
            for token in (
                "today",
                "tomorrow",
                "tonight",
                "am",
                "pm",
                "morning",
                "afternoon",
                "evening",
                "night",
                "noon",
                "midnight",
                "breakfast",
                "lunch",
                "dinner",
            )
        ):
            return True
        return bool(
            re.search(
                r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
                msg_lower,
            )
        )

    def _is_information_query(self, message: str) -> bool:
        message_text = str(message or "").strip().lower()
        if not message_text:
            return False
        if "?" in message_text:
            return True
        tokens = re.findall(r"[a-z0-9]+", message_text)
        if not tokens:
            return False
        first = tokens[0]
        if first in self._INFORMATION_QUERY_PREFIXES or first in self._INFORMATION_QUERY_VERBS:
            return True
        if any(token in self._INFORMATION_QUERY_TERMS for token in tokens):
            return True
        # Fuzzy keyword checks handle typo-heavy user queries
        # (for example: "ruls" -> "rules").
        for token in tokens:
            if any(self._is_similar_token(token, term) for term in self._INFORMATION_QUERY_TERMS):
                return True
        if "what do you have" in message_text or "what all do you have" in message_text:
            return True
        return False

    def _approved_facts(self, plugin: dict[str, Any]) -> list[dict[str, Any]]:
        plugin_id = self._normalize_identifier(plugin.get("id"))
        service_id = self._normalize_identifier(plugin.get("service_id"))

        # Preferred artifact source: service-scoped KB record.
        kb_record = config_service.get_service_kb_record(
            service_id=service_id or None,
            plugin_id=plugin_id or None,
            active_only=True,
        )
        facts = []
        if isinstance(kb_record, dict):
            kb_facts = kb_record.get("facts", [])
            if isinstance(kb_facts, list):
                facts = kb_facts

        # Backward compatibility fallback: plugin-local fact list.
        if not facts:
            plugin_facts = plugin.get("knowledge_facts", [])
            if isinstance(plugin_facts, list):
                facts = plugin_facts

        approved: list[dict[str, Any]] = []
        seen_texts: set[str] = set()
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            status = str(fact.get("status") or "").strip().lower()
            text = str(fact.get("text") or "").strip()
            if status == "approved" and text:
                dedupe_key = text.lower()
                if dedupe_key in seen_texts:
                    continue
                seen_texts.add(dedupe_key)
                approved.append(fact)

        # Service-KB menu documents are also strict, service-scoped data.
        if isinstance(kb_record, dict):
            menu_docs = kb_record.get("menu_documents", [])
            if isinstance(menu_docs, list):
                for menu_fact in self._menu_document_facts(menu_docs):
                    text = str(menu_fact.get("text") or "").strip()
                    if not text:
                        continue
                    dedupe_key = text.lower()
                    if dedupe_key in seen_texts:
                        continue
                    seen_texts.add(dedupe_key)
                    approved.append(menu_fact)
        return approved

    def _rank_facts(
        self,
        message: str,
        approved_facts: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], int]]:
        message_tokens = self._tokenize(message)
        if not message_tokens:
            return []

        ranked: list[tuple[dict[str, Any], int]] = []
        for fact in approved_facts:
            text = str(fact.get("text") or "")
            source = str(fact.get("source") or "")
            tags = fact.get("tags", [])
            if isinstance(tags, list):
                tags_text = " ".join(str(tag) for tag in tags)
            else:
                tags_text = ""
            fact_tokens = self._tokenize(f"{text} {source} {tags_text}")
            if not fact_tokens:
                continue
            overlap = len(message_tokens & fact_tokens)
            if overlap <= 0:
                continue
            ranked.append((fact, overlap))

        ranked.sort(
            key=lambda item: (
                -item[1],
                -len(self._tokenize(str(item[0].get("text") or ""))),
            )
        )
        return ranked

    def _match_fact(
        self,
        message: str,
        approved_facts: list[dict[str, Any]],
    ) -> tuple[Optional[dict[str, Any]], int]:
        ranked = self._rank_facts(message, approved_facts)
        if not ranked:
            return None, 0
        best_fact, best_score = ranked[0]
        return best_fact, best_score

    @staticmethod
    def _facts_preview(approved_facts: list[dict[str, Any]], limit: int = 3) -> str:
        lines: list[str] = []
        for fact in approved_facts[: max(1, limit)]:
            text = str(fact.get("text") or "").strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines).strip()

    def _menu_document_facts(self, menu_documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build searchable, approved fact lines from menu OCR documents."""
        facts: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add(text: str, fact_id: str) -> None:
            line = re.sub(r"\s+", " ", str(text or "")).strip()
            if not line:
                return
            if len(line) > self._MENU_FACT_TEXT_LIMIT:
                line = line[: self._MENU_FACT_TEXT_LIMIT].rstrip() + "..."
            key = line.lower()
            if key in seen:
                return
            seen.add(key)
            facts.append(
                {
                    "id": fact_id,
                    "text": line,
                    "source": "menu_ocr",
                    "tags": ["menu", "ocr"],
                    "status": "approved",
                }
            )

        for doc_idx, doc in enumerate(menu_documents):
            if not isinstance(doc, dict):
                continue
            doc_id = self._normalize_identifier(doc.get("id")) or f"menu_doc_{doc_idx + 1}"
            menu_name = str(
                doc.get("menu_name")
                or (doc.get("summary") or {}).get("menu_name")
                or ""
            ).strip()
            raw = doc.get("ocr_raw_output")
            if not isinstance(raw, dict):
                raw = {}

            if menu_name:
                _add(f"Menu: {menu_name}.", f"{doc_id}_menu_name")

            items = raw.get("items", [])
            if isinstance(items, list):
                for item_idx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    item_name = str(item.get("name") or "").strip()
                    if not item_name:
                        continue
                    description = str(item.get("description") or "").strip()
                    price = str(item.get("price") or "").strip()
                    dish_type = str(item.get("dish_type") or "").strip()
                    kcal = str(item.get("kcal") or "").strip()
                    allergens = item.get("allergens", [])
                    allergen_text = ""
                    if isinstance(allergens, list):
                        cleaned = [str(a).strip() for a in allergens if str(a).strip()]
                        if cleaned:
                            allergen_text = ", ".join(cleaned)
                    diet = ""
                    veg = str(item.get("veg") or "").strip().lower()
                    non_veg = str(item.get("non_veg") or "").strip().lower()
                    if veg:
                        diet = "Vegetarian"
                    elif non_veg:
                        diet = "Non-vegetarian"

                    segments = [item_name]
                    if dish_type:
                        segments.append(f"type: {dish_type}")
                    if description:
                        segments.append(description)
                    if price:
                        segments.append(f"price INR {price}")
                    if kcal:
                        segments.append(f"{kcal} kcal")
                    if diet:
                        segments.append(diet)
                    if allergen_text:
                        segments.append(f"allergens: {allergen_text}")
                    _add(". ".join(segments).strip(". ") + ".", f"{doc_id}_item_{item_idx + 1}")

            for key_name in ("other_text", "footer_text", "notes"):
                values = raw.get(key_name, [])
                if not isinstance(values, list):
                    continue
                for val_idx, value in enumerate(values):
                    text = str(value or "").strip()
                    if not text:
                        continue
                    _add(text, f"{doc_id}_{key_name}_{val_idx + 1}")

        return facts

    def _build_fact_list_response(
        self,
        plugin_name: str,
        facts: list[dict[str, Any]],
        heading: str = "approved details",
        limit: int = 6,
    ) -> str:
        preview = self._facts_preview(facts, limit=max(1, limit))
        if not preview:
            return ""
        return f"{plugin_name} {heading}:\n{preview}".strip()

    def _weak_plugin_topic_match(
        self,
        message: str,
        plugins: list[dict[str, Any]],
    ) -> bool:
        """
        Determine whether message is likely about plugin-managed domains even when
        there is no strong trigger match. Used by strict mode to block fallback.
        """
        message_lower = str(message or "").strip().lower()
        if not message_lower:
            return False
        message_tokens = self._tokenize(message_lower)
        if not message_tokens:
            return False

        for plugin in plugins:
            plugin_name_tokens = self._tokenize(str(plugin.get("name") or ""))
            service_tokens = self._tokenize(str(plugin.get("service_id") or ""))
            trigger_tokens: set[str] = set()
            triggers = plugin.get("trigger_phrases", [])
            if isinstance(triggers, list):
                for phrase in triggers:
                    trigger_tokens |= self._tokenize(str(phrase))

            known_tokens = plugin_name_tokens | service_tokens | trigger_tokens
            if not known_tokens:
                continue
            overlap = message_tokens & known_tokens
            if len(overlap) >= 2:
                return True
            if len(overlap) == 1 and any(token in self._DOMAIN_TOKENS for token in overlap):
                return True

            fuzzy_overlap = self._fuzzy_overlap_count(message_tokens, known_tokens)
            if fuzzy_overlap >= 2:
                return True
            if fuzzy_overlap == 1 and (overlap or (message_tokens & self._DOMAIN_TOKENS)):
                return True

        return False

    def _match_plugin(
        self,
        message: str,
        plugins: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        message_lower = str(message or "").strip().lower()
        if not message_lower:
            return None

        best_plugin = None
        best_score = 0.0
        message_tokens = self._tokenize(message_lower)

        for plugin in plugins:
            score = 0.0

            name = str(plugin.get("name") or "").strip().lower()
            service_id = str(plugin.get("service_id") or "").strip().lower()
            plugin_id = str(plugin.get("id") or "").strip().lower()

            # Use bounded phrase checks for identifiers as well so short tokens
            # (e.g., "spa") do not match substrings such as "space".
            if name and self._contains_phrase(message_lower, name):
                score = max(score, 0.6)
            if service_id and self._contains_phrase(message_lower, service_id.replace("_", " ")):
                score = max(score, 0.62)
            if plugin_id and self._contains_phrase(message_lower, plugin_id.replace("_", " ")):
                score = max(score, 0.58)

            trigger_phrases = plugin.get("trigger_phrases", [])
            if isinstance(trigger_phrases, list):
                for phrase in trigger_phrases:
                    if self._contains_phrase(message_lower, str(phrase)):
                        score = max(score, 0.65)
                        break

            knowledge_scope = plugin.get("knowledge_scope", {})
            if isinstance(knowledge_scope, dict):
                keywords = knowledge_scope.get("keywords", [])
                if isinstance(keywords, list):
                    keyword_hits = 0
                    for keyword in keywords:
                        keyword_text = str(keyword or "").strip().lower()
                        if keyword_text and keyword_text in message_lower:
                            keyword_hits += 1
                    if keyword_hits > 0:
                        score = max(score, min(0.45 + (keyword_hits * 0.08), 0.7))

            # Soft token-level overlap to catch natural phrases that miss strict
            # trigger boundaries (for example: "I want food in my room").
            known_tokens = set()
            known_tokens |= self._tokenize(name)
            known_tokens |= self._tokenize(service_id.replace("_", " "))
            known_tokens |= self._tokenize(plugin_id.replace("_", " "))
            trigger_phrases = plugin.get("trigger_phrases", [])
            if isinstance(trigger_phrases, list):
                for phrase in trigger_phrases:
                    known_tokens |= self._tokenize(str(phrase))
            known_tokens |= self._plugin_fact_tokens(plugin)
            overlap = message_tokens & known_tokens
            if len(overlap) >= 2:
                score = max(score, 0.58)
            elif len(overlap) == 1 and any(token in self._DOMAIN_TOKENS for token in overlap):
                score = max(score, 0.54)
            specific_overlap = {tok for tok in overlap if tok not in self._GENERIC_QUERY_TOKENS}
            if specific_overlap:
                score = max(score, 0.63)

            identity_tokens = self._plugin_identity_tokens(plugin)
            identity_overlap = message_tokens & identity_tokens
            identity_fuzzy = self._fuzzy_overlap_count(message_tokens, identity_tokens)

            fuzzy_overlap = self._fuzzy_overlap_count(message_tokens, known_tokens)
            if fuzzy_overlap >= 2:
                if overlap or identity_overlap:
                    score = max(score, 0.6)
                elif identity_fuzzy >= 1:
                    score = max(score, 0.59)
                else:
                    score = max(score, 0.57)
            elif fuzzy_overlap == 1 and overlap:
                score = max(score, 0.56)

            # Identity tokens (name/service/trigger) are weighted higher than
            # generic fact vocabulary for typo-tolerant routing.
            if identity_overlap:
                score = max(score, 0.6)
            else:
                if identity_fuzzy >= 1 and (
                    self._looks_like_transaction_request(message_lower)
                    or self._is_information_query(message_lower)
                ):
                    score = max(score, 0.58)

            # Hospitality-specific room-food phrasing.
            if {"room"} & message_tokens and {"food", "eat", "meal", "hungry"} & message_tokens:
                if {"dining", "food", "menu", "order", "restaurant"} & known_tokens:
                    score = max(score, 0.6)

            if score > best_score:
                best_score = score
                best_plugin = plugin

        if best_score < 0.56:
            return None
        return best_plugin

    def _plugin_fact_tokens(self, plugin: dict[str, Any], token_limit: int = 160) -> set[str]:
        """
        Include approved-fact vocabulary in plugin matching so fact-centric
        asks (for example: therapist unavailability) can still route properly.
        """
        tokens: set[str] = set()
        approved_facts = self._approved_facts(plugin)
        for fact in approved_facts:
            if len(tokens) >= token_limit:
                break
            text = str(fact.get("text") or "").strip()
            source = str(fact.get("source") or "").strip()
            tags = fact.get("tags", [])
            tags_text = " ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
            fact_tokens = self._tokenize(f"{text} {source} {tags_text}")
            tokens |= {tok for tok in fact_tokens if tok not in self._GENERIC_PLUGIN_TOKENS}
        return tokens

    def _plugin_identity_tokens(self, plugin: dict[str, Any]) -> set[str]:
        tokens: set[str] = set()
        tokens |= self._tokenize(str(plugin.get("name") or ""))
        tokens |= self._tokenize(str(plugin.get("service_id") or "").replace("_", " "))
        tokens |= self._tokenize(str(plugin.get("id") or "").replace("_", " "))
        trigger_phrases = plugin.get("trigger_phrases", [])
        if isinstance(trigger_phrases, list):
            for phrase in trigger_phrases:
                tokens |= self._tokenize(str(phrase))
        return {tok for tok in tokens if tok not in self._GENERIC_PLUGIN_TOKENS}

    def _plugin_known_tokens(self, plugin: dict[str, Any]) -> set[str]:
        known_tokens: set[str] = set()
        known_tokens |= self._tokenize(str(plugin.get("name") or ""))
        known_tokens |= self._tokenize(str(plugin.get("service_id") or "").replace("_", " "))
        known_tokens |= self._tokenize(str(plugin.get("id") or "").replace("_", " "))
        trigger_phrases = plugin.get("trigger_phrases", [])
        if isinstance(trigger_phrases, list):
            for phrase in trigger_phrases:
                known_tokens |= self._tokenize(str(phrase))
        known_tokens |= self._plugin_fact_tokens(plugin)
        known_tokens |= self._DOMAIN_TOKENS
        return known_tokens

    def _should_interrupt_slot_collection(
        self,
        message: str,
        plugin: dict[str, Any],
        expected_slot_id: Optional[str],
    ) -> bool:
        """
        Detect topic switches while collecting plugin slots so unrelated queries
        are not saved as slot values.
        """
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if not msg_lower:
            return False
        if self._normalize_marker_text(msg) in self._CANCEL_MARKERS:
            return False

        slot_id = self._normalize_identifier(expected_slot_id or "")
        if slot_id:
            if "room" in slot_id and "number" in slot_id and bool(re.search(r"\d", msg)):
                return False
            if any(token in slot_id for token in ("time", "date", "day")):
                if self._looks_like_time_reply(msg):
                    if not self._is_information_query(msg):
                        return False
            if "name" in slot_id and len(re.findall(r"[a-z]+", msg_lower)) <= 3 and "?" not in msg_lower:
                return False

        message_tokens = self._tokenize(msg_lower)
        known_tokens = self._plugin_known_tokens(plugin)
        overlap = message_tokens & known_tokens
        fuzzy_overlap = self._fuzzy_overlap_count(message_tokens, known_tokens)
        if overlap or fuzzy_overlap >= 1:
            return False

        # When we are expecting a time/date/day slot, short off-topic text should
        # pause the flow instead of being stored as slot content.
        if any(token in slot_id for token in ("time", "date", "day")):
            if not self._looks_like_time_reply(msg) and len(message_tokens) >= 2:
                return True

        if self._is_information_query(msg):
            return True
        if len(message_tokens) >= 4:
            return True
        return False

    @staticmethod
    def _build_flow_interrupted_message(plugin_name: str) -> str:
        clean_name = str(plugin_name or "service flow").strip()
        if not clean_name:
            clean_name = "service flow"
        return (
            f"I've paused the {clean_name} flow. "
            "I can help with hotel services like dining, spa, pool, room service, and bookings."
        )

    def _memory_recap_query_type(self, message: str) -> Optional[str]:
        normalized = self._normalize_marker_text(message)
        if not normalized:
            return None
        recap_starters = ("what", "which", "show", "tell", "repeat", "recap", "remind")
        if not ("?" in str(message or "") or normalized.startswith(recap_starters) or "what all" in normalized):
            return None

        has_self_reference = bool(re.search(r"\b(i|my|me|ive|i ve)\b", normalized))
        has_memory_context = any(
            phrase in normalized
            for phrase in (
                "with you",
                "i shared",
                "i gave",
                "i told",
                "you have",
                "you know",
                "is stored",
                "is saved",
                "is recorded",
                "stored",
                "saved",
                "recorded",
                "captured",
                "booking time",
                "shared info",
                "shared time",
                "shared details",
                "show shared",
            )
        )
        if not (has_self_reference or has_memory_context):
            return None

        if "time" in normalized and any(
            term in normalized
            for term in (
                "give",
                "gave",
                "mention",
                "mentioned",
                "say",
                "said",
                "tell",
                "told",
                "shared",
                "share",
                "provide",
                "provided",
                "stored",
                "saved",
                "recorded",
                "booking",
                "slot",
            )
        ):
            return "time"
        if re.search(r"\bwhat\s+(?:did|have)\s+i\s+(?:tell|say|told)\b", normalized):
            return "details"
        if re.search(r"\bwhat\s+(?:did|have)\s+you\s+(?:store|save|record)\b", normalized):
            if "time" in normalized:
                return "time"
            return "details"
        if (
            ("what" in normalized or "show" in normalized or "tell" in normalized or "repeat" in normalized)
            and any(
                term in normalized
                for term in (
                    "detail",
                    "details",
                    "shared",
                    "provided",
                    "stored",
                    "saved",
                    "recorded",
                    "captured",
                    "time",
                    "info",
                    "information",
                )
            )
        ):
            if "time" in normalized:
                return "time"
            return "details"
        return None

    def _build_recap_response(
        self,
        plugin: dict[str, Any],
        recap_type: str,
        collected_slots: dict[str, Any],
        expected_slot: Optional[dict[str, Any]],
    ) -> str:
        summary = self._slot_summary(plugin, collected_slots)
        slot_schema = plugin.get("slot_schema", [])
        slot_labels: dict[str, str] = {}
        if isinstance(slot_schema, list):
            for slot in slot_schema:
                if not isinstance(slot, dict):
                    continue
                slot_id = self._normalize_identifier(slot.get("id"))
                if not slot_id:
                    continue
                slot_labels[slot_id] = str(slot.get("label") or slot.get("id") or slot_id).strip()

        recap_line = ""
        if recap_type == "time":
            time_pairs: list[str] = []
            for slot_id, value in collected_slots.items():
                slot_key = self._normalize_identifier(slot_id)
                if not slot_key or not str(value or "").strip():
                    continue
                if any(token in slot_key for token in ("time", "date", "day")):
                    label = slot_labels.get(slot_key, slot_key.replace("_", " ").title())
                    time_pairs.append(f"{label}: {value}")
            if time_pairs:
                recap_line = f"You shared {', '.join(time_pairs)}."
            elif summary:
                recap_line = f"So far I have: {summary}."
            else:
                recap_line = "I don't have your booking time yet."
        else:
            if summary:
                recap_line = f"So far I have: {summary}."
            else:
                plugin_name = str(plugin.get("name") or "this request").strip()
                recap_line = f"I don't have any {plugin_name} details yet."

        prompt = str((expected_slot or {}).get("prompt") or "").strip()
        if prompt:
            return f"{recap_line} {prompt}".strip()
        return recap_line

    def _should_interrupt_confirmation_flow(
        self,
        message: str,
        plugin: dict[str, Any],
    ) -> bool:
        message_text = str(message or "").strip()
        marker_text = self._normalize_marker_text(message_text)
        if not marker_text:
            return False
        if marker_text in self._CANCEL_MARKERS or marker_text in self._YES_MARKERS or marker_text in self._NO_MARKERS:
            return False
        if self._memory_recap_query_type(message_text):
            return False

        message_tokens = self._tokenize(message_text.lower())
        known_tokens = self._plugin_known_tokens(plugin)
        overlap = message_tokens & known_tokens
        if overlap:
            return False
        fuzzy_overlap = self._fuzzy_overlap_count(message_tokens, known_tokens)
        if fuzzy_overlap >= 1:
            return False

        # In confirmation state, treat unknown multi-token utterances as topic
        # switches (for example: "weather tomorrow", "pool timings").
        if self._is_information_query(message_text):
            return True
        if len(message_tokens) >= 2:
            return True
        return False

    @staticmethod
    def _slot_summary(plugin: dict[str, Any], collected: dict[str, Any]) -> str:
        slots = plugin.get("slot_schema", [])
        if not isinstance(slots, list):
            return ""

        parts: list[str] = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("id") or "").strip()
            if not slot_id:
                continue
            value = str(collected.get(slot_id) or "").strip()
            if not value:
                continue
            label = str(slot.get("label") or slot_id.replace("_", " ").title()).strip()
            parts.append(f"{label}: {value}")
        return "; ".join(parts)

    def _build_default_intro(self, plugin: dict[str, Any], first_slot: Optional[dict[str, Any]]) -> str:
        templates = plugin.get("response_templates", {})
        if isinstance(templates, dict):
            intro_template = str(templates.get("intro") or "").strip()
            if intro_template:
                if first_slot and first_slot.get("prompt"):
                    return f"{intro_template} {first_slot.get('prompt')}".strip()
                return intro_template

        plugin_name = str(plugin.get("name") or "this service").strip()
        if first_slot and first_slot.get("prompt"):
            return f"I can help with {plugin_name}. {first_slot.get('prompt')}"
        return f"I can help with {plugin_name}. Please share what you need."

    def _build_success_message(self, plugin: dict[str, Any], summary: str) -> str:
        templates = plugin.get("response_templates", {})
        if isinstance(templates, dict):
            success_template = str(templates.get("success") or "").strip()
            if success_template:
                if "{summary}" in success_template:
                    return success_template.replace("{summary}", summary or "details provided")
                return success_template

        plugin_name = str(plugin.get("name") or "this service").strip()
        if summary:
            return f"Done. I have captured your {plugin_name} request ({summary}). Our team will take it forward."
        return f"Done. I have captured your {plugin_name} request. Our team will take it forward."

    def _build_info_message(self, plugin: dict[str, Any]) -> str:
        templates = plugin.get("response_templates", {})
        if isinstance(templates, dict):
            fallback = str(templates.get("fallback") or "").strip()
            if fallback:
                return fallback
            intro = str(templates.get("intro") or "").strip()
            if intro:
                return intro
        plugin_name = str(plugin.get("name") or "this service").strip()
        return (
            f"I can help with {plugin_name}. "
            "Please ask a specific question and I will share the relevant details."
        )

    def _build_confirmation_prompt(self, plugin: dict[str, Any], summary: str) -> str:
        templates = plugin.get("response_templates", {})
        if isinstance(templates, dict):
            confirmation_template = str(templates.get("confirmation") or "").strip()
            if confirmation_template:
                if "{summary}" in confirmation_template:
                    return confirmation_template.replace("{summary}", summary or "details provided")
                return confirmation_template
        if summary:
            return f"Please confirm this request: {summary}. Reply 'yes confirm' to submit or 'no' to edit."
        return "Please confirm your request. Reply 'yes confirm' to submit or 'no' to edit."

    def _build_cancel_message(self, plugin: dict[str, Any]) -> str:
        templates = plugin.get("response_templates", {})
        if isinstance(templates, dict):
            cancelled_template = str(templates.get("cancelled") or "").strip()
            if cancelled_template:
                return cancelled_template
        plugin_name = str(plugin.get("name") or "service request").strip()
        return f"Cancelled. I have stopped the {plugin_name} flow. How else can I help?"

    @staticmethod
    def _strict_unavailable_message(settings: dict[str, Any], plugins: list[dict[str, Any]]) -> str:
        configured = str(settings.get("strict_unavailable_response") or "").strip()
        if configured:
            return configured
        names = [str(item.get("name") or "").strip() for item in plugins if str(item.get("name") or "").strip()]
        if names:
            listed = ", ".join(names[:6])
            return f"I can only answer configured service-agent data right now. Available service agents: {listed}."
        return "I can only answer configured service-agent data right now."

    def _try_answer_from_approved_facts(
        self,
        message: str,
        plugin: dict[str, Any],
        plugin_settings: dict[str, Any],
        all_plugins: list[dict[str, Any]],
    ) -> Optional[AgentPluginRuntimeResult]:
        service_category = self._normalize_identifier(plugin.get("service_category") or "transactional")
        has_slots = isinstance(plugin.get("slot_schema"), list) and len(plugin.get("slot_schema")) > 0
        strict_facts_only = bool(plugin.get("strict_facts_only", True))
        kb_record = config_service.get_service_kb_record(
            service_id=self._normalize_identifier(plugin.get("service_id")) or None,
            plugin_id=self._normalize_identifier(plugin.get("id")) or None,
            active_only=True,
        )
        if isinstance(kb_record, dict) and "strict_mode" in kb_record:
            strict_facts_only = bool(kb_record.get("strict_mode", strict_facts_only))
        information_query = self._is_information_query(message)
        transactional_request = self._looks_like_transaction_request(message)
        listing_query = self._looks_like_listing_query(message)
        message_lower = str(message or "").strip().lower()
        approved_facts = self._approved_facts(plugin)

        # For transactional requests, keep deterministic slot workflow unless the
        # user clearly asked an informational question.
        if has_slots and service_category == "transactional" and not information_query:
            return None

        plugin_name = str(plugin.get("name") or "this service").strip()

        if approved_facts:
            ranked = self._rank_facts(message, approved_facts)
            matched_fact, score = (ranked[0] if ranked else (None, 0))
            if matched_fact and score > 0:
                matched_source = str(matched_fact.get("source") or "").strip().lower()
                matched_tags = matched_fact.get("tags", [])
                matched_tags_set = {str(tag or "").strip().lower() for tag in matched_tags} if isinstance(matched_tags, list) else set()
                menu_fact = matched_source == "menu_ocr" or "menu" in matched_tags_set
                menu_like_query = any(term in message_lower for term in ("menu", "dessert", "dish", "dishes", "item", "items", "price"))

                if has_slots and service_category == "transactional" and information_query and not transactional_request:
                    if menu_like_query and not menu_fact:
                        return None
                    if not menu_fact and score < 2:
                        return None

                if information_query and (listing_query or len(ranked) > 1):
                    top_facts = [item[0] for item in ranked[:6]]
                    response_text = self._build_fact_list_response(
                        plugin_name=plugin_name,
                        facts=top_facts,
                        heading="details",
                        limit=6,
                    )
                    if response_text:
                        return AgentPluginRuntimeResult(
                            handled=True,
                            response_text=response_text,
                            next_state=ConversationState.IDLE,
                            pending_action=None,
                            pending_data={},
                            suggested_actions=["Ask another question", "Start a request"],
                            metadata={
                                "agent_plugin_handled": True,
                                "agent_plugin_id": plugin.get("id"),
                                "agent_plugin_name": plugin.get("name"),
                                "agent_plugin_status": "fact_list_answered",
                            },
                        )
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=str(matched_fact.get("text") or "").strip(),
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data={},
                    suggested_actions=["Ask another question", "View services"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_id": plugin.get("id"),
                        "agent_plugin_name": plugin.get("name"),
                        "agent_plugin_status": "fact_answered",
                        "agent_plugin_fact_id": matched_fact.get("id"),
                    },
                )

            # If user asked a general question and no specific match exists,
            # provide a concise preview of approved facts.
            if information_query and not transactional_request:
                if has_slots and service_category == "transactional":
                    # For transactional agents, avoid broad generic previews so
                    # global KB/RAG questions can continue on the core path.
                    has_menu_facts = any(
                        str(fact.get("source") or "").strip().lower() == "menu_ocr"
                        or (
                            isinstance(fact.get("tags"), list)
                            and any(str(tag or "").strip().lower() == "menu" for tag in fact.get("tags", []))
                        )
                        for fact in approved_facts
                    )
                    if not has_menu_facts:
                        return None
                preview = self._build_fact_list_response(
                    plugin_name=plugin_name,
                    facts=approved_facts,
                    heading="approved details",
                    limit=5,
                )
                if preview:
                    return AgentPluginRuntimeResult(
                        handled=True,
                        response_text=preview,
                        next_state=ConversationState.IDLE,
                        pending_action=None,
                        pending_data={},
                        suggested_actions=["Ask another question", "Start a request"],
                        metadata={
                            "agent_plugin_handled": True,
                            "agent_plugin_id": plugin.get("id"),
                            "agent_plugin_name": plugin.get("name"),
                            "agent_plugin_status": "fact_preview",
                        },
                    )

            if not information_query:
                preview = self._facts_preview(approved_facts)
                if preview:
                    return AgentPluginRuntimeResult(
                        handled=True,
                        response_text=f"{plugin_name} approved facts:\n{preview}",
                        next_state=ConversationState.IDLE,
                        pending_action=None,
                        pending_data={},
                        suggested_actions=["Ask another question", "View services"],
                        metadata={
                            "agent_plugin_handled": True,
                            "agent_plugin_id": plugin.get("id"),
                            "agent_plugin_name": plugin.get("name"),
                            "agent_plugin_status": "fact_preview",
                        },
                    )

            if strict_facts_only and information_query:
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=self._strict_unavailable_message(plugin_settings, all_plugins),
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data={},
                    suggested_actions=["Contact staff", "Ask another service question"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_id": plugin.get("id"),
                        "agent_plugin_name": plugin.get("name"),
                        "agent_plugin_strict_fact_blocked_fallback": True,
                    },
                )
            return None

        # No approved facts exist for this plugin.
        if strict_facts_only and (information_query or service_category in {"informational", "hybrid"}):
            return AgentPluginRuntimeResult(
                handled=True,
                response_text=(
                    f"{plugin_name} does not have any approved facts yet. "
                    "Please contact staff."
                ),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Contact staff", "Ask another service question"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_strict_fact_blocked_fallback": True,
                    "agent_plugin_missing_approved_facts": True,
                },
            )
        return None

    def _start_plugin_flow(
        self,
        plugin: dict[str, Any],
        context: ConversationContext,
    ) -> AgentPluginRuntimeResult:
        collected_slots: dict[str, Any] = {}
        first_missing = self._first_missing_slot(plugin, collected_slots)

        runtime_state = {
            "active_plugin_id": plugin.get("id"),
            "status": "collecting" if first_missing else "completed",
            "collected_slots": collected_slots,
            "started_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "handoff_history": [
                {
                    "from": "core",
                    "to": plugin.get("id"),
                    "timestamp": datetime.now(UTC).isoformat(),
                    "reason": "trigger_match",
                }
            ],
        }

        if first_missing is None:
            service_category = self._normalize_identifier(plugin.get("service_category") or "transactional")
            response_text = (
                self._build_info_message(plugin)
                if service_category == "informational"
                else self._build_success_message(plugin, "")
            )
            return AgentPluginRuntimeResult(
                handled=True,
                response_text=response_text,
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data=self._build_pending_data(context, None),
                suggested_actions=["Ask another question", "View services"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_status": "completed",
                    "agent_plugin_handoff": "core_to_plugin",
                },
            )

        slot_id = str(first_missing.get("id"))
        pending_action = f"{self.PENDING_PREFIX}{plugin.get('id')}:collect:{slot_id}"
        response_text = self._build_default_intro(plugin, first_missing)
        return AgentPluginRuntimeResult(
            handled=True,
            response_text=response_text,
            next_state=ConversationState.AWAITING_INFO,
            pending_action=pending_action,
            pending_data=self._build_pending_data(context, runtime_state),
            suggested_actions=["Cancel"],
            metadata={
                "agent_plugin_handled": True,
                "agent_plugin_id": plugin.get("id"),
                "agent_plugin_name": plugin.get("name"),
                "agent_plugin_status": "collecting",
                "agent_plugin_handoff": "core_to_plugin",
                "agent_plugin_expected_slot": slot_id,
            },
        )

    def _continue_plugin_flow(
        self,
        message: str,
        plugin: dict[str, Any],
        runtime_state: dict[str, Any],
        context: ConversationContext,
    ) -> AgentPluginRuntimeResult:
        message_text = str(message or "").strip()
        message_lower = message_text.lower()
        marker_text = self._normalize_marker_text(message_text)

        if marker_text in self._CANCEL_MARKERS:
            return AgentPluginRuntimeResult(
                handled=True,
                response_text=self._build_cancel_message(plugin),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data=self._build_pending_data(context, None),
                suggested_actions=["View services", "Ask another question"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_status": "cancelled",
                    "agent_plugin_handoff": "plugin_to_core",
                },
            )

        collected_slots = runtime_state.get("collected_slots", {})
        if not isinstance(collected_slots, dict):
            collected_slots = {}

        pending_action = str(context.pending_action or "").strip().lower()
        plugin_id = str(plugin.get("id") or "").strip().lower()
        confirm_action = f"{self.PENDING_PREFIX}{plugin_id}:confirm"
        slot_schema = plugin.get("slot_schema", [])

        # Confirmation stage
        if pending_action == confirm_action:
            recap_query_type = self._memory_recap_query_type(message_text)
            if recap_query_type:
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=(
                        f"{self._build_recap_response(plugin, recap_query_type, collected_slots, None)} "
                        "Please reply with 'yes confirm' to submit, or 'no' to edit details."
                    ).strip(),
                    next_state=ConversationState.AWAITING_CONFIRMATION,
                    pending_action=confirm_action,
                    pending_data=self._build_pending_data(
                        context,
                        {
                            **runtime_state,
                            "status": "confirming",
                            "updated_at": datetime.now(UTC).isoformat(),
                        },
                    ),
                    suggested_actions=["yes confirm", "no", "cancel"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_id": plugin.get("id"),
                        "agent_plugin_name": plugin.get("name"),
                        "agent_plugin_status": "confirming",
                        "agent_plugin_memory_recap": True,
                    },
                )
            if self._should_interrupt_confirmation_flow(message_text, plugin):
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=self._build_flow_interrupted_message(str(plugin.get("name") or "")),
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data=self._build_pending_data(context, None),
                    suggested_actions=["View services", "Ask hotel question", "Restart request"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_id": plugin.get("id"),
                        "agent_plugin_name": plugin.get("name"),
                        "agent_plugin_status": "interrupted",
                        "agent_plugin_handoff": "plugin_to_core",
                        "agent_plugin_flow_interrupted": True,
                        "agent_plugin_interrupted_stage": "confirm",
                    },
                )
            if marker_text in self._YES_MARKERS:
                summary = self._slot_summary(plugin, collected_slots)
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=self._build_success_message(plugin, summary),
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data=self._build_pending_data(context, None),
                    suggested_actions=["Ask another question", "View services"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_id": plugin.get("id"),
                        "agent_plugin_name": plugin.get("name"),
                        "agent_plugin_status": "completed",
                        "agent_plugin_handoff": "plugin_to_core",
                        "agent_plugin_slots": collected_slots,
                    },
                )
            if marker_text in self._NO_MARKERS:
                first_missing = self._first_missing_slot(plugin, {})
                if first_missing is None:
                    return AgentPluginRuntimeResult(
                        handled=True,
                        response_text="Okay, let me know the details again.",
                        next_state=ConversationState.AWAITING_INFO,
                        pending_action=f"{self.PENDING_PREFIX}{plugin_id}:collect:details",
                        pending_data=self._build_pending_data(
                            context,
                            {
                                **runtime_state,
                                "status": "collecting",
                                "collected_slots": {},
                                "updated_at": datetime.now(UTC).isoformat(),
                            },
                        ),
                        suggested_actions=["Cancel"],
                        metadata={
                            "agent_plugin_handled": True,
                            "agent_plugin_id": plugin.get("id"),
                            "agent_plugin_name": plugin.get("name"),
                            "agent_plugin_status": "collecting",
                            "agent_plugin_reset": True,
                        },
                    )

                slot_id = str(first_missing.get("id"))
                prompt = str(first_missing.get("prompt") or "Please share details.").strip()
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=f"No problem. {prompt}",
                    next_state=ConversationState.AWAITING_INFO,
                    pending_action=f"{self.PENDING_PREFIX}{plugin_id}:collect:{slot_id}",
                    pending_data=self._build_pending_data(
                        context,
                        {
                            **runtime_state,
                            "status": "collecting",
                            "collected_slots": {},
                            "updated_at": datetime.now(UTC).isoformat(),
                        },
                    ),
                    suggested_actions=["Cancel"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_id": plugin.get("id"),
                        "agent_plugin_name": plugin.get("name"),
                        "agent_plugin_status": "collecting",
                        "agent_plugin_expected_slot": slot_id,
                    },
                )

            return AgentPluginRuntimeResult(
                handled=True,
                response_text="Please reply with 'yes confirm' to submit, or 'no' to edit details.",
                next_state=ConversationState.AWAITING_CONFIRMATION,
                pending_action=confirm_action,
                pending_data=self._build_pending_data(
                    context,
                    {
                        **runtime_state,
                        "status": "confirming",
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                ),
                suggested_actions=["yes confirm", "no", "cancel"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_status": "confirming",
                },
            )

        # Slot collection stage
        expected_slot_id = self._extract_pending_slot_id(context.pending_action)
        expected_slot = None
        if isinstance(slot_schema, list) and expected_slot_id:
            expected_slot = next(
                (
                    slot
                    for slot in slot_schema
                    if isinstance(slot, dict) and self._normalize_identifier(slot.get("id")) == expected_slot_id
                ),
                None,
            )

        recap_query_type = self._memory_recap_query_type(message_text)
        if recap_query_type:
            return AgentPluginRuntimeResult(
                handled=True,
                response_text=self._build_recap_response(
                    plugin=plugin,
                    recap_type=recap_query_type,
                    collected_slots=collected_slots,
                    expected_slot=expected_slot,
                ),
                next_state=ConversationState.AWAITING_INFO,
                pending_action=context.pending_action,
                pending_data=self._build_pending_data(context, runtime_state),
                suggested_actions=["Cancel"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_status": "collecting",
                    "agent_plugin_memory_recap": True,
                },
            )

        # Lightweight validation for common slot types.
        # If user asks menu/details while we're expecting order item details,
        # answer from approved facts and keep the slot pending.
        if expected_slot_id and ("order" in expected_slot_id or "item" in expected_slot_id):
            if self._is_information_query(message_text):
                approved_facts = self._approved_facts(plugin)
                plugin_name = str(plugin.get("name") or "this service").strip()
                ranked = self._rank_facts(message_text, approved_facts)
                response_text = ""
                if ranked:
                    top_facts = [item[0] for item in ranked[:6]]
                    response_text = self._build_fact_list_response(
                        plugin_name=plugin_name,
                        facts=top_facts,
                        heading="details",
                        limit=6,
                    )
                elif approved_facts:
                    response_text = self._build_fact_list_response(
                        plugin_name=plugin_name,
                        facts=approved_facts,
                        heading="approved details",
                        limit=5,
                    )

                if response_text:
                    slot_prompt = str((expected_slot or {}).get("prompt") or "What would you like to order?").strip()
                    return AgentPluginRuntimeResult(
                        handled=True,
                        response_text=f"{response_text}\n\n{slot_prompt}".strip(),
                        next_state=ConversationState.AWAITING_INFO,
                        pending_action=context.pending_action,
                        pending_data=self._build_pending_data(context, runtime_state),
                        suggested_actions=["Cancel"],
                        metadata={
                            "agent_plugin_handled": True,
                            "agent_plugin_id": plugin.get("id"),
                            "agent_plugin_name": plugin.get("name"),
                            "agent_plugin_status": "collecting",
                            "agent_plugin_slot_helped_with_info": expected_slot_id,
                        },
                    )

        if expected_slot_id and self._should_interrupt_slot_collection(message_text, plugin, expected_slot_id):
            return AgentPluginRuntimeResult(
                handled=True,
                response_text=self._build_flow_interrupted_message(str(plugin.get("name") or "")),
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data=self._build_pending_data(context, None),
                suggested_actions=["View services", "Ask hotel question", "Restart request"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_status": "interrupted",
                    "agent_plugin_handoff": "plugin_to_core",
                    "agent_plugin_flow_interrupted": True,
                    "agent_plugin_interrupted_slot": expected_slot_id,
                },
            )

        # Lightweight validation for common slot types.
        if expected_slot_id and "room" in expected_slot_id and "number" in expected_slot_id:
            if not re.search(r"\d", message_text):
                retry_prompt = str((expected_slot or {}).get("prompt") or "Please share your room number.").strip()
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=f"Please share a valid room number (for example, 1204). {retry_prompt}".strip(),
                    next_state=ConversationState.AWAITING_INFO,
                    pending_action=context.pending_action,
                    pending_data=self._build_pending_data(context, runtime_state),
                    suggested_actions=["Cancel"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_id": plugin.get("id"),
                        "agent_plugin_name": plugin.get("name"),
                        "agent_plugin_status": "collecting",
                        "agent_plugin_slot_validation_failed": expected_slot_id,
                    },
                )

        if expected_slot_id:
            collected_slots[expected_slot_id] = message_text
        else:
            missing = self._first_missing_slot(plugin, collected_slots)
            if missing is not None:
                collected_slots[str(missing.get("id"))] = message_text

        next_missing = self._first_missing_slot(plugin, collected_slots)
        if next_missing is not None:
            slot_id = str(next_missing.get("id"))
            runtime_state.update(
                {
                    "status": "collecting",
                    "collected_slots": collected_slots,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            prompt = str(next_missing.get("prompt") or "Please share details.").strip()
            return AgentPluginRuntimeResult(
                handled=True,
                response_text=prompt,
                next_state=ConversationState.AWAITING_INFO,
                pending_action=f"{self.PENDING_PREFIX}{plugin_id}:collect:{slot_id}",
                pending_data=self._build_pending_data(context, runtime_state),
                suggested_actions=["Cancel"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_status": "collecting",
                    "agent_plugin_expected_slot": slot_id,
                    "agent_plugin_slots": collected_slots,
                },
            )

        summary = self._slot_summary(plugin, collected_slots)
        confirmation_required = bool(plugin.get("confirmation_required", True))
        if confirmation_required:
            runtime_state.update(
                {
                    "status": "confirming",
                    "collected_slots": collected_slots,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            return AgentPluginRuntimeResult(
                handled=True,
                response_text=self._build_confirmation_prompt(plugin, summary),
                next_state=ConversationState.AWAITING_CONFIRMATION,
                pending_action=confirm_action,
                pending_data=self._build_pending_data(context, runtime_state),
                suggested_actions=["yes confirm", "no", "cancel"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_id": plugin.get("id"),
                    "agent_plugin_name": plugin.get("name"),
                    "agent_plugin_status": "confirming",
                    "agent_plugin_slots": collected_slots,
                },
            )

        return AgentPluginRuntimeResult(
            handled=True,
            response_text=self._build_success_message(plugin, summary),
            next_state=ConversationState.COMPLETED,
            pending_action=None,
            pending_data=self._build_pending_data(context, None),
            suggested_actions=["Ask another question", "View services"],
            metadata={
                "agent_plugin_handled": True,
                "agent_plugin_id": plugin.get("id"),
                "agent_plugin_name": plugin.get("name"),
                "agent_plugin_status": "completed",
                "agent_plugin_handoff": "plugin_to_core",
                "agent_plugin_slots": collected_slots,
            },
        )

    def handle_message(
        self,
        message: str,
        context: ConversationContext,
        channel: Optional[str] = None,
    ) -> AgentPluginRuntimeResult:
        """
        Route a user message to configured service-agent plugins.
        Returns handled=False when no plugin should intercept the message.
        """
        if not str(message or "").strip():
            return AgentPluginRuntimeResult()

        channel_id = self._normalize_identifier(channel or context.channel or "web")
        all_active_plugins = config_service.get_agent_plugins(
            active_only=True,
            channel=channel_id,
        )
        plugin_map = {
            self._normalize_identifier(item.get("id")): item
            for item in all_active_plugins
            if isinstance(item, dict)
        }

        runtime_state = self._get_runtime_state(context)
        active_plugin_id = self._normalize_identifier(runtime_state.get("active_plugin_id"))
        plugin_settings = config_service.get_agent_plugin_settings()
        plugin_runtime_enabled = bool(plugin_settings.get("enabled", True))
        strict_mode = bool(plugin_settings.get("strict_mode", True))

        if not plugin_runtime_enabled:
            if active_plugin_id or self._is_plugin_pending(context.pending_action):
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text="Service plugins are currently disabled. Continuing with the main assistant.",
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data=self._build_pending_data(context, None),
                    suggested_actions=["View services", "Ask another question"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_disabled": True,
                        "agent_plugin_handoff": "plugin_to_core",
                    },
                )
            return AgentPluginRuntimeResult()

        # Continue active plugin flow when runtime state exists.
        if active_plugin_id:
            active_plugin = plugin_map.get(active_plugin_id)
            if active_plugin is None:
                # Plugin was removed/disabled while session was active.
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text="That service flow is no longer active. We can continue here with the main assistant.",
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data=self._build_pending_data(context, None),
                    suggested_actions=["View services", "Ask another question"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_stale": True,
                        "agent_plugin_handoff": "plugin_to_core",
                    },
                )
            return self._continue_plugin_flow(message, active_plugin, runtime_state, context)

        if self._is_plugin_pending(context.pending_action):
            # Pending action points to a plugin flow, but runtime state is missing.
            return AgentPluginRuntimeResult(
                handled=True,
                response_text="That service flow expired. Let's continue with the main assistant.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data=self._build_pending_data(context, None),
                suggested_actions=["View services", "Ask another question"],
                metadata={
                    "agent_plugin_handled": True,
                    "agent_plugin_stale": True,
                    "agent_plugin_handoff": "plugin_to_core",
                },
            )

        # If another non-plugin pending action exists, do not hijack.
        if context.pending_action and not self._is_plugin_pending(context.pending_action):
            return AgentPluginRuntimeResult()

        matched_plugin = self._match_plugin(message, all_active_plugins)
        if matched_plugin is None:
            recap_query_type = self._memory_recap_query_type(message)
            if strict_mode and self._weak_plugin_topic_match(message, all_active_plugins):
                if recap_query_type:
                    recap_hint = (
                        "I can recap shared booking details and time while a service flow is active. "
                        "Please restart the relevant service request if you want me to continue."
                        if recap_query_type == "time"
                        else "I can recap shared service details while a flow is active. "
                        "Please restart the relevant service request if you want me to continue."
                    )
                    return AgentPluginRuntimeResult(
                        handled=True,
                        response_text=recap_hint.strip(),
                        next_state=ConversationState.IDLE,
                        pending_action=None,
                        pending_data=self._build_pending_data(context, None),
                        suggested_actions=["Restart service request", "Ask hotel service question"],
                        metadata={
                            "agent_plugin_handled": True,
                            "agent_plugin_memory_recap": True,
                            "agent_plugin_memory_recap_without_active_flow": True,
                        },
                    )
                return AgentPluginRuntimeResult(
                    handled=True,
                    response_text=self._strict_unavailable_message(plugin_settings, all_active_plugins),
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data=self._build_pending_data(context, None),
                    suggested_actions=["Ask another service question", "Contact staff"],
                    metadata={
                        "agent_plugin_handled": True,
                        "agent_plugin_strict_mode_blocked_fallback": True,
                    },
                )
            return AgentPluginRuntimeResult()

        fact_result = self._try_answer_from_approved_facts(
            message=message,
            plugin=matched_plugin,
            plugin_settings=plugin_settings,
            all_plugins=all_active_plugins,
        )
        if fact_result is not None:
            return fact_result

        # For transactional services, let core FAQ/RAG handle pure
        # informational questions when plugin-fact matching was inconclusive.
        if (
            self._normalize_identifier(matched_plugin.get("service_category") or "transactional") == "transactional"
            and self._is_information_query(message)
            and not self._looks_like_transaction_request(message)
        ):
            return AgentPluginRuntimeResult()

        return self._start_plugin_flow(matched_plugin, context)


agent_plugin_service = AgentPluginService()
