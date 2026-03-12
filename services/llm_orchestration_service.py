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
        kb_map = self._service_kb_by_service(capabilities_summary)
        extracted_kb_map = self._service_extracted_knowledge_by_service(capabilities_summary)
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
            required_slots = prompt_pack.get("required_slots", [])
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
                    "required_slots": normalized_slots,
                    "profile": str(prompt_pack.get("profile") or "").strip().lower(),
                    "service_prompt_role": str(prompt_pack.get("role") or "").strip()[:300],
                    "service_prompt_behavior": str(prompt_pack.get("professional_behavior") or "").strip()[:500],
                    "execution_guard": self._sanitize_json(execution_guard),
                    "confirmation_format": self._sanitize_json(confirmation_format),
                    "service_prompt_pack": self._sanitize_json(prompt_pack),
                    "knowledge_facts": kb_map.get(sid, [])[:15],
                    "extracted_knowledge": extracted_kb_map.get(sid, ""),
                    "confirmation_pending_action": (
                        "confirm_room_booking"
                        if str(prompt_pack.get("profile") or "").strip().lower() in {"room_booking", "stay_booking"}
                        else "confirm_booking"
                    ),
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
            "response_contract": {"suggested_actions": ["2-5 words action label"]},
        }
        system_prompt = (
            "You are a next-turn suggestion planner for concierge chat.\n"
            "Return STRICT JSON only.\n"
            "Generate 2 to 4 concise suggestion chips for likely next user replies.\n"
            "Prioritize context continuity from pending_action/history over generic options.\n"
            "If a confirmation is expected, include the exact confirmation phrase option.\n"
            "Keep labels short and guest-facing."
        )
        model = (
            str(getattr(settings, "chat_llm_next_suggestions_model", "") or "").strip()
            or str(getattr(settings, "llm_service_agent_model", "") or "").strip()
            or str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or None
        )
        try:
            raw = await llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=0.0,
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
        )
        model = (
            str(getattr(settings, "chat_llm_answer_first_guard_model", "") or "").strip()
            or str(getattr(settings, "llm_service_agent_model", "") or "").strip()
            or str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or None
        )
        temperature = float(getattr(settings, "chat_llm_answer_first_guard_temperature", 0.0) or 0.0)
        try:
            raw = await llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=temperature,
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
            "For room stay/availability/booking asks, prefer services whose profile or keywords indicate room booking/discovery.\n"
            "If the message is pure chit-chat/information not tied to a service, leave service_id empty.\n"
        )
        model = str(getattr(settings, "llm_orchestration_model", "") or "").strip() or None
        try:
            raw = await llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
                ],
                model=model,
                temperature=0.0,
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
        service_facts = service.get("knowledge_facts", [])
        if not isinstance(service_facts, list):
            service_facts = []
        extracted_knowledge = str(service.get("extracted_knowledge") or "").strip()
        required_slots = prompt_pack.get("required_slots", [])
        if not isinstance(required_slots, list):
            required_slots = []
        execution_guard = prompt_pack.get("execution_guard", {})
        if not isinstance(execution_guard, dict):
            execution_guard = {}
        behavior = str(prompt_pack.get("professional_behavior") or "").strip()
        service_profile = str(prompt_pack.get("profile") or "").strip().lower()
        role = str(prompt_pack.get("role") or "").strip() or f"You are the {service_name} service assistant."
        ticketing_policy = prompt_pack.get("ticketing_policy", {})
        if not isinstance(ticketing_policy, dict):
            ticketing_policy = {}
        ticketing_conditions = str(prompt_pack.get("ticketing_conditions") or "").strip()

        model = str(getattr(settings, "llm_service_agent_model", "") or "").strip() or None
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()
        confirmation_pending_action = (
            "confirm_room_booking"
            if service_profile in {"room_booking", "stay_booking"}
            else "confirm_booking"
        )
        requires_confirmation_step = bool(
            service_profile in {
                "room_booking", "stay_booking", "general_booking",
                "transport_request", "appointment_booking",
                "food_order", "service_request",
            }
        )
        # Transport-profile note: drop-off defaults to the hotel
        transport_note = ""
        if service_profile == "transport_request":
            transport_note = (
                "Transport constraints:\n"
                "- The drop-off location is ALWAYS this hotel (ICONIQA). Do NOT ask for drop location — set drop_location='ICONIQA Hotel' automatically in pending_data_updates.\n"
                "- VEHICLE: Only offer vehicles explicitly listed in service_knowledge or service_grounding.full_kb_text (e.g. Toyota Innova Hycross). "
                "If the guest requests a different vehicle type (e.g. Mercedes, BMW), politely inform them that only the listed vehicle is available and confirm if they would like to proceed with that.\n"
                "- PRICING: Use pricing from service_knowledge or full_kb_text only. Never invent prices.\n"
            )

        confirmation_flow_block = ""
        if requires_confirmation_step:
            confirmation_flow_block = (
                "Execution flow:\n"
                f"- This service requires explicit confirmation before execution. After all required slots are collected, set pending_action='{confirmation_pending_action}'.\n"
                "- BEFORE setting pending_action to confirm: present a complete summary of all booking details, then ask the guest to confirm.\n"
                f"- Ask the guest to reply with '{confirmation_phrase}' to confirm their booking.\n"
                "- Do not clear pending_action immediately after collecting the final required slot.\n"
                f"- CRITICAL: When pending_action='{confirmation_pending_action}' AND the user message matches '{confirmation_phrase}' exactly, "
                "you MUST set action=create_ticket, ticket.required=true, ticket.ready_to_create=true, pending_action=null. "
                "Do NOT just say confirmed — the ticket must be created at this point.\n"
            )
        history_preview = self._history_preview(
            context,
            max_messages=max(20, int(getattr(settings, "llm_orchestration_history_messages", 20) or 20)),
            max_chars=max(12000, int(getattr(settings, "llm_orchestration_history_chars", 12000) or 12000)),
        )
        pending_pub_pre = self._coerce_public_pending(context.pending_data)
        is_first_service_turn = not any(
            str(pending_pub_pre.get(s.get("id") or "") or "").strip()
            for s in (required_slots if isinstance(required_slots, list) else [])
            if isinstance(s, dict) and s.get("id")
        )
        service_grounding = await self._build_service_grounding_pack(
            user_message=user_message,
            context=context,
            service=service,
            service_profile=service_profile,
            is_first_service_turn=is_first_service_turn,
        )
        system_prompt = (
            "You are a service-level conversation agent. Return STRICT JSON only.\n"
            "\n"
            "BEFORE RESPONDING: Read the entire `history` array from oldest to newest. "
            "Use it to resolve references ('this', 'that', 'it', 'book this', 'same one') by checking what the last assistant message discussed. "
            "Use `pending_data_public` to see what slots are already collected. "
            "Use `memory_facts` and `memory_summary` for guest context. Never ignore prior context.\n"
            "\n"
            "RULE 1 — COLLECT ALL MISSING SLOTS AT ONCE: Do NOT ask one question at a time. "
            "After the service intro, ask for ALL missing required slots in a single message. "
            "List them naturally in one question (e.g. 'Could you share your check-in date, check-out date, number of guests, and room preference?'). "
            "If the guest only answers some slots, collect those, then ask for ALL remaining missing slots together in one follow-up. "
            "Exception: if the service has a single missing slot, ask just that one.\n"
            "\n"
            "RULE 2 — ALWAYS SET pending_action WHEN COLLECTING: When any required slot is still missing, you MUST set action=collect_info AND set pending_action to a descriptive string (e.g. 'collect_booking_details'). Never leave pending_action null while required slots are still being collected. This is critical for routing.\n"
            "\n"
            "RULE 3 — SERVICE INTRO: When `is_first_service_turn=true`, START your response with a brief overview using ACTUAL DETAILS from `service.service_knowledge` or `service_grounding.full_kb_text` (specific options, prices, inclusions). Then ask ALL missing required slots in the same message (RULE 1). Exception: only skip intro for pure issue/maintenance/complaint services (profile=issue_resolution) — food, booking, and service_request profiles MUST include intro.\n"
            "\n"
            "RULE 4 — SUMMARY BEFORE CONFIRMATION: Before setting pending_action to a confirm_* value, you MUST present a complete summary of all collected booking details (all slot values), then ask the guest to confirm. Format: 'Here is your [service] summary: [list all details]. Please reply with \\'yes confirm\\' to proceed.'\n"
            "\n"
            "RULE 5 — TICKET ISSUE REQUIRED: When setting ticket.ready_to_create=true, also set ticket.issue to a human-readable description (e.g. 'Room booking: Premier Twin, 2 guests, check-in Feb 3, check-out Feb 5'). Never empty.\n"
            "\n"
            "RULE 6 — STRICT GROUNDING: NEVER agree to, confirm, or arrange any vehicle type, room type, service, price, or amenity NOT explicitly listed in service_knowledge or service_grounding.full_kb_text. "
            "If a guest requests something unavailable (e.g. a vehicle type not in the KB), politely correct them: state only what IS available from the KB. Do not invent or agree to anything outside the KB.\n"
            "\n"
            "Context continuity:\n"
            "- ALWAYS read the full history. Resolve references like 'this', 'that', 'book this' from the last assistant message.\n"
            "- Use history + pending_action to interpret short replies ('yes', '21 march', '3 guests', etc.).\n"
            "- Do not treat a generic 'yes' as the confirmation phrase unless it exactly matches.\n"
            "- NEVER say you cannot help with something clearly within your service scope.\n"
            "\n"
            "Grounding:\n"
            "- service.service_knowledge is your PRIMARY source. Use it directly. Do not invent facts.\n"
            "- service_grounding.full_kb_text contains the FULL knowledge base. Use it when service_knowledge does not cover the query.\n"
            "\n"
            "Slot collection:\n"
            "- service.required_slots defines what must be collected. Check pending_data_public for already-collected slots.\n"
            "- When a user provides slot values, save them in pending_data_updates using the slot's exact 'id' as the key.\n"
            "- ALL required slots (required=true) MUST be collected before moving to confirmation.\n"
            "- Ask for ALL still-missing required slots together in one message (RULE 1).\n"
            "\n"
            "Missing fields:\n"
            "- missing_fields = only fields blocking this specific turn.\n"
            "- deferrable_fields = needed later but not now.\n"
            "- If only deferrable fields remain, use action=respond_only, pending_action=null.\n"
            "\n"
            "Phase guardrail:\n"
            "- Execute only when selected_phase.id == service.phase_id.\n"
            "- If phase mismatch: action=respond_only, use_handler=false, pending_action=null, ticket.required=false.\n"
            "\n"
            "Ticketing:\n"
            "- service.ticketing_enabled=true means a ticket MUST be created to execute the service request.\n"
            "- Set action=create_ticket, ticket.required=true, ticket.ready_to_create=true, AND ticket.issue=<description> when confirmed.\n"
            f"- Confirmation phrase is exactly: '{confirmation_phrase}'\n"
            f"{('- Ticket trigger condition: ' + ticketing_conditions + chr(10)) if ticketing_conditions else ''}"
            f"{transport_note}"
            f"{confirmation_flow_block}"
        )
        pending_pub = self._coerce_public_pending(context.pending_data)
        payload = {
            "service": {
                "id": service_id,
                "name": service_name,
                "type": str(service.get("type") or "").strip(),
                "profile": service_profile,
                "phase_id": self._normalize_identifier(service.get("phase_id")),
                "phase_name": str(service.get("phase_name") or "").strip(),
                "ticketing_enabled": bool(service.get("ticketing_enabled", True)),
                "ticketing_policy": str(service.get("ticketing_policy") or ticketing_policy.get("policy") or "").strip(),
                "role": role,
                "professional_behavior": behavior,
                "required_slots": self._sanitize_json(required_slots),
                "execution_guard": self._sanitize_json(execution_guard),
                "requires_confirmation_step": requires_confirmation_step,
                "confirmation_pending_action": confirmation_pending_action if requires_confirmation_step else "",
                "service_knowledge": extracted_knowledge or None,
                "knowledge_facts": self._sanitize_json(service_facts[:20]),
            },
            "is_first_service_turn": is_first_service_turn,
            "selected_phase": {
                "id": selected_phase_id,
                "name": selected_phase_name,
            },
            "phase_policy": {
                "service_phase_id": self._normalize_identifier(service.get("phase_id")),
                "service_phase_name": str(service.get("phase_name") or "").strip(),
                "is_service_allowed_in_selected_phase": (
                    self._normalize_identifier(service.get("phase_id")) == self._normalize_identifier(selected_phase_id)
                ),
            },
            "orchestrator_decision": self._sanitize_json(base_decision.model_dump(mode="json")),
            "current_state": context.state.value,
            "pending_action": str(context.pending_action or ""),
            "pending_data_public": self._sanitize_json(self._coerce_public_pending(context.pending_data)),
            "history": history_preview,
            "memory_summary": str(memory_snapshot.get("summary") or "")[:1200],
            "memory_facts": self._sanitize_json(memory_snapshot.get("facts", {})),
            "confirmation_phrase": confirmation_phrase,
            "service_grounding": self._sanitize_json(service_grounding),
            "user_message": str(user_message or ""),
            "response_contract": {
                "normalized_query": "string",
                "intent": "core intent",
                "confidence": "0..1",
                "action": "respond_only|collect_info|dispatch_handler|create_ticket|resume_pending|cancel_pending",
                "target_service_id": service_id,
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
        raw = await llm_client.chat_with_json(messages, model=model, temperature=0.0)
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
        user_compact = re.sub(r"\s+", " ", str(user_message or "").strip().lower())
        confirmation_compact = re.sub(r"\s+", " ", confirmation_phrase.lower())
        awaiting_confirmation = str(context.pending_action or "").strip().lower().startswith("confirm_")

        # Deterministic confirmation override — if the user said the confirmation phrase
        # while we were in a confirm_* pending state (or last bot message asked for it),
        # force ticket creation regardless of what the LLM returned.
        last_bot_asked_confirmation = False
        for _msg in reversed(context.messages):
            _role = str(getattr(_msg, "role", "") or "").lower()
            if "assistant" in _role:
                _content = str(getattr(_msg, "content", "") or "").lower()
                # Match if last bot message asked for confirmation (contains "confirm" broadly)
                if "confirm" in _content or confirmation_compact in _content:
                    last_bot_asked_confirmation = True
                break

        if (awaiting_confirmation or last_bot_asked_confirmation) and user_compact == confirmation_compact:
            pending_pub = self._coerce_public_pending(context.pending_data)
            room_type = str(
                pending_pub.get("room_type") or pending_pub.get("room_name") or ""
            ).strip()
            checkin = str(
                pending_pub.get("check_in_date") or pending_pub.get("checkin_date") or
                pending_pub.get("stay_checkin_date") or ""
            ).strip()
            checkout = str(
                pending_pub.get("check_out_date") or pending_pub.get("checkout_date") or
                pending_pub.get("stay_checkout_date") or ""
            ).strip()
            guests = str(pending_pub.get("guest_count") or pending_pub.get("guests") or "").strip()
            issue_parts = [service_name]
            if room_type:
                issue_parts.append(room_type)
            if checkin:
                issue_parts.append(f"check-in {checkin}")
            if checkout:
                issue_parts.append(f"check-out {checkout}")
            if guests:
                issue_parts.append(f"{guests} guests")
            auto_issue = ", ".join(issue_parts)
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
            requires_confirmation_step
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
        decision.metadata.setdefault("service_grounding_used", bool((service_grounding or {}).get("snippets")))
        if (service_grounding or {}).get("room_types_catalog"):
            decision.metadata.setdefault("room_types_catalog", list((service_grounding or {}).get("room_types_catalog")[:16]))

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
    ) -> OrchestrationDecision | None:
        orchestration_mode_enabled = bool(getattr(settings, "chat_llm_orchestration_mode", False))
        no_template_mode_enabled = bool(getattr(settings, "chat_no_template_response_mode", False))
        if not orchestration_mode_enabled and not no_template_mode_enabled:
            return None
        if not str(settings.openai_api_key or "").strip():
            return None

        # ── RESUME PROMPT HANDLER ────────────────────────────────────────────
        # If last turn asked user to resume a suspended service, evaluate reply.
        if context.resume_prompt_sent:
            context.resume_prompt_sent = False
            user_lower = re.sub(r"\s+", " ", (user_message or "").strip().lower())
            affirmative = any(w in user_lower for w in (
                "yes", "yeah", "yep", "sure", "ok", "okay", "continue",
                "go ahead", "proceed", "resume", "yea",
            ))
            if affirmative and context.suspended_services:
                suspended = context.suspended_services.pop(0)
                # Restore the pending state for the suspended service
                context.pending_action = suspended.get("pending_action") or None
                restored_pub = dict(suspended.get("pending_data") or {})
                restored_pub["service_id"] = suspended.get("service_id", "")
                _internal = {k: v for k, v in context.pending_data.items() if k.startswith("_")}
                context.pending_data = {**restored_pub, **_internal}
                # Rewrite user_message so orchestrator routes correctly
                user_message = f"[RESUME {suspended.get('service_name', 'previous request')}] {user_message}"
            else:
                # User ignored or denied — kill all suspended tasks
                context.suspended_services = []
        # ─────────────────────────────────────────────────────────────────────

        # ── STICKY SERVICE SHORT-CIRCUIT ─────────────────────────────────────
        # If a service is actively mid-collection (pending_action set + service_id
        # known), skip the orchestrator LLM entirely and go straight to the
        # service agent. This prevents short replies like "22", "yes", "no",
        # "book this" from being mis-routed by the orchestrator.
        _pub = self._coerce_public_pending(context.pending_data)
        _active_service_id = self._normalize_identifier(_pub.get("service_id") or "")
        _has_pending = bool(str(context.pending_action or "").strip())
        if _active_service_id and _has_pending:
            selected_phase_id, selected_phase_name = self._resolve_selected_phase(
                context=context,
                selected_phase_context=selected_phase_context,
                capabilities_summary=capabilities_summary,
            )
            services_snapshot_sc = self._service_snapshot(capabilities_summary)
            target_service_sc = None
            for _svc in services_snapshot_sc:
                if self._normalize_identifier(_svc.get("id")) == _active_service_id:
                    target_service_sc = _svc
                    break
            if target_service_sc:
                base_decision_sc = OrchestrationDecision(
                    intent="continue_service",
                    action="collect_info",
                    target_service_id=_active_service_id,
                    confidence=0.99,
                )
                base_decision_sc.metadata["source"] = "sticky_service"
                service_decision_sc = await self._run_service_agent(
                    user_message=user_message,
                    context=context,
                    memory_snapshot=memory_snapshot,
                    service=target_service_sc,
                    base_decision=base_decision_sc,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                )
                # Resume prompt check after sticky turn
                if (
                    context.suspended_services
                    and not context.resume_prompt_sent
                    and not str(service_decision_sc.pending_action or "").strip()
                ):
                    susp = context.suspended_services[0]
                    service_decision_sc.response_text = (
                        str(service_decision_sc.response_text or "").rstrip()
                        + f"\n\nAlso, you were in the middle of **{susp.get('service_name', 'a previous request')}** — would you like to continue where you left off?"
                    )
                    context.resume_prompt_sent = True
                return await self._ensure_suggested_actions(
                    user_message=user_message,
                    context=context,
                    decision=service_decision_sc,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                    target_service=target_service_sc,
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
                }
            )
            if len(out_of_phase_services) >= 120:
                break
        allowed_services_detailed = [
            {
                "id": self._normalize_identifier(row.get("id")),
                "name": str(row.get("name") or "").strip(),
                "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                "phase_id": self._normalize_identifier(row.get("phase_id")),
                "phase_name": str(row.get("phase_name") or "").strip(),
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
                "availability_hint": self._phase_transition_timing_hint(
                    selected_phase_id,
                    str(row.get("phase_id") or "").strip(),
                ),
            }
            for row in out_of_phase_services
            if isinstance(row, dict)
        ][:160]
        full_kb_text = config_service.get_full_kb_text(max_chars=60_000)
        payload = {
            "trace_id": orchestration_trace_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "user_message": str(user_message or ""),
            "state": context.state.value,
            "pending_action": str(context.pending_action or ""),
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
            "history": self._history_preview(
                context,
                max_messages=max(8, int(getattr(settings, "llm_orchestration_history_messages", 12) or 12)),
                max_chars=max(1800, int(getattr(settings, "llm_orchestration_history_chars", 8000) or 8000)),
            ),
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
            "You are the top-level conversation orchestrator for a multi-service concierge assistant.\n"
            "Your output controls routing and action selection.\n"
            "\n"
            "BEFORE DOING ANYTHING: Read the full `history` array from oldest to newest. Resolve pronouns and references "
            "('this', 'that', 'it', 'same', 'book this') using the last assistant message. Only then decide intent and routing.\n"
            "\n"
            "You have access to the full_knowledge_base field which contains the complete property knowledge base.\n"
            "Use it when answering informational questions or when service routing requires full context.\n"
            "Use only provided JSON context, never invent unsupported services.\n"
            "\n"
            "Rules:\n"
            "1) SERVICE-FIRST routing: decide target_service_id first; intent is secondary metadata.\n"
            "2) target_service_id must exactly match one service.id when service work is needed.\n"
            "3) CONTEXT CONTINUITY — CRITICAL: If pending_action is set (e.g. 'collect_checkin_date', 'collect_guest_count', 'confirm_room_booking'), the user is mid-flow. You MUST route to the same service that owns the pending flow. To identify which service owns the pending flow, match pending_action against each service's confirmation_pending_action field in the services list, or use conversation history. Do NOT re-route to a different service or generate a generic response just because the user message is short (a date, a number, 'yes confirm', 'no').\n"
            "4) STICKY SERVICE: If pending_data_public contains a service_id, that is the currently active service. Stay on it unless the user explicitly and clearly asks for something completely different.\n"
            "5) First classify requested service against policy_snapshot.phase_service_policy.\n"
            "5.1) For out-of-phase asks, set action=respond_only, use_handler=false, pending_action=null,\n"
            "     ticket.required=false, and explain availability phase in response_text.\n"
            "5.2) For in-phase asks, choose target_service_id from allowed_service_ids_in_current_phase and continue flow.\n"
            "5.3) Never propose execution for services in blocked_out_of_phase_services.\n"
            "6) Set interrupt_pending=true only when user CLEARLY switches to a completely different topic (not short answers, not clarifications).\n"
            "7) Set resume_pending=true only when user explicitly asks to continue a previous pending flow.\n"
            "8) Answer-first rule: always answer the user's current ask first when possible from context.\n"
            "   missing_fields must contain only fields blocking the current ask right now.\n"
            "   If details are needed later (not now), put them in deferrable_fields and keep action=respond_only.\n"
            "9) Ticketing: set action=create_ticket only when ticket.required=true AND ticket.ready_to_create=true.\n"
            "   Keep ticket.ready_to_create=false while collecting missing details or waiting confirmation.\n"
            "10) Set ticket.required=true only for actionable staff follow-up requests.\n"
            "11) Operational issue reports (e.g., cockroach, dirty room, broken AC, no water, maintenance/housekeeping problems)\n"
            "    must not be treated as FAQ. Use complaint/human_request style routing and response.\n"
            "12) Keep response_text natural, concise, and phase-aware.\n"
            "13) Use policy_snapshot as hard runtime boundaries.\n"
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
        raw = await llm_client.chat_with_json(messages, model=model, temperature=0.0)
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

        # ── SUSPEND DETECTION ────────────────────────────────────────────────
        # If routing to a NEW service while another service has partial data,
        # save the old service to suspended_services instead of discarding it.
        current_active_service = self._normalize_identifier(
            self._coerce_public_pending(context.pending_data).get("service_id") or ""
        )
        if (
            current_active_service
            and target_service_id
            and current_active_service != target_service_id
            and context.pending_action  # mid-collection signal
        ):
            pub = self._coerce_public_pending(context.pending_data)
            has_slots = any(
                v for k, v in pub.items()
                if k != "service_id" and str(v or "").strip()
            )
            if has_slots:
                susp_name = current_active_service
                for row in services_snapshot:
                    if self._normalize_identifier(row.get("id")) == current_active_service:
                        susp_name = str(row.get("name") or current_active_service).strip()
                        break
                context.suspended_services.append({
                    "service_id": current_active_service,
                    "service_name": susp_name,
                    "pending_data": dict(pub),
                    "pending_action": context.pending_action,
                })
                # Clear current service state — new service starts fresh
                _internal = {k: v for k, v in context.pending_data.items() if k.startswith("_")}
                context.pending_data = _internal
                context.pending_action = None
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
        merged_decision = self._merge_decisions(decision, service_decision)
        # Skip answer-first enforcement for first-turn intros (it overwrites the room list)
        # and for confirmation overrides (already deterministically correct)
        _skip_answer_first = bool(
            merged_decision.metadata.get("is_first_service_turn")
            or merged_decision.metadata.get("confirmation_override")
        )
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
