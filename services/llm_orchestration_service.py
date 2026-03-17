from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from config.settings import settings
from llm.client import llm_client
from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision, TicketDecision
from services.config_service import config_service
from services.rag_service import rag_service

# ── LLM Input Logger ─────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(_log_dir, exist_ok=True)
_llm_logger = logging.getLogger("llm_inputs")
if not _llm_logger.handlers:
    _fh = logging.FileHandler(os.path.join(_log_dir, "llm_inputs.log"), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(message)s"))
    _llm_logger.addHandler(_fh)
    _llm_logger.setLevel(logging.DEBUG)
    _llm_logger.propagate = False


def _log_llm_call(label: str, session_id: str, user_message: str, system_prompt: str, payload: Any, response: Any = None) -> None:
    """Write full LLM input (and optional response) to logs/llm_inputs.log."""
    try:
        sep = "=" * 80
        lines = [
            f"\n{sep}",
            f"[{datetime.now(UTC).isoformat()}] {label}",
            f"SESSION : {session_id}",
            f"USER    : {user_message}",
            "--- SYSTEM PROMPT ---",
            str(system_prompt or ""),
            "--- PAYLOAD ---",
            json.dumps(payload, ensure_ascii=False, indent=2) if not isinstance(payload, str) else payload,
        ]
        if response is not None:
            lines += ["--- LLM RESPONSE ---", json.dumps(response, ensure_ascii=False, indent=2) if not isinstance(response, str) else str(response)]
        lines.append(sep)
        _llm_logger.debug("\n".join(lines))
    except Exception:
        pass
# ─────────────────────────────────────────────────────────────────────────────


class LLMOrchestrationService:
    """
    Two-stage LLM runtime:
    1) global orchestrator decides service/intent/action
    2) service-level LLM generates grounded response and slot updates

    Both stages must emit strict JSON contracts.
    """

    def _normalize_identifier(self, value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return default

    def _normalize_field_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            key = self._normalize_identifier(item)
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
            if len(normalized) >= 12:
                break
        return normalized

    @staticmethod
    def _phase_transition_timing_hint(current_phase_id: str, service_phase_id: str) -> str:
        current_norm = str(current_phase_id or "").strip().lower().replace(" ", "_")
        service_norm = str(service_phase_id or "").strip().lower().replace(" ", "_")
        if not current_norm or not service_norm or current_norm == service_norm:
            return ""
        if current_norm == "pre_booking":
            if service_norm == "pre_checkin":
                return "after your booking is confirmed"
            if service_norm in {"during_stay", "post_checkout"}:
                return "after check-in"
        if current_norm == "pre_checkin":
            if service_norm == "during_stay":
                return "once you check in"
            if service_norm == "post_checkout":
                return "after checkout"
        if current_norm == "during_stay" and service_norm == "post_checkout":
            return "after checkout"
        return ""

    def _sanitize_json(self, value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return str(value)[:400]
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:1200]
        if isinstance(value, list):
            return [self._sanitize_json(item, depth + 1) for item in value[:60]]
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:80]:
                key = str(raw_key)[:80]
                normalized[key] = self._sanitize_json(raw_value, depth + 1)
            return normalized
        return str(value)[:1200]

    @staticmethod
    def _coerce_public_pending(pending_data: Any) -> dict[str, Any]:
        if not isinstance(pending_data, dict):
            return {}
        return {
            key: value
            for key, value in pending_data.items()
            if isinstance(key, str) and not key.startswith("_")
        }

    @staticmethod
    async def _chat_with_json(
        *,
        messages: list[dict[str, Any]],
        model: str | None,
        temperature: float | None,
        trace_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            return await llm_client.chat_with_json(
                messages=messages,
                model=model,
                temperature=temperature,
                trace_context=trace_context,
            )
        except TypeError as exc:
            # Backward compatibility for tests/mocks that still expose the older
            # chat_with_json(messages, model=None, temperature=None) signature.
            if "trace_context" not in str(exc):
                raise
            return await llm_client.chat_with_json(
                messages=messages,
                model=model,
                temperature=temperature,
            )

    @staticmethod
    def _last_message_by_role(context: ConversationContext, role: str) -> str:
        if not isinstance(getattr(context, "messages", None), list):
            return ""
        role_norm = str(role or "").strip().lower()
        for msg in reversed(context.messages):
            msg_role = str(getattr(getattr(msg, "role", None), "value", "") or "").strip().lower()
            if msg_role != role_norm:
                continue
            return str(getattr(msg, "content", "") or "").strip()
        return ""

    def _build_history_bundle(
        self,
        *,
        context: ConversationContext,
        memory_snapshot: dict[str, Any],
        last_window_messages: int = 10,
        last_window_chars: int = 5000,
        full_window_messages: int = 120,
        full_window_chars: int = 12000,
    ) -> dict[str, Any]:
        # Last-10 raw messages are always included for short-term grounding.
        last_10 = self._history_preview(
            context,
            max_messages=max(1, int(last_window_messages or 10)),
            max_chars=max(1000, int(last_window_chars or 5000)),
        )
        # Wider tail context is included with bounded size.
        full_tail = self._history_preview(
            context,
            max_messages=max(10, int(full_window_messages or 120)),
            max_chars=max(2000, int(full_window_chars or 12000)),
        )
        return {
            "history_last_10": last_10,
            "full_history_context": {
                "message_count_total": len(context.messages) if isinstance(context.messages, list) else 0,
                "tail_messages": full_tail,
                "older_context_summary": str((memory_snapshot or {}).get("summary") or "").strip()[:2400],
            },
            "last_user_message": self._last_message_by_role(context, "user"),
            "last_assistant_message": self._last_message_by_role(context, "assistant"),
        }

    def _service_name_by_id(self, service_id: str, services_snapshot: list[dict[str, Any]]) -> str:
        normalized_id = self._normalize_identifier(service_id)
        if not normalized_id:
            return ""
        for row in services_snapshot:
            if not isinstance(row, dict):
                continue
            if self._normalize_identifier(row.get("id")) != normalized_id:
                continue
            return str(row.get("name") or normalized_id).strip()
        return normalized_id

    def _suspend_active_service_task(
        self,
        *,
        context: ConversationContext,
        services_snapshot: list[dict[str, Any]],
        next_service_id: str = "",
        force: bool = False,
    ) -> bool:
        """
        Park the current in-flight service task before a topic/service diversion.
        Returns True when a task was suspended.
        """
        pending_public = self._coerce_public_pending(context.pending_data)
        current_active_service = self._normalize_identifier(pending_public.get("service_id") or "")
        pending_action = str(context.pending_action or "").strip()
        normalized_next = self._normalize_identifier(next_service_id)

        if not current_active_service or not pending_action:
            return False
        if not force and not normalized_next:
            return False
        if not force and normalized_next == current_active_service:
            return False

        has_collected_values = any(
            str(value or "").strip()
            for key, value in pending_public.items()
            if key != "service_id"
        )
        if not has_collected_values:
            return False

        existing_rows = context.suspended_services if isinstance(context.suspended_services, list) else []
        for existing in existing_rows:
            if not isinstance(existing, dict):
                continue
            if (
                self._normalize_identifier(existing.get("service_id")) == current_active_service
                and str(existing.get("pending_action") or "").strip() == pending_action
                and isinstance(existing.get("pending_data"), dict)
                and existing.get("pending_data") == pending_public
            ):
                # Already parked; only clear active state.
                internal = {
                    key: value
                    for key, value in (context.pending_data or {}).items()
                    if isinstance(key, str) and key.startswith("_")
                }
                context.pending_data = internal
                context.pending_action = None
                return True

        context.suspended_services.append(
            {
                "service_id": current_active_service,
                "service_name": self._service_name_by_id(current_active_service, services_snapshot),
                "pending_data": dict(pending_public),
                "pending_action": pending_action,
            }
        )
        internal = {
            key: value
            for key, value in (context.pending_data or {}).items()
            if isinstance(key, str) and key.startswith("_")
        }
        context.pending_data = internal
        context.pending_action = None
        return True

    @staticmethod
    def _phase_label(phase_id: str, capabilities_summary: dict[str, Any]) -> str:
        normalized = str(phase_id or "").strip().lower().replace(" ", "_")
        if not normalized:
            return ""
        phase_rows = capabilities_summary.get("journey_phases")
        if not isinstance(phase_rows, list):
            phase_rows = capabilities_summary.get("phases")
        if isinstance(phase_rows, list):
            for row in phase_rows:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "").strip().lower().replace(" ", "_")
                if rid == normalized:
                    label = str(row.get("name") or "").strip()
                    if label:
                        return label
        return normalized.replace("_", " ").title()

    def _resolve_selected_phase(
        self,
        *,
        context: ConversationContext,
        selected_phase_context: dict[str, Any] | None,
        capabilities_summary: dict[str, Any],
    ) -> tuple[str, str]:
        selected = selected_phase_context if isinstance(selected_phase_context, dict) else {}
        selected_id = self._normalize_identifier(selected.get("selected_phase_id"))
        selected_name = str(selected.get("selected_phase_name") or "").strip()
        if selected_id:
            if not selected_name:
                selected_name = self._phase_label(selected_id, capabilities_summary)
            return selected_id, selected_name

        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending_raw.get("_integration", {})
        if isinstance(integration, dict):
            phase_candidate = self._normalize_identifier(integration.get("phase"))
            if phase_candidate:
                return phase_candidate, self._phase_label(phase_candidate, capabilities_summary)

        fallback = "pre_booking"
        return fallback, self._phase_label(fallback, capabilities_summary)

    def _service_kb_by_service(self, capabilities_summary: dict[str, Any]) -> dict[str, list[str]]:
        records = capabilities_summary.get("service_kb_records", [])
        if not isinstance(records, list):
            return {}
        mapping: dict[str, list[str]] = {}
        for row in records[:220]:
            if not isinstance(row, dict):
                continue
            service_id = self._normalize_identifier(row.get("service_id"))
            if not service_id:
                continue
            facts_value = row.get("facts")
            facts: list[str] = []
            if isinstance(facts_value, list):
                for fact in facts_value:
                    if not isinstance(fact, dict):
                        continue
                    if str(fact.get("status") or "approved").strip().lower() not in {"approved", ""}:
                        continue
                    text = str(fact.get("text") or "").strip()
                    if not text:
                        continue
                    facts.append(text[:320])
                    if len(facts) >= 30:
                        break
            mapping[service_id] = facts
        return mapping

    def _service_extracted_knowledge_by_service(self, capabilities_summary: dict[str, Any]) -> dict[str, str]:
        """Return mapping of service_id -> LLM-extracted knowledge string (from enrich_service_kb_records)."""
        records = capabilities_summary.get("service_kb_records", [])
        if not isinstance(records, list):
            return {}
        mapping: dict[str, str] = {}
        for row in records[:220]:
            if not isinstance(row, dict):
                continue
            service_id = self._normalize_identifier(row.get("service_id"))
            if not service_id:
                continue
            extracted = str(row.get("extracted_knowledge") or "").strip()
            if extracted:
                mapping[service_id] = extracted
        return mapping

    @staticmethod
    def _service_routing_keywords(*, service_row: dict[str, Any], prompt_pack: dict[str, Any]) -> list[str]:
        source_text = " ".join(
            [
                str(service_row.get("id") or ""),
                str(service_row.get("name") or ""),
                str(service_row.get("type") or ""),
                str(service_row.get("description") or ""),
                str(prompt_pack.get("profile") or ""),
                str(prompt_pack.get("role") or ""),
            ]
        ).strip().lower()
        if not source_text:
            return []
        stopwords = {
            "the",
            "and",
            "for",
            "with",
            "you",
            "your",
            "our",
            "hotel",
            "service",
            "assistant",
            "support",
            "help",
            "request",
            "booking",
        }
        keywords: list[str] = []
        seen: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", source_text):
            if len(token) < 3 or token in stopwords:
                continue
            if token in seen:
                continue
            seen.add(token)
            keywords.append(token)
            if len(keywords) >= 14:
                break
        return keywords

    def _service_snapshot(self, capabilities_summary: dict[str, Any]) -> list[dict[str, Any]]:
        services = capabilities_summary.get("service_catalog", [])
        if not isinstance(services, list):
            return []
        result: list[dict[str, Any]] = []
        for row in services[:80]:
            if not isinstance(row, dict):
                continue
            sid = self._normalize_identifier(row.get("id"))
            if not sid:
                continue
            prompt_pack = row.get("service_prompt_pack")
            if not isinstance(prompt_pack, dict):
                prompt_pack = config_service.get_service_prompt_pack(sid)
            if not isinstance(prompt_pack, dict):
                prompt_pack = {}
            execution_guard = prompt_pack.get("execution_guard", {})
            if not isinstance(execution_guard, dict):
                execution_guard = {}
            confirmation_format = prompt_pack.get("confirmation_format", {})
            if not isinstance(confirmation_format, dict):
                confirmation_format = {}
            prompt_ticketing_policy = prompt_pack.get("ticketing_policy", {})
            if not isinstance(prompt_ticketing_policy, dict):
                prompt_ticketing_policy = {}
            ticketing_conditions = str(prompt_pack.get("ticketing_conditions") or "").strip()
            if not ticketing_conditions:
                ticketing_conditions = str(prompt_ticketing_policy.get("policy") or "").strip()
            if not ticketing_conditions:
                ticketing_conditions = str(row.get("ticketing_policy") or "").strip()
            prompt_pack_source = str(prompt_pack.get("source") or "").strip().lower()
            pack_is_admin_managed = (
                bool(row.get("service_prompt_pack_custom", False))
                or prompt_pack_source in {"manual_override", "admin_ui", "admin_override", "db"}
            )
            required_slots = prompt_pack.get("required_slots", []) if pack_is_admin_managed else []
            normalized_slots: list[dict[str, Any]] = []
            if isinstance(required_slots, list):
                for item in required_slots[:15]:
                    if not isinstance(item, dict):
                        continue
                    normalized_slots.append(
                        {
                            "id": self._normalize_identifier(item.get("id")),
                            "label": str(item.get("label") or "").strip(),
                            "required": bool(item.get("required", True)),
                        }
                    )
            result.append(
                {
                    "id": sid,
                    "name": str(row.get("name") or sid).strip(),
                    "type": str(row.get("type") or "service").strip(),
                    "description": str(row.get("description") or "").strip(),
                    "is_active": bool(row.get("is_active", True)),
                    "phase_id": self._normalize_identifier(row.get("phase_id")),
                    "phase_name": self._phase_label(self._normalize_identifier(row.get("phase_id")), capabilities_summary),
                    "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                    "ticketing_policy": str(row.get("ticketing_policy") or "").strip(),
                    "ticketing_conditions": ticketing_conditions,
                    "required_slots": normalized_slots,
                    "profile": str(prompt_pack.get("profile") or "").strip().lower(),
                    "service_prompt_role": str(prompt_pack.get("role") or "").strip()[:300],
                    "service_prompt_behavior": str(prompt_pack.get("professional_behavior") or "").strip()[:500],
                    "execution_guard": self._sanitize_json(execution_guard),
                    "confirmation_format": self._sanitize_json(confirmation_format),
                    "service_prompt_pack": self._sanitize_json(prompt_pack),
                    "knowledge_facts": [],
                    # Runtime grounding for service agent should come from DB-backed
                    # service_prompt_pack only (admin-entered/extracted values).
                    # Keep full extracted knowledge; do not trim here.
                    "extracted_knowledge": str(prompt_pack.get("extracted_knowledge") or "").strip(),
                    "confirmation_pending_action": "confirm_booking",
                    "routing_keywords": self._service_routing_keywords(service_row=row, prompt_pack=prompt_pack),
                }
            )
        return result

    @staticmethod
    def _looks_like_catalog_information_request(message: str) -> bool:
        text = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not text:
            return False
        markers = (
            "what rooms",
            "room types",
            "list",
            "all rooms",
            "all room",
            "more rooms",
            "more options",
            "tell me all",
            "show all",
            "available rooms",
            "with bathtub",
            "suite options",
        )
        if any(marker in text for marker in markers):
            return True
        return text.startswith(("what", "which", "show", "list", "tell me", "give me", "more"))

    @staticmethod
    def _room_type_candidates_from_text(content: str) -> list[str]:
        text = str(content or "")
        if not text:
            return []
        candidates: list[str] = []
        seen: set[str] = set()

        section_match = re.search(r"section:\s*([^\n\r]+)", text, flags=re.IGNORECASE)
        if section_match:
            section_name = str(section_match.group(1) or "").strip()
            if section_name:
                lowered = section_name.lower()
                if "room" in lowered or "suite" in lowered:
                    key = lowered
                    if key not in seen:
                        seen.add(key)
                        candidates.append(section_name.title())

        pattern = re.compile(
            r"\b([A-Z][A-Za-z0-9&'/-]*(?:\s+[A-Z][A-Za-z0-9&'/-]*)*\s+(?:Room|Suite))\b"
        )
        for match in pattern.findall(text):
            label = str(match or "").strip()
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(label)
            if len(candidates) >= 20:
                break
        return candidates

    async def _build_service_grounding_pack(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        service: dict[str, Any],
        service_profile: str,
        is_first_service_turn: bool = False,
    ) -> dict[str, Any]:
        """Return full KB text only when service has no extracted_knowledge yet."""
        extracted = str(service.get("extracted_knowledge") or "").strip()
        if extracted:
            # Enrichment has already run — service_knowledge in payload is sufficient
            return {"full_kb_text": ""}
        # No extracted knowledge yet — fall back to full KB so LLM can still answer
        from services.config_service import config_service as _cs
        full_kb_text = _cs.get_full_kb_text(max_chars=40_000)
        return {"full_kb_text": full_kb_text}

    def _build_pending_action_context(self, context: Any) -> str:
        """Return a human-readable sentence describing what the guest was mid-flow on."""
        pending_action = str(getattr(context, "pending_action", "") or "")
        if not pending_action:
            return ""
        pending_data = getattr(context, "pending_data", {}) or {}
        service_id = str(pending_data.get("service_id", "") or "").strip()
        svc_name = ""
        if service_id:
            try:
                from services.config_service import config_service as _cs
                svc = _cs.get_service(service_id)
                if svc:
                    svc_name = str(svc.get("name", "") or "").strip()
            except Exception:
                pass
        collected = [
            k for k in pending_data
            if not str(k).startswith("_") and k not in {"service_id", "phase"}
            and pending_data[k] not in (None, "", [])
        ]
        parts: list[str] = []
        if svc_name:
            parts.append(f"service: {svc_name}")
        if collected:
            parts.append(f"already collected: {', '.join(collected)}")
        suffix = f" ({', '.join(parts)})" if parts else ""
        return f"Guest was mid-flow: {pending_action}{suffix}"

    def _normalize_suggested_actions(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            label = str(item or "").strip()
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(label[:72])
            if len(normalized) >= 6:
                break
        return normalized

    async def _run_next_action_suggestion_agent(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
    ) -> list[str]:
        if not bool(getattr(settings, "chat_llm_next_suggestions_enabled", True)):
            return []
        if not str(settings.openai_api_key or "").strip():
            return []
        if decision.suggested_actions:
            return []

        service_payload: dict[str, Any] = {}
        if isinstance(target_service, dict):
            service_payload = {
                "id": self._normalize_identifier(target_service.get("id")),
                "name": str(target_service.get("name") or "").strip(),
                "profile": str(target_service.get("profile") or "").strip(),
            }

        # Build bot delivery boundary from live config — fully dynamic per property
        hotel_code = str(getattr(context, "hotel_code", None) or "DEFAULT")
        try:
            cap_summary = config_service.get_capability_summary(hotel_code)
            raw_caps = cap_summary.get("capabilities", {})
            enabled_capabilities = [
                cap_id
                for cap_id, cap_data in raw_caps.items()
                if isinstance(cap_data, dict) and cap_data.get("enabled", False)
            ]
            nlu_policy = cap_summary.get("nlu_policy", {})
            property_constraints = [
                str(d).strip()
                for d in (nlu_policy.get("donts", []) if isinstance(nlu_policy, dict) else [])
                if str(d).strip()
            ]
        except Exception:
            enabled_capabilities = []
            property_constraints = []

        bot_delivery_boundary = {
            "medium": "text only — cannot show images, photos, videos, or visual media; cannot provide real-time availability, live pricing, or data from external systems not listed as active services",
            "enabled_capabilities": enabled_capabilities,
            "property_constraints": property_constraints,
        }

        # Build list of service names that are NOT available in the current phase.
        # The suggestion agent must never recommend actions for these services.
        try:
            all_services = cap_summary.get("service_catalog", [])
            blocked_service_names = [
                str(s.get("name") or s.get("id") or "").strip()
                for s in all_services
                if isinstance(s, dict)
                and bool(s.get("is_active", True))
                and self._normalize_identifier(s.get("phase_id")) != selected_phase_id
                and str(s.get("name") or s.get("id") or "").strip()
            ]
        except Exception:
            blocked_service_names = []

        payload = {
            "user_message": str(user_message or "").strip(),
            "assistant_response": str(decision.response_text or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "current_state": context.state.value,
            "pending_action": str(context.pending_action or ""),
            "pending_data_public": self._sanitize_json(self._coerce_public_pending(context.pending_data)),
            "decision": {
                "action": str(decision.action or ""),
                "intent": str(decision.intent or ""),
                "target_service_id": str(decision.target_service_id or ""),
                "missing_fields": list(decision.missing_fields or []),
                "followup_question": str(decision.followup_question or ""),
            },
            "service": service_payload,
            "history": self._history_preview(context, max_messages=8, max_chars=3000),
            "bot_delivery_boundary": bot_delivery_boundary,
            "blocked_service_names": blocked_service_names,
            "response_contract": {"suggested_actions": ["what the guest would type next"]},
        }
        system_prompt = (
            "You are a next-turn suggestion planner for a hotel concierge chat.\n"
            "Return STRICT JSON only.\n"
            "Generate 2 to 4 suggestions representing what the guest would most likely send next.\n\n"
            "Work through the payload in this order:\n"
            "0. Read `bot_delivery_boundary` first — this defines the hard limits of what this bot can actually deliver. If a suggestion requires the bot to show images or media, provide real-time data, or use a capability not listed in `enabled_capabilities` — discard it. `property_constraints` lists explicit rules for this property that must also be respected.\n"
            "1. Read `assistant_response` — this is what the bot just said. Suggestions must be a natural direct response to that specific message.\n"
            "2. Read `history` — understand the full conversation thread. Do not suggest anything already discussed or resolved.\n"
            "3. Read `service` and `decision.missing_fields` — if a service is active, suggest messages that continue that flow. If fields are missing, suggest messages that would naturally lead toward providing that information, not the values themselves.\n"
            "4. Read `selected_phase` — only suggest things relevant to this journey phase.\n"
            "5. Read `blocked_service_names` — NEVER suggest booking, ordering, requesting, or asking to use any service listed there. "
            "These services are not available in the current phase and any such suggestion will result in 'not available right now', creating a dead-end for the guest. "
            "This includes indirect suggestions like 'Can I book a table?' when table booking is blocked, or 'Can I order food?' when food ordering is blocked.\n\n"
            "Voice — strictly enforced:\n"
            "Every suggestion must be a natural first-person guest message, exactly what they would type into the chat.\n"
            "Bad: 'Ask about room types', 'View services', 'Show options', 'Share details'\n"
            "Good: 'What room types do you have?', 'What services are available?', 'Can I see my options?'\n\n"
            "Never suggest any value that is unique to the individual guest — this includes names, room numbers, phone numbers, email addresses, flight numbers, dates, times, booking references, order quantities, party sizes, prices, or any other personal or context-specific data. The guest is the only one who knows these — they must type them.\n"
            "Also never suggest a message where the guest is offering or providing their personal information, even without stating the actual value. Messages like 'Here's my full name and details', 'I'll share my details', 'Here is my information', 'Let me provide my info' are all forbidden — they imply the guest is about to hand over unique personal data.\n"
            "Suggestions must only be questions the guest wants to ask, or service actions they want to request — never data submissions.\n\n"
            "Keep each suggestion 2-8 words."
        )
        model = (
            str(getattr(settings, "chat_llm_next_suggestions_model", "") or "").strip()
            or str(getattr(settings, "llm_service_agent_model", "") or "").strip()
            or str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or None
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=0.0,
                trace_context={
                    "responder_type": "main",
                    "agent": "next_action_suggestion_agent",
                    "session_id": str(getattr(context, "session_id", "")),
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                    "target_service_id": str(decision.target_service_id or ""),
                },
            )
        except Exception:
            return []

        if not isinstance(raw, dict):
            return []
        return self._normalize_suggested_actions(raw.get("suggested_actions"))

    async def _ensure_suggested_actions(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
    ) -> OrchestrationDecision:
        if decision.suggested_actions:
            return decision
        suggestions = await self._run_next_action_suggestion_agent(
            user_message=user_message,
            context=context,
            decision=decision,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service,
        )
        if suggestions:
            decision.suggested_actions = suggestions
            decision.metadata.setdefault("suggested_actions_source", "next_action_agent")
        return decision

    @staticmethod
    def _history_preview(context: ConversationContext, max_messages: int, max_chars: int) -> list[dict[str, str]]:
        messages = context.get_recent_messages(max_messages if max_messages > 0 else 12)
        result: list[dict[str, str]] = []
        total = 0
        for msg in messages:
            content = str(msg.content or "").strip()
            if not content:
                continue
            room_left = max_chars - total
            if room_left <= 0:
                break
            clipped = content[: min(room_left, 700)]
            result.append({"role": msg.role.value, "content": clipped})
            total += len(clipped)
        return result

    @staticmethod
    def _safe_parse_decision(raw: Any) -> OrchestrationDecision:
        if not isinstance(raw, dict):
            raw = {}
        try:
            return OrchestrationDecision.model_validate(raw)
        except Exception:
            return OrchestrationDecision(
                intent="faq",
                action="respond_only",
                response_text="I need one clarification to proceed accurately. Could you share a bit more detail?",
                confidence=0.45,
            )

    def _apply_answer_priority_fields(self, decision: OrchestrationDecision) -> OrchestrationDecision:
        """
        Normalize missing field semantics:
        - missing_fields == blocking_fields
        - deferrable_fields are kept for later and must not force immediate collect_info
        """
        blocking_fields = self._normalize_field_list(
            decision.blocking_fields if decision.blocking_fields else decision.missing_fields
        )
        deferrable_fields = self._normalize_field_list(decision.deferrable_fields)
        if blocking_fields:
            decision.blocking_fields = blocking_fields
            decision.missing_fields = list(blocking_fields)
        else:
            decision.blocking_fields = []
            decision.missing_fields = []
        decision.deferrable_fields = deferrable_fields
        return decision

    async def _run_answer_first_guard(
        self,
        *,
        user_message: str,
        decision: OrchestrationDecision,
        context: ConversationContext,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not bool(getattr(settings, "chat_llm_answer_first_guard_enabled", True)):
            return {}
        if not str(settings.openai_api_key or "").strip():
            return {}

        service_payload: dict[str, Any] = {}
        if isinstance(target_service, dict):
            service_payload = {
                "id": self._normalize_identifier(target_service.get("id")),
                "name": str(target_service.get("name") or "").strip(),
                "phase_id": self._normalize_identifier(target_service.get("phase_id")),
                "phase_name": str(target_service.get("phase_name") or "").strip(),
                "profile": str(target_service.get("profile") or "").strip(),
                "required_slots": self._sanitize_json(target_service.get("required_slots") or []),
                "knowledge_facts": self._sanitize_json((target_service.get("knowledge_facts") or [])[:12]),
            }

        payload = {
            "user_message": str(user_message or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "current_state": context.state.value,
            "pending_action": str(context.pending_action or ""),
            "pending_data_public": self._sanitize_json(self._coerce_public_pending(context.pending_data)),
            "decision": {
                "response_text": str(decision.response_text or "").strip(),
                "action": str(decision.action or ""),
                "target_service_id": str(decision.target_service_id or ""),
                "answered_current_query": bool(decision.answered_current_query),
                "blocking_fields": list(decision.blocking_fields or []),
                "deferrable_fields": list(decision.deferrable_fields or []),
                "missing_fields": list(decision.missing_fields or []),
                "pending_action": str(decision.pending_action or ""),
                "followup_question": str(decision.followup_question or ""),
            },
            "service": service_payload,
            "response_contract": {
                "answers_current_query": "bool",
                "can_answer_from_context": "bool",
                "revised_response_text": "string",
                "recommended_action": "respond_only|collect_info|dispatch_handler|create_ticket|resume_pending|cancel_pending",
                "recommended_pending_action": "string|null",
                "blocking_fields": ["field_id"],
                "deferrable_fields": ["field_id"],
                "followup_question": "single short question or empty",
                "reason": "short reason",
            },
        }
        system_prompt = (
            "You are an answer-first quality guard for a concierge service LLM output.\n"
            "Return STRICT JSON only.\n"
            "Evaluate whether decision.response_text directly answers the user's latest ask.\n"
            "Policy:\n"
            "1) If current ask can be answered from provided context/service facts, answers_current_query must be true.\n"
            "2) Missing fields needed only for a later transaction step must be deferrable_fields, not blocking_fields.\n"
            "3) blocking_fields are only fields required to answer the current ask now.\n"
            "4) If the current ask is answered, prefer recommended_action=respond_only unless a truly blocking field exists.\n"
            "5) If answer is weak but context supports a better answer, provide revised_response_text.\n"
            "6) Never invent unsupported facts. If unknown, keep the response transparent.\n"
            "7) If recommended_action=collect_info, set recommended_pending_action to the best next slot prompt id.\n"
            "8) If decision.target_service_id is empty, avoid recommended_action=collect_info.\n"
            "9) For out-of-phase or unavailable-service situations, keep recommended_action=respond_only and provide a clearer response instead.\n"
        )
        model = (
            str(getattr(settings, "chat_llm_answer_first_guard_model", "") or "").strip()
            or str(getattr(settings, "llm_service_agent_model", "") or "").strip()
            or str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or None
        )
        temperature = float(getattr(settings, "chat_llm_answer_first_guard_temperature", 0.0) or 0.0)
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=temperature,
                trace_context={
                    "responder_type": "main",
                    "agent": "answer_first_guard",
                    "session_id": str(getattr(context, "session_id", "")),
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                    "target_service_id": str(decision.target_service_id or ""),
                },
            )
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    async def _enforce_answer_first_policy(
        self,
        *,
        user_message: str,
        decision: OrchestrationDecision,
        context: ConversationContext,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
    ) -> OrchestrationDecision:
        decision = self._apply_answer_priority_fields(decision)
        guard_raw = await self._run_answer_first_guard(
            user_message=user_message,
            decision=decision,
            context=context,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service,
        )
        if not guard_raw:
            return decision

        answers_current_query = self._coerce_bool(
            guard_raw.get("answers_current_query"),
            default=bool(decision.answered_current_query),
        )
        blocking_fields = self._normalize_field_list(guard_raw.get("blocking_fields"))
        deferrable_fields = self._normalize_field_list(guard_raw.get("deferrable_fields"))
        revised_response_text = str(guard_raw.get("revised_response_text") or "").strip()
        recommended_action = self._normalize_identifier(guard_raw.get("recommended_action"))
        recommended_pending_action_raw = guard_raw.get("recommended_pending_action")
        recommended_pending_action = (
            self._normalize_identifier(recommended_pending_action_raw)
            if recommended_pending_action_raw is not None
            else ""
        )
        followup_question = str(guard_raw.get("followup_question") or "").strip()

        decision.answered_current_query = answers_current_query
        if revised_response_text:
            decision.response_text = revised_response_text
        decision.blocking_fields = list(blocking_fields)
        decision.missing_fields = list(blocking_fields)
        if deferrable_fields:
            decision.deferrable_fields = list(deferrable_fields)
        elif "deferrable_fields" in guard_raw:
            decision.deferrable_fields = []
        if followup_question:
            decision.followup_question = followup_question

        if recommended_action in {
            "respond_only",
            "collect_info",
            "dispatch_handler",
            "create_ticket",
            "resume_pending",
            "cancel_pending",
        }:
            decision.action = recommended_action

        if recommended_pending_action_raw is not None:
            decision.pending_action = recommended_pending_action or None

        decision = self._apply_answer_priority_fields(decision)

        decision.metadata.setdefault("answer_first_guard_applied", True)
        decision.metadata["answer_first_guard_answers_current_query"] = answers_current_query
        decision.metadata["answer_first_guard_blocking_fields"] = list(decision.blocking_fields or [])
        decision.metadata["answer_first_guard_deferrable_fields"] = list(decision.deferrable_fields or [])
        decision.metadata["answer_first_guard_recommended_action"] = recommended_action
        decision.metadata["answer_first_guard_recommended_pending_action"] = recommended_pending_action or ""
        decision.metadata["answer_first_guard_reason"] = str(guard_raw.get("reason") or "").strip()
        return decision

    async def _resolve_target_service_with_llm(
        self,
        *,
        user_message: str,
        selected_phase_id: str,
        selected_phase_name: str,
        services_snapshot: list[dict[str, Any]],
    ) -> tuple[str, str]:
        if not str(settings.openai_api_key or "").strip():
            return "", ""
        in_phase_services = [
            {
                "id": self._normalize_identifier(item.get("id")),
                "name": str(item.get("name") or "").strip(),
                "phase_id": self._normalize_identifier(item.get("phase_id")),
                "phase_name": str(item.get("phase_name") or "").strip(),
                "profile": str(item.get("profile") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "routing_keywords": item.get("routing_keywords", []),
            }
            for item in services_snapshot
            if isinstance(item, dict)
            and bool(item.get("is_active", True))
            and self._normalize_identifier(item.get("phase_id")) == selected_phase_id
        ]
        if not in_phase_services:
            return "", ""

        prompt_payload = {
            "user_message": str(user_message or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "in_phase_services": in_phase_services[:60],
            "response_contract": {
                "is_service_request": "bool",
                "service_id": "exact id from in_phase_services when service request else empty string",
                "action_hint": "respond_only|collect_info|dispatch_handler|create_ticket",
                "reason": "short reason",
            },
        }
        system_prompt = (
            "You are a service router for a concierge assistant.\n"
            "Return STRICT JSON only.\n"
            "Pick exactly one in-phase service_id when the user asks for a service action.\n"
            "Route using service name and description first; treat profile as optional supporting context only.\n"
            "Do not overfit to profile labels when the service name/description indicate a different intent.\n"
            "If the message is pure chit-chat/information not tied to a service, leave service_id empty.\n"
        )
        model = str(getattr(settings, "llm_orchestration_model", "") or "").strip() or None
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=0.0,
                trace_context={
                    "responder_type": "main",
                    "agent": "service_router",
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                },
            )
        except Exception:
            return "", ""
        if not isinstance(raw, dict):
            return "", ""
        service_request_raw = raw.get("is_service_request")
        service_request = False
        if isinstance(service_request_raw, bool):
            service_request = service_request_raw
        elif str(service_request_raw or "").strip().lower() in {"true", "1", "yes", "y"}:
            service_request = True
        if not service_request:
            return "", str(raw.get("action_hint") or "").strip().lower()
        candidate = self._normalize_identifier(raw.get("service_id"))
        if not candidate:
            return "", str(raw.get("action_hint") or "").strip().lower()
        valid_ids = {self._normalize_identifier(item.get("id")) for item in in_phase_services}
        if candidate not in valid_ids:
            return "", str(raw.get("action_hint") or "").strip().lower()
        return candidate, str(raw.get("action_hint") or "").strip().lower()

    def _merge_decisions(
        self,
        base: OrchestrationDecision,
        overlay: OrchestrationDecision,
    ) -> OrchestrationDecision:
        merged = base.model_copy(deep=True)

        if overlay.normalized_query:
            merged.normalized_query = overlay.normalized_query
        if overlay.intent:
            merged.intent = overlay.intent
        if overlay.confidence > 0:
            merged.confidence = max(merged.confidence, overlay.confidence)
        if overlay.action and overlay.action != "respond_only":
            merged.action = overlay.action
        if overlay.target_service_id:
            merged.target_service_id = overlay.target_service_id
        if overlay.response_text:
            merged.response_text = overlay.response_text
        if overlay.pending_action is not None:
            merged.pending_action = overlay.pending_action
        if overlay.pending_data_updates:
            merged.pending_data_updates.update(overlay.pending_data_updates)
        if overlay.missing_fields:
            merged.missing_fields = list(overlay.missing_fields)
        merged.answered_current_query = bool(merged.answered_current_query and overlay.answered_current_query)
        if overlay.blocking_fields:
            merged.blocking_fields = list(overlay.blocking_fields)
            merged.missing_fields = list(overlay.blocking_fields)
        if overlay.deferrable_fields:
            merged.deferrable_fields = list(overlay.deferrable_fields)
        if overlay.followup_question:
            merged.followup_question = overlay.followup_question
        if overlay.suggested_actions:
            merged.suggested_actions = list(overlay.suggested_actions)
        if overlay.use_handler:
            merged.use_handler = True
        if overlay.handler_intent:
            merged.handler_intent = overlay.handler_intent
        merged.interrupt_pending = bool(merged.interrupt_pending or overlay.interrupt_pending)
        merged.resume_pending = bool(merged.resume_pending or overlay.resume_pending)
        merged.cancel_pending = bool(merged.cancel_pending or overlay.cancel_pending)
        merged.requires_human_handoff = bool(merged.requires_human_handoff or overlay.requires_human_handoff)

        if overlay.ticket.required:
            merged.ticket.required = True
        if overlay.ticket.ready_to_create:
            merged.ticket.ready_to_create = True
        if overlay.ticket.reason:
            merged.ticket.reason = overlay.ticket.reason
        if overlay.ticket.issue:
            merged.ticket.issue = overlay.ticket.issue
        if overlay.ticket.category:
            merged.ticket.category = overlay.ticket.category
        if overlay.ticket.sub_category:
            merged.ticket.sub_category = overlay.ticket.sub_category
        if overlay.ticket.priority:
            merged.ticket.priority = overlay.ticket.priority

        if overlay.metadata:
            merged.metadata.update(overlay.metadata)
        return merged

    def _build_service_system_prompt(
        self,
        *,
        service_id: str,
        service_name: str,
        role: str,
        behavior: str,
        description: str,
        service_knowledge: str,
        knowledge_facts: list[str],
        required_slots: list[dict[str, Any]],
        slots_are_custom: bool,
        validation_rules: list[dict[str, Any]],
        confirmation_format: dict[str, Any],
        ticketing_enabled: bool,
        ticketing_policy: dict[str, Any],
        ticketing_conditions: str,
        service_hours: dict[str, Any],
        delivery_zones: list[str],
        cuisine: str,
        confirmation_phrase: str,
        confirmation_pending_action: str,
        requires_confirmation_step: bool,
        full_kb_text: str,
    ) -> str:
        lines: list[str] = []

        # ══ SECTION 1: BEHAVIOR (from the Description field in the UI) ════════
        # The description tells the agent exactly how to behave for this service.
        lines.append(
            f"You are the {service_name} specialist for this hotel.\n"
            f"{description}" if description else f"You are the {service_name} specialist for this hotel."
        )

        # ══ PHASE BOUNDARY RULE ════════════════════════════════════════════════
        # Injected into every service agent so it never promises unavailable services.
        lines.append(
            "\n=== PHASE BOUNDARY RULE ===\n"
            "The guest is in a specific journey phase (pre_booking / pre_checkin / during_stay / post_checkout). "
            "Each service is only available in its designated phase.\n"
            "If the guest asks about or mentions a service that is NOT available in the current phase:\n"
            "  - Provide factual information only (describe what the service offers, pricing, hours, etc.).\n"
            "  - Do NOT promise, invite, offer, or imply that the guest can use or book the service right now.\n"
            "  - Do NOT begin collecting booking/order slots for an out-of-phase service.\n"
            "  - At the END of your response, always add a closing note such as: 'Please note that booking or ordering this service is not available during the current phase — it will be available during [phase name].'\n"
            "  - Keep the tone warm and informative — not transactional.\n"
            "This rule applies even if the guest seems eager to book or asks you to proceed."
        )

        # ══ SECTION 2: TICKET CREATION (only shown when ticketing is enabled) ═
        # When ticket creation is on, the agent must collect the required info
        # and trigger a ticket. When it is off, skip this section entirely.
        if not ticketing_enabled:
            lines.append(
                "\n=== BOOKING / ORDER PROCESSING ===\n"
                "This service does NOT support booking or order processing through chat. "
                "Do NOT collect booking/order slots. Do NOT initiate or continue any transactional flow. "
                "Do NOT ask for confirmation. Provide factual information only and direct the guest to contact "
                "staff directly if they wish to proceed."
            )

        _valid_slots: list[dict] = [s for s in required_slots if isinstance(s, dict)]
        _already_collected_note = (
            "Before asking for any field, check pending_data_collected AND memory_facts. "
            "If a value is already known from either, do NOT ask for it again — use it directly."
        )
        _use_slot_list = ticketing_enabled and slots_are_custom and bool(_valid_slots)

        if ticketing_enabled:
            ticket_lines = ["\n=== TICKET CREATION ==="]
            policy_text = str(ticketing_policy.get("policy") or "").strip()
            if policy_text:
                ticket_lines.append(f"Ticketing policy: {policy_text}")
            if ticketing_conditions:
                ticket_lines.append(
                    "Admin ticketing condition text is the primary source of truth for required details. "
                    "If slot lists conflict with that text, follow the admin ticketing condition text."
                )

            # What to collect
            if ticketing_conditions:
                ticket_lines.append(
                    f"\nWhat to collect before raising a ticket:\n\"{ticketing_conditions}\"\n"
                    "Collect every piece of information described above. "
                    "Ask for ALL missing fields in one message — never one at a time."
                )
            elif _use_slot_list and _valid_slots:
                required_s = [s for s in _valid_slots if bool(s.get("required", True))]
                optional_s = [s for s in _valid_slots if not bool(s.get("required", True))]
                if required_s:
                    ticket_lines.append("\nRequired fields (collect ALL before confirming):")
                    for s in required_s:
                        ticket_lines.append(f"  - {s.get('label') or s.get('id')} (id: {s.get('id')})")
                if optional_s:
                    ticket_lines.append("Optional fields (collect only if the guest mentions them):")
                    for s in optional_s:
                        ticket_lines.append(f"  - {s.get('label') or s.get('id')} (id: {s.get('id')})")
                ticket_lines.append(
                    "\nAsk for ALL missing required fields in a single message — never one at a time.\n"
                    "Do NOT move to confirmation until every required field has a value."
                )

            if not ticketing_conditions and not (_use_slot_list and _valid_slots):
                ticket_lines.append(
                    "\nNo structured slot schema is configured for this service.\n"
                    "Derive needed details from admin ticketing policy text, service description, and user request.\n"
                    "Ask concise clarifying questions only for missing details required for accurate execution.\n"
                    "Do not invent or enforce fixed field names."
                )
            ticket_lines.append(_already_collected_note)
            ticket_lines.append("Save each collected value in pending_data_updates using the exact field id as the key.")

            # Confirmation step
            if requires_confirmation_step:
                confirm_template = str(confirmation_format.get("template") or "").strip()
                ticket_lines.append(
                    f"\nOnce all fields are collected, summarise everything clearly and ask the guest to confirm.\n"
                    f"Confirmation phrase the guest must say: '{confirmation_phrase}'\n"
                    f"Set pending_action='{confirmation_pending_action}' while waiting for confirmation.\n"
                    f"When the guest confirms: set action=create_ticket, ticket.required=true, "
                    f"ticket.ready_to_create=true, pending_action=null."
                )
                if confirm_template:
                    ticket_lines.append(f"Confirmation template: {confirm_template}")

            # Hard guard
            if ticketing_conditions:
                ticket_lines.append(f"\nDo NOT create a ticket until: {ticketing_conditions}")
            elif _use_slot_list:
                _req_names = [
                    s.get("label") or s.get("id")
                    for s in required_slots
                    if isinstance(s, dict) and bool(s.get("required", True))
                ]
                if _req_names:
                    ticket_lines.append(
                        f"\nDo NOT create a ticket until ALL of these are collected: {', '.join(_req_names)}."
                    )
            else:
                ticket_lines.append(
                    "\nDo NOT create a ticket until the guest has provided enough concrete details "
                    "for staff to execute the request safely."
                )
            ticket_lines.append("Set ticket.issue to a clear human-readable summary of what was requested.")

            # Mid-flow complaint handling: if the guest reports a problem about THIS service
            # while you are already collecting their booking/order details, handle it here —
            # do NOT bounce it to the main orchestrator. You have the full context.
            ticket_lines.append(
                "\n=== MID-FLOW COMPLAINT HANDLING ===\n"
                "If the guest reports a problem, malfunction, or dissatisfaction with this service "
                "while you are mid-flow (slots are being collected): handle it directly. "
                "Set action=create_ticket, ticket.category='complaint', ticket.ready_to_create=true, "
                "ticket.issue=a clear summary of the problem including any context already collected. "
                "Clear pending_action. Do NOT continue the booking/order flow after a complaint is raised."
            )
            lines.extend(ticket_lines)

        # ══ SECTION 3: KNOWLEDGE BASE ══════════════════════════════════════════
        # This is the complete knowledge for this service, exactly as entered in the system.
        # The agent must answer all questions from this knowledge and nothing else.
        kb_content = service_knowledge or full_kb_text
        if kb_content:
            lines.append(
                f"\n=== {service_name.upper()} — KNOWLEDGE BASE ===\n"
                f"{kb_content}\n"
                f"=== END OF KNOWLEDGE BASE ==="
            )
        elif description:
            lines.append(f"\nService description (use as knowledge): {description}")

        lines.append(
            "\nRULE: Answer ONLY from the knowledge base above. "
            "If something the guest asks is not covered there, say you do not have that detail "
            "and offer to connect them with staff. Never guess, invent, or assume anything."
        )

        # ── Operating constraints (hours, zones, cuisine) ─────────────────────
        constraints: list[str] = []
        if service_hours.get("open") or service_hours.get("close"):
            open_t = str(service_hours.get("open") or "").strip()
            close_t = str(service_hours.get("close") or "").strip()
            hour_str = " – ".join(filter(None, [open_t, close_t]))
            constraints.append(f"Operating hours: {hour_str}. Inform guest if their request is outside these hours.")
        if delivery_zones:
            constraints.append(f"Delivery zones: {', '.join(delivery_zones)}.")
        if cuisine:
            constraints.append(f"Cuisine type: {cuisine}.")
        if constraints:
            lines.append("\n--- CONSTRAINTS ---\n" + "\n".join(constraints))

        # ── Scope and handoff ─────────────────────────────────────────────────
        lines.append(
            f"\nYou only handle {service_name} topics. "
            "When a guest asks a question, first decide: is this question about THIS service's domain?\n"
            "  - If YES (topic-adjacent — e.g. room types/sizes/amenities/pricing/policies during a room booking flow): "
            "answer from your knowledge base. If the specific detail is not in your KB, say it is not available in the current system and offer to connect them with staff. "
            "Keep the pending booking/order flow alive — do NOT set context_switched.\n"
            "  - If NO (completely unrelated to this service — e.g. asking about the pool, spa, dining, gym, parking, events, or any other hotel facility while mid-booking): "
            "do NOT attempt to answer. Set context_switched=true in metadata and use a short handoff line like 'Let me get that answered for you.' "
            "The main hotel assistant has the full hotel knowledge base and will answer it. "
            "The suspended booking flow will be offered for resume after the main assistant answers.\n"
            "Rule of thumb: if the question is about THIS service's subject matter → stay and answer or say unavailable. "
            "If the question is about a completely different part of the hotel → delegate via context_switched.\n"
            "Always read history_last_10 first, then full_history_context (including older_context_summary) before responding. "
            "Use last_user_message and last_assistant_message as the strongest anchors for short replies.\n"
            "Resolve short replies ('yes', a date, a number) against what was last asked.\n"
            "Capture any useful guest details (name, room number, preferences, dietary needs, special occasion) "
            "in new_facts_to_remember as snake_case key-value pairs.\n"
            "Return STRICT JSON only matching the response schema."
        )

        # ── RESPONSE SCHEMA ───────────────────────────────────────────────────
        lines.append(
            "\nResponse schema (return this exact structure):\n"
            "{\n"
            '  "action": "respond_only|collect_info|create_ticket|cancel_pending",\n'
            '  "response_text": "your reply to the guest",\n'
            '  "pending_action": "short_descriptor_or_null",\n'
            '  "pending_data_updates": {"slot_id": "value"},\n'
            '  "missing_fields": ["slot_id"],\n'
            '  "suggested_actions": ["short chip label"],\n'
            '  "requires_human_handoff": false,\n'
            '  "new_facts_to_remember": {"guest_name": "value", "room_number": "value"},\n'
            '  "ticket": {\n'
            '    "required": false,\n'
            '    "ready_to_create": false,\n'
            '    "issue": "human readable description",\n'
            '    "category": "",\n'
            '    "sub_category": "",\n'
            '    "priority": "low|medium|high|critical"\n'
            '  },\n'
            '  "metadata": {"context_switched": false}\n'
            "}"
        )

        return "\n".join(lines)

    async def _run_service_agent(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        memory_snapshot: dict[str, Any],
        service: dict[str, Any],
        base_decision: OrchestrationDecision,
        selected_phase_id: str,
        selected_phase_name: str,
    ) -> OrchestrationDecision:
        service_id = self._normalize_identifier(service.get("id"))
        service_name = str(service.get("name") or service_id).strip()
        prompt_pack = service.get("service_prompt_pack")
        if not isinstance(prompt_pack, dict):
            prompt_pack = {
                "profile": service.get("profile"),
                "role": service.get("service_prompt_role"),
                "professional_behavior": service.get("service_prompt_behavior"),
                "required_slots": service.get("required_slots"),
                "ticketing_policy": {"policy": service.get("ticketing_policy")},
                "execution_guard": service.get("execution_guard"),
                "confirmation_format": service.get("confirmation_format"),
            }
        service_facts_raw = service.get("knowledge_facts", [])
        if not isinstance(service_facts_raw, list):
            service_facts_raw = []
        service_facts: list[str] = [str(f).strip() for f in service_facts_raw if str(f).strip()]
        extracted_knowledge = str(service.get("extracted_knowledge") or "").strip()
        _raw_pack = service.get("service_prompt_pack")
        if not isinstance(_raw_pack, dict):
            _raw_pack = {}
        _raw_pack_source = str(_raw_pack.get("source") or "").strip().lower()
        _pack_is_admin_managed = (
            bool(service.get("service_prompt_pack_custom", False))
            or _raw_pack_source in {"manual_override", "admin_ui", "admin_override", "db"}
        )
        required_slots_raw = prompt_pack.get("required_slots", [])
        if _pack_is_admin_managed and isinstance(required_slots_raw, list):
            required_slots = required_slots_raw
        else:
            required_slots = []
        validation_rules_raw = prompt_pack.get("validation_rules", [])
        if _pack_is_admin_managed and isinstance(validation_rules_raw, list):
            validation_rules = validation_rules_raw
        else:
            validation_rules = []
        confirmation_format = prompt_pack.get("confirmation_format", {})
        if not isinstance(confirmation_format, dict):
            confirmation_format = {}
        behavior = str(prompt_pack.get("professional_behavior") or "").strip()
        service_profile = str(prompt_pack.get("profile") or "").strip().lower()
        role = str(prompt_pack.get("role") or "").strip() or f"You are the {service_name} service assistant."
        description = str(service.get("description") or "").strip()
        ticketing_policy = prompt_pack.get("ticketing_policy", {})
        if not isinstance(ticketing_policy, dict):
            ticketing_policy = {}
        ticketing_conditions = str(prompt_pack.get("ticketing_conditions") or "").strip()
        if not ticketing_conditions:
            ticketing_conditions = str(ticketing_policy.get("policy") or "").strip()
        if not ticketing_conditions:
            ticketing_conditions = str(service.get("ticketing_policy") or "").strip()
        service_hours = service.get("hours") or {}
        if not isinstance(service_hours, dict):
            service_hours = {}
        service_delivery_zones = service.get("delivery_zones") or []
        if not isinstance(service_delivery_zones, list):
            service_delivery_zones = []
        service_cuisine = str(service.get("cuisine") or "").strip()
        ticketing_enabled = bool(service.get("ticketing_enabled", True))
        slots_are_custom = _pack_is_admin_managed and bool(required_slots)

        model = str(getattr(settings, "llm_service_agent_model", "") or "").strip() or None
        confirmation_phrase = str(confirmation_format.get("required_phrase") or "").strip()
        if not confirmation_phrase:
            confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()
        confirmation_pending_action = "confirm_booking"
        # Confirmation is required whenever ticketing is enabled — the guest must confirm before a ticket is raised.
        requires_confirmation_step = ticketing_enabled
        history_bundle = self._build_history_bundle(
            context=context,
            memory_snapshot=memory_snapshot,
            last_window_messages=10,
            last_window_chars=max(4000, int(getattr(settings, "llm_orchestration_history_chars", 8000) or 8000)),
            full_window_messages=max(
                60,
                int(getattr(settings, "llm_orchestration_full_history_messages", 120) or 120),
            ),
            full_window_chars=max(
                7000,
                int(getattr(settings, "llm_orchestration_full_history_chars", 12000) or 12000),
            ),
        )
        pending_pub_pre = self._coerce_public_pending(context.pending_data)
        # First turn = no slot data collected yet for this service
        is_first_service_turn = not any(
            str(v or "").strip()
            for k, v in pending_pub_pre.items()
            if k not in {"service_id"}
        )
        # Get full KB text for fallback when no extracted knowledge exists
        full_kb_text = ""
        if not extracted_knowledge:
            from services.config_service import config_service as _cs
            full_kb_text = _cs.get_full_kb_text(max_chars=40_000) or ""

        system_prompt = self._build_service_system_prompt(
            service_id=service_id,
            service_name=service_name,
            role=role,
            behavior=behavior,
            description=description,
            service_knowledge=extracted_knowledge,
            knowledge_facts=service_facts[:30],
            required_slots=required_slots,
            slots_are_custom=slots_are_custom,
            validation_rules=validation_rules,
            confirmation_format=confirmation_format,
            ticketing_enabled=ticketing_enabled,
            ticketing_policy=ticketing_policy,
            ticketing_conditions=ticketing_conditions,
            service_hours=service_hours,
            delivery_zones=service_delivery_zones,
            cuisine=service_cuisine,
            confirmation_phrase=confirmation_phrase,
            confirmation_pending_action=confirmation_pending_action,
            requires_confirmation_step=requires_confirmation_step,
            full_kb_text=full_kb_text,
        )

        pending_pub = self._coerce_public_pending(context.pending_data)
        # Build a human-readable view of already-collected slot data for the LLM
        collected_labels: dict[str, Any] = {}
        for s in required_slots:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            val = pending_pub.get(sid)
            if val is not None:
                collected_labels[s.get("label") or sid] = val

        payload = {
            "user_message": str(user_message or ""),
            "is_first_turn": is_first_service_turn,
            "current_phase_id": selected_phase_id,
            "service_phase_id": self._normalize_identifier(service.get("phase_id")),
            "pending_action": str(context.pending_action or ""),
            "pending_data_collected": collected_labels,
            "pending_data_raw": self._sanitize_json(pending_pub),
            "history": history_bundle.get("history_last_10", []),
            "history_last_10": history_bundle.get("history_last_10", []),
            "full_history_context": history_bundle.get("full_history_context", {}),
            "last_user_message": str(history_bundle.get("last_user_message") or ""),
            "last_assistant_message": str(history_bundle.get("last_assistant_message") or ""),
            "memory_summary": str(memory_snapshot.get("summary") or "")[:1200],
            "memory_facts": self._sanitize_json(memory_snapshot.get("facts", {})),
            "confirmation_phrase": confirmation_phrase,
        }
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        _log_llm_call(
            label=f"SERVICE AGENT [{service_id}]",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
        )
        raw = await self._chat_with_json(
            messages=messages,
            model=model,
            temperature=0.0,
            trace_context={
                "responder_type": "service",
                "agent": "service_agent",
                "session_id": str(getattr(context, "session_id", "")),
                "service_id": service_id,
                "service_name": service_name,
                "selected_phase_id": selected_phase_id,
                "selected_phase_name": selected_phase_name,
            },
        )
        _log_llm_call(
            label=f"SERVICE AGENT [{service_id}] RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={},
            response=raw,
        )
        decision = self._safe_parse_decision(raw)
        decision.target_service_id = service_id or decision.target_service_id
        model_source = str(decision.metadata.get("source") or "").strip()
        if model_source and model_source != "service_agent":
            decision.metadata["service_agent_model_source"] = model_source
        decision.metadata["source"] = "service_agent"
        decision.metadata["service_agent_id"] = service_id
        decision.metadata["is_first_service_turn"] = is_first_service_turn

        # Context-switch marker for immediate same-turn main-orchestrator re-route.
        if bool((decision.metadata or {}).get("context_switched")):
            decision.metadata["context_switched"] = True

        # Persist any new guest facts the service LLM discovered this turn.
        new_facts = raw.get("new_facts_to_remember") if isinstance(raw, dict) else None
        if isinstance(new_facts, dict) and new_facts:
            from services.conversation_memory_service import conversation_memory_service as _cms
            import datetime as _dt
            memory = _cms.ensure_memory(context)
            facts = memory.setdefault("facts", {})
            fact_history = memory.setdefault("fact_history", [])
            for key, value in new_facts.items():
                key_clean = str(key or "").strip().lower().replace(" ", "_")
                if not key_clean or value is None:
                    continue
                _cms._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key=key_clean,
                    value=value,
                    source_message=str(user_message or "")[:300],
                    change_type="set",
                )
            memory["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            decision.metadata["new_facts_saved"] = list(new_facts.keys())

        user_compact = re.sub(r"\s+", " ", str(user_message or "").strip().lower())
        confirmation_compact = re.sub(r"\s+", " ", confirmation_phrase.lower())
        awaiting_confirmation = str(context.pending_action or "").strip().lower().startswith("confirm_")
        strict_confirmation_enforced = bool(getattr(settings, "chat_require_strict_confirmation_phrase", False))

        # Deterministic confirmation override — if the user said the confirmation phrase
        # while we were in a confirm_* pending state (or last bot message asked for it),
        # force ticket creation regardless of what the LLM returned.
        last_bot_asked_confirmation = False
        if strict_confirmation_enforced:
            for _msg in reversed(context.messages):
                _role = str(getattr(_msg, "role", "") or "").lower()
                if "assistant" in _role:
                    _content = str(getattr(_msg, "content", "") or "").lower()
                    # Match if last bot message asked for confirmation (contains "confirm" broadly)
                    if "confirm" in _content or confirmation_compact in _content:
                        last_bot_asked_confirmation = True
                    break

        if strict_confirmation_enforced and (awaiting_confirmation or last_bot_asked_confirmation) and user_compact == confirmation_compact:
            pending_pub = self._coerce_public_pending(context.pending_data)
            # Build auto_issue from ALL collected pending data — no hardcoded slot names
            skip_keys = {"service_id", "service_name"}
            detail_parts = [
                f"{k.replace('_', ' ')}: {v}"
                for k, v in pending_pub.items()
                if k not in skip_keys and str(v or "").strip()
            ]
            auto_issue = service_name
            if detail_parts:
                auto_issue = f"{service_name} — {', '.join(detail_parts)}"
            decision.action = "create_ticket"
            decision.pending_action = None
            decision.missing_fields = []
            decision.ticket.required = True
            decision.ticket.ready_to_create = True
            if not str(decision.ticket.issue or "").strip():
                decision.ticket.issue = auto_issue
            if not str(decision.ticket.category or "").strip():
                decision.ticket.category = "request"
            if not str(decision.ticket.sub_category or "").strip():
                decision.ticket.sub_category = self._normalize_identifier(service_id)
            # Keep LLM response_text only if it looks like a genuine booking confirmation.
            # Normalize Unicode apostrophes (LLM often returns \u2019 instead of ASCII ')
            resp_lower = str(decision.response_text or "").strip().lower()
            resp_lower = resp_lower.replace("\u2019", "'").replace("\u2018", "'").replace("\u2014", " ")
            bad_signals = (
                "table booking", "table reservation",
                "can't confirm", "cant confirm", "cannot confirm",
                "unable to confirm", "i'm unable", "im unable",
                "i can help with", "feel free to ask",
                "hotel enquiries", "sightseeing",
                "not available", "can't help", "cant help",
            )
            # A genuine confirmation should mention "confirm" or "booking" or "reservation"
            good_signals = ("confirm", "booking confirmed", "reservation confirmed", "booking has been")
            looks_genuine = any(sig in resp_lower for sig in good_signals)
            if not resp_lower or not looks_genuine or any(sig in resp_lower for sig in bad_signals):
                summary_parts = []
                if room_type:
                    summary_parts.append(f"room: {room_type}")
                if checkin:
                    summary_parts.append(f"check-in: {checkin}")
                if checkout:
                    summary_parts.append(f"check-out: {checkout}")
                if guests:
                    summary_parts.append(f"guests: {guests}")
                summary = ", ".join(summary_parts)
                decision.response_text = (
                    f"Your {service_name} has been confirmed! "
                    + (f"Details: {summary}. " if summary else "")
                    + "A request has been raised and our team will follow up shortly."
                )
            decision.metadata["confirmation_override"] = True
        active_flow_signal = bool(str(context.pending_action or "").strip() or str(base_decision.pending_action or "").strip())
        if bool(base_decision.missing_fields):
            active_flow_signal = True
        if (
            strict_confirmation_enforced
            and requires_confirmation_step
            and active_flow_signal
            and not awaiting_confirmation
            and user_compact != confirmation_compact
            and not bool(decision.requires_human_handoff)
            and str(decision.action or "").strip().lower() in {"respond_only", "collect_info"}
            and not bool(decision.missing_fields)
            and not bool(getattr(decision.ticket, "ready_to_create", False))
            and not str(decision.pending_action or "").strip()
        ):
            decision.pending_action = confirmation_pending_action
            response_lower = str(decision.response_text or "").strip().lower()
            if confirmation_phrase.lower() not in response_lower:
                confirmation_line = f"Please reply '{confirmation_phrase}' to confirm."
                if str(decision.response_text or "").strip():
                    decision.response_text = f"{str(decision.response_text).strip()} {confirmation_line}".strip()
                else:
                    decision.response_text = confirmation_line
            decision.metadata.setdefault("confirmation_flow_forced", True)
            decision.metadata.setdefault("confirmation_pending_action", confirmation_pending_action)
        # Always stamp service_id into pending_data_updates so sticky routing works.
        # This ensures context.pending_data["service_id"] is always set while a
        # service is active, regardless of what the LLM put in pending_data_updates.
        if str(decision.pending_action or "").strip():
            if not isinstance(decision.pending_data_updates, dict):
                decision.pending_data_updates = {}
            decision.pending_data_updates.setdefault("service_id", service_id)

        decision = self._apply_answer_priority_fields(decision)
        return decision

    async def orchestrate_turn(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        memory_snapshot: dict[str, Any],
        selected_phase_context: dict[str, Any] | None = None,
        _service_reroute_depth: int = 0,
    ) -> OrchestrationDecision | None:
        orchestration_mode_enabled = bool(getattr(settings, "chat_llm_orchestration_mode", False))
        no_template_mode_enabled = bool(getattr(settings, "chat_no_template_response_mode", False))
        if not orchestration_mode_enabled and not no_template_mode_enabled:
            return None
        if not str(settings.openai_api_key or "").strip():
            return None

        # ── RESUME PROMPT HANDLER ────────────────────────────────────────────
        # If last turn asked user to resume a suspended service, annotate the
        # message with context and let the LLM orchestrator decide the intent.
        if context.resume_prompt_sent:
            context.resume_prompt_sent = False
            if context.suspended_services:
                suspended = context.suspended_services[0]
                svc_name = suspended.get("service_name", "previous request")
                # Annotate so the orchestrator sees full context and decides
                user_message = (
                    f"[CONTEXT: bot just asked whether to resume '{svc_name}'. "
                    f"Guest replied: '{user_message}'. "
                    f"Decide based on the reply whether to resume or abandon.]"
                )
        # ─────────────────────────────────────────────────────────────────────


        selected_phase_id, selected_phase_name = self._resolve_selected_phase(
            context=context,
            selected_phase_context=selected_phase_context,
            capabilities_summary=capabilities_summary,
        )
        orchestration_trace_id = f"orch-{uuid.uuid4().hex[:12]}"

        services_snapshot = self._service_snapshot(capabilities_summary)
        allowed_service_rows = [
            row
            for row in services_snapshot
            if isinstance(row, dict)
            and bool(row.get("is_active", True))
            and self._normalize_identifier(row.get("phase_id")) == selected_phase_id
        ]
        allowed_service_ids = [
            self._normalize_identifier(row.get("id"))
            for row in allowed_service_rows
            if self._normalize_identifier(row.get("id"))
        ][:60]
        allowed_service_names = [
            str(row.get("name") or "").strip()
            for row in allowed_service_rows
            if str(row.get("name") or "").strip()
        ][:60]
        out_of_phase_services: list[dict[str, str]] = []
        for row in services_snapshot:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("is_active", True)):
                continue
            phase_id = self._normalize_identifier(row.get("phase_id"))
            if not phase_id or phase_id == selected_phase_id:
                continue
            sid = self._normalize_identifier(row.get("id"))
            if not sid:
                continue
            out_of_phase_services.append(
                {
                    "id": sid,
                    "name": str(row.get("name") or sid).strip(),
                    "phase_id": phase_id,
                    "phase_name": str(row.get("phase_name") or self._phase_label(phase_id, capabilities_summary)).strip(),
                    "description": str(row.get("description") or "").strip(),
                    "profile": str(row.get("profile") or "").strip(),
                    "extracted_knowledge": str(row.get("extracted_knowledge") or "").strip()[:2500],
                    "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                    "ticketing_conditions": str(row.get("ticketing_conditions") or "").strip(),
                }
            )
            if len(out_of_phase_services) >= 120:
                break
        allowed_services_detailed = [
            {
                "id": self._normalize_identifier(row.get("id")),
                "name": str(row.get("name") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                "phase_id": self._normalize_identifier(row.get("phase_id")),
                "phase_name": str(row.get("phase_name") or "").strip(),
                "knowledge_facts": row.get("knowledge_facts") or [],
                "extracted_knowledge": str(row.get("extracted_knowledge") or "").strip()[:5000],
                "profile": str(row.get("profile") or "").strip(),
                "required_slots": row.get("required_slots") or [],
            }
            for row in allowed_service_rows
            if isinstance(row, dict) and self._normalize_identifier(row.get("id"))
        ][:120]
        out_of_phase_services_detailed = [
            {
                "id": str(row.get("id") or "").strip(),
                "name": str(row.get("name") or "").strip(),
                "phase_id": str(row.get("phase_id") or "").strip(),
                "phase_name": str(row.get("phase_name") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "profile": str(row.get("profile") or "").strip(),
                "extracted_knowledge": str(row.get("extracted_knowledge") or "").strip()[:2500],
                "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                "ticketing_conditions": str(row.get("ticketing_conditions") or "").strip(),
                "availability_hint": self._phase_transition_timing_hint(
                    selected_phase_id,
                    str(row.get("phase_id") or "").strip(),
                ),
            }
            for row in out_of_phase_services
            if isinstance(row, dict)
        ][:160]
        full_kb_text = config_service.get_full_kb_text(max_chars=60_000)
        # Augment full_kb_text with extracted_knowledge from ALL service KB records.
        # This ensures admin-entered KB content (stored in service_kb_records, not in flat files)
        # is always visible to the orchestrator when it needs to answer directly.
        service_kb_records = capabilities_summary.get("service_kb_records", [])
        if isinstance(service_kb_records, list):
            extracted_parts: list[str] = []
            for rec in service_kb_records[:30]:
                if not isinstance(rec, dict):
                    continue
                ek = str(rec.get("extracted_knowledge") or "").strip()
                sid = str(rec.get("service_id") or "").strip()
                if ek and sid:
                    extracted_parts.append(f"[Service: {sid}]\n{ek}")
            if extracted_parts:
                combined_service_kb = "\n\n---\n\n".join(extracted_parts)
                full_kb_text = (
                    (full_kb_text + "\n\n" if full_kb_text else "")
                    + "=== SERVICE KNOWLEDGE BASE ===\n\n"
                    + combined_service_kb
                )[:80_000]
        history_bundle = self._build_history_bundle(
            context=context,
            memory_snapshot=memory_snapshot,
            last_window_messages=10,
            last_window_chars=max(3000, int(getattr(settings, "llm_orchestration_history_chars", 8000) or 8000)),
            full_window_messages=max(
                60,
                int(getattr(settings, "llm_orchestration_full_history_messages", 120) or 120),
            ),
            full_window_chars=max(
                7000,
                int(getattr(settings, "llm_orchestration_full_history_chars", 12000) or 12000),
            ),
        )
        payload = {
            "trace_id": orchestration_trace_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "user_message": str(user_message or ""),
            "state": context.state.value,
            "pending_action": str(context.pending_action or ""),
            "pending_action_context": self._build_pending_action_context(context),
            "pending_data_public": self._sanitize_json(self._coerce_public_pending(context.pending_data)),
            "selected_phase": {
                "id": selected_phase_id,
                "name": selected_phase_name,
            },
            "memory_summary": str(memory_snapshot.get("summary") or "")[:1400],
            "memory_facts": self._sanitize_json(memory_snapshot.get("facts", {})),
            "suspended_services": [
                {"service_id": s.get("service_id"), "service_name": s.get("service_name"), "collected_so_far": list((s.get("pending_data") or {}).keys())}
                for s in (context.suspended_services or [])
            ],
            "full_knowledge_base": full_kb_text or None,
            "history": history_bundle.get("history_last_10", []),
            "history_last_10": history_bundle.get("history_last_10", []),
            "full_history_context": history_bundle.get("full_history_context", {}),
            "last_user_message": str(history_bundle.get("last_user_message") or ""),
            "last_assistant_message": str(history_bundle.get("last_assistant_message") or ""),
            "services": self._sanitize_json(services_snapshot),
            "policy_snapshot": {
                "selected_phase_id": selected_phase_id,
                "selected_phase_name": selected_phase_name,
                "allowed_service_ids_in_current_phase": allowed_service_ids,
                "allowed_service_names_in_current_phase": allowed_service_names,
                "out_of_phase_services": self._sanitize_json(out_of_phase_services),
                "phase_service_policy": {
                    "current_phase_id": selected_phase_id,
                    "current_phase_name": selected_phase_name,
                    "allowed_services": self._sanitize_json(allowed_services_detailed),
                    "blocked_out_of_phase_services": self._sanitize_json(out_of_phase_services_detailed),
                },
                "ticketing_enabled_by_service": {
                    self._normalize_identifier(row.get("id")): bool(row.get("ticketing_enabled", True))
                    for row in services_snapshot
                    if isinstance(row, dict) and self._normalize_identifier(row.get("id"))
                },
                "execution_rules": [
                    "Response text must be final user-facing text; do not rely on handler text generation.",
                    "Out-of-phase service asks must not trigger transactional execution.",
                    "Ticket creation intent is allowed only when service ticketing is enabled.",
                ],
            },
            "complaint_routing_note": (
                "IMPORTANT: 'ticketing_enabled=true' on a service means the service CAN raise a support ticket "
                "if the guest reports a problem with it — it does NOT mean the service is a complaint handler. "
                "Room bookings, food orders, and all other service requests are handled as normal service requests. "
                "Only set action=create_ticket when the guest explicitly describes a problem, malfunction, or dissatisfaction. "
                "Asking about a service, requesting a booking, or asking for information is NEVER a complaint."
            ),
            "response_contract": {
                "normalized_query": "string",
                "intent": "core intent",
                "confidence": "0..1",
                "action": "respond_only|collect_info|dispatch_handler|create_ticket|resume_pending|cancel_pending",
                "target_service_id": "exact service id from services list when service-specific",
                "response_text": "assistant response",
                "pending_action": "string|null",
                "pending_data_updates": {"slot": "value"},
                "missing_fields": ["field_id"],
                "answered_current_query": "bool",
                "blocking_fields": ["field_id"],
                "deferrable_fields": ["field_id"],
                "followup_question": "single question or empty",
                "suggested_actions": ["short action"],
                "use_handler": "bool",
                "handler_intent": "intent string when use_handler=true",
                "interrupt_pending": "bool",
                "resume_pending": "bool",
                "cancel_pending": "bool",
                "requires_human_handoff": "bool",
                "ticket": {
                    "required": "bool",
                    "ready_to_create": "bool",
                    "reason": "string",
                    "issue": "string",
                    "category": "string",
                    "sub_category": "string",
                    "priority": "low|medium|high|critical",
                },
                "metadata": {"any": "json"},
            },
        }

        system_prompt = (
            "You are the main concierge assistant for this hotel. "
            "You are the single authority on routing and responding — no other layer will override your decision.\n"
            "\n"
            "=== STEP 1: READ HISTORY ===\n"
            "Always read history_last_10 first, then full_history_context before deciding anything. "
            "Use last_user_message and last_assistant_message as high-priority anchors for continuity. "
            "Resolve pronouns ('it', 'that', 'same', 'this') against the last assistant message.\n"
            "\n"
            "=== STEP 2: MID-FLOW CHECK ===\n"
            "Read pending_action and pending_action_context carefully. "
            "If pending_action is set, the guest is mid-flow in a service — read pending_action_context to understand exactly what was happening. "
            "Stay on that service and route back to it immediately using action=dispatch_handler with the same target_service_id. "
            "Short replies ('yes', 'ok', 'sure', 'no', 'cancel', a number, a name, a date) when pending_action is set are ALWAYS a direct continuation of that flow — never re-route them. "
            "A question about the current service (e.g. asking about vehicle type during airport transfer, asking about room details during room booking, asking about menu during table booking) is NOT an interrupt — dispatch to the same service agent so it can answer and continue collecting slots. "
            "When the guest confirms a booking ('yes', 'yes confirm', 'ok', 'proceed', 'go ahead') while pending_action is set: dispatch to the service agent — the service agent owns confirmation and ticket creation. Do NOT set action=create_ticket yourself. "
            "When the guest cancels or refuses ('no', 'cancel', 'stop', 'forget it') while pending_action is set: set action=cancel_pending and acknowledge politely. "
            "Only interrupt the pending flow if the guest explicitly and clearly asks to start a completely unrelated and different service.\n"
            "\n"
            "=== STEP 2B: AMBIGUITY CHECK ===\n"
            "If the message is short, vague, or has no clear intent AND pending_action is empty AND last_assistant_message does not provide enough context to interpret it — "
            "do not guess at intent and do not route to any service. Set action=respond_only and generate a friendly clarification question asking what the guest needs. "
            "Example: 'Could you tell me a bit more about what you are looking for?'\n"
            "\n"
            "=== STEP 3: DECIDE WHAT THE GUEST NEEDS ===\n"
            "\n"
            "A) INFORMATION QUESTION — guest is asking about hotel facilities, timings, policies, menus, room types, or any general details.\n"
            "   -> Answer directly from extracted_knowledge and full_knowledge_base. Set action=respond_only.\n"
            "   -> Asking about room types, availability, prices, or facilities is NEVER a complaint.\n"
            "   -> Never invent facts. If details are not present, say they are not available.\n"
            "\n"
            "B) SERVICE REQUEST — guest wants to book, order, request, or arrange something.\n"
            "   -> From the very first message requesting a service — even just 'I need a room', 'book a table', 'get me a cab' — IMMEDIATELY set action=dispatch_handler. Do not answer first, do not ask for details yourself.\n"
            "   -> The service agent handles ALL slot collection and responses. Your job is only to dispatch to the right service.\n"
            "   -> target_service_id MUST be an exact ID from allowed_service_ids_in_current_phase (e.g. 'room_booking_support'). NEVER use 'complaint', 'complaint_service', 'complain', or any complaint-flavored ID for a service request.\n"
            "   -> If the service is only in blocked_out_of_phase_services: set action=respond_only. Provide factual information about the service (what it offers, hours, pricing if known). Do NOT promise, invite, offer, or imply the guest can use it now. At the END of your response, always add a note like: 'Please note that booking/ordering for this service is not available in the current phase — it will be available during [phase name].'\n"
            "   -> Wanting to book a room, order food, or request any service is NEVER a complaint.\n"
            "\n"
            "C) COMPLAINT / ISSUE — guest explicitly reports a problem, malfunction, or dissatisfaction WITH NO active pending service flow.\n"
            "   -> ONLY when guest describes something that went wrong. Questions about services are NOT complaints.\n"
            "   -> Set action=create_ticket, ticket.category='complaint'.\n"
            "   -> NEVER use complaint routing for: room inquiries, booking requests, food orders, or any request to do something.\n"
            "\n"
            "D) HUMAN / EMERGENCY — guest is distressed or explicitly asks for a human: set requires_human_handoff=true.\n"
            "\n"
            "=== ROUTING SANITY CHECK ===\n"
            "Before writing output, ask: is the guest asking for something or reporting a problem? "
            "Asking → service request or info. Reporting → complaint. When in doubt → never assume complaint.\n"
            "\n"
            "=== GROUNDING RULE ===\n"
            "Use only provided policy + knowledge data. Never invent prices, timings, availability, or capabilities.\n"
            "\n"
            "=== OUTPUT ===\n"
            "Return strict JSON only."
        )
        model = str(getattr(settings, "llm_orchestration_model", "") or "").strip() or None
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        _log_llm_call(
            label="ORCHESTRATOR",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
        )
        raw = await self._chat_with_json(
            messages=messages,
            model=model,
            temperature=0.0,
            trace_context={
                "responder_type": "main",
                "agent": "orchestrator",
                "session_id": str(getattr(context, "session_id", "")),
                "selected_phase_id": selected_phase_id,
                "selected_phase_name": selected_phase_name,
                "trace_id": orchestration_trace_id,
            },
        )
        _log_llm_call(
            label="ORCHESTRATOR RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={},
            response=raw,
        )
        decision = self._safe_parse_decision(raw)
        decision.metadata.setdefault("source", "orchestrator")
        decision.metadata.setdefault("orchestration_trace_id", orchestration_trace_id)
        decision = self._apply_answer_priority_fields(decision)

        if decision.action in {"resume_pending", "cancel_pending"}:
            return await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
            )

        decision.target_service_id = self._normalize_identifier(
            decision.target_service_id
            or (decision.metadata or {}).get("target_service_id")
            or (decision.metadata or {}).get("service_id")
            or ""
        )

        if not decision.target_service_id:
            resolved_service_id, action_hint = await self._resolve_target_service_with_llm(
                user_message=user_message,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                services_snapshot=services_snapshot,
            )
            if resolved_service_id:
                decision.target_service_id = resolved_service_id
                decision.metadata.setdefault("service_resolution_source", "service_router_llm")
                if (
                    str(decision.action or "").strip().lower() == "respond_only"
                    and action_hint in {"collect_info", "dispatch_handler", "create_ticket"}
                ):
                    decision.action = action_hint

        service_agent_enabled = bool(getattr(settings, "chat_llm_service_agent_enabled", True))
        target_service_id = self._normalize_identifier(decision.target_service_id)
        target_service = None
        for service in services_snapshot:
            if self._normalize_identifier(service.get("id")) == target_service_id:
                target_service = service
                break

        # Park unfinished active task before dispatching to a different service.
        self._suspend_active_service_task(
            context=context,
            services_snapshot=services_snapshot,
            next_service_id=target_service_id,
        )
        # ─────────────────────────────────────────────────────────────────────
        if not service_agent_enabled or not target_service_id:
            fallback_decision = await self._enforce_answer_first_policy(
                user_message=user_message,
                decision=decision,
                context=context,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
            )
            return await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=fallback_decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=target_service if isinstance(target_service, dict) else None,
            )

        if not isinstance(target_service, dict):
            fallback_decision = await self._enforce_answer_first_policy(
                user_message=user_message,
                decision=decision,
                context=context,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
            )
            return await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=fallback_decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
            )

        service_decision = await self._run_service_agent(
            user_message=user_message,
            context=context,
            memory_snapshot=memory_snapshot,
            service=target_service,
            base_decision=decision,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
        )
        service_decision.metadata.setdefault("orchestration_trace_id", orchestration_trace_id)
        if bool((service_decision.metadata or {}).get("context_switched")):
            # Preserve unfinished work from the current service before rerouting.
            suspended = self._suspend_active_service_task(
                context=context,
                services_snapshot=services_snapshot,
                next_service_id="",
                force=True,
            )
            if not suspended:
                # Even with no suspendable data, clear active service pin so main can re-route.
                internal = {
                    key: value
                    for key, value in (context.pending_data or {}).items()
                    if isinstance(key, str) and key.startswith("_")
                }
                context.pending_data = internal
                context.pending_action = None
            if _service_reroute_depth < 1:
                rerouted_decision = await self.orchestrate_turn(
                    user_message=user_message,
                    context=context,
                    capabilities_summary=capabilities_summary,
                    memory_snapshot=memory_snapshot,
                    selected_phase_context=selected_phase_context,
                    _service_reroute_depth=_service_reroute_depth + 1,
                )
                if isinstance(rerouted_decision, OrchestrationDecision):
                    rerouted_decision.metadata.setdefault(
                        "context_switch_rerouted_from_service",
                        target_service_id,
                    )
                    return rerouted_decision

        merged_decision = self._merge_decisions(decision, service_decision)
        # Skip answer-first enforcement for first-turn intros (it overwrites the room list),
        # for confirmation overrides (already deterministically correct),
        # and for ready-to-create ticket confirmations.
        ticket_creation_ready = bool(
            str(merged_decision.action or "").strip().lower() == "create_ticket"
            and bool(merged_decision.ticket.required)
            and bool(merged_decision.ticket.ready_to_create)
        )
        _skip_answer_first = bool(
            merged_decision.metadata.get("is_first_service_turn")
            or merged_decision.metadata.get("confirmation_override")
            or ticket_creation_ready
        )
        if ticket_creation_ready:
            merged_decision.metadata.setdefault("answer_first_guard_skipped_reason", "ticket_creation_ready")
        if not _skip_answer_first:
            merged_decision = await self._enforce_answer_first_policy(
                user_message=user_message,
                decision=merged_decision,
                context=context,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=target_service,
            )

        # ── RESUME PROMPT ────────────────────────────────────────────────────
        # After the current turn settles with no active pending, ask user ONCE
        # if they want to resume a suspended service.
        if (
            context.suspended_services
            and not context.resume_prompt_sent
            and not str(merged_decision.pending_action or "").strip()
        ):
            susp = context.suspended_services[0]
            susp_name = susp.get("service_name", "a previous request")
            merged_decision.response_text = (
                str(merged_decision.response_text or "").rstrip()
                + f"\n\nAlso, you were in the middle of **{susp_name}** — would you like to continue where you left off?"
            )
            context.resume_prompt_sent = True
        # ─────────────────────────────────────────────────────────────────────

        return await self._ensure_suggested_actions(
            user_message=user_message,
            context=context,
            decision=merged_decision,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service,
        )


llm_orchestration_service = LLMOrchestrationService()

