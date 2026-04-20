"""
Suggestion Prefetch Service

Speculatively generates answer candidates for suggestion chips and serves them
through semantic matching on the next user turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

from config.settings import settings
from llm.client import llm_client
from schemas.chat import ConversationContext, ConversationState, MessageRole
from services.config_service import config_service
from services.llm_orchestration_service import llm_orchestration_service
from services.observability_service import observability_service


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _coerce_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed):
        return default
    return parsed


@dataclass
class PrefetchEntry:
    entry_id: str
    chip_text: str
    status: str = "queued"  # queued|running|ready|failed
    embedding: list[float] = field(default_factory=list)
    answer_payload: dict[str, Any] = field(default_factory=dict)
    similarity_hint: float = 0.0
    consumed: bool = False
    error: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)


@dataclass
class PrefetchBatch:
    batch_id: str
    session_id: str
    hotel_code: str
    assistant_turn_id: str
    phase_id: str
    conversation_state: str
    last_bot_message: str
    triggering_user_message: str
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    active_service: dict[str, Any] = field(default_factory=dict)
    services_allowed_in_phase: list[dict[str, Any]] = field(default_factory=list)
    knowledge_base_excerpt: dict[str, str] = field(default_factory=dict)
    capabilities_summary: dict[str, Any] = field(default_factory=dict)
    memory_snapshot: dict[str, Any] = field(default_factory=dict)
    selected_phase_context: dict[str, Any] = field(default_factory=dict)
    pending_action: str = ""
    pending_data_public: dict[str, Any] = field(default_factory=dict)
    known_context: dict[str, str] = field(default_factory=dict)
    config_fingerprint: str = ""
    context_fingerprint: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    expires_at: datetime = field(default_factory=_utc_now)
    entries: dict[str, PrefetchEntry] = field(default_factory=dict)


class SuggestionPrefetchService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._batches: dict[str, PrefetchBatch] = {}
        self._session_batches: dict[str, list[str]] = {}
        parallelism = max(1, int(getattr(settings, "chat_suggestion_prefetch_parallelism", 3) or 3))
        self._semaphore = asyncio.Semaphore(parallelism)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        a_sq = 0.0
        b_sq = 0.0
        for va, vb in zip(a, b):
            dot += va * vb
            a_sq += va * va
            b_sq += vb * vb
        if a_sq <= 0.0 or b_sq <= 0.0:
            return 0.0
        return max(-1.0, min(1.0, dot / (math.sqrt(a_sq) * math.sqrt(b_sq))))

    @staticmethod
    def _dedupe_suggestions(suggestions: list[str], limit: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in suggestions:
            text = " ".join(str(item or "").strip().split())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
            if len(cleaned) >= limit:
                break
        return cleaned

    @staticmethod
    def _coerce_state(value: str) -> ConversationState:
        raw = str(value or "").strip().lower()
        for state in ConversationState:
            if state.value == raw:
                return state
        return ConversationState.IDLE

    @staticmethod
    def _coerce_role(value: str) -> MessageRole:
        raw = str(value or "").strip().lower()
        if raw == MessageRole.USER.value:
            return MessageRole.USER
        if raw == MessageRole.ASSISTANT.value:
            return MessageRole.ASSISTANT
        return MessageRole.SYSTEM

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
        if depth > 6:
            return str(value)[:800]
        if value is None:
            return None
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return str(value)[:1200]
        if isinstance(value, list):
            return [SuggestionPrefetchService._safe_json_value(item, depth=depth + 1) for item in value[:80]]
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in list(value.items())[:80]:
                result[str(key)[:120]] = SuggestionPrefetchService._safe_json_value(item, depth=depth + 1)
            return result
        return str(value)[:1200]

    @staticmethod
    def _fingerprint(payload: Any) -> str:
        serialized = json.dumps(
            SuggestionPrefetchService._safe_json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    def _build_config_fingerprint(
        self,
        *,
        hotel_code: str,
        capabilities_summary: dict[str, Any] | None,
    ) -> str:
        cap = dict(capabilities_summary or {})
        faq_signature: list[dict[str, Any]] = []
        for faq in cap.get("faq_bank", []) if isinstance(cap.get("faq_bank", []), list) else []:
            if not isinstance(faq, dict):
                continue
            if not bool(faq.get("enabled", True)):
                continue
            faq_signature.append(
                {
                    "id": self._normalize_identifier(faq.get("id")),
                    "question": str(faq.get("question") or "").strip(),
                    "answer": str(faq.get("answer") or "").strip(),
                    "tags": [str(tag).strip().lower() for tag in (faq.get("tags", []) or []) if str(tag).strip()][:12],
                }
            )
            if len(faq_signature) >= 200:
                break

        service_signature: list[dict[str, Any]] = []
        catalog = cap.get("service_catalog", [])
        if isinstance(catalog, list):
            for row in catalog:
                if not isinstance(row, dict):
                    continue
                service_signature.append(
                    {
                        "id": self._normalize_identifier(row.get("id")),
                        "name": str(row.get("name") or "").strip(),
                        "type": str(row.get("type") or "").strip().lower(),
                        "phase_id": self._normalize_identifier(row.get("phase_id")),
                        "ticketing_enabled": bool(row.get("ticketing_enabled", True)),
                        "ticketing_mode": str(row.get("ticketing_mode") or "").strip().lower(),
                        "is_active": bool(row.get("is_active", True)),
                    }
                )
                if len(service_signature) >= 240:
                    break

        payload = {
            "hotel_code": self._normalize_identifier(hotel_code),
            "business_id": self._normalize_identifier(cap.get("business_id")),
            "hotel_name": str(cap.get("hotel_name") or "").strip(),
            "expected_property_count": int(cap.get("expected_property_count") or 0),
            "knowledge_sources": [
                str(src).strip()
                for src in (cap.get("knowledge_sources", []) or [])
                if str(src).strip()
            ][:120],
            "faq_signature": faq_signature,
            "service_signature": service_signature,
        }
        return self._fingerprint(payload)

    def _build_context_fingerprint(
        self,
        *,
        batch: PrefetchBatch,
    ) -> str:
        active_service = batch.active_service if isinstance(batch.active_service, dict) else {}
        active_service_id = self._normalize_identifier(active_service.get("id") or active_service.get("service_id"))
        allowed_ids: list[str] = []
        for row in batch.services_allowed_in_phase if isinstance(batch.services_allowed_in_phase, list) else []:
            if not isinstance(row, dict):
                continue
            sid = self._normalize_identifier(row.get("id") or row.get("service_id"))
            if sid:
                allowed_ids.append(sid)
        kb_excerpt = batch.knowledge_base_excerpt if isinstance(batch.knowledge_base_excerpt, dict) else {}
        payload = {
            "session_id": str(batch.session_id or "").strip(),
            "hotel_code": self._normalize_identifier(batch.hotel_code),
            "assistant_turn_id": str(batch.assistant_turn_id or "").strip(),
            "phase_id": self._normalize_identifier(batch.phase_id),
            "conversation_state": self._normalize_identifier(batch.conversation_state),
            "pending_action": self._normalize_identifier(batch.pending_action),
            "active_service_id": active_service_id,
            "allowed_service_ids": sorted(set(allowed_ids)),
            "known_context": batch.known_context if isinstance(batch.known_context, dict) else {},
            "pending_data_public": batch.pending_data_public if isinstance(batch.pending_data_public, dict) else {},
            "last_bot_message": _normalize_text(batch.last_bot_message),
            "triggering_user_message": _normalize_text(batch.triggering_user_message),
            "history_tail": batch.conversation_history[-6:] if isinstance(batch.conversation_history, list) else [],
            "kb_excerpt_hash": self._fingerprint(
                {
                    "full_kb_text": str(kb_excerpt.get("full_kb_text") or "").strip(),
                    "service_knowledge_corpus": str(kb_excerpt.get("service_knowledge_corpus") or "").strip(),
                }
            ),
            "config_fingerprint": str(batch.config_fingerprint or "").strip(),
        }
        return self._fingerprint(payload)

    def _build_runtime_context(self, batch: PrefetchBatch) -> ConversationContext:
        known_context = batch.known_context if isinstance(batch.known_context, dict) else {}
        context = ConversationContext(
            session_id=str(batch.session_id or "").strip(),
            hotel_code=str(batch.hotel_code or "").strip() or "default",
            guest_name=str(known_context.get("guest_name") or "").strip() or None,
            room_number=str(known_context.get("room_number") or "").strip() or None,
            state=self._coerce_state(batch.conversation_state),
            pending_action=str(batch.pending_action or "").strip() or None,
            pending_data=(
                dict(batch.pending_data_public)
                if isinstance(batch.pending_data_public, dict)
                else {}
            ),
            channel="web",
        )

        history_rows = batch.conversation_history if isinstance(batch.conversation_history, list) else []
        for row in history_rows[-20:]:
            if not isinstance(row, dict):
                continue
            role = self._coerce_role(str(row.get("role") or ""))
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            context.add_message(role, content)

        return context

    async def _generate_with_orchestration_flow(
        self,
        *,
        batch: PrefetchBatch,
        chip_text: str,
    ) -> str:
        context = self._build_runtime_context(batch)

        capabilities_summary = (
            dict(batch.capabilities_summary)
            if isinstance(batch.capabilities_summary, dict)
            else {}
        )
        if not capabilities_summary:
            capabilities_summary = config_service.get_capability_summary(batch.hotel_code)

        memory_snapshot = (
            dict(batch.memory_snapshot)
            if isinstance(batch.memory_snapshot, dict)
            else {"summary": "", "facts": {}, "recent_changes": []}
        )
        selected_phase_context = (
            dict(batch.selected_phase_context)
            if isinstance(batch.selected_phase_context, dict)
            else {}
        )
        if not selected_phase_context:
            selected_phase_context = {
                "selected_phase_id": str(batch.phase_id or "").strip(),
                "selected_phase_name": str(batch.phase_id or "").strip().replace("_", " ").title(),
            }

        decision = await llm_orchestration_service.orchestrate_turn(
            user_message=str(chip_text or "").strip(),
            context=context,
            capabilities_summary=capabilities_summary,
            memory_snapshot=memory_snapshot,
            selected_phase_context=selected_phase_context,
        )
        if decision is None:
            return ""
        return str(decision.response_text or "").strip()

    def _collect_ready_candidates(
        self,
        *,
        session_batch_ids: list[str],
        preferred_batch_id: str,
    ) -> list[tuple[PrefetchBatch, PrefetchEntry]]:
        if preferred_batch_id:
            ordered_ids = [preferred_batch_id] + [bid for bid in session_batch_ids if bid != preferred_batch_id]
        else:
            ordered_ids = list(reversed(session_batch_ids))

        candidates: list[tuple[PrefetchBatch, PrefetchEntry]] = []
        for candidate_batch_id in ordered_ids:
            batch = self._batches.get(candidate_batch_id)
            if batch is None:
                continue
            for entry in batch.entries.values():
                if entry.status == "ready" and not entry.consumed and entry.answer_payload:
                    candidates.append((batch, entry))
        return candidates

    async def _embed_text(self, text: str) -> list[float]:
        if not str(settings.openai_api_key or "").strip():
            return []
        content = str(text or "").strip()
        if not content:
            return []
        try:
            response = await llm_client.raw_embeddings_create(
                model=str(getattr(settings, "openai_embedding_model", "") or "text-embedding-3-small"),
                input=[content],
                trace_context={
                    "component": "suggestion_prefetch_embedding",
                },
                purpose="Create embedding for suggestion-chip semantic match",
            )
            if not getattr(response, "data", None):
                return []
            vector = getattr(response.data[0], "embedding", None)
            if isinstance(vector, list):
                return [float(v) for v in vector]
            return []
        except Exception as exc:
            observability_service.log_event(
                "suggestion_prefetch_embedding_failed",
                {"error": str(exc)},
            )
            return []

    async def _generate_answer_for_entry(self, batch_id: str, entry_id: str) -> None:
        async with self._semaphore:
            async with self._lock:
                batch = self._batches.get(batch_id)
                if batch is None or _utc_now() >= batch.expires_at:
                    return
                entry = batch.entries.get(entry_id)
                if entry is None:
                    return
                if entry.status not in {"queued", "failed"}:
                    return
                entry.status = "running"
                entry.updated_at = _utc_now()

                phase_service_names = [
                    str(row.get("name") or "").strip()
                    for row in (batch.services_allowed_in_phase or [])
                    if isinstance(row, dict) and str(row.get("name") or "").strip()
                ][:12]
                generation_payload = {
                    "chip_text": entry.chip_text,
                    "last_bot_message": batch.last_bot_message,
                    "triggering_user_message": batch.triggering_user_message,
                    "conversation_state": batch.conversation_state,
                    "phase_id": batch.phase_id,
                    "active_service": batch.active_service,
                    "phase_services": phase_service_names,
                    "history": batch.conversation_history[-8:],
                    "knowledge_excerpt": batch.knowledge_base_excerpt,
                }

            chip_embedding = await self._embed_text(entry.chip_text)
            if not chip_embedding:
                async with self._lock:
                    current_batch = self._batches.get(batch_id)
                    if current_batch is None:
                        return
                    current_entry = current_batch.entries.get(entry_id)
                    if current_entry is None:
                        return
                    current_entry.status = "failed"
                    current_entry.error = "embedding_unavailable"
                    current_entry.updated_at = _utc_now()
                return

            model_name = str(getattr(settings, "chat_suggestion_prefetch_model", "") or "").strip() or None
            temperature = _coerce_float(getattr(settings, "chat_suggestion_prefetch_temperature", 0.2), 0.2)
            max_tokens = int(getattr(settings, "chat_suggestion_prefetch_max_tokens", 420) or 420)

            answer_text = ""
            try:
                answer_text = await self._generate_with_orchestration_flow(
                    batch=batch,
                    chip_text=entry.chip_text,
                )
            except Exception as exc:
                observability_service.log_event(
                    "suggestion_prefetch_orchestration_failed",
                    {"batch_id": batch_id, "entry_id": entry_id, "error": str(exc)},
                )
                answer_text = ""

            if not str(answer_text or "").strip():
                system_prompt = (
                    "You are a hotel concierge assistant.\n"
                    "Generate the exact assistant reply for the guest message in `chip_text`.\n"
                    "Rules:\n"
                    "1. Be grounded in provided conversation and knowledge excerpt.\n"
                    "2. If details are uncertain or missing, say so briefly and provide the safest helpful guidance.\n"
                    "3. Do not invent live inventory, prices, or external-system confirmations.\n"
                    "4. For room-related answers, lead with unique experiences/perks guests care about; do not lead with square-foot specs unless explicitly asked.\n"
                    "5. Keep answer concise and directly useful.\n"
                    "Return plain text only."
                )
                try:
                    answer_text = await llm_client.chat(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(generation_payload, ensure_ascii=False)},
                        ],
                        model=model_name,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        trace_context={
                            "component": "suggestion_prefetch",
                            "batch_id": batch_id,
                            "entry_id": entry_id,
                            "session_id": batch.session_id,
                        },
                    )
                except Exception as exc:
                    answer_text = ""
                    observability_service.log_event(
                        "suggestion_prefetch_generation_failed",
                        {"batch_id": batch_id, "entry_id": entry_id, "error": str(exc)},
                    )

            if not str(answer_text or "").strip():
                answer_text = ""

            cleaned_answer = str(answer_text or "").strip()
            if not cleaned_answer:
                cleaned_answer = "I can help with that. Could you share one more detail so I can answer accurately?"

            payload = {
                "message": cleaned_answer,
                "display_message": cleaned_answer,
                "state": batch.conversation_state or "idle",
                "suggested_actions": [],
                "metadata": {
                    "response_source": "suggestion_prefetch_cache",
                    "prefetch_batch_id": batch.batch_id,
                    "prefetch_entry_id": entry.entry_id,
                    "prefetch_chip_text": entry.chip_text,
                    "prefetch_speculative": True,
                    "prefetch_config_fingerprint": str(batch.config_fingerprint or "").strip(),
                    "prefetch_context_fingerprint": str(batch.context_fingerprint or "").strip(),
                },
            }

            async with self._lock:
                current_batch = self._batches.get(batch_id)
                if current_batch is None:
                    return
                current_entry = current_batch.entries.get(entry_id)
                if current_entry is None:
                    return
                current_entry.embedding = chip_embedding
                current_entry.answer_payload = payload
                current_entry.status = "ready"
                current_entry.error = ""
                current_entry.updated_at = _utc_now()

            observability_service.log_event(
                "suggestion_prefetch_ready",
                {
                    "batch_id": batch_id,
                    "entry_id": entry_id,
                    "session_id": batch.session_id,
                },
            )

    async def _cleanup_expired(self) -> None:
        now = _utc_now()
        expired_batch_ids = [
            batch_id
            for batch_id, batch in self._batches.items()
            if now >= batch.expires_at
        ]
        if not expired_batch_ids:
            return
        for batch_id in expired_batch_ids:
            batch = self._batches.pop(batch_id, None)
            if batch is None:
                continue
            bucket = self._session_batches.get(batch.session_id, [])
            if batch_id in bucket:
                bucket.remove(batch_id)
            if not bucket:
                self._session_batches.pop(batch.session_id, None)

    async def schedule_batch(
        self,
        *,
        session_id: str,
        hotel_code: str,
        assistant_turn_id: str,
        phase_id: str,
        conversation_state: str,
        last_bot_message: str,
        triggering_user_message: str,
        suggestions: list[str],
        conversation_history: list[dict[str, str]] | None = None,
        active_service: dict[str, Any] | None = None,
        services_allowed_in_phase: list[dict[str, Any]] | None = None,
        knowledge_base_excerpt: dict[str, str] | None = None,
        capabilities_summary: dict[str, Any] | None = None,
        memory_snapshot: dict[str, Any] | None = None,
        selected_phase_context: dict[str, Any] | None = None,
        pending_action: str = "",
        pending_data_public: dict[str, Any] | None = None,
        known_context: dict[str, str] | None = None,
    ) -> Optional[str]:
        if not bool(getattr(settings, "chat_suggestion_prefetch_enabled", True)):
            return None
        if not str(settings.openai_api_key or "").strip():
            return None

        cleaned_suggestions = self._dedupe_suggestions(
            list(suggestions or []),
            limit=max(1, int(getattr(settings, "chat_suggestion_prefetch_count", 3) or 3)),
        )
        if not cleaned_suggestions:
            return None

        ttl_seconds = max(30, int(getattr(settings, "chat_suggestion_prefetch_ttl_seconds", 300) or 300))
        max_batches = max(1, int(getattr(settings, "chat_suggestion_prefetch_max_batches_per_session", 6) or 6))
        kb_chars = max(1200, int(getattr(settings, "chat_suggestion_prefetch_max_kb_chars", 12000) or 12000))
        full_kb = str((knowledge_base_excerpt or {}).get("full_kb_text") or "").strip()
        service_kb = str((knowledge_base_excerpt or {}).get("service_knowledge_corpus") or "").strip()
        history_rows = conversation_history if isinstance(conversation_history, list) else []
        known_context_map = {
            str(k): str(v)
            for k, v in dict(known_context or {}).items()
            if str(k).strip() and str(v).strip()
        }
        effective_cap_summary = (
            dict(capabilities_summary)
            if isinstance(capabilities_summary, dict)
            else {}
        )
        if not effective_cap_summary:
            try:
                effective_cap_summary = config_service.get_capability_summary(hotel_code)
            except Exception:
                effective_cap_summary = {}

        batch_id = f"pf_{uuid4().hex}"
        entries = {
            f"e_{idx+1}": PrefetchEntry(entry_id=f"e_{idx+1}", chip_text=chip)
            for idx, chip in enumerate(cleaned_suggestions)
        }
        batch = PrefetchBatch(
            batch_id=batch_id,
            session_id=str(session_id or "").strip(),
            hotel_code=str(hotel_code or "").strip(),
            assistant_turn_id=str(assistant_turn_id or "").strip(),
            phase_id=str(phase_id or "").strip(),
            conversation_state=str(conversation_state or "").strip().lower() or "idle",
            last_bot_message=str(last_bot_message or "").strip(),
            triggering_user_message=str(triggering_user_message or "").strip(),
            conversation_history=[row for row in history_rows if isinstance(row, dict)][-10:],
            active_service=dict(active_service or {}),
            services_allowed_in_phase=[
                row
                for row in (services_allowed_in_phase or [])
                if isinstance(row, dict)
            ][:18],
            knowledge_base_excerpt={
                "full_kb_text": full_kb[:kb_chars],
                "service_knowledge_corpus": service_kb[:kb_chars],
            },
            capabilities_summary=effective_cap_summary,
            memory_snapshot=(
                dict(memory_snapshot)
                if isinstance(memory_snapshot, dict)
                else {}
            ),
            selected_phase_context=(
                dict(selected_phase_context)
                if isinstance(selected_phase_context, dict)
                else {}
            ),
            pending_action=str(pending_action or "").strip(),
            pending_data_public=(
                dict(pending_data_public)
                if isinstance(pending_data_public, dict)
                else {}
            ),
            known_context=known_context_map,
            expires_at=_utc_now() + timedelta(seconds=ttl_seconds),
            entries=entries,
        )
        batch.config_fingerprint = self._build_config_fingerprint(
            hotel_code=batch.hotel_code,
            capabilities_summary=effective_cap_summary,
        )
        batch.context_fingerprint = self._build_context_fingerprint(batch=batch)

        async with self._lock:
            await self._cleanup_expired()
            self._batches[batch_id] = batch
            session_key = batch.session_id
            bucket = self._session_batches.setdefault(session_key, [])
            bucket.append(batch_id)
            if len(bucket) > max_batches:
                old_ids = list(bucket[:-max_batches])
                for old_id in old_ids:
                    old_batch = self._batches.pop(old_id, None)
                    if old_batch is None:
                        continue
                    if old_id in bucket:
                        bucket.remove(old_id)
            if not bucket:
                self._session_batches.pop(session_key, None)

        observability_service.log_event(
            "suggestion_prefetch_scheduled",
            {
                "session_id": batch.session_id,
                "batch_id": batch_id,
                "count": len(entries),
                "assistant_turn_id": batch.assistant_turn_id,
            },
        )

        for entry_id in entries.keys():
            asyncio.create_task(self._generate_answer_for_entry(batch_id, entry_id))

        return batch_id

    async def resolve_cached_answer(
        self,
        *,
        session_id: str,
        user_message: str,
        source_type: str = "",
        batch_id: str = "",
        similarity_threshold: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        if not bool(getattr(settings, "chat_suggestion_prefetch_enabled", True)):
            return None
        if not str(settings.openai_api_key or "").strip():
            return None

        normalized_source = str(source_type or "").strip().lower()
        normalized_query = _normalize_text(user_message)
        if not normalized_query:
            return None

        threshold = (
            _coerce_float(similarity_threshold, 0.9)
            if similarity_threshold is not None
            else _coerce_float(
                getattr(settings, "chat_suggestion_prefetch_similarity_threshold", 0.90),
                0.90,
            )
        )
        threshold = max(0.5, min(0.99, threshold))
        latest_context_fingerprint = ""

        async with self._lock:
            await self._cleanup_expired()
            session_key = str(session_id or "").strip()
            if not session_key:
                return None
            session_batch_ids = list(self._session_batches.get(session_key, []))
            if not session_batch_ids:
                return None
            latest_batch = self._batches.get(session_batch_ids[-1])
            latest_context_fingerprint = (
                str(latest_batch.context_fingerprint or "").strip()
                if latest_batch is not None
                else ""
            )
            preferred_batch_id = str(batch_id or "").strip()
            candidates = self._collect_ready_candidates(
                session_batch_ids=session_batch_ids,
                preferred_batch_id=preferred_batch_id,
            )

        if not candidates:
            wait_ms = max(0, int(getattr(settings, "chat_suggestion_prefetch_wait_ms", 220) or 220))
            if wait_ms <= 0 or not str(batch_id or "").strip():
                return None
            await asyncio.sleep(wait_ms / 1000.0)
            async with self._lock:
                await self._cleanup_expired()
                session_key = str(session_id or "").strip()
                session_batch_ids = list(self._session_batches.get(session_key, []))
                if not session_batch_ids:
                    return None
                latest_batch = self._batches.get(session_batch_ids[-1])
                latest_context_fingerprint = (
                    str(latest_batch.context_fingerprint or "").strip()
                    if latest_batch is not None
                    else ""
                )
                candidates = self._collect_ready_candidates(
                    session_batch_ids=session_batch_ids,
                    preferred_batch_id=str(batch_id or "").strip(),
                )
            if not candidates:
                return None

        if latest_context_fingerprint:
            context_filtered = [
                (candidate_batch, candidate_entry)
                for candidate_batch, candidate_entry in candidates
                if str(candidate_batch.context_fingerprint or "").strip() == latest_context_fingerprint
            ]
            if context_filtered:
                candidates = context_filtered
            else:
                observability_service.log_event(
                    "suggestion_prefetch_context_stale",
                    {
                        "session_id": str(session_id or "").strip(),
                        "source_type": normalized_source,
                        "preferred_batch_id": str(batch_id or "").strip(),
                    },
                )
                return None

        exact_best: Optional[tuple[PrefetchBatch, PrefetchEntry]] = None
        for batch, entry in candidates:
            if _normalize_text(entry.chip_text) == normalized_query:
                exact_best = (batch, entry)
                break

        best_batch: Optional[PrefetchBatch] = None
        best_entry: Optional[PrefetchEntry] = None
        best_similarity = 0.0

        if exact_best is not None:
            best_batch, best_entry = exact_best
            best_similarity = 1.0
        else:
            query_embedding = await self._embed_text(user_message)
            if not query_embedding:
                return None
            for batch, entry in candidates:
                if not entry.embedding:
                    continue
                score = self._cosine_similarity(query_embedding, entry.embedding)
                if score > best_similarity:
                    best_similarity = score
                    best_batch = batch
                    best_entry = entry

        if best_batch is None or best_entry is None:
            return None

        if best_similarity < threshold:
            observability_service.log_event(
                "suggestion_prefetch_miss",
                {
                    "session_id": best_batch.session_id,
                    "batch_id": best_batch.batch_id,
                    "entry_id": best_entry.entry_id,
                    "similarity": round(best_similarity, 4),
                    "threshold": threshold,
                    "source_type": normalized_source,
                },
            )
            return None

        expected_config_fingerprint = str(best_batch.config_fingerprint or "").strip()
        current_config_fingerprint = ""
        if expected_config_fingerprint:
            try:
                current_cap_summary = config_service.get_capability_summary(best_batch.hotel_code)
                current_config_fingerprint = self._build_config_fingerprint(
                    hotel_code=best_batch.hotel_code,
                    capabilities_summary=current_cap_summary,
                )
            except Exception:
                current_config_fingerprint = ""
            if current_config_fingerprint and current_config_fingerprint != expected_config_fingerprint:
                observability_service.log_event(
                    "suggestion_prefetch_config_stale",
                    {
                        "session_id": best_batch.session_id,
                        "batch_id": best_batch.batch_id,
                        "entry_id": best_entry.entry_id,
                        "source_type": normalized_source,
                    },
                )
                return None

        async with self._lock:
            latest_batch = self._batches.get(best_batch.batch_id)
            if latest_batch is None:
                return None
            if latest_context_fingerprint and str(latest_batch.context_fingerprint or "").strip() != latest_context_fingerprint:
                return None
            if (
                expected_config_fingerprint
                and current_config_fingerprint
                and str(latest_batch.config_fingerprint or "").strip() != current_config_fingerprint
            ):
                return None
            latest_entry = latest_batch.entries.get(best_entry.entry_id)
            if latest_entry is None or latest_entry.consumed:
                return None
            latest_entry.consumed = True
            latest_entry.similarity_hint = best_similarity
            latest_entry.updated_at = _utc_now()
            payload = dict(latest_entry.answer_payload or {})
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                payload["metadata"] = metadata
            metadata["prefetch_batch_id"] = latest_batch.batch_id
            metadata["prefetch_entry_id"] = latest_entry.entry_id
            metadata["prefetch_chip_text"] = latest_entry.chip_text
            metadata["prefetch_similarity"] = best_similarity
            metadata["prefetch_match_mode"] = "exact" if best_similarity >= 0.999 else "semantic"
            metadata["prefetch_threshold"] = threshold
            metadata["prefetch_context_fingerprint"] = str(latest_batch.context_fingerprint or "").strip()
            metadata["prefetch_config_fingerprint"] = str(latest_batch.config_fingerprint or "").strip()

        observability_service.log_event(
            "suggestion_prefetch_hit",
            {
                "session_id": best_batch.session_id,
                "batch_id": best_batch.batch_id,
                "entry_id": best_entry.entry_id,
                "similarity": round(best_similarity, 4),
                "source_type": normalized_source,
            },
        )
        return payload


suggestion_prefetch_service = SuggestionPrefetchService()
