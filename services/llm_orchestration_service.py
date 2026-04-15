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
from services.everything_trace_service import everything_trace_service
from services.form_mode_service import (
    canonicalize_trigger_pending_data,
    normalize_trigger_missing_fields,
    resolve_trigger_field_value,
    strip_form_confirmation_instructions,
)

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

# ── Structured Decision Logger ────────────────────────────────────────────────
_decision_log_dir = os.path.join(_log_dir, "decisions")
os.makedirs(_decision_log_dir, exist_ok=True)
_decision_logger = logging.getLogger("orchestration_decisions")
if not _decision_logger.handlers:
    _dfh = logging.FileHandler(os.path.join(_decision_log_dir, "decisions.jsonl"), encoding="utf-8")
    _dfh.setFormatter(logging.Formatter("%(message)s"))
    _decision_logger.addHandler(_dfh)
    _decision_logger.setLevel(logging.DEBUG)
    _decision_logger.propagate = False

logger = logging.getLogger(__name__)


def _log_decision(record: dict) -> None:
    """Append a structured decision record as one JSON line."""
    try:
        _decision_logger.debug(json.dumps(record, ensure_ascii=False, default=str))
        from services.turn_diagnostics_service import turn_diagnostics_service

        turn_diagnostics_service.log_orchestration_decision(record)
        everything_trace_service.log_event(
            "orchestration_decision",
            {"record": record},
            session_id=str((record or {}).get("session_id") or ""),
            component="services.llm_orchestration_service",
        )
    except Exception:
        pass


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
        everything_trace_service.log_event(
            "llm_orchestration_call",
            {
                "label": str(label or "").strip(),
                "user_message": str(user_message or ""),
                "system_prompt": str(system_prompt or ""),
                "payload": payload,
                "response": response,
            },
            session_id=str(session_id or ""),
            component="services.llm_orchestration_service",
        )
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

    @staticmethod
    def _build_booking_context_prompt_block(context: Any) -> str:
        """Build a reusable prompt block with booking context + phase behavioral rules.
        Appended to orchestrator, service router, service agent, and suggestion prompts."""
        lines: list[str] = []

        # --- Booking context ---
        if getattr(context, "booking_id", None):
            lines.append("\n=== ACTIVE BOOKING CONTEXT ===")
            if getattr(context, "booking_guest_name", None):
                lines.append(f"Guest Name: {context.booking_guest_name}")
            if getattr(context, "room_number", None):
                lines.append(f"Room Number: {context.room_number}")
            if getattr(context, "booking_room_type", None):
                lines.append(f"Room Type: {context.booking_room_type}")
            if getattr(context, "booking_property_name", None):
                lines.append(f"Property: {context.booking_property_name}")
            if getattr(context, "booking_confirmation_code", None):
                lines.append(f"Confirmation Code: {context.booking_confirmation_code}")
            _bk_ci = getattr(context, "booking_check_in_date", None) or ""
            _bk_co = getattr(context, "booking_check_out_date", None) or ""
            if _bk_ci and _bk_co:
                lines.append(f"Stay: {_bk_ci} to {_bk_co}")
                lines.append(
                    "IMPORTANT: Any date the guest selects for services (spa, dining, transport, etc.) "
                    f"MUST fall within their stay dates ({_bk_ci} to {_bk_co}). "
                    "If they request a date outside this window, politely inform them and ask for a valid date."
                )
            lines.append(
                "NAME USAGE RULE (strict): Do NOT begin every reply with 'Hi {name}!' or any name-led "
                "greeting — that reads robotic, not human. Use the guest's first name ONLY when it "
                "genuinely adds value: the very first greeting of the session, a personal confirmation "
                "('Done, {name} — booked for 3 PM.'), apologising, or acknowledging something sensitive. "
                "For ordinary informational answers, follow-up replies, and mid-conversation turns, do "
                "NOT use their name at all. Aim for AT MOST one name mention every several turns. "
                "Humans don't repeat each other's names in every message — mirror that. "
                "Reference room number and property only when it actually helps the answer. "
                "NEVER include raw booking metadata (guest ID, confirmation codes, phone/email) in "
                "response text unless the guest explicitly asks for it."
            )

            lines.append(
                "TIMING QUESTION RULE: When the guest asks WHEN they can check in / check out, "
                "what time their stay starts/ends, or any question combining a time-of-day policy "
                "with their specific stay, ALWAYS combine the guest's specific check-in/check-out DATE "
                "(from this booking context) with the property's check-in/check-out TIME (from KB policy). "
                "Never give only the time or only the date when both are available in context — "
                "ground the policy time on the guest's actual stay date."
            )

            lines.append(
                "SPECIFICITY RULE: When ACTIVE BOOKING CONTEXT exists, interpret the guest's message through "
                "that context first. In pre_checkin, during_stay, and post_checkout, default to the guest's "
                "own reservation or stay when the message is ambiguous. Prefer the most specific grounded answer "
                "available, using this priority order: active booking context, booked-property context, general "
                "property policy, then broad fallback knowledge. If active booking context provides a specific "
                "date, room type, property, or stay detail, do not replace it with a generic policy answer. "
                "If both personal stay data and general property policy are relevant, combine them naturally in "
                "one answer. Only answer generically when the guest is clearly asking a general question, asking "
                "hypothetically, asking about another property, or when no stay-specific detail is available. "
                "Before finalizing the reply, silently check whether active booking context makes the answer more "
                "personal, more specific, or more accurate. If it does, use it."
            )

        # --- Phase behavioral rules ---
        phase = str(getattr(context, "booking_phase", None) or "").strip()
        if phase:
            lines.append(f"\n=== PHASE RULES ({phase}) ===")
            if phase == "pre_checkin":
                lines.append(
                    "The guest has a confirmed booking but has NOT checked in yet.\n"
                    "Only services assigned to the pre_checkin phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "If the guest asks for something only available during their stay, phrase it naturally: "
                    "'We can arrange that once you arrive' or 'This will be available during your stay'."
                )
            elif phase == "during_stay":
                lines.append(
                    "The guest is currently checked in and staying at the property.\n"
                    "Only services assigned to the during_stay phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "Any service scheduling must fall within their stay dates.\n"
                    "Be proactive: offer relevant available services based on their room and property."
                )
            elif phase == "post_checkout":
                lines.append(
                    "The guest has already checked out.\n"
                    "Only services assigned to the post_checkout phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "If the guest asks for in-stay services, phrase naturally: "
                    "'Since you have already checked out, I can help with [available services] "
                    "or planning your next visit'."
                )
            elif phase == "pre_booking":
                lines.append(
                    "The guest is exploring options and has no confirmed booking yet.\n"
                    "Only services assigned to the pre_booking phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "Goal: help the guest find the right option and complete a booking."
                )

        return "\n".join(lines)

    def _normalize_identifier(self, value: Any) -> str:
        return str(value or "").strip().lower().replace(" ", "_")

    @classmethod
    def _normalize_phase_identifier(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "_")
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

    @classmethod
    def _is_stay_property_pin_phase(cls, phase_id: Any) -> bool:
        return cls._normalize_phase_identifier(phase_id) in {
            "pre_checkin",
            "during_stay",
            "post_checkout",
        }

    @staticmethod
    def _normalize_property_id_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    def _apply_stay_property_pin(
        self,
        *,
        context: ConversationContext,
        selected_phase_id: str,
        property_ids: list[str],
        source: str,
    ) -> tuple[list[str], bool]:
        normalized_ids = self._normalize_property_id_list(property_ids)
        if not self._is_stay_property_pin_phase(selected_phase_id):
            return normalized_ids, False

        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        stay_property_id = str(pending.get("_stay_property_id") or "").strip()
        stay_property_name = str(
            pending.get("_stay_property_name")
            or getattr(context, "booking_property_name", "")
            or ""
        ).strip()
        if not stay_property_id:
            return normalized_ids, False
        if stay_property_id in normalized_ids:
            return normalized_ids, False

        pinned_ids = [stay_property_id, *normalized_ids]
        logger.info(
            "Stay property pin applied (session_id=%s phase=%s source=%s stay_property_id=%s incoming_ids=%s)",
            str(getattr(context, "session_id", "") or "").strip(),
            self._normalize_phase_identifier(selected_phase_id),
            str(source or "").strip(),
            stay_property_id,
            normalized_ids,
        )
        if isinstance(context.pending_data, dict):
            context.pending_data["_stay_property_name"] = stay_property_name or stay_property_id
            context.pending_data["_stay_property_pin_last_source"] = str(source or "").strip()
        return pinned_ids, True

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

    def _normalize_ticket_evidence(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = " ".join(str(item or "").strip().split())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text[:280])
            if len(normalized) >= 4:
                break
        return normalized

    def _generic_kb_ticketing_enabled(self) -> bool:
        return bool(getattr(settings, "chat_llm_generic_kb_ticketing_enabled", True))

    def _is_generic_kb_request(self, decision: OrchestrationDecision | None) -> bool:
        if not isinstance(decision, OrchestrationDecision):
            return False
        if not self._generic_kb_ticketing_enabled():
            return False
        if bool(getattr(decision.ticket, "generic_request", False)):
            return True
        metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
        return self._coerce_bool(
            metadata.get("generic_kb_request") or metadata.get("kb_grounded_request"),
            default=False,
        )

    def _normalize_generic_kb_request_decision(
        self,
        decision: OrchestrationDecision,
    ) -> OrchestrationDecision:
        if not self._is_generic_kb_request(decision):
            return decision

        if not isinstance(decision.metadata, dict):
            decision.metadata = {}
        decision.ticket.generic_request = True
        decision.metadata["generic_kb_request"] = True

        evidence = self._normalize_ticket_evidence(
            decision.ticket.evidence
            or decision.metadata.get("generic_kb_evidence")
            or decision.metadata.get("kb_evidence")
        )
        if evidence:
            decision.ticket.evidence = list(evidence)
            decision.metadata["generic_kb_evidence"] = list(evidence)

        if decision.ticket.phase_applicable is None:
            if "generic_kb_phase_applicable" in decision.metadata:
                decision.ticket.phase_applicable = self._coerce_bool(
                    decision.metadata.get("generic_kb_phase_applicable"),
                    default=False,
                )
            elif "kb_phase_applicable" in decision.metadata:
                decision.ticket.phase_applicable = self._coerce_bool(
                    decision.metadata.get("kb_phase_applicable"),
                    default=False,
                )
        if decision.ticket.phase_applicable is not None:
            decision.metadata["generic_kb_phase_applicable"] = bool(decision.ticket.phase_applicable)
        if bool(decision.ticket.approval_required):
            decision.metadata["generic_kb_approval_required"] = True

        if not str(decision.ticket.category or "").strip():
            decision.ticket.category = "request"
        if not str(decision.ticket.sub_category or "").strip():
            decision.ticket.sub_category = "generic_kb_request"

        if not isinstance(decision.pending_data_updates, dict):
            decision.pending_data_updates = {}
        decision.pending_data_updates.setdefault("generic_kb_request", True)
        if decision.ticket.phase_applicable is not None:
            decision.pending_data_updates.setdefault(
                "generic_kb_phase_applicable",
                bool(decision.ticket.phase_applicable),
            )
        if bool(decision.ticket.approval_required):
            decision.pending_data_updates.setdefault("generic_kb_approval_required", True)
        if decision.ticket.evidence:
            decision.pending_data_updates.setdefault("generic_kb_evidence", list(decision.ticket.evidence))
        if str(decision.ticket.sub_category or "").strip():
            decision.pending_data_updates.setdefault("ticket_sub_category", decision.ticket.sub_category)
        if str(decision.ticket.issue or "").strip():
            decision.pending_data_updates.setdefault("generic_kb_issue", str(decision.ticket.issue).strip())
        if str(decision.ticket.reason or "").strip():
            decision.pending_data_updates.setdefault("generic_kb_reason", str(decision.ticket.reason).strip())

        action = str(decision.action or "").strip().lower()
        if action == "collect_info" and not str(decision.pending_action or "").strip():
            if bool(decision.ticket.required) and not bool(decision.missing_fields) and not bool(decision.ticket.ready_to_create):
                decision.pending_action = "confirm_generic_kb_request"
            else:
                decision.pending_action = "collect_generic_kb_request"
        if action == "create_ticket" and bool(decision.ticket.ready_to_create):
            decision.pending_action = None

        return decision

    @staticmethod
    def _split_kb_text_for_llm(*, kb_text: str, chunk_chars: int) -> list[str]:
        text = str(kb_text or "").strip()
        if not text:
            return []
        target = max(4000, int(chunk_chars or 18000))
        if len(text) <= target:
            return [text]

        chunks: list[str] = []
        current_lines: list[str] = []
        current_chars = 0
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            line_len = len(line) + 1
            if current_lines and current_chars + line_len > target:
                chunks.append("\n".join(current_lines).strip())
                current_lines = []
                current_chars = 0
            current_lines.append(line)
            current_chars += line_len
        if current_lines:
            chunks.append("\n".join(current_lines).strip())
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _build_kb_source_manifest(kb_text: str, *, max_sources: int = 120) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"^=== SOURCE: (.+?) ===$", str(kb_text or ""), flags=re.MULTILINE):
            name = str(match.group(1) or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= max_sources:
                break
        return names

    @staticmethod
    def _build_property_scope_history_text(
        *,
        context: ConversationContext,
        max_messages: int = 12,
        max_chars: int = 5000,
    ) -> str:
        messages = getattr(context, "messages", []) or []
        parts: list[str] = []
        for item in messages[-max_messages:] if isinstance(messages, list) else []:
            if isinstance(item, dict):
                role = str(item.get("role") or "").strip().lower()
                content = str(item.get("content") or "").strip()
            else:
                role = str(getattr(item, "role", "") or "").strip().lower()
                content = str(getattr(item, "content", "") or "").strip()
            if not content:
                continue
            parts.append(f"{role or 'message'}: {content}")
        history_text = "\n".join(parts).strip()
        if max_chars > 0 and len(history_text) > max_chars:
            history_text = history_text[-max_chars:]
        return history_text

    async def _prepare_full_kb_context_for_message(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        full_kb_text: str,
    ) -> dict[str, Any]:
        raw_text = str(full_kb_text or "").strip()
        if not raw_text:
            return {
                "full_kb_text": "",
                "property_scope": {
                    "mode": "unscoped",
                    "active_property_id": "",
                    "active_property_name": "",
                    "matched_property_ids": [],
                    "history_property_ids": [],
                    "selected_property_ids": [],
                    "property_manifest": [],
                    "expected_property_count": 0,
                    "detected_property_count": 0,
                    "count_mismatch": False,
                    "requires_clarification": False,
                    "clarification_question": "",
                },
            }

        inline_budget = max(
            12000,
            int(getattr(settings, "full_kb_llm_max_kb_chars", 180000) or 180000),
        )
        property_scope: dict[str, Any] = {
            "mode": "unscoped",
            "active_property_id": "",
            "active_property_name": "",
            "matched_property_ids": [],
            "history_property_ids": [],
            "selected_property_ids": [],
            "property_manifest": [],
            "expected_property_count": 0,
            "detected_property_count": 0,
            "count_mismatch": False,
            "requires_clarification": False,
            "clarification_question": "",
        }

        service_knowledge_block = ""
        kb_without_service_block = raw_text
        service_marker = "=== SERVICE KNOWLEDGE BASE ==="
        if service_marker in raw_text:
            prefix, _sep, suffix = raw_text.partition(service_marker)
            kb_without_service_block = prefix.strip()
            service_knowledge_block = f"{service_marker}\n{suffix.strip()}".strip()

        history_text = self._build_property_scope_history_text(context=context)
        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        seed_property_id = self._normalize_identifier(
            pending_raw.get("_selected_property_scope_id")
            or pending_raw.get("selected_property_id")
            or ""
        )
        try:
            property_scope = await config_service.build_property_scoped_kb_context(
                user_message=user_message,
                history_text=history_text,
                seed_property_id=seed_property_id,
                max_sources=200,
                max_scope_chars=max(inline_budget, 90000),
                preloaded_full_kb_text=raw_text,
            )
        except Exception:
            property_scope = dict(property_scope)

        prepared_text = str(
            (property_scope.get("scoped_full_kb_text") if isinstance(property_scope, dict) else "")
            or kb_without_service_block
            or raw_text
        ).strip()
        if service_knowledge_block:
            prepared_text = (
                f"{prepared_text}\n\n{service_knowledge_block}".strip()
                if prepared_text
                else service_knowledge_block
            )

        if len(prepared_text) <= inline_budget:
            return {
                "full_kb_text": prepared_text,
                "property_scope": property_scope,
            }

        if not bool(getattr(settings, "full_kb_llm_chunk_scan_enabled", True)):
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }
        if not str(settings.openai_api_key or "").strip():
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }

        max_scan_chunks = max(
            1,
            int(getattr(settings, "full_kb_llm_chunk_max_chunks", 24) or 24),
        )
        chunk_chars = max(
            4000,
            int(getattr(settings, "full_kb_llm_chunk_chars", 18000) or 18000),
        )
        if len(prepared_text) > chunk_chars * max_scan_chunks:
            chunk_chars = max(chunk_chars, (len(prepared_text) + max_scan_chunks - 1) // max_scan_chunks)
        chunks = self._split_kb_text_for_llm(kb_text=prepared_text, chunk_chars=chunk_chars)
        if not chunks:
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }

        model = (
            str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or str(getattr(settings, "openai_model", "") or "").strip()
            or None
        )
        temperature = float(getattr(settings, "full_kb_llm_temperature", 0.1) or 0.1)
        source_manifest = self._build_kb_source_manifest(prepared_text)
        system_prompt = (
            "You are scanning one chunk of the hotel's full knowledge base for the main concierge orchestrator.\n"
            "Extract only the facts from this chunk that are relevant to the guest's latest message.\n"
            "Keep factual details concrete: offerings, timings, policies, amenities, room details, prices, options, locations, or restrictions.\n"
            "Do not invent facts. Do not answer the guest directly. Do not summarize unrelated material.\n"
            "If this chunk contains nothing useful for the guest's message, return exactly: NO_RELEVANT_INFO"
        )

        extracted_parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            user_prompt = (
                f"GUEST MESSAGE:\n{str(user_message or '').strip()}\n\n"
                f"KB CHUNK {index}/{len(chunks)}:\n{chunk}\n\n"
                "Return only the relevant facts from this KB chunk. "
                "If none, return exactly: NO_RELEVANT_INFO"
            )
            try:
                result = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=model,
                    temperature=temperature,
                    max_tokens=2200,
                    trace_context={
                        "responder_type": "main",
                        "agent": "full_kb_chunk_scan",
                        "session_id": str(getattr(context, "session_id", "")),
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                    },
                )
            except Exception:
                continue
            text = str(result or "").strip()
            if not text or text.upper().startswith("NO_RELEVANT_INFO"):
                continue
            if text not in extracted_parts:
                extracted_parts.append(text)

        if not extracted_parts:
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }

        result_budget = max(
            8000,
            int(getattr(settings, "full_kb_llm_chunk_result_chars", 32000) or 32000),
        )
        sections: list[str] = []
        if source_manifest:
            sections.append(
                "=== FULL KB SOURCES INCLUDED ===\n"
                + "\n".join(f"- {name}" for name in source_manifest)
            )
        sections.append(
            "=== QUESTION-RELEVANT FACTS FROM FULL KB CHUNK SCAN ===\n"
            + "\n\n---\n\n".join(extracted_parts)
        )
        prepared = "\n\n".join(section for section in sections if section).strip()
        _log_llm_call(
            label="FULL KB CHUNK SCAN SUMMARY",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={
                "raw_kb_chars": len(raw_text),
                "chunk_count": len(chunks),
                "result_chars": len(prepared),
                "source_manifest": source_manifest[:50],
            },
        )
        return {
            "full_kb_text": prepared[:result_budget],
            "property_scope": property_scope,
        }

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
        if depth > 8:
            return str(value)[:8000]
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:16000]
        if isinstance(value, list):
            return [self._sanitize_json(item, depth + 1) for item in value[:120]]
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:120]:
                key = str(raw_key)[:200]
                normalized[key] = self._sanitize_json(raw_value, depth + 1)
            return normalized
        return str(value)[:16000]

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
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        try:
            return await llm_client.chat_with_json(
                messages=messages,
                model=model,
                temperature=temperature,
                trace_context=trace_context,
                max_tokens=max_tokens,
            )
        except TypeError as exc:
            # Backward compatibility for tests/mocks that still expose the older
            # chat_with_json(messages, model=None, temperature=None) signature.
            if "trace_context" not in str(exc) and "max_tokens" not in str(exc):
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
                "older_context_summary": str((memory_snapshot or {}).get("summary") or "").strip()[:6000],
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
    def _parse_property_scoped_knowledge_sections(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if not raw or "=== PROPERTY:" not in raw and "=== COMMON KNOWLEDGE ===" not in raw:
            return {"common": "", "properties": []}
        common_lines: list[str] = []
        properties: list[dict[str, Any]] = []
        current_kind = ""
        current_property: dict[str, Any] | None = None
        header_pattern = re.compile(r"^=== PROPERTY:\s*(.+?)(?:\s*\|\s*ALIASES:\s*(.+?))?\s*===\s*$", re.IGNORECASE)

        def flush_current() -> None:
            nonlocal current_property
            if current_kind == "property" and isinstance(current_property, dict):
                content = "\n".join(current_property.pop("_lines", [])).strip()
                if content:
                    current_property["content"] = content
                    properties.append(current_property)
            current_property = None

        for line in raw.splitlines():
            if re.match(r"^=== COMMON KNOWLEDGE ===\s*$", line.strip(), flags=re.IGNORECASE):
                flush_current()
                current_kind = "common"
                continue
            header_match = header_pattern.match(line.strip())
            if header_match:
                flush_current()
                current_kind = "property"
                property_name = re.sub(r"\s+", " ", str(header_match.group(1) or "").strip())
                aliases_raw = str(header_match.group(2) or "").strip()
                aliases = [
                    re.sub(r"\s+", " ", alias.strip())
                    for alias in aliases_raw.split(",")
                    if alias.strip()
                ]
                current_property = {
                    "property_name": property_name,
                    "aliases": aliases,
                    "_lines": [],
                }
                continue
            if current_kind == "common":
                common_lines.append(line)
            elif current_kind == "property" and isinstance(current_property, dict):
                current_property.setdefault("_lines", []).append(line)
        flush_current()
        return {
            "common": "\n".join(common_lines).strip(),
            "properties": properties,
        }

    def _scope_service_knowledge_to_runtime_property(
        self,
        *,
        service_knowledge: str,
        user_message: str,
        context: ConversationContext,
    ) -> str:
        """Filter extracted_knowledge to only the active property/properties.

        Priority order:
        1. LLM-determined ``_active_property_ids`` (set by orchestrator)
        2. Legacy ``_selected_property_scope_ids`` / ``_selected_property_scope_name``
        3. No active properties → return full KB (all properties, no truncation)
        """
        parsed = self._parse_property_scoped_knowledge_sections(service_knowledge)
        properties = parsed.get("properties", [])
        if not isinstance(properties, list) or not properties:
            return str(service_knowledge or "").strip()

        common = str(parsed.get("common") or "").strip()
        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}

        # ── Get active property IDs from LLM state ──
        active_ids = list(pending_raw.get("_active_property_ids") or [])
        # Legacy fallback
        if not active_ids:
            legacy_ids = pending_raw.get("_selected_property_scope_ids") or []
            if isinstance(legacy_ids, str):
                legacy_ids = [legacy_ids]
            active_ids = [str(pid).strip() for pid in legacy_ids if str(pid).strip()]
        if not active_ids:
            legacy_name = str(pending_raw.get("_selected_property_scope_name") or "").strip()
            if legacy_name:
                active_ids = [legacy_name]

        # Stay-property-pin fallback: if the guest has a confirmed booking at a
        # specific property, use that as the scope even when no active_ids are set.
        # This ensures service agents (banquet, dining, etc.) default to the guest's
        # stay property rather than showing all properties.
        if not active_ids:
            stay_property_id = str(pending_raw.get("_stay_property_id") or "").strip()
            stay_property_name = str(pending_raw.get("_stay_property_name") or "").strip()
            if stay_property_name:
                active_ids = [stay_property_name]
            elif stay_property_id:
                active_ids = [stay_property_id]

        if not active_ids:
            # No active properties — return full KB with all properties
            return str(service_knowledge or "").strip()

        # Match active IDs against parsed property sections
        _active_lowers = [str(aid).strip().lower() for aid in active_ids if str(aid).strip()]
        matched: list[dict[str, Any]] = []
        for row in properties:
            if not isinstance(row, dict):
                continue
            prop_name = str(row.get("property_name") or "").strip()
            aliases = [str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()]
            all_labels = [prop_name.lower()] + [a.lower() for a in aliases]
            _is_match = any(
                _aid in lbl or lbl in _aid
                for _aid in _active_lowers
                for lbl in all_labels
                if len(lbl) >= 3 and len(_aid) >= 3
            )
            if _is_match:
                matched.append(row)

        if matched:
            return self._build_scoped_kb_text(common=common, property_rows=matched)

        # No matches found — return full KB rather than losing data
        return str(service_knowledge or "").strip()

    @staticmethod
    def _build_scoped_kb_text(*, common: str, property_rows: list[dict[str, Any]]) -> str:
        """Assemble scoped KB text from common knowledge + matched property rows."""
        parts: list[str] = []
        if common:
            parts.append("=== COMMON KNOWLEDGE ===\n" + common)
        for row in property_rows:
            property_name = str(row.get("property_name") or "").strip()
            aliases = [str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()]
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            alias_block = f" | ALIASES: {', '.join(aliases[:6])}" if aliases else ""
            parts.append(f"=== PROPERTY: {property_name}{alias_block} ===\n{content}")
        return "\n\n".join(parts).strip() if parts else ""

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
        # Merge extracted_knowledge from service_kb_records so service agents
        # get the same KB data that the main orchestrator sees.
        kb_by_service = self._service_extracted_knowledge_by_service(capabilities_summary)
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
            # Preserve form_config from the original service row so the service
            # agent gets ticketing_mode, trigger_field, and form fields at runtime.
            _row_form_config = row.get("form_config")
            if not isinstance(_row_form_config, dict):
                _row_form_config = None

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
                    "ticketing_mode": str(row.get("ticketing_mode") or "").strip().lower(),
                    "form_config": _row_form_config,
                    "required_slots": normalized_slots,
                    "profile": str(prompt_pack.get("profile") or "").strip().lower(),
                    "service_prompt_role": str(prompt_pack.get("role") or "").strip()[:300],
                    "service_prompt_behavior": str(prompt_pack.get("professional_behavior") or "").strip()[:500],
                    "execution_guard": self._sanitize_json(execution_guard),
                    "confirmation_format": self._sanitize_json(confirmation_format),
                    "service_prompt_pack": self._sanitize_json(prompt_pack),
                    "service_prompt_pack_custom": bool(row.get("service_prompt_pack_custom", False)),
                    "knowledge_facts": [],
                    # Merge extracted_knowledge: prefer prompt_pack (admin-entered),
                    # fall back to service_kb_records (auto-compiled from KB files).
                    "extracted_knowledge": (
                        str(prompt_pack.get("extracted_knowledge") or "").strip()
                        or kb_by_service.get(sid, "")
                    ),
                    "generated_system_prompt": str(row.get("generated_system_prompt") or "").strip(),
                    "cuisine": str(row.get("cuisine") or "").strip(),
                    "hours": row.get("hours") if isinstance(row.get("hours"), dict) else {},
                    "delivery_zones": row.get("delivery_zones") if isinstance(row.get("delivery_zones"), list) else [],
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
        full_kb_text = _cs.get_full_kb_text_with_sources(
            max_chars=40_000,
            max_sources=200,
        )
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

        # Determine who answered this turn and extract their KB for grounding.
        _svc_answered = isinstance(target_service, dict)
        answering_llm = "service_agent" if _svc_answered else "main_orchestrator"

        service_payload: dict[str, Any] = {}
        answering_service_kb: dict[str, Any] = {}
        if _svc_answered:
            service_payload = {
                "id": self._normalize_identifier(target_service.get("id")),
                "name": str(target_service.get("name") or "").strip(),
                "profile": str(target_service.get("profile") or "").strip(),
            }
            # Pull KB content so the suggestion agent can ground its suggestions in facts.
            _svc_pack = target_service.get("service_prompt_pack") or {}
            if not isinstance(_svc_pack, dict):
                _svc_pack = {}
            _ek = str(
                target_service.get("extracted_knowledge")
                or _svc_pack.get("extracted_knowledge")
                or ""
            ).strip()
            _slots = _svc_pack.get("required_slots") or []
            answering_service_kb = {
                "extracted_knowledge": _ek,  # no truncation — per-file extraction keeps each property small
                "required_slots": _slots if isinstance(_slots, list) else [],
            }

        # Build bot delivery boundary from live config — fully dynamic per property
        hotel_code = str(getattr(context, "hotel_code", None) or "DEFAULT")
        try:
            cap_summary = config_service.get_capability_summary(hotel_code)
            raw_caps = cap_summary.get("capabilities", {})
            try:
                from services.db_config_service import db_config_service

                db_caps = await db_config_service.get_capabilities()
                if isinstance(db_caps, dict):
                    raw_caps = db_caps
                db_knowledge = await db_config_service.get_knowledge_config()
                if isinstance(db_knowledge, dict):
                    db_nlu_policy = db_knowledge.get("nlu_policy", {})
                    if isinstance(db_nlu_policy, dict):
                        cap_summary["nlu_policy"] = db_nlu_policy
                db_services = await db_config_service.get_services()
                if isinstance(db_services, list):
                    cap_summary["service_catalog"] = db_services
            except Exception:
                pass
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

        # Build allowed and blocked service lists for the suggestion agent.
        # Include a kb_hint per allowed service so the agent can verify topics before suggesting.
        try:
            all_services = cap_summary.get("service_catalog", [])
            allowed_services = []
            for s in all_services:
                if not isinstance(s, dict):
                    continue
                if not bool(s.get("is_active", True)):
                    continue
                if self._normalize_identifier(s.get("phase_id")) != selected_phase_id:
                    continue
                svc_name = str(s.get("name") or s.get("id") or "").strip()
                if not svc_name:
                    continue
                _pack = s.get("service_prompt_pack") or {}
                if not isinstance(_pack, dict):
                    _pack = {}
                _kb_hint = str(
                    s.get("extracted_knowledge") or _pack.get("extracted_knowledge") or ""
                ).strip()[:6000]
                allowed_services.append({
                    "name": svc_name,
                    "can_do": str(s.get("description") or s.get("profile") or "").strip()[:120],
                    "ticketing_enabled": bool(s.get("ticketing_enabled", True)),
                    "kb_hint": _kb_hint,  # factual KB snippet — use this to verify suggestions
                })
            blocked_service_names = [
                str(s.get("name") or s.get("id") or "").strip()
                for s in all_services
                if isinstance(s, dict)
                and bool(s.get("is_active", True))
                and self._normalize_identifier(s.get("phase_id")) != selected_phase_id
                and str(s.get("name") or s.get("id") or "").strip()
            ]
        except Exception:
            allowed_services = []
            blocked_service_names = []

        payload = {
            "user_message": str(user_message or "").strip(),
            "assistant_response": str(decision.response_text or "").strip(),
            "answering_llm": answering_llm,
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
            "answering_service_kb": answering_service_kb,
            "history": self._history_preview(context, max_messages=8, max_chars=3000),
            "bot_delivery_boundary": bot_delivery_boundary,
            "allowed_services": allowed_services,
            "blocked_service_names": blocked_service_names,
            "response_contract": {"suggested_actions": ["what the guest would type next"]},
        }
        system_prompt = (
            "You are a next-turn suggestion planner for a hotel concierge chat.\n"
            "Return STRICT JSON only.\n"
            "Generate 2 to 4 suggestions representing what the guest would most likely send next.\n\n"
            "Work through the payload STRICTLY in this order — do not skip steps:\n"
            "0. Read `bot_delivery_boundary` first — this defines the hard limits of what this bot can actually deliver. "
            "If a suggestion requires the bot to show images or media, provide real-time data, or use a capability not listed in `enabled_capabilities` — discard it. "
            "`property_constraints` lists explicit rules for this property that must also be respected.\n"
            "1. Read `assistant_response` — this is what the bot just said. Suggestions must be a natural direct response to that specific message.\n"
            "2. Read `history` — understand the full conversation thread. Do not suggest anything already discussed or resolved.\n"
            "3. Read `service` and `decision.missing_fields` — if a service is active, suggest messages that continue that flow. "
            "If fields are missing, suggest messages that would naturally lead toward providing that information, not the values themselves.\n"
            "4. KB GROUNDING — CRITICAL STEP. This is the most important filter.\n"
            "   a. If `answering_llm` is `service_agent`: read `answering_service_kb.extracted_knowledge` carefully. "
            "This is the ONLY knowledge the bot has for this service. "
            "Before including any suggestion about this service, verify the topic exists in this KB text. "
            "Rules:\n"
            "      - If a specific item (menu item, package, amenity) is NOT listed in the KB → do not ask about it.\n"
            "      - If a price is NOT stated in the KB → do not ask about the price.\n"
            "      - If a feature or option is NOT mentioned in the KB → do not ask whether it exists.\n"
            "      - Silence in the KB means the bot cannot answer — treat it the same as 'not available'.\n"
            "      - Only suggest questions where the answer is clearly present and positive in the KB.\n"
            "   b. If `answering_llm` is `main_orchestrator`: the bot answered from general hotel info. "
            "Only suggest broad hotel questions (amenities, policies, facilities) or questions that lead to an allowed service. "
            "Do not suggest service-specific detail questions.\n"
            "   c. For suggestions about OTHER allowed services: use each service's `kb_hint` field the same way. "
            "If the `kb_hint` does not mention the topic, do not suggest asking about it. "
            "If a service has an empty `kb_hint`, only suggest broad open questions (e.g. 'What can you help me with?').\n"
            "5. Read `blocked_service_names` — NEVER suggest booking, ordering, requesting, or asking to use any of these. "
            "They are unavailable right now — any suggestion about them leads to a dead-end. "
            "This includes indirect suggestions like 'Can I book a table?' when table booking is blocked.\n"
            "6. Final quality gate — for each remaining suggestion, ask: if the guest sent this exact message, would the bot give a genuinely useful and positive answer backed by KB data? "
            "If the answer would be 'I don't have that information', 'not available', or 'please contact staff' — discard it and replace with something the bot CAN answer well from its KB.\n\n"
            "Voice — strictly enforced:\n"
            "Every suggestion must be a natural first-person guest message, exactly what they would type into the chat.\n"
            "Bad: 'Ask about room types', 'View services', 'Show options', 'Share details'\n"
            "Good: 'What room types do you have?', 'What services are available?', 'Can I see my options?'\n\n"
            "Never suggest any value that is unique to the individual guest — this includes names, room numbers, phone numbers, email addresses, flight numbers, dates, times, booking references, order quantities, party sizes, prices, or any other personal or context-specific data. The guest is the only one who knows these — they must type them.\n"
            "Also never suggest a message where the guest is offering or providing their personal information, even without stating the actual value. Messages like 'Here's my full name and details', 'I'll share my details', 'Here is my information', 'Let me provide my info' are all forbidden — they imply the guest is about to hand over unique personal data.\n"
            "Never suggest that the guest edits, modifies, or changes a request that has already been confirmed or completed (e.g. do not suggest 'Edit my booking' or 'Change my order' after confirmation).\n"
            "Suggestions must only be questions the guest wants to ask, or service actions they want to request — never data submissions.\n\n"
            "Keep each suggestion 2-8 words."
        )
        _sugg_bk = self._build_booking_context_prompt_block(context)
        if _sugg_bk:
            system_prompt = system_prompt + "\n" + _sugg_bk
        model = (
            str(getattr(settings, "chat_llm_next_suggestions_model", "") or "").strip()
            or str(getattr(settings, "llm_service_agent_model", "") or "").strip()
            or str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or None
        )
        _log_llm_call(
            label="NEXT SUGGESTION AGENT",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
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
        _log_llm_call(
            label="NEXT SUGGESTION AGENT RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={},
            response=raw if isinstance(raw, dict) else {},
        )

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
            clipped = content[: min(room_left, 2500)]
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

    @staticmethod
    def _count_bullet_lines(text: str) -> int:
        lines = str(text or "").splitlines()
        count = 0
        for line in lines:
            if re.match(r"^\s*[-*]\s+\S+", str(line or "")):
                count += 1
        return count

    @staticmethod
    def _looks_like_food_or_dining_service(service: dict[str, Any] | None) -> bool:
        if not isinstance(service, dict):
            return False
        combined = " ".join(
            [
                str(service.get("id") or ""),
                str(service.get("name") or ""),
                str(service.get("type") or ""),
                str(service.get("profile") or ""),
                str(service.get("description") or ""),
                str(service.get("cuisine") or ""),
            ]
        ).lower().replace("_", " ")
        if not combined:
            return False
        markers = (
            "dining",
            "food",
            "restaurant",
            "menu",
            "meal",
            "beverage",
            "breakfast",
            "lunch",
            "dinner",
            "room service",
            "in room dining",
            "order",
        )
        return any(marker in combined for marker in markers)

    @staticmethod
    def _extract_food_order_values(pending_data: dict[str, Any]) -> list[str]:
        if not isinstance(pending_data, dict):
            return []
        key_markers = (
            "meal",
            "breakfast",
            "lunch",
            "dinner",
            "beverage",
            "drink",
            "food",
            "dish",
            "item",
            "order",
            "snack",
            "kid",
        )
        excluded_exact = {
            "service_id",
            "service_name",
            "guest_id",
            "guest_name",
            "guest_phone",
            "room_number",
            "check_in",
            "check_out",
            "stay_checkin_date",
            "stay_checkout_date",
            "stay_date_range",
            "booking_ref",
        }
        excluded_values = {"yes", "no", "true", "false", "none", "na", "n/a"}
        values: list[str] = []
        seen: set[str] = set()
        for raw_key, raw_value in pending_data.items():
            key = str(raw_key or "").strip().lower()
            if not key or key.startswith("_") or key in excluded_exact:
                continue
            if not any(marker in key for marker in key_markers):
                continue
            value_text = str(raw_value or "").strip()
            if not value_text:
                continue
            for part in re.split(r"[\n,]+", value_text):
                candidate = re.sub(r"\s+", " ", str(part or "").strip(" ."))
                if not candidate:
                    continue
                candidate_norm = candidate.lower()
                if candidate_norm in excluded_values:
                    continue
                if re.fullmatch(r"\d+", candidate_norm):
                    continue
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate_norm):
                    continue
                if candidate_norm in seen:
                    continue
                seen.add(candidate_norm)
                values.append(candidate)
                if len(values) >= 10:
                    return values
        return values

    @staticmethod
    def _response_mentions_value(response_text: str, value: str) -> bool:
        response_norm = re.sub(r"[^a-z0-9]+", " ", str(response_text or "").lower())
        value_norm = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
        if not response_norm or not value_norm:
            return False
        if value_norm in response_norm:
            return True
        tokens = [tok for tok in value_norm.split() if len(tok) >= 3][:5]
        if not tokens:
            return False
        return all(tok in response_norm for tok in tokens)

    async def _run_answer_first_guard(
        self,
        *,
        user_message: str,
        decision: OrchestrationDecision,
        context: ConversationContext,
        selected_phase_id: str,
        selected_phase_name: str,
        target_service: dict[str, Any] | None = None,
        full_kb_text: str = "",
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
                "generic_kb_request": bool(self._is_generic_kb_request(decision)),
                "answered_current_query": bool(decision.answered_current_query),
                "blocking_fields": list(decision.blocking_fields or []),
                "deferrable_fields": list(decision.deferrable_fields or []),
                "missing_fields": list(decision.missing_fields or []),
                "pending_action": str(decision.pending_action or ""),
                "followup_question": str(decision.followup_question or ""),
            },
            "service": service_payload,
            "full_knowledge_base": full_kb_text or None,
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

        # --- WEATHER INJECTION FOR GUARD ---
        user_msg_lower = str(user_message or "").lower()
        if any(w in user_msg_lower for w in ["weather", "temperature", "forecast", "climate", "rain", "sunny"]):
            try:
                from services.weather_service import get_current_weather
                hotel_code_val = getattr(context, "hotel_code", "DEFAULT")
                weather_info = await get_current_weather(hotel_code_val)
                if weather_info:
                    payload["live_hotel_weather_context"] = weather_info
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Failed to inject weather into guard payload: %s", e)
        # -------------------------

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
            "8) If decision.target_service_id is empty and decision.generic_kb_request is false, avoid recommended_action=collect_info.\n"
            "9) For out-of-phase or unavailable-service situations, keep recommended_action=respond_only and provide a clearer response instead.\n"
            "10) If full_knowledge_base contains explicit facts answering the ask, can_answer_from_context must be true and revised_response_text must use those facts.\n"
            "11) Do not say details are unavailable when full_knowledge_base or service facts contain them.\n"
            "12) For decision.generic_kb_request=true, preserve the collection flow when the response is explicitly collecting the remaining details for a KB-grounded manual request.\n"
        )
        _guard_bk = self._build_booking_context_prompt_block(context)
        if _guard_bk:
            system_prompt = system_prompt + "\n" + _guard_bk
        model = (
            str(getattr(settings, "chat_llm_answer_first_guard_model", "") or "").strip()
            or str(getattr(settings, "llm_service_agent_model", "") or "").strip()
            or str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or None
        )
        temperature = float(getattr(settings, "chat_llm_answer_first_guard_temperature", 0.0) or 0.0)
        _log_llm_call(
            label="ANSWER FIRST GUARD",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
        )
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
        _log_llm_call(
            label="ANSWER FIRST GUARD RESPONSE",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={},
            response=raw if isinstance(raw, dict) else {},
        )
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
        full_kb_text: str = "",
    ) -> OrchestrationDecision:
        decision = self._apply_answer_priority_fields(decision)
        pre_guard_response_text = str(decision.response_text or "").strip()
        guard_raw = await self._run_answer_first_guard(
            user_message=user_message,
            decision=decision,
            context=context,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service,
            full_kb_text=full_kb_text,
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
        rewrite_reverted_reason = ""

        if pre_guard_response_text and str(decision.response_text or "").strip():
            before_bullets = self._count_bullet_lines(pre_guard_response_text)
            after_bullets = self._count_bullet_lines(decision.response_text)
            if before_bullets >= 2 and after_bullets < before_bullets:
                decision.response_text = pre_guard_response_text
                rewrite_reverted_reason = "list_item_loss"

        if self._looks_like_food_or_dining_service(target_service):
            merged_pending = dict(self._coerce_public_pending(context.pending_data))
            if isinstance(decision.pending_data_updates, dict):
                for key, value in decision.pending_data_updates.items():
                    if str(value or "").strip():
                        merged_pending[str(key)] = value
            order_values = self._extract_food_order_values(merged_pending)
            if len(order_values) >= 2:
                missing_after = [
                    item for item in order_values
                    if not self._response_mentions_value(str(decision.response_text or ""), item)
                ]
                if missing_after:
                    missing_before = [
                        item for item in order_values
                        if not self._response_mentions_value(pre_guard_response_text, item)
                    ]
                    if len(missing_before) < len(missing_after) and pre_guard_response_text:
                        decision.response_text = pre_guard_response_text
                        rewrite_reverted_reason = "food_order_item_loss"
                        decision.metadata["answer_first_guard_missing_order_items"] = missing_after[:6]

        decision.metadata.setdefault("answer_first_guard_applied", True)
        decision.metadata["answer_first_guard_answers_current_query"] = answers_current_query
        decision.metadata["answer_first_guard_blocking_fields"] = list(decision.blocking_fields or [])
        decision.metadata["answer_first_guard_deferrable_fields"] = list(decision.deferrable_fields or [])
        decision.metadata["answer_first_guard_recommended_action"] = recommended_action
        decision.metadata["answer_first_guard_recommended_pending_action"] = recommended_pending_action or ""
        decision.metadata["answer_first_guard_reason"] = str(guard_raw.get("reason") or "").strip()
        if rewrite_reverted_reason:
            decision.metadata["answer_first_guard_rewrite_reverted"] = rewrite_reverted_reason
        return decision

    async def _resolve_target_service_with_llm(
        self,
        *,
        user_message: str,
        selected_phase_id: str,
        selected_phase_name: str,
        services_snapshot: list[dict[str, Any]],
        context: Any = None,
    ) -> tuple[str, str]:
        if not str(settings.openai_api_key or "").strip():
            return "", ""
        in_phase_services = [
            {
                "id": self._normalize_identifier(item.get("id")),
                "name": str(item.get("name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "what_it_handles": str(item.get("description") or "").strip(),
                "routing_keywords": item.get("routing_keywords", []),
                "profile": str(item.get("profile") or "").strip(),
                "phase_id": self._normalize_identifier(item.get("phase_id")),
            }
            for item in services_snapshot
            if isinstance(item, dict)
            and bool(item.get("is_active", True))
            and self._normalize_identifier(item.get("phase_id")) == selected_phase_id
        ]
        if not in_phase_services:
            return "", ""

        # Build a plain-prose summary of each available service for the router LLM
        service_guide_lines = ["AVAILABLE SERVICES IN CURRENT PHASE:"]
        for svc in in_phase_services[:60]:
            _sid = svc.get("id", "")
            _sname = svc.get("name", _sid)
            _sdesc = svc.get("description", "")
            _skw = ", ".join(svc.get("routing_keywords", [])[:8])
            service_guide_lines.append(
                f"- {_sname} (id: {_sid})"
                + (f": {_sdesc}" if _sdesc else "")
                + (f" | triggers: {_skw}" if _skw else "")
            )
        service_guide = "\n".join(service_guide_lines)

        prompt_payload = {
            "user_message": str(user_message or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "in_phase_services": in_phase_services[:60],
            "response_contract": {
                "is_service_request": "bool",
                "service_id": "exact id from in_phase_services when service request else empty string",
                "action_hint": "respond_only|collect_info|dispatch_handler|create_ticket",
                "ambiguous": "bool — true if two or more services are equally plausible",
                "reason": "short reason for the routing decision",
            },
        }
        system_prompt = (
            "You are a service router for a hotel concierge assistant.\n"
            "Return STRICT JSON only.\n"
            "\n"
            f"{service_guide}\n"
            "\n"
            "ROUTING RULES:\n"
            "1. Read the service name AND description to understand what each service handles.\n"
            "2. When one service is clearly the best fit, return its exact service_id.\n"
            "3. If the request could belong to TWO OR MORE services equally, set ambiguous=true, "
            "leave service_id empty, and use action_hint=respond_only so the orchestrator asks a clarifying question.\n"
            "4. If the request is informational (no booking/order intent), leave service_id empty.\n"
            "5. Never force a guess when the message is ambiguous or vague.\n"
            "6. Route by service name and description first; routing_keywords are supporting hints only.\n"
        )
        if context:
            _router_bk = self._build_booking_context_prompt_block(context)
            if _router_bk:
                system_prompt = system_prompt + "\n" + _router_bk
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
        _log_llm_call(
            label="SERVICE ROUTER RESPONSE",
            session_id="",
            user_message=user_message,
            system_prompt=system_prompt,
            payload=prompt_payload,
            response=raw if isinstance(raw, dict) else {},
        )
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

    async def _resolve_last_service_continuation_with_llm(
        self,
        *,
        user_message: str,
        last_assistant_message: str,
        last_service_id: str,
        selected_phase_id: str,
        selected_phase_name: str,
        services_snapshot: list[dict[str, Any]],
        context: Any = None,
    ) -> tuple[bool, str]:
        if not str(settings.openai_api_key or "").strip():
            return False, ""

        normalized_last_service_id = self._normalize_identifier(last_service_id)
        if not normalized_last_service_id:
            return False, ""

        in_phase_services = [
            item
            for item in services_snapshot
            if isinstance(item, dict)
            and bool(item.get("is_active", True))
            and self._normalize_identifier(item.get("phase_id")) == selected_phase_id
        ]
        if not in_phase_services:
            return False, ""

        last_service = next(
            (
                svc
                for svc in in_phase_services
                if self._normalize_identifier(svc.get("id")) == normalized_last_service_id
            ),
            None,
        )
        if not isinstance(last_service, dict):
            return False, ""

        prompt_payload = {
            "user_message": str(user_message or "").strip(),
            "last_assistant_message": str(last_assistant_message or "").strip(),
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "last_active_service": {
                "id": self._normalize_identifier(last_service.get("id")),
                "name": str(last_service.get("name") or "").strip(),
                "description": str(last_service.get("description") or "").strip(),
                "type": str(last_service.get("type") or "").strip(),
                "cuisine": str(last_service.get("cuisine") or "").strip(),
            },
            "response_contract": {
                "continue_with_last_service": "bool",
                "reason": "short string",
                "confidence": "number 0..1",
            },
        }

        system_prompt = (
            "You are a continuity router for a hotel concierge assistant.\n"
            "Return STRICT JSON only.\n"
            "\n"
            "Task: decide whether the latest user message should continue the same service flow\n"
            "as the immediately previous assistant message.\n"
            "\n"
            "Rules:\n"
            "1. Use both `last_assistant_message` and `user_message`.\n"
            "2. If the user message is an answer/reply/follow-up to the last assistant turn,\n"
            "   set continue_with_last_service=true.\n"
            "3. If the user clearly starts a different topic/service, set false.\n"
            "4. For short replies, favor continuity when they plausibly answer the last assistant prompt.\n"
            "5. Do not infer a different service unless explicitly requested.\n"
        )
        if context:
            _continuity_bk = self._build_booking_context_prompt_block(context)
            if _continuity_bk:
                system_prompt = system_prompt + "\n" + _continuity_bk

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
                    "agent": "continuity_router",
                    "selected_phase_id": selected_phase_id,
                    "selected_phase_name": selected_phase_name,
                    "last_service_id": normalized_last_service_id,
                },
            )
        except Exception:
            return False, ""

        _log_llm_call(
            label="CONTINUITY ROUTER RESPONSE",
            session_id=str(getattr(context, "session_id", "")) if context is not None else "",
            user_message=user_message,
            system_prompt=system_prompt,
            payload=prompt_payload,
            response=raw if isinstance(raw, dict) else {},
        )
        if not isinstance(raw, dict):
            return False, ""

        continue_raw = raw.get("continue_with_last_service")
        continue_with_last_service = False
        if isinstance(continue_raw, bool):
            continue_with_last_service = continue_raw
        elif str(continue_raw or "").strip().lower() in {"true", "1", "yes", "y"}:
            continue_with_last_service = True

        reason = str(raw.get("reason") or "").strip()
        return continue_with_last_service, reason

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
        elif str(overlay.action or "").strip().lower() == "create_ticket":
            # Service agent explicitly set action=create_ticket (e.g. confirmation
            # override) and cleared pending_action to None.  The merge must honour
            # the clear; otherwise the orchestrator's stale pending_action survives
            # and the ticket-creation gate blocks with "ticket_waiting_for_user_input".
            merged.pending_action = None
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
        if overlay.ticket.generic_request:
            merged.ticket.generic_request = True
        if overlay.ticket.phase_applicable is not None:
            merged.ticket.phase_applicable = overlay.ticket.phase_applicable
        if overlay.ticket.approval_required:
            merged.ticket.approval_required = True
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
        if overlay.ticket.department_id:
            merged.ticket.department_id = overlay.ticket.department_id
        if overlay.ticket.department_name:
            merged.ticket.department_name = overlay.ticket.department_name
        if overlay.ticket.evidence:
            merged.ticket.evidence = list(overlay.ticket.evidence)

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
        current_phase_id: str = "",
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
            "\n=== SERVICE AVAILABILITY RULE ===\n"
            "Some services are only available at certain points in the guest journey.\n"
            "If this service is not available right now:\n"
            "  - Provide factual information only (what the service offers, pricing, hours, etc.).\n"
            "  - Do NOT promise, invite, offer, or imply that the guest can use or book this service right now.\n"
            "  - Do NOT begin collecting booking/order slots.\n"
            "  - Phrase unavailability naturally — NEVER mention phase names (pre_booking, pre_checkin, "
            "during_stay, post_checkout) or say 'current phase' in any response.\n"
            "  - Use natural alternatives: 'That will be available once you check in', "
            "'We can arrange this during your stay', 'This can be set up when you arrive', etc.\n"
            "  - Keep the tone warm and informative — not transactional.\n"
            "This rule applies even if the guest seems eager to book or asks you to proceed.\n"
            "ABSOLUTE RULE: Do NOT use the words pre_booking, pre_checkin, during_stay, post_checkout, "
            "'current phase', or 'this phase' anywhere in your response."
        )

        if "=== PROPERTY:" in str(service_knowledge or "") or "=== PROPERTY:" in str(full_kb_text or ""):
            lines.append(
                "\n=== MULTI-PROPERTY KNOWLEDGE RULE ===\n"
                "The knowledge base for this service contains separate property/location sections.\n"
                "If conversation context already makes one property/location clear, use ONLY that property's section "
                "plus any shared/common knowledge.\n"
                "If the property is not yet clear and the guest needs property-specific details or wants to proceed "
                "transactionally, ask a short clarification question before continuing.\n"
                "Never mix facts from one property's section into another property's answer.\n"
                "Never continue booking, ordering, or request execution until the property is clear."
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
            ticket_lines = [
                "\n=== TICKET CREATION ===",
                "\n--- SLOT COLLECTION INTENT GATE (read this before collecting anything) ---",
                "NEVER begin collecting slots (name, dates, room, phone, payment, etc.) until the guest",
                "has expressed EXPLICIT booking/order intent. Expressing a need or preference is NOT intent.",
                "",
                "NOT intent — answer the question first, then offer to proceed:",
                "  - 'I need a room with a bathtub' → describe matching room(s), then ask 'Would you like to go ahead and book it?'",
                "  - 'Do you have a room for two?' → describe options, then ask 'Shall I help you book one?'",
                "  - 'I'm looking for something quiet / with a view / comfortable' → answer, then offer",
                "  - Any question starting with 'do you have', 'what is', 'can you tell me', 'I need X', 'I'm looking for X'",
                "",
                "IS intent — only now collect slots:",
                "  - 'I'd like to book', 'Reserve it for me', 'Go ahead', 'Yes, book it'",
                "  - 'Can I make a reservation?', 'I want to order', 'Book it'",
                "  - 'Yes, please' / 'Yes, proceed' (after you explicitly offered to proceed)",
                "",
                "THE TWO-STEP RULE:",
                "  Step 1: Answer the question (which room has a bathtub? what are the hours?).",
                "  Step 2: Offer to proceed ('Would you like to go ahead and book the Prestige Suite?').",
                "  Only after the guest says YES do you ask for name, dates, and other details.",
                "--- END INTENT GATE ---",
                "",
                "--- INFORM BEFORE COLLECT ---",
                "Even after the guest has expressed clear booking intent, never ask for a slot",
                "whose answer requires knowledge the guest does not yet have.",
                "",
                "PRESENT BEFORE ASK (MANDATORY): Before asking the guest to choose or specify",
                "a menu item, treatment, room type, package, time slot, or ANY preference from a list —",
                "FIRST share all available options from the KB. The guest cannot choose from options",
                "they haven't seen. Only ask 'which one?' after you have shown the full list.",
                "  Examples:",
                "  - In-room dining: show the full menu section first, then ask which item they want",
                "  - Spa: list all treatments with duration and price, then ask which they prefer",
                "  - Restaurant: list all restaurants with timings, then ask which one",
                "  - Room booking: describe available room types, then ask which to book",
                "",
                "If this service has MULTIPLE OPTIONS the guest must choose from:",
                "  → List ALL available options from the KB first.",
                "  → Then ask which one they want.",
                "  → Never ask 'which restaurant?' or 'which room type?' if the guest hasn't seen the list.",
                "",
                "If this service has a COST or POLICY the guest is committing to",
                "(charges, cancellation terms, pricing, availability conditions):",
                "  → State the relevant cost/policy clearly BEFORE asking for their personal details.",
                "  → The guest must know what they are agreeing to before you take their name or contact.",
                "--- END INFORM BEFORE COLLECT ---",
                "",
                "--- CONFIRMATION RULE (ABSOLUTE — NO EXCEPTIONS) ---",
                "CRITICAL: NEVER ask for confirmation while ANY required field is still missing.",
                "The confirmation step ONLY happens after every single required field has a value.",
                "If fields are still missing → ask for those fields. Do NOT mix collecting and confirming.",
                "",
                "Once ALL required fields are collected, THEN:",
                "  Step 1: Show a complete bullet-point summary of every collected detail",
                "          (name, dates, room type, restaurant, time, item, price — everything).",
                "  Step 2: Ask the guest to confirm: 'Please confirm the above details to proceed.'",
                "  Step 3: Only after the guest explicitly confirms → create the ticket.",
                "Even if the guest already said 'go ahead' or 'book it', still show the full summary",
                "and ask for explicit confirmation. Never skip or abbreviate the summary.",
                "Never create a ticket without this confirmation step.",
                "--- END CONFIRMATION RULE ---",
            ]
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
            ticket_lines.append(
                "Save each collected value in pending_data_updates using the exact field id as the key.\n"
                "CRITICAL — pending_data_updates MUST always reflect EVERY field value you currently know, "
                "not just newly-collected ones this turn. If the guest confirmed a room type earlier and you "
                "are now collecting dates, pending_data_updates must still include the room type. "
                "Omitting a known value causes the system to lose track of it. "
                "Only omit a field if it is genuinely unknown or still in missing_fields."
            )

            # Confirmation step
            if requires_confirmation_step:
                confirm_template = str(confirmation_format.get("template") or "").strip()
                ticket_lines.append(
                    f"\nOnce all fields are collected, summarise everything clearly and ask the guest to confirm.\n"
                    f"The summary MUST explicitly list every collected detail present in pending_data_raw "
                    f"or known_context that is relevant to THIS service. For example:\n"
                    f"  - For room bookings: guest name, phone, email, room type, check-in/check-out dates, guest count, etc.\n"
                    f"  - For food/dining orders: ordered items with quantities, total price, delivery preferences, etc.\n"
                    f"  - For restaurant reservations: restaurant name, date, time, party size, etc.\n"
                    f"  - For event bookings: venue, event date, event type, special requests, etc.\n"
                    f"Show ONLY the fields that are actually collected and relevant — do NOT include booking/stay "
                    f"metadata (guest_id, check-in/check-out dates, stay date range, confirmation codes) "
                    f"unless this service is specifically about modifying a room booking.\n"
                    f"NEVER display any field from _internal_booking_reference to the guest.\n"
                    f"Never say 'the above details' — always spell them out.\n"
                    f"Confirmation phrase the guest must say: '{confirmation_phrase}'\n"
                    f"Set pending_action='{confirmation_pending_action}' while waiting for confirmation.\n"
                    f"When the guest confirms: set action=create_ticket, ticket.required=true, "
                    f"ticket.ready_to_create=true, pending_action=null. "
                    f"The confirmation response_text MUST again list ALL collected details relevant to this service "
                    f"from pending_data_raw — every field that was actually collected."
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
            ticket_lines.append(
                "\n=== DEPARTMENT ASSIGNMENT ===\n"
                "The payload includes available_departments — a list of {department_id, department_name} objects.\n"
                "When creating a ticket (ticket.ready_to_create=true), choose the department_id from available_departments "
                "that best matches this service.\n"
                "Examples: spa/wellness service → Wellness dept, dining/food order → F&B dept, "
                "room issue/housekeeping → Housekeeping dept, transport → Transport/Concierge dept.\n"
                "Set ticket.department_id (the numeric id) and ticket.department_name.\n"
                "If available_departments is empty or no match exists, leave both fields empty."
            )

            # Mid-flow complaint handling: if the guest reports a problem about THIS service
            # while you are already collecting their booking/order details, handle it here —
            # do NOT bounce it to the main orchestrator. You have the full context.
            ticket_lines.append(
                "\n=== MID-FLOW COMPLAINT HANDLING ===\n"
                "If the guest reports a problem, malfunction, or dissatisfaction with this service "
                "while you are mid-flow (slots are being collected): handle it directly — BUT only if "
                "the complaint is contextually plausible given where the guest is in their journey.\n"
                "Examples of implausible complaints: reporting a cockroach or broken AC before checking in, "
                "reporting a damaged room while in pre-booking phase, complaining about a meal not yet ordered.\n"
                "If the complaint is plausible: set action=create_ticket, ticket.category='complaint', "
                "ticket.ready_to_create=true, ticket.issue=a clear summary of the problem. "
                "Clear pending_action. Do NOT continue the booking/order flow after a valid complaint is raised.\n"
                "If the complaint is NOT plausible in the current context: respond with empathy, "
                "acknowledge the concern, but do NOT create a ticket. Offer to assist or connect with staff."
            )
            lines.extend(ticket_lines)

        # ══ SECTION 3: KNOWLEDGE BASE ══════════════════════════════════════════
        # This is the complete knowledge for this service, exactly as entered in the system.
        # The agent must answer all questions from this knowledge and nothing else.
        kb_sections: list[str] = []
        if str(service_knowledge or "").strip():
            kb_sections.append(str(service_knowledge or "").strip())
        if str(full_kb_text or "").strip():
            kb_sections.append("=== PROPERTY-SCOPED HOTEL KNOWLEDGE ===\n" + str(full_kb_text or "").strip())
        kb_content = "\n\n".join(section for section in kb_sections if section).strip()
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
        lines.append(
            "\n=== COMPLETENESS RULE ===\n"
            "When the guest asks about rooms, options, packages, amenities, services, or any list of offerings:\n"
            "- List ALL items from the knowledge base — never summarize, skip, or show only a partial list.\n"
            "- If the KB lists 5 room types, show all 5. If it lists 8 amenities, show all 8.\n"
            "- Include ALL details for each item (price, description, features, capacity, etc.) as given in the KB.\n"
            "- Do NOT say 'and more' or 'among others' — be exhaustive.\n"
            "- The guest deserves complete information to make a decision."
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

        lines.append(
            "\n=== RESPONSE STYLE ===\n"
            "Use plain chat text for guest-facing responses. "
            "Do NOT use markdown markers such as **bold** or *italic*.\n"
            "When asking for multiple missing details, always use bullet points with '-' items.\n"
            "Example:\n"
            "Please share the following details:\n"
            "- Room number\n"
            "- Check-in date\n"
            "- Check-out date\n"
            "\n=== TONE & LANGUAGE RULES ===\n"
            "You are speaking to a hotel guest. Use warm, natural, human language at all times.\n"
            "NEVER use these words or phrases in guest-facing responses:\n"
            "  - 'escalate', 'ticket', 'reference number', 'ticket ID', 'logged your complaint'\n"
            "  - 'create a ticket', 'automated', 'system issue', 'ticketing'\n"
            "INSTEAD use natural alternatives:\n"
            "  - 'I have noted this down' instead of 'I have created a ticket'\n"
            "  - 'Our team will look into this' instead of 'This has been escalated'\n"
            "  - 'I will make sure someone helps you' instead of 'I will escalate this'\n"
            "  - 'Let me connect you with our team' instead of 'Let me transfer you'\n"
            "For complaints: ALWAYS empathize first ('I am sorry to hear that', "
            "'That is not the experience we want for our guests'), then offer help.\n"
            "Never jump straight to asking for details after a complaint — acknowledge the feeling first."
        )

        # ── Scope, handoff, and known context ────────────────────────────────
        lines.append(
            f"\nYou only handle {service_name} topics. "
            "When a guest asks a question, first decide: is this question about THIS service's domain?\n"
            "  - If YES (topic-adjacent — e.g. room types/sizes/amenities/pricing/policies during a room booking flow): "
            "answer from your knowledge base. If the specific detail is not in your KB, say it is not available in the current system and offer to connect them with staff. "
            "Keep the pending booking/order flow alive — do NOT set context_switched.\n"
            "  - If NO (completely unrelated to this service — e.g. asking about the pool, spa, dining, gym, parking, events, or any other hotel facility while mid-booking): "
            "do NOT attempt to answer. Set relevant_to_service=false AND context_switched=true in metadata and use a short handoff line like 'Let me get that answered for you.' "
            "The main hotel assistant has the full hotel knowledge base and will answer it. "
            "The suspended booking flow will be offered for resume after the main assistant answers.\n"
            "Rule of thumb: if the question is about THIS service's subject matter → stay and answer or say unavailable (relevant_to_service=true). "
            "If the question is about a completely different part of the hotel → delegate (relevant_to_service=false, context_switched=true).\n"
            "\n=== KNOWN CONTEXT & GUEST FACTS (read this before asking for ANYTHING) ===\n"
            "The payload includes known_context with guest data the hotel already has on file "
            "(e.g. guest_name, room_number, phone, email).\n"
            "\n"
            "The payload also includes _internal_booking_reference with booking/stay metadata "
            "(guest_id, check-in/check-out dates, stay date range, confirmation codes). "
            "This is for YOUR internal reference only — use it for date-based logic if needed, "
            "but NEVER display these fields to the guest in any response or confirmation summary.\n"
            "\n"
            "ABSOLUTE RULE: NEVER ask for any information that is already present in known_context or pending_data_collected. "
            "Use the value directly — do not ask the guest to repeat or re-confirm it.\n"
            "\n"
            "HOTEL CHECK-IN REALITY — think like a hotel:\n"
            "  When a guest checks in, the hotel collects: full name, room number, phone, email.\n"
            "  These are on file for the entire stay and after checkout.\n"
            "  A guest who is already in the hotel should NEVER be asked for their name, room number, phone, or email.\n"
            "\n"
            "PHASE-SPECIFIC RULES (current_phase_id is in the payload):\n"
            f"  {'during_stay / post_checkout' if current_phase_id in ('during_stay', 'post_checkout') else current_phase_id or 'during_stay / post_checkout'} phase rules:\n"
            + (
                "  -> Guest is checked in (or has checked out). Hotel has name, room number, phone, email on file.\n"
                "  -> NEVER ask for guest_name, room_number, phone, or email — treat them as known even if not yet in known_context.\n"
                "  -> Only collect service-specific details: date, time, preferences, item choices, special requests.\n"
                if current_phase_id in ("during_stay", "post_checkout")
                else
                "  pre_checkin phase rules:\n"
                "  -> Guest has a confirmed reservation. Hotel has name, phone, email from booking.\n"
                "  -> NEVER ask for guest_name, phone, or email if they are in known_context.\n"
                "  -> Room number may not be assigned yet — only ask if the service genuinely needs it and it is absent from known_context.\n"
                if current_phase_id == "pre_checkin"
                else
                "  pre_booking phase rules:\n"
                "  -> Guest may not have a reservation yet. Name, phone, email may be unknown.\n"
                "  -> Only collect contact info (name, phone, email) if the service genuinely needs it AND it is NOT in known_context.\n"
                "  -> Never ask speculatively — only request what is actually required to fulfil the specific request.\n"
            )
            + "\n"
            "UNIVERSAL RULE (all phases): If a field is present in known_context — regardless of phase — NEVER ask for it.\n"
            "\nAlways read history_last_10 first, then full_history_context (including older_context_summary) before responding. "
            "Use last_user_message and last_assistant_message as the strongest anchors for short replies.\n"
            "Resolve short replies ('yes', a date, a number) against what was last asked.\n"
            "Capture any NEW useful guest details (preferences, dietary needs, special occasion) "
            "in new_facts_to_remember as snake_case key-value pairs. Do NOT re-store fields already in known_context.\n"
            "Return STRICT JSON only matching the response schema."
        )

        # ── DATE VALIDATION ───────────────────────────────────────────────────
        lines.append(
            "\n=== DATE & INPUT VALIDATION ===\n"
            "The payload includes `current_date` (today's date) and `current_day` (day of week). "
            "Use these to validate any dates the guest provides:\n"
            "  - If a check-in or check-out date is in the past (before current_date), do NOT store it in "
            "pending_data_updates. Keep those slots in missing_fields and ask for a valid future date.\n"
            "  - If pending_data_raw already contains a past date for check_in or check_out, treat those "
            "slots as still-empty and prompt the guest to correct them — do NOT re-use those values.\n"
            "  - When the guest provides a new date after a previous one was rejected, accept the new date "
            "as the correction. Do NOT compare it against the old rejected date. Simply validate it against "
            "current_date and, if valid, store it in pending_data_updates.\n"
            "  - Be tolerant of minor typos in month names (e.g. 'match' → 'march', 'jaunary' → 'january'). "
            "If the intent is clear, parse the date rather than asking for clarification.\n"
            "  - If check-out is on the same day or before check-in, flag it and ask for correction.\n"
            "  - If the guest says 'tomorrow', 'next Friday', 'next week', etc., resolve against current_date "
            "and store the resolved YYYY-MM-DD value.\n"
            "STAY-DATE BOUNDARY VALIDATION (pre_checkin / during_stay / post_checkout):\n"
            "  The payload includes the guest's booking dates in known_context or memory_facts "
            "(check_in / check_out or stay_checkin_date / stay_checkout_date).\n"
            "  - pre_checkin & during_stay: Any service date (event date, spa appointment, restaurant reservation, "
            "cab pickup, etc.) MUST fall within the guest's check-in and check-out dates (inclusive).\n"
            "    If the guest provides a date outside their stay, do NOT store it. Politely inform them: "
            "'Your stay is from [check-in] to [check-out] — could you pick a date within that period?'\n"
            "  - post_checkout: Service dates (e.g. lost & found pickup) must be on or after the check-out date.\n"
            "  - If booking dates are not available in the payload, skip this validation.\n"
            "PHONE NUMBER VALIDATION:\n"
            "  - A valid phone number must contain exactly 10 digits (digits only, ignoring spaces/dashes/brackets).\n"
            "  - Reject obvious fake or garbage numbers (e.g., all identical digits like 9999999999, sequential digits like 1234567890, or common test numbers).\n"
            "  - If the guest provides an invalid or garbage phone number, do NOT store it in pending_data_updates. "
            "Keep the phone field in missing_fields and politely ask the guest to provide a real, valid 10-digit phone number.\n"
            "  - If pending_data_raw already contains an invalid phone number, treat that slot as still-empty."
        )

        # ── RESPONSE SCHEMA ───────────────────────────────────────────────────
        lines.append(
            "\nResponse schema (return this exact structure):\n"
            "{\n"
            '  "action": "respond_only|collect_info|create_ticket|cancel_pending",\n'
            '  "response_text": "your reply to the guest",\n'
            '  "relevant_to_service": true,\n'
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
            '    "priority": "low|medium|high|critical",\n'
            '    "department_id": "pick the numeric id from available_departments that best fits this service",\n'
            '    "department_name": "the matching department name"\n'
            '  },\n'
            '  "metadata": {"context_switched": false}\n'
            "}"
        )

        # ── FORM-FLOW ACTION RULE ────────────────────────────────────────────
        lines.append(
            "\n=== FORM-FLOW ACTION RULE ===\n"
            "CRITICAL: When the guest has confirmed their choice and you are ready for them to fill in "
            "booking/order details, you MUST set action='collect_info' and state='awaiting_info'. "
            "List all uncollected required slots in missing_fields.\n"
            "Do NOT use action='respond_only' if your response text directs the guest to fill in a form "
            "or provide their details. The form will only appear when action='collect_info'.\n"
            "If you say 'please fill in the details below' but set action='respond_only', the form will NOT "
            "appear and the guest will be confused."
        )

        # ── PENDING DATA COMPLETENESS RULE ───────────────────────────────────
        lines.append(
            "\n=== PENDING DATA COMPLETENESS RULE ===\n"
            "pending_data_updates MUST always include EVERY field value you currently know — "
            "not just values collected this turn. If the guest chose a room type earlier and you "
            "are now asking for dates, pending_data_updates must still contain the room type. "
            "Omitting a known value causes the system to lose track of it. "
            "Only omit a field if it is genuinely unknown or listed in missing_fields."
        )

        return "\n".join(lines)

    @staticmethod
    def _build_service_runtime_json_contract(
        *,
        confirmation_pending_action: str,
        current_phase_id: str = "",
        ticketing_mode: str = "",
        trigger_field_id: str = "",
        trigger_field_label: str = "",
        form_field_names: list[str] | None = None,
        service_type: str = "",
        service_cuisine: str = "",
        service_description: str = "",
    ) -> str:
        confirm_action = str(confirmation_pending_action or "confirm_booking").strip() or "confirm_booking"
        _phase = str(current_phase_id or "").strip()
        _tm = str(ticketing_mode or "").strip().lower()
        _is_form_mode = _tm == "form"
        if _phase in ("during_stay", "post_checkout"):
            _guest_facts_rule = (
                "=== GUEST FACTS RULE (MANDATORY) ===\n"
                "The guest is checked in (or has checked out). The hotel already has their name, room number, phone, and email.\n"
                "NEVER ask for guest_name, room_number, phone, or email — these are on file.\n"
                "Only collect service-specific details: date, time, item/treatment choice, special requests.\n"
                "ALWAYS check known_context FIRST before asking for any field — if it is there, use it directly.\n\n"
            )
        elif _phase == "pre_checkin":
            _guest_facts_rule = (
                "=== GUEST FACTS RULE (MANDATORY) ===\n"
                "The guest has a confirmed reservation. The hotel has their name, phone, and email from the booking.\n"
                "NEVER ask for guest_name, phone, or email if they are in known_context.\n"
                "Room number may not be assigned yet — only ask if genuinely needed and absent from known_context.\n"
                "ALWAYS check known_context FIRST before asking for any field.\n\n"
            )
        else:
            _guest_facts_rule = (
                "=== GUEST FACTS RULE (MANDATORY) ===\n"
                "ALWAYS check known_context FIRST before asking for any field.\n"
                "If guest_name, room_number, phone, or email are in known_context — use them directly, NEVER ask again.\n"
                "Only collect contact info (name, phone, email) when genuinely required and NOT already known.\n\n"
            )

        _validation_rules = (
            "=== DATE & INPUT VALIDATION (MANDATORY) ===\n"
            "The payload includes `current_date` (today's date) and `current_day` (day of week). "
            "Use these to validate any dates the guest provides:\n"
            "  - If a check-in or check-out date is in the past (before current_date), do NOT store it in "
            "pending_data_updates. Keep those slots in missing_fields and ask for a valid future date.\n"
            "  - If pending_data_raw already contains a past date for check_in or check_out, treat those "
            "slots as still-empty and prompt the guest to correct them — do NOT re-use those values.\n"
            "  - When the guest provides a new date after a previous one was rejected, accept the new date "
            "as the correction. Do NOT compare it against the old rejected date. Simply validate it against "
            "current_date and, if valid, store it in pending_data_updates.\n"
            "  - Be tolerant of minor typos in month names (e.g. 'match' → 'march', 'jaunary' → 'january'). "
            "If the intent is clear, parse the date rather than asking for clarification.\n"
            "  - If check-out is on the same day or before check-in, flag it and ask for correction.\n"
            "  - If the guest says 'tomorrow', 'next Friday', 'next week', etc., resolve against current_date "
            "and store the resolved YYYY-MM-DD value.\n"
            "STAY-DATE BOUNDARY VALIDATION (pre_checkin / during_stay / post_checkout):\n"
            "  The payload includes the guest's booking dates in known_context or memory_facts "
            "(check_in / check_out or stay_checkin_date / stay_checkout_date).\n"
            "  - pre_checkin & during_stay: Any service date (event date, spa appointment, restaurant reservation, "
            "cab pickup, etc.) MUST fall within the guest's check-in and check-out dates (inclusive).\n"
            "    If the guest provides a date outside their stay, do NOT store it. Politely inform them: "
            "'Your stay is from [check-in] to [check-out] — could you pick a date within that period?'\n"
            "  - post_checkout: Service dates (e.g. lost & found pickup) must be on or after the check-out date.\n"
            "  - If booking dates are not available in the payload, skip this validation.\n"
            "PHONE NUMBER VALIDATION:\n"
            "  - A valid phone number must contain exactly 10 digits (digits only, ignoring spaces/dashes/brackets).\n"
            "  - Reject obvious fake or garbage numbers (e.g., all identical digits like 9999999999, sequential digits like 1234567890, or common test numbers).\n"
            "  - If the guest provides an invalid or garbage phone number, do NOT store it in pending_data_updates. "
            "Keep the phone field in missing_fields and politely ask the guest to provide a real, valid 10-digit phone number.\n"
            "  - If pending_data_raw already contains an invalid phone number, treat that slot as still-empty.\n\n"
        )

        _json_shape = (
            "=== RUNTIME OUTPUT CONTRACT (MANDATORY) ===\n"
            "Return STRICT JSON only.\n"
            "Use exactly this JSON object shape:\n"
            "{\n"
            '  "action": "respond_only|collect_info|create_ticket|cancel_pending",\n'
            '  "response_text": "your reply to the guest",\n'
            '  "relevant_to_service": true,\n'
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
            '    "priority": "low|medium|high|critical",\n'
            '    "department_id": "numeric id from available_departments that best fits this service",\n'
            '    "department_name": "the matching department name"\n'
            "  },\n"
            '  "metadata": {"context_switched": false}\n'
            "}\n"
            "relevant_to_service: set to false ONLY when the guest's message is entirely outside "
            "this service's domain AND you cannot make meaningful progress. When false, also set "
            "metadata.context_switched=true so the main orchestrator re-routes the query. "
            "Default is true — most messages, even adjacent questions, should be handled here.\n"
        )

        # ── Ticketing flow rules: FORM-based vs TEXT-based ──
        if _is_form_mode:
            _trigger_id = str(trigger_field_id or "").strip()
            _trigger_label = str(trigger_field_label or _trigger_id).strip()
            _form_fields_hint = ""
            if form_field_names:
                _form_fields_hint = (
                    f"The form will collect these fields automatically: {', '.join(form_field_names)}. "
                    "Do NOT ask for any of these in conversation — the form handles them.\n"
                )
            _ticketing_rules = (
                "=== FORM-BASED TICKETING RULES (MANDATORY — THIS SERVICE USES A FORM) ===\n"
                "The payload also includes `service_form_trigger_field`, `service_form_fields`, and "
                "`service_form_instructions`. Use those exact definitions as the source of truth for the form.\n"
                "This service uses an INLINE FORM for collecting guest details. The form appears automatically "
                "in the UI once you signal readiness. Your job is to guide the guest to choose their option, "
                "confirm the choice, and then let the form take over. NEVER ask the guest to type 'yes confirm' "
                "or any confirmation phrase — the form submission IS the confirmation.\n\n"
                "HOW THE FORM TRIGGER WORKS (understand this to set the right state):\n"
                "The system shows the form when ALL of these are true:\n"
                "  1. action = 'collect_info'\n"
                f"  2. pending_data_updates contains the trigger field '{_trigger_id}' with the guest's chosen value\n"
                "  3. missing_fields does NOT contain the trigger field\n"
                "If any condition is missing, the form will NOT appear and the guest sees a dead end.\n\n"
                "STEP-BY-STEP FLOW:\n"
                f"  Step 1 — PRESENT OPTIONS: When the guest asks about this service, show all available "
                f"options from the KB. Let them browse and choose.\n"
                f"  Step 2 — CONFIRM CHOICE: Once the guest picks an option (e.g. a room type, treatment, "
                f"terminal), give a warm 1-2 sentence confirmation highlighting key features of their choice. "
                f"End with a line like 'Please fill in the booking details below.' to signal the form.\n"
                f"  Step 3 — TRIGGER THE FORM: In the same response where you confirm the choice, set:\n"
                f"    - action = 'collect_info'\n"
                f"    - pending_action = 'collect_form_details'\n"
                f"    - pending_data_updates must include '{_trigger_id}' with the guest's chosen value\n"
                f"    - missing_fields = [] (empty — the form handles remaining fields)\n"
                f"  Step 4 — AFTER FORM SUBMISSION: The system creates the ticket automatically from the form "
                f"data. You do not need to create the ticket yourself.\n\n"
                f"{_form_fields_hint}"
                "CRITICAL DO-NOTs FOR FORM MODE:\n"
                "  - NEVER set pending_action to 'confirm_booking' or any confirm_* value\n"
                "  - NEVER ask the guest to say 'yes confirm', 'confirm', or any confirmation phrase\n"
                "  - NEVER ask for form field values (dates, name, phone, etc.) in conversation\n"
                "  - NEVER set action to 'create_ticket' — the form submission handles ticket creation\n"
                "  - NEVER hold back the form waiting for more conversational input after the trigger choice is made\n\n"
                "SMART STATE HANDLING:\n"
                "  - Guest is browsing / asking questions about options → action='respond_only', answer from KB\n"
                "  - Guest selects a SPECIFIC option (names it, points to it like 'the first one', "
                "'Terminal 1', 'the prestige suite', 'candlelight therapy') → confirm + trigger form (Step 3)\n"
                "  - Guest changes mind about their choice → acknowledge, present options again, "
                "do NOT trigger the form yet\n"
                "  - Guest asks an unrelated question mid-flow → answer if within your domain, "
                "or set context_switched=true\n"
                "  - Guest provides the trigger value upfront (e.g. 'book candlelight therapy') → "
                "skip browsing, confirm + trigger form immediately\n"
                "  CRITICAL — INTENT vs SELECTION (use conversation context to decide):\n"
                f"  The trigger field is '{_trigger_id}'. You can ONLY trigger the form when you know the "
                "SPECIFIC value for this field. Look at the conversation history to decide:\n"
                "    - If your last message asked 'Would you like to book?' or offered to help, and the "
                "guest says 'yes' → that is booking INTENT only. You still need to ask which specific "
                f"'{_trigger_id}' they want. Set the trigger value to the ACTUAL option, not 'yes'.\n"
                "    - If your last message asked the guest to choose between specific options (e.g. "
                f"'Which {_trigger_id}?') or confirmed a specific option (e.g. 'Shall I go ahead with "
                "Terminal 1?'), and the guest says 'yes' → that IS a selection. Set the trigger value "
                "to the specific option from your last message (e.g. 'Terminal 1'), NOT 'yes'.\n"
                "  KEY RULE: The trigger field value must ALWAYS be the actual option name/identifier "
                "(e.g. 'Terminal 1', 'Prestige Suite', 'Swedish Massage'), NEVER a confirmation word "
                "like 'yes', 'ok', 'sure'. Derive the real value from conversation context.\n"
                "  When there is only ONE option in the KB, 'yes' after presenting it means that option "
                "— set the trigger to the option's actual name.\n"
            )
        else:
            _ticketing_rules = (
                f"When waiting for guest confirmation before ticket creation, set pending_action to \"{confirm_action}\".\n"
                "CONFIRMATION RULES (mandatory):\n"
                f"- NEVER ask for confirmation (pending_action=\"{confirm_action}\") while ANY required field is still missing.\n"
                "  'Required fields' includes BOTH structured required_slots AND any details mentioned in the admin\n"
                "  ticketing condition text (the 'What to collect before raising a ticket' section above).\n"
                "  Collect ALL missing fields first. Only transition to the confirmation step when every required field has a value.\n"
                f"- When asking for confirmation (pending_action=\"{confirm_action}\"), response_text MUST list the collected "
                "details that are RELEVANT TO THIS SERVICE from pending_data_raw.\n"
                "  Show ONLY the fields that the guest provided or that are specific to this service.\n"
                "  For food/dining orders: ordered items with quantities, delivery time, total price, special instructions.\n"
                "  For room bookings: guest name, room type, check-in/check-out dates, guest count, etc.\n"
                "  For spa/restaurant: treatment/restaurant name, date, time, party size, etc.\n"
                "  Do NOT include internal booking metadata (guest ID, stay dates, confirmation codes) unless\n"
                "  the guest explicitly provided them during THIS conversation for THIS service.\n"
                "  Never say 'the above details' — always spell each one out.\n"
                "- When action=\"create_ticket\" (guest confirmed), response_text MUST again include ALL collected "
                "details relevant to this service from pending_data_raw. Do not include unrelated known_context fields.\n"
            )

        _common_rules = (
            "PRESENT BEFORE ASK RULE (mandatory):\n"
            "Before asking the guest to choose a menu item, treatment, room type, package, or any option from a list —\n"
            "FIRST share the full list of available options from the KB. Never ask 'which one?' before showing the options.\n"
            "DEPARTMENT ASSIGNMENT (mandatory when creating a ticket):\n"
            "The payload includes available_departments — a list of {department_id, department_name} objects.\n"
            "When setting ticket.ready_to_create=true, set ticket.department_id to the numeric id of the department\n"
            "that best matches this service (e.g. spa → Wellness, dining → F&B, room issue → Housekeeping).\n"
            "If available_departments is empty or no match, leave department_id empty.\n"
            "PENDING DATA COMPLETENESS (mandatory):\n"
            "pending_data_updates MUST always include EVERY field value you currently know — "
            "not just values collected this turn. If the guest chose a room type on a previous turn "
            "and you are now asking for dates, pending_data_updates must still contain the room type. "
            "Omitting a known value causes the system to lose track of it. "
            "Only omit a field if it is genuinely unknown or listed in missing_fields."
        )

        # Food / dining multi-item ordering rules — the LLM decides if they apply
        # based on the service context it already has (type, cuisine, description, KB).
        _svc_type = str(service_type or "").strip()
        _svc_cuisine = str(service_cuisine or "").strip()
        _svc_desc = str(service_description or "").strip()
        _food_context_hint = ""
        if _svc_type:
            _food_context_hint += f"Service type: {_svc_type}. "
        if _svc_cuisine:
            _food_context_hint += f"Cuisine: {_svc_cuisine}. "
        if _svc_desc:
            _food_context_hint += f"Description: {_svc_desc}. "

        _food_ordering_rules = (
            "\n=== FOOD & DINING MULTI-ITEM ORDERING (apply ONLY if this is a food/dining service) ===\n"
            f"{_food_context_hint}\n"
            "If this service involves food ordering, in-room dining, restaurant ordering, or any "
            "menu-based ordering (you can tell from the service name, type, cuisine, description, "
            "or KB content containing menus/dishes/food items), apply these rules:\n"
            "  1. When the guest mentions a dish, verify it exists in the KB menu. If not found, "
            "politely inform and suggest similar items from the menu.\n"
            "  2. After acknowledging each item, ALWAYS ask 'Would you like anything else?' or "
            "'Is there anything else you would like to add?'. Do NOT rush to the form or "
            "confirmation after just one item — guests often order multiple items.\n"
            "  3. Keep a running tally of ordered items and mention it naturally "
            "(e.g. 'So far I have 1x Margherita Pizza. Anything else?').\n"
            "  4. Only proceed to the form/confirmation step when the guest explicitly signals "
            "they are done (e.g. 'that is all', 'nothing else', 'no thanks', 'just that', "
            "'I am done'). A single item mention is NOT a signal to finalize.\n"
            "  5. For form-mode: set the trigger value to the FULL ORDER SUMMARY "
            "(e.g. '2x Margherita Pizza, 1x Caesar Salad, 1x Mango Smoothie'). "
            "For text-mode: collect remaining details only after the order is finalized.\n"
            "  6. If the guest asks about ingredients, allergens, or dietary suitability, "
            "answer from KB data before adding the item to the order.\n"
            "If this is NOT a food/dining service, IGNORE this entire section.\n"
        )

        return (
            _guest_facts_rule
            + _validation_rules
            + _json_shape
            + _ticketing_rules
            + _common_rules
            + _food_ordering_rules
        )

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
        if extracted_knowledge:
            extracted_knowledge = self._scope_service_knowledge_to_runtime_property(
                service_knowledge=extracted_knowledge,
                user_message=user_message,
                context=context,
            )
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
        # Confirmation is required for text-mode ticketing — the guest must confirm
        # before a ticket is raised.  For form-mode services, the form submission
        # itself acts as confirmation, so we skip the "yes confirm" step entirely.
        _svc_ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
        requires_confirmation_step = ticketing_enabled and _svc_ticketing_mode != "form"

        # Extract form_config details for form-mode services (used by runtime contract)
        _svc_form_config = service.get("form_config") if isinstance(service.get("form_config"), dict) else {}
        _svc_trigger_field = _svc_form_config.get("trigger_field") if isinstance(_svc_form_config.get("trigger_field"), dict) else {}
        _svc_trigger_field_id = str(_svc_trigger_field.get("id") or "").strip()
        _svc_trigger_field_label = str(_svc_trigger_field.get("label") or _svc_trigger_field_id).strip()
        _svc_form_field_names: list[str] = []
        _svc_form_fields_payload: list[dict[str, Any]] = []
        for _ff in (_svc_form_config.get("fields") or []):
            if not isinstance(_ff, dict):
                continue
            _field_id = str(_ff.get("id") or "").strip()
            _field_label = str(_ff.get("label") or _field_id).strip()
            if _field_label:
                _svc_form_field_names.append(_field_label)
            if _field_id:
                _svc_form_fields_payload.append({
                    "id": _field_id,
                    "label": _field_label,
                    "type": str(_ff.get("type") or "text").strip(),
                    "required": bool(_ff.get("required", True)),
                    "validation_prompt": str(_ff.get("validation_prompt") or "").strip(),
                })
        _svc_form_field_names = [n for n in _svc_form_field_names if n]

        # For form-mode services, suppress confirmation phrase — form submission is the confirmation
        if _svc_ticketing_mode == "form":
            confirmation_phrase = ""

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
        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        selected_property_hint = str(
            pending_raw.get("_selected_property_scope_id")
            or pending_raw.get("_selected_property_scope_name")
            or ""
        ).strip()
        known_property_scope_count = int(pending_raw.get("_known_property_scope_count") or 0)
        needs_property_scoped_kb_support = bool(
            selected_property_hint
            and known_property_scope_count > 1
            and "=== PROPERTY:" not in str(extracted_knowledge or "")
        )
        # Service agent uses ONLY its own extracted_knowledge — no full-KB fallback.
        # The per-file extraction pipeline ensures extracted_knowledge is complete
        # with per-property headers. No runtime chunk scanning needed.
        full_kb_text = ""  # kept for backward compat with _build_service_system_prompt

        # Prefer LLM-generated prompt if available; append KB knowledge so the
        # agent always has access to its data even when using the generated prompt.
        _generated_prompt = str(service.get("generated_system_prompt") or "").strip()
        if _generated_prompt:
            kb_section = str(extracted_knowledge or "").strip()
            if kb_section:
                system_prompt = _generated_prompt + "\n\n=== KNOWLEDGE BASE ===\n" + kb_section
            else:
                system_prompt = _generated_prompt
            system_prompt = (
                f"{system_prompt}\n\n"
                "=== COMPLETENESS RULE ===\n"
                "When the guest asks about rooms, options, packages, amenities, services, or any list of offerings:\n"
                "- List ALL items from the knowledge base — never summarize, skip, or show only a partial list.\n"
                "- Include ALL details for each item (price, description, features, capacity, etc.) as given in the KB.\n"
                "- Do NOT say 'and more' or 'among others' — be exhaustive.\n\n"
                "=== RUNTIME RULES (OVERRIDE — these take precedence over any earlier instructions) ===\n"
                f"{self._build_service_runtime_json_contract(confirmation_pending_action=confirmation_pending_action, current_phase_id=selected_phase_id, ticketing_mode=_svc_ticketing_mode, trigger_field_id=_svc_trigger_field_id, trigger_field_label=_svc_trigger_field_label, form_field_names=_svc_form_field_names, service_type=str(service.get('type') or '').strip(), service_cuisine=service_cuisine, service_description=description)}"
            )
        else:
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
                current_phase_id=selected_phase_id,
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

        _now_svc = datetime.now(UTC)
        # Build known_context: merge memory facts + public pending_data as pre-filled guest slots
        _mem_facts = memory_snapshot.get("facts", {})
        _known_ctx: dict[str, Any] = {}
        _internal_booking_ref: dict[str, Any] = {}
        # Keys that are internal booking/stay metadata — useful for date-based logic
        # but should NEVER be displayed to the guest in confirmation messages.
        _booking_metadata_keys = {
            "guest_id", "booking_id", "reservation_id", "confirmation_code",
            "booking_confirmation_code", "booking_check_in_date", "booking_check_out_date",
            "booking_guest_name", "booking_property_name", "booking_room_type",
            "stay_check_in_date", "stay_check_out_date", "stay_date_range",
            "check_in_date", "check_out_date", "checkin_date", "checkout_date",
            "check_in", "check_out",
        }
        if isinstance(_mem_facts, dict):
            for _k, _v in _mem_facts.items():
                if _v is not None and str(_v).strip():
                    _key_lower = str(_k).strip().lower()
                    if _key_lower in _booking_metadata_keys:
                        _internal_booking_ref[str(_k)] = _v
                    else:
                        _known_ctx[str(_k)] = _v
        for _k, _v in pending_pub.items():
            if _v is not None and str(_v).strip() and not str(_k).startswith("_"):
                _known_ctx[str(_k)] = _v

        # Merge booking context fields into internal booking reference (not known_context)
        for _bk_attr in (
            "booking_guest_name", "booking_property_name", "booking_room_type",
            "booking_check_in_date", "booking_check_out_date", "booking_confirmation_code",
        ):
            _bk_val = getattr(context, _bk_attr, None)
            if _bk_val:
                _internal_booking_ref[_bk_attr] = str(_bk_val)
        if getattr(context, "room_number", None):
            _known_ctx["room_number"] = context.room_number
        if getattr(context, "guest_name", None):
            _known_ctx["guest_name"] = context.guest_name

        # Fetch available departments (best-effort) so LLM can assign the right one to the ticket.
        _available_departments: list[dict[str, Any]] = []
        if ticketing_enabled:
            try:
                from integrations.lumira_ticketing_repository import lumira_ticketing_repository as _ltr
                from models.database import AsyncSessionLocal as _ASL
                _integration_ctx = {}
                _pd = context.pending_data if isinstance(context.pending_data, dict) else {}
                _int_raw = _pd.get("_integration")
                if isinstance(_int_raw, dict):
                    _integration_ctx = _int_raw
                _entity_id = (
                    _integration_ctx.get("entity_id")
                    or _integration_ctx.get("organisation_id")
                    or _integration_ctx.get("org_id")
                )
                if _entity_id:
                    async with _ASL() as _db:
                        _available_departments = await _ltr.fetch_departments_of_entity(_db, _entity_id)
            except Exception:
                pass  # department list is best-effort; empty list = LLM leaves department_id blank

        payload = {
            "user_message": str(user_message or ""),
            "current_date": _now_svc.strftime("%Y-%m-%d"),
            "current_day": _now_svc.strftime("%A"),
            "current_time": _now_svc.strftime("%H:%M"),
            "current_timezone": "UTC",
            "is_first_turn": is_first_service_turn,
            "current_phase_id": selected_phase_id,
            "service_phase_id": self._normalize_identifier(service.get("phase_id")),
            "pending_action": str(context.pending_action or ""),
            "pending_data_collected": collected_labels,
            "pending_data_raw": self._sanitize_json(pending_pub),
            "known_context": self._sanitize_json(_known_ctx),
            "_internal_booking_reference": self._sanitize_json(_internal_booking_ref),
            "history": history_bundle.get("history_last_10", []),
            "history_last_10": history_bundle.get("history_last_10", []),
            "full_history_context": history_bundle.get("full_history_context", {}),
            "last_user_message": str(history_bundle.get("last_user_message") or ""),
            "last_assistant_message": str(history_bundle.get("last_assistant_message") or ""),
            "memory_summary": str(memory_snapshot.get("summary") or "")[:1200],
            "memory_facts": self._sanitize_json(_mem_facts),
            "confirmation_phrase": confirmation_phrase,
            "available_departments": _available_departments,
            "service_ticketing_mode": _svc_ticketing_mode,
            "service_form_trigger_field": self._sanitize_json({
                "id": _svc_trigger_field_id,
                "label": _svc_trigger_field_label,
                "description": str(_svc_trigger_field.get("description") or "").strip(),
            }),
            "service_form_fields": self._sanitize_json(_svc_form_fields_payload),
            "service_form_instructions": str(_svc_form_config.get("pre_form_instructions") or "").strip(),
        }
        # Inject booking context + phase rules into system prompt
        _booking_block = self._build_booking_context_prompt_block(context)
        if _booking_block:
            system_prompt = system_prompt + "\n" + _booking_block

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        _prompt_source = "generated" if str(service.get("generated_system_prompt") or "").strip() else "template"
        _log_llm_call(
            label=f"SERVICE AGENT [{service_id}] (prompt:{_prompt_source})",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt=system_prompt,
            payload=payload,
        )
        raw = await self._chat_with_json(
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=4096,
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

        if _svc_ticketing_mode == "form":
            decision.response_text = strip_form_confirmation_instructions(decision.response_text)
            decision.pending_data_updates, _trigger_alias_key, _ = canonicalize_trigger_pending_data(
                decision.pending_data_updates,
                _svc_trigger_field_id,
                _svc_trigger_field_label,
            )
            _, _trigger_value = resolve_trigger_field_value(
                decision.pending_data_updates,
                _svc_trigger_field_id,
                _svc_trigger_field_label,
            )
            decision.missing_fields = normalize_trigger_missing_fields(
                decision.missing_fields,
                _svc_trigger_field_id,
                _svc_trigger_field_label,
                trigger_value_present=bool(str(_trigger_value or "").strip()),
            )

            # ── FORM-MODE ENFORCEMENT ──
            # The LLM is instructed to never set confirm_* or create_ticket for
            # form-mode services, but it sometimes ignores instructions.  Enforce
            # the contract at runtime so the frontend always gets the right state.
            # The LLM has full conversation history and decides what the trigger
            # value is — we trust its judgment on whether "yes" means a specific
            # option or generic intent.

            _form_trigger_present = bool(str(_trigger_value or "").strip())
            _pa_lower = str(decision.pending_action or "").strip().lower()

            # Strip confirm_* pending_action — form submission IS the confirmation
            if _pa_lower.startswith("confirm_"):
                decision.pending_action = "collect_form_details" if _form_trigger_present else None
                decision.metadata["form_mode_stripped_confirm_pending_action"] = _pa_lower

            # Strip create_ticket action — form submission handles ticket creation
            if str(decision.action or "").strip().lower() == "create_ticket":
                decision.action = "collect_info" if _form_trigger_present else "respond_only"
                if hasattr(decision, "ticket") and hasattr(decision.ticket, "ready_to_create"):
                    decision.ticket.ready_to_create = False
                decision.metadata["form_mode_stripped_create_ticket"] = True

            # When trigger value is present and no missing fields → ensure action
            # and pending_action are set correctly so the frontend shows the form.
            if _form_trigger_present and not decision.missing_fields:
                if str(decision.action or "").strip().lower() not in ("collect_info",):
                    decision.action = "collect_info"
                    decision.metadata["form_mode_forced_collect_info"] = True
                if not str(decision.pending_action or "").strip():
                    decision.pending_action = "collect_form_details"

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
        if requires_confirmation_step:
            for _msg in reversed(context.messages):
                _role = str(getattr(_msg, "role", "") or "").lower()
                if "assistant" in _role:
                    _content = str(getattr(_msg, "content", "") or "").lower()
                    # Match if last bot message asked for confirmation (contains "confirm" broadly)
                    if "confirm" in _content or confirmation_compact in _content:
                        last_bot_asked_confirmation = True
                    break

        if requires_confirmation_step and (awaiting_confirmation or last_bot_asked_confirmation) and user_compact == confirmation_compact:
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

        # Gate: intercept premature ticket creation — force confirmation step first.
        # Fires when the LLM returned create_ticket but the guest never saw a summary
        # and confirmed explicitly (i.e. we haven't been in a confirm_* pending state).
        if (
            requires_confirmation_step
            and not awaiting_confirmation
            and user_compact != confirmation_compact
            and not bool(decision.requires_human_handoff)
            and str(decision.action or "").strip().lower() == "create_ticket"
            and bool(getattr(decision.ticket, "ready_to_create", False))
        ):
            decision.action = "collect_info"
            decision.pending_action = confirmation_pending_action
            decision.ticket.ready_to_create = False
            # Build summary from context pending_data + any new updates the LLM provided
            merged = dict(self._coerce_public_pending(context.pending_data))
            if isinstance(decision.pending_data_updates, dict):
                for k, v in decision.pending_data_updates.items():
                    if str(v or "").strip():
                        merged[k] = v
            skip_keys = {"service_id", "service_name"}
            detail_parts = [
                f"- {k.replace('_', ' ').title()}: {v}"
                for k, v in merged.items()
                if k not in skip_keys and str(v or "").strip()
            ]
            existing = str(decision.response_text or "").strip()
            confirmation_ask = "Please confirm the above to proceed."
            if detail_parts:
                summary_block = "\n".join(detail_parts)
                if existing and "confirm" not in existing.lower():
                    decision.response_text = f"{existing}\n\n{summary_block}\n\n{confirmation_ask}"
                elif existing:
                    decision.response_text = f"{existing}\n\n{confirmation_ask}"
                else:
                    decision.response_text = f"{summary_block}\n\n{confirmation_ask}"
            else:
                if existing and "confirm" not in existing.lower():
                    decision.response_text = f"{existing}\n\n{confirmation_ask}"
                else:
                    decision.response_text = existing or confirmation_ask
            decision.metadata["confirmation_gate_forced"] = True

        if (
            requires_confirmation_step
            and active_flow_signal
            and not awaiting_confirmation
            and user_compact != confirmation_compact
            and not bool(decision.requires_human_handoff)
            and not bool(decision.missing_fields)
            and not bool(getattr(decision.ticket, "ready_to_create", False))
            and not str(decision.pending_action or "").strip()
            and not bool(decision.metadata.get("confirmation_override"))
        ):
            _action_lower = str(decision.action or "").strip().lower()
            # collect_info with no missing fields → LLM finished collecting → confirm
            # respond_only → only confirm if the LLM actually collected new field
            #   data this turn (otherwise it's just answering a side question)
            _should_force_confirm = False
            if _action_lower == "collect_info":
                _should_force_confirm = True
            elif _action_lower == "respond_only":
                _new_updates = decision.pending_data_updates if isinstance(decision.pending_data_updates, dict) else {}
                _meaningful = {
                    k: v for k, v in _new_updates.items()
                    if str(v or "").strip() and k not in {"service_id", "service_name"}
                }
                _should_force_confirm = bool(_meaningful)
            if _should_force_confirm:
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

        try:
            from services.flow_logger import log_service_runtime

            missing_fields = []
            for field in list(getattr(decision, "missing_fields", []) or []):
                text = str(field or "").strip()
                if text:
                    missing_fields.append(text)
            log_service_runtime(
                session_id=str(getattr(context, "session_id", "") or ""),
                service_id=service_id,
                service_name=service_name,
                phase_id=str(selected_phase_id or ""),
                prompt_source=_prompt_source,
                extracted_knowledge_chars=len(extracted_knowledge),
                generated_prompt_chars=len(_generated_prompt),
                full_kb_fallback_chars=len(full_kb_text),
                pending_action_before=str(context.pending_action or ""),
                response_action=str(getattr(decision, "action", "") or ""),
                missing_fields=missing_fields,
                ticket_ready_to_create=bool(getattr(decision.ticket, "ready_to_create", False)),
                context_switched=bool((decision.metadata or {}).get("context_switched")),
            )
        except Exception:
            pass

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
        # ── Property-scoped KB loading ──────────────────────────────────
        # Instead of loading ALL KB files into one blob and trying to scope
        # after the fact, we use the property index to load ONLY the KB pages
        # for properties the guest is currently discussing.
        #
        # Three scenarios:
        #   1) Single property (1 KB file)  → load full KB as before
        #   2) Multi-property, active IDs known (turn 2+) → load only those
        #   3) Multi-property, no active IDs (turn 1) → manifest + full pipeline
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        _pending = context.pending_data
        _cached_property_count = int(_pending.get("_known_property_scope_count") or 0)
        _cached_active_ids = self._normalize_property_id_list(list(_pending.get("_active_property_ids") or []))
        _cached_active_names = [
            str(name).strip()
            for name in list(_pending.get("_active_property_names") or [])
            if str(name).strip()
        ]
        _runtime_past_ctx = dict(_pending.get("_past_property_context") or {})
        # Legacy fallback — if old-style single fields exist but new array doesn't
        if not _cached_active_ids:
            _legacy_id = str(_pending.get("_selected_property_scope_id") or "").strip()
            _legacy_name = str(_pending.get("_selected_property_scope_name") or "").strip()
            if _legacy_id:
                _cached_active_ids = [_legacy_id]
                _cached_active_names = [_legacy_name] if _legacy_name else [_legacy_id]
        _cached_active_ids, _prefetch_pin_applied = self._apply_stay_property_pin(
            context=context,
            selected_phase_id=selected_phase_id,
            property_ids=_cached_active_ids,
            source="pre_kb_scope",
        )
        if _prefetch_pin_applied:
            _stay_property_name = str(_pending.get("_stay_property_name") or "").strip()
            if _stay_property_name and _stay_property_name not in _cached_active_names:
                _cached_active_names = [_stay_property_name, *_cached_active_names]
            context.pending_data["_active_property_ids"] = list(_cached_active_ids)
            if _cached_active_names:
                context.pending_data["_active_property_names"] = list(_cached_active_names)

        if _cached_property_count > 1 and _cached_active_ids:
            # ── Fast path: multi-property, active IDs known ──
            # Load ONLY the active properties' KB pages via property index.
            # No full KB load, no text-matching, no LLM scanning.
            try:
                _scoped_result = await config_service.get_kb_text_for_properties(
                    property_ids=list(_cached_active_ids),
                    include_manifest=True,
                    include_common=True,
                    past_property_context=_runtime_past_ctx if _runtime_past_ctx else None,
                    max_sources=200,
                )
                full_kb_text = str(_scoped_result.get("scoped_kb_text") or "").strip()
            except Exception as _scoped_err:
                print(f"[ORCH] get_kb_text_for_properties failed, falling back to full KB: {_scoped_err}")
                full_kb_text = config_service.get_full_kb_text_with_sources(max_chars=None, max_sources=200)

            _mode = "single" if len(_cached_active_ids) == 1 else "multi"
            property_scope = {
                "mode": _mode,
                "active_property_id": _cached_active_ids[0] if len(_cached_active_ids) == 1 else "",
                "active_property_name": _cached_active_names[0] if len(_cached_active_names) == 1 else "",
                "matched_property_ids": list(_cached_active_ids),
                "history_property_ids": [],
                "selected_property_ids": list(_cached_active_ids),
                "property_manifest": [],
                "expected_property_count": _cached_property_count,
                "detected_property_count": _cached_property_count,
                "count_mismatch": False,
                "requires_clarification": False,
                "clarification_question": "",
            }
            active_property_id = _cached_active_ids[0] if len(_cached_active_ids) == 1 else ""
            active_property_name = _cached_active_names[0] if len(_cached_active_names) == 1 else ""
            known_property_count = _cached_property_count
            property_manifest = []
        else:
            # ── Full pipeline: first turn or single-property setup ──
            # For single-property setups, load the full KB directly.
            # For multi-property turn 1, the pipeline builds manifest + scoped text.
            full_kb_text = config_service.get_full_kb_text_with_sources(
                max_chars=None,
                max_sources=200,
            )
            kb_context = await self._prepare_full_kb_context_for_message(
                user_message=user_message,
                context=context,
                full_kb_text=full_kb_text,
            )
            full_kb_text = str((kb_context or {}).get("full_kb_text") or "").strip()
            property_scope = (kb_context or {}).get("property_scope") if isinstance(kb_context, dict) else {}
            if not isinstance(property_scope, dict):
                property_scope = {}

            active_property_id = str(property_scope.get("active_property_id") or "").strip()
            active_property_name = str(property_scope.get("active_property_name") or "").strip()
            if not isinstance(context.pending_data, dict):
                context.pending_data = {}
            if active_property_id:
                context.pending_data["_selected_property_scope_id"] = active_property_id
                if active_property_name:
                    context.pending_data["_selected_property_scope_name"] = active_property_name
            known_property_count = int(property_scope.get("detected_property_count") or 0)
            if known_property_count > 0:
                context.pending_data["_known_property_scope_count"] = known_property_count
            property_manifest = property_scope.get("property_manifest", []) if isinstance(property_scope, dict) else []

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
                )

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
        stay_property_pin_id = str(_pending.get("_stay_property_id") or "").strip()
        stay_property_pin_name = str(_pending.get("_stay_property_name") or "").strip()
        property_scope_payload = {
            "mode": str(property_scope.get("mode") or "").strip(),
            "active_property_id": active_property_id,
            "active_property_name": active_property_name,
            "matched_property_ids": list(property_scope.get("matched_property_ids") or []),
            "history_property_ids": list(property_scope.get("history_property_ids") or []),
            "selected_property_ids": list(property_scope.get("selected_property_ids") or []),
            "detected_properties": [
                {
                    "id": str(row.get("id") or "").strip(),
                    "name": str(row.get("name") or "").strip(),
                    "aliases": list(row.get("aliases") or []),
                }
                for row in property_manifest[:12]
                if isinstance(row, dict)
            ],
            "expected_property_count": int(property_scope.get("expected_property_count") or 0),
            "detected_property_count": int(property_scope.get("detected_property_count") or 0),
            "count_mismatch": bool(property_scope.get("count_mismatch")),
            "requires_clarification": bool(property_scope.get("requires_clarification")),
            "clarification_question": str(property_scope.get("clarification_question") or "").strip(),
            "stay_property_pin": {
                "enabled": bool(
                    stay_property_pin_id
                    and self._is_stay_property_pin_phase(selected_phase_id)
                ),
                "property_id": stay_property_pin_id,
                "property_name": stay_property_pin_name,
            },
        }
        # Property clarification is now fully LLM-driven.
        # The orchestrator system prompt contains property resolution rules and
        # receives property_scope in the payload — it decides when to ask for
        # clarification vs answer from the full KB. No hardcoded guard needed.
        _now = datetime.now(UTC)
        payload = {
            "trace_id": orchestration_trace_id,
            "timestamp": _now.isoformat(),
            "current_date": _now.strftime("%Y-%m-%d"),
            "current_day": _now.strftime("%A"),
            "user_message": str(user_message or ""),
            "state": context.state.value,
            "pending_action": str(context.pending_action or ""),
            "pending_action_context": self._build_pending_action_context(context),
            "pending_data_public": self._sanitize_json(self._coerce_public_pending(context.pending_data)),
            "last_active_service": {
                "id": str((context.pending_data or {}).get("_last_service_id") or ""),
                "name": str((context.pending_data or {}).get("_last_service_name") or ""),
            },
            "selected_phase": {
                "id": selected_phase_id,
                "name": selected_phase_name,
            },
            "memory_summary": str(memory_snapshot.get("summary") or "")[:6000],
            "memory_facts": self._sanitize_json(memory_snapshot.get("facts", {})),
            "suspended_services": [
                {"service_id": s.get("service_id"), "service_name": s.get("service_name"), "collected_so_far": list((s.get("pending_data") or {}).keys())}
                for s in (context.suspended_services or [])
            ],
            "property_scope": self._sanitize_json(property_scope_payload),
            "active_property_ids": list(_cached_active_ids) if _cached_active_ids else [],
            "past_property_context": _runtime_past_ctx if _runtime_past_ctx else {},
            "full_knowledge_base": full_kb_text or None,
            "history": history_bundle.get("history_last_10", []),
            "history_last_10": history_bundle.get("history_last_10", []),
            "full_history_context": history_bundle.get("full_history_context", {}),
            "last_user_message": str(history_bundle.get("last_user_message") or ""),
            "last_assistant_message": str(history_bundle.get("last_assistant_message") or ""),
            "services": self._sanitize_json([
                {k: v for k, v in svc.items() if k not in ("extracted_knowledge", "generated_system_prompt", "service_prompt_pack")}
                for svc in services_snapshot
                if isinstance(svc, dict)
            ]),
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
                    "KB-grounded generic requests may keep target_service_id empty only when backed by KB evidence.",
                ],
            },
            "complaint_routing_note": (
                "IMPORTANT: 'ticketing_enabled=true' on a service means the service CAN raise a support ticket "
                "if the guest reports a problem with it — it does NOT mean the service is a complaint handler. "
                "Room bookings, food orders, and all other service requests are handled as normal service requests. "
                "Set action=create_ticket in two cases: "
                "(1) The guest needs staff to physically do something during their stay (missing items, maintenance, housekeeping) — use ticket.category='request'. "
                "(2) The guest is upset or something went wrong — use ticket.category='complaint'. "
                "Asking about a service, requesting a booking, or asking for information is NEVER a reason to create a ticket."
            ),
            "response_contract": {
                "normalized_query": "string",
                "intent": "core intent",
                "confidence": "0..1",
                "action": "respond_only|collect_info|dispatch_handler|create_ticket|resume_pending|cancel_pending",
                "target_service_id": "exact service id from services list when service-specific; empty for KB-grounded generic requests",
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
                "selected_property_name": "name of the property the guest is currently discussing, or empty if general/unknown",
                "selected_property_ids": ["property id(s) the guest is interested in — from detected_properties list"],
                "ticket": {
                    "required": "bool",
                    "ready_to_create": "bool",
                    "generic_request": "bool",
                    "phase_applicable": "bool|null",
                    "approval_required": "bool",
                    "reason": "string",
                    "issue": "string",
                    "category": "string",
                    "sub_category": "string",
                    "priority": "low|medium|high|critical",
                    "evidence": ["1-3 short KB facts backing the request"],
                },
                "metadata": {"any": "json"},
            },
        }

        # --- WEATHER INJECTION ---
        user_msg_lower = str(user_message or "").lower()
        if any(w in user_msg_lower for w in ["weather", "temperature", "forecast", "climate", "rain", "sunny"]):
            try:
                from services.weather_service import get_current_weather
                hotel_code_val = getattr(context, "hotel_code", "DEFAULT")
                weather_info = await get_current_weather(hotel_code_val)
                if weather_info:
                    if not isinstance(payload.get("memory_facts"), dict):
                        payload["memory_facts"] = {}
                    payload["memory_facts"]["live_hotel_weather"] = weather_info
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Failed to inject weather into payload: %s", e)
        # -------------------------

        system_prompt = (
            "You are the main concierge assistant for this hotel. "
            "For informational questions, answer fully yourself from the hotel's KB. "
            "For bookings, orders, or service execution, never collect transactional details yourself and route to the right service agent when one is available.\n"
            "You are the single authority on routing and responding — no other layer will override your decision.\n"
            "\n"
            "=== ABSOLUTE LANGUAGE RULE ===\n"
            "NEVER use the words pre_booking, pre_checkin, during_stay, post_checkout, 'current phase', "
            "'this phase', 'not available in this phase', or any internal system terminology in any guest-facing response. "
            "When a service is unavailable right now, phrase it naturally: "
            "'That will be available once you check in', 'We can arrange this during your stay', "
            "'This is something we can help with when you arrive', etc. "
            "Guests must never know about internal phase names.\n"
            "\n"
            "=== SERVICE AVAILABILITY (read before every step) ===\n"
            "The current guest journey phase is in selected_phase.id. "
            "The ONLY services you may dispatch or offer are those listed in allowed_service_ids_in_current_phase. "
            "Services listed in blocked_out_of_phase_services are NOT available now — do NOT dispatch to them, do NOT offer them as options, do NOT ask clarification questions about them. "
            "If the guest asks for something that only maps to blocked_out_of_phase_services: "
            "1) share factual info from the KB about what the service offers, "
            "2) explicitly state that booking/ordering/requesting it is not possible right now, "
            "3) use the 'availability_hint' field from that service entry to tell the guest when it will be available. "
            "Exception: if NO configured service matches, but the KB clearly says the hotel can handle the guest's request now "
            "or through manual staff approval in the guest's current context, you may keep target_service_id empty and handle it "
            "as a KB-grounded generic request. Only do this when you can cite KB evidence; never guess.\n"
            "Phrase this naturally — NEVER mention phase names.\n"
            "\n"
            "=== KNOWLEDGE PRIORITY ===\n"
            "For every factual or informational ask, inspect full_knowledge_base carefully before answering.\n"
            "If the answer exists anywhere in full_knowledge_base, use those specific facts directly.\n"
            "Do not give a generic fallback if full_knowledge_base already contains the needed detail.\n"
            "Prefer complete factual answers over generic summaries. If the KB lists options or specific offerings, name them explicitly.\n"
            "COMPLETENESS: When listing rooms, packages, amenities, or any offerings — list ALL of them from the KB. "
            "Never show a partial list. If the KB has 5 room types, list all 5 with their details. "
            "Do NOT say 'and more' or summarize — be exhaustive.\n"
            "\n"
            "=== PROPERTY RESOLUTION (YOU decide — no hardcoded rules) ===\n"
            "The payload includes:\n"
            "  - property_scope.detected_properties: ALL properties in the KB\n"
            "  - active_property_ids: properties the guest is currently interested in (may be empty, 1, or multiple)\n"
            "  - past_property_context: brief summaries of properties the guest discussed earlier but moved on from\n"
            "  - full_knowledge_base: FULL KB for active properties (or ALL properties if none active yet)\n"
            "Read these AND conversation history BEFORE deciding.\n"
            "\n"
            "CRITICAL — ALWAYS OUTPUT PROPERTY TRACKING:\n"
            "  In EVERY response, set these two fields in your JSON output:\n"
            "  - selected_property_name: the property name the guest is currently interested in (empty string if general/unknown)\n"
            "  - selected_property_ids: array of ALL property IDs the guest is currently interested in (from detected_properties list, empty array if general/unknown)\n"
            "  HOW THIS WORKS:\n"
            "  - These fields control which property KBs the system loads on the NEXT turn.\n"
            "  - If you set selected_property_ids to ['mumbai_orchid', 'ira_orchid_mumbai'], BOTH properties' full KB will be available next turn.\n"
            "  - If you set it to just ['mumbai_orchid'], only that property's KB will be loaded next turn.\n"
            "  - If you leave it empty, ALL properties' KB will be available next turn.\n"
            "  - IMPORTANT: When the guest is discussing MULTIPLE properties (e.g. 'which Mumbai hotel has offers?', or you just listed rooms from 2 hotels), "
            "include ALL relevant property IDs — do NOT pick just one.\n"
            "  - If the guest switches property ('actually I want Shimla'), update to the new property's ID. The old one moves to past_property_context automatically.\n"
            "  - If the guest asks about a property in past_property_context, add it back to selected_property_ids to get full details.\n"
            "\n"
            "WHEN TO ASK which property:\n"
            "  - ONLY when the guest asks a property-specific question (check-in time, room types, amenities, pricing, booking) "
            "AND you genuinely cannot determine which property from conversation history or the current message.\n"
            "  - If the guest already mentioned or selected a property earlier in the conversation, use that — do NOT re-ask.\n"
            "  - If active_property_ids already has entries, the guest is discussing those — use them.\n"
            "\n"
            "WHEN TO ANSWER WITHOUT ASKING which property:\n"
            "  - General/comparative questions: 'which is best for couples', 'recommend a location', 'top 5 for adventure', "
            "'how many properties', 'list all', 'compare', 'suggest', 'plan a trip', 'budget options' → answer from ALL properties.\n"
            "  - Exploratory/planning questions: 'I want to travel', 'where should I go', 'family trip' → recommend from full KB.\n"
            "  - When the guest mentions a CITY (e.g. 'Mumbai') and multiple properties are in that city → include ALL of them in selected_property_ids and answer about all.\n"
            "  - Greetings, chit-chat, general hotel questions → answer directly.\n"
            "  - ANY question where the KB gives a clear answer across properties → answer it.\n"
            "\n"
            "PROPERTY SWITCHING:\n"
            "  - If the guest says 'actually I want Shimla' or 'let me try Mumbai instead', treat this as a property switch.\n"
            "  - Acknowledge the switch naturally and start answering from the new property's KB.\n"
            "  - Update selected_property_ids to the new property (the system moves old ones to past context automatically).\n"
            "\n"
            "NEVER ask for property clarification twice in a row. If the last assistant message already asked, "
            "try to answer from whatever context you have or ask a different clarifying question.\n"
            "NEVER ask 'Which property would you like details for?' as a robotic default — phrase it naturally based on what the guest asked.\n"
            "\n"
            "POST-BOOKING PROPERTY FOCUS (pre_checkin / during_stay / post_checkout):\n"
            "When stay_property_pin.enabled is true, the guest has BOOKED at a specific property — that is their hotel.\n"
            "  - Answer ONLY from that property's knowledge. This is where they are staying.\n"
            "  - Do NOT volunteer information about other properties unless the guest explicitly asks "
            "(e.g. 'what about your Mumbai hotel?', 'do you have this at another location?').\n"
            "  - Treat the pinned property as the default for ALL questions — rooms, amenities, dining, services, policies.\n"
            "  - If the guest asks something generic like 'what time is checkout?', answer for their booked property only.\n"
            "  - When active booking context exists, treat that stay as the default frame of reference for interpreting the message.\n"
            "  - Resolve ambiguity toward the guest's own stay, not toward generic hotel information.\n"
            "  - If a question can be answered from the guest's active stay plus the booked property's knowledge, answer that way.\n"
            "  - Do NOT default to a general hotel-policy answer when a more guest-specific answer is already grounded in context.\n"
            "  - Ask a clarifying question only when there is genuine ambiguity that cannot be resolved from active booking context, booked-property context, or conversation history.\n"
            "\n"
            "=== STEP 1: READ HISTORY ===\n"
            "Always read history_last_10 first, then full_history_context before deciding anything. "
            "Use last_user_message and last_assistant_message as high-priority anchors for continuity. "
            "Resolve pronouns ('it', 'that', 'same', 'this') against the last assistant message.\n"
            "\n"
            "=== GUEST FACTS (hotel already knows these — never re-ask) ===\n"
            "The payload includes memory_facts and known_context with data the hotel already has on file.\n"
            "Think like a hotel: a checked-in guest's name, room number, phone, and email are ALWAYS on file.\n"
            "  - during_stay / post_checkout: NEVER ask for guest name, room number, phone, or email.\n"
            "  - pre_checkin: NEVER ask for name, phone, or email — they were collected at booking.\n"
            "  - pre_booking: only request contact info if the service genuinely needs it and it is absent from known_context.\n"
            "UNIVERSAL: If a value is already in memory_facts or known_context, treat it as known and do NOT ask for it again.\n"
            "When a service agent is dispatched, it will also receive known_context — it will handle the details.\n"
            "\n"
            "=== STEP 2: MID-FLOW CHECK ===\n"
            "Read pending_action and pending_action_context carefully. "
            "If pending_action is set, the guest is mid-flow in a service — read pending_action_context to understand exactly what was happening. "
            "Stay on that service and route back to it immediately using action=dispatch_handler with the same target_service_id. "
            "If pending_action starts with collect_generic_kb_request or confirm_generic_kb_request, continue that same KB-grounded request yourself. "
            "Keep target_service_id empty unless a configured service later becomes clearly correct. "
            "Short replies ('yes', 'ok', 'sure', 'no', 'cancel', a number, a name, a date) when pending_action is set are ALWAYS a direct continuation of that flow — never re-route them. "
            "A question about the current service (e.g. asking about vehicle type during airport transfer, asking about room details during room booking, asking about menu during table booking) is NOT an interrupt — dispatch to the same service agent so it can answer and continue collecting slots. "
            "When the guest confirms a booking ('yes', 'yes confirm', 'ok', 'proceed', 'go ahead') while pending_action is set: dispatch to the service agent — the service agent owns confirmation and ticket creation. Do NOT set action=create_ticket yourself. "
            "When the guest confirms a KB-grounded generic request while pending_action starts with confirm_generic_kb_request: "
            "set action=create_ticket, target_service_id='', ticket.required=true, ticket.ready_to_create=true, and keep the same KB evidence. "
            "When the guest cancels or refuses ('no', 'cancel', 'stop', 'forget it') while pending_action is set: set action=cancel_pending and acknowledge politely. "
            "Only interrupt the pending flow if the guest explicitly and clearly asks to start a completely unrelated and different service.\n"
            "Also check last_active_service — if it has an id, the guest was just interacting with that service last turn. "
            "If the current message is still related to that service (questions about its menu, options, availability, booking, or anything that service handles), dispatch to it again. "
            "Only switch away if the guest clearly asks for a different service or a completely unrelated topic.\n"
            "\n"
            "=== STEP 2B: AMBIGUITY CHECK ===\n"
            "If the message is short, vague, or has no clear intent AND pending_action is empty AND last_assistant_message does not provide enough context to interpret it — "
            "do not guess at intent and do not route to any service. Set action=respond_only and generate a friendly clarification question asking what the guest needs. "
            "Example: 'Could you tell me a bit more about what you are looking for?'\n"
            "\n"
            "=== STEP 2C: MULTI-SERVICE CLARIFICATION ===\n"
            "ONLY applies when ALL of these are true: pending_action is empty AND the guest's message is a service request.\n"
            "PHASE GATE — do this FIRST before anything else in this step:\n"
            "  1. Look at allowed_service_ids_in_current_phase (the list of services available RIGHT NOW).\n"
            "  2. Ask: does the guest's request match ANY service in that list?\n"
            "  3. If ZERO services in allowed_service_ids_in_current_phase match → STOP. Skip this step. Go to STEP 3B immediately.\n"
            "  4. NEVER ask a clarification question that offers services from blocked_out_of_phase_services.\n"
            "If the phase gate passes (at least one allowed service matches):\n"
            "  - Check: could the request belong to TWO OR MORE services in allowed_service_ids_in_current_phase?\n"
            "  - Compare service names and descriptions: if multiple services are plausible for the same ask, treat it as ambiguous.\n"
            "  - If yes → ask ONE short question naming only the allowed matching services.\n"
            "  - In ambiguity cases, keep target_service_id empty and do not dispatch yet.\n"
            "  - If only one matches → dispatch directly (no question).\n"
            "  - If the message is specific enough to rule out ambiguity → dispatch directly.\n"
            "  - If guest is answering a previous clarification → dispatch to what they picked.\n"
            "\n"
            "=== STEP 3: DECIDE WHAT THE GUEST NEEDS ===\n"
            "\n"
            "A) INFORMATION QUESTION — guest is asking about hotel facilities, timings, policies, menus, room types, or any general details.\n"
            "   -> Answer directly from full_knowledge_base. Set action=respond_only.\n"
            "   -> If full_knowledge_base contains specific offerings, treatments, timings, policies, options, or amenities, include them explicitly instead of giving a generic summary.\n"
            "   -> Asking about room types, availability, prices, or facilities is NEVER a complaint.\n"
            "   -> Never invent facts. If details are not present, say they are not available.\n"
            "\n"
            "B) SERVICE REQUEST — guest wants to book, order, request, or arrange something.\n"
            "   -> From the very first message requesting a service — even just 'I need a room', 'book a table', 'get me a cab' — IMMEDIATELY set action=dispatch_handler. Do not answer first, do not ask for details yourself.\n"
            "   -> The service agent handles ALL slot collection and responses. Your job is only to dispatch to the right service.\n"
            "   -> target_service_id MUST be an exact ID from allowed_service_ids_in_current_phase (e.g. 'room_booking_support'). NEVER use 'complaint', 'complaint_service', 'complain', or any complaint-flavored ID for a service request.\n"
            "   -> If the service is only in blocked_out_of_phase_services: set action=respond_only. "
            "Provide factual information about the service (what it offers, hours, pricing if known). "
            "Then EXPLICITLY tell the guest that booking/ordering/requesting this service is not possible at this moment. "
            "Use the 'availability_hint' field from that service's entry in blocked_out_of_phase_services to tell the guest WHEN it will become available. "
            "Examples: 'We do offer spa treatments from 9 AM to 11 PM — however, spa bookings can only be made once you check in. "
            "I will be happy to help you book a session then!', "
            "'Airport transfers are available — I can help you arrange one after your booking is confirmed.', "
            "'In-room dining has a wonderful menu! You will be able to place orders once you check in.' "
            "The key: share the info, clearly state you cannot book/order it now, and warmly tell them when they can. "
            "Do NOT promise, invite, offer, or imply the guest can use it now. NEVER mention phase names (pre_checkin, during_stay, etc.).\n"
            "   -> Wanting to book a room, order food, or request any service is NEVER a complaint.\n"
            "   -> If NO allowed configured service matches, but full_knowledge_base clearly says the hotel does allow or handle this request "
            "in the guest's current context (including cases that require staff approval), you may manage it as a KB-grounded generic request.\n"
            "      For a KB-grounded generic request:\n"
            "      - keep target_service_id empty\n"
            "      - set ticket.generic_request=true\n"
            "      - set ticket.phase_applicable=true only when the request makes sense right now from KB + guest context\n"
            "      - set ticket.approval_required=true when KB says approval or manual review is needed\n"
            "      - set ticket.evidence to 1-3 short fact lines copied or paraphrased from full_knowledge_base / policy text\n"
            "      - if details are missing: action=collect_info and pending_action=collect_generic_kb_request\n"
            "      - if all details are present but you still need the guest to approve submission: action=collect_info and pending_action=confirm_generic_kb_request\n"
            "      - if the guest already confirmed and it is ready: action=create_ticket, ticket.required=true, ticket.ready_to_create=true, pending_action=null\n"
            "      - if KB is unclear, silent, or contradictory: do NOT guess, do NOT collect transactional details, do NOT create a ticket\n"
            "\n"
            "C) STAFF-ACTION REQUEST — guest needs something that requires a hotel staff member to physically do something, "
            "but it is NOT a booking or an order and no specific service is configured for it.\n"
            "   Think like a real hotel front desk: if a guest walks up and says any of these, you would never just say "
            "'contact housekeeping' — you would immediately note it down and make sure it gets handled:\n"
            "   - Missing items: 'no towels', 'need extra pillows', 'no toiletries', 'minibar not stocked'\n"
            "   - Maintenance: 'AC not working', 'hot water issue', 'lights flickering', 'TV remote broken'\n"
            "   - Housekeeping: 'room not cleaned', 'need fresh sheets', 'trash not picked up'\n"
            "   - Disturbances: 'noisy neighbors', 'construction noise', 'someone knocking on my door'\n"
            "   - Any situation where the guest is stuck and needs staff to act\n"
            "   HOW TO HANDLE:\n"
            "   -> during_stay: This is the guest's home right now. Take ownership. "
            "Empathize naturally ('I'm sorry about that' / 'Let me get that sorted for you right away'), "
            "then set action=create_ticket with ticket.category='request', ticket.sub_category='in_room_assistance', "
            "and ticket.priority based on urgency (no hot water or AC = high, extra pillow = medium). "
            "Tell the guest someone from the team will take care of it shortly. "
            "Do NOT tell them to 'contact housekeeping' or 'call the front desk' — YOU are the front desk.\n"
            "   -> pre_booking / pre_checkin: These requests don't apply yet. Reassure them: "
            "'We will make sure everything is perfect for your arrival.'\n"
            "   -> post_checkout: Only relevant if they left something behind or have a billing issue. "
            "Otherwise respond warmly.\n"
            "   IMPORTANT — do NOT create tickets for:\n"
            "   - Questions: 'do you have extra pillows?' (just answer yes/no from KB)\n"
            "   - Future requests: 'can I get extra towels when I check in?' (just note it)\n"
            "   - Things the guest can do themselves: 'how do I connect to WiFi?' (just answer)\n"
            "   - General feedback without urgency: 'the breakfast was okay' (just acknowledge)\n"
            "   The test: would a real front desk person write this down and send someone to the room? "
            "If yes → create ticket. If they would just answer verbally → respond_only.\n"
            "\n"
            "D) COMPLAINT — guest explicitly expresses dissatisfaction, frustration, or reports something that went wrong.\n"
            "   -> This is different from C: in C the guest just needs something done. "
            "In D the guest is unhappy — there is negative emotion or something has failed.\n"
            "   -> Examples: 'this is unacceptable', 'I've been waiting 30 minutes', 'the food was cold', "
            "'cockroach in my room', 'worst experience', 'I want to speak to a manager'\n"
            "   -> PHASE-AWARE COMPLAINT GATE: Before creating a complaint ticket, ask: does this complaint make sense given where the guest is right now?\n"
            "      - If the guest has NOT yet checked in (pre-booking or pre-checkin phase) and reports an in-room issue (cockroach, broken AC, dirty room, etc.) → the complaint is impossible in context. Respond with empathy but do NOT create a ticket. Offer to note it or suggest they contact the hotel directly if it is a future concern.\n"
            "      - If the guest is during their stay (during_stay) → in-room and on-property complaints ARE valid. Create a ticket.\n"
            "      - If the guest has checked out (post-checkout) → only post-stay complaints (billing, lost items, quality feedback) are valid.\n"
            "      - Apply common sense: a complaint must be physically possible given the guest's current journey stage.\n"
            "   -> Set action=create_ticket, ticket.category='complaint', and set priority higher than normal (high or critical if the guest sounds upset).\n"
            "   -> Empathize FIRST, then tell them you've noted it and the team will address it. Never be defensive.\n"
            "   -> NEVER use complaint routing for: room inquiries, booking requests, food orders, or any request to do something.\n"
            "\n"
            "E) HUMAN / EMERGENCY — guest is distressed or explicitly asks for a human: set requires_human_handoff=true.\n"
            "\n"
            "=== ROUTING SANITY CHECK ===\n"
            "Before writing output, ask yourself: if a guest said this to me at the front desk, what would I do?\n"
            "  - Would I just answer them? → respond_only\n"
            "  - Would I route them to a specific department to book/order? → dispatch_handler\n"
            "  - Would I write it down and send someone to help? → create_ticket (category=request)\n"
            "  - Would I escalate because the guest is upset? → create_ticket (category=complaint)\n"
            "When in doubt between respond_only and create_ticket, ask: does this need a human to physically do something? If yes, create the ticket.\n"
            "\n"
            "=== GROUNDING RULE ===\n"
            "Use only provided policy + knowledge data. Never invent prices, timings, availability, or capabilities.\n"
            "If the guest asks for weather details, provide the weather of the area the hotel is located in.\n"
            "\n"
            "=== TONE & LANGUAGE ===\n"
            "You are speaking to a hotel guest. Use warm, natural, human language.\n"
            "NEVER use: 'escalate', 'ticket', 'reference number', 'ticket ID', 'logged', 'automated ticketing', 'system issue'.\n"
            "INSTEAD use: 'I have noted this', 'our team will look into this', 'let me connect you with our team'.\n"
            "For complaints: empathize first, then offer help. Never jump to data collection after a complaint.\n"
            "For requests to speak with staff: offer contact info or say someone will reach out — do not use internal process language.\n"
            "\n"
            "=== OUTPUT ===\n"
            "Return strict JSON only."
        )
        # Inject booking context + phase rules into orchestrator prompt
        _orch_booking_block = self._build_booking_context_prompt_block(context)
        if _orch_booking_block:
            system_prompt = system_prompt + "\n" + _orch_booking_block

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
            max_tokens=4096,
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
        decision = self._normalize_generic_kb_request_decision(decision)

        # ── Persist LLM-determined property scope into conversation state ──
        # The orchestrator returns selected_property_name / selected_property_ids.
        # We track active properties (full KB sent) and past properties (brief
        # summary retained) so the LLM always has the right context.
        _llm_prop_name = str(decision.selected_property_name or "").strip()
        _llm_prop_ids = self._normalize_property_id_list(
            [str(pid).strip() for pid in (decision.selected_property_ids or []) if str(pid).strip()]
        )
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        if _llm_prop_ids:
            _llm_prop_ids, _post_llm_pin_applied = self._apply_stay_property_pin(
                context=context,
                selected_phase_id=selected_phase_id,
                property_ids=_llm_prop_ids,
                source="post_orchestrator_decision",
            )
            if _post_llm_pin_applied:
                decision.metadata["stay_property_pin_reapplied"] = True

        _prev_active_ids = self._normalize_property_id_list(
            list(context.pending_data.get("_active_property_ids") or [])
        )
        _prev_active_names = list(context.pending_data.get("_active_property_names") or [])
        _past_ctx = dict(context.pending_data.get("_past_property_context") or {})

        if _llm_prop_ids:
            # Move previously-active properties that are no longer active to past context
            for _old_id in _prev_active_ids:
                if _old_id and _old_id not in _llm_prop_ids and _old_id not in _past_ctx:
                    # Build a brief summary from the old property name
                    _old_idx = _prev_active_ids.index(_old_id) if _old_id in _prev_active_ids else -1
                    _old_name = _prev_active_names[_old_idx] if 0 <= _old_idx < len(_prev_active_names) else _old_id
                    _past_ctx[_old_id] = f"Previously discussed: {_old_name}"
            # Cap past context to 10 entries max
            if len(_past_ctx) > 10:
                _past_ctx = dict(list(_past_ctx.items())[-10:])

            context.pending_data["_active_property_ids"] = _llm_prop_ids
            context.pending_data["_active_property_names"] = (
                [_llm_prop_name] if _llm_prop_name and len(_llm_prop_ids) == 1
                else _llm_prop_ids  # will be refined by KB section names at scoping time
            )
            context.pending_data["_past_property_context"] = _past_ctx

            # Keep legacy fields in sync for backward compat
            context.pending_data["_selected_property_scope_ids"] = _llm_prop_ids
            if _llm_prop_name:
                context.pending_data["_selected_property_scope_name"] = _llm_prop_name
            if len(_llm_prop_ids) == 1:
                context.pending_data["_selected_property_scope_id"] = _llm_prop_ids[0]
        elif _llm_prop_name and not _llm_prop_ids:
            # LLM gave a name but no IDs — keep legacy behavior
            context.pending_data["_selected_property_scope_name"] = _llm_prop_name

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
        decision = self._normalize_generic_kb_request_decision(decision)
        generic_kb_request = self._is_generic_kb_request(decision)

        # Continuity router: use LLM to decide whether this short/ambiguous turn
        # should continue the last active service flow.
        _last_assistant_message = str(history_bundle.get("last_assistant_message") or "")
        _last_service_id = self._normalize_identifier(
            (context.pending_data or {}).get("_last_service_id")
            if isinstance(context.pending_data, dict)
            else ""
        )
        if (
            str(decision.action or "").strip().lower() == "respond_only"
            and not decision.target_service_id
            and _last_service_id
            and str(user_message or "").strip()
            and str(_last_assistant_message or "").strip()
        ):
            continue_last_service, continuation_reason = await self._resolve_last_service_continuation_with_llm(
                user_message=user_message,
                last_assistant_message=_last_assistant_message,
                last_service_id=_last_service_id,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                services_snapshot=services_snapshot,
                context=context,
            )
            if continue_last_service:
                decision.action = "dispatch_handler"
                decision.target_service_id = _last_service_id
                if not isinstance(decision.metadata, dict):
                    decision.metadata = {}
                decision.metadata["service_resolution_source"] = "continuation_router_llm"
                if continuation_reason:
                    decision.metadata["continuation_router_reason"] = continuation_reason

        # Only call the fallback service router when the orchestrator did NOT return
        # a clarification question. If action=respond_only with non-empty response_text
        # and no target_service_id, it means the orchestrator intentionally chose to ask
        # the user a question (STEP 2B/2C). Calling the service router here would bypass
        # that clarification and force-dispatch to a service.
        _orchestrator_gave_clarification = (
            str(decision.action or "").strip().lower() == "respond_only"
            and str(decision.response_text or "").strip()
            and not decision.target_service_id
        )
        _skip_answer_first = False  # default; overridden later if service runs
        if not decision.target_service_id and not _orchestrator_gave_clarification and not generic_kb_request:
            resolved_service_id, action_hint = await self._resolve_target_service_with_llm(
                user_message=user_message,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                services_snapshot=services_snapshot,
                context=context,
            )
            _log_llm_call(
                label="SERVICE ROUTER",
                session_id=str(getattr(context, "session_id", "")),
                user_message=user_message,
                system_prompt="(see _resolve_target_service_with_llm)",
                payload={"resolved_service_id": resolved_service_id, "action_hint": action_hint},
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
                full_kb_text=full_kb_text,
            )
            fallback_decision = self._normalize_generic_kb_request_decision(fallback_decision)
            _early_result = await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=fallback_decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=target_service if isinstance(target_service, dict) else None,
            )
            _log_decision({
                "ts": datetime.now(UTC).isoformat(),
                "trace_id": orchestration_trace_id,
                "session_id": str(getattr(context, "session_id", "")),
                "phase": {"id": selected_phase_id, "name": selected_phase_name},
                "user_message": user_message,
                "routing": {
                    "orchestrator_action": str(decision.action or ""),
                    "orchestrator_service_id": str(decision.target_service_id or ""),
                    "clarification_protected": _orchestrator_gave_clarification,
                    "final_action": str((_early_result.action or "") if _early_result else ""),
                    "final_service_id": "",
                    "prompt_source": "n/a",
                    "service_resolution_source": "none",
                    "early_return_reason": "no_service_agent_enabled_or_id",
                },
                "response_text": str((_early_result.response_text or "")[:500]) if _early_result else "",
                "intent": str((_early_result.intent or "")) if _early_result else "",
                "missing_fields": [],
                "context_switched": False,
                "answer_first_guard_skipped": False,
                "pending_action_at_start": str(context.pending_action or ""),
            })
            return _early_result

        if not isinstance(target_service, dict):
            fallback_decision = await self._enforce_answer_first_policy(
                user_message=user_message,
                decision=decision,
                context=context,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
                full_kb_text=full_kb_text,
            )
            _early_result = await self._ensure_suggested_actions(
                user_message=user_message,
                context=context,
                decision=fallback_decision,
                selected_phase_id=selected_phase_id,
                selected_phase_name=selected_phase_name,
                target_service=None,
            )
            _log_decision({
                "ts": datetime.now(UTC).isoformat(),
                "trace_id": orchestration_trace_id,
                "session_id": str(getattr(context, "session_id", "")),
                "phase": {"id": selected_phase_id, "name": selected_phase_name},
                "user_message": user_message,
                "routing": {
                    "orchestrator_action": str(decision.action or ""),
                    "orchestrator_service_id": str(decision.target_service_id or ""),
                    "clarification_protected": _orchestrator_gave_clarification,
                    "final_action": str((_early_result.action or "") if _early_result else ""),
                    "final_service_id": "",
                    "prompt_source": "n/a",
                    "service_resolution_source": "none",
                    "early_return_reason": "target_service_not_dict",
                },
                "response_text": str((_early_result.response_text or "")[:500]) if _early_result else "",
                "intent": str((_early_result.intent or "")) if _early_result else "",
                "missing_fields": [],
                "context_switched": False,
                "answer_first_guard_skipped": False,
                "pending_action_at_start": str(context.pending_action or ""),
            })
            return _early_result

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
        # Track which service was last active so the next turn's orchestrator has context.
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        context.pending_data["_last_service_id"] = target_service_id
        context.pending_data["_last_service_name"] = str(target_service.get("name") or target_service_id)
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
        merged_decision = self._normalize_generic_kb_request_decision(merged_decision)
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
                full_kb_text=full_kb_text,
            )
            merged_decision = self._normalize_generic_kb_request_decision(merged_decision)

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

        final = await self._ensure_suggested_actions(
            user_message=user_message,
            context=context,
            decision=merged_decision,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
            target_service=target_service,
        )
        final = self._normalize_generic_kb_request_decision(final)
        _log_decision({
            "ts": datetime.now(UTC).isoformat(),
            "trace_id": orchestration_trace_id,
            "session_id": str(getattr(context, "session_id", "")),
            "phase": {"id": selected_phase_id, "name": selected_phase_name},
            "user_message": user_message,
            "routing": {
                "orchestrator_action": str(decision.action or ""),
                "orchestrator_service_id": str(decision.target_service_id or ""),
                "clarification_protected": _orchestrator_gave_clarification,
                "final_action": str(final.action or "") if final else "",
                "final_service_id": str(final.target_service_id or "") if final else "",
                "prompt_source": ("generated" if target_service and str(target_service.get("generated_system_prompt") or "").strip() else "template") if target_service else "n/a",
                "service_resolution_source": str((merged_decision.metadata or {}).get("service_resolution_source") or "orchestrator"),
            },
            "response_text": str(final.response_text or "")[:500] if final else "",
            "intent": str(final.intent or "") if final else "",
            "missing_fields": list(final.missing_fields or []) if final else [],
            "ticket": {
                "required": bool(getattr((final or decision).ticket, "required", False)) if (final or decision) else False,
                "ready_to_create": bool(getattr((final or decision).ticket, "ready_to_create", False)) if (final or decision) else False,
                "generic_request": bool(getattr((final or decision).ticket, "generic_request", False)) if (final or decision) else False,
                "evidence": list(getattr((final or decision).ticket, "evidence", []) or [])[:3] if (final or decision) else [],
            },
            "context_switched": bool((merged_decision.metadata or {}).get("context_switched")),
            "answer_first_guard_skipped": _skip_answer_first,
            "pending_action_at_start": str(context.pending_action or ""),
        })
        return final


llm_orchestration_service = LLMOrchestrationService()
