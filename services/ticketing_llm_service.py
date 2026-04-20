"""
Ticketing LLM Service

Optional LLM-assisted helpers for ticketing:
- sub-category classification
- long-term guest preference extraction

All methods are safe-by-default and fall back to deterministic behavior when
feature flags are disabled or the LLM returns invalid output.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from config.settings import settings
from llm.client import llm_client
from services.prompt_registry_service import (
    PromptMissingError,
    prompt_registry,
)

logger = logging.getLogger(__name__)


_GENERIC_SUBCATEGORY_BLOCKLIST = {
    "complaint",
    "request",
    "conversation",
    "issue",
    "ticket",
    "support",
    "general",
    "other",
    "others",
}


class TicketingLLMService:
    """LLM-backed helpers for complaint ticketing enrichment."""

    @staticmethod
    def _normalize_label(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        normalized = re.sub(r"[^a-z0-9 ]+", " ", raw)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized.replace(" ", "_")

    @staticmethod
    def _normalize_priority(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"low", "medium", "high", "critical"}:
            return raw
        return "medium"

    @classmethod
    def _heuristic_expired_update_decision(cls, note: str) -> dict[str, Any]:
        text = str(note or "").strip().lower()
        if not text:
            return {
                "create_new_ticket": False,
                "priority": "medium",
                "reason": "empty_note",
                "source": "heuristic",
            }
        return {
            "create_new_ticket": False,
            "priority": "medium",
            "reason": "llm_unavailable_or_disabled",
            "source": "heuristic",
        }

    async def assess_expired_ticket_update_followup(
        self,
        *,
        note: str,
        conversation: str,
        ticket_id: str = "",
    ) -> dict[str, Any]:
        """
        Decide whether an expired ticket-update request should become a new
        human-followup ticket.

        Returns:
            {
              "create_new_ticket": bool,
              "priority": "low|medium|high|critical",
              "reason": str,
              "source": "llm|heuristic"
            }
        """
        fallback = self._heuristic_expired_update_decision(note)
        if not bool(getattr(settings, "ticketing_update_window_llm_assessment_enabled", True)):
            return fallback
        if not str(settings.openai_api_key or "").strip():
            return fallback

        note_text = str(note or "").strip()
        if not note_text:
            return fallback

        try:
            prompt = await prompt_registry.get(
                "ticketing.expired_update_assessment",
                {
                    "ticket_id": str(ticket_id or "").strip() or "(unknown)",
                    "note_text": note_text,
                    "conversation": str(conversation or "").strip()[:1200] or "(none)",
                },
            )
        except PromptMissingError:
            logger.exception("ticketing_expired_update_assessment_prompt_missing")
            return fallback
        messages = [
            {"role": "system", "content": "You are a strict JSON decision engine for support triage."},
            {"role": "user", "content": prompt},
        ]
        model = str(getattr(settings, "ticketing_subcategory_model", "") or "").strip() or None
        try:
            parsed = await llm_client.chat_with_json(messages, model=model, temperature=0.0)
        except Exception:
            logger.exception("ticketing_expired_update_assessment_llm_failed")
            return fallback

        if not isinstance(parsed, dict):
            return fallback

        decision = bool(parsed.get("create_new_ticket"))
        priority = self._normalize_priority(parsed.get("priority"))
        reason = str(parsed.get("reason") or "").strip() or "llm_assessment"
        return {
            "create_new_ticket": decision,
            "priority": priority,
            "reason": reason,
            "source": "llm",
        }

    async def classify_sub_category(
        self,
        *,
        issue: str,
        latest_user_message: str,
        conversation: str,
        fallback_sub_category: str,
        allowed_sub_categories: list[str] | None = None,
    ) -> str:
        """
        Return a concrete sub-category for ticketing.

        Falls back to `fallback_sub_category` on any failure.
        """
        fallback = self._normalize_label(fallback_sub_category)
        if not bool(getattr(settings, "ticketing_subcategory_llm_enabled", False)):
            return fallback
        if not str(settings.openai_api_key or "").strip():
            return fallback

        issue_text = str(issue or "").strip()
        latest_text = str(latest_user_message or "").strip()
        if not issue_text and not latest_text:
            return fallback

        allowed = [
            self._normalize_label(item)
            for item in (allowed_sub_categories or [])
            if str(item or "").strip()
        ]
        allowed = [item for item in allowed if item]

        prompt = (
            "Return one concrete hotel-service sub_category for a support ticket.\n"
            "Rules:\n"
            "1) Prefer a specific operational label (for example: housekeeping, maintenance, billing, amenities, transport).\n"
            "2) Do not return generic labels like complaint/request/issue/general/other.\n"
            "3) If an allowed label fits, use it exactly.\n"
            "4) Return strict JSON: {\"sub_category\":\"...\"}\n\n"
            f"Allowed labels: {allowed or ['(none)']}\n"
            f"Issue summary: {issue_text or '(none)'}\n"
            f"Latest user message: {latest_text or '(none)'}\n"
            f"Conversation excerpt: {str(conversation or '').strip()[:1200] or '(none)'}"
        )
        messages = [
            {"role": "system", "content": "You classify ticket sub-categories with strict JSON output."},
            {"role": "user", "content": prompt},
        ]
        model = str(getattr(settings, "ticketing_subcategory_model", "") or "").strip() or None
        try:
            parsed = await llm_client.chat_with_json(messages, model=model, temperature=0.0)
        except Exception:
            logger.exception("ticketing_subcategory_llm_failed")
            return fallback

        if not isinstance(parsed, dict):
            return fallback
        candidate = self._normalize_label(parsed.get("sub_category"))
        if not candidate:
            return fallback
        if candidate in _GENERIC_SUBCATEGORY_BLOCKLIST:
            return fallback
        if allowed and candidate not in set(allowed):
            # Keep a stable taxonomy when an allow-list is provided.
            return fallback
        return candidate

    async def extract_guest_preferences(
        self,
        *,
        latest_user_message: str,
        conversation: str,
    ) -> list[str]:
        """
        Extract stable guest preferences from complaint/support conversation.

        Returns normalized short strings. Empty list when nothing reliable.
        """
        text = str(latest_user_message or "").strip()
        if not text:
            return []

        heuristic = self._extract_preferences_heuristic(text)
        if not bool(getattr(settings, "ticketing_guest_preferences_enabled", True)):
            return []
        if not bool(getattr(settings, "ticketing_guest_preferences_use_llm", False)):
            return heuristic
        if not str(settings.openai_api_key or "").strip():
            return heuristic

        prompt = (
            "Extract only long-term guest preferences from the message and short context.\n"
            "Do not include one-time service requests.\n"
            "Examples to include: dietary restrictions, room environment preferences, allergy constraints.\n"
            "Return strict JSON: {\"preferences\":[\"...\"]}\n\n"
            f"Latest user message: {text}\n"
            f"Conversation excerpt: {str(conversation or '').strip()[:1200]}"
        )
        messages = [
            {"role": "system", "content": "You extract durable guest preferences in JSON."},
            {"role": "user", "content": prompt},
        ]
        model = str(getattr(settings, "ticketing_guest_preferences_model", "") or "").strip() or None
        try:
            parsed = await llm_client.chat_with_json(messages, model=model, temperature=0.0)
        except Exception:
            logger.exception("ticketing_guest_preferences_llm_failed")
            return heuristic

        if not isinstance(parsed, dict):
            return heuristic
        raw_preferences = parsed.get("preferences")
        extracted: list[str] = []
        if isinstance(raw_preferences, list):
            for item in raw_preferences:
                normalized = self._normalize_preference(item)
                if normalized and normalized not in extracted:
                    extracted.append(normalized)
                    if len(extracted) >= 6:
                        break

        if not extracted:
            return heuristic
        return extracted

    @classmethod
    def _normalize_preference(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[^a-z0-9 /-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 3:
            return ""
        return text[:80]

    @classmethod
    def _extract_preferences_heuristic(cls, message: str) -> list[str]:
        text = str(message or "").strip().lower()
        if not text:
            return []

        found: list[str] = []

        # Strong explicit preference statements.
        explicit_patterns = (
            r"\bi\s+prefer\s+([a-z0-9 /-]{3,60})",
            r"\bi(?:'d| would)\s+like\s+([a-z0-9 /-]{3,60})",
            r"\bplease\s+always\s+([a-z0-9 /-]{3,60})",
            r"\bi\s+am\s+allergic\s+to\s+([a-z0-9 /-]{2,40})",
            r"\ballergic\s+to\s+([a-z0-9 /-]{2,40})",
        )
        for pattern in explicit_patterns:
            for match in re.findall(pattern, text):
                normalized = cls._normalize_preference(match)
                if normalized and normalized not in found:
                    found.append(normalized)

        # Common durable markers.
        marker_map = {
            "vegetarian": "vegetarian diet",
            "vegan": "vegan diet",
            "jain": "jain diet",
            "halal": "halal diet",
            "gluten free": "gluten-free preference",
            "gluten-free": "gluten-free preference",
            "non smoking": "non-smoking room preference",
            "non-smoking": "non-smoking room preference",
            "quiet room": "quiet room preference",
            "high floor": "high-floor room preference",
            "low floor": "low-floor room preference",
            "feather free": "feather-free bedding preference",
            "no feather": "feather-free bedding preference",
            "no spicy": "non-spicy food preference",
        }
        compact = re.sub(r"\s+", " ", text)
        for marker, value in marker_map.items():
            if marker in compact and value not in found:
                found.append(value)

        return found[:6]


ticketing_llm_service = TicketingLLMService()
