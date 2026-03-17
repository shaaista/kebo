"""
Full-KB LLM Service

Runs a KB-only conversational flow by sending complete tenant KB content
to the LLM on each turn. This path intentionally bypasses retrieval chunks
and avoids deterministic domain-specific hardcoded rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json
import re
import uuid
from typing import Any, Optional

from config.settings import settings
from llm.client import llm_client
from schemas.chat import ConversationContext, ConversationState, IntentType


DEFAULT_KB_DIR = Path(__file__).parent.parent / "config" / "knowledge_base"
DEFAULT_BUNDLED_KB_FILE = Path(__file__).parent.parent / "ROHL_Test_property.txt"
_FALLBACK_NOT_FOUND = (
    "I could not find that in the current knowledge base for this property. "
    "If you want, I can connect you with our team right away."
)


@dataclass
class FullKBLLMResult:
    """Normalized result returned by full-KB LLM flow."""

    response_text: str
    normalized_query: str
    intent: IntentType
    raw_intent: str
    confidence: float
    next_state: ConversationState
    pending_action: Optional[str]
    pending_data: dict[str, Any]
    room_number: Optional[str]
    suggested_actions: list[str]
    trace_id: str
    llm_output: dict[str, Any]
    clear_pending_data: bool = False
    status: str = "success"


class FullKBLLMService:
    """Full knowledge-base prompting with structured LLM orchestration."""

    def __init__(self, kb_dir: Optional[Path] = None):
        self.kb_dir = Path(kb_dir or DEFAULT_KB_DIR)
        self.step_logs_enabled = bool(getattr(settings, "full_kb_llm_step_logs_enabled", True))
        self.step_log_file = Path(getattr(settings, "rag_step_log_file", "./logs/detailedsteps.log"))
        self.step_log_preview_chars = max(100, int(getattr(settings, "rag_step_log_preview_chars", 260)))
        self.max_kb_chars = max(20_000, int(getattr(settings, "full_kb_llm_max_kb_chars", 180_000)))
        configured_history_messages = int(getattr(settings, "full_kb_llm_max_history_messages", 0) or 0)
        self.max_history_messages = configured_history_messages if configured_history_messages > 0 else 0
        self.history_max_chars = max(1000, int(getattr(settings, "full_kb_llm_history_max_chars", 12000) or 12000))
        self.memory_summary_chars = max(800, int(getattr(settings, "full_kb_llm_memory_summary_chars", 2200)))
        self.temperature = float(getattr(settings, "full_kb_llm_temperature", 0.1))
        self._kb_cache: dict[str, tuple[str, list[str], str]] = {}

    @staticmethod
    def _normalize_tenant(value: str) -> str:
        return str(value or "default").strip().lower().replace(" ", "_")

    @staticmethod
    def _normalize_phase_identifier(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

    @staticmethod
    def _phase_transition_timing_hint(current_phase_id: str, service_phase_id: str) -> str:
        current_norm = re.sub(r"[^a-z0-9]+", "_", str(current_phase_id or "").strip().lower()).strip("_")
        service_norm = re.sub(r"[^a-z0-9]+", "_", str(service_phase_id or "").strip().lower()).strip("_")
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

    def _phase_label(self, phase_id: str, capabilities_summary: dict[str, Any]) -> str:
        normalized = self._normalize_phase_identifier(phase_id)
        if not normalized:
            return ""
        phase_rows = capabilities_summary.get("journey_phases")
        if not isinstance(phase_rows, list):
            phase_rows = capabilities_summary.get("phases")
        if isinstance(phase_rows, list):
            for phase in phase_rows:
                if not isinstance(phase, dict):
                    continue
                candidate_id = self._normalize_phase_identifier(phase.get("id"))
                if candidate_id != normalized:
                    continue
                label = str(phase.get("name") or "").strip()
                if label:
                    return label
        return normalized.replace("_", " ").title()

    def _resolve_selected_phase_context(
        self,
        *,
        context: ConversationContext,
        pending_public: dict[str, Any],
        capabilities_summary: dict[str, Any],
    ) -> tuple[str, str]:
        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending_raw.get("_integration", {})
        integration_map = integration if isinstance(integration, dict) else {}

        candidates = (
            pending_public.get("phase"),
            pending_raw.get("phase"),
            integration_map.get("phase"),
        )
        for candidate in candidates:
            normalized = self._normalize_phase_identifier(candidate)
            if normalized:
                return normalized, self._phase_label(normalized, capabilities_summary)

        flow = str(
            integration_map.get("flow")
            or integration_map.get("bot_mode")
            or ""
        ).strip().lower()
        default_phase_id = "pre_booking" if flow in {"engage", "booking", "booking_bot"} else "during_stay"
        return default_phase_id, self._phase_label(default_phase_id, capabilities_summary)

    def _preview_text(self, value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if len(text) <= self.step_log_preview_chars:
            return text
        return text[: self.step_log_preview_chars] + "..."

    def _new_trace(self, question: str, tenant_id: str) -> Optional[dict[str, Any]]:
        if not self.step_logs_enabled:
            return None
        return {
            "trace_id": f"fullkb-{uuid.uuid4().hex[:12]}",
            "started_at": datetime.now(UTC).isoformat(),
            "tenant_id": self._normalize_tenant(tenant_id),
            "question": re.sub(r"\s+", " ", str(question or "").strip()),
            "steps": [],
        }

    def _trace_step(
        self,
        trace: Optional[dict[str, Any]],
        step: str,
        status: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        if not self.step_logs_enabled or trace is None:
            return
        entry: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(),
            "step": step,
            "status": status,
        }
        if input_data:
            entry["input"] = input_data
        if output_data:
            entry["output"] = output_data
        if error:
            entry["error"] = self._preview_text(error)
        trace.setdefault("steps", []).append(entry)

    @staticmethod
    def _trace_step_by_name(trace: dict[str, Any], name: str) -> dict[str, Any]:
        for step in trace.get("steps", []):
            if isinstance(step, dict) and step.get("step") == name:
                return step
        return {}

    def _render_human_trace(self, trace: dict[str, Any]) -> str:
        trace_id = str(trace.get("trace_id") or "")
        tenant_id = str(trace.get("tenant_id") or "")
        started_at = str(trace.get("started_at") or "")
        completed_at = str(trace.get("completed_at") or "")
        final_status = str(trace.get("final_status") or "unknown")

        normalize_step = self._trace_step_by_name(trace, "normalize_query")
        normalize_status = str(normalize_step.get("status") or "unknown")
        normalized_query = ""
        if isinstance(normalize_step.get("output"), dict):
            normalized_query = str(normalize_step["output"].get("normalized_query") or "")

        kb_step = self._trace_step_by_name(trace, "load_full_kb")
        kb_output = kb_step.get("output", {}) if isinstance(kb_step.get("output"), dict) else {}
        kb_sources = kb_output.get("sources", [])
        if not isinstance(kb_sources, list):
            kb_sources = []

        llm_step = self._trace_step_by_name(trace, "llm_decision")
        llm_status = str(llm_step.get("status") or "unknown")
        llm_output = llm_step.get("output", {}) if isinstance(llm_step.get("output"), dict) else {}
        intent = str(llm_output.get("intent") or "")
        next_state = str(llm_output.get("next_state") or "")
        answer_preview = str(llm_output.get("assistant_response_preview") or "")

        lines: list[str] = []
        lines.append(f"===== FULL KB LLM FLOW TRACE {trace_id} =====")
        lines.append(f"Tenant: {tenant_id} | Status: {final_status}")
        lines.append(f"Started: {started_at} | Completed: {completed_at}")
        lines.append("")
        lines.append("1) USER QUERY")
        lines.append(f"   {self._preview_text(trace.get('question', ''))}")
        lines.append("")
        lines.append(f"2) NORMALIZED QUERY ({normalize_status})")
        lines.append(f"   {self._preview_text(normalized_query)}")
        lines.append("")
        lines.append("3) FULL KB INPUT")
        lines.append(f"   Sources: {', '.join(kb_sources[:10]) if kb_sources else 'None'}")
        lines.append(f"   KB chars: {kb_output.get('kb_chars', 0)}")
        lines.append("")
        lines.append("4) INPUT SENT TO LLM")
        lines.append(f"   State: {trace.get('state_before', 'idle')}")
        lines.append(f"   Pending action: {trace.get('pending_action_before', '') or 'None'}")
        lines.append(f"   Pending data preview: {self._preview_text(trace.get('pending_data_before', {}))}")
        lines.append("")
        lines.append(f"5) LLM OUTPUT ({llm_status})")
        lines.append(f"   intent={intent} | next_state={next_state}")
        lines.append(f"   {self._preview_text(answer_preview)}")
        lines.append("===== END FULL KB LLM FLOW TRACE =====")
        lines.append("")
        return "\n".join(lines)

    def _write_trace(self, trace: Optional[dict[str, Any]], final_status: str) -> None:
        if not self.step_logs_enabled or trace is None:
            return
        trace["completed_at"] = datetime.now(UTC).isoformat()
        trace["final_status"] = str(final_status or "unknown")
        try:
            self.step_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.step_log_file.open("a", encoding="utf-8") as fh:
                fh.write(self._render_human_trace(trace))
                fh.write(f"--- FULL KB LLM TRACE {trace.get('trace_id', '')} ---\n")
                fh.write(json.dumps(trace, ensure_ascii=False, indent=2))
                fh.write("\n--- END FULL KB LLM TRACE ---\n")
        except Exception:
            return

    @staticmethod
    def _normalize_query(query: str) -> str:
        value = str(query or "").strip()
        if not value:
            return ""
        return re.sub(r"\s+", " ", value)

    def _resolve_source_paths(self, tenant_id: str, source_paths: Optional[list[str | Path]] = None) -> list[Path]:
        candidates: list[Path] = []

        if source_paths:
            for item in source_paths:
                path = Path(item)
                if path.exists() and path.is_file():
                    candidates.append(path)
            return self._dedupe_paths(candidates)

        normalized_tenant = self._normalize_tenant(tenant_id)
        tenant_uploads = self.kb_dir / "uploads" / normalized_tenant
        if tenant_uploads.exists():
            for candidate in tenant_uploads.iterdir():
                if candidate.is_file():
                    candidates.append(candidate)

        default_uploads = self.kb_dir / "uploads" / "default"
        if default_uploads.exists():
            for candidate in default_uploads.iterdir():
                if candidate.is_file():
                    candidates.append(candidate)

        if self.kb_dir.exists():
            for candidate in self.kb_dir.iterdir():
                if candidate.is_file():
                    candidates.append(candidate)

        if DEFAULT_BUNDLED_KB_FILE.exists() and DEFAULT_BUNDLED_KB_FILE.is_file():
            candidates.append(DEFAULT_BUNDLED_KB_FILE)

        filtered = [
            p
            for p in candidates
            if p.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".yml"}
        ]
        filtered.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
        return self._dedupe_paths(filtered)

    @staticmethod
    def _dedupe_paths(paths: list[Path]) -> list[Path]:
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _read_text_file(self, path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-1")
        except Exception:
            return ""
        return str(text or "").strip()

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _build_kb_signature(self, paths: list[Path]) -> str:
        parts: list[str] = []
        for path in paths:
            try:
                stat = path.stat()
                parts.append(f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}")
            except Exception:
                parts.append(str(path))
        return "|".join(parts)

    def _load_full_kb_text(self, tenant_id: str) -> tuple[str, list[str]]:
        normalized_tenant = self._normalize_tenant(tenant_id)
        paths = self._resolve_source_paths(normalized_tenant)
        signature = self._build_kb_signature(paths)

        cached = self._kb_cache.get(normalized_tenant)
        if cached and cached[2] == signature:
            return cached[0], cached[1]

        documents: list[str] = []
        sources: list[str] = []
        seen_hashes: set[str] = set()
        total_chars = 0

        for path in paths:
            text = self._read_text_file(path)
            if not text:
                continue
            digest = self._content_hash(re.sub(r"\s+", " ", text))
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)

            section_text = f"[SOURCE: {path.name}]\n{text}"
            room_left = self.max_kb_chars - total_chars
            if room_left <= 0:
                break
            if len(section_text) > room_left:
                section_text = section_text[:room_left]
            documents.append(section_text)
            sources.append(path.name)
            total_chars += len(section_text)

            if total_chars >= self.max_kb_chars:
                break

        combined = "\n\n".join(documents).strip()
        self._kb_cache[normalized_tenant] = (combined, sources, signature)
        return combined, sources

    @staticmethod
    def _sanitize_json_value(value: Any, depth: int = 0) -> Any:
        if depth > 4:
            return str(value)[:400]
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            if isinstance(value, str):
                return value[:1000]
            return value
        if isinstance(value, list):
            return [FullKBLLMService._sanitize_json_value(v, depth + 1) for v in value[:40]]
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:60]:
                key = str(raw_key)[:80]
                if key.startswith("_"):
                    continue
                sanitized[key] = FullKBLLMService._sanitize_json_value(raw_value, depth + 1)
            return sanitized
        return str(value)[:1000]

    @staticmethod
    def _coerce_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return FullKBLLMService._sanitize_json_value(value) or {}
        return {}

    @staticmethod
    def _coerce_list_of_strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                cleaned.append(text[:80])
        return cleaned[:8]

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "y"}

    @staticmethod
    def _parse_intent(intent_raw: str) -> tuple[IntentType, str]:
        intent_norm = str(intent_raw or "faq").strip().lower().replace(" ", "_")
        mapping = {
            "greeting": IntentType.GREETING,
            "faq": IntentType.FAQ,
            "order_food": IntentType.ORDER_FOOD,
            "food_order": IntentType.ORDER_FOOD,
            "table_booking": IntentType.TABLE_BOOKING,
            "reservation": IntentType.TABLE_BOOKING,
            "room_booking": IntentType.TABLE_BOOKING,
            "stay_booking": IntentType.TABLE_BOOKING,
            "room_service": IntentType.ROOM_SERVICE,
            "human_request": IntentType.HUMAN_REQUEST,
            "confirmation_yes": IntentType.CONFIRMATION_YES,
            "confirmation_no": IntentType.CONFIRMATION_NO,
            "unclear": IntentType.UNCLEAR,
            "out_of_scope": IntentType.OUT_OF_SCOPE,
        }
        return mapping.get(intent_norm, IntentType.FAQ), intent_norm or "faq"

    @staticmethod
    def _parse_state(raw_state: str, fallback: ConversationState) -> ConversationState:
        state_norm = str(raw_state or "").strip().lower().replace("-", "_")
        aliases = {
            "idle": ConversationState.IDLE,
            "awaiting_info": ConversationState.AWAITING_INFO,
            "awaiting_confirmation": ConversationState.AWAITING_CONFIRMATION,
            "awaiting_selection": ConversationState.AWAITING_SELECTION,
            "processing_order": ConversationState.PROCESSING_ORDER,
            "completed": ConversationState.COMPLETED,
            "escalated": ConversationState.ESCALATED,
        }
        return aliases.get(state_norm, fallback)

    def _fallback_result(self, trace_id: str, query: str, current_state: ConversationState) -> FullKBLLMResult:
        return FullKBLLMResult(
            response_text=_FALLBACK_NOT_FOUND,
            normalized_query=self._normalize_query(query),
            intent=IntentType.FAQ,
            raw_intent="faq",
            confidence=0.7,
            next_state=(
                current_state
                if current_state in {ConversationState.AWAITING_INFO, ConversationState.AWAITING_CONFIRMATION}
                else ConversationState.IDLE
            ),
            pending_action=None,
            pending_data={},
            room_number=None,
            suggested_actions=["Ask another question", "Talk to human"],
            trace_id=trace_id,
            llm_output={},
            clear_pending_data=False,
            status="fallback",
        )

    async def run_turn(
        self,
        user_message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        memory_snapshot: dict[str, Any],
    ) -> FullKBLLMResult:
        tenant_id = context.hotel_code or "default"
        trace = self._new_trace(user_message, tenant_id)
        trace_id = str((trace or {}).get("trace_id") or "")
        final_status = "unknown"

        pending_public = {
            key: value
            for key, value in (context.pending_data or {}).items()
            if isinstance(key, str) and not key.startswith("_")
        }
        selected_phase_id, selected_phase_name = self._resolve_selected_phase_context(
            context=context,
            pending_public=pending_public,
            capabilities_summary=capabilities_summary,
        )
        if trace is not None:
            trace["state_before"] = context.state.value
            trace["pending_action_before"] = context.pending_action
            trace["pending_data_before"] = self._sanitize_json_value(pending_public)

        self._trace_step(
            trace,
            step="input_received",
            status="success",
            input_data={
                "message": self._preview_text(user_message),
                "state": context.state.value,
                "pending_action": context.pending_action,
            },
        )

        try:
            normalized_query = self._normalize_query(user_message)
            self._trace_step(
                trace,
                step="normalize_query",
                status="success",
                output_data={"normalized_query": normalized_query},
            )

            kb_text, kb_sources = self._load_full_kb_text(tenant_id=tenant_id)
            self._trace_step(
                trace,
                step="load_full_kb",
                status="success",
                output_data={
                    "sources": kb_sources,
                    "sources_count": len(kb_sources),
                    "kb_chars": len(kb_text),
                },
            )
            if not kb_text:
                final_status = "no_kb_sources"
                return self._fallback_result(trace_id, user_message, context.state)

            if not settings.openai_api_key:
                final_status = "missing_openai_key"
                self._trace_step(
                    trace,
                    step="llm_decision",
                    status="failed",
                    error="openai_api_key_missing",
                )
                return self._fallback_result(trace_id, user_message, context.state)

            history_messages = (
                context.messages
                if self.max_history_messages <= 0
                else context.get_recent_messages(self.max_history_messages)
            )
            recent_history = []
            total_history_chars = 0
            for msg in history_messages:
                content = str(msg.content or "").strip()
                if not content:
                    continue
                max_remaining = self.history_max_chars - total_history_chars
                if max_remaining <= 0:
                    break
                clipped = content[: min(1200, max_remaining)]
                recent_history.append({"role": msg.role.value, "content": clipped})
                total_history_chars += len(clipped)

            memory_facts = memory_snapshot.get("facts", {})
            if not isinstance(memory_facts, dict):
                memory_facts = {}
            memory_recent_changes = memory_snapshot.get("recent_changes", [])
            if not isinstance(memory_recent_changes, list):
                memory_recent_changes = []
            confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm")).strip() or "yes confirm"

            business_name = str(
                capabilities_summary.get("hotel_name")
                or capabilities_summary.get("business_name")
                or "Hotel"
            ).strip() or "Hotel"
            bot_name = str(capabilities_summary.get("bot_name") or "Assistant").strip() or "Assistant"
            city = str(capabilities_summary.get("city") or "").strip()
            business_type = str(capabilities_summary.get("business_type") or "hotel").strip() or "hotel"
            timezone = str(capabilities_summary.get("timezone") or "").strip()
            currency = str(capabilities_summary.get("currency") or "").strip()
            language = str(capabilities_summary.get("language") or "").strip()
            timestamp_format = str(capabilities_summary.get("timestamp_format") or "").strip()
            location = str(capabilities_summary.get("location") or "").strip()
            address = str(capabilities_summary.get("address") or "").strip()
            contact_email = str(capabilities_summary.get("contact_email") or "").strip()
            contact_phone = str(capabilities_summary.get("contact_phone") or "").strip()
            website = str(capabilities_summary.get("website") or "").strip()
            channels = capabilities_summary.get("channels", {})
            if not isinstance(channels, dict):
                channels = {}
            welcome_message = str(capabilities_summary.get("welcome_message") or "").strip()
            knowledge_sources = capabilities_summary.get("knowledge_sources", [])
            if not isinstance(knowledge_sources, list):
                knowledge_sources = []
            knowledge_notes = str(capabilities_summary.get("knowledge_notes") or "").strip()

            prompts_cfg = capabilities_summary.get("prompts", {})
            if not isinstance(prompts_cfg, dict):
                prompts_cfg = {}
            prompt_template_id = str(prompts_cfg.get("template_id") or "").strip()
            configured_system_prompt = str(prompts_cfg.get("system_prompt") or "").strip()
            classifier_prompt = str(prompts_cfg.get("classifier_prompt") or "").strip()
            response_style = str(prompts_cfg.get("response_style") or "").strip()

            placeholder_map = {
                "{business_name}": business_name,
                "{bot_name}": bot_name,
                "{city}": city,
            }
            for token, value in placeholder_map.items():
                configured_system_prompt = configured_system_prompt.replace(token, value)
                classifier_prompt = classifier_prompt.replace(token, value)
                response_style = response_style.replace(token, value)

            nlu_policy = capabilities_summary.get("nlu_policy", {})
            if not isinstance(nlu_policy, dict):
                nlu_policy = {}
            nlu_dos_raw = nlu_policy.get("dos", [])
            nlu_donts_raw = nlu_policy.get("donts", [])
            if not isinstance(nlu_dos_raw, list):
                nlu_dos_raw = []
            if not isinstance(nlu_donts_raw, list):
                nlu_donts_raw = []
            nlu_dos = [str(item or "").strip() for item in nlu_dos_raw if str(item or "").strip()][:30]
            nlu_donts = [str(item or "").strip() for item in nlu_donts_raw if str(item or "").strip()][:30]

            intent_rows = capabilities_summary.get("intents", [])
            if not isinstance(intent_rows, list):
                intent_rows = []
            enabled_intents: list[dict[str, Any]] = []
            for item in intent_rows[:120]:
                if not isinstance(item, dict):
                    continue
                if not bool(item.get("enabled", False)):
                    continue
                intent_id = str(item.get("id") or "").strip()
                if not intent_id:
                    continue
                enabled_intents.append(
                    {
                        "id": intent_id,
                        "label": str(item.get("label") or intent_id).strip(),
                        "maps_to": str(item.get("maps_to") or "").strip(),
                    }
                )

            service_rows = capabilities_summary.get("service_catalog", [])
            if not isinstance(service_rows, list):
                service_rows = []
            service_kb_rows = capabilities_summary.get("service_kb_records", [])
            if not isinstance(service_kb_rows, list):
                service_kb_rows = []
            service_kb_by_service: dict[str, dict[str, Any]] = {}
            for row in service_kb_rows[:200]:
                if not isinstance(row, dict):
                    continue
                kb_service_id = str(row.get("service_id") or "").strip()
                if not kb_service_id:
                    continue
                facts_value = row.get("facts", [])
                approved_facts: list[str] = []
                if isinstance(facts_value, list):
                    for fact in facts_value:
                        if not isinstance(fact, dict):
                            continue
                        status = str(fact.get("status") or "approved").strip().lower()
                        if status not in {"approved", ""}:
                            continue
                        text = str(fact.get("text") or "").strip()
                        if not text:
                            continue
                        approved_facts.append(text[:420])
                service_kb_by_service[kb_service_id] = {
                    "service_id": kb_service_id,
                    "plugin_id": str(row.get("plugin_id") or "").strip(),
                    "strict_mode": bool(row.get("strict_mode", True)),
                    "version": int(row.get("version") or 0),
                    "published_at": str(row.get("published_at") or "").strip(),
                    "published_by": str(row.get("published_by") or "").strip(),
                    "facts": approved_facts[:40],
                    "completeness": self._sanitize_json_value(row.get("completeness", {})),
                }
            admin_services: list[dict[str, Any]] = []
            service_agent_prompts: list[dict[str, Any]] = []
            service_knowledge_packs: list[dict[str, Any]] = []
            included_service_kb_ids: set[str] = set()
            for item in service_rows[:80]:
                if not isinstance(item, dict):
                    continue
                service_id = str(item.get("id") or "").strip()
                if not service_id:
                    continue
                service_phase_id = self._normalize_phase_identifier(item.get("phase_id"))
                service_phase_name = (
                    self._phase_label(service_phase_id, capabilities_summary)
                    if service_phase_id
                    else ""
                )
                ticketing_enabled = bool(item.get("ticketing_enabled", True))
                ticketing_policy = str(item.get("ticketing_policy") or "").strip()
                kb_pack = service_kb_by_service.get(service_id, {})
                kb_facts = kb_pack.get("facts", []) if isinstance(kb_pack, dict) else []
                if not isinstance(kb_facts, list):
                    kb_facts = []
                if kb_pack:
                    service_knowledge_packs.append(kb_pack)
                    included_service_kb_ids.add(service_id)
                admin_services.append(
                    {
                        "id": service_id,
                        "name": str(item.get("name") or service_id).strip(),
                        "type": str(item.get("type") or "service").strip(),
                        "description": str(item.get("description") or "").strip(),
                        "is_active": bool(item.get("is_active", True)),
                        "phase_id": service_phase_id,
                        "phase_name": service_phase_name,
                        "ticketing_enabled": ticketing_enabled,
                        "ticketing_policy": ticketing_policy,
                        "hours": self._sanitize_json_value(item.get("hours", {})),
                        "delivery_zones": self._sanitize_json_value(item.get("delivery_zones", [])),
                        "knowledge_facts": kb_facts[:20],
                    }
                )
                agent_instruction_parts = [
                    f"Service agent: {str(item.get('name') or service_id).strip()} ({service_id}).",
                    "Use only KB/admin-config facts.",
                ]
                if service_phase_name:
                    agent_instruction_parts.append(
                        f"Action scope phase: {service_phase_name} ({service_phase_id})."
                    )
                if ticketing_enabled:
                    agent_instruction_parts.append(
                        "Ticketing can be created only when this service truly needs staff action."
                    )
                else:
                    agent_instruction_parts.append(
                        "Ticketing is disabled for this service: never request ticket creation for it."
                    )
                if ticketing_policy:
                    agent_instruction_parts.append(f"Ticketing policy: {ticketing_policy}")
                if kb_facts:
                    agent_instruction_parts.append(
                        f"Service knowledge pack has {len(kb_facts)} approved facts. "
                        "Use these facts first for service-specific replies."
                    )
                else:
                    agent_instruction_parts.append(
                        "No approved service knowledge-pack facts are available yet; "
                        "if service-specific detail is missing, state it is unavailable."
                    )
                service_agent_prompts.append(
                    {
                        "service_id": service_id,
                        "service_name": str(item.get("name") or service_id).strip(),
                        "instruction": " ".join(agent_instruction_parts).strip(),
                        "knowledge_facts": kb_facts[:20],
                        "strict_mode": bool((kb_pack or {}).get("strict_mode", True)),
                    }
                )
            for kb_service_id, kb_pack in service_kb_by_service.items():
                if kb_service_id in included_service_kb_ids:
                    continue
                service_knowledge_packs.append(kb_pack)

            faq_rows = capabilities_summary.get("faq_bank", [])
            if not isinstance(faq_rows, list):
                faq_rows = []
            admin_faq_bank: list[dict[str, Any]] = []
            for item in faq_rows[:80]:
                if not isinstance(item, dict):
                    continue
                if not bool(item.get("enabled", True)):
                    continue
                question = str(item.get("question") or "").strip()
                answer = str(item.get("answer") or "").strip()
                if not question and not answer:
                    continue
                admin_faq_bank.append(
                    {
                        "id": str(item.get("id") or "").strip(),
                        "question": question,
                        "answer": answer,
                        "description": str(item.get("description") or "").strip(),
                        "tags": self._sanitize_json_value(item.get("tags", [])),
                    }
                )

            tool_rows = capabilities_summary.get("tools", [])
            if not isinstance(tool_rows, list):
                tool_rows = []
            admin_tools: list[dict[str, Any]] = []
            for item in tool_rows[:60]:
                if not isinstance(item, dict):
                    continue
                if not bool(item.get("enabled", True)):
                    continue
                tool_id = str(item.get("id") or "").strip()
                if not tool_id:
                    continue
                admin_tools.append(
                    {
                        "id": tool_id,
                        "name": str(item.get("name") or tool_id).strip(),
                        "type": str(item.get("type") or "workflow").strip(),
                        "description": str(item.get("description") or "").strip(),
                        "handler": str(item.get("handler") or "").strip(),
                        "channels": self._sanitize_json_value(item.get("channels", [])),
                    }
                )
            admin_workflows = [
                tool
                for tool in admin_tools
                if str(tool.get("type") or "").strip().lower() in {"workflow", "handoff", "automation", "plugin"}
            ]
            current_phase_allowed_services: list[dict[str, Any]] = []
            out_of_phase_services: list[dict[str, Any]] = []
            for service in admin_services:
                if not isinstance(service, dict):
                    continue
                if not bool(service.get("is_active", True)):
                    continue
                service_id = str(service.get("id") or "").strip()
                if not service_id:
                    continue
                service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
                service_phase_name = str(service.get("phase_name") or self._phase_label(service_phase_id, capabilities_summary)).strip()
                row = {
                    "id": service_id,
                    "name": str(service.get("name") or service_id).strip(),
                    "phase_id": service_phase_id,
                    "phase_name": service_phase_name,
                    "ticketing_enabled": bool(service.get("ticketing_enabled", True)),
                }
                if service_phase_id == selected_phase_id:
                    current_phase_allowed_services.append(row)
                elif service_phase_id:
                    row["availability_hint"] = self._phase_transition_timing_hint(selected_phase_id, service_phase_id)
                    out_of_phase_services.append(row)
            phase_service_policy = {
                "current_phase_id": selected_phase_id,
                "current_phase_name": selected_phase_name,
                "allowed_services": current_phase_allowed_services[:120],
                "blocked_out_of_phase_services": out_of_phase_services[:180],
            }

            admin_config = {
                "business_profile": {
                    "business_id": str(capabilities_summary.get("business_id") or "").strip(),
                    "business_name": business_name,
                    "bot_name": bot_name,
                    "city": city,
                    "location": location,
                    "address": address,
                    "business_type": business_type,
                    "timezone": timezone,
                    "currency": currency,
                    "language": language,
                    "timestamp_format": timestamp_format,
                    "contact_email": contact_email,
                    "contact_phone": contact_phone,
                    "website": website,
                    "channels": self._sanitize_json_value(channels),
                    "welcome_message": welcome_message,
                },
                "prompt_template_id": prompt_template_id,
                "custom_system_prompt": configured_system_prompt,
                "classifier_prompt": classifier_prompt,
                "response_style": response_style,
                "knowledge_sources": self._sanitize_json_value(knowledge_sources),
                "knowledge_notes": knowledge_notes,
                "nlu_do_rules": nlu_dos,
                "nlu_dont_rules": nlu_donts,
                "enabled_intents": enabled_intents,
                "services": admin_services,
                "service_agent_prompts": service_agent_prompts,
                "service_knowledge_packs": service_knowledge_packs,
                "faq_bank": admin_faq_bank,
                "tools": admin_tools,
                "workflows": admin_workflows,
                "selected_phase": {
                    "id": selected_phase_id,
                    "name": selected_phase_name,
                },
                "phase_service_policy": phase_service_policy,
            }

            llm_input = {
                "user_message": user_message,
                "current_state": context.state.value,
                "pending_action": context.pending_action,
                "pending_data": self._sanitize_json_value(pending_public),
                "selected_phase_id": selected_phase_id,
                "selected_phase_name": selected_phase_name,
                "room_number": context.room_number,
                "recent_history": recent_history,
                "memory_summary": str(memory_snapshot.get("summary") or "")[: self.memory_summary_chars],
                "memory_facts": self._sanitize_json_value(memory_facts),
                "memory_recent_changes": self._sanitize_json_value(memory_recent_changes),
                "confirmation_phrase": confirmation_phrase,
                "capabilities_summary": self._sanitize_json_value(capabilities_summary),
                "admin_config": self._sanitize_json_value(admin_config),
                "hotel_context": {
                    "hotel_name": business_name,
                    "business_name": business_name,
                    "bot_name": bot_name,
                    "city": city,
                    "location": location,
                    "address": address,
                    "business_type": business_type,
                    "timezone": timezone,
                    "currency": currency,
                    "language": language,
                    "welcome_message": welcome_message,
                },
            }

            business_rule_lines: list[str] = []
            if configured_system_prompt:
                business_rule_lines.append("Business system prompt: " + configured_system_prompt)
            if response_style:
                business_rule_lines.append("Response style: " + response_style)
            if classifier_prompt:
                business_rule_lines.append("Classifier guidance: " + classifier_prompt)
            if nlu_dos:
                business_rule_lines.append("NLU DO rules: " + " | ".join(nlu_dos[:12]))
            if nlu_donts:
                business_rule_lines.append("NLU DON'T rules: " + " | ".join(nlu_donts[:12]))
            business_rules_block = "\n".join(business_rule_lines).strip()
            business_rules_section = (
                f"BUSINESS ADMIN RULES (apply strictly):\n{business_rules_block}\n\n"
                if business_rules_block
                else ""
            )

            system_prompt = (
                "You are a strict KB-grounded assistant.\n"
                "Use three evidence sources only:\n"
                "A) KB CONTENT below for business/property facts.\n"
                "B) ADMIN CONFIG from the user JSON for business identity, enabled workflows, prompts, and policy rules.\n"
                "C) conversation memory (memory_summary, memory_facts, memory_recent_changes) for user/session facts.\n"
                "Never invent facts outside these sources.\n"
                "Do not use your own pretrained/world knowledge when answering.\n"
                "If a fact is not present in these sources, explicitly say it is unavailable in the current knowledge base for this property.\n\n"
                f"Business identity to use in replies: assistant='{bot_name}', business='{business_name}', city='{city}'.\n"
                f"Current selected user journey phase: '{selected_phase_name}' ({selected_phase_id}).\n"
                "Treat ADMIN CONFIG as source-of-truth for what is enabled/allowed.\n"
                "If KB text conflicts with ADMIN CONFIG operational settings, prefer ADMIN CONFIG and communicate uncertainty politely.\n\n"
                "Conversation behavior requirements:\n"
                "0) Enforce selected phase context from selected_phase_id/selected_phase_name for action availability and ticketing behavior.\n"
                "0.1) Treat admin_config.phase_service_policy.allowed_services as the only executable services in the current phase.\n"
                "0.2) For services in admin_config.phase_service_policy.blocked_out_of_phase_services: do not execute booking/order/ticket flow.\n"
                "0.3) For out-of-phase asks: provide factual information only (describe the service, hours, pricing if known). "
                "Do NOT promise, invite, offer, or imply the guest can use or book it right now. "
                "Do NOT begin collecting slots or ask booking/order questions for out-of-phase services. "
                "At the END of your response, always add a note such as: 'Please note that booking or ordering for this service is not available in the current phase — it will be available during [phase name].' "
                "Keep tone warm and informative, not transactional.\n"
                "1) Normalize user typos mentally and output normalized_query.\n"
                "2) Understand multi-turn context from current_state, pending_action, pending_data, complete chat history, and memory.\n"
                "2.1) Use service-agent reasoning from admin_config.service_agent_prompts: identify which service agent(s) own each ask, answer each ask with the relevant agent instruction, then return one combined user response.\n"
                "2.2) When an actionable request maps to a configured service, set service_id to that exact admin service id and keep pending_data.service_id aligned.\n"
                "2.2.1) service_id must be an exact value from admin_config.services[].id (never a generic word).\n"
                "2.2.2) For any request that needs service action/routing (including out-of-phase asks), set service_id explicitly. Leave service_id empty only for pure information asks.\n"
                "2.2.3) Service_id is the primary routing signal. Intent label is secondary metadata and must not contradict chosen service_id.\n"
                "2.3) For service-specific facts (timings, amenities, inclusions, pricing, policies), use admin_config.service_knowledge_packs facts for that service first. "
                "If pack facts are missing for that detail, clearly state unavailable instead of guessing.\n"
                "3) Handle flows naturally (ordering, bookings, support, FAQs).\n"
                "4) For non-integrated actions, never fabricate a final system confirmation; mark as forwarded to staff.\n"
                "4.1) If requested info/action is not explicitly available in KB or ADMIN CONFIG, do not ask exploratory follow-up questions.\n"
                "4.2) In that case, clearly say information is unavailable right now and offer human/staff handoff.\n"
                "5) Preserve and update pending_action/pending_data coherently.\n"
                "6) For change/modification requests on an existing reservation/order/profile detail, route to staff (human_request).\n"
                "7) Ask explicit confirmation before any final confirmation action.\n"
                f"8) Treat confirmation_yes as valid only for final confirmation steps, and only when user explicitly types the exact phrase: '{confirmation_phrase}'.\n"
                "9) For stay/room bookings, do not ask final confirmation until required details are collected (room type + guest count + check-in + check-out).\n"
                "10) If user intent is broad (e.g., asks for food/room/table without specifics), proactively offer relevant options from KB.\n"
                "10.1) Never recommend, offer, or list any service/item/workflow unless it is explicitly present in KB CONTENT or ADMIN CONFIG (services/tools/workflows/faq).\n"
                "10.2) Do not suggest generic examples (for example table booking, room service, food ordering, transport) unless those capabilities are explicitly present in KB CONTENT or ADMIN CONFIG.\n"
                "10.3) If a requested service/item is missing, clearly say it is unavailable in the current knowledge base and offer staff handoff.\n"
                "10.4) Keep recommendations scoped to this business/property only; do not recommend other hotels/properties.\n"
                "10.5) For nearby sightseeing/location asks around this hotel, provide the best available nearby guidance from KB/context; if exact detail is missing, state that clearly and still provide useful local direction.\n"
                "10.6) If a user asks multiple questions in one message, answer each ask in one combined response (do not ignore any part).\n"
                "10.7) For out-of-phase services: confirm the hotel offers the service, share factual details (descriptions, inclusions, pricing) from KB, and clearly state when it becomes available. "
                "Do NOT promise or offer to execute the service, do NOT invite booking or ordering, do NOT collect any booking/order slots. Information only.\n"
                "10.8) Never list menus/options as immediately orderable for an out-of-phase service.\n"
                "11) If user chooses one branch of a prior question, advance that branch; do not repeat the same choice question.\n"
                "11.1) During an active booking flow, if the user asks an informational follow-up (for example room options/types/details), answer it from KB first and keep booking pending_action context.\n"
                "11.2) Never block a direct informational answer only because transactional slots are incomplete.\n"
                "11.3) Ask for missing transactional details only after answering the current ask.\n"
                "12) suggested_actions must be user-askable prompts, not bot instructions (avoid 'Provide/Specify/Enter').\n"
                "13) Provide 3-5 short, context-relevant suggested_actions for UI chips.\n"
                "14) If user asks to show/list/recommend/options (including follow-ups like 'show more'), return the complete matching option set from KB, not a sample subset.\n"
                "15) For category asks (for example burger, pizza, red wine, room types), include every matching option present in KB; include prices when available.\n"
                "16) For follow-up 'more' requests, do not repeat already listed items; return only additional unseen options, or explicitly say no additional options are available.\n"
                "17) Keep responses clear. Be concise for normal Q&A, but be exhaustive when user explicitly asks for options/lists/recommendations.\n\n"
                "17.1) If user asks for treatments/packages/options/details, answer directly with concrete details from KB first. Do not respond with another clarifying question unless required data is missing in KB.\n"
                "17.2) Avoid repetitive generic phrasing across turns. If user repeats/clarifies, progress the answer with new specifics.\n\n"
                "17.5) If only one follow-up is needed, ask exactly one concise follow-up question at the end of the answer.\n\n"
                "17.3) Memory/profile updates or recall asks (for example 'remember my room', 'call me Sam') are FAQ context updates, not service actions.\n"
                "17.4) Informational policy/privacy/security asks (for example card data retention) stay FAQ unless user explicitly asks for staff action.\n\n"
                "18) For order_food, behave like a real concierge and collect required order details before final confirmation.\n"
                "19) Mandatory order slots before final confirm: item_name + quantity (portions). If quantity is missing, ask naturally (for example: 'How many portions would you like?').\n"
                "20) For table_booking, collect required booking details before final confirmation: service/restaurant name + party size + booking time (and date when user provides or asks for a specific day).\n"
                "21) If party size is missing, ask naturally (for example: 'Sure, for how many guests?'). If time is missing, ask naturally (for example: 'What time would you like the reservation?').\n"
                "22) Ask one missing detail at a time in natural conversational style. Do not jump directly to final confirmation while required slots are missing.\n"
                "23) If user says yes/no while required details are still missing, continue collecting missing details instead of moving to final confirmation.\n\n"
                "24) Never assume default values for booking time/date or order quantity. If a required slot is missing, ask for it.\n"
                "25) Strict table-booking flow:\n"
                "    - If service is missing: next_state=awaiting_info, pending_action=select_service.\n"
                "    - Else if party size is missing: next_state=awaiting_info, pending_action=collect_booking_party_size.\n"
                "    - Else if time is missing: next_state=awaiting_info, pending_action=collect_booking_time.\n"
                "    - Only when service + party size + time are present: next_state=awaiting_confirmation, pending_action=confirm_booking.\n"
                "26) If user replies with only a number while collecting booking details (for example '2'), treat it as party size and then ask the missing booking time naturally.\n"
                "27) Strict food-order flow: never ask final confirmation until both item and quantity are present.\n"
                "28) Keep booking/order questions human and concise (for example: 'Perfect, table for 2. What time should I book it for?').\n\n"
                "29) Before any final order confirmation, provide a full order summary in natural language.\n"
                "30) Order summary must include: each item name, quantity for each item, add-ons/special requests (if any), and total price if available in KB or context.\n"
                "31) After summary, ask for explicit final confirmation; do not skip directly to confirmed state.\n\n"
                "31.1) Differentiate room_booking vs room_service carefully:\n"
                "    - room_booking: stay/reservation/check-in/check-out/room type/pricing/availability intent.\n"
                "    - room_service: in-stay operational needs (housekeeping, towels, maintenance, amenities, issues).\n\n"
                "31.2) Operational issue reports (for example cockroach, dirty room, broken AC, no water, maintenance/housekeeping complaints)\n"
                "      must not be treated as FAQ. Classify as room_service, complaint-style, or human_request depending on requested action.\n\n"
                "32) Decide ticketing per turn:\n"
                "    - Set requires_ticket=true only when staff follow-up/action is required (complaints, maintenance/service requests, manual booking/order fulfillment, escalations).\n"
                "    - Set requires_ticket=false for pure informational replies or when you still need follow-up details before creating a staff task.\n"
                "    - You may set requires_ticket=true for complaints, room service, order_food, table_booking, room_booking, or faq if human/staff action is required.\n"
                "    - Set ticket_ready_to_create=true only when ticket details are complete and no further user confirmation/details are pending.\n"
                "    - Keep ticket_ready_to_create=false while you are still asking follow-up or confirmation questions.\n"
                "    - Never set requires_ticket=true for a service where admin_config.services[].ticketing_enabled is false.\n"
                "    - If assistant_response says a ticket was created/raised/escalated/forwarded, requires_ticket must be true with a non-empty ticket_reason.\n"
                "    - Provide a short ticket_reason when requires_ticket=true.\n\n"
                "33) Ticketing conversation style:\n"
                "    - Keep chat human and service-first; do not ask users technical ticketing questions.\n"
                "    - For order/table booking, collect required slots and final confirmation first; backend ticket creation happens automatically after confirmation.\n"
                "    - For complaints/room-needs requiring staff action, gather missing operational details naturally (for example room number) and proceed.\n\n"
                f"{business_rules_section}"
                "Evidence policy:\n"
                "- If the user asks about their own previously shared details, use memory facts.\n"
                "- If business fact is missing from KB, say it is unavailable in current knowledge base.\n"
                "- Never answer from external/common knowledge; only use KB CONTENT, ADMIN CONFIG, and memory.\n"
                "- Do not infer or advertise services from industry defaults; only use services/tools explicitly present in KB CONTENT or ADMIN CONFIG.\n"
                "- If asked about other hotels/competitors, politely decline and refocus on this business.\n"
                "- When business fact is missing, prefer offering staff handoff over asking extra qualifying questions.\n"
                "- If business identity/style/guardrail is missing in KB but present in ADMIN CONFIG, use ADMIN CONFIG.\n"
                "- If user/session fact is missing from memory, ask the user to provide it again.\n\n"
                "State values allowed: idle, awaiting_info, awaiting_selection, awaiting_confirmation, processing_order, completed, escalated.\n"
                "Intent values allowed: greeting, faq, order_food, table_booking, room_booking, room_service, human_request, confirmation_yes, confirmation_no, unclear.\n\n"
                "Return ONLY a JSON object with keys:\n"
                "{\n"
                '  "normalized_query": "...",\n'
                '  "intent": "...",\n'
                '  "confidence": 0.0,\n'
                '  "next_state": "...",\n'
                '  "pending_action": null,\n'
                '  "pending_data": {},\n'
                '  "pending_data_updates": {},\n'
                '  "clear_pending_data": false,\n'
                '  "room_number": null,\n'
                '  "service_id": "",\n'
                '  "answered_current_query": true,\n'
                '  "blocking_fields": [],\n'
                '  "deferrable_fields": [],\n'
                '  "requires_ticket": false,\n'
                '  "ticket_ready_to_create": false,\n'
                '  "ticket_reason": "",\n'
                '  "ticket_category": "",\n'
                '  "ticket_sub_category": "",\n'
                '  "ticket_priority": "",\n'
                '  "ticket_issue": "",\n'
                '  "suggested_actions": ["..."],\n'
                '  "assistant_response": "..."\n'
                "}\n\n"
                f"KB CONTENT START\n{kb_text}\nKB CONTENT END"
            )

            self._trace_step(
                trace,
                step="build_prompt",
                status="success",
                output_data={
                    "history_count": len(recent_history),
                    "pending_action": context.pending_action,
                    "kb_chars": len(kb_text),
                    "admin_services_count": len(admin_services),
                    "admin_faq_count": len(admin_faq_bank),
                    "admin_tools_count": len(admin_tools),
                    "admin_intents_count": len(enabled_intents),
                },
            )

            llm_json = await llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(llm_input, ensure_ascii=False)},
                ],
                temperature=self.temperature,
            )
            if not isinstance(llm_json, dict):
                llm_json = {}

            normalized_from_llm = str(
                llm_json.get("normalized_query")
                or llm_json.get("rewritten_query")
                or normalized_query
            ).strip()

            response_text = str(
                llm_json.get("assistant_response")
                or llm_json.get("response")
                or llm_json.get("answer")
                or ""
            ).strip()
            if not response_text:
                response_text = _FALLBACK_NOT_FOUND

            intent_value = str(llm_json.get("intent") or "faq")
            intent, raw_intent = self._parse_intent(intent_value)
            confidence = float(llm_json.get("confidence") or 0.75)
            confidence = max(0.0, min(1.0, confidence))

            next_state = self._parse_state(str(llm_json.get("next_state") or ""), context.state)
            pending_action_value = llm_json.get("pending_action")
            pending_action = str(pending_action_value).strip() if pending_action_value is not None else None
            if pending_action == "":
                pending_action = None
            clear_pending_data = self._coerce_bool(llm_json.get("clear_pending_data"))

            pending_data = self._coerce_dict(llm_json.get("pending_data"))
            if not pending_data and isinstance(llm_json.get("pending_data_updates"), dict):
                updates = self._coerce_dict(llm_json.get("pending_data_updates"))
                merged = dict(self._coerce_dict(pending_public))
                merged.update(updates)
                pending_data = merged
            if clear_pending_data:
                pending_data = {}

            room_number_raw = llm_json.get("room_number")
            room_number = str(room_number_raw).strip() if room_number_raw is not None else None
            if room_number == "":
                room_number = None

            suggested_actions = self._coerce_list_of_strings(llm_json.get("suggested_actions"))

            self._trace_step(
                trace,
                step="llm_decision",
                status="success",
                output_data={
                    "intent": raw_intent,
                    "confidence": confidence,
                    "next_state": next_state.value,
                    "pending_action": pending_action,
                    "clear_pending_data": clear_pending_data,
                    "assistant_response_preview": self._preview_text(response_text),
                },
            )

            final_status = "success"
            return FullKBLLMResult(
                response_text=response_text,
                normalized_query=normalized_from_llm,
                intent=intent,
                raw_intent=raw_intent,
                confidence=confidence,
                next_state=next_state,
                pending_action=pending_action,
                pending_data=pending_data,
                room_number=room_number,
                suggested_actions=suggested_actions,
                trace_id=trace_id,
                llm_output=self._coerce_dict(llm_json),
                clear_pending_data=clear_pending_data,
                status="success",
            )
        except Exception as exc:
            final_status = "error"
            self._trace_step(
                trace,
                step="llm_decision",
                status="failed",
                error=str(exc),
            )
            return self._fallback_result(trace_id, user_message, context.state)
        finally:
            self._write_trace(trace, final_status=final_status)


full_kb_llm_service = FullKBLLMService()
